import pytest
from unittest.mock import AsyncMock, patch, MagicMock
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
