"""宏觀預期差標準化模型（純量化純函式模組）。

參照規格 docs/macro_sentiment/05_liquidity_regime_and_macro_surprise.md：
1. 歷史波動率標準化預期差 (Standardized Surprise Z-Score):
   z_t = (A_t - F_t) / sigma_12(A - F)
2. 最小樣本門檻 (N >= 6) 與離群值箝制 ([-4.0, +4.0])
3. 官方總經發布註冊表與繁體中文對照 (Release Registry)
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# 具名量化常數
MIN_SURPRISE_SAMPLES: int = 6  # 計算標準差的最小歷史樣本數
MAX_SURPRISE_LOOKBACK: int = 12  # 歷史預期差回溯期數上限
Z_SCORE_CLIP_MIN: float = -4.0  # Z 分數極值箝制下限
Z_SCORE_CLIP_MAX: float = 4.0  # Z 分數極值箝制上限
EPSILON_STD: float = 1e-9  # 標準差除零防禦下限


@dataclass(frozen=True)
class MacroEventDefinition:
    """宏觀發布事件規格定義。"""

    event_key: str
    name_zh: str
    growth_sign: int  # 1: 增長導向 (越高代表景氣強勁), -1: 緊縮/通膨/壓力導向 (越高代表壓力或緊縮)
    keywords: tuple[str, ...]


# 宏觀發布事件標準註冊表
MACRO_EVENT_REGISTRY: tuple[MacroEventDefinition, ...] = (
    MacroEventDefinition(
        event_key="CPI_MOM",
        name_zh="消費者物價指數 (MoM)",
        growth_sign=-1,
        keywords=("CPI MoM", "Consumer Price Index MoM", "CPI (MoM)"),
    ),
    MacroEventDefinition(
        event_key="CORE_CPI_MOM",
        name_zh="核心消費者物價指數 (MoM)",
        growth_sign=-1,
        keywords=("Core CPI MoM", "Core Consumer Price Index MoM", "Core CPI (MoM)"),
    ),
    MacroEventDefinition(
        event_key="CPI_YOY",
        name_zh="消費者物價指數 (YoY)",
        growth_sign=-1,
        keywords=("CPI YoY", "Consumer Price Index YoY", "CPI (YoY)"),
    ),
    MacroEventDefinition(
        event_key="CORE_CPI_YOY",
        name_zh="核心消費者物價指數 (YoY)",
        growth_sign=-1,
        keywords=("Core CPI YoY", "Core Consumer Price Index YoY", "Core CPI (YoY)"),
    ),
    MacroEventDefinition(
        event_key="NFP",
        name_zh="非農就業人口變動",
        growth_sign=1,
        keywords=("Non Farm Payrolls", "Nonfarm Payrolls", "NFP"),
    ),
    MacroEventDefinition(
        event_key="UNEMPLOYMENT_RATE",
        name_zh="失業率",
        growth_sign=-1,
        keywords=("Unemployment Rate",),
    ),
    MacroEventDefinition(
        event_key="RETAIL_SALES_MOM",
        name_zh="零售銷售月增率",
        growth_sign=1,
        keywords=("Retail Sales MoM", "Retail Sales (MoM)"),
    ),
    MacroEventDefinition(
        event_key="GDP_QOQ",
        name_zh="實質 GDP 季增年率",
        growth_sign=1,
        keywords=("GDP QoQ", "Gross Domestic Product QoQ", "GDP (QoQ)"),
    ),
    MacroEventDefinition(
        event_key="ISM_MANUFACTURING",
        name_zh="ISM 製造業採購經理人指數",
        growth_sign=1,
        keywords=("ISM Manufacturing PMI", "ISM Manufacturing"),
    ),
    MacroEventDefinition(
        event_key="ISM_SERVICES",
        name_zh="ISM 非製造業採購經理人指數",
        growth_sign=1,
        keywords=("ISM Non-Manufacturing PMI", "ISM Services PMI", "ISM Services"),
    ),
    MacroEventDefinition(
        event_key="PPI_MOM",
        name_zh="生產者物價指數 (MoM)",
        growth_sign=-1,
        keywords=("PPI MoM", "Producer Price Index MoM", "PPI (MoM)"),
    ),
    MacroEventDefinition(
        event_key="INITIAL_CLAIMS",
        name_zh="每週初領失業金人數",
        growth_sign=-1,
        keywords=("Initial Jobless Claims", "Jobless Claims"),
    ),
)


def match_macro_event(raw_event_name: str) -> MacroEventDefinition | None:
    """將日曆中的事件名稱匹配至標準註冊表中的總經事件。"""
    normalized = raw_event_name.strip().lower()
    for defn in MACRO_EVENT_REGISTRY:
        for kw in defn.keywords:
            if kw.lower() in normalized:
                return defn
    return None


def calculate_sample_std(diffs: Sequence[float]) -> float | None:
    """計算樣本標準差 (自由度 N - 1)。若有效樣本數 < 2 則回傳 None。"""
    clean_diffs = [
        float(x)
        for x in diffs
        if x is not None and not math.isnan(x) and not math.isinf(x)
    ]
    n = len(clean_diffs)
    if n < 2:
        return None
    mean = sum(clean_diffs) / n
    variance = sum((x - mean) ** 2 for x in clean_diffs) / (n - 1)
    if variance < 0:
        return 0.0
    return math.sqrt(variance)


def calculate_standardized_surprise(
    actual: float,
    forecast: float,
    historical_diffs: Sequence[float],
) -> tuple[float, float | None]:
    """計算標準化預期差 Z 分數。

    - 原始差值 raw_diff = actual - forecast
    - 歷史預期差樣本標準差 sigma_12：取最近最多 12 期歷史樣本
    - 若歷史樣本數 N < 6，則不計算 Z 分數，回傳 (raw_diff, None)
    - 離群值箝制：z in [-4.0, +4.0]

    回傳：(raw_diff, z_score)
    """
    if (
        math.isnan(actual)
        or math.isinf(actual)
        or math.isnan(forecast)
        or math.isinf(forecast)
    ):
        return (0.0, None)

    raw_diff = actual - forecast
    # 取最近最多 12 筆歷史樣本，排除非有限數值
    clean_history = [
        float(x)
        for x in historical_diffs
        if x is not None and not math.isnan(x) and not math.isinf(x)
    ]
    samples = clean_history[-MAX_SURPRISE_LOOKBACK:]
    if len(samples) < MIN_SURPRISE_SAMPLES:
        return (raw_diff, None)

    sigma = calculate_sample_std(samples)
    if sigma is None or math.isnan(sigma) or sigma <= EPSILON_STD:
        return (raw_diff, None)

    z = raw_diff / sigma
    if math.isnan(z) or math.isinf(z):
        return (raw_diff, None)
    z_clipped = min(max(z, Z_SCORE_CLIP_MIN), Z_SCORE_CLIP_MAX)
    return (raw_diff, z_clipped)
