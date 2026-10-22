"""賣 Put 倉位：現金擔保上限、無負 β 對沖折抵、/x 帶號參考 Delta 與倉位用 IV。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.unified_terminal.symbol_deep_dive import select_sizing_iv
from market_analysis.risk_engine import (
    build_macro_context,
    compute_available_collateral,
    is_cash_secured_put_strategy,
    is_short_put_strategy,
    load_available_collateral,
    optimize_position_risk,
)
from models.quant import IVMetrics, OptimizationResult

_MAX_WARNING_LEN = 20


def _base(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        current_delta=0.0,
        unit_weighted_delta=-3.0,
        user_capital=1_000_000.0,
        spy_price=500.0,
        stock_iv=0.30,
        strategy="STO_PUT",
        macro_data=build_macro_context({"vix": 20.0, "oil": 70.0, "vix_change": 0.0}),
        risk_limit=15.0,
        vix_spot=20.0,
        pcr=0.9,
        skew=0.0,
    )
    kw.update(over)
    return kw


def test_predicates() -> None:
    for s in ("STO_PUT", "CSP", "Short Put", "SHORT_PUT", "Cash Secured Put"):
        assert is_short_put_strategy(s)
        assert is_cash_secured_put_strategy(s)
    for s in ("Bull Put Spread", "BULL_PUT"):
        assert is_short_put_strategy(s)
        assert not is_cash_secured_put_strategy(s)
    for s in ("STO_CALL", "Covered Call", "BTO_PUT", "BTO_CALL", "Short Call"):
        assert not is_short_put_strategy(s)


def test_negative_beta_gets_no_hedge_credit() -> None:
    # 賣 Put 的帶號 Delta 為負；β < 0 時 β 加權後為正，再乘 -1 得負（原本的對沖折抵）。
    neg = optimize_position_risk(**_base(current_delta=15.0, unit_weighted_delta=0.4))
    pos = optimize_position_risk(**_base(current_delta=15.0, unit_weighted_delta=-0.4))
    assert neg.suggested_contracts == pos.suggested_contracts
    # 賣 Call 保留原行為（走 else 分支，有折抵）口數較大
    old_style = optimize_position_risk(
        **_base(current_delta=15.0, unit_weighted_delta=0.4, strategy="STO_CALL")
    )
    assert old_style.suggested_contracts > neg.suggested_contracts


def test_cap_binds_to_five_contracts() -> None:
    free = optimize_position_risk(**_base(unit_weighted_delta=-0.1))
    assert free.suggested_contracts > 5
    capped = optimize_position_risk(
        **_base(
            unit_weighted_delta=-0.1,
            available_collateral=62_500.0,
            collateral_per_contract=11_700.0,
        )
    )
    assert capped.suggested_contracts == 5
    msg = next(w for w in capped.warnings if "擔保上限 5 口" in w)
    assert len(msg) <= _MAX_WARNING_LEN
    assert capped.exposure_pct < free.exposure_pct
    one = optimize_position_risk(
        **_base(
            unit_weighted_delta=-0.1,
            available_collateral=62_500.0,
            collateral_per_contract=11_700.0,
            user_capital=1_000_000.0,
        )
    )
    assert one.unit_adj_delta is not None
    expected = (
        abs(one.suggested_contracts * one.unit_adj_delta) * 500.0 / 1_000_000 * 100
    )
    assert one.exposure_pct == pytest.approx(expected, abs=0.02)


@pytest.mark.parametrize("avail", [0.0, -500.0, 5_000.0])
def test_cap_zero_pauses(avail: float) -> None:
    res = optimize_position_risk(
        **_base(
            unit_weighted_delta=-0.1,
            available_collateral=avail,
            collateral_per_contract=11_700.0,
        )
    )
    assert res.suggested_contracts == 0
    msg = next(w for w in res.warnings if "暫停" in w)
    assert msg == "現金擔保不足：暫停賣方新倉"
    assert len(msg) <= _MAX_WARNING_LEN


@pytest.mark.parametrize("strategy", ["STO_CALL", "Covered Call"])
def test_calls_unchanged_by_cap_args(strategy: str) -> None:
    kw = _base(current_delta=15.0, unit_weighted_delta=0.4, strategy=strategy)
    base = optimize_position_risk(**kw)
    with_cap = optimize_position_risk(
        **kw, available_collateral=1_000.0, collateral_per_contract=11_700.0
    )
    assert with_cap == base


def test_bull_put_spread_no_credit_but_no_cap() -> None:
    kw = _base(current_delta=15.0, unit_weighted_delta=0.1, strategy="Bull Put Spread")
    spread = optimize_position_risk(**kw)
    pos = optimize_position_risk(**{**kw, "unit_weighted_delta": -0.1})
    assert spread.suggested_contracts == pos.suggested_contracts
    capped = optimize_position_risk(
        **kw, available_collateral=1_000.0, collateral_per_contract=11_700.0
    )
    assert capped == spread


def test_no_cap_args_equals_positive_sto_put_and_directional_unchanged() -> None:
    a = optimize_position_risk(**_base(unit_weighted_delta=3.0))
    b = optimize_position_risk(**_base(unit_weighted_delta=3.0, strategy="STO"))
    assert a.suggested_contracts == b.suggested_contracts
    assert a.exposure_pct == b.exposure_pct
    for strat in ("BTO_CALL", "BTO_PUT"):
        kw = _base(current_delta=15.0, unit_weighted_delta=-0.4, strategy=strat)
        res = optimize_position_risk(**kw)
        again = optimize_position_risk(
            **kw, available_collateral=1_000.0, collateral_per_contract=11_700.0
        )
        assert res == again


def test_warning_strings_within_budget() -> None:
    texts = [
        f"擔保上限 {5} 口（現金 ${62_500:,.0f}）",
        "現金擔保不足：暫停賣方新倉",
        "未設現金儲備：無擔保上限",
        "持倉讀取失敗：無擔保上限",
        "近到期 IV 未校驗",
    ]
    assert all(len(t) <= _MAX_WARNING_LEN for t in texts)


def test_compute_available_collateral() -> None:
    trades = [
        {
            "symbol": "WMT",
            "opt_type": "put",
            "strike": 100.0,
            "expiry": "x",
            "quantity": -2,
        }
    ]
    assert compute_available_collateral(50_000.0, trades) == pytest.approx(30_000.0)
    assert compute_available_collateral(0.0, trades) is None


@pytest.mark.asyncio
async def test_load_available_collateral_paths() -> None:
    avail, warn = await load_available_collateral(1, 0.0)
    assert avail is None and warn == "未設現金儲備：無擔保上限"
    asset = SimpleNamespace(
        symbol="WMT",
        metadata={"opt_type": "put", "strike": 100.0, "expiry": "x", "quantity": -1},
    )
    with patch("services.asset_manager.AssetManager.get_assets", return_value=[asset]):
        avail, warn = await load_available_collateral(1, 20_000.0)
    assert avail == pytest.approx(10_000.0) and warn is None
    with patch(
        "services.asset_manager.AssetManager.get_assets", side_effect=RuntimeError("x")
    ):
        avail, warn = await load_available_collateral(1, 20_000.0)
    assert avail is None and warn == "持倉讀取失敗：無擔保上限"


# ---------------- /x 倉位用 IV 選擇 ----------------


def test_select_sizing_iv_near_expiry_uses_straddle() -> None:
    iv, reason = select_sizing_iv(0.086, 0.324, "LIVE_IV", 1, False)
    assert iv == 0.324 and reason is None


def test_select_sizing_iv_divergence_uses_straddle() -> None:
    iv, reason = select_sizing_iv(0.15, 0.32, "LIVE_IV", 20, False)
    assert iv == 0.32 and reason is None
    iv, reason = select_sizing_iv(0.30, 0.32, "LIVE_IV", 20, False)
    assert iv == 0.30 and reason is None


def test_select_sizing_iv_no_straddle_near_expiry_degraded() -> None:
    iv, reason = select_sizing_iv(0.4, None, "LIVE_IV", 2, False)
    assert iv == 0.4 and reason == "近到期 IV 未校驗"
    # 尺度修正或非 LIVE_IV 不算近到期
    assert select_sizing_iv(0.4, None, "LIVE_IV", 2, True) == (0.4, None)
    assert select_sizing_iv(0.4, None, "STORED_IV", 1, False) == (0.4, None)


# ---------------- /x 整合 ----------------


async def _run_hub(iv: IVMetrics, ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin

    class _DD(SymbolDeepDiveMixin):
        def __init__(self) -> None:
            self.bot = MagicMock()

    raw: dict[str, Any] = {
        "df_spy": pd.DataFrame({"Close": [500.0]}),
        "macro_raw": {"vix": 20.0},
        "quote": {"c": 117.0, "d": 0.0, "dp": 0.0, "pc": 117.0},
        "skew_data": {},
        "pcr_data": {},
        "uoa_data": [],
        "sto_physical_cap_strikes": [],
        "max_pain_data": {},
        "iv_metrics": iv,
        "reddit_text": "",
        "poly_markets": [],
        "ddp_report": {},
        "df_hist_1d": pd.DataFrame(),
        "month_max_pains": [],
        "gex_profile_data": None,
        "volume_profile": None,
        "atr_15m": 0.0,
        "session_vwap": 0.0,
        "bar_15m": None,
        "catalysts": [],
    }
    captured: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> OptimizationResult:
        captured.update(kwargs)
        return OptimizationResult(suggested_contracts=1, exposure_pct=0.0)

    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
        patch("market_math.analyze_symbol", new_callable=AsyncMock) as m,
        patch(
            "cogs.unified_terminal.utils.find_matching_polymarket_odds",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "cogs.unified_terminal.utils.calculate_polymarket_weighted_odds",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("database.get_full_user_context", return_value=ctx),
        patch("market_time.is_market_open", return_value=False),
        patch("market_analysis.risk_engine.optimize_position_risk", side_effect=_fake),
    ):
        m.return_value = {"symbol": "WMT", "price": 117.0}
        res = await _DD()._process_symbol_hub_data("WMT", 1, raw)
    return res, captured


def _live_iv(**kw: Any) -> IVMetrics:
    base: dict[str, Any] = dict(
        symbol="WMT",
        current_iv=0.086,
        iv_source="LIVE_IV",
        current_iv_dte=1,
        straddle_implied_iv=0.324,
        straddle_expiry="2026-10-23",
        straddle_dte=1,
        reference_spot_price=117.0,
        hv_20=0.25,
    )
    base.update(kw)
    return IVMetrics(**base)


@pytest.mark.asyncio
async def test_hub_passes_signed_delta_cap_args_and_straddle_iv() -> None:
    ctx = SimpleNamespace(capital=500_000.0, risk_limit=15.0, cash_reserve=62_500.0)
    res, kw = await _run_hub(_live_iv(), ctx)
    assert kw["strategy"] == "STO_PUT"
    assert kw["unit_weighted_delta"] < 0
    assert kw["available_collateral"] == pytest.approx(62_500.0)
    assert kw["collateral_per_contract"] == pytest.approx(11_700.0)
    assert kw["stock_iv"] == pytest.approx(0.324)
    assert res["kelly_unit_weighted_delta"] > 0
    assert res["kelly_unit_weighted_delta"] == pytest.approx(-kw["unit_weighted_delta"])


@pytest.mark.asyncio
async def test_hub_near_expiry_without_straddle_degrades() -> None:
    ctx = SimpleNamespace(capital=500_000.0, risk_limit=15.0, cash_reserve=0.0)
    _, kw = await _run_hub(_live_iv(straddle_implied_iv=None), ctx)
    assert kw["stock_iv"] == pytest.approx(0.086)
    assert "近到期 IV 未校驗" in kw["data_degraded_reasons"]
    assert kw["available_collateral"] is None


@pytest.mark.asyncio
async def test_hub_read_failure_does_not_abort_sizing() -> None:
    ctx = SimpleNamespace(capital=500_000.0, risk_limit=15.0, cash_reserve=62_500.0)
    with patch(
        "market_analysis.risk_engine.load_available_collateral",
        new_callable=AsyncMock,
        return_value=(None, "持倉讀取失敗：無擔保上限"),
    ):
        res, kw = await _run_hub(_live_iv(), ctx)
    assert kw["available_collateral"] is None
    assert "持倉讀取失敗：無擔保上限" in res["kelly_sizing"].warnings
