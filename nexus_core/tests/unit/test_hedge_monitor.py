import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from models.quant import MacroRiskMetrics
from services.hedge_monitor_service import HedgeMonitorService


@pytest.mark.asyncio
async def test_vix_spike_detection():
    bot = MagicMock()
    service = HedgeMonitorService(bot)

    # Mock macro environment
    with patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro:
        # Initial state: VIX 18 (Ready)
        mock_macro.return_value = {"vix": 18.0}
        await service._check_spikes_and_alerts()
        assert service._last_vix_level == 18.0
        assert service._last_vix_stage == 2  # Index of Ready

        # Scenario 1: VIX spikes to 24 (18 -> 24 is > 10% change)
        mock_macro.return_value = {"vix": 24.0}
        with patch.object(
            service, "_trigger_global_hedge_assessment", new_callable=AsyncMock
        ) as mock_trigger:
            await service._check_spikes_and_alerts()
            assert mock_trigger.called
            args, _ = mock_trigger.call_args
            assert args[0] == 24.0


@pytest.mark.asyncio
async def test_vix_stage_move_detection():
    bot = MagicMock()
    service = HedgeMonitorService(bot)

    with patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro:
        # Initial state: VIX 15 (Caution)
        mock_macro.return_value = {"vix": 15.0}
        await service._check_spikes_and_alerts()

        # Scenario 2: VIX jumps 2 stages (Caution -> Ready -> Aggressive)
        # Caution (15), Aggressive starts at 24.
        mock_macro.return_value = {"vix": 25.0}
        with patch.object(
            service, "_trigger_global_hedge_assessment", new_callable=AsyncMock
        ) as mock_trigger:
            await service._check_spikes_and_alerts()
            assert mock_trigger.called
            # Stage move: Aggressive (3) - Caution (1) = 2
            args, _ = mock_trigger.call_args
            assert args[1] == 2


@pytest.mark.asyncio
async def test_send_discord_alert_uses_embed_builder():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    service = HedgeMonitorService(bot)
    metrics = MacroRiskMetrics(
        net_exposure_dollars=50000.0,
        exposure_pct=25.0,
        total_beta_delta=75.0,
        gamma_threshold=10.0,
        theta_yield=0.12,
        portfolio_heat=18.0,
        portfolio_heat_limit=30.0,
        total_gamma=2.5,
        total_theta=120.0,
        total_margin_used=10000.0,
        total_vega=-15.5,
        total_vanna=8.2,
        vix_tier_name="Aggressive",
        vix_scale_multiplier=1.2,
    )
    embed = object()

    with patch(
        "services.hedge_monitor_service.create_hedge_alert_embed",
        return_value=embed,
    ) as mock_create:
        await service._send_discord_alert(
            user_id=123,
            vix=24.0,
            stage_move=2,
            metrics=metrics,
            adj_delta=82.0,
            hedge_qty=82,
            instr="賣出 82 股 SPY",
            narration="請先降低曝險。",
            alert_id=5,
            poly_snapshot=[{"question": "test", "odds_distribution": []}],
        )

    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["total_beta_delta"] == 75.0
    assert mock_create.call_args.kwargs["total_vega"] == -15.5
    bot.queue_dm.assert_awaited_once_with(123, embed=embed)
