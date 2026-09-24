"""notif-report：各通知頻道「照做 vs 持有不動」的 Sortino / MDD / CVaR 差異報告。

資料來源是 production 資料庫**快照**中的 notification_dispatch_log +
notification_dispatch_outcome（v083），由 03:30 ET labeler 回填。**只讀、只產報告、
不改任何參數**；判讀準則見 docs/architecture/05_calibration_harness_and_forward_collection.md。

使用方式（先以 `.backup` 複製 DB，絕不直接讀 production 檔）：

    # VPS 上：
    sqlite3 data/nexus_data.db ".backup /tmp/snapshot.db"
    # 開發機上 (把 snapshot.db 放到 nexus_core/.calibration_cache/ 之後)：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python -m calibration notif-report --snapshot-db /app/.calibration_cache/snapshot.db

合併方式：同一 (頻道, 情境, 視窗) 內的所有事件，把逐日報酬序列串接成一條序列計算
Sortino 與 CVaR95——單一事件只有 21 個觀測，逐筆算 Sortino 再平均會被極端小樣本主導，
也不足以估計 95% 尾部。MDD 與總報酬取逐事件差的平均。信賴區間以「事件」為單位重抽樣
（bootstrap，固定 seed，結果可重現），保留事件內的時間相關性。

視窗：20 日（全部已標註事件，取每筆路徑的前 21 期）與 60 日（已延伸標註的事件）並列。
防護類訊號（停損、逃頂、基本面出場）的價值常在更長期間才顯現，只看 20 日會系統性低估；
反之，減碼類訊號的機會成本也可能在 60 日才完整顯現。兩個視窗結論相反時應視為「無法判定」。
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from market_analysis.downside_risk import (
    historical_var_cvar,
    max_drawdown,
    nav_from_returns,
    sortino_ratio,
)
from market_analysis.notification_outcome import DEFAULT_RF_ANNUAL, REPORT_HORIZONS

# 單一 (頻道, 情境) 至少要有這麼多已標註事件才給出建議；少於此數只描述不判讀
NOTIF_REPORT_MIN_EVENTS = 20
DEFAULT_BOOTSTRAP = 500
DEFAULT_SEED = 7
_CI = (2.5, 97.5)

VERDICT_INSUFFICIENT = "樣本不足（僅描述，不判讀）"
VERDICT_SUGGEST_OFF = "建議 B&H 預設關閉（待人工審核）"
VERDICT_POSITIVE = "照做顯著改善 Sortino"
VERDICT_INCONCLUSIVE = "無法判定"


def load_rows(snapshot_db: Optional[Path]) -> list[dict[str, Any]]:
    """讀取已標註事件；指定快照時以唯讀模式開啟，否則用 NEXUS_DB_NAME 指向的 DB。"""
    from database.notification_dispatch_log import load_labeled_dispatches

    if snapshot_db is not None:
        from database.connection import connect_external_readonly

        conn = connect_external_readonly(str(snapshot_db))
    else:
        from database.connection import get_read_connection

        conn = get_read_connection()
    try:
        return load_labeled_dispatches(conn)
    finally:
        conn.close()


def _series(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [float(v) for v in parsed if v is not None]


def _pooled_sortino(events: Sequence[dict[str, Any]], key: str) -> float:
    pooled = [r for e in events for r in e[key]]
    return sortino_ratio(pooled, DEFAULT_RF_ANNUAL) if pooled else 0.0


def _pooled_cvar(events: Sequence[dict[str, Any]], key: str) -> Optional[float]:
    pooled = [r for e in events for r in e[key]]
    res = historical_var_cvar(pooled)
    return res.cvar if res is not None else None


def _metrics(events: Sequence[dict[str, Any]]) -> dict[str, Optional[float]]:
    d_sortino = _pooled_sortino(events, "follow") - _pooled_sortino(events, "hold")
    cvar_f = _pooled_cvar(events, "follow")
    cvar_h = _pooled_cvar(events, "hold")
    d_cvar = cvar_f - cvar_h if cvar_f is not None and cvar_h is not None else None
    d_mdd = float(np.mean([e["follow_mdd"] - e["hold_mdd"] for e in events]))
    d_ret = float(
        np.mean([e["follow_total_return"] - e["hold_total_return"] for e in events])
    )
    return {
        "delta_sortino": d_sortino,
        "delta_mdd": d_mdd,
        "delta_cvar95": d_cvar,
        "delta_total_return": d_ret,
    }


def _bootstrap_ci(
    events: Sequence[dict[str, Any]], n_boot: int, rng: np.random.Generator
) -> dict[str, Optional[list[float]]]:
    samples: dict[str, list[float]] = defaultdict(list)
    n = len(events)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        m = _metrics([events[i] for i in idx])
        for k, v in m.items():
            if v is not None and math.isfinite(v):
                samples[k].append(v)
    out: dict[str, Optional[list[float]]] = {}
    for k in ("delta_sortino", "delta_mdd", "delta_cvar95", "delta_total_return"):
        vals = samples.get(k, [])
        out[k] = (
            [float(np.percentile(vals, _CI[0])), float(np.percentile(vals, _CI[1]))]
            if len(vals) >= max(10, n_boot // 2)
            else None
        )
    return out


def _verdict(
    n: int,
    metrics: dict[str, Optional[float]],
    ci: dict[str, Optional[list[float]]],
) -> str:
    if n < NOTIF_REPORT_MIN_EVENTS:
        return VERDICT_INSUFFICIENT
    s_ci = ci.get("delta_sortino")
    if s_ci is None:
        return VERDICT_INCONCLUSIVE
    d_cvar = metrics.get("delta_cvar95")
    # CVaR 為正值損失比例：差值 ≥ 0 代表照做沒有降低尾部損失
    cvar_not_improved = d_cvar is None or d_cvar >= 0.0
    if s_ci[1] <= 0.0 and cvar_not_improved:
        return VERDICT_SUGGEST_OFF
    if s_ci[0] > 0.0:
        return VERDICT_POSITIVE
    return VERDICT_INCONCLUSIVE


def build_notif_report(
    rows: Sequence[dict[str, Any]],
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        follow_full = _series(row.get("follow_returns_json"))
        hold_full = _series(row.get("hold_returns_json"))
        if not follow_full or len(follow_full) != len(hold_full):
            continue
        key_base = (str(row.get("channel") or ""), str(row.get("scenario") or ""))
        for horizon in REPORT_HORIZONS:
            # 路徑長度為 horizon + 1（第 0 期為參考價 → 第一個觀測日收盤）
            n = horizon + 1
            if len(follow_full) < n:
                continue
            follow, hold = follow_full[:n], hold_full[:n]
            follow_nav, hold_nav = nav_from_returns(follow), nav_from_returns(hold)
            groups[(*key_base, horizon)].append(
                {
                    "follow": follow,
                    "hold": hold,
                    # 由截斷後的序列重算，不沿用儲存欄位（其視窗可能不同）
                    "follow_mdd": max_drawdown(follow_nav).max_drawdown,
                    "hold_mdd": max_drawdown(hold_nav).max_drawdown,
                    "follow_total_return": float(follow_nav[-1] - 1.0),
                    "hold_total_return": float(hold_nav[-1] - 1.0),
                    "signal_kind": str(row.get("signal_kind") or ""),
                }
            )

    rng = np.random.default_rng(seed)
    sections: list[dict[str, Any]] = []
    for (channel, scenario, horizon), events in sorted(groups.items()):
        metrics = _metrics(events)
        ci = _bootstrap_ci(events, n_boot, rng)
        kinds = sorted({e["signal_kind"] for e in events})
        sections.append(
            {
                "channel": channel,
                "scenario": scenario,
                "horizon_days": horizon,
                "signal_kinds": kinds,
                "n_events": len(events),
                **metrics,
                "ci95": ci,
                "verdict": _verdict(len(events), metrics, ci),
            }
        )
    return {
        "total_labeled": len(
            [
                r
                for r in rows
                if _series(r.get("follow_returns_json"))
                and len(_series(r.get("follow_returns_json")))
                == len(_series(r.get("hold_returns_json")))
            ]
        ),
        "horizons": list(REPORT_HORIZONS),
        "min_events": NOTIF_REPORT_MIN_EVENTS,
        "bootstrap": n_boot,
        "seed": seed,
        "mar_annual": DEFAULT_RF_ANNUAL,
        "sections": sections,
    }


def _fmt(value: Optional[float], pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.2f}%" if pct else f"{value:+.3f}"


def _fmt_ci(ci: Optional[list[float]], pct: bool = False) -> str:
    if not ci:
        return "—"
    return f"[{_fmt(ci[0], pct)}, {_fmt(ci[1], pct)}]"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# 通知成效前向評估：照做 vs 持有不動",
        "",
        f"已標註事件：{result['total_labeled']}；每組最少 {result['min_events']} 筆才判讀；"
        f"bootstrap {result['bootstrap']} 次（seed {result['seed']}）；"
        f"MAR = {result['mar_annual']:.1%}。",
        "",
        "ΔX = 照做 − 持有。Sortino 為主判讀指標；CVaR95 為正值損失比例，Δ < 0 代表照做降低尾部損失。",
        "",
        "20 日與 60 日視窗並列；兩者結論相反時視為無法判定（防護類訊號的價值常在長視窗才顯現）。",
        "",
        "| 頻道 | 情境 | 視窗 | 訊號 | n | ΔSortino [95% CI] | ΔMDD | ΔCVaR95 | Δ總報酬 | 判讀 |",
        "| :--- | :--- | ---: | :--- | ---: | :--- | :--- | :--- | :--- | :--- |",
    ]
    for s in result["sections"]:
        lines.append(
            f"| {s['channel']} | {s['scenario'] or '—'} | {s['horizon_days']} 日 "
            f"| {'/'.join(s['signal_kinds'])} "
            f"| {s['n_events']} | {_fmt(s['delta_sortino'])} {_fmt_ci(s['ci95']['delta_sortino'])} "
            f"| {_fmt(s['delta_mdd'], True)} | {_fmt(s['delta_cvar95'], True)} "
            f"| {_fmt(s['delta_total_return'], True)} | {s['verdict']} |"
        )
    if not result["sections"]:
        lines.append("| — | — | — | — | 0 | — | — | — | — | 尚無已標註事件 |")
    lines += [
        "",
        "> 本報告只供人工審核；任何頻道預設值的調整都必須另開 PR。",
        "",
    ]
    return "\n".join(lines)


def write_notif_report(out_dir: Path, result: dict[str, Any]) -> Path:
    from calibration.report import write_study_report

    return write_study_report(
        out_dir, "notif-report", result, markdown=render_markdown(result)
    )
