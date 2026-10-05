"""單元測試：宏觀預期差標準化模型純函式 (macro_surprise.py)。"""

from __future__ import annotations

import pytest
from market_analysis.fundamental_pipeline.macro_surprise import (
    calculate_sample_std,
    calculate_standardized_surprise,
    match_macro_event,
)


def test_calculate_sample_std() -> None:
    """測試樣本標準差 (N - 1 自由度)。"""
    # 樣本數 < 2
    assert calculate_sample_std([]) is None
    assert calculate_sample_std([1.0]) is None

    # 已知樣本: [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    # mean = 5.0, variance = 32 / 7 = 4.5714, std = 2.13809
    diffs = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    std = calculate_sample_std(diffs)
    assert std is not None
    assert pytest.approx(std, 1e-4) == 2.13809


def test_calculate_standardized_surprise_insufficient_samples() -> None:
    """測試歷史樣本少於 6 期時，不計算 Z-score (回傳 None)。"""
    # 只有 5 期歷史樣本
    history = [0.1, -0.2, 0.05, 0.15, -0.1]
    raw_diff, z_score = calculate_standardized_surprise(
        actual=3.4,
        forecast=3.2,
        historical_diffs=history,
    )
    assert pytest.approx(raw_diff, 1e-4) == 0.2
    assert z_score is None


def test_calculate_standardized_surprise_with_sufficient_samples() -> None:
    """測試歷史樣本 >= 6 期時正確計算 Z 分數。"""
    # 6 期歷史樣本: 標準差已知
    history = [0.1, 0.2, -0.1, -0.2, 0.0, 0.3]
    # std([0.1, 0.2, -0.1, -0.2, 0.0, 0.3])
    # mean = 0.05, sum of sq diffs = 0.0025 + 0.0225 + 0.0225 + 0.0625 + 0.0025 + 0.0625 = 0.175
    # variance = 0.175 / 5 = 0.035, std = 0.18708
    raw_diff, z_score = calculate_standardized_surprise(
        actual=3.3,
        forecast=3.1,  # diff = 0.2
        historical_diffs=history,
    )
    assert pytest.approx(raw_diff, 1e-4) == 0.2
    assert z_score is not None
    expected_z = 0.2 / 0.1870828
    assert pytest.approx(z_score, 1e-3) == expected_z


def test_calculate_standardized_surprise_clipping() -> None:
    """測試極端離群值箝制於 [-4.0, +4.0]。"""
    history = [0.01, 0.02, -0.01, -0.02, 0.0, 0.01]  # std 非常小 (~0.015)
    # 正向極端：diff = +1.0 -> z ~ 66 -> 箝制為 4.0
    _, z_high = calculate_standardized_surprise(
        actual=4.0,
        forecast=3.0,
        historical_diffs=history,
    )
    assert z_high == 4.0

    # 負向極端：diff = -1.0 -> 箝制為 -4.0
    _, z_low = calculate_standardized_surprise(
        actual=2.0,
        forecast=3.0,
        historical_diffs=history,
    )
    assert z_low == -4.0


def test_calculate_standardized_surprise_zero_variance() -> None:
    """測試所有歷史數值相同（標準差為 0）時的防禦。"""
    history = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
    raw_diff, z_score = calculate_standardized_surprise(
        actual=3.3,
        forecast=3.1,
        historical_diffs=history,
    )
    assert pytest.approx(raw_diff, 1e-4) == 0.2
    assert z_score is None


def test_match_macro_event() -> None:
    """測試總經事件名稱匹配與繁體中文屬性。"""
    cpi = match_macro_event("US CPI MoM (May)")
    assert cpi is not None
    assert cpi.event_key == "CPI_MOM"
    assert cpi.growth_sign == -1
    assert "物價" in cpi.name_zh

    nfp = match_macro_event("Non Farm Payrolls")
    assert nfp is not None
    assert nfp.event_key == "NFP"
    assert nfp.growth_sign == 1

    ism = match_macro_event("ISM Manufacturing PMI")
    assert ism is not None
    assert ism.event_key == "ISM_MANUFACTURING"
    assert ism.growth_sign == 1

    unknown = match_macro_event("Some Random Local Survey")
    assert unknown is None


def test_macro_surprise_nan_and_inf_handling() -> None:
    """測試非有限浮點數 (NaN / Inf) 輸入防禦，避免污染資料庫。"""
    # 1. 樣本中混入 NaN / Inf 應被乾淨剔除
    std_nan = calculate_sample_std([1.0, 2.0, 3.0, float("nan"), float("inf")])
    assert std_nan is not None
    # 剩餘 [1.0, 2.0, 3.0]，標準差為 1.0
    assert pytest.approx(std_nan, 1e-4) == 1.0

    # 2. actual 或 forecast 為 NaN
    diff, z = calculate_standardized_surprise(float("nan"), 2.0, [0.1] * 10)
    assert z is None

    # 3. 歷史資料中含 NaN 導致有效資料不足 6 筆
    history_with_nans = [0.1, 0.2, float("nan"), 0.3, float("inf"), 0.4]
    _, z_insufficient = calculate_standardized_surprise(1.0, 0.5, history_with_nans)
    assert z_insufficient is None
