"""總經訊號乾跑的前向報告（只讀快照、不寫 DB、不改參數）。

讀取 production 快照中的 `macro_regime_log` / `macro_signal_log`，輸出：
1. 已確認三態的區段（起訖日、天數）；
2. 各指標的亮起時段；
3. 每段狀態開始後 20／60 個交易日，科技池（等權）、VOO、BOXX 的報酬——
   判斷訊號是領先（切換後科技池相對轉弱）還是落後（切換後科技池反而反彈）；
4. SPY 回檔檢核：紀錄期間是否已出現 ≥ 10% 回檔（docs/architecture/05 §5.15）。

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

# docs/architecture/05 §5.15；SPY 收盤價自高點回落，2026-11-26 使用者決定
DRAWDOWN_SYMBOL = "SPY"
DRAWDOWN_THRESHOLD = 0.10
DRAWDOWN_PEAK_LOOKBACK = 252  # 高點回看筆數（交易日，含當日）
DRAWDOWN_PRICE_UNAVAILABLE = "SPY 價格不可得，無法檢核"
DRAWDOWN_NO_RECORD = "尚無前向紀錄，無法檢核"
BENCHMARK_SYMBOLS: tuple[str, ...] = (VOO_SYMBOL, BOXX_SYMBOL, DRAWDOWN_SYMBOL)

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


def spy_drawdown(
    prices: Optional[PriceSeries],
    first_date: Optional[str],
    last_date: Optional[str],
) -> dict[str, Any] | str:
    """紀錄期間內 SPY 收盤相對近 `DRAWDOWN_PEAK_LOOKBACK` 筆（含當日）高點的最大回落。

    回落以正值表示（同 `market_analysis/downside_risk.py` 的 MDD 口徑）；
    無法檢核時回傳原因字串。
    """
    if first_date is None or last_date is None:
        return DRAWDOWN_NO_RECORD
    if not prices:
        return DRAWDOWN_PRICE_UNAVAILABLE
    start, end = date.fromisoformat(first_date), date.fromisoformat(last_date)
    n_days = 0
    max_dd = 0.0
    max_dd_date: Optional[date] = None
    first_breach: Optional[date] = None
    for i, (day, close) in enumerate(prices):
        if not start <= day <= end:
            continue
        window = prices[max(0, i - DRAWDOWN_PEAK_LOOKBACK + 1) : i + 1]
        peak = max(c for _, c in window)
        if peak <= 0:
            continue
        n_days += 1
        drawdown = (peak - close) / peak
        if max_dd_date is None or drawdown > max_dd:
            max_dd, max_dd_date = drawdown, day
        # 以價格比較（含等號），避開 `close / peak - 1` 的浮點誤差
        if first_breach is None and close <= peak * (1 - DRAWDOWN_THRESHOLD):
            first_breach = day
    if max_dd_date is None:
        return DRAWDOWN_PRICE_UNAVAILABLE
    return {
        "symbol": DRAWDOWN_SYMBOL,
        "threshold": DRAWDOWN_THRESHOLD,
        "lookback": DRAWDOWN_PEAK_LOOKBACK,
        "n_days": n_days,
        "max_drawdown": max_dd,
        "max_drawdown_date": max_dd_date.isoformat(),
        "first_breach_date": first_breach.isoformat() if first_breach else None,
        "reached": first_breach is not None,
    }


def build_report(
    regimes: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    tech_prices: dict[str, PriceSeries],
    voo_prices: PriceSeries,
    boxx_prices: PriceSeries,
    *,
    spy_prices: Optional[PriceSeries] = None,
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
    first_date = regimes[0]["trading_date"] if regimes else None
    last_date = regimes[-1]["trading_date"] if regimes else None
    return {
        "first_date": first_date,
        "last_date": last_date,
        "logged_days": len(regimes),
        "state_days": state_days,
        "segments": seg_rows,
        "lit_periods": lit_periods(signals),
        "spy_drawdown": spy_drawdown(spy_prices, first_date, last_date),
    }


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _drawdown_lines(check: dict[str, Any] | str) -> list[str]:
    lines = ["", f"## {DRAWDOWN_SYMBOL} 回檔檢核（§5.15）", ""]
    if isinstance(check, str):
        return lines + [f"- {check}"]
    threshold = f"{DRAWDOWN_THRESHOLD * 100:.0f}%"
    status = (
        f"已出現（首次 {check['first_breach_date']}）"
        if check["reached"]
        else "尚未出現"
    )
    return lines + [
        f"- 定義：{DRAWDOWN_SYMBOL} 收盤價相對前 {DRAWDOWN_PEAK_LOOKBACK} 個交易日（含當日）"
        f"最高收盤的跌幅；≥ {threshold} 視為一次回檔（2026-11-26 使用者決定）",
        f"- 記錄期間最大回落：{check['max_drawdown'] * 100:.1f}%（{check['max_drawdown_date']}）",
        f"- ≥ {threshold} 回檔：{status}",
    ]


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
    check = result.get("spy_drawdown")
    if check is not None:
        lines += _drawdown_lines(check)
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
