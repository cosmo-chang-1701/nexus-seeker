"""遙測對齊端到端：真實資料層＋真實 embed 建構，所有外部呼叫皆已 patch（不連網）。"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cogs.embed_builders._ansi_utils import _clean_ansi
from cogs.embed_builders.order_embeds import (
    create_telemetry_alignment_embeds,
    create_telemetry_apply_result_embed,
)
from database.orders import add_active_order, get_user_active_orders
from models.quant import IVMetrics

CONTRACT_KEYS = {
    "symbol", "order_id", "order_type", "side", "price_label", "current_price",
    "original_qty", "suggested_price", "suggested_qty", "suggestion_changed",
    "is_size_down", "action", "alignment_available", "live_price", "prev_close",
    "avg_cost", "gain_loss_pct", "holding_shares", "holding_status",
    "holding_type_label", "put_wall", "put_wall_is_stale", "max_pain_price",
    "wall_dist_pct", "wall_status", "skew_val", "skew_pct", "skew_status",
    "iv_val", "iv_rank", "iv_status", "iv_source", "iv_event_loading_applied",
    "is_premarket", "proximity_pct", "radar_status", "system_status_flag",
    "system_instruction_directive", "alert_text",
}  # fmt: skip


def _patch_inputs(iv: IVMetrics, gex: dict[str, Any], max_pain: float = 101.0) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=AsyncMock(return_value=iv),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(
                return_value={"skew": 1.2, "skew_percentile": 65.0, "state": "平穩"}
            ),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new=AsyncMock(return_value={"max_pain": max_pain}),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
            new=AsyncMock(return_value=[]),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new=AsyncMock(return_value={"volume_pcr": 1.0}),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new=AsyncMock(return_value=gex),
        )
    )
    stack.enter_context(
        patch(
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new=AsyncMock(return_value=None),
        )
    )
    return stack


@pytest.mark.asyncio
async def test_hist_iv_baseline_flows_into_decision_kwargs() -> None:
    from services.order_telemetry_service import _fetch_alignment_market_inputs

    live = IVMetrics(
        symbol="AAPL",
        current_iv=0.40,
        iv_rank=40.0,
        iv_source="LIVE_IV",
        iv_hist_mean_20d=0.30,
    )
    with _patch_inputs(live, {"put_wall": 95.0}):
        out = await _fetch_alignment_market_inputs("AAPL", set())
    assert out["decision_kwargs"]["hist_iv"] == pytest.approx(0.30)

    proxy = IVMetrics(
        symbol="AAPL",
        current_iv=0.40,
        iv_rank=40.0,
        iv_source="HV_PROXY",
        iv_hist_mean_20d=0.30,
    )
    with _patch_inputs(proxy, {"put_wall": 95.0}):
        out = await _fetch_alignment_market_inputs("AAPL", set())
    assert out["decision_kwargs"]["hist_iv"] == pytest.approx(
        out["decision_kwargs"]["iv"]
    )


@pytest.mark.asyncio
async def test_real_items_render_suggestion_wall_and_max_pain() -> None:
    from services.order_telemetry_service import build_telemetry_alignment_items

    iv = IVMetrics(
        symbol="AAPL",
        current_iv=0.35,
        iv_rank=45.0,
        iv_source="LIVE_IV",
        iv_hist_mean_20d=0.30,
    )
    orders = [
        {
            "id": 11,
            "user_id": 1,
            "symbol": "AAPL",
            "quantity": 20.0,
            "order_type": "LIMIT",
            "limit_price": 100.0,
            "stop_price": 0.0,
            "side": "BUY",
        }
    ]
    with (
        _patch_inputs(iv, {"put_wall": 95.0}, max_pain=101.0),
        patch(
            "services.order_telemetry_service.fetch_cache_and_live_price",
            new=AsyncMock(return_value=(101.0, 102.0, 100.5)),
        ),
        patch("services.order_telemetry_service.is_market_open", return_value=True),
    ):
        items, truncated = await build_telemetry_alignment_items(
            user_id=1,
            orders=orders,
            holding_type="LEVERAGED_MARGIN",
            holding_map={},
            macro_event_dates=set(),
        )
    assert truncated is False and len(items) == 1
    assert CONTRACT_KEYS <= set(items[0])

    [embed] = create_telemetry_alignment_embeds(items)
    text = _clean_ansi(embed.fields[0].value or "")
    assert "對齊建議" in text
    assert "GEX PutWall: $95.00" in text
    assert "最大痛點: $101.00" in text


@pytest.mark.asyncio
async def test_real_apply_on_stop_limit_renders_limit_and_shifted_stop(
    db_conn: Any,
) -> None:
    from services.order_telemetry_service import apply_telemetry_to_orders

    user_id = 4242
    order_id = add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=20,
        order_type="STOP_LIMIT",
        validity="DAY",
        limit_price=100.0,
        stop_price=102.0,
    )
    orders = get_user_active_orders(user_id)
    count, details = await apply_telemetry_to_orders(
        user_id=user_id,
        orders=orders,
        suggestions={order_id: (98.0, 15)},
        holding_type="LEVERAGED_MARGIN",
        holding_map={},
    )
    assert count == 1 and len(details) == 1
    assert details[0]["new_stop"] == pytest.approx(100.0)

    embeds = create_telemetry_apply_result_embed(count, details)
    text = _clean_ansi("\n".join((f.value or "") for e in embeds for f in e.fields))
    assert "$98.00" in text  # 限價腿
    assert "$100.00" in text  # 平移後的停損腿
