"""盤後資料管線：結算失敗標示、風險指標貫通、Vega 不加權、LLM raw_data 契約。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.analyst_agent import AnalystAgent
from market_analysis import portfolio as pf
from market_analysis.portfolio import PortfolioStatusOrchestrator
from models.quant import MacroRiskMetrics
from services.trading_service.reports import ReportsMixin

TOP_KEYS = {
    "report_status",
    "macro_snapshot",
    "pnl_attribution_since_entry",
    "aggregate_risk_metrics",
    "positions",
    "sectors",
    "poly_events",
    "spy_max_pain",
}
PNL_KEYS = {
    "scope",
    "total_net_pnl",
    "alpha_selection_pnl",
    "hedge_pnl",
    "unpriced_count",
    "uncosted_count",
}
RISK_KEYS = {
    "total_theta",
    "total_beta_delta",
    "net_delta_exposure_pct",
    "margin_heat_pct",
    "total_margin_used",
    "withdrawal_runway",
}

ROW = (1, 0, "AAPL", "call", 150.0, "2026-07-20", 5.0, 1, 0.0)


def _metrics() -> MacroRiskMetrics:
    return MacroRiskMetrics(
        net_exposure_dollars=1000.0,
        exposure_pct=2.5,
        total_beta_delta=3.0,
        gamma_threshold=1.0,
        theta_yield=0.1,
        portfolio_heat=12.0,
        portfolio_heat_limit=30.0,
        total_gamma=0.1,
        total_theta=4.0,
        total_margin_used=5000.0,
    )


def _ctx(load_failed: bool = False) -> Any:
    return SimpleNamespace(capital=100000.0, load_failed=load_failed)


# ── reports.py ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_failure_marks_report_failed() -> None:
    with (
        patch("database.get_all_portfolio", return_value=[ROW]),
        patch("database.get_full_user_context", side_effect=RuntimeError("db")),
    ):
        res = await ReportsMixin().get_after_market_report_data()
    assert res == {1: {"report_failed": True}}


@pytest.mark.asyncio
async def test_reports_status_failure_marks_report_failed() -> None:
    with (
        patch("database.get_all_portfolio", return_value=[ROW]),
        patch("database.get_full_user_context", return_value=_ctx()),
        patch(
            "services.trading_service.reports.get_adjusted_user_capital",
            new_callable=AsyncMock,
            return_value=1000.0,
        ),
        patch(
            "services.trading_service.reports.portfolio.run_portfolio_status",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        res = await ReportsMixin().get_after_market_report_data()
    assert res == {1: {"report_failed": True}}


@pytest.mark.asyncio
async def test_reports_load_failed_context_never_runs_settlement() -> None:
    run = AsyncMock()
    with (
        patch("database.get_all_portfolio", return_value=[ROW]),
        patch("database.get_full_user_context", return_value=_ctx(load_failed=True)),
        patch("services.trading_service.reports.portfolio.run_portfolio_status", run),
    ):
        res = await ReportsMixin().get_after_market_report_data()
    assert res == {1: {"report_failed": True}}
    run.assert_not_called()


@pytest.mark.asyncio
async def test_reports_success_entry_carries_metrics_and_hedge_flags() -> None:
    positions = [{"symbol": "AAPL"}]
    status = pf.PortfolioStatusResult(
        report_lines=["x"], metrics=_metrics(), positions=positions
    )
    hedge = AsyncMock(return_value={"net_pnl": 1.0})
    with (
        patch("database.get_all_portfolio", return_value=[ROW]),
        patch("database.get_full_user_context", return_value=_ctx()),
        patch(
            "services.trading_service.reports.get_adjusted_user_capital",
            new_callable=AsyncMock,
            return_value=1000.0,
        ),
        patch(
            "services.trading_service.reports.portfolio.run_portfolio_status",
            new_callable=AsyncMock,
            return_value=status,
        ),
        patch(
            "services.withdrawal_runway_service.get_runway_display",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
        patch(
            "services.trading_service.reports.hedging.analyze_hedge_performance", hedge
        ),
        patch(
            "services.trading_service.reports.hedging.calculate_daily_effectiveness",
            new_callable=AsyncMock,
        ),
        patch(
            "services.trading_service.reports.hedging.calculate_dynamic_tau",
            new_callable=AsyncMock,
            return_value=0.5,
        ),
    ):
        res = await ReportsMixin().get_after_market_report_data()
    entry = res[1]
    assert entry["report_failed"] is False
    assert entry["risk_metrics"] == _metrics().model_dump()
    assert entry["positions"] == positions
    hedge.assert_awaited_once_with(1, include_virtual=False, include_holdings=True)


# ── portfolio.py ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_portfolio_status_metrics_none_without_spy() -> None:
    async def fake_prepare(self: Any, rows: Any) -> None:
        self.spy_price = None

    async def fake_process(self: Any, symbol: Any, rows: Any) -> None:
        return None

    with (
        patch.object(PortfolioStatusOrchestrator, "_prepare_market_data", fake_prepare),
        patch.object(
            PortfolioStatusOrchestrator, "_process_symbol_positions", fake_process
        ),
        patch(
            "market_analysis.portfolio.analyze_sector_correlation_core",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        res = await pf.run_portfolio_status([("AAPL", "call", 1, "x", 1, 1, 0)], 1000.0)
    assert res.metrics is None
    assert res.report_lines


def test_position_summary_fields() -> None:
    f = PortfolioStatusOrchestrator._position_summary
    opt = f("AAPL", "put", -2, 30, 0.3456, 55.54, 0.123456)
    assert opt == {
        "symbol": "AAPL",
        "type": "PUT",
        "direction": "STO",
        "quantity": -2,
        "dte": 30,
        "iv_pct": 34.6,
        "iv_rank_pct": 55.5,
        "pnl_pct": 12.35,
    }
    stock = f("MSFT", "stock", 100, None, None, None, None)
    assert stock["type"] == "STOCK" and stock["direction"] == "BTO"
    assert stock["iv_pct"] is None and stock["dte"] is None


async def _run_vega_case(spy_price: float | None) -> PortfolioStatusOrchestrator:
    orch = PortfolioStatusOrchestrator(user_capital=50000.0)
    orch.spy_price = spy_price
    chain = MagicMock()
    chain.calls = pd.DataFrame(
        [{"strike": 150.0, "impliedVolatility": 0.3, "bid": 4.9, "ask": 5.1}]
    )
    chain.puts = pd.DataFrame()
    greeks = {
        "delta": 0.5,
        "gamma": 0.01,
        "vega": 0.2,
        "vanna": 0.1,
        "theta": -0.05,
    }
    info = {"price": 150.0, "dividend_yield": 0.0, "beta": 2.0}
    with (
        patch.object(orch, "_get_stock_info", AsyncMock(return_value=info)),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("x"),
        ),
        patch(
            "market_analysis.portfolio.get_option_chain",
            new_callable=AsyncMock,
            return_value=chain,
        ),
        patch("market_analysis.portfolio.calculate_greeks", return_value=greeks),
    ):
        await orch._process_symbol_positions("AAPL", [ROW[2:]])
    return orch


@pytest.mark.asyncio
async def test_vega_is_unweighted_by_beta() -> None:
    orch = await _run_vega_case(500.0)
    weight = 2.0 * (150.0 / 500.0)
    assert orch.total_vega == pytest.approx(0.2 * 1 * 100)
    assert orch.total_vanna == pytest.approx(0.1 * 100 * weight)
    assert len(orch.positions) == 1 and orch.positions[0]["type"] == "CALL"


@pytest.mark.asyncio
async def test_vega_accumulates_without_spy_price() -> None:
    """Vega 在 SPY 缺失時仍累加；Vanna（Beta 加權）則為 0。"""
    orch = await _run_vega_case(None)
    assert orch.total_vega == pytest.approx(0.2 * 100)
    assert orch.total_vanna == 0


# ── analyst_agent dispatch ────────────────────────────────────────────────


async def _dispatch(user_reports: Any, gather_exc: bool = False) -> tuple[Any, Any]:
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)
    agent.gather_sector_rotation_data = AsyncMock(  # type: ignore
        return_value={
            "vix": 15.0,
            "vix_tier_name": "Low",
            "spy_price": 0,
            "sectors": [],
            "poly_events": [],
            "spy_max_pain": {},
        }
    )
    ctx = MagicMock()
    ctx.enable_analyst_agent = True
    get_data = AsyncMock(
        side_effect=RuntimeError("x") if gather_exc else None,
        return_value=user_reports,
    )
    with (
        patch("database.purge_old_cache", return_value=0),
        patch(
            "services.trading_service.TradingService.get_after_market_report_data",
            get_data,
        ),
        patch("database.get_all_user_ids", return_value=[7]),
        patch("database.is_notification_enabled", return_value=True),
        patch("database.get_full_user_context", return_value=ctx),
        patch(
            "cogs.analyst_agent.generate_analyst_report", new_callable=AsyncMock
        ) as gen,
        patch("cogs.analyst_agent.is_memory_safe", return_value=True),
        patch(
            "services.withdrawal_runway_service.get_runway_display",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
        patch(
            "services.downside_risk_service.get_downside_snapshots",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch("cogs.embed_builder.build_post_market_intelligence_embed") as build,
        patch("cogs.analyst_agent.notify_many", new_callable=AsyncMock),
    ):
        gen.return_value = "ok"
        build.return_value = []
        await agent.dispatch_post_market_intelligence()
    return gen.call_args.args[1], build.call_args.kwargs


def _assert_contract(raw: dict[str, Any]) -> None:
    assert set(raw) == TOP_KEYS
    assert set(raw["pnl_attribution_since_entry"]) == PNL_KEYS
    assert set(raw["aggregate_risk_metrics"]) == RISK_KEYS


@pytest.mark.asyncio
async def test_dispatch_ok_contract() -> None:
    entry = {
        "report_lines": ["l"],
        "hedge_analysis": {
            "net_pnl": 10.0,
            "alpha_contribution": 7.0,
            "hedge_contribution": 3.0,
            "unpriced_count": 1,
            "uncosted_count": 2,
            "scope": "real",
        },
        "risk_metrics": _metrics().model_dump(),
        "positions": [{"symbol": str(i)} for i in range(30)],
        "report_failed": False,
    }
    raw, kwargs = await _dispatch({7: entry})
    _assert_contract(raw)
    assert raw["report_status"] == "ok"
    assert raw["macro_snapshot"]["spy_price"] is None
    assert raw["aggregate_risk_metrics"]["margin_heat_pct"] == 12.0
    assert raw["aggregate_risk_metrics"]["net_delta_exposure_pct"] == 2.5
    assert raw["pnl_attribution_since_entry"]["hedge_pnl"] == 3.0
    assert raw["pnl_attribution_since_entry"]["uncosted_count"] == 2
    assert len(raw["positions"]) == 20
    assert kwargs["report_failed"] is False


@pytest.mark.asyncio
async def test_dispatch_failed_contract() -> None:
    raw, kwargs = await _dispatch({7: {"report_failed": True}})
    _assert_contract(raw)
    assert raw["report_status"] == "failed"
    assert kwargs["report_failed"] is True
    assert not kwargs["hedge_analysis"]
    assert kwargs["downside_fields"] is None
    assert raw["positions"] == []
    assert raw["pnl_attribution_since_entry"]["total_net_pnl"] is None


@pytest.mark.asyncio
async def test_dispatch_gather_exception_marks_failed() -> None:
    raw, kwargs = await _dispatch({}, gather_exc=True)
    assert raw["report_status"] == "failed"
    assert kwargs["report_failed"] is True


@pytest.mark.asyncio
async def test_dispatch_no_positions_contract() -> None:
    raw, kwargs = await _dispatch({})
    _assert_contract(raw)
    assert raw["report_status"] == "no_positions"
    assert kwargs["report_failed"] is False
