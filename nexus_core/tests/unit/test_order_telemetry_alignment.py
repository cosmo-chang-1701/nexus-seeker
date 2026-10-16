"""遙測對齊快照的資料來源／數學／範圍行為測試 (全部不連網)。"""

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.orders import add_active_order, get_user_active_orders
from market_analysis.telemetry_pricing_engine import (
    AlignmentDecision,
    _build_alert_text,
    generate_alignment_decision,
)
from services import order_telemetry_service as svc

SVC = "services.order_telemetry_service"


# ---------------------------------------------------------------------------
# 共用輔助
# ---------------------------------------------------------------------------
def _order(
    order_id: int = 1,
    symbol: str = "AAPL",
    order_type: str = "LIMIT",
    limit_price: float = 100.0,
    stop_price: float = 0.0,
    quantity: float = 10,
    side: str = "BUY",
) -> dict[str, Any]:
    return {
        "id": order_id,
        "symbol": symbol,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "trailing_value": 0.0,
        "quantity": quantity,
        "side": side,
    }


def _inputs(
    *,
    iv_metrics: Any = None,
    put_wall: float | None = 95.0,
    stale: bool = False,
    max_pain: float = 98.0,
) -> dict[str, Any]:
    return {
        "decision_kwargs": {
            "iv": 0.30,
            "hist_iv": 0.30,
            "iv_rank": 0.40,
            "max_pain_price": max_pain,
            "prev_max_pain": max_pain,
            "skew_percentile_pct": 50.0,
            "put_call_ratio": 1.0,
            "uoa_array": [],
        },
        "iv_metrics": iv_metrics,
        "skew_metrics": {"skew": 1.5, "skew_percentile": 60.0, "state": "平穩"},
        "max_pain_price": max_pain,
        "put_wall": put_wall,
        "put_wall_is_stale": stale,
    }


def _iv(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "current_iv": 0.30,
        "iv_rank": 40.0,
        "iv_status": "Normal",
        "iv_source": "LIVE_IV",
        "event_loading_applied": False,
        "is_premarket": False,
        "iv_history_count": 70,
        "iv_history_required": 60,
        "iv_hist_mean_20d": 0.28,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _decision(**kw: Any) -> AlignmentDecision:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "order_id": 1,
        "action": "PRICE_UP",
        "current_order_price": 100.0,
        "spot_price": 101.0,
        "suggested_price": 100.5,
        "original_qty": 10,
        "suggested_qty": 10,
        "is_size_down": False,
        "reasons": [],
        "alert_text": None,
    }
    base.update(kw)
    return AlignmentDecision(**base)


async def _build(
    orders: list[dict[str, Any]],
    *,
    quote: tuple[float, float, float | None] = (101.0, 101.0, 99.5),
    inputs: dict[str, Any] | None = None,
    decision: Any = None,
    holding_type: str = "LEVERAGED_MARGIN",
    holding_map: dict[str, dict] | None = None,
) -> tuple[list[dict], bool, dict[str, MagicMock]]:
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        mocks["quote"] = stack.enter_context(
            patch(
                f"{SVC}.fetch_cache_and_live_price", new=AsyncMock(return_value=quote)
            )
        )
        mocks["inputs"] = stack.enter_context(
            patch(
                f"{SVC}._fetch_alignment_market_inputs",
                new=AsyncMock(return_value=inputs or _inputs(iv_metrics=_iv())),
            )
        )
        mocks["decide"] = stack.enter_context(
            patch(
                "market_analysis.telemetry_pricing_engine.generate_alignment_decision",
                new=AsyncMock(return_value=decision or _decision()),
            )
        )
        items, truncated = await svc.build_telemetry_alignment_items(
            user_id=1,
            orders=orders,
            holding_type=holding_type,
            holding_map=holding_map if holding_map is not None else {},
            macro_event_dates=set(),
        )
    return items, truncated, mocks


