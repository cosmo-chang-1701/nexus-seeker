import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

# Ensure we can import from nexus_core
import sys
import os

sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from market_analysis.risk_engine import (
    get_macro_risk_metrics,
    calculate_hedge_instruction,
)
from services.memory_manager import MemoryManager
from models.quant import MacroRiskMetrics


def test_task1_spy_delta_calculation():
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


def test_task1_nro_telemetry():
    """驗證 Task 1: NRO Telemetry 增強欄位 (現在已整合到 MacroRiskMetrics)"""
    metrics = get_macro_risk_metrics(
        total_beta_delta=166.94,
        total_theta=50.0,
        total_margin_used=5000.0,
        total_gamma=2.0,
        user_capital=100000.0,
        spy_price=500.0,
        vix_spot=20.0,
        total_vega=100.0,
        total_vanna=20.0,
    )

    assert isinstance(metrics, MacroRiskMetrics)
    assert metrics.total_beta_delta == 166.94
    assert metrics.total_theta == 50.0
    assert metrics.total_margin_used == 5000.0
    assert metrics.vix_scale_multiplier == 1.0


@pytest.mark.asyncio
async def test_task2_warmup_idempotency():
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


@pytest.mark.asyncio
async def test_task2_warmup_memory_gate():
    """驗證 Task 2: 快取預熱記憶體保護門檻"""
    bot = MagicMock()
    mm = MemoryManager(bot)

    with patch("psutil.virtual_memory") as mock_mem, patch(
        "database.watchlist.get_all_watchlist"
    ) as mock_list:
        # 模擬記憶體過高 (86%)
        mock_mem.return_value.percent = 86.0

        await mm.proactive_warmup()
        assert mm._last_warmup_date is None  # 未執行
        assert mock_list.call_count == 0
