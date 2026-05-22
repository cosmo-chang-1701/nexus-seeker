import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from services.market_data_service import _execute_api_call


@pytest.mark.asyncio
async def test_execute_api_call_success():
    """Test _execute_api_call runs successfully under normal conditions."""
    mock_func = MagicMock(return_value="success")
    res = await _execute_api_call(mock_func, "arg1", kwarg1="val")
    assert res == "success"
    mock_func.assert_called_once_with("arg1", kwarg1="val")


@pytest.mark.asyncio
async def test_execute_api_call_cooperative_backoff():
    """Test that _execute_api_call cooperative backoff delay occurs if _rate_limit_until is set in the future."""
    mock_func = MagicMock(return_value="delayed_success")
    future_time = time.time() + 1.0

    with patch("services.market_data_service._rate_limit_until", future_time), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as m_sleep:
        res = await _execute_api_call(mock_func)
        assert res == "delayed_success"

        # Verify that asyncio.sleep was called to wait out the rate limit
        assert m_sleep.called
        # The first sleep should be the remaining wait time
        args, kwargs = m_sleep.call_args_list[0]
        wait_time = args[0]
        assert 0.0 < wait_time <= 1.0


@pytest.mark.asyncio
async def test_execute_api_call_sets_rate_limit_on_429():
    """Test that _execute_api_call sets _rate_limit_until when hitting a 429."""
    mock_func = MagicMock()
    # Raise a 429 Exception on first call, succeed on second call
    mock_func.side_effect = [Exception("429 Too Many Requests"), "recovered"]

    # We must patch _rate_limit_until inside market_data_service so we don't pollute global state
    with patch("services.market_data_service._rate_limit_until", 0.0), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as m_sleep:
        res = await _execute_api_call(mock_func)
        assert res == "recovered"

        # Verify sleep was called for the 429 delay
        assert m_sleep.called

        # Verify services.market_data_service._rate_limit_until was updated to a future time
        import services.market_data_service

        # Since it is patched, the actual module variable won't be modified in global namespace,
        # but the local lookup in _execute_api_call modified the patched value.
        # Let's verify that the module reference (which is patched) was set.
        assert services.market_data_service._rate_limit_until > time.time()
