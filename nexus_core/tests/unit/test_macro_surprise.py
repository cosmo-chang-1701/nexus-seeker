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
    """以 economic_calendar_events.event 實際儲存的中文全名匹配。"""
    cpi = match_macro_event("CPI 月增率")
    assert cpi is not None
    assert cpi.event_key == "CPI_MOM"
    assert cpi.growth_sign == -1
    assert "物價" in cpi.name_zh

    core_cpi = match_macro_event("核心 CPI 月增率")
    assert core_cpi is not None
    assert core_cpi.event_key == "CORE_CPI_MOM"

    nfp = match_macro_event("非農就業人數")
    assert nfp is not None
    assert nfp.event_key == "NFP"
    assert nfp.growth_sign == 1

    claims = match_macro_event("初領失業救濟金人數")
    assert claims is not None
    assert claims.event_key == "INITIAL_CLAIMS"

    ism = match_macro_event("ISM 製造業 PMI")
    assert ism is not None
    assert ism.event_key == "ISM_MANUFACTURING"
    assert ism.growth_sign == 1

    retail = match_macro_event("零售銷售月增率")
    assert retail is not None
    assert retail.event_key == "RETAIL_SALES_MOM"

    unknown = match_macro_event("Some Random Local Survey")
    assert unknown is None
    assert match_macro_event("") is None


def test_match_macro_event_english_fallback_and_normalization() -> None:
    """英文原名僅作備援；空白與大小寫正規化後精確相等。"""
    nfp = match_macro_event("Non Farm Payrolls")
    assert nfp is not None and nfp.event_key == "NFP"
    cpi = match_macro_event("  inflation   rate MoM ")
    assert cpi is not None and cpi.event_key == "CPI_MOM"
    ism = match_macro_event("ism  製造業 pmi")
    assert ism is not None and ism.event_key == "ISM_MANUFACTURING"


def test_match_macro_event_no_substring_misclassification() -> None:
    """子字串相近但語意不同的事件不得被錯歸（舊版子字串 + 先到先得的缺陷）。"""
    # 核心 CPI 不得落入 CPI_MOM
    core = match_macro_event("核心 CPI 月增率")
    assert core is not None and core.event_key != "CPI_MOM"
    core_en = match_macro_event("Core CPI MoM")
    assert core_en is not None and core_en.event_key == "CORE_CPI_MOM"
    # 續領 / 4 週均值不得落入 INITIAL_CLAIMS
    assert match_macro_event("連續請領失業救濟金人數") is None
    assert match_macro_event("初領失業金 4 週移動平均") is None
    assert match_macro_event("Continuing Jobless Claims") is None
    assert match_macro_event("Initial Jobless Claims 4-week Average") is None
    # 核心零售、ISM 子項不得落入主指標
    assert match_macro_event("核心零售銷售月增率 (除汽車)") is None
    assert match_macro_event("ISM 製造業價格指數") is None
    # 含額外前後綴的名稱不做模糊比對
    assert match_macro_event("US CPI MoM (May)") is None


def test_macro_event_registry_names_match_translator_output() -> None:
    """註冊表中文名稱必須是翻譯器的實際輸出，避免與 Edge 翻譯表漂移後永遠匹配不到。"""
    from market_analysis.fundamental_pipeline.macro_surprise import (
        MACRO_EVENT_REGISTRY,
    )
    from market_analysis.macro_calendar_translator import (
        MACRO_EVENT_TRANSLATIONS,
        translate_macro_event,
    )

    translated_values = set(MACRO_EVENT_TRANSLATIONS.values())
    for defn in MACRO_EVENT_REGISTRY:
        for name in defn.names_zh:
            assert name in translated_values, f"{defn.event_key}: {name}"
        for alias in defn.aliases_en:
            translated = translate_macro_event(alias)
            matched = match_macro_event(translated)
            assert (
                matched is not None and matched.event_key == defn.event_key
            ), f"{alias} -> {translated}"


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