# ---------------------------------------------------------------------------
# 即時價來源與快取作廢
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_price_from_get_quote_after_symbol_invalidation() -> None:
    from services import market_data_service as mds

    get_quote = AsyncMock(
        side_effect=[{"c": 100.0, "pc": 98.0}, {"c": 101.5, "pc": 99.0}]
    )
    invalidate = MagicMock()
    clear = MagicMock()
    yf = AsyncMock(return_value={"c": 555.0})
    with (
        patch.object(mds, "get_quote", get_quote),
        patch.object(mds, "invalidate_quote_cache", invalidate),
        patch.object(mds, "clear_quote_cache", clear),
        patch.object(mds, "get_yfinance_quote", yf),
    ):
        cache_price, live_price, prev_close = await svc.fetch_cache_and_live_price(
            "AAPL"
        )

    assert (cache_price, live_price, prev_close) == (100.0, 101.5, 99.0)
    invalidate.assert_called_once_with("AAPL")
    clear.assert_not_called()
    yf.assert_not_awaited()


def test_invalidate_quote_cache_removes_only_that_symbol() -> None:
    from services.market_data_service import _quote_cache, invalidate_quote_cache

    _quote_cache.clear()
    _quote_cache["AAPL"] = ({"c": 1.0}, 9e18)
    _quote_cache["MSFT"] = ({"c": 2.0}, 9e18)
    try:
        invalidate_quote_cache("aapl")
        assert "AAPL" not in _quote_cache
        assert "MSFT" in _quote_cache
        invalidate_quote_cache("$msft")
        assert "MSFT" not in _quote_cache
    finally:
        _quote_cache.clear()


# ---------------------------------------------------------------------------
# 無即時價 → 降級 item
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_live_price_yields_degraded_item() -> None:
    items, truncated, mocks = await _build([_order()], quote=(0.0, 0.0, None))

    assert truncated is False
    assert len(items) == 1
    item = items[0]
    assert item["live_price"] is None
    assert item["alignment_available"] is False
    assert item["action"] == "NO_LIVE_PRICE"
    assert item["suggested_price"] == item["current_price"] == 100.0
    assert item["suggested_qty"] == item["original_qty"] == 10
    assert item["suggestion_changed"] is False
    assert item["proximity_pct"] is None
    assert item["radar_status"] == "現價缺失"
    assert item["system_status_flag"] == "DATA DEGRADED"
    assert item["gain_loss_pct"] is None
    assert item["wall_dist_pct"] is None
    mocks["decide"].assert_not_awaited()


@pytest.mark.asyncio
async def test_prev_close_is_quote_pc_passed_to_engine() -> None:
    items, _, mocks = await _build([_order()], quote=(101.0, 102.0, 99.5))
    kwargs = mocks["decide"].await_args.kwargs
    assert kwargs["prev_close"] == 99.5
    assert kwargs["live_price"] == 102.0
    assert items[0]["prev_close"] == 99.5
    assert items[0]["proximity_pct"] == pytest.approx(abs(102.0 - 100.0) / 102.0 * 100)


# ---------------------------------------------------------------------------
# Put Wall vs Max Pain
# ---------------------------------------------------------------------------
def _patch_market_inputs(
    *,
    iv_metrics: Any,
    gex: Any,
    skew: Any = None,
    max_pain: Any = None,
    iv_exc: Exception | None = None,
) -> ExitStack:
    stack = ExitStack()
    iv_mock = (
        AsyncMock(side_effect=iv_exc)
        if iv_exc is not None
        else AsyncMock(return_value=iv_metrics)
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=iv_mock,
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(
                return_value=skew
                if skew is not None
                else {"skew": 1.0, "skew_percentile": 55.0, "state": "平穩"}
            ),
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new=AsyncMock(return_value=max_pain or {"max_pain": 98.0}),
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
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new=AsyncMock(return_value=None),
        )
    )
    gex_mock = (
        AsyncMock(side_effect=gex)
        if isinstance(gex, Exception)
        else AsyncMock(return_value=gex)
    )
    stack.enter_context(
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new=gex_mock,
        )
    )
    return stack


