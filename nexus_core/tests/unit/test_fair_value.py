"""兩段式現金流折現與同業倍數估值模型單元測試。"""

import pytest
from market_analysis.fundamental_pipeline.fair_value import (
    FLAG_VALUATION_DIVERGENCE,
    VALUATION_FLAG_LABELS_ZH,
    VALUATION_DIVERGENCE_RATIO,
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


def _comps(value: float) -> CompsResult:
    return CompsResult(
        comps_value=value, median_pe=20.0, liquidity_penalty_factor=1.0, is_valid=True
    )


def test_valuation_divergence_flag_threshold_and_symmetry() -> None:
    """BLENDED 且 max/min >= 3 才加旗標（DCF 高或 Comps 高皆可）；< 3 不加。"""
    assert VALUATION_DIVERGENCE_RATIO == 3.0
    assert FLAG_VALUATION_DIVERGENCE in VALUATION_FLAG_LABELS_ZH

    hi_comps = integrate_fair_value(
        80.0, DCFResult(dcf_value=100.0, is_valid=True), _comps(300.0), 0.08, 0.045
    )
    assert FLAG_VALUATION_DIVERGENCE in hi_comps.flags
    hi_dcf = integrate_fair_value(
        80.0, DCFResult(dcf_value=300.0, is_valid=True), _comps(100.0), 0.08, 0.045
    )
    assert FLAG_VALUATION_DIVERGENCE in hi_dcf.flags
    just_below = integrate_fair_value(
        80.0, DCFResult(dcf_value=100.0, is_valid=True), _comps(299.0), 0.08, 0.045
    )
    assert FLAG_VALUATION_DIVERGENCE not in just_below.flags


def test_valuation_divergence_not_flagged_for_single_method() -> None:
    comps_invalid = CompsResult(
        comps_value=None, median_pe=None, liquidity_penalty_factor=1.0, is_valid=False
    )
    dcf_invalid = DCFResult(dcf_value=None, is_valid=False, rejection_reason="X")
    dcf_only = integrate_fair_value(
        80.0, DCFResult(dcf_value=500.0, is_valid=True), comps_invalid, 0.08, 0.045
    )
    comps_only = integrate_fair_value(80.0, dcf_invalid, _comps(500.0), 0.08, 0.045)
    assert dcf_only.method == "DCF_ONLY"
    assert comps_only.method == "COMPS_ONLY"
    assert FLAG_VALUATION_DIVERGENCE not in dcf_only.flags
    assert FLAG_VALUATION_DIVERGENCE not in comps_only.flags


def test_valuation_divergence_takes_lower_value() -> None:
    """比值 >= 3：取低值、兩旗標齊備、method 仍 BLENDED、MOS 與 DEEP_VALUE 以低值計算。"""
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_VALUATION_DIVERGENCE_CAPPED,
    )

    assert FLAG_VALUATION_DIVERGENCE_CAPPED in VALUATION_FLAG_LABELS_ZH
    assert "3" in VALUATION_FLAG_LABELS_ZH[FLAG_VALUATION_DIVERGENCE]
    # 分歧（比值 5）：舊邏輯中樞 180（深度價值），新邏輯取低值 60 → 現價 80 溢價
    diverged = integrate_fair_value(
        80.0, DCFResult(dcf_value=60.0, is_valid=True), _comps(300.0), 0.08, 0.045
    )
    assert FLAG_VALUATION_DIVERGENCE in diverged.flags
    assert FLAG_VALUATION_DIVERGENCE_CAPPED in diverged.flags
    assert diverged.method == "BLENDED"
    assert diverged.fair_value == 60.0
    assert diverged.margin_of_safety == pytest.approx((60.0 - 80.0) / 60.0, abs=1e-4)
    assert diverged.is_deep_value is False
    assert "OVERVALUED" in diverged.flags

    # 取低後仍深度價值：Comps 低、DCF 高
    deep = integrate_fair_value(
        80.0, DCFResult(dcf_value=900.0, is_valid=True), _comps(300.0), 0.08, 0.045
    )
    assert deep.fair_value == 300.0
    assert deep.is_deep_value is True and "DEEP_VALUE" in deep.flags

    # 取低後的治理壓制
    gov = integrate_fair_value(
        80.0,
        DCFResult(dcf_value=900.0, is_valid=True),
        _comps(300.0),
        0.08,
        0.045,
        is_governance_clean=False,
    )
    assert gov.is_deep_value is False
    assert "DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE" in gov.flags


