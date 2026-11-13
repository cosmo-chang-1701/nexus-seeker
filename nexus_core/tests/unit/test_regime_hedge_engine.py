"""兩級體制避險引擎 (market_analysis.dynamic_rollover.regime_hedge) 單元測試。"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.index_microstructure import MarketRegimeInputs
from market_analysis.dynamic_rollover.regime_hedge_timing import MacroFreezeStatus

_MOD = "market_analysis.dynamic_rollover.regime_hedge"
_REGIME = "market_analysis.index_microstructure.get_market_regime"
_INPUTS = "market_analysis.index_microstructure.get_market_regime_inputs"
_CTX = "market_analysis.dynamic_rollover.get_full_user_context"

_ORDERS = "database.orders.get_user_active_orders"

_PRICES = {"PSQ": 30.0, "SQQQ": 10.0, "SOXS": 20.0}


def _asset(symbol: str, value: float, **kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "symbol": symbol,
        "asset_class": "SATELLITE",
        "quantity": 10.0,
        "current_value": value,
        "spot_price": 100.0,
        "put_wall": 0.0,
        "instrument_type": "SPOT",
    }
    base.update(kw)
    return base


def _orders_kw(orders: Any) -> Dict[str, Any]:
    if isinstance(orders, Exception):
        return {"side_effect": orders}
    return {"return_value": orders if orders is not None else []}


def _engine() -> DynamicRolloverEngine:
    eng = DynamicRolloverEngine()

    async def _price(sym: str) -> float:
        return _PRICES.get(sym, 0.0)

    setattr(eng, "_resolve_target_reference_price", _price)
    return eng


def _ctx(cash: float = 10000.0, load_failed: bool = False) -> MagicMock:
    return MagicMock(cash_reserve=cash, load_failed=load_failed)


_UNSET: Any = object()


def _inputs(
    vix: Optional[float] = 25.0,
    vts: Optional[float] = 1.05,
    spot: Optional[float] = 98.0,
    flip: Optional[float] = 100.0,
    regime: str = "SHORT_GAMMA_CRITICAL",
) -> MarketRegimeInputs:
    return MarketRegimeInputs(regime, vix, vts, spot, flip, 0.0)


async def _run(
    assets: List[Dict[str, Any]],
    regime: str,
    *,
    cash: float = 10000.0,
    load_failed: bool = False,
    no_edge: Any = False,
    frozen: bool = False,
    preliminary: bool = False,
    flagged: Any = None,
    freeze_error: bool = False,
    orders: Any = None,
    inputs: Any = _UNSET,
) -> List[Any]:
    if inputs is _UNSET:
        inputs = _inputs()
    inputs_mock = (
        AsyncMock(side_effect=inputs)
        if isinstance(inputs, Exception)
        else AsyncMock(return_value=inputs)
    )
    eng = _engine()
    if callable(no_edge):
        edge = AsyncMock(side_effect=lambda **kw: (no_edge(kw["symbol"]), False, False))
    else:
        edge = AsyncMock(return_value=(no_edge, False, False))
    setattr(eng, "_evaluate_structural_no_edge", edge)
    status = MacroFreezeStatus(
        frozen=frozen,
        calendar_known=True,
        event_name="FOMC 利率決策",
        event_time_utc=None,
    )
    if frozen:
        status = MacroFreezeStatus(frozen=True, calendar_known=False)
    with (
        patch(_REGIME, new=AsyncMock(return_value=regime)),
        patch(_INPUTS, new=inputs_mock),
        patch(_CTX, return_value=_ctx(cash, load_failed)),
        patch(f"{_MOD}.load_earnings_dates", new=AsyncMock(return_value={})),
        patch(
            f"{_MOD}.load_macro_freeze_status",
            new=AsyncMock(
                side_effect=RuntimeError("x") if freeze_error else None,
                return_value=status,
            ),
        ),
        patch(f"{_MOD}.is_preliminary_window", return_value=preliminary),
        patch(_ORDERS, **_orders_kw(orders)),
    ):
        return await eng.evaluate_regime_hedge(1, assets, flagged)


async def test_unknown_regime_returns_empty() -> None:
    assert await _run([_asset("NVDA", 50000.0)], "UNKNOWN") == []


async def test_load_failed_returns_empty() -> None:
    out = await _run(
        [_asset("NVDA", 50000.0, put_wall=120.0)], "NORMAL", load_failed=True
    )
    assert out == []


async def test_normal_without_putwall_break_returns_empty() -> None:
    out = await _run([_asset("NVDA", 50000.0, put_wall=90.0)], "NORMAL")
    assert out == []


async def test_normal_putwall_break_emits_tier1_psq_only() -> None:
    out = await _run([_asset("NVDA", 50000.0, put_wall=120.0)], "NORMAL")
    assert len(out) == 1
    ins = out[0]
    assert ins["symbol"] == "PORTFOLIO"
    assert ins["action"] == "HEDGE_TIER1"
    assert ins["scenario"] == "REGIME_HEDGE"
    assert ins["hedge_tier"] == 1
    assert ins["market_regime"] == "NORMAL"
    assert ins["is_manual_override_required"] is True
    assert [leg.symbol for leg in ins["hedge_plan"].legs] == ["PSQ"]
    assert ins["target_core"] == "PSQ"


async def test_cash_equivalents_and_inverse_not_counted_as_broken() -> None:
    # 各自獨立：排除邏輯失效時會產出 PSQ 腿而使測試失敗
    for sym, cls in (("BOXX", "CORE"), ("PSQ", "SATELLITE"), ("SQQQ", "SATELLITE")):
        assets = [_asset(sym, 1000.0, put_wall=120.0, asset_class=cls)]
        assert await _run(assets, "NORMAL", cash=30000.0) == [], sym


async def test_crisis_no_edge_emits_tier2_with_liquidations() -> None:
    assets = [
        _asset("NVDA", 30000.0),
        _asset("AAPL", 30000.0),
        _asset("AMD", 20000.0),
        _asset("VOO", 20000.0, asset_class="CORE"),
    ]
    out = await _run(
        assets,
        "SHORT_GAMMA_CRITICAL",
        no_edge=lambda sym: sym in ("NVDA", "AAPL"),
    )
    assert len(out) == 1
    ins = out[0]
    assert ins["action"] == "HEDGE_TIER2"
    assert ins["hedge_tier"] == 2
    plan = ins["hedge_plan"]
    assert set(plan.liquidate_symbols) == {"NVDA", "AAPL"}
    assert {leg.symbol for leg in plan.legs} == {"SOXS", "SQQQ"}
    assert ins["target_core"] == "SOXS/SQQQ"


async def test_crisis_macro_freeze_drops_minus3x_legs() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(assets, "SYSTEMIC_LIQUIDITY_CRISIS", no_edge=True, frozen=True)
    assert len(out) == 1
    plan = out[0]["hedge_plan"]
    assert plan.legs == ()
    assert plan.minus3x_frozen_reason
    assert plan.liquidate_symbols
    assert out[0]["target_core"] == "BOXX"


async def test_options_positions_ignored() -> None:
    opt = _asset(
        "NVDA",
        50000.0,
        put_wall=120.0,
        instrument_type="OPTION",
    )
    assert await _run([opt], "NORMAL", cash=1000.0) == []


async def test_is_preliminary_passed_through() -> None:
    assets = [_asset("NVDA", 50000.0, put_wall=120.0)]
    on = await _run(assets, "NORMAL", preliminary=True)
    off = await _run(assets, "NORMAL", preliminary=False)
    assert on[0]["is_preliminary"] is True
    assert off[0]["is_preliminary"] is False


async def test_macro_freeze_load_failure_fails_closed() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(
        assets,
        "SHORT_GAMMA_CRITICAL",
        no_edge=True,
        freeze_error=True,
    )
    plan = out[0]["hedge_plan"]
    assert plan.legs == ()
    assert plan.minus3x_frozen_reason
    assert plan.liquidate_symbols


async def test_fallback_reference_price_treated_as_missing() -> None:
    eng = _engine()

    async def _fallback(sym: str) -> float:
        return 500.0

    setattr(eng, "_resolve_target_reference_price", _fallback)
    with (
        patch(_REGIME, new=AsyncMock(return_value="NORMAL")),
        patch(_CTX, return_value=_ctx()),
        patch(f"{_MOD}.is_preliminary_window", return_value=False),
    ):
        out = await eng.evaluate_regime_hedge(
            1, [_asset("NVDA", 50000.0, put_wall=120.0)], None
        )
    plan = out[0]["hedge_plan"]
    assert plan.legs[0].shares == 0
    assert any("股數待確認" in n for n in plan.notes)


async def test_no_edge_exception_skips_symbol() -> None:
    def _boom(sym: str) -> bool:
        if sym == "NVDA":
            raise RuntimeError("x")
        return True

    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(assets, "SHORT_GAMMA_CRITICAL", no_edge=_boom)
    assert out[0]["hedge_plan"].liquidate_symbols == ("AAPL",)


async def test_core_defense_etf_never_liquidated() -> None:
    assets = [_asset("VOO", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(assets, "SHORT_GAMMA_CRITICAL", no_edge=True)
    assert "VOO" not in out[0]["hedge_plan"].liquidate_symbols
    assert "AAPL" in out[0]["hedge_plan"].liquidate_symbols


def _gtc_buy(amount: float) -> Dict[str, Any]:
    return {"validity": "GTC", "side": "BUY", "limit_price": amount, "quantity": 1.0}


async def test_tier1_gtc_commitment_reduces_funding() -> None:
    assets = [_asset("NVDA", 50000.0, put_wall=120.0)]
    base = await _run(assets, "NORMAL", cash=30000.0)
    assert base[0]["hedge_plan"].sell_for_funding_value == 0.0
    assert not any("GTC" in n for n in base[0]["hedge_plan"].notes)
    out = await _run(assets, "NORMAL", cash=30000.0, orders=[_gtc_buy(30000.0)])
    plan = out[0]["hedge_plan"]
    assert plan.sell_for_funding_value > 0
    assert plan.boxx_target_value >= 30000 - 1e-6
    assert any("已預留 GTC 買單承諾 $30,000" in n for n in plan.notes)


async def test_tier2_gtc_commitment_reduces_funding() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    base = await _run(
        assets, "SHORT_GAMMA_CRITICAL", cash=20000.0, no_edge=lambda s: s == "NVDA"
    )
    out = await _run(
        assets,
        "SHORT_GAMMA_CRITICAL",
        cash=20000.0,
        no_edge=lambda s: s == "NVDA",
        orders=[_gtc_buy(20000.0)],
    )
    b, plan = base[0]["hedge_plan"], out[0]["hedge_plan"]
    assert plan.boxx_target_value >= 20000 - 1e-6
    legs_b = sum(leg.target_value for leg in b.legs)
    legs_p = sum(leg.target_value for leg in plan.legs)
    assert b.boxx_target_value + legs_b - b.sell_for_funding_value == pytest.approx(
        20000 + b.liquidate_value
    )
    assert plan.boxx_target_value - 20000 + legs_p - plan.sell_for_funding_value == (
        pytest.approx(plan.liquidate_value)
    )
    assert any("已預留 GTC 買單承諾 $20,000" in n for n in plan.notes)
    assert not any("GTC" in n for n in b.notes)


async def test_gtc_commitment_ignores_non_gtc_and_sell_orders() -> None:
    assets = [_asset("NVDA", 50000.0, put_wall=120.0)]
    orders = [
        {"validity": "DAY", "side": "BUY", "limit_price": 30000.0, "quantity": 1.0},
        {"validity": "GTC", "side": "SELL", "limit_price": 30000.0, "quantity": 1.0},
    ]
    out = await _run(assets, "NORMAL", cash=30000.0, orders=orders)
    plan = out[0]["hedge_plan"]
    assert plan.sell_for_funding_value == 0.0
    assert not any("GTC" in n for n in plan.notes)


async def test_gtc_commitment_orders_read_failure_falls_back() -> None:
    assets = [_asset("NVDA", 50000.0, put_wall=120.0)]
    out = await _run(assets, "NORMAL", cash=30000.0, orders=RuntimeError("db"))
    assert len(out) == 1
    assert not any("GTC" in n for n in out[0]["hedge_plan"].notes)


_REDLINE_PREFIX = "−3x 啟動紅線未達："


async def test_redline_not_met_freezes_minus3x_keeps_liquidations() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(
        assets,
        "SHORT_GAMMA_CRITICAL",
        no_edge=True,
        inputs=_inputs(vix=21.0, vts=1.05, spot=98.0, flip=100.0),
    )
    assert len(out) == 1
    plan = out[0]["hedge_plan"]
    assert not [leg for leg in plan.legs if leg.symbol in ("SQQQ", "SOXS")]
    assert set(plan.liquidate_symbols) == {"NVDA", "AAPL"}
    assert plan.minus3x_frozen_reason.startswith(_REDLINE_PREFIX)
    assert plan.boxx_target_value > 0


async def test_redline_met_produces_minus3x_legs() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(assets, "SHORT_GAMMA_CRITICAL", no_edge=True)
    plan = out[0]["hedge_plan"]
    assert {leg.symbol for leg in plan.legs} == {"SOXS", "SQQQ"}
    assert plan.minus3x_frozen_reason is None


@pytest.mark.parametrize("bad", [None, RuntimeError("boom")])
async def test_redline_inputs_unavailable_fails_closed(bad: Any) -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(assets, "SHORT_GAMMA_CRITICAL", no_edge=True, inputs=bad)
    plan = out[0]["hedge_plan"]
    assert not plan.legs
    assert "啟動紅線資料不足" in plan.minus3x_frozen_reason


async def test_redline_and_macro_freeze_reasons_joined() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(
        assets,
        "SHORT_GAMMA_CRITICAL",
        no_edge=True,
        frozen=True,
        inputs=_inputs(vix=20.0),
    )
    reason = out[0]["hedge_plan"].minus3x_frozen_reason
    assert "；" in reason
    assert _REDLINE_PREFIX in reason
    assert not out[0]["hedge_plan"].legs


async def test_redline_liquidity_crisis_with_low_vix_frozen() -> None:
    assets = [_asset("NVDA", 30000.0), _asset("AAPL", 30000.0)]
    out = await _run(
        assets,
        "SYSTEMIC_LIQUIDITY_CRISIS",
        no_edge=True,
        inputs=_inputs(vix=18.0, regime="SYSTEMIC_LIQUIDITY_CRISIS"),
    )
    plan = out[0]["hedge_plan"]
    assert not [leg for leg in plan.legs if leg.symbol in ("SQQQ", "SOXS")]
    assert plan.minus3x_frozen_reason.startswith(_REDLINE_PREFIX)
