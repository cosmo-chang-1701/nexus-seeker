"""帳戶資料讀取失敗 (UserContext.load_failed) 時各呼叫端須 fail closed。

每個守衛測試都有成對的對照組：同一組 fixture 在 load_failed=False 時會產生
指令 / 寫入 / 口數 / 警報，load_failed=True 時則不會。
"""

import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import database
from database.user_settings import USER_CONTEXT_LOAD_FAILED_WARNING, UserContext
from market_analysis.dynamic_rollover import DynamicRolloverEngine, ShortCandidateInput
from models.quant import OptimizationResult
from tests.unit.test_sar_kelly_sell_put_sizing import _live_iv, _run_hub
from tests.unit.short_entry_helpers import make_short_entry_evaluation


def _ctx(failed: bool, **over: Any) -> UserContext:
    kw: dict[str, Any] = dict(
        user_id=7,
        capital=50_000.0,
        risk_limit=15.0,
        total_weighted_delta=20.0,
        total_theta=0.2,
        total_gamma=-5.0,
        total_vanna=6.0,
        load_failed=failed,
    )
    kw.update(over)
    return UserContext(**kw)


# ---------------------------------------------------------------- context
def test_context_db_error_marks_load_failed() -> None:
    with patch(
        "database.user_settings.get_read_connection",
        side_effect=sqlite3.OperationalError("boom"),
    ):
        ctx = database.get_full_user_context(99)
    assert ctx.load_failed is True
    assert ctx.capital == 100000.0 and ctx.risk_limit == 15.0
    assert ctx.total_weighted_delta == 0.0


def test_context_no_settings_row_is_not_a_failure(db_conn: sqlite3.Connection) -> None:
    import json

    db_conn.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) "
        "VALUES (?, ?, ?, ?)",
        (4343, "AAPL", "HOLDING", json.dumps({"weighted_delta": 120.0})),
    )
    db_conn.commit()
    ctx = database.get_full_user_context(4343)
    assert ctx.load_failed is False
    assert ctx.total_weighted_delta == pytest.approx(120.0)


def test_context_default_dataclass_not_failed() -> None:
    assert UserContext(1, 1.0, 15.0, 0.0, 0.0, 0.0).load_failed is False
    assert "暫停" in USER_CONTEXT_LOAD_FAILED_WARNING


# ---------------------------------------------------------------- xpanel
@pytest.mark.asyncio
async def test_xpanel_load_failed_sizes_zero_with_warning() -> None:
    from tests.unit.test_x_panel_embed_budget import _mrvl_data
    from tests.unit.test_x_panel_gex_uoa_coherence import _render

    ok_res, ok_kw = await _run_hub(
        _live_iv(),
        SimpleNamespace(capital=500_000.0, risk_limit=15.0, cash_reserve=0.0),
    )
    assert ok_kw and ok_res["kelly_sizing"].suggested_contracts == 1

    res, kw = await _run_hub(
        _live_iv(),
        SimpleNamespace(
            capital=100_000.0, risk_limit=15.0, cash_reserve=0.0, load_failed=True
        ),
    )
    assert kw == {}  # 不得以佔位資金呼叫倉位引擎
    sizing = res["kelly_sizing"]
    assert sizing.suggested_contracts == 0
    assert sizing.warnings == [USER_CONTEXT_LOAD_FAILED_WARNING]

    d = _mrvl_data()
    d["kelly_sizing"] = sizing
    text, _ = _render(d)
    assert "0 口原因" not in text
    assert USER_CONTEXT_LOAD_FAILED_WARNING in text


