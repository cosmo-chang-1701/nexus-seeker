"""兩級體制避險引擎 (market_analysis.dynamic_rollover.regime_hedge) 單元測試。"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.regime_hedge_timing import MacroFreezeStatus

_MOD = "market_analysis.dynamic_rollover.regime_hedge"
_REGIME = "market_analysis.index_microstructure.get_market_regime"
_CTX = "market_analysis.dynamic_rollover.get_full_user_context"

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


def _engine() -> DynamicRolloverEngine:
    eng = DynamicRolloverEngine()

    async def _price(sym: str) -> float:
        return _PRICES.get(sym, 0.0)

    setattr(eng, "_resolve_target_reference_price", _price)
    return eng


def _ctx(cash: float = 10000.0, load_failed: bool = False) -> MagicMock:
    return MagicMock(cash_reserve=cash, load_failed=load_failed)


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
) -> List[Any]:
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