def test_below_threshold_keeps_fifty_fifty_average() -> None:
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_VALUATION_DIVERGENCE_CAPPED,
    )

    plain = integrate_fair_value(
        80.0, DCFResult(dcf_value=150.0, is_valid=True), _comps(210.0), 0.08, 0.045
    )
    assert plain.fair_value == 180.0
    assert plain.method == "BLENDED"
    assert FLAG_VALUATION_DIVERGENCE not in plain.flags
    assert FLAG_VALUATION_DIVERGENCE_CAPPED not in plain.flags
    assert plain.is_deep_value is True


def _cyc(dcf: float | None, comps: float | None, spot: float = 80.0, **kw: bool):  # type: ignore[no-untyped-def]
    d = (
        DCFResult(dcf_value=dcf, is_valid=True)
        if dcf is not None
        else DCFResult(dcf_value=None, is_valid=False, rejection_reason="X")
    )
    c = (
        _comps(comps)
        if comps is not None
        else CompsResult(
            comps_value=None,
            median_pe=None,
            liquidity_penalty_factor=1.0,
            is_valid=False,
        )
    )
    return integrate_fair_value(spot, d, c, 0.08, 0.045, **kw)


def test_cyclical_caps_blended_at_dcf() -> None:
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_CYCLICAL_DCF_CAP,
        FLAG_CYCLICAL_NO_DCF_CAP,
    )

    assert FLAG_CYCLICAL_DCF_CAP in VALUATION_FLAG_LABELS_ZH
    assert FLAG_CYCLICAL_NO_DCF_CAP in VALUATION_FLAG_LABELS_ZH
    res = _cyc(100.0, 250.0, cyclical=True)  # 平均 175 > DCF 100；比值 2.5 未分歧
    assert res.fair_value == 100.0
    assert res.method == "BLENDED"
    assert FLAG_CYCLICAL_DCF_CAP in res.flags
    assert res.margin_of_safety == pytest.approx((100.0 - 80.0) / 100.0, abs=1e-4)
    assert "MODERATE_DISCOUNT" in res.flags
    # 非景氣循環：行為不變
    plain = _cyc(100.0, 250.0)
    assert plain.fair_value == 175.0
    assert FLAG_CYCLICAL_DCF_CAP not in plain.flags


def test_cyclical_not_capped_when_fv_not_above_dcf() -> None:
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_CYCLICAL_DCF_CAP,
        FLAG_CYCLICAL_NO_DCF_CAP,
    )

    res = _cyc(200.0, 100.0, cyclical=True)  # 平均 150 <= DCF 200
    assert res.fair_value == 150.0
    assert FLAG_CYCLICAL_DCF_CAP not in res.flags
    assert FLAG_CYCLICAL_NO_DCF_CAP not in res.flags
    only = _cyc(200.0, None, cyclical=True)  # DCF_ONLY：FV == DCF
    assert only.fair_value == 200.0 and only.method == "DCF_ONLY"
    assert FLAG_CYCLICAL_DCF_CAP not in only.flags


def test_cyclical_without_dcf_flags_but_keeps_value() -> None:
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_CYCLICAL_DCF_CAP,
        FLAG_CYCLICAL_NO_DCF_CAP,
    )

    res = _cyc(None, 300.0, cyclical=True)
    assert res.method == "COMPS_ONLY" and res.fair_value == 300.0
    assert FLAG_CYCLICAL_NO_DCF_CAP in res.flags
    assert FLAG_CYCLICAL_DCF_CAP not in res.flags
    assert FLAG_CYCLICAL_NO_DCF_CAP not in _cyc(None, 300.0).flags


def test_cyclical_cap_after_divergence_min() -> None:
    from market_analysis.fundamental_pipeline.fair_value import (
        FLAG_CYCLICAL_DCF_CAP,
        FLAG_VALUATION_DIVERGENCE_CAPPED,
    )

    # 分歧取低後 = DCF → 不再封頂
    low_dcf = _cyc(60.0, 300.0, cyclical=True)
    assert low_dcf.fair_value == 60.0
    assert FLAG_VALUATION_DIVERGENCE_CAPPED in low_dcf.flags
    assert FLAG_CYCLICAL_DCF_CAP not in low_dcf.flags
    # 分歧取低後 = Comps(100) < DCF(400) → 不封頂
    low_comps = _cyc(400.0, 100.0, cyclical=True)
    assert low_comps.fair_value == 100.0
    assert FLAG_CYCLICAL_DCF_CAP not in low_comps.flags


def test_cyclical_cap_governance_and_deep_value_use_capped_value() -> None:
    res = _cyc(100.0, 250.0, spot=60.0, cyclical=True)  # 封頂 100 → MOS 40%
    assert res.fair_value == 100.0 and res.is_deep_value is True
    sup = _cyc(100.0, 250.0, spot=60.0, cyclical=True, is_governance_clean=False)
    assert sup.is_deep_value is False
    assert "DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE" in sup.flags