# ---------------------------------------------------------------- analysis
async def _run_manual_scan(failed: bool) -> tuple[MagicMock, dict[str, Any]]:
    import pandas as pd
    from cogs.terminal import analysis

    captured: dict[str, Any] = {}

    def _scan_embed(data: dict[str, Any], capital: float) -> str:
        captured.update(data)
        captured["_capital"] = capital
        return "embed"

    opt = MagicMock(
        return_value=OptimizationResult(suggested_contracts=3, exposure_pct=2.0)
    )
    interaction = MagicMock()
    interaction.user.id = 7
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    result = {
        "symbol": "AAA",
        "price": 100.0,
        "strike": 95.0,
        "strategy": "STO_PUT",
        "weighted_delta": -3.0,
        "iv": 0.3,
    }
    with (
        patch.object(analysis, "create_scan_embed", _scan_embed),
        patch("market_analysis.risk_engine.optimize_position_risk", opt),
        patch(
            "market_analysis.risk_engine.load_available_collateral",
            new=AsyncMock(return_value=(None, None)),
        ),
        patch("database.get_full_user_context", return_value=_ctx(failed)),
        patch(
            "services.market_data_service.validate_symbol",
            new=AsyncMock(return_value=True),
        ),
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
        patch(
            "services.market_data_service.get_spy_history_df",
            new=AsyncMock(return_value=pd.DataFrame({"Close": [500.0]})),
        ),
        patch(
            "services.market_data_service.get_macro_environment",
            new=AsyncMock(return_value={"vix": 20.0, "oil": 70.0, "vix_change": 0.0}),
        ),
        patch("market_math.analyze_symbol", new=AsyncMock(return_value=dict(result))),
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=pd.DataFrame()),
        ),
        patch("market_analysis.psq_engine.analyze_psq", return_value=None),
        patch("database.cache.get_kv_cache", return_value=None),
        patch(
            "services.news_service.fetch_recent_news",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(return_value={"skew": 0.0}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new=AsyncMock(return_value={"pcr": 0.9}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.llm_service.evaluate_trade_risk",
            new=AsyncMock(return_value={"decision": "APPROVE"}),
        ),
    ):
        await analysis.manual_scan_impl(interaction, "aaa")
    return opt, captured


@pytest.mark.asyncio
async def test_analysis_zero_contracts_when_context_failed() -> None:
    opt, data = await _run_manual_scan(False)
    assert opt.call_count == 1
    assert data["safe_qty"] == 3 and data["_capital"] == 50_000.0

    opt, data = await _run_manual_scan(True)
    assert opt.call_count == 0
    assert data["safe_qty"] == 0
    assert data["nro_warnings"] == [USER_CONTEXT_LOAD_FAILED_WARNING]


def test_analysis_whatif_renderer_shows_warning_not_verdict() -> None:
    from cogs.embed_builders._embed_helpers import _add_risk_optimization_fields

    def _fields(warnings: list[str]) -> str:
        embed = MagicMock()
        added: list[str] = []
        embed.add_field.side_effect = lambda **kw: added.append(
            f"{kw.get('name')}|{kw.get('value')}"
        )
        data = {
            "projected_exposure_pct": 0.0,
            "safe_qty": 0,
            "hedge_spy": 0.0,
            "risk_limit": 15.0,
            "nro_warnings": warnings,
        }
        with patch(
            "cogs.embed_builders._embed_helpers._add_ansi_field_safely",
            side_effect=lambda e, name, value: added.append(f"{name}|{value}"),
        ):
            _add_risk_optimization_fields(embed, data)
        return "\n".join(added)

    ok = _fields([])
    bad = _fields([USER_CONTEXT_LOAD_FAILED_WARNING])
    assert "風險受控" in ok
    assert "風險受控" not in bad and "What-if" not in bad
    assert USER_CONTEXT_LOAD_FAILED_WARNING in bad


# ---------------------------------------------------------------- market_scan
async def _run_market_scan_ctx(failed: bool) -> AsyncMock:
    import pandas as pd
    from services.trading_service import TradingService

    svc = TradingService(MagicMock())
    spy_df = pd.DataFrame({"Close": [500.0]})
    collateral = AsyncMock(return_value=(None, None))
    with (
        patch("database.get_all_watchlist", return_value=[(7, "AAA", 1)]),
        patch("database.holdings.get_all_holdings", return_value=[]),
        patch(
            "services.market_data_service.get_spy_history_df",
            new_callable=AsyncMock,
            return_value=spy_df,
        ),
        patch(
            "services.market_data_service.get_macro_environment",
            new_callable=AsyncMock,
            return_value={"vix": 15.0, "oil": 70.0, "vix_change": 0.0},
        ),
        patch.object(
            svc,
            "_scan_single_target",
            new=AsyncMock(
                return_value=(("AAA", 0.0), {"is_option_valid": False, "symbol": "AAA"})
            ),
        ),
        patch("database.get_full_user_context", return_value=_ctx(failed)),
        patch(
            "market_analysis.portfolio.refresh_portfolio_greeks",
            new_callable=AsyncMock,
        ),
        patch("market_analysis.risk_engine.load_available_collateral", collateral),
    ):
        await svc.run_market_scan(is_auto=True)
    return collateral


@pytest.mark.asyncio
async def test_market_scan_skips_user_with_failed_context() -> None:
    assert (await _run_market_scan_ctx(False)).await_count == 1
    assert (await _run_market_scan_ctx(True)).await_count == 0


# ---------------------------------------------------------------- vtr
async def _run_vtr(failed: bool) -> list[dict[str, Any]]:
    from services.trading_service import TradingService

    svc = TradingService(MagicMock())
    svc.vtr_engine = MagicMock()
    svc.vtr_engine.manage_virtual_positions = AsyncMock()
    svc.vtr_engine.execute_virtual_roll = AsyncMock()
    svc.vtr_engine.get_transition_candidates = AsyncMock(return_value=[])
    opens = iter([[{"id": 1}], []])
    with (
        patch(
            "database.virtual_trading.get_all_open_virtual_trades",
            side_effect=lambda: next(opens),
        ),
        patch(
            "database.virtual_trading.get_virtual_trades",
            return_value=[{"id": 1, "user_id": 7}],
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 500.0},
        ),
        patch("database.get_full_user_context", return_value=_ctx(failed)),
        patch(
            "market_analysis.hedging.get_market_regime_target",
            new_callable=AsyncMock,
            return_value=(10.0, "Balanced"),
        ),
    ):
        return await svc.monitor_vtr_and_calculate_hedging()


