"""大盤三態切換 + 動能輪動（日線）回測：報告與及格判定。只產出報告，不改任何參數。

用法（在 nexus_core 下）：
    # 先補抓日線（只抓 1d，不抓小時線）
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_regime_momentum_backtest.py --fetch
    # 回測與報告（只讀快取）
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_regime_momentum_backtest.py

報告輸出到 gitignored 的 `reports/regime_momentum/`（report.md / results.json）。
判讀以 Sortino 為主，超額報酬 vs 減碼 B&H、MDD、CVaR95 為輔；Sharpe 只作描述。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.data_store import DataStore  # noqa: E402
from calibration.regime_momentum_backtest import (  # noqa: E402
    CASH_PROXY,
    CASH_SYMBOL,
    CORE_PROXY,
    CORE_SYMBOL,
    MARKET_SYMBOL,
    Metrics,
    RegimeMomentumParams,
    SimulationResult,
    annual_turnover,
    build_cash_index,
    buy_and_hold,
    chain_returns,
    compute_metrics,
    equal_weight_pit,
    realized_cash_rate,
    regime_switch_stats,
    scaled_benchmark,
    simulate,
    slice_nav,
    window_drawdown,
)

DEFAULT_CACHE = Path("/app/.calibration_cache")
DEFAULT_OUT = Path("reports/regime_momentum")
START = "2007-01-03"
END = "2025-12-31"
FETCH_FROM = "2005-01-01"

CRASH_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("2008 金融海嘯", "2007-10-01", "2009-03-31"),
    ("2020 疫情崩跌", "2020-02-01", "2020-03-31"),
    ("2022 空頭", "2022-01-01", "2022-12-31"),
)
SUBPERIODS: tuple[tuple[str, str, str], ...] = (
    ("2007–2015", "2007-01-03", "2015-12-31"),
    ("2016–2025", "2016-01-01", "2025-12-31"),
)
# 「三段崩跌 MDD 明顯低於 B&H」的量化門檻：策略 MDD ≤ 對照 MDD × 此比例
CRASH_MDD_RATIO = 0.75


def all_symbols(params: RegimeMomentumParams) -> list[str]:
    return sorted(
        set(params.universe)
        | set(params.defensive)
        | {MARKET_SYMBOL, CORE_SYMBOL, CORE_PROXY, CASH_SYMBOL, CASH_PROXY}
    )


async def fetch_daily(store: DataStore, symbols: list[str]) -> None:
    from calibration.fetcher import YFinanceFetcher

    fetcher = YFinanceFetcher()
    for sym in symbols:
        df = await fetcher.fetch(sym, "max", "1d")
        if not df.empty:
            idx = pd.to_datetime(df.index, utc=True)
            df = df[idx >= pd.Timestamp(FETCH_FROM, tz="UTC")]
        n = store.save("1d", sym, df)
        print(f"  {sym}: {n} 列", flush=True)


def _to_dates(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
    out = df.copy()
    out.index = pd.DatetimeIndex(idx.tz_localize(None).normalize())
    return out[~out.index.duplicated(keep="last")]


def load_panel(
    store: DataStore, symbols: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """以 SPY 的交易日曆對齊所有標的的 (開盤, 收盤)。"""
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = store.load("1d", sym)
        if df is None or df.empty:
            continue
        frames[sym] = _to_dates(df).astype(float)
    calendar = frames[MARKET_SYMBOL].index
    opens = pd.DataFrame({s: f["Open"] for s, f in frames.items()}).reindex(calendar)
    closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).reindex(calendar)
    return opens, closes


def prepare(
    opens: pd.DataFrame, closes: pd.DataFrame, params: RegimeMomentumParams
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """VOO 上市前以 SPY 串接；建立 BOXX 現金指數。"""
    opens = opens.copy()
    closes = closes.copy()
    closes[CORE_SYMBOL] = chain_returns(closes[CORE_SYMBOL], closes[CORE_PROXY])
    opens[CORE_SYMBOL] = chain_returns(opens[CORE_SYMBOL], opens[CORE_PROXY])
    cash_index = build_cash_index(
        closes.index,
        closes.get(CASH_SYMBOL),
        closes.get(CASH_PROXY),
        params.fallback_cash_rate,
    )
    for sym in list(params.universe) + list(params.defensive):
        if sym not in closes.columns:
            closes[sym] = float("nan")
            opens[sym] = float("nan")
    return opens, closes, cash_index


@dataclasses.dataclass
class Evaluation:
    name: str
    metrics: Metrics
    excess_vs: dict[str, float]  # 對照組名 → 累積超額報酬（策略 − 減碼對照）
    excess_cagr_vs: dict[str, float]  # 對照組名 → 年化超額報酬（CAGR 差）
    scaled_sortino: dict[str, float]
    scaled_weight: dict[str, float]


def evaluate(
    name: str,
    nav: pd.Series,
    benches: dict[str, pd.Series],
    cash_index: pd.Series,
    mar: float,
) -> Evaluation:
    m = compute_metrics(nav, mar)
    excess: dict[str, float] = {}
    excess_cagr: dict[str, float] = {}
    s_sortino: dict[str, float] = {}
    s_w: dict[str, float] = {}
    for bname, bnav in benches.items():
        w, scaled = scaled_benchmark(nav, bnav.reindex(nav.index), cash_index, mar)
        sm = compute_metrics(scaled, mar)
        excess[bname] = m.total_return - sm.total_return
        excess_cagr[bname] = m.cagr - sm.cagr
        s_sortino[bname] = sm.sortino
        s_w[bname] = w
    return Evaluation(name, m, excess, excess_cagr, s_sortino, s_w)


def run_strategy(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cash_index: pd.Series,
    params: RegimeMomentumParams,
    start: str = START,
    end: str = END,
) -> SimulationResult:
    return simulate(
        opens, closes, cash_index, closes[MARKET_SYMBOL], start, end, params
    )


def _pct(x: Optional[float]) -> str:
    return "N/A" if x is None or x != x else f"{x * 100:.1f}%"


def _pp(x: float) -> str:
    return f"{x * 100:+.1f} pp"


def _f(x: float) -> str:
    return "N/A" if x != x else f"{x:.2f}"


def build_everything(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cash_index: pd.Series,
    raw_first_dates: dict[str, str],
) -> dict[str, Any]:
    base = RegimeMomentumParams()
    mar = realized_cash_rate(cash_index, START, END)
    result = run_strategy(opens, closes, cash_index, base)
    nav = result.nav
    bench_a = buy_and_hold(closes[CORE_SYMBOL], START, END).reindex(nav.index)
    bench_b = equal_weight_pit(
        opens, closes, base.universe, START, END, base.cost_rate
    ).reindex(nav.index)
    benches = {"a": bench_a, "b": bench_b}

    main = evaluate("策略", nav, benches, cash_index, mar)
    bench_eval = {
        "a": compute_metrics(bench_a, mar),
        "b": compute_metrics(bench_b, mar),
    }

    # 逐年
    yearly: list[dict[str, Any]] = []
    for year in range(2007, 2026):
        ys, ye = f"{year}-01-01", f"{year}-12-31"
        row: dict[str, Any] = {"year": year}
        for label, series in (("strategy", nav), ("a", bench_a), ("b", bench_b)):
            s = slice_nav(series, ys, ye)
            if len(s) < 20:
                continue
            ym = compute_metrics(s, mar)
            row[label] = {
                "return": ym.total_return,
                "sortino": ym.sortino,
                "mdd": ym.max_drawdown,
            }
        yearly.append(row)

    # 三段崩跌
    crashes: list[dict[str, Any]] = []
    for label, cs, ce in CRASH_WINDOWS:
        s_mdd = window_drawdown(nav, cs, ce)
        a_mdd = window_drawdown(bench_a, cs, ce)
        b_mdd = window_drawdown(bench_b, cs, ce)
        s_ret = float(
            slice_nav(nav, cs, ce).iloc[-1] / slice_nav(nav, cs, ce).iloc[0] - 1
        )
        a_ret = float(
            slice_nav(bench_a, cs, ce).iloc[-1] / slice_nav(bench_a, cs, ce).iloc[0] - 1
        )
        b_ret = float(
            slice_nav(bench_b, cs, ce).iloc[-1] / slice_nav(bench_b, cs, ce).iloc[0] - 1
        )
        crashes.append(
            {
                "label": label,
                "start": cs,
                "end": ce,
                "strategy_mdd": s_mdd,
                "a_mdd": a_mdd,
                "b_mdd": b_mdd,
                "strategy_ret": s_ret,
                "a_ret": a_ret,
                "b_ret": b_ret,
                "pass_a": s_mdd <= a_mdd * CRASH_MDD_RATIO,
                "pass_b": s_mdd <= b_mdd * CRASH_MDD_RATIO,
            }
        )

    # 及格判定
    criteria = {
        "sortino_vs_scaled_a": main.metrics.sortino > main.scaled_sortino["a"],
        "sortino_vs_scaled_b": main.metrics.sortino > main.scaled_sortino["b"],
        "excess_vs_scaled_a": main.excess_cagr_vs["a"] > 0,
        "excess_vs_scaled_b": main.excess_cagr_vs["b"] > 0,
        "crash_mdd_vs_a": all(c["pass_a"] for c in crashes),
        "crash_mdd_vs_b": all(c["pass_b"] for c in crashes),
    }

    # 參數敏感度（一次只改一個）
    variants: list[tuple[str, RegimeMomentumParams]] = [
        ("基準（前 5／25%／確認 3 日／12-1）", base),
        ("前 3 名", dataclasses.replace(base, top_n=3)),
        ("前 7 名", dataclasses.replace(base, top_n=7)),
        ("回落 20%", dataclasses.replace(base, trailing_stop=0.20)),
        ("回落 30%", dataclasses.replace(base, trailing_stop=0.30)),
        ("確認 1 日", dataclasses.replace(base, confirm_days=1)),
        ("確認 5 日", dataclasses.replace(base, confirm_days=5)),
        ("動能 6-1", dataclasses.replace(base, momentum_lookback=126)),
    ]
    sensitivity: list[dict[str, Any]] = []
    for label, p in variants:
        r = result if p == base else run_strategy(opens, closes, cash_index, p)
        ev = evaluate(label, r.nav, benches, cash_index, mar)
        sensitivity.append(
            {
                "label": label,
                "sortino": ev.metrics.sortino,
                "excess_a": ev.excess_cagr_vs["a"],
                "excess_b": ev.excess_cagr_vs["b"],
                "mdd": ev.metrics.max_drawdown,
                "total_return": ev.metrics.total_return,
                "cagr": ev.metrics.cagr,
                "switches": regime_switch_stats(r.regime)["switches"],
                "crash_mdd": [
                    window_drawdown(r.nav, cs, ce) for _, cs, ce in CRASH_WINDOWS
                ],
            }
        )

    # 子期間（切片主回測，狀態連續）
    subperiods: list[dict[str, Any]] = []
    for label, ss, se in SUBPERIODS:
        sn = slice_nav(nav, ss, se)
        sub_benches = {k: slice_nav(v, ss, se) for k, v in benches.items()}
        sub_mar = realized_cash_rate(cash_index, ss, se)
        ev = evaluate(label, sn, sub_benches, cash_index, sub_mar)
        subperiods.append(
            {
                "label": label,
                "mar": sub_mar,
                "sortino": ev.metrics.sortino,
                "a_sortino": compute_metrics(sub_benches["a"], sub_mar).sortino,
                "b_sortino": compute_metrics(sub_benches["b"], sub_mar).sortino,
                "scaled_a_sortino": ev.scaled_sortino["a"],
                "scaled_b_sortino": ev.scaled_sortino["b"],
                "excess_a": ev.excess_cagr_vs["a"],
                "excess_b": ev.excess_cagr_vs["b"],
                "mdd": ev.metrics.max_drawdown,
                "a_mdd": compute_metrics(sub_benches["a"], sub_mar).max_drawdown,
                "b_mdd": compute_metrics(sub_benches["b"], sub_mar).max_drawdown,
            }
        )

    # 換手與 whipsaw
    sw = regime_switch_stats(result.regime)
    hp = result.holding_periods
    stats = {
        **sw,
        "annual_turnover": annual_turnover(result),
        "avg_holding_days": sum(hp) / len(hp) if hp else float("nan"),
        "stock_positions_closed": float(len(hp)),
        "trailing_stops": float(len(result.stops)),
        "trades": float(len(result.trades)),
        "avg_cash_weight": float(result.weights["CASH"].mean()),
    }
    stop_by_symbol: dict[str, int] = {}
    for ev_stop in result.stops:
        stop_by_symbol[ev_stop.symbol] = stop_by_symbol.get(ev_stop.symbol, 0) + 1
    picks: dict[str, int] = {}
    for t in result.trades:
        if t.notional > 0 and t.symbol in base.universe:
            picks[t.symbol] = picks.get(t.symbol, 0) + 1

    return {
        "mar": mar,
        "main": main,
        "bench_eval": bench_eval,
        "yearly": yearly,
        "crashes": crashes,
        "criteria": criteria,
        "sensitivity": sensitivity,
        "subperiods": subperiods,
        "stats": stats,
        "stop_by_symbol": stop_by_symbol,
        "picks": picks,
        "first_dates": raw_first_dates,
    }


def build_report(d: dict[str, Any]) -> str:
    main: Evaluation = d["main"]
    ba: Metrics = d["bench_eval"]["a"]
    bb: Metrics = d["bench_eval"]["b"]
    m = main.metrics
    lines: list[str] = []
    lines.append("# 大盤三態切換 + 動能輪動 回測報告（2007–2025，日線）\n")
    lines.append(
        f"MAR = 期間內 BOXX（BIL 代理）實際年化報酬 **{d['mar'] * 100:.2f}%**。"
        "判讀以 Sortino 為主；減碼 B&H = w × 對照 + (1−w) × BOXX，w 以下行差對齊。\n"
    )
    lines.append("## 1. 整體結果\n")
    lines.append(
        "| 組合 | **Sortino** | 年化超額 vs 減碼 a | 年化超額 vs 減碼 b | MDD | 1 日 CVaR95 | 總報酬 | CAGR | Sharpe（描述） |"
    )
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    lines.append(
        f"| **策略** | **{_f(m.sortino)}** | {_pp(main.excess_cagr_vs['a'])} | {_pp(main.excess_cagr_vs['b'])} | "
        f"{_pct(m.max_drawdown)} | {_pct(m.cvar_95)} | {_pct(m.total_return)} | {_pct(m.cagr)} | {_f(m.sharpe)} |"
    )
    lines.append(
        f"| a. B&H VOO | {_f(ba.sortino)} | — | — | {_pct(ba.max_drawdown)} | {_pct(ba.cvar_95)} | "
        f"{_pct(ba.total_return)} | {_pct(ba.cagr)} | {_f(ba.sharpe)} |"
    )
    lines.append(
        f"| b. B&H 等權選股池 | {_f(bb.sortino)} | — | — | {_pct(bb.max_drawdown)} | {_pct(bb.cvar_95)} | "
        f"{_pct(bb.total_return)} | {_pct(bb.cagr)} | {_f(bb.sharpe)} |"
    )
    lines.append(
        f"| c-a. 減碼 VOO（w={main.scaled_weight['a']:.2f}） | {_f(main.scaled_sortino['a'])} | | | | | | | |"
    )
    lines.append(
        f"| c-b. 減碼等權池（w={main.scaled_weight['b']:.2f}） | {_f(main.scaled_sortino['b'])} | | | | | | | |"
    )
    lines.append("")

    lines.append("## 2. 及格判定（使用者標準）\n")
    labels = {
        "sortino_vs_scaled_a": "Sortino > 減碼 VOO",
        "sortino_vs_scaled_b": "Sortino > 減碼等權選股池",
        "excess_vs_scaled_a": "年化超額報酬 vs 減碼 VOO > 0",
        "excess_vs_scaled_b": "年化超額報酬 vs 減碼等權選股池 > 0",
        "crash_mdd_vs_a": f"三段崩跌 MDD 皆 ≤ VOO × {CRASH_MDD_RATIO}",
        "crash_mdd_vs_b": f"三段崩跌 MDD 皆 ≤ 等權池 × {CRASH_MDD_RATIO}",
    }
    for key, label in labels.items():
        lines.append(f"- {'✅' if d['criteria'][key] else '❌'} {label}")
    overall = all(d["criteria"].values())
    lines.append(f"\n**整體：{'通過' if overall else '不通過'}**\n")

    lines.append("## 3. 三段崩跌\n")
    lines.append(
        "| 區間 | 策略 MDD／報酬 | VOO MDD／報酬 | 等權池 MDD／報酬 | vs VOO | vs 等權池 |"
    )
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for c in d["crashes"]:
        lines.append(
            f"| {c['label']}（{c['start']}～{c['end']}） | {_pct(c['strategy_mdd'])}／{_pct(c['strategy_ret'])} | "
            f"{_pct(c['a_mdd'])}／{_pct(c['a_ret'])} | {_pct(c['b_mdd'])}／{_pct(c['b_ret'])} | "
            f"{'✅' if c['pass_a'] else '❌'} | {'✅' if c['pass_b'] else '❌'} |"
        )
    lines.append("")

    lines.append("## 4. 逐年\n")
    lines.append(
        "| 年 | 策略 報酬／Sortino／MDD | VOO 報酬／Sortino／MDD | 等權池 報酬／Sortino／MDD |"
    )
    lines.append("| :--- | :---: | :---: | :---: |")
    for row in d["yearly"]:
        cells = []
        for k in ("strategy", "a", "b"):
            v = row.get(k)
            cells.append(
                f"{_pct(v['return'])}／{_f(v['sortino'])}／{_pct(v['mdd'])}"
                if v
                else "—"
            )
        lines.append(f"| {row['year']} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## 5. 參數敏感度（一次只改一個參數）\n")
    lines.append(
        "| 變體 | Sortino | 年化超額 vs 減碼 VOO | 年化超額 vs 減碼等權池 | MDD | CAGR | 狀態切換 | 2008／2020／2022 MDD |"
    )
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |")
    for s in d["sensitivity"]:
        cm = "／".join(_pct(x) for x in s["crash_mdd"])
        lines.append(
            f"| {s['label']} | {_f(s['sortino'])} | {_pp(s['excess_a'])} | {_pp(s['excess_b'])} | "
            f"{_pct(s['mdd'])} | {_pct(s['cagr'])} | {int(s['switches'])} | {cm} |"
        )
    lines.append("")

    lines.append("## 6. 子期間\n")
    lines.append(
        "| 期間 | MAR | 策略 Sortino | VOO | 等權池 | 減碼 VOO | 減碼等權池 | 年化超額 vs 減碼 VOO | 年化超額 vs 減碼等權池 | 策略／VOO／等權池 MDD |"
    )
    lines.append(
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |"
    )
    for s in d["subperiods"]:
        lines.append(
            f"| {s['label']} | {s['mar'] * 100:.2f}% | {_f(s['sortino'])} | {_f(s['a_sortino'])} | {_f(s['b_sortino'])} | "
            f"{_f(s['scaled_a_sortino'])} | {_f(s['scaled_b_sortino'])} | {_pp(s['excess_a'])} | {_pp(s['excess_b'])} | "
            f"{_pct(s['mdd'])}／{_pct(s['a_mdd'])}／{_pct(s['b_mdd'])} |"
        )
    lines.append("")

    st = d["stats"]
    picks: dict[str, int] = d["picks"]
    picks_sorted = dict(sorted(picks.items(), key=lambda kv: (-kv[1], kv[0])))
    lines.append("## 7. 換手、狀態切換與 whipsaw\n")
    lines.append(
        f"- 狀態切換 {int(st['switches'])} 次；whipsaw（切換後 20 個交易日內切回）{int(st['whipsaws'])} 次\n"
        f"- 各狀態天數：GOOD {int(st['days_good'])}／WEAK {int(st['days_weak'])}／BAD {int(st['days_bad'])}\n"
        f"- 單邊年換手率 {st['annual_turnover'] * 100:.0f}%；成交 {int(st['trades'])} 筆\n"
        f"- 個股平均持有 {st['avg_holding_days']:.0f} 個交易日（已平倉 {int(st['stock_positions_closed'])} 筆）\n"
        f"- 回落停損 {int(st['trailing_stops'])} 次：{d['stop_by_symbol']}\n"
        f"- 平均現金（BOXX）權重 {st['avg_cash_weight'] * 100:.1f}%\n"
        f"- 個股被選入次數：{picks_sorted}\n"
    )

    lines.append("## 8. 限制\n")
    lines.append(
        "- **存活者偏差**：Yahoo 沒有已下市公司的資料，選股池只含至今仍存在的公司（雖刻意納入 INTC、CSCO、IBM、"
        "MRNA、COIN 等落後或崩跌標的）。已下市者（例如被併購、破產的科技股）無法納入，動能策略的表現因此偏樂觀。"
        "對照組 b（等權持有同一選股池）承受同樣的偏差，所以「策略 vs 等權池」的比較較不受影響。\n"
        "- **選股池本身是 2026 年挑的**：成員都是今天仍為大型股的公司，這是無法消除的前視；解讀絕對報酬時應打折，"
        "重點看相對對照組 b 的差異。\n"
        "- **VOO 上市前（2010-09 前）以 SPY 報酬代理**；**BOXX 上市前（2022-12 前）以 BIL 日報酬代理，"
        "BIL 上市前（2007-05 前）以固定年化利率計息**。\n"
        "- 價格為 Yahoo 分割與股息調整後價格（總報酬近似）；以開盤價成交、單邊成本 0.15%，未模擬滑價與稅。\n"
        "- BOXX 部位視為現金，進出不計成本。\n"
    )
    lines.append("## 9. 標的資料起始日\n")
    lines.append(
        ", ".join(f"{k} {v}" for k, v in sorted(d["first_dates"].items())) + "\n"
    )
    return "\n".join(lines)


def to_json(d: dict[str, Any]) -> dict[str, Any]:
    main: Evaluation = d["main"]
    out = {k: v for k, v in d.items() if k not in ("main", "bench_eval")}
    out["main"] = {
        "metrics": dataclasses.asdict(main.metrics),
        "excess_vs": main.excess_vs,
        "excess_cagr_vs": main.excess_cagr_vs,
        "scaled_sortino": main.scaled_sortino,
        "scaled_weight": main.scaled_weight,
    }
    out["bench_eval"] = {k: dataclasses.asdict(v) for k, v in d["bench_eval"].items()}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="大盤三態 + 動能輪動回測（只產報告）")
    parser.add_argument("--fetch", action="store_true", help="補抓日線後結束")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    store = DataStore(Path(args.cache_dir))
    params = RegimeMomentumParams()
    symbols = all_symbols(params)
    if args.fetch:
        asyncio.run(fetch_daily(store, symbols))
        return 0

    opens, closes = load_panel(store, symbols)
    raw_first_dates = {
        s: str(closes[s].first_valid_index().date())
        for s in closes.columns
        if closes[s].first_valid_index() is not None
    }
    opens, closes, cash_index = prepare(opens, closes, params)
    d = build_everything(opens, closes, cash_index, raw_first_dates)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(build_report(d), encoding="utf-8")
    (out / "results.json").write_text(
        json.dumps(to_json(d), ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"報告已輸出：{out / 'report.md'}")
    print("及格判定：", d["criteria"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