@pytest.mark.asyncio
async def test_put_wall_comes_from_gex_not_max_pain_and_stale_propagates() -> None:
    with _patch_market_inputs(
        iv_metrics=_iv(),
        gex={"put_wall": 90.0, "_is_stale_cache": True},
        max_pain={"max_pain": 98.0},
    ):
        inputs = await svc._fetch_alignment_market_inputs("AAPL", set())
    assert inputs["put_wall"] == 90.0
    assert inputs["put_wall_is_stale"] is True
    assert inputs["max_pain_price"] == 98.0

    items, _, _ = await _build([_order()], quote=(101.0, 101.0, 99.5), inputs=inputs)
    item = items[0]
    assert item["put_wall"] == 90.0
    assert item["put_wall_is_stale"] is True
    assert item["max_pain_price"] == 98.0
    assert item["wall_dist_pct"] == pytest.approx((101.0 - 90.0) / 101.0 * 100)
    assert item["wall_status"] == "上方緩衝"


@pytest.mark.asyncio
async def test_gex_failure_leaves_put_wall_unknown_but_command_succeeds() -> None:
    with _patch_market_inputs(iv_metrics=_iv(), gex=RuntimeError("edge down")):
        inputs = await svc._fetch_alignment_market_inputs("AAPL", set())
    assert inputs["put_wall"] is None
    assert inputs["put_wall_is_stale"] is False

    items, _, _ = await _build([_order()], inputs=inputs)
    assert items[0]["put_wall"] is None
    assert items[0]["wall_status"] == "待確認"
    assert items[0]["wall_dist_pct"] is None


@pytest.mark.asyncio
async def test_put_wall_zero_is_unknown() -> None:
    with _patch_market_inputs(iv_metrics=_iv(), gex={"put_wall": 0.0}):
        inputs = await svc._fetch_alignment_market_inputs("AAPL", set())
    assert inputs["put_wall"] is None


@pytest.mark.asyncio
async def test_broken_wall_below_live_price_is_marked_breached() -> None:
    items, _, _ = await _build(
        [_order()],
        quote=(101.0, 101.0, 99.5),
        inputs=_inputs(iv_metrics=_iv(), put_wall=105.0),
    )
    assert items[0]["wall_status"] == "跌破支撐"


# ---------------------------------------------------------------------------
# hist_iv 基準
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "iv_kw", "expected_hist"),
    [
        ("live", {}, 0.28),
        ("hv_proxy", {"iv_source": "HV_PROXY"}, 0.30),
        ("stored", {"iv_source": "STORED_IV"}, 0.30),
        ("event_loaded", {"event_loading_applied": True}, 0.30),
        ("missing_baseline", {"iv_hist_mean_20d": None}, 0.30),
        ("zero_baseline", {"iv_hist_mean_20d": 0.0}, 0.30),
    ],
)
@pytest.mark.asyncio
async def test_hist_iv_baseline_rule_in_alignment_inputs(
    label: str, iv_kw: dict[str, Any], expected_hist: float
) -> None:
    with _patch_market_inputs(iv_metrics=_iv(**iv_kw), gex={"put_wall": 90.0}):
        inputs = await svc._fetch_alignment_market_inputs("AAPL", set())
    kwargs = inputs["decision_kwargs"]
    assert kwargs["iv"] == pytest.approx(0.30)
    assert kwargs["hist_iv"] == pytest.approx(expected_hist), label


@pytest.mark.parametrize(
    ("iv_kw", "expected_hist"),
    [
        ({}, 0.28),
        ({"iv_source": "HV_PROXY"}, 0.30),
        ({"iv_source": "STORED_IV"}, 0.30),
        ({"event_loading_applied": True}, 0.30),
        ({"iv_hist_mean_20d": None}, 0.30),
    ],
)
@pytest.mark.asyncio
async def test_hist_iv_baseline_rule_in_order_creation_path(
    iv_kw: dict[str, Any], expected_hist: float
) -> None:
    calc = AsyncMock(return_value=(100.0, 10, []))
    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0, "pc": 99.0}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=AsyncMock(return_value=_iv(**iv_kw)),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(return_value={"skew_percentile": 50.0}),
        ),
        patch("services.telemetry_pricing_engine.calculate_telemetry_price", new=calc),
    ):
        await svc.resolve_telemetry_pricing("AAPL", "LIMIT", 10)
    call = calc.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["iv"] == pytest.approx(0.30)
    assert kwargs["hist_iv"] == pytest.approx(expected_hist)