@pytest.mark.asyncio
async def test_vtr_skips_hedge_for_failed_context() -> None:
    ok = await _run_vtr(False)
    assert len(ok) == 1 and ok[0]["uid"] == 7
    assert await _run_vtr(True) == []


# ---------------------------------------------------------------- margin_defense
async def _run_margin(failed: bool) -> list[Any]:
    engine = DynamicRolloverEngine()
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "spot_price": 100.0,
            "sqz_mom": -5.0,
            "skew": -0.5,
            "uoa": [
                {
                    "type": "PUT",
                    "action": "BTO",
                    "ratio": 2.0,
                    "notional_value": 600_000.0,
                    "strike": 100.0,
                }
            ],
        }
    ]
    with (
        patch(
            "market_analysis.dynamic_rollover.margin_defense."
            "confirm_inverse_hedge_spot_momentum",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="SHORT_GAMMA_CRITICAL",
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "market_analysis.dynamic_rollover.get_full_user_context",
            return_value=_ctx(failed, cash_reserve=1000.0),
        ),
    ):
        return list(await engine.evaluate_margin_defense(1, portfolio))


@pytest.mark.asyncio
async def test_margin_defense_no_instructions_when_context_failed() -> None:
    assert len(await _run_margin(False)) == 1
    assert await _run_margin(True) == []


# ---------------------------------------------------------------- hedging
@pytest.mark.asyncio
async def test_hedging_tau_does_not_upsert_from_placeholder() -> None:
    from market_analysis import hedging

    history = [
        {"effectiveness": 0.3, "alpha_pnl": 100.0, "hedge_pnl": -50.0} for _ in range(4)
    ]

    async def _run(failed: bool) -> MagicMock:
        upsert = MagicMock()
        with (
            patch("database.get_hedge_history", return_value=history),
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch("database.upsert_user_config", upsert),
        ):
            await hedging.calculate_dynamic_tau(7)
        return upsert

    ok = await _run(False)
    ok.assert_called_once()
    with pytest.raises(RuntimeError):
        await _run(True)
    with (
        patch("database.get_hedge_history", return_value=history),
        patch("database.get_full_user_context", return_value=_ctx(True)),
        patch("database.upsert_user_config") as upsert,
        pytest.raises(RuntimeError),
    ):
        await hedging.calculate_dynamic_tau(7)
    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_hedging_effectiveness_skips_history_write_when_failed() -> None:
    from market_analysis import hedging

    perf = {"alpha_contribution": 1.0, "hedge_contribution": -1.0, "effectiveness": 0.5}

    async def _run(failed: bool) -> tuple[Any, MagicMock]:
        add = MagicMock()
        with (
            patch.object(
                hedging, "analyze_hedge_performance", new=AsyncMock(return_value=perf)
            ),
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch("database.add_hedge_history", add),
        ):
            out = await hedging.calculate_daily_effectiveness(7)
        return out, add

    out, add = await _run(False)
    add.assert_called_once()
    out, add = await _run(True)
    assert out == perf
    add.assert_not_called()


def test_hedging_advice_unchanged_when_context_failed() -> None:
    from market_analysis import hedging

    def _run(failed: bool) -> dict[str, Any]:
        advice = {"action": "RE_HEDGE", "suggested_spy_qty": 10.0, "reason": "r"}
        with patch(
            "database.get_full_user_context",
            return_value=_ctx(failed, dynamic_tau=0.5),
        ):
            return hedging.get_tuned_risk_advice(7, advice)

    assert _run(False)["suggested_spy_qty"] == 5.0
    bad = _run(True)
    assert bad["suggested_spy_qty"] == 10.0 and bad["reason"] == "r"


