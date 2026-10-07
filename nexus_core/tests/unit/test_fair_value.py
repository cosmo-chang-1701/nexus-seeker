"""兩段式現金流折現與同業倍數估值模型單元測試。"""

import pytest
from market_analysis.fundamental_pipeline.fair_value import (
    calculate_comps_value,
    calculate_cost_of_equity,
    calculate_dcf_value,
    calculate_equity_risk_premium,
    integrate_fair_value,
)
from market_analysis.fundamental_pipeline.models import (
    CompsInputs,
    CompsResult,
    DCFInputs,
    DCFResult,
)


def test_calculate_equity_risk_premium() -> None:
    # NFCI = None 預設基準值 4.5%
    assert pytest.approx(calculate_equity_risk_premium(None), rel=1e-4) == 0.045

    # NFCI = 0.0 中性
    assert pytest.approx(calculate_equity_risk_premium(0.0), rel=1e-4) == 0.045

    # NFCI = 1.0 緊縮，上升 100 bps -> 5.5%
    assert pytest.approx(calculate_equity_risk_premium(1.0), rel=1e-4) == 0.055

    # NFCI = -0.5 寬鬆，下降 50 bps -> 4.0%
    assert pytest.approx(calculate_equity_risk_premium(-0.5), rel=1e-4) == 0.040

    # NFCI 箝制於 [-1.0, 2.0]
    assert (
        pytest.approx(calculate_equity_risk_premium(3.5), rel=1e-4)
        == 0.045 + 0.010 * 2.0
    )
    assert (
        pytest.approx(calculate_equity_risk_premium(-2.5), rel=1e-4)
        == 0.045 + 0.010 * -1.0
    )


def test_calculate_cost_of_equity() -> None:
    # us10y = 4.25 (百分比自動轉小數), beta = 1.2, nfci = 0.0 (erp = 0.045)
    # r = 0.0425 + 1.2 * 0.045 = 0.0425 + 0.054 = 0.0965
    r, erp = calculate_cost_of_equity(us10y=4.25, nfci=0.0, beta=1.2)
    assert pytest.approx(r, rel=1e-4) == 0.0965
    assert pytest.approx(erp, rel=1e-4) == 0.045

    # beta 箝制於 [0.5, 2.0]
    r_high_beta, _ = calculate_cost_of_equity(us10y=0.04, nfci=0.0, beta=3.5)
    assert pytest.approx(r_high_beta, rel=1e-4) == 0.04 + 2.0 * 0.045


def test_calculate_dcf_value_normal() -> None:
    inputs = DCFInputs(
        fcf_per_share=5.0,
        growth_rate_1y=0.10,
        cost_of_equity=0.08,
        perpetual_growth_rate=0.025,
    )
    res = calculate_dcf_value(inputs)
    assert res.is_valid is True
    assert res.dcf_value is not None
    assert res.dcf_value > 50.0  # 正常合理數值範圍


def test_calculate_dcf_value_rejections() -> None:
    # 負自由現金流拒絕
    res_neg_fcf = calculate_dcf_value(
        DCFInputs(fcf_per_share=-2.0, growth_rate_1y=0.10, cost_of_equity=0.08)
    )
    assert res_neg_fcf.is_valid is False
    assert res_neg_fcf.rejection_reason == "FCF_NON_POSITIVE"

    # 零現金流拒絕
    res_zero_fcf = calculate_dcf_value(
        DCFInputs(fcf_per_share=0.0, growth_rate_1y=0.10, cost_of_equity=0.08)
    )
    assert res_zero_fcf.is_valid is False
    assert res_zero_fcf.rejection_reason == "FCF_NON_POSITIVE"

    # 利差過窄拒絕 (r - g_T < 0.010, 例如 r = 0.03, g_T = 0.025 -> 0.005)
    res_narrow_spread = calculate_dcf_value(
        DCFInputs(
            fcf_per_share=5.0,
            growth_rate_1y=0.05,
            cost_of_equity=0.03,
            perpetual_growth_rate=0.025,
        )
    )
    assert res_narrow_spread.is_valid is False
    assert res_narrow_spread.rejection_reason == "SPREAD_TOO_NARROW"


def test_calculate_comps_value_normal() -> None:
    inputs = CompsInputs(
        forward_eps=4.0,
        peer_pes=[20.0, 25.0, 30.0, 35.0],
        nfci=0.0,
    )
    res = calculate_comps_value(inputs)
    assert res.is_valid is True
    assert res.comps_value is not None
    assert res.median_pe == 27.5
    # penalty = exp(-0.10 * 0) = 1.0, fv = 4.0 * 27.5 = 110.0
    assert pytest.approx(res.comps_value, rel=1e-2) == 110.0


def test_calculate_comps_value_rejections() -> None:
    # 負 Forward EPS
    res_neg_eps = calculate_comps_value(
        CompsInputs(forward_eps=-1.5, peer_pes=[20.0, 25.0, 30.0], nfci=0.0)
    )
    assert res_neg_eps.is_valid is False
    assert res_neg_eps.rejection_reason == "FORWARD_EPS_NON_POSITIVE"

    # 有效同業不足 3 家 (只有 2 家正本益比)
    res_few_peers = calculate_comps_value(
        CompsInputs(forward_eps=2.0, peer_pes=[20.0, -5.0, 0.0, 25.0], nfci=0.0)
    )
    assert res_few_peers.is_valid is False
    assert res_few_peers.rejection_reason == "INSUFFICIENT_PEERS"


