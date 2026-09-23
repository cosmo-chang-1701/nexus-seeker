from typing import Any
import pytest
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from database.orders import add_active_order
from database.holdings import add_holding

from market_analysis.telemetry_pricing_engine import (
    DataContaminationException,
    generate_alignment_decision,
)


@pytest.mark.asyncio
async def test_iv_rank_fuse_suppresses_price_up(caplog: Any, db_conn: Any):  # type: ignore
    """IV Rank > 0.70 must suppress any PRICE_UP suggestion."""
    user_id = 42
    symbol = "SPY"

    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.0,
    )

    # Create a PRICE_UP scenario via skew-tail baseline logic.
    with caplog.at_level("WARNING"):
        decision = await generate_alignment_decision(
            user_id=user_id,
            order_id=order_id,
            symbol=symbol,
            current_order_price=400.0,
            spot_price=486.51,
            original_qty=10,
            iv=0.55,
            hist_iv=0.35,
            iv_rank=1.0,  # 100%
            max_pain_price=437.50,
            prev_max_pain=437.50,
            skew_percentile_pct=98.0,
            put_call_ratio=1.0,
            order_side="SELL",
        )

    assert decision is None
    assert any("SYSTEM_LOCK: IV_TOO_HIGH" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_max_pain_expected_move_clamp(db_conn: Any):  # type: ignore
    """Suggested PRICE_UP must be clamped to upper_bound = min(max_pain, spot - EM)."""
    user_id = 43
    symbol = "SPY"

    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=100,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.0,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=400.0,
        spot_price=486.51,
        original_qty=100,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.50,
        max_pain_price=437.50,
        prev_max_pain=437.50,
        skew_percentile_pct=98.0,
        put_call_ratio=1.0,
        order_side="SELL",
    )

    assert decision is not None
    assert decision.action == "PRICE_UP"
    assert decision.suggested_price == 437.50
    assert decision.suggested_price > decision.current_order_price


@pytest.mark.asyncio
async def test_no_alignment_needed_when_clamp_not_above_current(db_conn: Any):  # type: ignore
    """If clamp results in <= current order price, suppress entirely."""
    user_id = 44
    symbol = "SPY"

    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=440.0,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=440.0,
        spot_price=486.51,
        original_qty=10,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.50,
        max_pain_price=437.50,
        prev_max_pain=437.50,
        skew_percentile_pct=98.0,
        put_call_ratio=1.0,
        order_side="SELL",
    )

    assert decision is None


@pytest.mark.asyncio
async def test_recent_clear_position_suppresses_buy_alignment(db_conn: Any):  # type: ignore
    """If holdings quantity == 0 updated within 24h, suppress buy/alignment alerts."""
    user_id = 45
    symbol = "TSLA"

    # Record a "cleared" holding (qty=0). This is treated as a recent CLEAR_POSITION.
    assert (
        add_holding(user_id=user_id, symbol=symbol, quantity=0.0, avg_cost=0.0) is True
    )

    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=200.0,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=200.0,
        spot_price=250.0,
        original_qty=10,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.30,
        max_pain_price=230.0,
        prev_max_pain=230.0,
        skew_percentile_pct=98.0,
        put_call_ratio=1.0,
    )

    assert decision is None


@pytest.mark.asyncio
async def test_data_contamination_raises_and_aborts(db_conn: Any):  # type: ignore
    user_id = 46
    symbol = "TSM"
    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.5,
    )

    with pytest.raises(DataContaminationException):
        await generate_alignment_decision(
            user_id=user_id,
            order_id=order_id,
            symbol=symbol,
            current_order_price=400.5,
            spot_price=444.94,
            original_qty=10,
            iv=0.55,
            hist_iv=0.35,
            iv_rank=0.4,
            max_pain_price=430.0,
            prev_max_pain=430.0,
            skew_percentile_pct=98.0,
            put_call_ratio=1.0,
            cache_price=401.0,
            live_price=444.94,
        )


@pytest.mark.asyncio
async def test_deep_sea_buy_relock_returns_suppressed_decision(db_conn: Any):  # type: ignore
    user_id = 47
    symbol = "TSM"
    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.5,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=400.5,
        spot_price=444.94,
        original_qty=10,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.4,
        max_pain_price=430.0,
        prev_max_pain=430.0,
        skew_percentile_pct=98.0,
        put_call_ratio=1.0,
        cache_price=444.8,
        live_price=444.94,
        order_side="BUY",
        emit_suppressed_decision=True,
    )

    assert decision is not None
    assert decision.action == "SUPPRESSED"
    assert decision.system_status_flag == "FORTRESS RE-LOCKED"
    assert "禁止追價改單" in decision.system_instruction_directive


@pytest.mark.asyncio
async def test_pure_stock_sovereign_gate_returns_suppressed_decision(db_conn: Any):  # type: ignore
    user_id = 48
    symbol = "TSM"
    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.5,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=440.5,
        spot_price=444.94,
        original_qty=10,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.4,
        max_pain_price=430.0,
        prev_max_pain=430.0,
        skew_percentile_pct=50.0,
        put_call_ratio=1.0,
        cache_price=444.8,
        live_price=444.94,
        holding_type="PURE_STOCK_100X",
        holding_shares=0.0,
        emit_suppressed_decision=True,
    )

    assert decision is not None
    assert decision.action == "SUPPRESSED"
    assert decision.system_status_flag == "FORTRESS RE-LOCKED"
    assert "空倉維持被動深海限價" in decision.system_instruction_directive