# ---------------------------------------------------------------- settings
@pytest.mark.asyncio
async def test_settings_refuses_write_when_context_failed() -> None:
    from cogs.settings_ui import AccountSettingsView

    async def _run(failed: bool) -> tuple[AsyncMock, MagicMock]:
        upsert = MagicMock()
        interaction = AsyncMock()
        interaction.data = {"values": ["enable_vtr"]}
        with (
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch("database.upsert_user_config", upsert),
        ):
            view = AccountSettingsView(7)
            await view.on_select_callback(interaction)
        return interaction, upsert

    interaction, upsert = await _run(False)
    upsert.assert_called_once()
    interaction, upsert = await _run(True)
    upsert.assert_not_called()
    interaction.response.send_message.assert_awaited_once()
    assert "帳戶設定讀取失敗" in interaction.response.send_message.call_args.args[0]
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_settings_select_views_refuse_when_context_failed() -> None:
    from cogs.settings_ui import RiskAppetiteSelect, TradingStrategySelect

    for cls, key in (
        (TradingStrategySelect, "DYNAMIC"),
        (RiskAppetiteSelect, "AGGRESSIVE"),
    ):
        for failed in (False, True):
            upsert = MagicMock()
            parent = MagicMock()
            interaction = AsyncMock()
            interaction.data = {"values": [key]}
            with (
                patch("database.get_full_user_context", return_value=_ctx(failed)),
                patch("database.upsert_user_config", upsert),
            ):
                sel = cls(7, parent)
                await sel.callback(interaction)
            assert upsert.called is (not failed)


# ---------------------------------------------------------------- short_entry
@pytest.mark.asyncio
async def test_short_entry_no_instruction_when_context_failed() -> None:
    engine = DynamicRolloverEngine()
    radar = {
        "quote": {"c": 100.0},
        "psq_result": {"squeeze_level": "Release", "signal_direction": "Short"},
        "gex_profile_data": {},
        "uoa": [],
    }

    async def _run(failed: bool) -> list[Any]:
        ctx = SimpleNamespace(
            trading_strategy="SHORT_SIDE",
            capital=100_000.0,
            risk_limit=15.0,
            load_failed=failed,
        )
        with (
            patch(
                "market_analysis.dynamic_rollover.short_entry_deployment."
                "get_full_user_context",
                return_value=ctx,
            ),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
                return_value=make_short_entry_evaluation(),
            ),
        ):
            return list(
                await engine.evaluate_short_entry_opportunity(
                    1, [ShortCandidateInput("AAA", radar)], vix_spot=20.0
                )
            )

    assert len(await _run(False)) == 1
    assert await _run(True) == []


# ---------------------------------------------------------------- anti_washout
@pytest.mark.asyncio
async def test_anti_washout_falls_back_to_defensive_when_context_failed() -> None:
    from market_analysis.dynamic_rollover import anti_washout

    async def _run(failed: bool) -> MagicMock:
        ctx = SimpleNamespace(
            risk_appetite="AGGRESSIVE",
            capital=50_000.0,
            risk_limit=9.0,
            load_failed=failed,
        )
        resolve = MagicMock(wraps=anti_washout.resolve_risk_profile)
        with patch.object(anti_washout, "resolve_risk_profile", resolve), patch(
            "database.orders.get_user_active_orders", return_value=[]
        ):
            await anti_washout.check_satellite_rebalancing_impl(
                MagicMock(), lambda _uid: ctx, 7, [], 100_000.0
            )
        return resolve

    assert (await _run(False)).call_args.args[0] == "AGGRESSIVE"
    assert (await _run(True)).call_args.args[0] == "DEFENSIVE"


