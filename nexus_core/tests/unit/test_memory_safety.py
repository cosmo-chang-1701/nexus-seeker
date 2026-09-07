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


def test_is_memory_critical_matrix() -> None:
    """測試 RAM 與 Swap 綜合告警判定矩陣。"""
    from unittest.mock import MagicMock
    from services.memory_manager import MemoryManager

    bot = MagicMock()
    mm = MemoryManager(bot, threshold=90.0, swap_threshold=50.0, swap_critical=80.0)

    # 1. RAM 高但 Swap 充足 (低使用率) -> 正常置換緩衝，不應誤報
    assert (
        mm.is_memory_critical(mem_percent=92.5, swap_percent=12.0, swap_total=2048)
        is False
    )

    # 2. RAM 高且 Swap 超過 50% 壓力門檻 -> 綜合告警觸發
    assert (
        mm.is_memory_critical(mem_percent=91.0, swap_percent=55.0, swap_total=2048)
        is True
    )

    # 3. Swap 瀕臨耗盡 (>= 80%)，即便 RAM 未滿 90% -> 劇烈 Thrashing / OOM 告警
    assert (
        mm.is_memory_critical(mem_percent=82.0, swap_percent=85.0, swap_total=2048)
        is True
    )

    # 4. 無 Swap 分區環境 (swap_total == 0) -> 無緩衝，RAM >= 90% 即刻告警
    assert (
        mm.is_memory_critical(mem_percent=90.5, swap_percent=0.0, swap_total=0) is True
    )

    # 5. 無 Swap 分區環境但 RAM 安全 -> 不告警
    assert (
        mm.is_memory_critical(mem_percent=85.0, swap_percent=0.0, swap_total=0) is False
    )

    # 6. 有 Swap 分區但 Swap 尚未被使用 (0%) -> 不告警
    assert (
        mm.is_memory_critical(mem_percent=92.0, swap_percent=0.0, swap_total=2048)
        is False
    )

    # 7. 邊緣未知 Swap 大小 (swap_total is None) -> 遵循保守綜合判定
    assert (
        mm.is_memory_critical(mem_percent=92.0, swap_percent=10.0, swap_total=None)
        is False
    )
    assert (
        mm.is_memory_critical(mem_percent=92.0, swap_percent=55.0, swap_total=None)
        is True
    )

    # 8. 建構子顯式自定義門檻優先度驗證
    custom_mm = MemoryManager(
        bot, threshold=85.0, swap_threshold=40.0, swap_critical=75.0
    )
    assert custom_mm.threshold == 85.0
    assert custom_mm.swap_threshold == 40.0
    assert custom_mm.swap_critical == 75.0


@pytest.mark.asyncio
async def test_perform_health_check_alert_logic() -> None:
    """測試健康檢查時的警報與抑制邏輯。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.memory_manager import MemoryManager

    bot = MagicMock()
    mm = MemoryManager(bot, threshold=90.0, swap_threshold=50.0)
    mm._trigger_emergency_alert = AsyncMock()  # type: ignore

    with patch("psutil.virtual_memory") as mock_mem, patch(
        "psutil.swap_memory"
    ) as mock_swap, patch("psutil.Process"):
        # Case A: RAM 92% 但 Swap 只有 15% -> 不觸發緊急警報
        mock_mem.return_value.percent = 92.0
        mock_swap.return_value.percent = 15.0
        mock_swap.return_value.total = 1024 * 1024 * 1024
        await mm._perform_health_check()
        mm._trigger_emergency_alert.assert_not_awaited()

        # Case B: RAM 92% 且 Swap 達 55% -> 觸發緊急警報
        mock_swap.return_value.percent = 55.0
        await mm._perform_health_check()
        mm._trigger_emergency_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_emergency_cache_eviction_on_droplet_alert() -> None:
    """測試主節點發出警報時會自動清空歷史與期權鏈快取。"""
    from unittest.mock import MagicMock, patch
    from services.memory_manager import MemoryManager

    bot = MagicMock()
    mm = MemoryManager(bot)

    with patch(
        "services.market_data_service.clear_history_cache"
    ) as mock_clear_hist, patch(
        "services.market_data_service.clear_options_cache"
    ) as mock_clear_opt, patch("gc.collect") as mock_gc, patch(
        "config.DISCORD_ADMIN_USER_ID", 0
    ):
        await mm._trigger_emergency_alert(
            total_usage=92.0, proc_mem=500.0, swap_usage=55.0, source="Droplet (主節點)"
        )
        mock_clear_hist.assert_called_once()
        mock_clear_opt.assert_called_once()
        mock_gc.assert_called_once()


@pytest.mark.asyncio
async def test_memory_manager_independent_node_cooldown() -> None:
    """測試主節點與邊緣節點各自擁有獨立的 1 小時警報冷卻時間。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.memory_manager import MemoryManager

    bot = MagicMock()
    mm = MemoryManager(bot, threshold=90.0, swap_threshold=50.0)
    alerts_recorded: list[str] = []

    async def mock_alert(
        total_usage: float, proc_mem: float, swap_usage: float = 0.0, source: str = ""
    ) -> None:
        alerts_recorded.append(source)

    mm._trigger_emergency_alert = AsyncMock(side_effect=mock_alert)  # type: ignore

    with patch("psutil.virtual_memory") as mock_mem, patch(
        "psutil.swap_memory"
    ) as mock_swap, patch("psutil.Process"), patch(
        "config.TUNNEL_URL", "https://edge.example.com"
    ), patch("httpx.AsyncClient") as mock_client_cls:
        # Mock main node in critical state
        mock_mem.return_value.percent = 95.0
        mock_swap.return_value.percent = 60.0
        mock_swap.return_value.total = 1024**3

        # Mock edge node also in critical state
        mock_client = AsyncMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "os_system": "Darwin",
            "memory_percent": 96.0,
            "swap_percent": 65.0,
            "swap_total_mb": 2048.0,
            "process_memory_mb": 300.0,
        }
        mock_client.get = AsyncMock(return_value=mock_res)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # 第一輪檢測：主節點與邊緣節點皆應發出警報（兩者不互相阻擋）
        await mm._perform_health_check()
        assert "Droplet (主節點)" in alerts_recorded
        assert "Darwin (邊緣節點)" in alerts_recorded
        assert len(alerts_recorded) == 2

        # 立即第二輪檢測：因 1 小時冷卻期，兩者皆不應再次發送
        await mm._perform_health_check()
        assert len(alerts_recorded) == 2