@pytest.mark.asyncio
async def test_order_creation_path_iv_unavailable_uses_neutral_hist_iv() -> None:
    calc = AsyncMock(return_value=(100.0, 10, []))
    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0, "pc": 99.0}),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new=AsyncMock(return_value=None),
        ),
        patch("services.telemetry_pricing_engine.calculate_telemetry_price", new=calc),
    ):
        await svc.resolve_telemetry_pricing("AAPL", "LIMIT", 10)
    call = calc.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["iv"] == 0.35
    assert kwargs["hist_iv"] == 0.35


# ---------------------------------------------------------------------------
# 單一輸入失敗不拖垮整個指令
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_input_failure_still_yields_item() -> None:
    with _patch_market_inputs(
        iv_metrics=None,
        gex={"put_wall": 90.0},
        iv_exc=RuntimeError("iv boom"),
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new=AsyncMock(side_effect=RuntimeError("skew boom")),
    ):
        inputs = await svc._fetch_alignment_market_inputs("AAPL", set())
    assert inputs["iv_metrics"] is None
    assert inputs["skew_metrics"] is None
    assert inputs["decision_kwargs"]["iv_rank"] is None
    assert inputs["decision_kwargs"]["skew_percentile_pct"] == 50.0
    assert inputs["put_wall"] == 90.0

    items, _, _ = await _build([_order()], inputs=inputs)
    assert len(items) == 1
    assert items[0]["iv_status"] == "UNAVAILABLE"
    assert items[0]["iv_val"] is None
    assert items[0]["skew_val"] is None


# ---------------------------------------------------------------------------
# alert text
# ---------------------------------------------------------------------------
def test_alert_text_without_reasons_makes_no_clamp_claim() -> None:
    text = _build_alert_text(
        symbol="aapl", reason_text="", suggested_price=100.5, sizing_multiplier=1.0
    )
    assert "截斷" not in text
    assert "修正提價" not in text
    assert "$100.50" in text

    with_reason = _build_alert_text(
        symbol="aapl",
        reason_text="Max Pain 截斷",
        suggested_price=100.5,
        sizing_multiplier=1.0,
    )
    assert "截斷/修正" in with_reason


# ---------------------------------------------------------------------------
# 持倉／損益／標籤
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_holding_pnl_sign_and_status() -> None:
    items, _, _ = await _build(
        [_order()],
        quote=(100.0, 100.0, 99.0),
        holding_map={"AAPL": {"quantity": -50.0, "avg_cost": 110.0}},
    )
    item = items[0]
    assert item["holding_status"] == "空頭持倉"
    assert item["holding_shares"] == -50
    assert item["gain_loss_pct"] == pytest.approx((110.0 - 100.0) / 110.0 * 100.0)


@pytest.mark.asyncio
async def test_long_holding_pnl_and_missing_cost() -> None:
    items, _, _ = await _build(
        [_order(order_id=1), _order(order_id=2, symbol="MSFT")],
        quote=(100.0, 100.0, 99.0),
        holding_map={
            "AAPL": {"quantity": 5.0, "avg_cost": 80.0},
            "MSFT": {"quantity": 5.0, "avg_cost": 0.0},
        },
    )
    assert items[0]["holding_status"] == "持倉中"
    assert items[0]["gain_loss_pct"] == pytest.approx(25.0)
    assert items[1]["avg_cost"] is None
    assert items[1]["gain_loss_pct"] is None


@pytest.mark.asyncio
async def test_flat_holding_has_no_pnl() -> None:
    items, _, _ = await _build(
        [_order()], holding_map={"AAPL": {"quantity": 0.0, "avg_cost": 80.0}}
    )
    assert items[0]["holding_status"] == "空倉待命"
    assert items[0]["gain_loss_pct"] is None


@pytest.mark.parametrize(
    ("holding_type", "holding_map", "label"),
    [
        ("PURE_STOCK_100X", {"AAPL": {"quantity": 1.0}}, "1.00x 純現貨 (0 槓桿)"),
        ("COMPLEX_OPTIONS", {"AAPL": {"quantity": 1.0}}, "現貨＋選擇權"),
        ("LEVERAGED_MARGIN", {"AAPL": {"quantity": 1.0}}, "保證金槓桿 (推定)"),
        ("LEVERAGED_MARGIN", {}, "無持倉紀錄"),
    ],
)
@pytest.mark.asyncio
async def test_holding_type_labels(
    holding_type: str, holding_map: dict[str, dict], label: str
) -> None:
    items, _, _ = await _build(
        [_order()], holding_type=holding_type, holding_map=holding_map
    )
    assert items[0]["holding_type_label"] == label


