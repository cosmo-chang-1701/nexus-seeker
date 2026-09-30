"""總經三態配置回測：好 → 100% 科技池、轉差 → 100% VOO、最差 → 100% BOXX。

使用者的預設狀態是 100% 科技池；VOO 與 BOXX 只是例外的防守狀態。本腳本回答：
三態切換相對「一直 100% 科技池」降低了多少回撤、付出了多少報酬，以及能否同時做到
「大部分時間 100% 科技池」與「MDD ≤ 25%」。只產出報告，不改任何參數。

- 主版本：信用利差（BAA10Y）+ Sahm 即時版；0 個警訊 → 好、1 個 → 轉差、2 個 → 最差。
- 變體：再加入聯準會升息循環（DFF 較 126 個交易日前上升 ≥ 1.0 pp）；≥ 2 個 → 最差。
- 額外對照：股票 40% 固定、股票內科技／VOO 切換（任一亮／兩者皆亮）。

資料與選項 A 共用 `calibration/daily_panel.py`；總經指標見 `calibration/macro_regime.py`；
指標一律呼叫 `market_analysis/downside_risk.py`。

用法（在 nexus_core 下；日線快取由 run_regime_momentum_backtest.py --fetch 建立，
FRED 快取由 `python -m calibration fetch-fred` 建立）：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_macro_switch_backtest.py

報告輸出到 gitignored 的 `reports/macro_switch/`（report.md / results.json）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.daily_panel import load_panel, prepare_panel  # noqa: E402
from calibration.data_store import DataStore  # noqa: E402
from calibration.macro_regime import (  # noqa: E402
    FED_SERIES,
    SAHM_SERIES,
    SPREAD_SERIES,
    SPREAD_SERIES_PREFERRED,
    MacroParams,
    StateEpisode,
    load_fred,
    macro_states,
    macro_states3,
    state_episodes,
    targets_series,
    tech_share_series,
    whipsaw_count,
)
from calibration.regime_momentum_backtest import (  # noqa: E402
    CASH_PROXY,
    CASH_SYMBOL,
    CORE_PROXY,
    CORE_SYMBOL,
    MARKET_SYMBOL,
    RegimeMomentumParams,
    equal_weight_pit,
    realized_cash_rate,
)
from calibration.static_allocation_backtest import (  # noqa: E402
    StaticAllocationParams,
    annual_turnover_static,
    simulate_static,
)
from scripts.run_static_allocation_backtest import (  # noqa: E402
    END,
    HEADER,
    START,
    SUBPERIODS,
    Row,
    _evaluate,
    _f,
    _p,
    _row_md,
    run_references,
)

DEFAULT_CACHE = Path("/app/.calibration_cache")
DEFAULT_OUT = Path("reports/macro_switch")

MDD_LIMIT = 0.25
WHIPSAW_WINDOW = 60
LOOK_WINDOW = 60  # 區段前後比較的交易日數
COVID_PEAK = "2020-02-19"  # 2020 急跌前的 S&P 500 高點
X_GRID: tuple[float, ...] = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4)

BASE_MAIN = MacroParams()
BASE_FED = MacroParams(include_fed_hike=True)
STATE_NAMES = {
    "GOOD": "好（100% 科技池）",
    "WEAK": "轉差（100% VOO）",
    "WORST": "最差（100% BOXX）",
}


def _sens(base: MacroParams, with_fed: bool) -> tuple[tuple[str, MacroParams], ...]:
    items: list[tuple[str, MacroParams]] = [
        ("利差倍數 1.1", replace(base, spread_mult=1.1)),
        ("利差倍數 1.3", replace(base, spread_mult=1.3)),
        ("利差均線 63 日", replace(base, spread_ma_days=63)),
        ("利差均線 252 日", replace(base, spread_ma_days=252)),
        ("確認 1 日", replace(base, confirm_days=1)),
        ("確認 10 日", replace(base, confirm_days=10)),
        ("Sahm 門檻 0.3", replace(base, sahm_threshold=0.3)),
    ]
    if with_fed:
        items += [
            ("升息門檻 0.75pp", replace(base, hike_threshold=0.75)),
            ("升息門檻 1.5pp", replace(base, hike_threshold=1.5)),
        ]
    return tuple(items)


@dataclass
class Run3:
    key: str
    params: MacroParams
    row: Row
    nav: pd.Series
    states: pd.DataFrame
    episodes: list[StateEpisode]
    switches: int
    whipsaws: int
    equity_scale: float = 1.0


@dataclass
class Inputs:
    opens: pd.DataFrame
    closes: pd.DataFrame
    cash_index: pd.Series
    spread: pd.Series
    sahm: pd.Series
    dff: pd.Series
    mar: float


def _base_params() -> StaticAllocationParams:
    # 三態的權重由 targets_by_day 給定；這裡只提供再平衡方式、成本與選股池
    return StaticAllocationParams(equity_share=1.0, tech_share=1.0, rebalance="ANNUAL")


def run3(
    key: str, params: MacroParams, inp: Inputs, label: str, equity_scale: float = 1.0
) -> Run3:
    states = macro_states3(inp.spread, inp.sahm, inp.closes.index, params, inp.dff)
    tgt = targets_series(states["effective"], equity_scale)
    res = simulate_static(
        inp.opens,
        inp.closes,
        inp.cash_index,
        START,
        END,
        _base_params(),
        targets_by_day=tgt,
    )
    row = _evaluate(
        key,
        label,
        "macro3",
        res.nav,
        inp.mar,
        None,
        annual_turnover_static(res),
        len(res.rebalance_days),
    )
    eff = states["effective"].where(states["effective"].notna(), "GOOD")
    eps = state_episodes(eff, START, END)
    return Run3(
        key=key,
        params=params,
        row=row,
        nav=res.nav,
        states=states,
        episodes=eps,
        switches=max(len(eps) - 1, 0),
        whipsaws=whipsaw_count(eps, WHIPSAW_WINDOW),
        equity_scale=equity_scale,
    )


def run_sleeve(key: str, params: MacroParams, inp: Inputs) -> Row:
    """額外對照：股票 40% 固定、股票內科技／VOO 切換，BOXX 60% 不動。"""
    states = macro_states(inp.spread, inp.sahm, inp.closes.index, params)
    t = tech_share_series(states["effective"], params)
    p = StaticAllocationParams(equity_share=0.40, tech_share=1.0, rebalance="ANNUAL")
    res = simulate_static(
        inp.opens, inp.closes, inp.cash_index, START, END, p, tech_share_by_day=t
    )
    rule = "任一亮" if params.combine == "ANY" else "兩者皆亮"
    return _evaluate(
        key,
        f"股 40% 固定、股票內切換（{rule} → VOO）",
        "sleeve",
        res.nav,
        inp.mar,
        p,
        annual_turnover_static(res),
        len(res.rebalance_days) + len(res.switch_days),
    )


def run_static_4060(inp: Inputs) -> Row:
    p = StaticAllocationParams(equity_share=0.40, tech_share=1.0, rebalance="ANNUAL")
    res = simulate_static(inp.opens, inp.closes, inp.cash_index, START, END, p)
    return _evaluate(
        "S_4060",
        "靜態 股 40% 全科技池 + 60% BOXX",
        "static",
        res.nav,
        inp.mar,
        p,
        annual_turnover_static(res),
        len(res.rebalance_days),
    )


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------


def state_time_share(run: Run3) -> dict[str, float]:
    eff = run.states["effective"]
    eff = eff[(eff.index >= pd.Timestamp(START)) & (eff.index <= pd.Timestamp(END))]
    eff = eff.where(eff.notna(), "GOOD")
    return {s: float((eff == s).mean()) for s in ("GOOD", "WEAK", "WORST")}


def yearly_non_tech_days(run: Run3) -> dict[int, dict[str, int]]:
    eff = run.states["effective"]
    eff = eff[(eff.index >= pd.Timestamp(START)) & (eff.index <= pd.Timestamp(END))]
    eff = eff.where(eff.notna(), "GOOD")
    out: dict[int, dict[str, int]] = {}
    for year in sorted(set(eff.index.year)):
        y = eff[eff.index.year == year]
        out[int(year)] = {
            "WEAK": int((y == "WEAK").sum()),
            "WORST": int((y == "WORST").sum()),
            "days": int(len(y)),
        }
    return out


def _ret(series: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> float:
    s = series.dropna()
    s = s[(s.index >= a) & (s.index <= b)]
    if len(s) < 2:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def _drawdown_at(series: pd.Series, day: pd.Timestamp, lookback: int = 252) -> float:
    s = series.dropna()
    s = s[s.index <= day].iloc[-lookback:]
    if s.empty:
        return float("nan")
    return float(1.0 - s.iloc[-1] / s.max())


def defensive_episodes(
    run: Run3, tech: pd.Series, voo: pd.Series
) -> list[dict[str, Any]]:
    """每段「轉差」與「最差」：起訖、進入時已從一年高點跌了多少、區段內與區段後 60 日報酬。"""
    idx = tech.dropna().index
    rows: list[dict[str, Any]] = []
    for ep in run.episodes:
        if ep.state == "GOOD":
            continue
        after_end_pos = idx.searchsorted(ep.end) + LOOK_WINDOW
        after_end = idx[min(after_end_pos, len(idx) - 1)]
        rows.append(
            {
                "state": ep.state,
                "start": str(ep.start.date()),
                "end": str(ep.end.date()),
                "days": ep.days,
                "voo_dd_at_entry": _drawdown_at(voo, ep.start),
                "tech_dd_at_entry": _drawdown_at(tech, ep.start),
                "voo_during": _ret(voo, ep.start, ep.end),
                "tech_during": _ret(tech, ep.start, ep.end),
                "voo_after_60": _ret(voo, ep.end, after_end),
                "tech_after_60": _ret(tech, ep.end, after_end),
            }
        )
    return rows


def covid_indicator_timing(
    runs: list[Run3], tech: pd.Series, voo: pd.Series
) -> list[dict[str, Any]]:
    """2020-02～06：各指標第一次亮起（已可用）的日期、當時 VOO／科技池距 2020-02-19 高點的跌幅。"""
    peak = pd.Timestamp(COVID_PEAK)
    lo, hi = pd.Timestamp("2020-02-01"), pd.Timestamp("2020-06-30")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def dd_from_peak(series: pd.Series, day: pd.Timestamp) -> float:
        s = series.dropna()
        p = s[s.index <= peak].iloc[-1]
        cur = s[s.index <= day].iloc[-1]
        return float(1.0 - cur / p)

    for run in runs:
        st = run.states
        window = st[(st.index >= lo) & (st.index <= hi)]
        for col, name in (
            ("spread_warn", "信用利差"),
            ("sahm_warn", "Sahm"),
            ("fed_warn", "升息循環"),
        ):
            if col not in window.columns or name in seen:
                continue
            seen.add(name)
            lit = window.index[window[col] >= 0.5]
            day = lit[0] if len(lit) else None
            out.append(
                {
                    "item": f"{name}警訊可用",
                    "date": str(day.date()) if day is not None else "未亮",
                    "voo_dd": dd_from_peak(voo, day)
                    if day is not None
                    else float("nan"),
                    "tech_dd": dd_from_peak(tech, day)
                    if day is not None
                    else float("nan"),
                }
            )
        eff = window["effective"]
        for state in ("WEAK", "WORST"):
            hit = eff.index[eff == state]
            day = hit[0] if len(hit) else None
            out.append(
                {
                    "item": f"{run.row.label}：開盤採用「{STATE_NAMES[state]}」",
                    "date": str(day.date()) if day is not None else "未進入",
                    "voo_dd": dd_from_peak(voo, day)
                    if day is not None
                    else float("nan"),
                    "tech_dd": dd_from_peak(tech, day)
                    if day is not None
                    else float("nan"),
                }
            )
    return out


def year_2022_boxx(run: Run3) -> list[str]:
    eps = [
        e
        for e in run.episodes
        if e.state == "WORST"
        and e.end >= pd.Timestamp("2022-01-01")
        and e.start <= pd.Timestamp("2022-12-31")
    ]
    return [f"{e.start.date()} → {e.end.date()}（{e.days} 日）" for e in eps]


def verdict(run: Run3, sens: list[Run3], s4060: Row) -> list[tuple[str, bool, str]]:
    def c1_of(r: Row) -> bool:
        return r.metrics.max_drawdown <= MDD_LIMIT + 1e-12 and all(
            v <= MDD_LIMIT + 1e-12 for v in r.crashes.values()
        )

    def c2_of(r: Row) -> bool:
        return (
            r.metrics.sortino > s4060.metrics.sortino
            and r.metrics.cagr > s4060.metrics.cagr
        )

    r = run.row
    c1, c2 = c1_of(r), c2_of(r)
    sub = {
        n: (
            r.sub[n]["sortino"] > s4060.sub[n]["sortino"]
            and r.sub[n]["cagr"] > s4060.sub[n]["cagr"]
        )
        for n, _, _ in SUBPERIODS
    }
    c3 = all(sub.values())
    base_ok = c1 and c2
    same = [c1_of(v.row) and c2_of(v.row) for v in sens]
    consistent = all(s == base_ok for s in same)
    c4 = consistent and base_ok
    return [
        (
            "1. 整體與三段崩跌 MDD 皆 ≤ 25%",
            c1,
            f"整體 {_p(r.metrics.max_drawdown)}；"
            + "／".join(f"{k} {_p(v)}" for k, v in r.crashes.items()),
        ),
        (
            "2. Sortino 與 CAGR 皆優於靜態 40% 全科技池 + 60% BOXX",
            c2,
            f"Sortino {_f(r.metrics.sortino)} vs {_f(s4060.metrics.sortino)}；"
            f"CAGR {_p(r.metrics.cagr)} vs {_p(s4060.metrics.cagr)}",
        ),
        (
            "3. 兩段子期間皆優於靜態 40%／60%",
            c3,
            "；".join(f"{k}：{'優於' if v else '未優於'}" for k, v in sub.items()),
        ),
        (
            "4. 敏感度：結論不因單一參數翻轉（且本身及格）",
            c4,
            f"{sum(same)}／{len(same)} 個變體同時通過 1 與 2；結論"
            f"{'一致' if consistent else '不一致'}",
        ),
    ]


def x_tradeoff(base: MacroParams, inp: Inputs, tag: str) -> list[Run3]:
    out: list[Run3] = []
    for x in X_GRID:
        label = f"{tag}，X = {x:.0%}"
        print(f"  取捨 {label}", flush=True)
        out.append(run3(f"X_{tag}_{x:.2f}", base, inp, label, equity_scale=x))
    return out


def static_x_rows(inp: Inputs) -> list[Row]:
    """對等比較：一直 X% 全科技池 + (1−X)% BOXX（每年再平衡，不做任何總經判斷）。"""
    out: list[Row] = []
    for x in X_GRID:
        p = StaticAllocationParams(equity_share=x, tech_share=1.0, rebalance="ANNUAL")
        res = simulate_static(inp.opens, inp.closes, inp.cash_index, START, END, p)
        out.append(
            _evaluate(
                f"SX_{x:.2f}",
                f"靜態 {x:.0%} 全科技池 + {1 - x:.0%} BOXX",
                "static",
                res.nav,
                inp.mar,
                p,
                annual_turnover_static(res),
                len(res.rebalance_days),
            )
        )
    return out


def max_x_under_limit(runs: list[Run3]) -> Optional[Run3]:
    ok = [
        r
        for r in runs
        if r.row.metrics.max_drawdown <= MDD_LIMIT + 1e-12
        and all(v <= MDD_LIMIT + 1e-12 for v in r.row.crashes.values())
    ]
    return max(ok, key=lambda r: r.equity_scale) if ok else None


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def _vs_tech(run_row: Row, tech_row: Row) -> str:
    m, t = run_row.metrics, tech_row.metrics
    crash = "／".join(
        f"{k} {_p(run_row.crashes[k] - tech_row.crashes[k])}" for k in tech_row.crashes
    )
    return (
        f"| {run_row.label} | {_p(m.max_drawdown - t.max_drawdown)} | {crash} "
        f"| {_p(m.cagr - t.cagr)} | {m.sortino - t.sortino:+.2f} |"
    )


def _episode_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 狀態 | 起 → 訖 | 交易日 | 進入時 VOO／科技池距一年高點 | 區段內 VOO／科技池 | 結束後 60 日 VOO／科技池 |",
        "| :--- | :--- | ---: | :---: | :---: | :---: |",
    ]
    for r in rows:
        lines.append(
            f"| {STATE_NAMES[r['state']]} | {r['start']} → {r['end']} | {r['days']} "
            f"| −{_p(r['voo_dd_at_entry'])}／−{_p(r['tech_dd_at_entry'])} "
            f"| {_p(r['voo_during'])}／{_p(r['tech_during'])} "
            f"| {_p(r['voo_after_60'])}／{_p(r['tech_after_60'])} |"
        )
    return lines


def build_report(ctx: dict[str, Any]) -> str:
    main: Run3 = ctx["main"]
    fed: Run3 = ctx["fed"]
    tech_row: Row = ctx["tech_row"]
    s4060: Row = ctx["s4060"]
    lines: list[str] = [
        "# 總經三態配置回測報告",
        "",
        f"- 期間：{START} → {END}（日線）；Sortino 的 MAR = 期間內 BOXX／BIL 實際年化 "
        f"{ctx['mar'] * 100:.2f}%",
        "- 三態：好（0 個警訊）→ 100% 科技池（22 檔 point-in-time 等權）；轉差（1 個）→ 100% VOO；"
        "最差（主版本 2 個、變體 ≥ 2 個）→ 100% BOXX。狀態改變的次一交易日開盤完整再平衡，"
        "另每年第一個交易日再平衡一次（科技池成員更新）",
        f"- 指標：{ctx['spread_note']}；Sahm：FRED `{SAHM_SERIES}`（即時公布值，次月 10 日才可用）；"
        f"變體另加 FRED `{FED_SERIES}`（較 126 個交易日前上升 ≥ 1.0 pp）。狀態切換需連續 5 個交易日",
        "- 使用者的預設狀態是 100% 科技池，VOO 與 BOXX 為例外的防守狀態；主要比較基準為 B&H 100% 科技池",
        "",
        "## 1. 重點回答",
        "",
        "### 1.1 相對「一直 100% 科技池」：降低多少回撤、付出多少報酬（負值＝回撤較淺／報酬較低）",
        "",
        "| 版本 | ΔMDD | Δ崩跌回撤 2008／2020／2022 | ΔCAGR | ΔSortino |",
        "| :--- | ---: | :--- | ---: | ---: |",
        _vs_tech(main.row, tech_row),
        _vs_tech(fed.row, tech_row),
        _vs_tech(s4060, tech_row),
        "",
        "### 1.2 2022 年是否進入 BOXX",
        "",
        f"- 主版本：{'；'.join(ctx['boxx_2022_main']) or '否（2022 年未進入最差狀態）'}",
        f"- 變體（加升息）：{'；'.join(ctx['boxx_2022_fed']) or '否（2022 年未進入最差狀態）'}",
        "",
        "## 2. 及格判定",
        "",
    ]
    for title, checks in (
        ("主版本", ctx["checks_main"]),
        ("變體（加升息）", ctx["checks_fed"]),
    ):
        lines += [
            f"### {title}",
            "",
            "| 標準 | 結果 | 說明 |",
            "| :--- | :---: | :--- |",
        ]
        for name, ok, note in checks:
            lines.append(f"| {name} | {'✅' if ok else '❌'} | {note} |")
        lines.append("")
    lines += [
        "## 3. 整體結果",
        "",
        HEADER,
        _row_md(main.row),
        _row_md(fed.row),
        _row_md(tech_row),
        _row_md(s4060),
        *[_row_md(r) for r in ctx["other_refs"]],
        *[_row_md(r) for r in ctx["sleeves"]],
        "",
        "### 3.1 子期間",
        "",
        "| 配置 | 2007–2015 CAGR／Sortino／MDD | 2016–2025 CAGR／Sortino／MDD |",
        "| :--- | :---: | :---: |",
    ]
    for r in [main.row, fed.row, tech_row, s4060]:
        cells = [
            f"{_p(r.sub[n]['cagr'])}／{_f(r.sub[n]['sortino'])}／{_p(r.sub[n]['mdd'])}"
            for n, _, _ in SUBPERIODS
        ]
        lines.append(f"| {r.label} | {cells[0]} | {cells[1]} |")
    lines += [
        "",
        "## 4. 各狀態時間占比與每年非科技天數",
        "",
        "| 版本 | 好（100% 科技池） | 轉差（VOO） | 最差（BOXX） | 切換次數 | whipsaw（60 日內切回） |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in (main, fed):
        sh = state_time_share(run)
        lines.append(
            f"| {run.row.label} | {_p(sh['GOOD'])} | {_p(sh['WEAK'])} | {_p(sh['WORST'])} "
            f"| {run.switches} | {run.whipsaws} |"
        )
    lines += [
        "",
        "| 年 | 主版本 轉差／最差 天數 | 變體 轉差／最差 天數 | 交易日 |",
        "| :--- | :---: | :---: | ---: |",
    ]
    ym, yf = yearly_non_tech_days(main), yearly_non_tech_days(fed)
    for year in sorted(ym):
        a, b = ym[year], yf[year]
        lines.append(
            f"| {year} | {a['WEAK']}／{a['WORST']} | {b['WEAK']}／{b['WORST']} | {a['days']} |"
        )
    lines += [
        "",
        "## 5. 每段「轉差」與「最差」",
        "",
        "> 「進入時距一年高點」越大代表訊號越落後（已跌很多才進入防守）；"
        "「區段內」若科技池報酬高於 VOO／BOXX，代表防守期間反而錯過上漲。",
        "",
        "### 5.1 主版本",
        "",
        *_episode_lines(ctx["episodes_main"]),
        "",
        "### 5.2 變體（加升息）",
        "",
        *_episode_lines(ctx["episodes_fed"]),
        "",
        "## 6. 2020 年 2–3 月急跌：各指標亮起時已發生的跌幅（自 2020-02-19 高點起算）",
        "",
        "| 事件 | 日期 | VOO 已跌 | 科技池已跌 |",
        "| :--- | :--- | ---: | ---: |",
        *[
            f"| {r['item']} | {r['date']} | {_p(r['voo_dd'])} | {_p(r['tech_dd'])} |"
            for r in ctx["covid"]
        ],
        "",
        "## 7. 目標之間的取捨：大部分時間 100% 科技池 vs MDD ≤ 25%",
        "",
        "(a) 三態版本本身的 MDD 見 §3；(b) 好／轉差狀態改為 X% 股票 + (1−X)% BOXX（最差仍 100% BOXX），"
        "找出能讓整體與三段崩跌 MDD 皆 ≤ 25% 的最大 X。",
        "",
    ]
    lines += [
        "### 對等對照：一直 X% 全科技池 + (1−X)% BOXX（不做總經判斷）",
        "",
        HEADER,
        *[_row_md(r) for r in ctx["static_x"]],
        "",
    ]
    for title, runs, best in (
        ("主版本", ctx["x_main"], ctx["x_main_best"]),
        ("變體（加升息）", ctx["x_fed"], ctx["x_fed_best"]),
    ):
        lines += [
            f"### {title}",
            "",
            HEADER,
            *[_row_md(r.row) for r in runs],
            "",
            f"- MDD ≤ 25% 的最大 X：**{f'{best.equity_scale:.0%}' if best else '格點內無'}**"
            + (
                f"（CAGR {_p(best.row.metrics.cagr)}、Sortino {_f(best.row.metrics.sortino)}）"
                if best
                else ""
            ),
            "",
        ]
    lines += [
        "## 8. 敏感度（一次只改一個參數）",
        "",
        "### 8.1 主版本",
        "",
        HEADER,
        _row_md(main.row),
        *[_row_md(v.row) for v in ctx["sens_main"]],
        "",
        "### 8.2 變體（加升息）",
        "",
        HEADER,
        _row_md(fed.row),
        *[_row_md(v.row) for v in ctx["sens_fed"]],
        "",
        "## 9. 限制",
        "",
        "- 科技池是 2026 年挑的 22 檔，Yahoo 無已下市公司資料：科技池的絕對報酬偏樂觀；"
        "B&H 100% 科技池與三態版本同受此偏差，兩者的比較較可信，但絕對數字應視為上限。",
        "- `BAA10Y` 是投資等級利差，對高收益債市場壓力的反應比 ICE 高收益債 OAS 溫和（後者在 FRED "
        "只有近 3 年資料）。",
        "- Sahm 即時版以當時公布的失業率計算；發布日期以次月 10 日近似（實際為次月第一個週五，偏保守）。",
        "- 價格為分割與股息調整後價格，屬總報酬近似；未模擬滑價與稅；BOXX 進出不計成本。",
    ]
    return "\n".join(lines) + "\n"


def _row_json(r: Row) -> dict[str, Any]:
    m = r.metrics
    return {
        "key": r.key,
        "label": r.label,
        "cagr": m.cagr,
        "sortino": m.sortino,
        "mdd": m.max_drawdown,
        "cvar_95": m.cvar_95,
        "worst_12m": r.worst_12m,
        "recovery_days": r.episode.recovery_days,
        "crashes": r.crashes,
        "sub": r.sub,
        "turnover": r.turnover,
    }


def _run_json(run: Run3) -> dict[str, Any]:
    return {
        **_row_json(run.row),
        "equity_scale": run.equity_scale,
        "switches": run.switches,
        "whipsaws": run.whipsaws,
        "time_share": state_time_share(run),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="總經三態配置回測（只產報告）")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    cache = Path(args.cache_dir)
    base = RegimeMomentumParams()
    symbols = sorted(
        set(base.universe)
        | set(base.defensive)
        | {MARKET_SYMBOL, CORE_SYMBOL, CORE_PROXY, CASH_SYMBOL, CASH_PROXY}
    )
    store = DataStore(cache)
    opens, closes = load_panel(store, symbols)
    opens, closes, cash_index = prepare_panel(
        opens,
        closes,
        base.fallback_cash_rate,
        list(base.universe) + list(base.defensive),
    )
    mar = realized_cash_rate(cash_index, START, END)
    inp = Inputs(
        opens=opens,
        closes=closes,
        cash_index=cash_index,
        spread=load_fred(SPREAD_SERIES, cache),
        sahm=load_fred(SAHM_SERIES, cache),
        dff=load_fred(FED_SERIES, cache),
        mar=mar,
    )
    spread_note = f"FRED `{SPREAD_SERIES}`（{inp.spread.index[0].date()} 起）"
    try:
        preferred = load_fred(SPREAD_SERIES_PREFERRED, cache)
        spread_note += (
            f"；原規格的 `{SPREAD_SERIES_PREFERRED}` 在 FRED 只有 "
            f"{preferred.index[0].date()} 起的資料，不足以涵蓋回測期間，故改用 `{SPREAD_SERIES}`"
        )
    except FileNotFoundError:
        pass

    print("執行對照組 ...", flush=True)
    refs = run_references(opens, closes, cash_index, mar)
    ref_by_key = {r.key: r for r in refs}
    tech_row = ref_by_key["REF_EW"]
    tech_row.label = "B&H 100% 科技池（使用者預設狀態）"
    other_refs = [ref_by_key["REF_VOO"], ref_by_key["REF_REGIME"]]
    s4060 = run_static_4060(inp)
    sleeves = [
        run_sleeve("SLV_ANY", MacroParams(combine="ANY"), inp),
        run_sleeve("SLV_ALL", MacroParams(combine="ALL"), inp),
    ]

    print("執行三態 ...", flush=True)
    main_run = run3("M3", BASE_MAIN, inp, BASE_MAIN.label3())
    fed_run = run3("M3_FED", BASE_FED, inp, BASE_FED.label3())
    sens_main: list[Run3] = []
    for name, p in _sens(BASE_MAIN, with_fed=False):
        print(f"  敏感度（主）：{name}", flush=True)
        sens_main.append(run3(f"SENS_M_{name}", p, inp, f"主版本：{name}"))
    sens_fed: list[Run3] = []
    for name, p in _sens(BASE_FED, with_fed=True):
        print(f"  敏感度（變體）：{name}", flush=True)
        sens_fed.append(run3(f"SENS_F_{name}", p, inp, f"變體：{name}"))
    x_main = x_tradeoff(BASE_MAIN, inp, "主版本")
    x_fed = x_tradeoff(BASE_FED, inp, "變體")
    static_x = static_x_rows(inp)

    tech_nav = equal_weight_pit(
        opens, closes, base.universe, START, END, base.cost_rate
    )
    voo = closes[CORE_SYMBOL]
    ctx: dict[str, Any] = {
        "mar": mar,
        "spread_note": spread_note,
        "main": main_run,
        "fed": fed_run,
        "tech_row": tech_row,
        "s4060": s4060,
        "other_refs": other_refs,
        "sleeves": sleeves,
        "checks_main": verdict(main_run, sens_main, s4060),
        "checks_fed": verdict(fed_run, sens_fed, s4060),
        "boxx_2022_main": year_2022_boxx(main_run),
        "boxx_2022_fed": year_2022_boxx(fed_run),
        "episodes_main": defensive_episodes(main_run, tech_nav, voo),
        "episodes_fed": defensive_episodes(fed_run, tech_nav, voo),
        "covid": covid_indicator_timing([main_run, fed_run], tech_nav, voo),
        "x_main": x_main,
        "x_fed": x_fed,
        "x_main_best": max_x_under_limit(x_main),
        "x_fed_best": max_x_under_limit(x_fed),
        "static_x": static_x,
        "sens_main": sens_main,
        "sens_fed": sens_fed,
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(build_report(ctx), encoding="utf-8")
    payload: dict[str, Any] = {
        "mar": mar,
        "spread_note": spread_note,
        "main": _run_json(main_run),
        "fed": _run_json(fed_run),
        "checks_main": [
            {"name": n, "pass": ok, "note": note} for n, ok, note in ctx["checks_main"]
        ],
        "checks_fed": [
            {"name": n, "pass": ok, "note": note} for n, ok, note in ctx["checks_fed"]
        ],
        "boxx_2022_main": ctx["boxx_2022_main"],
        "boxx_2022_fed": ctx["boxx_2022_fed"],
        "yearly_non_tech_main": yearly_non_tech_days(main_run),
        "yearly_non_tech_fed": yearly_non_tech_days(fed_run),
        "episodes_main": ctx["episodes_main"],
        "episodes_fed": ctx["episodes_fed"],
        "covid": ctx["covid"],
        "x_main": [_run_json(r) for r in x_main],
        "x_fed": [_run_json(r) for r in x_fed],
        "static_x": [_row_json(r) for r in static_x],
        "sens_main": [_run_json(r) for r in sens_main],
        "sens_fed": [_run_json(r) for r in sens_fed],
        "tech_bh": _row_json(tech_row),
        "static_40_60": _row_json(s4060),
        "other_refs": [_row_json(r) for r in other_refs],
        "sleeves": [_row_json(r) for r in sleeves],
    }
    (out / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(f"報告已輸出：{out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