def test_integrate_fair_value_blended_and_deep_value() -> None:
    dcf_res = DCFResult(dcf_value=120.0, is_valid=True)
    comps_res = CompsResult(
        comps_value=100.0, median_pe=25.0, liquidity_penalty_factor=1.0, is_valid=True
    )

    # 1. 雙有效融合中樞 (120 + 100) / 2 = 110.0
    # spot = 80.0, MOS = (110 - 80) / 110 = 27.27% >= 25% 且無治理問題 -> DEEP_VALUE
    res = integrate_fair_value(
        spot_price=80.0,
        dcf_res=dcf_res,
        comps_res=comps_res,
        discount_rate=0.08,
        equity_risk_premium=0.045,
        is_governance_clean=True,
    )
    assert res.fair_value == 110.0
    assert res.method == "BLENDED"
    assert pytest.approx(res.margin_of_safety, rel=1e-4) == 0.2727
    assert res.is_deep_value is True
    assert "DEEP_VALUE" in res.flags

    # 2. 治理有問題時壓制深度價值
    res_gov_risk = integrate_fair_value(
        spot_price=80.0,
        dcf_res=dcf_res,
        comps_res=comps_res,
        discount_rate=0.08,
        equity_risk_premium=0.045,
        is_governance_clean=False,
    )
    assert res_gov_risk.is_deep_value is False
    assert "DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE" in res_gov_risk.flags
    assert "GOVERNANCE_RISK" in res_gov_risk.flags


def test_integrate_fair_value_fallbacks() -> None:
    dcf_res = DCFResult(
        dcf_value=None, is_valid=False, rejection_reason="FCF_NON_POSITIVE"
    )
    comps_res = CompsResult(
        comps_value=95.0, median_pe=20.0, liquidity_penalty_factor=1.0, is_valid=True
    )

    # DCF 失效退回 Comps
    res_comps_only = integrate_fair_value(
        spot_price=90.0,
        dcf_res=dcf_res,
        comps_res=comps_res,
        discount_rate=0.08,
        equity_risk_premium=0.045,
    )
    assert res_comps_only.fair_value == 95.0
    assert res_comps_only.method == "COMPS_ONLY"

    # 兩者皆失效
    comps_invalid = CompsResult(
        comps_value=None, median_pe=None, liquidity_penalty_factor=1.0, is_valid=False
    )
    res_none = integrate_fair_value(
        spot_price=90.0,
        dcf_res=dcf_res,
        comps_res=comps_invalid,
        discount_rate=0.08,
        equity_risk_premium=0.045,
    )
    assert res_none.fair_value is None
    assert res_none.method == "NONE"


def test_calculate_cost_of_equity_low_yield_boundary() -> None:
    # 測試歷史低利率環境 (例如 2020 年 COVID 0.85% 國債殖利率)
    # 0.85% 應正確辨識為百分比並轉為 0.0085，而非 0.85 (85%)
    r_low, erp = calculate_cost_of_equity(us10y=0.85, nfci=0.0, beta=1.0)
    assert pytest.approx(r_low, rel=1e-4) == 0.0085 + 1.0 * 0.045

    # 1.0% 也應正確除以 100 -> 0.010
    r_one_pct, _ = calculate_cost_of_equity(us10y=1.0, nfci=0.0, beta=1.0)
    assert pytest.approx(r_one_pct, rel=1e-4) == 0.010 + 1.0 * 0.045

    # 0.0425 小數形式應直接保留為 0.0425
    r_decimal, _ = calculate_cost_of_equity(us10y=0.0425, nfci=0.0, beta=1.0)
    assert pytest.approx(r_decimal, rel=1e-4) == 0.0425 + 1.0 * 0.045


def test_calculate_dcf_spread_floating_point_tolerance() -> None:
    # 恰好利差 100 bps (r = 0.035, g_T = 0.025)，因浮點數減法可能有微小誤差，需容差保護通過
    inputs = DCFInputs(
        fcf_per_share=4.0,
        growth_rate_1y=0.05,
        cost_of_equity=0.035,
        perpetual_growth_rate=0.025,
    )
    res = calculate_dcf_value(inputs)
    assert res.is_valid is True
    assert res.dcf_value is not None
    assert res.dcf_value > 0


def test_calculate_comps_value_optional_nfci() -> None:
    # nfci 為 None 時應安全退回 0.0 懲罰 (penalty factor = 1.0)
    inputs = CompsInputs(
        forward_eps=5.0,
        peer_pes=[20.0, 22.0, 24.0],
        nfci=None,
    )
    res = calculate_comps_value(inputs)
    assert res.is_valid is True
    assert res.median_pe == 22.0
    assert pytest.approx(res.liquidity_penalty_factor, rel=1e-4) == 1.0
    assert pytest.approx(res.comps_value, rel=1e-2) == 110.0
