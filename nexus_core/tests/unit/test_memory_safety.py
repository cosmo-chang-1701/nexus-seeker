from unittest.mock import patch
from services.market_data_service import BoundedCache
from services.llm_service import is_memory_safe


def test_bounded_cache_lru():
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


def test_is_memory_safe_logic():
    with patch("psutil.virtual_memory") as mock_mem:
        # Case 1: Safe (70%)
        mock_mem.return_value.percent = 70.0
        assert is_memory_safe() is True

        # Case 2: Unsafe (90%)
        mock_mem.return_value.percent = 90.0
        assert is_memory_safe() is False
