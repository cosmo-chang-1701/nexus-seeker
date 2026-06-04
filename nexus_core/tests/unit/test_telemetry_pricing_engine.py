import pytest
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from database.orders import add_active_order
from database.holdings import add_holding

from market_analysis.telemetry_pricing_engine import generate_alignment_decision


@pytest.mark.asyncio
async def test_iv_rank_fuse_suppresses_price_up(caplog, db_conn):
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
            skew_percentile=0.98,
            put_call_ratio=1.0,
        )

    assert decision is None
    assert any("SYSTEM_LOCK: IV_TOO_HIGH" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_max_pain_expected_move_clamp(db_conn):
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
        skew_percentile=0.98,
        put_call_ratio=1.0,
    )

    assert decision is not None
    assert decision.action == "PRICE_UP"
    assert decision.suggested_price == 437.50
    assert decision.suggested_price > decision.current_order_price


@pytest.mark.asyncio
async def test_no_alignment_needed_when_clamp_not_above_current(db_conn):
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
        skew_percentile=0.98,
        put_call_ratio=1.0,
    )

    assert decision is None


@pytest.mark.asyncio
async def test_recent_clear_position_suppresses_buy_alignment(db_conn):
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
        skew_percentile=0.98,
        put_call_ratio=1.0,
    )

    assert decision is None
