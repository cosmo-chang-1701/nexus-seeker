"""總經訊號乾跑的前向報告（只讀快照、不寫 DB、不改參數）。

讀取 production 快照中的 `macro_regime_log` / `macro_signal_log`，輸出：
1. 已確認三態的區段（起訖日、天數）；
2. 各指標的亮起時段；
3. 每段狀態開始後 20／60 個交易日，科技池（等權）、VOO、BOXX 的報酬——
   判斷訊號是領先（切換後科技池相對轉弱）還是落後（切換後科技池反而反彈）。

用法（開發機）：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python -m calibration macro-forward-report --snapshot-db /app/.calibration_cache/snapshot.db
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

FORWARD_WINDOWS: tuple[int, ...] = (20, 60)
BOXX_SYMBOL = "BOXX"
VOO_SYMBOL = "VOO"

PriceSeries = list[tuple[date, float]]  # 依日期排序的 (日期, 收盤)


@dataclass(frozen=True)
class Segment:
    state: str
    start: str
    end: str
    days: int


def load_logs(
    snapshot_db: Optional[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """讀取三態與指標紀錄；指定快照時以唯讀模式開啟，否則用 NEXUS_DB_NAME 指向的 DB。"""
    from database.macro_signal_log import load_regime_log, load_signal_log

    if snapshot_db is not None:
        from database.connection import connect_external_readonly

        conn = connect_external_readonly(str(snapshot_db))
    else:
        from database.connection import get_read_connection

        conn = get_read_connection()
    try:
        return load_regime_log(conn), load_signal_log(conn)
    finally:
        conn.close()


def state_segments(regimes: Sequence[dict[str, Any]]) -> list[Segment]:
    """已確認狀態的連續區段（尚未確認的暖機期略過）。"""
    segments: list[Segment] = []
    cur_state: Optional[str] = None
    start = end = ""
    days = 0
    for row in regimes:
        state = row.get("confirmed_state")
        td = str(row["trading_date"])
        if state is None:
            continue
        if state == cur_state:
            end = td
            days += 1
            continue
        if cur_state is not None:
            segments.append(Segment(cur_state, start, end, days))
        cur_state, start, end, days = str(state), td, td, 1
    if cur_state is not None:
        segments.append(Segment(cur_state, start, end, days))
    return segments


def lit_periods(signals: Sequence[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """各指標 flag 連續為 True 的時段（依交易日）。"""
    by_ind: dict[str, list[tuple[str, Optional[bool]]]] = {}
    for row in signals:
        by_ind.setdefault(str(row["indicator"]), []).append(
            (str(row["trading_date"]), row.get("flag"))
        )
    out: dict[str, list[tuple[str, str]]] = {}
    for ind, rows in by_ind.items():
        periods: list[tuple[str, str]] = []
        start: Optional[str] = None
        last = ""
        for td, flag in sorted(rows):
            if flag:
                if start is None:
                    start = td
                last = td
            elif start is not None:
                periods.append((start, last))
                start = None
        if start is not None:
            periods.append((start, last))
        out[ind] = periods
    return out


def forward_return(prices: PriceSeries, start: date, window: int) -> Optional[float]:
    """以 `start` 當天（或之後第一個交易日）收盤為基準，往後第 `window` 個交易日的報酬。"""
    idx = next((i for i, (d, _) in enumerate(prices) if d >= start), None)
    if idx is None or idx + window >= len(prices):
        return None
    base = prices[idx][1]
    if base <= 0:
        return None
    return prices[idx + window][1] / base - 1.0


def equal_weight_return(
    pool: dict[str, PriceSeries], start: date, window: int
) -> Optional[float]:
    rets = [
        r
        for r in (forward_return(p, start, window) for p in pool.values())
        if r is not None
    ]
    return sum(rets) / len(rets) if rets else None


def build_report(
    regimes: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    tech_prices: dict[str, PriceSeries],
    voo_prices: PriceSeries,
    boxx_prices: PriceSeries,
) -> dict[str, Any]:
    segments = state_segments(regimes)
    seg_rows: list[dict[str, Any]] = []
    for seg in segments:
        start = date.fromisoformat(seg.start)
        row: dict[str, Any] = {
            "state": seg.state,
            "start": seg.start,
            "end": seg.end,
            "days": seg.days,
        }
        for w in FORWARD_WINDOWS:
            tech = equal_weight_return(tech_prices, start, w)
            voo = forward_return(voo_prices, start, w)
            row[f"tech_{w}d"] = tech
            row[f"voo_{w}d"] = voo
            row[f"boxx_{w}d"] = forward_return(boxx_prices, start, w)
            row[f"tech_minus_voo_{w}d"] = (
                tech - voo if tech is not None and voo is not None else None
            )
        seg_rows.append(row)
    state_days: dict[str, int] = {}
    for seg in segments:
        state_days[seg.state] = state_days.get(seg.state, 0) + seg.days
    return {
        "first_date": regimes[0]["trading_date"] if regimes else None,
        "last_date": regimes[-1]["trading_date"] if regimes else None,
        "logged_days": len(regimes),
        "state_days": state_days,
        "segments": seg_rows,
        "lit_periods": lit_periods(signals),
    }


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# 總經訊號乾跑前向報告",
        "",
        f"期間：{result['first_date']} → {result['last_date']}（記錄 {result['logged_days']} 個交易日）",
        "",
        "判讀方式：「轉差／最差」開始後，科技池相對 VOO 若轉弱，代表訊號領先；"
        "若科技池反而較強，代表訊號落後（在反彈期才防守）。資料不足 20／60 個交易日者顯示「—」。",
        "",
        "## 各狀態天數",
        "",
    ]
    for state, days in sorted(result["state_days"].items()):
        lines.append(f"- {state}：{days} 天")
    lines += [
        "",
        "## 已確認狀態區段與之後的報酬",
        "",
        "| 狀態 | 起 | 迄 | 天數 | 科技池 20d | VOO 20d | BOXX 20d | 科技−VOO 20d | 科技池 60d | VOO 60d | 科技−VOO 60d |",
        "| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in result["segments"]:
        lines.append(
            f"| {r['state']} | {r['start']} | {r['end']} | {r['days']} | "
            f"{_pct(r['tech_20d'])} | {_pct(r['voo_20d'])} | {_pct(r['boxx_20d'])} | "
            f"{_pct(r['tech_minus_voo_20d'])} | {_pct(r['tech_60d'])} | {_pct(r['voo_60d'])} | "
            f"{_pct(r['tech_minus_voo_60d'])} |"
        )
    lines += ["", "## 各指標亮起時段", ""]
    for ind, periods in sorted(result["lit_periods"].items()):
        spans = "、".join(f"{a}→{b}" for a, b in periods) or "未亮起"
        lines.append(f"- `{ind}`：{spans}")
    lines += [
        "",
        "> 門檻皆為 PRE_CALIBRATION；本報告只供人工判讀，不自動調整任何參數。",
    ]
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, result: dict[str, Any]) -> Path:
    """輸出 {out_dir}/calibration/macro-forward-report_{stamp}/（寫檔集中在 report.py）。"""
    from calibration.report import write_study_report

    return write_study_report(
        out_dir, "macro-forward-report", result, markdown=render_markdown(result)
    )


async def load_prices(
    symbols: Sequence[str],
    loader: Optional[Callable[[str], Any]] = None,
) -> dict[str, PriceSeries]:
    """日線收盤；預設經 `get_history_df`（tz-naive US/Eastern，取 `.date()` 即交易日）。"""
    if loader is None:
        from services.market_data_service import get_history_df

        async def _default(symbol: str) -> Any:
            return await get_history_df(symbol, period="5y")

        loader = _default
    out: dict[str, PriceSeries] = {}
    for sym in symbols:
        df = await loader(sym)
        if df is None or getattr(df, "empty", True):
            out[sym] = []
            continue
        out[sym] = [(idx.date(), float(v)) for idx, v in df["Close"].dropna().items()]
    return out
