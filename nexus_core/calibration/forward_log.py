"""前向蒐集報告：讀取 production 資料庫快照中的 regime_evaluation_log +
regime_evaluation_outcome，分析 GEX 相關條件的實際效果。

使用方式 (先把 VPS 上的 DB 以 .backup 複製下來，絕不直接讀 production 檔)：

    # VPS 上：
    sqlite3 data/nexus_data.db ".backup /tmp/snapshot.db"
    # 開發機上 (把 snapshot.db 放到 nexus_core/.calibration_cache/ 之後)：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \\
        -e NEXUS_DB_NAME=/app/.calibration_cache/snapshot.db \\
        nexus-seeker python -m calibration forward-report
"""

import json
import math
from bisect import bisect_left
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from calibration.config import CalibrationConfig
from calibration.stats import (
    clustered_bootstrap_diff,
    clustered_bootstrap_mean,
    summarize,
)
from market_analysis.outcome_labeling import directional_touch

FORWARD_MIN_ROWS = 100
_CONDITION_NAMES = "一二三四五六"


def _open_readonly(snapshot_db: Optional[Path]) -> Any:
    """指定快照時以唯讀模式開啟，否則沿用 NEXUS_DB_NAME 的讀取連線。"""
    if snapshot_db is not None:
        from database.connection import connect_external_readonly

        return connect_external_readonly(str(snapshot_db))
    from database.connection import get_read_connection

    return get_read_connection()


def load_forward_rows(snapshot_db: Optional[Path] = None) -> pd.DataFrame:
    conn = _open_readonly(snapshot_db)
    try:
        return pd.read_sql_query(
            """
            SELECT l.*, o.label_status, o.first_touch_k1, o.first_touch_k15,
                   o.first_touch_k2, o.fwd_ret_1d, o.fwd_ret_5d, o.plan_outcome,
                   o.max_up_atr_5d, o.max_down_atr_5d
            FROM regime_evaluation_log l
            JOIN regime_evaluation_outcome o ON o.evaluation_id = l.id
            WHERE o.label_status = 'LABELED'
            """,
            conn,
        )
    finally:
        conn.close()


def _prepare(rows: pd.DataFrame, primary_col: str) -> pd.DataFrame:
    df = rows.copy()
    df["direction"] = df["direction"].fillna("LONG")
    df["outcome"] = [
        directional_touch(int(t), str(d)) if pd.notna(t) else 0
        for t, d in zip(df[primary_col], df["direction"])
    ]
    df["win"] = (df["outcome"] == 1).astype(int)
    df["r_multiple"] = df["outcome"].astype(float)
    df["date"] = df["bar_ts"].astype(str).str.slice(0, 10)
    return df


def build_forward_report(rows: pd.DataFrame, cfg: CalibrationConfig) -> dict[str, Any]:
    report: dict[str, Any] = {"total_labeled": int(len(rows)), "sections": []}
    if rows.empty:
        report["status"] = "資料累積中 (尚無已標註紀錄)"
        report["regime_breakdown"] = []
        return report
    df = _prepare(rows, "first_touch_k15")
    report["status"] = "OK"

    for (evaluator, decision), grp in df.groupby(["evaluator", "decision"]):
        section: dict[str, Any] = {
            "evaluator": evaluator,
            "decision": int(decision),
            "n": int(len(grp)),
        }
        if len(grp) < FORWARD_MIN_ROWS:
            section["status"] = f"資料累積中 ({len(grp)}/{FORWARD_MIN_ROWS})"
            report["sections"].append(section)
            continue
        st = summarize(grp, cfg.n_boot, cfg.seed, cfg.oos_split)
        section.update(
            {
                "status": "OK",
                "win_rate": st.win_rate,
                "wilson": [st.wilson_lo, st.wilson_hi],
                "expectancy": st.expectancy,
                "expectancy_ci": [st.exp_lo, st.exp_hi],
            }
        )
        # 逐條件的條件勝率與「只差這一條」近失誤分析
        per_condition = []
        masks = grp.dropna(subset=["conditions_mask", "conditions_evaluated_mask"])
        for i, digit in enumerate(_CONDITION_NAMES):
            bit = 1 << i
            evaluated = masks[
                (masks["conditions_evaluated_mask"].astype(int) & bit) > 0
            ]
            if evaluated.empty:
                continue
            passed = evaluated[(evaluated["conditions_mask"].astype(int) & bit) > 0]
            failed = evaluated[(evaluated["conditions_mask"].astype(int) & bit) == 0]
            others_all = masks["conditions_evaluated_mask"].astype(int) & ~bit
            near_miss = masks[
                ((masks["conditions_mask"].astype(int) & ~bit) == others_all)
                & ((masks["conditions_mask"].astype(int) & bit) == 0)
                & ((masks["conditions_evaluated_mask"].astype(int) & bit) > 0)
            ]
            per_condition.append(
                {
                    "condition": f"條件{digit}",
                    "pass_n": int(len(passed)),
                    "pass_win_rate": float(passed["win"].mean())
                    if len(passed)
                    else None,
                    "fail_n": int(len(failed)),
                    "fail_win_rate": float(failed["win"].mean())
                    if len(failed)
                    else None,
                    "near_miss_n": int(len(near_miss)),
                    "near_miss_win_rate": float(near_miss["win"].mean())
                    if len(near_miss)
                    else None,
                }
            )
        section["conditions"] = per_condition
        report["sections"].append(section)

    # GEX 相關量值分佈 vs 結果 (四分位)。只看進場／分類評估：EXIT_* 的 win
    # 是「離場訊號正確」，與進場勝率語意相反，混入會讓四分位結果失真。
    entries = df[~df["evaluator"].astype(str).str.startswith("EXIT_")]
    magnitudes: dict[str, Any] = {}
    for name, series in (
        (
            "next_node_space_atr",
            (entries["spot"] - entries["next_negative_node"]) / entries["atr_1d"],
        ),
        (
            "resistance_buffer_atr15",
            (entries["resistance_wall"] - entries["spot"]) / entries["atr_15m"],
        ),
        (
            "put_wall_dist_pct",
            (entries["spot"] - entries["put_wall"]) / entries["spot"],
        ),
    ):
        valid = pd.DataFrame({"x": series, "win": entries["win"]}).dropna()
        valid = valid[(valid["x"] > 0) & (valid["x"] < 1e6)]
        if len(valid) < FORWARD_MIN_ROWS:
            magnitudes[name] = f"資料累積中 ({len(valid)}/{FORWARD_MIN_ROWS})"
            continue
        valid["q"] = pd.qcut(valid["x"], 4, duplicates="drop")
        magnitudes[name] = [
            {"bucket": str(q), "n": int(len(g)), "win_rate": float(g["win"].mean())}
            for q, g in valid.groupby("q", observed=True)
        ]
    report["magnitudes"] = magnitudes
    report["exit_tiers"] = build_exit_tier_breakdown(df)
    report["regime_breakdown"] = build_regime_breakdown(df, cfg)
    report["threshold_studies"] = build_threshold_studies(entries)
    return report


