"""固定比例配置 + 定期再平衡（選項 A）回測：全格點、目標回撤查表、子期間穩定性。

只產出報告，不改任何參數。資料與 #19（大盤三態 + 動能輪動）共用
`calibration/daily_panel.py`，指標一律呼叫 `market_analysis/downside_risk.py`。

用法（在 nexus_core 下；日線快取由 run_regime_momentum_backtest.py --fetch 建立）：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_static_allocation_backtest.py

報告輸出到 gitignored 的 `reports/static_allocation/`（report.md / results.json）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.daily_panel import load_panel, prepare_panel  # noqa: E402
from calibration.data_store import DataStore  # noqa: E402
from calibration.regime_momentum_backtest import (  # noqa: E402
    CASH_PROXY,
    CASH_SYMBOL,
    CORE_PROXY,
    CORE_SYMBOL,
    MARKET_SYMBOL,
    Metrics,
    RegimeMomentumParams,
    buy_and_hold,
    compute_metrics,
    equal_weight_pit,
    realized_cash_rate,
    simulate,
    slice_nav,
    window_drawdown,
)
from calibration.static_allocation_backtest import (  # noqa: E402
    EQUITY_SHARES,
    REBALANCE_MODES,
    TECH_SHARES,
    DrawdownEpisode,
    StaticAllocationParams,
    annual_turnover_static,
    max_drawdown_episode,
    simulate_static,
    worst_rolling_return,
)

DEFAULT_CACHE = Path("/app/.calibration_cache")
DEFAULT_OUT = Path("reports/static_allocation")
START = "2007-01-03"
END = "2025-12-31"

CRASH_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("2008", "2007-10-01", "2009-03-31"),
    ("2020", "2020-02-01", "2020-03-31"),
    ("2022", "2022-01-01", "2022-12-31"),
)
SUBPERIODS: tuple[tuple[str, str, str], ...] = (
    ("2007–2015", "2007-01-03", "2015-12-31"),
    ("2016–2025", "2016-01-01", "2025-12-31"),
)
TARGET_MDDS: tuple[float, ...] = (0.20, 0.25, 0.30, 0.35, 0.40)


@dataclass
class Row:
    key: str
    label: str
    kind: str  # "static" | "reference"
    params: Optional[StaticAllocationParams]
    metrics: Metrics
    episode: DrawdownEpisode
    worst_12m: float
    crashes: dict[str, float]
    turnover: float
    rebalances: int
    sub: dict[str, dict[str, float]]


def _evaluate(
    key: str,
    label: str,
    kind: str,
    nav: pd.Series,
    mar: float,
    params: Optional[StaticAllocationParams] = None,
    turnover: float = float("nan"),
    rebalances: int = 0,
) -> Row:
    sub: dict[str, dict[str, float]] = {}
    for name, s, e in SUBPERIODS:
        piece = slice_nav(nav, s, e)
        pm = compute_metrics(piece, mar)
        sub[name] = {
            "cagr": pm.cagr,
            "mdd": pm.max_drawdown,
            "sortino": pm.sortino,
        }
    return Row(
        key=key,
        label=label,
        kind=kind,
        params=params,
        metrics=compute_metrics(nav, mar),
        episode=max_drawdown_episode(nav),
        worst_12m=worst_rolling_return(nav),
        crashes={n: window_drawdown(nav, s, e) for n, s, e in CRASH_WINDOWS},
        turnover=turnover,
        rebalances=rebalances,
        sub=sub,
    )


def run_grid(
    opens: pd.DataFrame, closes: pd.DataFrame, cash_index: pd.Series, mar: float
) -> list[Row]:
    rows: list[Row] = []
    for e, t, mode in itertools.product(EQUITY_SHARES, TECH_SHARES, REBALANCE_MODES):
        p = StaticAllocationParams(equity_share=e, tech_share=t, rebalance=mode)
        res = simulate_static(opens, closes, cash_index, START, END, p)
        rows.append(
            _evaluate(
                f"E{e:.2f}_T{t:.2f}_{mode}",
                p.label(),
                "static",
                res.nav,
                mar,
                p,
                annual_turnover_static(res),
                len(res.rebalance_days),
            )
        )
        print(f"  {p.label()}", flush=True)
    return rows


def run_references(
    opens: pd.DataFrame, closes: pd.DataFrame, cash_index: pd.Series, mar: float
) -> list[Row]:
    base = RegimeMomentumParams()
    strat = simulate(
        opens, closes, cash_index, closes[MARKET_SYMBOL], START, END, base
    ).nav
    voo = buy_and_hold(closes[CORE_SYMBOL], START, END).reindex(strat.index)
    ew = equal_weight_pit(
        opens, closes, base.universe, START, END, base.cost_rate
    ).reindex(strat.index)
    return [
        _evaluate("REF_VOO", "B&H VOO（100% 不再平衡）", "reference", voo, mar),
        _evaluate("REF_EW", "B&H 等權科技池（年度再平衡）", "reference", ew, mar),
        _evaluate(
            "REF_REGIME", "#19 候選：大盤三態 + 動能輪動", "reference", strat, mar
        ),
    ]


# 查表的科技占比限定：None = 不限；0.0 = 只用 VOO（不受選股池存活者偏差影響，最可信）
LOOKUP_FILTERS: tuple[tuple[str, Optional[float]], ...] = (
    ("不限科技占比", None),
    ("科技占比 50%", 0.50),
    ("只用 VOO（科技 0%，不受存活者偏差影響）", 0.00),
)


def lookup(
    rows: list[Row], target: float, metric_of: Any, mdd_of: Any
) -> Optional[Row]:
    ok = [r for r in rows if r.kind == "static" and mdd_of(r) <= target + 1e-12]
    return max(ok, key=metric_of) if ok else None


def _filtered(rows: list[Row], tech: Optional[float]) -> list[Row]:
    if tech is None:
        return rows
    return [
        r
        for r in rows
        if r.params is not None and abs(r.params.tech_share - tech) < 1e-9
    ]


def build_lookup(rows: list[Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for title, tech in LOOKUP_FILTERS:
        for entry in _build_lookup_one(_filtered(rows, tech)):
            entry["filter"] = title
            out.append(entry)
    return out


def _build_lookup_one(rows: list[Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for target in TARGET_MDDS:
        best = lookup(
            rows, target, lambda r: r.metrics.cagr, lambda r: r.metrics.max_drawdown
        )
        entry: dict[str, Any] = {"target": target, "full": best.key if best else None}
        for name, _, _ in SUBPERIODS:
            sb = lookup(
                rows,
                target,
                lambda r, n=name: r.sub[n]["cagr"],
                lambda r, n=name: r.sub[n]["mdd"],
            )
            entry[name] = sb.key if sb else None
        if best is not None:
            entry["full_pick_sub_mdd"] = {
                name: best.sub[name]["mdd"] for name, _, _ in SUBPERIODS
            }
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def _p(x: float) -> str:
    return "N/A" if x != x else f"{x * 100:.1f}%"


def _f(x: float) -> str:
    return "N/A" if x != x else f"{x:.2f}"


def _recovery(ep: DrawdownEpisode) -> str:
    if ep.recovered is None:
        return f"未收復（{ep.recovery_days} 日）"
    return f"{ep.recovery_days} 日（{ep.recovered.date()}）"


def _row_md(r: Row) -> str:
    m = r.metrics
    return (
        f"| {r.label} | {_p(m.cagr)} | **{_f(m.sortino)}** | {_p(m.max_drawdown)} "
        f"| {_p(m.cvar_95)} | {_p(r.worst_12m)} | {_recovery(r.episode)} "
        f"| {_p(r.crashes['2008'])} | {_p(r.crashes['2020'])} | {_p(r.crashes['2022'])} "
        f"| {_p(r.turnover)} |"
    )


HEADER = (
    "| 配置 | CAGR | Sortino | MDD | CVaR95 | 最差 12 個月 | MDD 恢復時間 "
    "| 2008 | 2020 | 2022 | 年換手 |\n"
    "| :--- | ---: | ---: | ---: | ---: | ---: | :--- | ---: | ---: | ---: | ---: |"
)


def build_report(
    rows: list[Row], refs: list[Row], table: list[dict[str, Any]], mar: float
) -> str:
    by_key = {r.key: r for r in rows}
    lines: list[str] = [
        "# 固定比例配置 + 定期再平衡（選項 A）回測報告",
        "",
        f"- 期間：{START} → {END}（日線）；Sortino 的 MAR = 期間內 BOXX／BIL 實際年化 "
        f"{mar * 100:.2f}%",
        "- 資產桶：科技池（22 檔 point-in-time 等權）、VOO（2010 前以 SPY 代理）、"
        "BOXX（2022-12 前以 BIL、2007-05 前以固定利率代理）",
        "- 成本：股票單邊 0.15%，BOXX 不計；再平衡以前一日收盤判定、當日開盤成交",
        "- 回撤恢復時間：最大回撤從高點起算，到淨值收復該高點的交易日數",
        "",
        "## 1. 依目標最大回撤查表（全期間 CAGR 最高者）",
    ]
    current_filter: Optional[str] = None
    for entry in table:
        if entry["filter"] != current_filter:
            current_filter = entry["filter"]
            lines += [
                "",
                f"### {current_filter}",
                "",
                "| 目標 MDD ≤ | 配置 | CAGR | Sortino | 實際 MDD | 2008 | 2020 | 2022 "
                "| 恢復時間 | 2007–2015 最佳 | 2016–2025 最佳 | 全期間選擇在兩子期間的 MDD |",
                "| :---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- "
                "| :--- | :--- |",
            ]
        target = entry["target"]
        if entry["full"] is None:
            lines.append(f"| {_p(target)} | 無配置符合 | | | | | | | | | | |")
            continue
        r = by_key[entry["full"]]
        s1 = by_key[entry["2007–2015"]].label if entry["2007–2015"] else "無"
        s2 = by_key[entry["2016–2025"]].label if entry["2016–2025"] else "無"
        sub_mdd = "／".join(_p(v) for v in entry["full_pick_sub_mdd"].values())
        lines.append(
            f"| {_p(target)} | {r.label} | {_p(r.metrics.cagr)} | {_f(r.metrics.sortino)} "
            f"| {_p(r.metrics.max_drawdown)} | {_p(r.crashes['2008'])} "
            f"| {_p(r.crashes['2020'])} | {_p(r.crashes['2022'])} "
            f"| {_recovery(r.episode)} | {s1} | {s2} | {sub_mdd} |"
        )

    lines += ["", "## 2. 對照組與 #19 候選策略", "", HEADER]
    lines += [_row_md(r) for r in refs]

    for mode, title in (
        ("ANNUAL", "每年再平衡"),
        ("QUARTERLY", "每季再平衡"),
        ("THRESHOLD", "偏離 ≥5pp 才再平衡（每月檢查）"),
    ):
        lines += ["", f"## 3. 全格點：{title}", "", HEADER]
        for r in rows:
            if r.params is not None and r.params.rebalance == mode:
                lines.append(_row_md(r))

    lines += [
        "",
        "## 4. 再平衡方式比較（同一 E／T，三種方式的 CAGR／MDD／年換手）",
        "",
        "| 股票／科技 | 年 | 季 | 偏離 |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for e, t in itertools.product(EQUITY_SHARES, TECH_SHARES):
        cells = []
        for mode in REBALANCE_MODES:
            r = by_key[f"E{e:.2f}_T{t:.2f}_{mode}"]
            cells.append(
                f"{_p(r.metrics.cagr)}／{_p(r.metrics.max_drawdown)}／{_p(r.turnover)}"
                f"（{r.rebalances} 次）"
            )
        lines.append(f"| {e * 100:.0f}%／{t * 100:.0f}% | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 5. 子期間穩定性（每年再平衡；CAGR／MDD）",
        "",
        "| 股票／科技 | 2007–2015 | 2016–2025 |",
        "| :--- | :--- | :--- |",
    ]
    for e, t in itertools.product(EQUITY_SHARES, TECH_SHARES):
        r = by_key[f"E{e:.2f}_T{t:.2f}_ANNUAL"]
        cells = [
            f"{_p(r.sub[n]['cagr'])}／{_p(r.sub[n]['mdd'])}" for n, _, _ in SUBPERIODS
        ]
        lines.append(f"| {e * 100:.0f}%／{t * 100:.0f}% | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 6. 限制",
        "",
        "- **存活者偏差與事後挑選**：科技池是 2026 年挑的 22 檔，且 Yahoo 沒有已下市公司"
        "資料；科技池占比越高，絕對報酬越偏樂觀。VOO 與 BOXX 不受此影響，"
        "因此科技占比 0% 的列最可信。",
        "- **回撤主要由股票比例決定**：固定比例配置不擇時，回撤大致與股票比例成正比；"
        "但科技池的回撤比 VOO 深，同一 E 下科技占比越高，回撤越大。",
        "- **偏離門檻模式只看資產桶層級**：只有單一資產桶時（例如股 100%／科技 100%）"
        "永遠不會觸發，科技池的成員名單也只在再平衡時更新，因此新上市標的可能長期不被納入；"
        "該模式的科技池結果應以年度再平衡為準。",
        "- 目標 MDD ≤ 20% 需要股票比例低於 40%，不在本次格點內（40% 的配置 MDD 約 21–25%）。",
        "- 價格為分割與股息調整後價格（總報酬近似）；以開盤價成交，未模擬滑價與稅。",
        "- VOO 上市前以 SPY 代理；BOXX 上市前以 BIL、BIL 上市前以固定利率代理。",
    ]
    return "\n".join(lines) + "\n"


def to_json(rows: list[Row], refs: list[Row], table: list[dict[str, Any]]) -> Any:
    def one(r: Row) -> dict[str, Any]:
        m = r.metrics
        return {
            "key": r.key,
            "label": r.label,
            "kind": r.kind,
            "cagr": m.cagr,
            "sortino": m.sortino,
            "mdd": m.max_drawdown,
            "cvar_95": m.cvar_95,
            "worst_12m": r.worst_12m,
            "mdd_peak": str(r.episode.peak.date()),
            "mdd_trough": str(r.episode.trough.date()),
            "mdd_recovered": str(r.episode.recovered.date())
            if r.episode.recovered is not None
            else None,
            "recovery_days": r.episode.recovery_days,
            "crashes": r.crashes,
            "turnover": r.turnover,
            "rebalances": r.rebalances,
            "sub": r.sub,
        }

    return {
        "grid": [one(r) for r in rows],
        "references": [one(r) for r in refs],
        "lookup": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="固定比例配置 + 定期再平衡回測（只產報告）"
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    base = RegimeMomentumParams()
    symbols = sorted(
        set(base.universe)
        | set(base.defensive)
        | {MARKET_SYMBOL, CORE_SYMBOL, CORE_PROXY, CASH_SYMBOL, CASH_PROXY}
    )
    store = DataStore(Path(args.cache_dir))
    opens, closes = load_panel(store, symbols)
    opens, closes, cash_index = prepare_panel(
        opens,
        closes,
        base.fallback_cash_rate,
        list(base.universe) + list(base.defensive),
    )
    mar = realized_cash_rate(cash_index, START, END)

    print("執行對照組 ...", flush=True)
    refs = run_references(opens, closes, cash_index, mar)
    print("執行固定比例格點 ...", flush=True)
    rows = run_grid(opens, closes, cash_index, mar)
    table = build_lookup(rows)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(
        build_report(rows, refs, table, mar), encoding="utf-8"
    )
    (out / "results.json").write_text(
        json.dumps(
            to_json(rows, refs, table), ensure_ascii=False, indent=1, default=str
        ),
        encoding="utf-8",
    )
    print(f"報告已輸出：{out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