# ---------------------------------------------------------------------------
# IV 狀態
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_iv_status_variants() -> None:
    cases = [
        (_iv(iv_source="UNAVAILABLE", current_iv=None), "UNAVAILABLE"),
        (None, "UNAVAILABLE"),
        (
            _iv(iv_rank=None, iv_history_count=12, iv_history_required=60),
            "樣本累積 12/60",
        ),
        (_iv(iv_status="High", iv_rank=85.0), "High"),
    ]
    for metrics, expected in cases:
        items, _, _ = await _build([_order()], inputs=_inputs(iv_metrics=metrics))
        assert items[0]["iv_status"] == expected

    items, _, _ = await _build(
        [_order()], inputs=_inputs(iv_metrics=_iv(event_loading_applied=True))
    )
    assert items[0]["iv_event_loading_applied"] is True
    assert items[0]["iv_source"] == "LIVE_IV"


# ---------------------------------------------------------------------------
# 上限與 memo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_item_cap_stops_fetching_before_extra_orders() -> None:
    orders = [_order(order_id=i, symbol=f"S{i}") for i in range(1, 26)]
    items, truncated, mocks = await _build(orders)
    assert svc.MAX_ALIGNMENT_ITEMS == 20
    assert len(items) == 20
    assert truncated is True
    assert mocks["quote"].await_count == 20
    assert mocks["inputs"].await_count == 20
    assert mocks["decide"].await_count == 20


@pytest.mark.asyncio
async def test_same_symbol_fetches_quote_and_inputs_once() -> None:
    items, truncated, mocks = await _build(
        [_order(order_id=1), _order(order_id=2, limit_price=99.0)]
    )
    assert len(items) == 2
    assert truncated is False
    assert mocks["quote"].await_count == 1
    assert mocks["inputs"].await_count == 1
    assert mocks["decide"].await_count == 2


@pytest.mark.asyncio
async def test_suppressed_decision_stays_in_snapshot() -> None:
    items, _, _ = await _build(
        [_order()],
        decision=_decision(
            action="SUPPRESSED",
            suggested_price=100.0,
            system_status_flag="FORTRESS RE-LOCKED",
            system_instruction_directive="x",
        ),
    )
    assert len(items) == 1
    assert items[0]["action"] == "SUPPRESSED"
    assert items[0]["radar_status"] == "FORTRESS RE-LOCKED"
    assert items[0]["suggestion_changed"] is False


# ---------------------------------------------------------------------------
# 引擎 emit 模式
# ---------------------------------------------------------------------------
def _buy_kwargs(order_id: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": 77,
        "order_id": order_id,
        "symbol": "SPY",
        "current_order_price": 470.0,
        "spot_price": 486.51,
        "original_qty": 10,
        "iv": 0.10,
        "hist_iv": 0.10,
        "iv_rank": 0.50,
        "max_pain_price": 475.0,
        "prev_max_pain": 475.0,
        "skew_percentile_pct": 98.0,
        "put_call_ratio": 1.0,
        "order_side": "BUY",
    }
    base.update(over)
    return base


def _add_spy(side: str = "BUY") -> int:
    return add_active_order(
        user_id=77,
        symbol="SPY",
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        side=side,
        limit_price=470.0,
    )