# Skew 分位分組邊界：對應 skew_taxonomy 的現行門檻 (15 / 85 / 90 / 98)，
# 讓每一組恰好是「某個閘門會不會觸發」的區間。
_SKEW_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 15.0, "<15"),
    (15.0, 85.0, "15-85"),
    (85.0, 90.0, "85-90"),
    (90.0, 98.0, "90-98"),
    (98.0, 100.01, ">=98"),
)


def _features(df: pd.DataFrame) -> pd.DataFrame:
    """把 features_json 展開成欄位；無法解析的列留空。"""
    if "features_json" not in df.columns or df.empty:
        return pd.DataFrame(index=df.index)
    parsed: list[dict[str, Any]] = []
    for raw in df["features_json"]:
        try:
            obj = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            obj = {}
        parsed.append(obj if isinstance(obj, dict) else {})
    return pd.DataFrame(parsed, index=df.index)


def _bucket_rows(frame: pd.DataFrame, key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    has_dates = "date" in frame.columns and bool(
        (frame["date"].astype(str) != "").any()
    )
    for label, g in frame.groupby(key, observed=True, sort=True):
        n = int(len(g))
        win_ci: Optional[list[float]] = None
        adverse_ci: Optional[list[float]] = None
        if has_dates and n:
            # 日期叢集 bootstrap：同日樣本高度相關，逐筆重抽會低估區間
            dates = list(g["date"])
            _, wlo, whi = clustered_bootstrap_mean(
                [float(v) for v in g["win"]], dates, _FLIP_BOOT_N, _FLIP_BOOT_SEED
            )
            _, alo, ahi = clustered_bootstrap_mean(
                [1.0 if o == -1 else 0.0 for o in g["outcome"]],
                dates,
                _FLIP_BOOT_N,
                _FLIP_BOOT_SEED,
            )
            win_ci, adverse_ci = [wlo, whi], [alo, ahi]
        out.append(
            {
                "bucket": str(label),
                "n": n,
                "n_dates": int(g["date"].nunique()) if "date" in g.columns else None,
                "win_rate": float(g["win"].mean()),
                "adverse_rate": float((g["outcome"] == -1).mean()),
                "win_ci95": win_ci,
                "adverse_ci95": adverse_ci,
                "status": "OK"
                if n >= FORWARD_MIN_ROWS
                else f"資料累積中 ({n}/{FORWARD_MIN_ROWS})",
            }
        )
    return out


def build_threshold_studies(entries: pd.DataFrame) -> dict[str, Any]:
    """D-04 牆體深度比與 Skew 分位門檻的前向驗證 (docs §5.13)。

    - `wall_depth_ratio`：support_gex × 0.01 ÷ adv_dollar_20d 的四分位 × 勝率。
      只取 REGIME_CLASSIFIER 與進場閘門，EXIT_* 已由呼叫端排除。
    - `skew_percentile`：依現行門檻區間分組，並分開日級母體 (`CANONICAL`) 與
      高頻回退池——兩者的分位語意不同，混在一起無法判讀。
      總表依 (symbol, bar_ts, source) 對同一根 K 棒去重，優先保留
      REGIME_CLASSIFIER 的列；依評估器拆分的子表則使用未去重的資料。
    """
    feats = _features(entries)
    result: dict[str, Any] = {}

    if {"support_gex", "adv_dollar_20d"}.issubset(feats.columns):
        sg = pd.to_numeric(feats["support_gex"], errors="coerce")
        adv = pd.to_numeric(feats["adv_dollar_20d"], errors="coerce")
        ratio = (sg * 0.01 / adv).where((sg > 0) & (adv > 0))
        frame = pd.DataFrame(
            {
                "x": ratio,
                "date": entries["date"] if "date" in entries.columns else "",
                "win": entries["win"],
                "outcome": entries["outcome"],
            }
        ).dropna()
        if len(frame) < FORWARD_MIN_ROWS:
            result["wall_depth_ratio"] = f"資料累積中 ({len(frame)}/{FORWARD_MIN_ROWS})"
        else:
            frame["q"] = pd.qcut(frame["x"], 4, duplicates="drop")
            result["wall_depth_ratio"] = _bucket_rows(frame, "q")
    else:
        result["wall_depth_ratio"] = (
            "資料累積中 (尚無 support_gex / adv_dollar_20d 欄位)"
        )

    if "skew_percentile" in feats.columns:
        pct = pd.to_numeric(feats["skew_percentile"], errors="coerce")
        source = (
            feats["skew_percentile_source"].fillna("UNKNOWN")
            if "skew_percentile_source" in feats.columns
            else pd.Series(["UNKNOWN"] * len(feats), index=feats.index)
        )
        frame = pd.DataFrame(
            {
                "x": pct,
                "source": source,
                "date": entries["date"] if "date" in entries.columns else "",
                "win": entries["win"],
                "outcome": entries["outcome"],
            }
        ).dropna(subset=["x"])
        by_source = _skew_by_source(_dedupe_bars(entries, frame))
        result["skew_percentile"] = by_source or "資料累積中 (尚無 skew_percentile)"
        result["skew_percentile_by_evaluator"] = _skew_by_evaluator(entries, frame)
    else:
        result["skew_percentile"] = "資料累積中 (尚無 skew_percentile 欄位)"
        result["skew_percentile_by_evaluator"] = (
            "資料累積中 (尚無 skew_percentile 欄位)"
        )
    result["gamma_flip_materiality"] = _gamma_flip_materiality_study(entries, feats)
    return result


def _dedupe_bars(entries: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """同一根 K 棒被多個評估器記錄時只計一次 (鍵為紀錄的 symbol／bar_ts／source)。

    每個鍵優先保留 REGIME_CLASSIFIER 的列，否則取原順序第一列；
    缺任一鍵欄位時維持原樣。`frame` 索引沿用 `entries`。
    """
    keys = ["symbol", "bar_ts", "source"]
    if not set(keys).issubset(entries.columns):
        return frame
    key_df = entries[keys].reindex(frame.index)
    if "evaluator" in entries.columns:
        is_regime = entries["evaluator"].reindex(frame.index) == "REGIME_CLASSIFIER"
    else:
        is_regime = pd.Series(False, index=frame.index)
    order = pd.Series(range(len(frame)), index=frame.index)
    ranked = (
        pd.DataFrame({"pref": (~is_regime).astype(int), "pos": order})
        .join(key_df)
        .sort_values(["pref", "pos"], kind="stable")
    )
    keep = ranked[~ranked.duplicated(subset=keys, keep="first")].index
    return frame.loc[frame.index.isin(keep)]


def _skew_by_source(frame: pd.DataFrame) -> dict[str, Any]:
    """Skew 分位區間表，依母體來源 (`source` 欄) 分開。"""
    by_source: dict[str, Any] = {}
    for src, g in frame.groupby("source"):
        g = g.copy()
        g["bucket"] = pd.cut(
            g["x"],
            bins=[b[0] for b in _SKEW_BUCKETS] + [_SKEW_BUCKETS[-1][1]],
            labels=[b[2] for b in _SKEW_BUCKETS],
            right=False,
        )
        by_source[str(src)] = _bucket_rows(g.dropna(subset=["bucket"]), "bucket")
    return by_source


_SKEW_EVALUATOR_GROUPS: tuple[str, ...] = ("REGIME_CLASSIFIER", "ENTRY_SQUEEZE")


def _skew_by_evaluator(entries: pd.DataFrame, frame: pd.DataFrame) -> Any:
    """Skew 分位區間表再依評估器拆分 (REGIME_CLASSIFIER／ENTRY_SQUEEZE／其他)。

    不同評估器的母體與 `decision` 語意不同，混在同一張表會無法判讀。
    `frame` 為已去除缺 skew 的列，索引沿用 `entries`。
    """
    if "evaluator" not in entries.columns:
        return "資料累積中 (尚無 evaluator 欄位)"
    ev = entries["evaluator"].reindex(frame.index).astype("object")
    group = ev.where(ev.isin(_SKEW_EVALUATOR_GROUPS), "其他")
    out: dict[str, Any] = {}
    for name in (*_SKEW_EVALUATOR_GROUPS, "其他"):
        sub = frame[group == name]
        out[name] = (
            _skew_by_source(sub) if len(sub) else "資料累積中 (尚無 skew_percentile)"
        )
    return out


_FLIP_BOOT_N = 2000
_FLIP_BOOT_SEED = 7


def _gamma_flip_materiality_study(entries: pd.DataFrame, feats: pd.DataFrame) -> Any:
    """閘門 Gamma Flip 為雜訊交叉 vs 重要交叉的事後表現 (docs/microstructure/03 §5.6)。

    只取閘門 Flip 存在 (`gamma_flip_raw > 0`) 且有重要性比的列，以呈現層門檻
    `GAMMA_FLIP_MATERIALITY_DISPLAY` 分兩組，另附重要性比四分位。逆向先觸及率
    以日期叢集 bootstrap；兩組皆 n ≥ FORWARD_MIN_ROWS 且雜訊組 CI 下界高於重要組
    CI 上界，才判讀「可提案改閘門」。
    """
    from calibration.stats import clustered_bootstrap_mean
    from market_analysis.index_microstructure import GAMMA_FLIP_MATERIALITY_DISPLAY

    if not {"gamma_flip_raw", "gamma_flip_ratio"}.issubset(feats.columns):
        return "資料累積中 (尚無 gamma_flip_raw / gamma_flip_ratio 欄位)"
    raw = pd.to_numeric(feats["gamma_flip_raw"], errors="coerce")
    ratio = pd.to_numeric(feats["gamma_flip_ratio"], errors="coerce")
    frame = pd.DataFrame(
        {
            "x": ratio,
            "date": entries["date"] if "date" in entries.columns else "",
            "win": entries["win"],
            "outcome": entries["outcome"],
        }
    )[(raw > 0) & ratio.notna()]
    if frame.empty:
        return f"資料累積中 (0/{FORWARD_MIN_ROWS})"
    frame["group"] = [
        "雜訊交叉" if x < GAMMA_FLIP_MATERIALITY_DISPLAY else "重要交叉"
        for x in frame["x"]
    ]
    groups: dict[str, Any] = {}
    for name in ("雜訊交叉", "重要交叉"):
        g = frame[frame["group"] == name]
        adverse = [1.0 if o == -1 else 0.0 for o in g["outcome"]]
        rate, lo, hi = clustered_bootstrap_mean(
            adverse, list(g["date"]), _FLIP_BOOT_N, _FLIP_BOOT_SEED
        )
        groups[name] = {
            "n": int(len(g)),
            "n_dates": int(g["date"].nunique()),
            "win_rate": float(g["win"].mean()) if len(g) else None,
            "adverse_rate": rate if len(g) else None,
            "adverse_ci95": [lo, hi] if len(g) else None,
        }
    noise, material = groups["雜訊交叉"], groups["重要交叉"]
    if min(noise["n"], material["n"]) < FORWARD_MIN_ROWS:
        verdict = (
            f"樣本不足 (雜訊 {noise['n']}／重要 {material['n']}，"
            f"各需 ≥ {FORWARD_MIN_ROWS})，維持單向無門檻定義"
        )
    elif noise["adverse_ci95"][0] > material["adverse_ci95"][1]:
        verdict = (
            "雜訊交叉組逆向先觸及率 CI 下界高於重要組 CI 上界，可提案閘門改用重要性門檻"
        )
    else:
        verdict = "CI 重疊或雜訊組未較差，維持單向無門檻定義，只保留呈現層揭露"
    out: dict[str, Any] = {
        "threshold": GAMMA_FLIP_MATERIALITY_DISPLAY,
        "groups": groups,
        "判讀": verdict,
    }
    if len(frame) >= FORWARD_MIN_ROWS:
        frame["q"] = pd.qcut(frame["x"], 4, duplicates="drop")
        out["ratio_quartiles"] = _bucket_rows(frame, "q")
    return out


def _is_advisory(features_json: Any) -> bool:
    if not isinstance(features_json, str):
        return False
    try:
        return bool(json.loads(features_json).get("advisory"))
    except (ValueError, AttributeError):
        return False


def build_exit_tier_breakdown(df: pd.DataFrame) -> list[dict[str, Any]]:
    """出場分層洗盤率 (handoff §5.4)：EXIT_* evaluator 依分層 × 顧問旗標分組。

    `direction` 已是訊號押注方向 (evaluation_recorder.record_exit_signal)，
    因此 outcome=+1 為訊號正確 (平倉後確實朝不利部位的方向走)，outcome=-1
    為反向先觸及——對平倉類分層即「被洗盤掃出」。顧問持倉的分層多半未推播，
    與指令持倉分開統計，避免混入使用者未收到的樣本。

    樣本未達 FORWARD_MIN_ROWS 時仍輸出比率，但以 status 標示不可據以調參。
    """
    if df.empty or "evaluator" not in df.columns:
        return []
    exits = df[df["evaluator"].astype(str).str.startswith("EXIT_")].copy()
    if exits.empty:
        return []
    features = (
        exits["features_json"]
        if "features_json" in exits.columns
        else pd.Series([None] * len(exits), index=exits.index)
    )
    exits["advisory"] = [_is_advisory(f) for f in features]
    out: list[dict[str, Any]] = []
    for (evaluator, advisory), grp in exits.groupby(["evaluator", "advisory"]):
        n = int(len(grp))
        out.append(
            {
                "evaluator": str(evaluator),
                "advisory": bool(advisory),
                "n": n,
                "correct_rate": float((grp["outcome"] == 1).mean()),
                "washout_rate": float((grp["outcome"] == -1).mean()),
                "timeout_rate": float((grp["outcome"] == 0).mean()),
                "median_fwd_ret_5d": float(grp["fwd_ret_5d"].median())
                if "fwd_ret_5d" in grp.columns and grp["fwd_ret_5d"].notna().any()
                else None,
                "status": "OK"
                if n >= FORWARD_MIN_ROWS
                else f"資料累積中 ({n}/{FORWARD_MIN_ROWS})",
            }
        )
    return out


# ----------------------------------------------------------------- 體制／來源分組
_REGIME_CI_MIN_N = 30


def build_regime_breakdown(
    df: pd.DataFrame, cfg: Optional[CalibrationConfig] = None
) -> list[dict[str, Any]]:
    """REGIME_CLASSIFIER 依 (體制, 來源) 分組的結果。

    v1.16.0 起 Regime III／III-B 的多頭建倉交給擠壓規則，分類器的 decision=1
    不再等於「進場」，需依體制拆開才能正確解讀。n < FORWARD_MIN_ROWS 時照樣輸出
    數值，但 status 標示「資料累積中」——有數字但不可判讀。
    """
    cfg = cfg or CalibrationConfig()
    if df.empty or "evaluator" not in df.columns:
        return []
    if "outcome" not in df.columns:
        df = _prepare(df, "first_touch_k15")
    sub = df[df["evaluator"] == "REGIME_CLASSIFIER"].copy()
    if sub.empty:
        return []
    sub["regime"] = sub["regime"].fillna("(未知)").astype(str)
    sub["source"] = sub["source"].fillna("(未知)").astype(str)
    out: list[dict[str, Any]] = []
    for (regime, source), grp in sub.groupby(["regime", "source"], sort=True):
        n = int(len(grp))
        win_rate = float((grp["outcome"] == 1).mean())
        adverse = float((grp["outcome"] == -1).mean())
        expectancy = float(grp["r_multiple"].mean())
        ci: Optional[list[float]] = None
        if n >= _REGIME_CI_MIN_N:
            _m, lo, hi = clustered_bootstrap_mean(
                grp["r_multiple"].to_numpy(),
                grp["date"].to_numpy(),
                cfg.n_boot,
                cfg.seed,
            )
            ci = [lo, hi]
        out.append(
            {
                "regime": str(regime),
                "source": str(source),
                "n": n,
                "n_dates": int(grp["date"].nunique()),
                "win_rate": win_rate,
                "adverse_rate": adverse,
                "timeout_rate": 1.0 - win_rate - adverse,
                "expectancy": expectancy,
                "expectancy_ci": ci,
                "status": "OK"
                if n >= FORWARD_MIN_ROWS
                else f"資料累積中 ({n}/{FORWARD_MIN_ROWS})",
            }
        )
    return out


# ----------------------------------------------------------------- 擠壓乾跑翻轉檢核
# docs/architecture/05 §5.8 G：第 1–3 條由本段計算，第 4 條為人工項目。
SQUEEZE_PRIMARY_SOURCE = "WATCHLIST_ADVISOR"
SQUEEZE_DEDUP_TRADING_DAYS = 10
SQUEEZE_MIN_EVENTS = 30
SQUEEZE_MIN_PER_TIER = 5
SQUEEZE_HORIZON_DAYS = 20
SQUEEZE_TIERS = (1, 2, 3)
SQUEEZE_T_THRESHOLD = 2.0
SQUEEZE_BENCHMARK = "VOO"
SQUEEZE_MARKET_BAND = 0.02
SQUEEZE_G3_NOTE = "無顯著擇時優勢，翻轉僅為自動化使用者規則，不得宣稱 alpha（§5.8 G-3）"
SQUEEZE_G4_REMINDER = (
    "G.4 人工抽查：至少 3 檔標的的 W／3D／65m K 棒須與 TradingView 一致（本報表不計算）"
)
SQUEEZE_FOOTER = "本段只供人工審核；`SQUEEZE_ENTRY_DRY_RUN` 是否翻轉由使用者決定。"
PRICE_UNAVAILABLE = "價格不可得"

# 影子否決旗標鍵（契約見 docs/architecture/05 §5.18）。
# 旗標只寫入 features_json、不參與任何判定；本段只做離線分組比較。
SHADOW_FLAG_KEYS: tuple[str, ...] = (
    "sv_f_ceil",
    "sv_f_skew",
    "sv_f_mp",
    "sv_f_rr",
    "sv_f_wide",
    "sv_f_mev",
    "sv_any",
)
SHADOW_FLAG_LABELS: dict[str, str] = {
    "sv_f_ceil": "封頂",
    "sv_f_skew": "Skew 高分位",
    "sv_f_mp": "Max Pain 引力",
    "sv_f_rr": "牆位盈虧比 <2",
    "sv_f_wide": "停損過寬",
    "sv_f_mev": "總經事件 −2～+48h",
    "sv_any": "任一",
}
# 每組（旗標成立／放行）最少事件數。PRE_CALIBRATION／僅紀錄，不參與判定
SHADOW_MIN_N = 10
SHADOW_VERDICT_INSUFFICIENT = "樣本不足"
SHADOW_VERDICT_WORSE = "影子否決組顯著較差（支持評估接進判定，仍須人工審核）"
SHADOW_VERDICT_BETTER = "影子否決組反而較好（不支持否決）"
SHADOW_VERDICT_NO_DIFF = "無顯著差異（不支持否決）"

Closes = Mapping[str, Sequence[tuple[date, float]]]


def load_squeeze_rows(snapshot_db: Optional[Path] = None) -> pd.DataFrame:
    """讀取全部 ENTRY_SQUEEZE 判定列（含尚未標註者，不 JOIN outcome 表）。"""
    conn = _open_readonly(snapshot_db)
    try:
        # 舊快照可能沒有 features_json 欄：此時影子旗標全部視為無資料
        has_features = any(
            r[1] == "features_json"
            for r in conn.execute("PRAGMA table_info(regime_evaluation_log)")
        )
        return pd.read_sql_query(
            """
            SELECT id, bar_ts, symbol, source, sub_mode, decision, conditions_mask"""
            + (", features_json" if has_features else "")
            + """
            FROM regime_evaluation_log
            WHERE evaluator = 'ENTRY_SQUEEZE'
            """,
            conn,
        )
    finally:
        conn.close()


def _event_date(bar_ts: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(bar_ts)[:10])
    except ValueError:
        return None


def _trade_pos(sym_dates: Optional[list[date]], d: date, anchor: date) -> int:
    """交易日序位。有價格時為 `d` 當日（非交易日取下一交易日）在日線中的位置；
    無價格時以週一至週五估算（未計休市日），序位僅供相對間距使用。"""
    if sym_dates:
        if d > sym_dates[-1]:
            # 價格序列結尾之後：以最後價格日為起點估算，避免全部落在同一序位
            return (
                len(sym_dates)
                - 1
                + int(np.busday_count(sym_dates[-1].isoformat(), d.isoformat()))
            )
        return bisect_left(sym_dates, d)
    return int(np.busday_count(anchor.isoformat(), d.isoformat()))


def _shadow_flags(raw: Any) -> dict[str, Optional[bool]]:
    """解析 features_json 的影子旗標；解析失敗、缺鍵或非布林值一律為 None。"""
    obj: Any = {}
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
    if not isinstance(obj, dict):
        obj = {}
    out: dict[str, Optional[bool]] = {}
    for k in SHADOW_FLAG_KEYS:
        v = obj.get(k)
        out[k] = v if isinstance(v, bool) else None
    return out


def _independent_events(
    rows: pd.DataFrame, dates_by_sym: Mapping[str, list[date]]
) -> list[dict[str, Any]]:
    """decision=1 的列依 (標的, 等級) 去重：與上一個被保留事件相距 < 10 個交易日者丟棄。"""
    events: list[dict[str, Any]] = []
    anchor = date(2000, 1, 3)
    pos = rows[rows["decision"] == 1]
    parsed: list[tuple[str, Any, date, Any, dict[str, Optional[bool]]]] = []
    for r in pos.itertuples(index=False):
        d = _event_date(r.bar_ts)
        if d is None:
            continue
        tier = None if pd.isna(r.conditions_mask) else int(r.conditions_mask)
        flags = _shadow_flags(getattr(r, "features_json", None))
        parsed.append((str(r.symbol), tier, d, r.bar_ts, flags))
    parsed.sort(key=lambda x: (x[0], str(x[1]), x[2], str(x[3])))
    last: dict[tuple[str, Any], int] = {}
    for sym, tier, d, _ts, flags in parsed:
        p = _trade_pos(dates_by_sym.get(sym), d, anchor)
        key = (sym, tier)
        if key in last and p - last[key] < SQUEEZE_DEDUP_TRADING_DAYS:
            continue
        last[key] = p
        events.append({"symbol": sym, "tier": tier, "date": d, "flags": flags})
    return events


def _ret20(closes: Sequence[tuple[date, float]], d: date) -> Optional[float]:
    """事件日（非交易日取下一交易日）起 20 個交易日的報酬；資料不足回傳 None。"""
    dates = [c[0] for c in closes]
    t = bisect_left(dates, d)
    if t + SQUEEZE_HORIZON_DAYS >= len(closes):
        return None
    base = closes[t][1]
    if not base or not math.isfinite(base):
        return None
    return float(closes[t + SQUEEZE_HORIZON_DAYS][1] / base - 1.0)


def _finite_ci(lo: float, hi: float) -> Optional[list[float]]:
    return [lo, hi] if math.isfinite(lo) and math.isfinite(hi) else None


def _squeeze_strata(
    treat_v: list[float],
    treat_c: list[str],
    control_v: list[float],
    control_c: list[str],
    benchmark: Optional[Sequence[tuple[date, float]]],
    cfg: CalibrationConfig,
) -> dict[str, Any]:
    """G.2 的同日匹配與市況分層（僅供參考，不影響 g2_passed）。

    單一週、高 beta 標的與大盤方向會讓處理組／對照組的差混入 beta 與日期組成，
    故另列：(1) 同事件日對照組均值相減；(2) 依基準 (SQUEEZE_BENCHMARK) 同日起
    20 日報酬分「下跌／盤整／上漲」後各自比較。
    """
    ctrl_by_date: dict[str, list[float]] = {}
    for v, c in zip(control_v, control_c):
        ctrl_by_date.setdefault(c, []).append(v)
    diffs: list[float] = []
    diff_c: list[str] = []
    skipped = 0
    for v, c in zip(treat_v, treat_c):
        peers = ctrl_by_date.get(c)
        if not peers:
            skipped += 1
            continue
        diffs.append(v - sum(peers) / len(peers))
        diff_c.append(c)
    matched: dict[str, Any] = {
        "n": len(diffs),
        "n_dates": len(set(diff_c)),
        "excess": None,
        "excess_ci": None,
        "no_same_day_control": skipped,
    }
    if diffs:
        mean, lo, hi = clustered_bootstrap_mean(diffs, diff_c, cfg.n_boot, cfg.seed)
        matched["excess"] = mean
        matched["excess_ci"] = _finite_ci(lo, hi)

    out: dict[str, Any] = {"日期匹配": matched}
    if not benchmark:
        out["市況分層"] = "基準價格不可得"
        return out
    state_cache: dict[str, Optional[str]] = {}

    def _state(iso: str) -> Optional[str]:
        if iso not in state_cache:
            r = _ret20(benchmark, date.fromisoformat(iso))
            state_cache[iso] = (
                None
                if r is None
                else "下跌"
                if r < -SQUEEZE_MARKET_BAND
                else "上漲"
                if r > SQUEEZE_MARKET_BAND
                else "盤整"
            )
        return state_cache[iso]

    buckets: dict[str, dict[str, list[Any]]] = {
        k: {"tv": [], "tc": [], "cv": [], "cc": []} for k in ("下跌", "盤整", "上漲")
    }
    pending_bm = 0
    for side_v, side_c, vk, ck in (
        (treat_v, treat_c, "tv", "tc"),
        (control_v, control_c, "cv", "cc"),
    ):
        for v, c in zip(side_v, side_c):
            st = _state(c)
            if st is None:
                pending_bm += 1
                continue
            buckets[st][vk].append(v)
            buckets[st][ck].append(c)
    layered: dict[str, Any] = {}
    for st, b in buckets.items():
        entry: dict[str, Any] = {
            "n_treat": len(b["tv"]),
            "n_control": len(b["cv"]),
            "excess": None,
            "excess_ci": None,
        }
        if b["tv"] and b["cv"]:
            diff, lo, hi, _se = clustered_bootstrap_diff(
                b["tv"], b["tc"], b["cv"], b["cc"], cfg.n_boot, cfg.seed
            )
            entry["excess"] = diff
            entry["excess_ci"] = _finite_ci(lo, hi)
        else:
            entry["status"] = "樣本不足"
        layered[st] = entry
    layered["pending_benchmark"] = pending_bm
    out["市況分層"] = layered
    return out


def _shadow_veto_groups(
    events: Sequence[Mapping[str, Any]], closes: Closes, cfg: CalibrationConfig
) -> dict[str, Any]:
    """影子否決分組（僅參考，不影響 G.1／G.2 判定）。

    只取 20 日報酬已可得的獨立事件；每個旗標分「成立」與「放行」兩組
    （旗標為 None 者計入 n_null、不進任一組），以事件日期分群 bootstrap 比較
    成立組 − 放行組的 20 日報酬差。
    """
    samples: list[tuple[float, str, dict[str, Optional[bool]]]] = []
    for e in events:
        series = closes.get(e["symbol"])
        if not series:
            continue
        r = _ret20(series, e["date"])
        if r is None:
            continue
        flags = e.get("flags") or {}
        samples.append((r, e["date"].isoformat(), flags))
    out: dict[str, Any] = {}
    for key in SHADOW_FLAG_KEYS:
        fv: list[float] = []
        fc: list[str] = []
        pv: list[float] = []
        pc: list[str] = []
        n_null = 0
        for r, c, flags in samples:
            f = flags.get(key)
            if f is None:
                n_null += 1
            elif f:
                fv.append(r)
                fc.append(c)
            else:
                pv.append(r)
                pc.append(c)
        entry: dict[str, Any] = {
            "label": SHADOW_FLAG_LABELS[key],
            "n_flag": len(fv),
            "n_pass": len(pv),
            "n_null": n_null,
            "n_dates_flag": len(set(fc)),
            "diff": None,
            "ci": None,
            "verdict": SHADOW_VERDICT_INSUFFICIENT,
        }
        if fv and pv:
            diff, lo, hi, _se = clustered_bootstrap_diff(
                fv, fc, pv, pc, cfg.n_boot, cfg.seed
            )
            entry["diff"] = diff
            ci = _finite_ci(lo, hi)
            entry["ci"] = ci
            if len(fv) >= SHADOW_MIN_N and len(pv) >= SHADOW_MIN_N and ci:
                if ci[1] < 0:
                    entry["verdict"] = SHADOW_VERDICT_WORSE
                elif ci[0] > 0:
                    entry["verdict"] = SHADOW_VERDICT_BETTER
                else:
                    entry["verdict"] = SHADOW_VERDICT_NO_DIFF
        out[key] = entry
    return out


def _squeeze_analysis(
    rows: pd.DataFrame, closes: Closes, cfg: CalibrationConfig
) -> dict[str, Any]:
    dates_by_sym: dict[str, list[date]] = {
        s: [c[0] for c in v] for s, v in closes.items() if v
    }
    events = _independent_events(rows, dates_by_sym)
    by_tier = {t: sum(1 for e in events if e["tier"] == t) for t in SQUEEZE_TIERS}
    g1 = {
        "independent_events": len(events),
        "by_tier": by_tier,
        "decision1_rows": int((rows["decision"] == 1).sum()),
        "no_price_events": sum(1 for e in events if e["symbol"] not in dates_by_sym),
        "g1_passed": len(events) >= SQUEEZE_MIN_EVENTS
        and all(by_tier[t] >= SQUEEZE_MIN_PER_TIER for t in SQUEEZE_TIERS),
    }
    result: dict[str, Any] = {"g1": g1}
    result["shadow_vetoes"] = (
        _shadow_veto_groups(events, closes, cfg) if dates_by_sym else PRICE_UNAVAILABLE
    )
    if not dates_by_sym:
        result["g2"] = {"status": PRICE_UNAVAILABLE, "g2_passed": None}
        result["g3"] = None
        return result

    treat_v: list[float] = []
    treat_c: list[str] = []
    pending = 0
    no_price = 0
    for e in events:
        series = closes.get(e["symbol"])
        if not series:
            no_price += 1
            continue
        r = _ret20(series, e["date"])
        if r is None:
            pending += 1
            continue
        treat_v.append(r)
        treat_c.append(e["date"].isoformat())

    control_v: list[float] = []
    control_c: list[str] = []
    if events:
        lo_d = min(e["date"] for e in events)
        hi_d = max(e["date"] for e in events)
        # 任何 decision=1 原始列（含被 10 日規則丟棄者）的 (標的, 日) 不得進入對照組
        treated_pairs = {
            (str(sym_), _event_date(ts))
            for sym_, ts in zip(
                rows.loc[rows["decision"] == 1, "symbol"],
                rows.loc[rows["decision"] == 1, "bar_ts"],
            )
        }
        ctrl = rows[rows["decision"] == 0].copy()
        ctrl["_d"] = [_event_date(x) for x in ctrl["bar_ts"]]
        keep = np.array(
            [
                (str(s_), d_) not in treated_pairs
                for s_, d_ in zip(ctrl["symbol"], ctrl["_d"])
            ],
            dtype=bool,
        )
        ctrl = ctrl.loc[keep]
        ctrl = ctrl.dropna(subset=["_d"]).sort_values("bar_ts")
        ctrl = ctrl[(ctrl["_d"] >= lo_d) & (ctrl["_d"] <= hi_d)]
        ctrl = ctrl.drop_duplicates(subset=["symbol", "_d"], keep="first")
        for sym_, d_ in zip(ctrl["symbol"], ctrl["_d"]):
            series = closes.get(str(sym_))
            if not series:
                continue
            r = _ret20(series, d_)
            if r is None:
                continue
            control_v.append(r)
            control_c.append(d_.isoformat())
    g2: dict[str, Any] = {
        "n_treat": len(treat_v),
        "n_control": len(control_v),
        "pending": pending,
        "no_price_events": no_price,
        "excess": None,
        "excess_ci": None,
        "t_stat": None,
        "g2_passed": None,
    }
    if len(treat_v) >= 1 and len(control_v) >= 1:
        diff, lo, hi, se = clustered_bootstrap_diff(
            treat_v, treat_c, control_v, control_c, cfg.n_boot, cfg.seed
        )
        g2["excess"] = diff
        g2["g2_passed"] = bool(diff >= 0)
        if math.isfinite(lo) and math.isfinite(hi):
            g2["excess_ci"] = [lo, hi]
        if math.isfinite(se) and se > 0:
            g2["t_stat"] = diff / se
    else:
        g2["status"] = "樣本不足"
    g2["strata"] = _squeeze_strata(
        treat_v, treat_c, control_v, control_c, closes.get(SQUEEZE_BENCHMARK), cfg
    )
    result["g2"] = g2
    t_stat = g2["t_stat"]
    if t_stat is None:
        result["g3"] = "樣本不足，尚無法判讀 t 值"
    elif abs(t_stat) < SQUEEZE_T_THRESHOLD:
        result["g3"] = SQUEEZE_G3_NOTE
    else:
        result["g3"] = (
            f"|t| ≥ {SQUEEZE_T_THRESHOLD:g}，超額報酬達統計顯著（仍須人工審核）"
        )
    return result


def build_squeeze_flip_check(
    rows: pd.DataFrame, closes: Closes, cfg: Optional[CalibrationConfig] = None
) -> dict[str, Any]:
    """擠壓乾跑翻轉檢核 (docs §5.8 G 第 1–3 條)。

    主結果只用 `WATCHLIST_ADVISOR`（將來要翻轉推播的路徑）；`SYMBOL_VIEW` 為使用者
    手動 /x 查詢，有選樣偏差，只併入「全部來源（參考）」，不參與達標判定。
    """
    cfg = cfg or CalibrationConfig()
    base: pd.DataFrame = (
        rows
        if not rows.empty
        else pd.DataFrame(
            columns=["bar_ts", "symbol", "source", "decision", "conditions_mask"]
        )
    )
    primary = base[base["source"] == SQUEEZE_PRIMARY_SOURCE]
    return {
        "primary_source": SQUEEZE_PRIMARY_SOURCE,
        "n_rows": int(len(primary)),
        "primary": _squeeze_analysis(primary, closes, cfg),
        "all_sources": {
            "n_rows": int(len(base)),
            **_squeeze_analysis(base, closes, cfg),
        },
        "g4_reminder": SQUEEZE_G4_REMINDER,
        "footer": SQUEEZE_FOOTER,
    }
