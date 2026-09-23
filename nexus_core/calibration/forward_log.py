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
from typing import Any

import pandas as pd

from calibration.config import CalibrationConfig
from calibration.stats import summarize
from market_analysis.outcome_labeling import directional_touch

FORWARD_MIN_ROWS = 100
_CONDITION_NAMES = "一二三四五六"


def load_forward_rows() -> pd.DataFrame:
    from database.connection import get_read_connection

    conn = get_read_connection()
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
    return report


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