@pytest.mark.asyncio
async def test_engine_emit_mode_keeps_iv_fuse_and_clamp_orders(db_conn: Any) -> None:
    oid = _add_spy()

    fuse = await generate_alignment_decision(
        **_buy_kwargs(oid, iv_rank=1.0), emit_suppressed_decision=True
    )
    assert fuse is not None
    assert fuse.action == "SUPPRESSED"
    assert fuse.system_status_flag == "FORTRESS RE-LOCKED"
    assert fuse.suggested_price == 470.0 and fuse.suggested_qty == 10
    assert "100.0%" in fuse.system_instruction_directive

    fuse_none = await generate_alignment_decision(
        **_buy_kwargs(oid, iv_rank=None), emit_suppressed_decision=True
    )
    assert fuse_none is not None
    assert "IV Rank 樣本累積中" in fuse_none.system_instruction_directive

    clamp = await generate_alignment_decision(
        **_buy_kwargs(oid, max_pain_price=465.0, prev_max_pain=465.0),
        emit_suppressed_decision=True,
    )
    assert clamp is not None
    assert clamp.action == "NO_ALIGNMENT_NEEDED"
    assert clamp.suggested_price == 470.0 and clamp.suggested_qty == 10
    assert (
        clamp.system_instruction_directive
        == "提價建議被 Max Pain／預期波動邊界截斷，維持原價原量。"
    )


@pytest.mark.asyncio
async def test_engine_sell_is_never_adjusted(db_conn: Any) -> None:
    oid = _add_spy(side="SELL")

    emitted = await generate_alignment_decision(
        **_buy_kwargs(oid, order_side="SELL"), emit_suppressed_decision=True
    )
    assert emitted is not None
    assert emitted.action == "SUPPRESSED"
    assert emitted.system_status_flag == "BUY-ONLY ENGINE"
    assert (
        emitted.system_instruction_directive
        == "對齊引擎僅支援買入委託，賣出委託維持原價原量。"
    )
    assert emitted.suggested_price == 470.0 and emitted.suggested_qty == 10

    assert (
        await generate_alignment_decision(**_buy_kwargs(oid, order_side="SELL")) is None
    )


@pytest.mark.asyncio
async def test_engine_non_emit_mode_still_returns_none(db_conn: Any) -> None:
    oid = _add_spy()
    assert await generate_alignment_decision(**_buy_kwargs(oid, iv_rank=1.0)) is None
    assert (
        await generate_alignment_decision(
            **_buy_kwargs(oid, max_pain_price=465.0, prev_max_pain=465.0)
        )
        is None
    )


# ---------------------------------------------------------------------------
# 一鍵套用
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_untouched_without_suggestion_and_no_market_fetch() -> None:
    fetch = AsyncMock()
    with (
        patch(f"{SVC}.fetch_cache_and_live_price", new=fetch),
        patch(f"{SVC}._fetch_alignment_market_inputs", new=fetch),
        patch("database.orders.update_active_order_price") as upd,
    ):
        updated, details = await svc.apply_telemetry_to_orders(
            user_id=1,
            orders=[_order(order_id=5)],
            suggestions={},
            holding_type="LEVERAGED_MARGIN",
            holding_map={},
        )
    assert (updated, details) == (0, [])
    fetch.assert_not_awaited()
    upd.assert_not_called()


@pytest.mark.asyncio
async def test_apply_stop_limit_shifts_stop_by_same_delta(db_conn: Any) -> None:
    uid = 91
    oid = add_active_order(
        user_id=uid,
        symbol="AAPL",
        quantity=20,
        order_type="STOP_LIMIT",
        validity="GTC_90",
        limit_price=100.0,
        stop_price=98.0,
    )
    orders = get_user_active_orders(uid)
    updated, details = await svc.apply_telemetry_to_orders(
        user_id=uid,
        orders=orders,
        suggestions={oid: (101.0, 15)},
        holding_type="LEVERAGED_MARGIN",
        holding_map={},
    )
    assert updated == 1
    row = get_user_active_orders(uid)[0]
    assert row["limit_price"] == 101.0
    assert row["stop_price"] == 99.0
    assert row["quantity"] == 15.0

    assert details == [
        {
            "order_id": oid,
            "symbol": "AAPL",
            "order_type": "STOP_LIMIT",
            "old_price": 100.0,
            "new_price": 101.0,
            "old_qty": 20,
            "new_qty": 15,
            "old_stop": 98.0,
            "new_stop": 99.0,
            "is_size_down": True,
        }
    ]


