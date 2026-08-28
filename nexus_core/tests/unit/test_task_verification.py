import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Ensure we can import from nexus_core
import sys
import os

sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from market_analysis.risk_engine import calculate_hedge_instruction
from services.memory_manager import MemoryManager


def test_task1_spy_delta_calculation() -> None:
    """驗證 Task 1: SPY Delta 換算邏輯 (1.0 vs 0.5)"""
    # 以前的錯誤邏輯是 qty = round(abs(adj_delta) / 0.5)
    # 現在應該是 qty = round(abs(adj_delta) / 1.0)

    # 使用 calculate_hedge_instruction(total_beta_delta, hedge_instrument_delta=-1.0)
    # 假設 total_beta_delta = 166.94, SPY 每股 Delta = 1.0 (用 -1.0 代表賣出 1 股抵消 1 Delta)
    qty = calculate_hedge_instruction(166.94, -1.0)
    assert qty == 167  # 需買入/賣出數量為 167 (方向取決於符號，此處公式已包含負號)

    # 驗證如果是 -166.94
    qty_long = calculate_hedge_instruction(-166.94, -1.0)
    assert qty_long == -167  # 代表反向操作


# Task 1 的 NRO Telemetry / MacroRiskMetrics 基本欄位驗證見
# tests/unit/test_risk_engine.py::test_get_macro_risk_metrics。


@pytest.mark.asyncio
async def test_task2_warmup_idempotency() -> None:
    """驗證 Task 2: 快取預熱冪等性"""
    bot = MagicMock()
    mm = MemoryManager(bot)

    # Mock dependencies
    with patch("database.watchlist.get_all_watchlist") as mock_list, patch(
        "services.market_data_service.get_quote", autospec=True
    ) as mock_quote, patch(
        "services.market_data_service.get_sma", autospec=True
    ), patch("services.market_data_service.get_ema", autospec=True):
        mock_list.return_value = [("user", "AAPL"), ("user", "MSFT")]

        # 第一次執行
        await mm.proactive_warmup()
        assert mm._last_warmup_date == datetime.now().strftime("%Y-%m-%d")
        first_call_count = mock_quote.call_count
        assert first_call_count > 0

        # 第二次執行 (同日)
        await mm.proactive_warmup()
        assert mock_quote.call_count == first_call_count  # 不應增加


# Task 2 的記憶體保護門檻分支驗證見
# tests/unit/test_memory_safety.py::test_memory_manager_warmup_gate。


@pytest.mark.asyncio
async def test_memory_manager_alert_uses_embed_builder() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    mm = MemoryManager(bot)
    embed = object()

    with patch("config.DISCORD_ADMIN_USER_ID", 999), patch(
        "services.memory_manager.create_memory_alert_embed", return_value=embed
    ) as mock_create, patch(
        "services.market_data_service._sma_cache", {1: 1, 2: 2}
    ), patch("services.market_data_service._ema_cache", {1: 1}):
        await mm._trigger_emergency_alert(92.5, 640.0)

    mock_create.assert_called_once_with(
        total_usage=92.5,
        process_memory_mb=640.0,
        sma_cache_size=2,
        ema_cache_size=1,
        swap_usage=0.0,
        source="Droplet (主節點)",
    )
    bot.queue_dm.assert_awaited_once_with(999, embed=embed)
