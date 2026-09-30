"""多資產動態轉倉回測（VOO 核心 + 8 檔個股衛星 + GLD，2022–2025）。

只產出報告，不修改任何 production 參數。判讀以 Sortino 為主，超額報酬 vs 下行差對齊
減碼 B&H、MDD、CVaR95 為輔；Sharpe／Calmar 只作描述。

資料：日線來自 Yahoo（`python -m calibration fetch`），1h 線整段來自 Alpaca SIP
（`python -m calibration fetch-alpaca-1h`），皆放在 `.calibration_cache/multi_asset/`。

用法（在 nexus_core 下）：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_multi_asset_backtest.py --suite all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.backtest_analysis import (  # noqa: E402
    retreat_summary,
    symbol_contribution,
    yearly_metrics,
)
from calibration.backtest_engine_2025 import (  # noqa: E402
    BOXX_SYMBOL,
    BacktestMetrics,
    RolloverBacktestEngine2025,
    multi_asset_universe,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path("/app/.calibration_cache/multi_asset")
DEFAULT_START = "2022-01-03"
DEFAULT_END = "2025-12-30"


@dataclass(frozen=True)
class RunSpec:
    name: str
    mode: str
    hybrid: bool = False  # aggressive，但換股門檻／冷卻改回 defensive
    flags: dict[str, bool] = field(default_factory=dict)


CORE_SUITE: tuple[RunSpec, ...] = (
    RunSpec("DEFENSIVE", "defensive"),
    RunSpec("AGGRESSIVE", "aggressive"),
    RunSpec("HYBRID", "aggressive", hybrid=True),
    RunSpec("DEFENSIVE+PYR", "defensive", flags={"enable_pyramid_add": True}),
    RunSpec("AGGRESSIVE+PYR", "aggressive", flags={"enable_pyramid_add": True}),
    RunSpec(
        "HYBRID+PYR", "aggressive", hybrid=True, flags={"enable_pyramid_add": True}
    ),
)
ESCAPE_SUITE: tuple[RunSpec, ...] = (
    RunSpec("DEFENSIVE+ESC", "defensive", flags={"enable_escape_tiers": True}),
    RunSpec("AGGRESSIVE+ESC", "aggressive", flags={"enable_escape_tiers": True}),
)
RETREAT_SUITE: tuple[RunSpec, ...] = (
    RunSpec("DEFENSIVE+BOXX", "defensive", flags={"enable_boxx_retreat": True}),
    RunSpec("AGGRESSIVE+BOXX", "aggressive", flags={"enable_boxx_retreat": True}),
    RunSpec(
        "AGGRESSIVE+PYR+BOXX",
        "aggressive",
        flags={"enable_pyramid_add": True, "enable_boxx_retreat": True},
    ),
)


def run_one(spec: RunSpec, cache_dir: Path, start: str, end: str) -> dict[str, Any]:
    eng = RolloverBacktestEngine2025(
        cache_dir=cache_dir,
        start_date=start,
        end_date=end,
        mode=spec.mode,
        universe=multi_asset_universe(),
        **spec.flags,
    )
    if spec.hybrid:
        ref = RolloverBacktestEngine2025(cache_dir=cache_dir, mode="defensive")
        eng.opp_cost_hurdle = ref.opp_cost_hurdle
        eng.opp_cost_cd_days = ref.opp_cost_cd_days
    eng.run_simulation()
    m = eng.calculate_metrics()
    years = yearly_metrics(eng.portfolio.daily_history)
    contrib = symbol_contribution(eng.portfolio.trades, eng.portfolio.positions)
    scen: dict[str, int] = {}
    for t in eng.portfolio.trades:
        scen[t.scenario] = scen.get(t.scenario, 0) + 1
    retreat: Optional[dict[str, Any]] = None
    if eng.enable_boxx_retreat:
        last = eng.trading_dates[-1]
        retreat = retreat_summary(
            eng.retreat_episodes,
            eng._close_price_on(eng.core_symbol, last),
            eng._close_price_on(BOXX_SYMBOL, last),
            len(eng.trading_dates),
        )
        retreat["boxx_listing_date"] = str(eng.boxx_listing_date)
    return {
        "spec": spec,
        "metrics": m,
        "years": years,
        "contrib": contrib,
        "scenarios": scen,
        "retreat": retreat,
        "escape_tiers": dict(eng.escape_tier_history),
        "exit_tiers": eng.summarize_exit_events(),
    }


def _pct(x: Optional[float], digits: int = 2) -> str:
    return "N/A" if x is None else f"{x * 100:.{digits}f}%"


def _pp(x: float) -> str:
    return f"{x * 100:+.2f} pp"


def summary_table(results: list[dict[str, Any]]) -> str:
    b: BacktestMetrics = results[0]["metrics"]
    lines = [
        "| 組合 | **Sortino** | 超額 vs 減碼 B&H | MDD | 1 日 CVaR95 | 總報酬 | 交易筆數 | 換股 | 加碼 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| **B&H（單純持有）** | **{b.benchmark_sortino:.2f}** | — | {_pct(b.benchmark_max_drawdown)} | {_pct(b.benchmark_cvar_95)} | {_pct(b.benchmark_total_return)} | 0 | — | — |",
    ]
    for r in results:
        m: BacktestMetrics = r["metrics"]
        sc = r["scenarios"]
        lines.append(
            f"| {r['spec'].name} | **{m.sortino_ratio:.2f}** | {_pp(m.excess_return_vs_scaled)} | "
            f"{_pct(m.max_drawdown)} | {_pct(m.cvar_95)} | {_pct(m.total_return)} | "
            f"{m.total_trades} | {sc.get('OPPORTUNITY_COST', 0)} | {sc.get('PYRAMID_ADD', 0)} |"
        )
    return "\n".join(lines)


def yearly_table(results: list[dict[str, Any]]) -> str:
    years = [y[0].label for y in results[0]["years"]]
    head = "| 組合 | " + " | ".join(f"{y} Sortino／報酬／MDD" for y in years) + " |"
    sep = "| :--- | " + " | ".join(":---:" for _ in years) + " |"
    rows = [head, sep]
    bench_cells = [
        f"{bh.sortino:.2f}／{_pct(bh.total_return, 1)}／{_pct(bh.max_drawdown, 1)}"
        for _s, bh in results[0]["years"]
    ]
    rows.append("| **B&H** | " + " | ".join(bench_cells) + " |")
    for r in results:
        cells = [
            f"{s.sortino:.2f}／{_pct(s.total_return, 1)}／{_pct(s.max_drawdown, 1)}"
            for s, _bh in r["years"]
        ]
        rows.append(f"| {r['spec'].name} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def contribution_table(result: dict[str, Any], initial: float) -> str:
    rows = [
        "| 標的 | 已實現 | 期末未實現 | 合計 | 佔期初本金 | 賣出次數 | 其中停損 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sym, c in sorted(result["contrib"].items(), key=lambda kv: -kv[1]["total"]):
        rows.append(
            f"| {sym} | ${c['realized']:,.0f} | ${c['unrealized']:,.0f} | ${c['total']:,.0f} | "
            f"{c['total'] / initial * 100:+.1f}% | {int(c['sells'])} | {int(c['stop_exits'])} |"
        )
    return "\n".join(rows)


def retreat_section(result: dict[str, Any]) -> str:
    rt = result["retreat"]
    if rt is None:
        return ""
    lines = [
        f"#### {result['spec'].name}",
        "",
        f"- 退場 {rt['exits']} 次、回場 {rt['entries']} 次，退場期間合計 {rt['days_in_retreat']} 個交易日",
        f"- 錯過反彈（退場期間核心反而上漲）：{len(rt['missed_rebounds'])} 次",
        "",
        "| 退場日 | 回場日 | 天數 | 核心價變化（>0 = 錯過的反彈） | BOXX 報酬 |",
        "| :--- | :--- | ---: | ---: | ---: |",
    ]
    for ep in rt["episodes"]:
        lines.append(
            f"| {ep['exit_date']} | {ep['enter_date'] or '（期末仍在退場）'} | {ep['days']} | "
            f"{ep['core_change'] * 100:+.2f}% | {ep['boxx_return'] * 100:+.2f}% |"
        )
    return "\n".join(lines)


def build_report(
    suites: dict[str, list[dict[str, Any]]], start: str, end: str, initial: float
) -> str:
    parts = [
        f"# 多資產動態轉倉回測（{start} → {end}）",
        "",
        "配置（策略與 B&H 共用）：VOO 核心 40%；NVDA、META、GOOGL、TSLA、MU、PLTR、FCX、MRNA 各 6%；"
        "GLD 7%；現金 5%。大盤訊號代理（負 Gamma、危機判定、逃頂、保護性 Put）維持 SPY。",
        "判讀以 **Sortino（MAR = 4.5%）為主**，超額報酬 vs 下行差對齊減碼 B&H、MDD、CVaR95 為輔。",
        "",
    ]
    for title, results in suites.items():
        if not results:
            continue
        parts += [
            f"## {title}",
            "",
            summary_table(results),
            "",
            "### 逐年分列（Sortino／總報酬／該年 MDD）",
            "",
            yearly_table(results),
            "",
        ]
        if title.startswith("BOXX"):
            parts += ["### 退場統計", ""]
            for r in results:
                parts += [retreat_section(r), ""]
    core = suites.get("核心 6 組", [])
    for r in core:
        if r["spec"].name in ("DEFENSIVE", "AGGRESSIVE+PYR"):
            parts += [
                f"## 逐檔貢獻：{r['spec'].name}",
                "",
                contribution_table(r, initial),
                "",
            ]
    return "\n".join(parts)


def to_json(suites: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for title, results in suites.items():
        rows = []
        for r in results:
            m: BacktestMetrics = r["metrics"]
            rows.append(
                {
                    "name": r["spec"].name,
                    "sortino": m.sortino_ratio,
                    "benchmark_sortino": m.benchmark_sortino,
                    "excess_vs_scaled": m.excess_return_vs_scaled,
                    "max_drawdown": m.max_drawdown,
                    "benchmark_max_drawdown": m.benchmark_max_drawdown,
                    "cvar_95": m.cvar_95,
                    "benchmark_cvar_95": m.benchmark_cvar_95,
                    "total_return": m.total_return,
                    "benchmark_total_return": m.benchmark_total_return,
                    "sharpe_descriptive": m.sharpe_ratio,
                    "trades": m.total_trades,
                    "scenarios": r["scenarios"],
                    "years": [
                        {
                            "year": s.label,
                            "sortino": s.sortino,
                            "return": s.total_return,
                            "mdd": s.max_drawdown,
                            "cvar_95": s.cvar_95,
                            "bench_sortino": b.sortino,
                            "bench_return": b.total_return,
                            "bench_mdd": b.max_drawdown,
                            "bench_cvar_95": b.cvar_95,
                        }
                        for s, b in r["years"]
                    ],
                    "contribution": r["contrib"],
                    "retreat": r["retreat"],
                    "escape_tiers": r["escape_tiers"],
                    "exit_tiers": r["exit_tiers"],
                }
            )
        out[title] = rows
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="多資產動態轉倉回測（只產報告）")
    parser.add_argument(
        "--suite",
        choices=["core", "escape", "retreat", "all"],
        default="all",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--initial-capital", type=float, default=100_000.0)
    parser.add_argument("--out", default="reports/multi_asset_2022_2025")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cache = Path(args.cache_dir)
    plan: dict[str, tuple[RunSpec, ...]] = {
        "核心 6 組": CORE_SUITE if args.suite in ("core", "all") else (),
        "逃頂三級階梯 A/B（對照：核心 6 組中同模式、無開關者）": ESCAPE_SUITE
        if args.suite in ("escape", "all")
        else (),
        "BOXX 大盤退場 A/B（對照：核心 6 組中同模式、無開關者）": RETREAT_SUITE
        if args.suite in ("retreat", "all")
        else (),
    }
    suites: dict[str, list[dict[str, Any]]] = {}
    for title, specs in plan.items():
        suites[title] = []
        for spec in specs:
            print(f"執行 {spec.name} ...", flush=True)
            suites[title].append(run_one(spec, cache, args.start, args.end))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(
        build_report(suites, args.start, args.end, args.initial_capital),
        encoding="utf-8",
    )
    (out / "results.json").write_text(
        json.dumps(to_json(suites), ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"報告已輸出：{out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
