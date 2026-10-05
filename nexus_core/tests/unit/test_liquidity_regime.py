"""單元測試：央行淨流動性與體制分類量化純函式 (liquidity_regime.py)。"""

from __future__ import annotations

import pytest
from market_analysis.fundamental_pipeline.liquidity_regime import (
    DEFAULT_BASE_ERP,
    calculate_13w_change,
    calculate_dynamic_erp,
    calculate_net_liquidity,
    classify_liquidity_regime,
)


def test_calculate_net_liquidity_standard_values() -> None:
    """測試 WALCL (Mil)、WTREGEN (Mil)、RRPONTSYD (Bn) 換算為 Net Liquidity (Bn)。"""
    # 聯準會總資產 7,000,000M = 7,000B
    # 財政部 TGA 800,000M = 800B
    # 逆回購 200B
    # 淨流動性 = 7000 - 800 - 200 = 6000B
    net_liq = calculate_net_liquidity(
        walcl_mil=7_000_000.0,
        wtregen_mil=800_000.0,
        rrp_bn=200.0,
    )
    assert pytest.approx(net_liq, 1e-4) == 6000.0


def test_calculate_13w_change() -> None:
    """測試 13 週動態變更率計算與邊界防護。"""
    # 正常成長: 6600 vs 6000 -> +10%
    assert pytest.approx(calculate_13w_change(6600.0, 6000.0) or 0.0, 1e-4) == 10.0
    # 正常縮減: 5700 vs 6000 -> -5%
    assert pytest.approx(calculate_13w_change(5700.0, 6000.0) or 0.0, 1e-4) == -5.0
    # 基期缺失
    assert calculate_13w_change(6000.0, None) is None
    # 基期為 0 防禦
    assert calculate_13w_change(6000.0, 0.0) is None


def test_classify_liquidity_regime_tight() -> None:
    """測試緊縮 (TIGHT) 體制判定。"""
    # NFCI >= 0.0 即為緊縮
    assert classify_liquidity_regime(nfci=0.05, net_liq_chg_13w=2.0) == "TIGHT"
    assert classify_liquidity_regime(nfci=0.0, net_liq_chg_13w=1.0) == "TIGHT"
    # 13w 變更率 <= -3.0% 即為緊縮
    assert classify_liquidity_regime(nfci=-0.2, net_liq_chg_13w=-3.5) == "TIGHT"
    assert classify_liquidity_regime(nfci=-0.4, net_liq_chg_13w=-3.0) == "TIGHT"


def test_classify_liquidity_regime_easy() -> None:
    """測試寬鬆 (EASY) 體制判定：NFCI <= -0.5 且 13w 變更率 >= 0.0%。"""
    assert classify_liquidity_regime(nfci=-0.55, net_liq_chg_13w=1.5) == "EASY"
    assert classify_liquidity_regime(nfci=-0.5, net_liq_chg_13w=0.0) == "EASY"
    # 若 13w 變更率小於 0，即使 NFCI <= -0.5 亦不構成 EASY，落入 NEUTRAL
    assert classify_liquidity_regime(nfci=-0.6, net_liq_chg_13w=-1.0) == "NEUTRAL"


def test_classify_liquidity_regime_neutral() -> None:
    """測試中性 (NEUTRAL) 體制判定。"""
    assert classify_liquidity_regime(nfci=-0.3, net_liq_chg_13w=1.0) == "NEUTRAL"
    assert classify_liquidity_regime(nfci=-0.1, net_liq_chg_13w=-2.0) == "NEUTRAL"


def test_classify_liquidity_regime_unknown() -> None:
    """測試關鍵數值缺失回傳 UNKNOWN。"""
    assert classify_liquidity_regime(nfci=None, net_liq_chg_13w=1.0) == "UNKNOWN"
    assert classify_liquidity_regime(nfci=-0.3, net_liq_chg_13w=None) == "UNKNOWN"
    assert classify_liquidity_regime(nfci=None, net_liq_chg_13w=None) == "UNKNOWN"


def test_calculate_dynamic_erp() -> None:
    """測試動態 ERP 計算與箝制範圍。"""
    # 基準狀況：NFCI = 0.0 -> ERP = 0.045
    erp_zero = calculate_dynamic_erp(0.0)
    assert erp_zero is not None
    assert pytest.approx(erp_zero, 1e-6) == DEFAULT_BASE_ERP

    # 寬鬆狀況：NFCI = -0.5 -> ERP = 0.045 + 0.010 * (-0.5) = 0.040
    erp_easy = calculate_dynamic_erp(-0.5)
    assert erp_easy is not None
    assert pytest.approx(erp_easy, 1e-6) == 0.040

    # 緊縮狀況：NFCI = +1.0 -> ERP = 0.045 + 0.010 * 1.0 = 0.055
    erp_tight = calculate_dynamic_erp(1.0)
    assert erp_tight is not None
    assert pytest.approx(erp_tight, 1e-6) == 0.055

    # 箝制下限：NFCI = -2.0 -> 箝制為 -1.0 -> ERP = 0.045 + 0.010 * (-1.0) = 0.035
    erp_min = calculate_dynamic_erp(-2.0)
    assert erp_min is not None
    assert pytest.approx(erp_min, 1e-6) == 0.035

    # 箝制上限：NFCI = +3.5 -> 箝制為 +2.0 -> ERP = 0.045 + 0.010 * 2.0 = 0.065
    erp_max = calculate_dynamic_erp(3.5)
    assert erp_max is not None
    assert pytest.approx(erp_max, 1e-6) == 0.065

    # 數值缺失回傳 None
    assert calculate_dynamic_erp(None) is None


def test_liquidity_regime_nan_inf_guards() -> None:
    """測試非有限浮點數 (NaN / Inf) 防禦，確保不會因比較失敗誤判為 NEUTRAL。"""
    # 1. 體制判定：NaN 必須安全降級為 UNKNOWN
    assert classify_liquidity_regime(float("nan"), 2.0) == "UNKNOWN"
    assert classify_liquidity_regime(-0.6, float("nan")) == "UNKNOWN"
    assert classify_liquidity_regime(float("inf"), 2.0) == "UNKNOWN"

    # 2. ERP 計算：NaN 必須回傳 None
    assert calculate_dynamic_erp(float("nan")) is None
    assert calculate_dynamic_erp(float("inf")) is None

    # 3. 13w 變更率：NaN 必須回傳 None
    assert calculate_13w_change(float("nan"), 100.0) is None
    assert calculate_13w_change(100.0, float("nan")) is None