@pytest.mark.asyncio
async def test_apply_limit_and_stop_orders_unchanged_behaviour(db_conn: Any) -> None:
    uid = 92
    lim = add_active_order(
        user_id=uid,
        symbol="AAPL",
        quantity=10,
        order_type="LIMIT",
        validity="GTC_90",
        limit_price=100.0,
    )
    stp = add_active_order(
        user_id=uid,
        symbol="MSFT",
        quantity=10,
        order_type="STOP",
        validity="GTC_90",
        side="SELL",
        stop_price=50.0,
    )
    trail = add_active_order(
        user_id=uid,
        symbol="TSLA",
        quantity=10,
        order_type="TRAILING_STOP_PCT",
        validity="GTC_90",
        side="SELL",
        trailing_value=5.0,
    )
    updated, details = await svc.apply_telemetry_to_orders(
        user_id=uid,
        orders=get_user_active_orders(uid),
        suggestions={lim: (99.0, 10), stp: (51.0, 10), trail: (1.0, 5)},
        holding_type="LEVERAGED_MARGIN",
        holding_map={},
    )
    assert updated == 2
    by_id = {r["id"]: r for r in get_user_active_orders(uid)}
    assert by_id[lim]["limit_price"] == 99.0
    assert by_id[stp]["stop_price"] == 51.0
    assert by_id[trail]["trailing_value"] == 5.0
    assert by_id[trail]["quantity"] == 10.0

    d = {x["order_id"]: x for x in details}
    assert d[lim]["old_stop"] is None and d[lim]["new_stop"] is None
    assert d[stp]["old_price"] == 50.0 and d[stp]["new_price"] == 51.0
    assert d[lim]["is_size_down"] is False
    for entry in details:
        assert set(entry) == {
            "order_id", "symbol", "order_type", "old_price", "new_price",
            "old_qty", "new_qty", "old_stop", "new_stop", "is_size_down",
        }  # fmt: skip


# ---------------------------------------------------------------------------
# 套用結果 Embed
# ---------------------------------------------------------------------------
def test_apply_result_embed_renders_stop_limit_and_size_down() -> None:
    from cogs.embed_builder import create_telemetry_apply_result_embed

    details = [
        {
            "order_id": 7,
            "symbol": "AAPL",
            "order_type": "STOP_LIMIT",
            "old_price": 100.0,
            "new_price": 101.0,
            "old_qty": 20,
            "new_qty": 15,
            "old_stop": 98.0,
            "new_stop": 99.0,
            "is_size_down": True,
        }
    ]
    embeds = create_telemetry_apply_result_embed(1, details)
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed.to_dict().get("color") == 0x8E44AD
    assert len(embed.fields) == 1
    value = embed.fields[0].value
    assert value is not None
    assert value.startswith("```ansi") and value.rstrip().endswith("```")
    assert "├─" in value and "└─" in value
    assert "$98.00" in value and "$99.00" in value
    assert "$100.00" in value and "$101.00" in value
    assert "+1.00%" in value
    assert "[⚠️ 尾端風險防禦]" in value
    assert all(len(f.value or "") <= 1024 for f in embed.fields)
    # 品牌前綴由 NexusEmbed.set_footer 統一加上，builder 只傳模組描述
    assert (embed.footer.text or "").endswith("遙測對齊套用結果")


def test_apply_result_embed_success_colour_pages_and_missing_values() -> None:
    from cogs.embed_builder import create_telemetry_apply_result_embed

    details: list[dict[str, Any]] = [
        {
            "order_id": i,
            "symbol": "AAPL",
            "order_type": "LIMIT",
            "old_price": 100.0,
            "new_price": 99.0,
            "old_qty": 10,
            "new_qty": 10,
            "old_stop": None,
            "new_stop": None,
            "is_size_down": False,
        }
        for i in range(12)
    ]
    details[0]["new_price"] = None
    embeds = create_telemetry_apply_result_embed(12, details)
    assert len(embeds) == 2
    assert all(e.to_dict().get("color") == 0x2ECC71 for e in embeds)
    assert "(第 1/2 頁)" in (embeds[0].title or "")
    assert "(第 2/2 頁)" in (embeds[1].title or "")
    assert len(embeds[0].fields) == 10 and len(embeds[1].fields) == 2
    first = embeds[0].fields[0].value or ""
    assert "--" in first
    for e in embeds:
        for f in e.fields:
            assert len(f.value or "") <= 1024
