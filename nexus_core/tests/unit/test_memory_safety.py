import pytest
from unittest.mock import patch
from services.market_data_service import BoundedCache
from services.llm_service import is_memory_safe


def test_bounded_cache_lru() -> None:
    cache = BoundedCache(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3

    assert len(cache) == 3
    assert list(cache.keys()) == ["a", "b", "c"]

    # Add one more, 'a' should be removed (oldest)
    cache["d"] = 4
    assert len(cache) == 3
    assert "a" not in cache
    assert list(cache.keys()) == ["b", "c", "d"]

    # Access 'b', making it most recent
    _ = cache["b"]
    cache["e"] = 5
    # 'c' should be removed next since 'b' was moved to end
    assert "c" not in cache
    assert list(cache.keys()) == ["d", "b", "e"]


def test_is_memory_safe_logic() -> None:
    with patch("psutil.virtual_memory") as mock_mem, patch(
        "psutil.swap_memory"
    ) as mock_swap:
        # Case 1: Safe (70%)
        mock_mem.return_value.total = 1000
        mock_mem.return_value.used = 700
        mock_swap.return_value.total = 0
        mock_swap.return_value.used = 0
        assert is_memory_safe() is True

        # Case 2: Unsafe (90%)
        mock_mem.return_value.used = 900
        assert is_memory_safe() is False


def test_squeeze_engine_memory_gate() -> None:
    import pandas as pd
    from market_analysis.squeeze_engine import calculate_power_squeeze

    df = pd.DataFrame(
        {
            "Close": [100.0 + i for i in range(30)],
            "High": [105.0 + i for i in range(30)],
            "Low": [95.0 + i for i in range(30)],
        }
    )

    # When memory is safe
    with patch("market_analysis.squeeze_engine.is_memory_safe", return_value=True):
        res_safe = calculate_power_squeeze(df)
        assert "is_squeezing" in res_safe
        assert "momentum" in res_safe

    # When memory is unsafe
    with patch("market_analysis.squeeze_engine.is_memory_safe", return_value=False):
        res_unsafe = calculate_power_squeeze(df)
        assert res_unsafe == {"is_squeezing": False, "momentum": 0.0, "direction": "⚪"}


@pytest.mark.asyncio
async def test_memory_manager_warmup_gate() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from services.memory_manager import MemoryManager

    bot = MagicMock()
    mm = MemoryManager(bot)

    # When memory is unsafe
    with patch("services.memory_manager.is_memory_safe", return_value=False), patch(
        "database.watchlist.get_all_watchlist"
    ) as mock_wl:
        await mm.proactive_warmup()
        mock_wl.assert_not_called()

    # When memory is safe
    with patch("services.memory_manager.is_memory_safe", return_value=True), patch(
        "database.watchlist.get_all_watchlist", return_value=[(1, "SPY")]
    ), patch("services.market_data_service.get_quote", new_callable=AsyncMock), patch(
        "services.market_data_service.get_sma", new_callable=AsyncMock
    ), patch("services.market_data_service.get_ema", new_callable=AsyncMock), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        await mm.proactive_warmup()
        assert mm._last_warmup_date is not None