@pytest.mark.asyncio
async def test_uoa_macro_alignment_triggers_defensive_suppression(db_conn: Any):  # type: ignore
    user_id = 49
    symbol = "TSM"
    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=400.5,
    )

    decision = await generate_alignment_decision(
        user_id=user_id,
        order_id=order_id,
        symbol=symbol,
        current_order_price=440.5,
        spot_price=444.94,
        original_qty=10,
        iv=0.55,
        hist_iv=0.35,
        iv_rank=0.4,
        max_pain_price=430.0,
        prev_max_pain=430.0,
        skew_percentile_pct=50.0,
        put_call_ratio=1.0,
        cache_price=444.8,
        live_price=444.94,
        uoa_array=[
            {
                "expiration_date": "2026-06-12",
                "strike": 445.0,
                "option_type": "CALL",
                "volume_to_oi_ratio": 12.78,
            }
        ],
        macro_event_dates={"2026-06-12"},
        emit_suppressed_decision=True,
    )

    assert decision is not None
    assert decision.action == "SUPPRESSED"
    assert "機構籌碼鎖定事件週" in decision.system_instruction_directive


def test_resolve_skew_percentile_pct_prefers_real_percentile() -> None:
    """委託單遙測的 Skew 一律優先使用真實分位，分位缺失才退回絕對值換算。"""
    from services.order_telemetry_service import resolve_skew_percentile_pct

    # 真實分位優先：原始 Skew 6.0 (> 5) 但分位只有 40 → 40，不觸發尾端防禦
    assert resolve_skew_percentile_pct({"skew": 6.0, "skew_percentile": 40.0}) == 40.0
    # 低 Skew 標的的歷史高點：原始 Skew 3.0 (舊判定為中性) 但分位 97 → 97
    assert resolve_skew_percentile_pct({"skew": 3.0, "skew_percentile": 97.0}) == 97.0
    # 0~1% 是合法的最低分位，不得被放大
    assert resolve_skew_percentile_pct({"skew": -1.0, "skew_percentile": 0.8}) == 0.8
    # 分位缺失：退回改版前的絕對值換算
    assert resolve_skew_percentile_pct({"skew": 6.0, "skew_percentile": None}) == 98.0
    assert resolve_skew_percentile_pct({"skew": -3.0}) == 2.0
    assert resolve_skew_percentile_pct({"skew": 1.0}) == 50.0
    # 無任何資料：中性
    assert resolve_skew_percentile_pct({"skew": None, "skew_percentile": None}) == 50.0
    assert resolve_skew_percentile_pct(None) == 50.0
    # 越界分位視為中性
    assert resolve_skew_percentile_pct({"skew_percentile": 150.0}) == 50.0


@pytest.mark.asyncio
async def test_apply_fallback_uses_live_market_inputs(db_conn: Any) -> None:
    """一鍵套用在沒有記憶體／快取建議時，以即時資料重算，不再使用固定假值。

    舊版後備路徑寫死 IV 0.55、IV Rank 0.50、Max Pain 100、Skew 98、PCR 1.0；
    現在必須與 /telemetry_alert 共用同一份輸入。移動停損單不做價格對齊。
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from services.order_telemetry_service import apply_telemetry_to_orders

    orders = [
        {
            "id": 1,
            "symbol": "AAPL",
            "order_type": "LIMIT",
            "limit_price": 100.0,
            "stop_price": 0.0,
            "trailing_value": 0.0,
            "quantity": 10,
            "side": "BUY",
        },
        {
            "id": 2,
            "symbol": "AAPL",
            "order_type": "TRAILING_STOP_PCT",
            "limit_price": 0.0,
            "stop_price": 0.0,
            "trailing_value": 5.0,
            "quantity": 10,
            "side": "SELL",
        },
    ]
    iv_metrics = MagicMock(current_iv=0.22, iv_rank=35.0)
    decision = MagicMock(suggested_price=99.0, suggested_qty=10)
    mock_decision = AsyncMock(return_value=decision)

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch(
            "services.order_telemetry_service.fetch_cache_and_live_price",
            new=AsyncMock(return_value=(100.5, 101.0)),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=AsyncMock(return_value=iv_metrics),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(return_value={"skew": 6.0, "skew_percentile": 40.0}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new=AsyncMock(return_value={"max_pain": 97.0}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new=AsyncMock(return_value={"volume_pcr": 1.8}),
        ),
        patch(
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.calendar_service.calendar_service.get_high_impact_events",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "market_analysis.telemetry_pricing_engine.generate_alignment_decision",
            new=mock_decision,
        ),
        patch("database.orders.update_active_order_price") as mock_update,
    ):
        updated, _ = await apply_telemetry_to_orders(
            user_id=7,
            orders=orders,
            suggestions={},
            holding_type="PURE_STOCK_100X",
            holding_map={},
        )

    # 只有限價單被重算與更新；移動停損單略過
    assert mock_decision.await_count == 1
    assert updated == 1
    mock_update.assert_called_once_with(1, 99.0, 10)

    call = mock_decision.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["iv"] == pytest.approx(0.22)
    assert kwargs["iv_rank"] == pytest.approx(0.35)
    assert kwargs["max_pain_price"] == 97.0
    assert kwargs["skew_percentile_pct"] == 40.0
    assert kwargs["put_call_ratio"] == 1.8