# ---------------------------------------------------------------- pipeline
@pytest.mark.asyncio
@pytest.mark.slow
async def test_pipeline_skips_user_with_failed_context() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from market_analysis.intraday_pipeline.pipeline import IntradayScanPipeline

    async def _run(failed: bool) -> list[str]:
        pipe = IntradayScanPipeline(MagicMock())
        pipe.is_running = True
        called: list[str] = []

        async def _eval(ticker: Any) -> None:
            called.append(ticker)

        pipe.evaluate_watchlist_symbol = _eval  # type: ignore[method-assign, assignment]
        now = datetime(2026, 6, 5, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        async def _sleep(_s: Any) -> None:
            pipe.is_running = False

        with (
            patch(
                "market_analysis.intraday_pipeline.pipeline.is_market_open",
                return_value=True,
            ),
            patch("market_analysis.intraday_pipeline.pipeline.datetime") as dt_cls,
            patch("database.get_all_user_ids", return_value=[7]),
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch("database.get_user_watchlist", return_value=[("AAA", 1)]),
            patch("services.llm_service.is_memory_safe", return_value=True),
            patch("asyncio.sleep", side_effect=_sleep),
        ):
            dt_cls.now.return_value = now
            await pipe._run_loop()
        return called

    assert await _run(False) == ["AAA"]
    assert await _run(True) == []


# ---------------------------------------------------------------- scan_cog
@pytest.mark.asyncio
async def test_scan_cog_skips_alerts_and_vtr_when_context_failed() -> None:
    from cogs.trading.scan import MarketScanCog

    async def _run(failed: bool) -> tuple[AsyncMock, AsyncMock, MagicMock]:
        cog = MarketScanCog(MagicMock())
        data = {"symbol": "AAA", "alert_type": "OPTION", "price": 100.0}
        vtr = AsyncMock()
        notify_many = AsyncMock()
        scan_embed = MagicMock(return_value=MagicMock())
        cog.trading_service.run_market_scan = AsyncMock(  # type: ignore[method-assign]
            return_value={7: [data]}
        )
        cog.trading_service.execute_vtr_auto_entry = vtr  # type: ignore[method-assign]
        with (
            patch("database.get_all_watchlist", return_value=[]),
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch("database.get_watchlist_alert_state", return_value=None),
            patch(
                "cogs.trading.scan.should_send_priority_alert",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch("cogs.trading.scan.notify_many", notify_many),
            patch("cogs.trading.scan.notify", AsyncMock()),
            patch("cogs.trading.scan.create_scan_embed", scan_embed),
        ):
            await cog._run_market_scan_logic(is_auto=True)
        return vtr, notify_many, scan_embed

    vtr, notify_many, scan_embed = await _run(False)
    vtr.assert_awaited_once()
    scan_embed.assert_called_once()
    assert scan_embed.call_args.args[1] == 50_000.0
    vtr, notify_many, scan_embed = await _run(True)
    vtr.assert_not_awaited()
    notify_many.assert_not_awaited()
    scan_embed.assert_not_called()


@pytest.mark.asyncio
async def test_scan_cog_iv_scan_skipped_when_context_failed() -> None:
    from cogs.trading.scan import MarketScanCog

    async def _run(failed: bool) -> AsyncMock:
        cog = MarketScanCog(MagicMock())
        iv_scan = AsyncMock(return_value=[])
        cog.trading_service.run_ddp_scan = AsyncMock(return_value=[])  # type: ignore[method-assign]
        cog.trading_service.run_iv_opportunity_scan = iv_scan  # type: ignore[method-assign]
        cog.trading_service.run_market_scan = AsyncMock(return_value={})  # type: ignore[method-assign]
        with (
            patch("database.get_all_watchlist", return_value=[(7, "AAA", 1)]),
            patch("database.get_full_user_context", return_value=_ctx(failed)),
            patch(
                "cogs.trading.scan.is_channel_enabled",
                new=AsyncMock(return_value=True),
            ),
        ):
            await cog._run_market_scan_logic(is_auto=True)
        return iv_scan

    (await _run(False)).assert_awaited_once()
    (await _run(True)).assert_not_awaited()


# ---------------------------------------------------------------- event_monitor
@pytest.mark.asyncio
async def test_event_monitor_sends_no_alert_when_context_failed() -> None:
    from services.event_monitor import EventMonitor

    async def _run(failed: bool) -> tuple[bool, MagicMock]:
        bot = MagicMock()
        bot.queue_dm = AsyncMock()
        monitor = EventMonitor(bot)
        events = [SimpleNamespace(type="ECONOMIC", event="CPI", tte_hours=8)]
        builder = MagicMock(return_value=[object()])
        with (
            patch(
                "services.event_monitor.get_full_user_context",
                return_value=_ctx(failed),
            ),
            patch(
                "services.event_monitor.get_quote",
                new=AsyncMock(return_value={"c": 500.0}),
            ),
            patch("services.event_monitor.create_proactive_event_alert_embed", builder),
        ):
            sent = await monitor._send_event_alert(123, events)
        return sent, builder

    sent, builder = await _run(False)
    builder.assert_called_once()
    sent, builder = await _run(True)
    assert sent is False
    builder.assert_not_called()
