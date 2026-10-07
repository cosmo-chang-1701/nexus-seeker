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
    """宏觀發布事件規格定義。

    - `names_zh`：`economic_calendar_events.event` 實際儲存的中文全名，即 Edge
      Scraper (`nexus_edge_scraper/local_api/macro_calendar.py` TRANSLATIONS) 與
      `market_analysis/macro_calendar_translator.py` 翻譯後的輸出，為主要比對依據。
    - `aliases_en`：TradingView 原始英文名稱，僅作為未翻譯資料的備援。
    兩者皆以「正規化後精確相等」比對（見 `match_macro_event`），不做子字串比對。
    """

    event_key: str
    name_zh: str
    growth_sign: int  # 1: 增長導向 (越高代表景氣強勁), -1: 緊縮/通膨/壓力導向 (越高代表壓力或緊縮)
    names_zh: tuple[str, ...]
    aliases_en: tuple[str, ...] = ()


# 宏觀發布事件標準註冊表
MACRO_EVENT_REGISTRY: tuple[MacroEventDefinition, ...] = (
    MacroEventDefinition(
        event_key="CPI_MOM",
        name_zh="消費者物價指數 (MoM)",
        growth_sign=-1,
        names_zh=("CPI 月增率",),
        aliases_en=("CPI MoM", "Inflation Rate MoM", "Consumer Price Index MoM"),
    ),
    MacroEventDefinition(
        event_key="CORE_CPI_MOM",
        name_zh="核心消費者物價指數 (MoM)",
        growth_sign=-1,
        names_zh=("核心 CPI 月增率",),
        aliases_en=("Core CPI MoM", "Core Inflation Rate MoM"),
    ),
    MacroEventDefinition(
        event_key="CPI_YOY",
        name_zh="消費者物價指數 (YoY)",
        growth_sign=-1,
        names_zh=("CPI 年增率",),
        aliases_en=("CPI YoY", "Inflation Rate YoY", "Consumer Price Index YoY"),
    ),
    MacroEventDefinition(
        event_key="CORE_CPI_YOY",
        name_zh="核心消費者物價指數 (YoY)",
        growth_sign=-1,
        names_zh=("核心 CPI 年增率",),
        aliases_en=("Core CPI YoY", "Core Inflation Rate YoY"),
    ),
    MacroEventDefinition(
        event_key="NFP",
        name_zh="非農就業人口變動",
        growth_sign=1,
        names_zh=("非農就業人數",),
        aliases_en=("Non Farm Payrolls", "Non-Farm Payrolls", "Nonfarm Payrolls"),
    ),
    MacroEventDefinition(
        event_key="UNEMPLOYMENT_RATE",
        name_zh="失業率",
        growth_sign=-1,
        names_zh=("失業率",),
        aliases_en=("Unemployment Rate",),
    ),
    MacroEventDefinition(
        event_key="RETAIL_SALES_MOM",
        name_zh="零售銷售月增率",
        growth_sign=1,
        names_zh=("零售銷售月增率",),
        aliases_en=("Retail Sales MoM",),
    ),
    MacroEventDefinition(
        event_key="GDP_QOQ",
        name_zh="實質 GDP 季增年率",
        growth_sign=1,
        # 僅收錄初值 (Adv) 與無後綴版本；修訂值 / 終值的共識預期與初值不同序列，
        # 混入同一歷史樣本會扭曲 sigma_12，故刻意不納入。
        names_zh=("GDP 成長率季增年率 (初值)", "GDP 成長率 (季增年率)"),
        aliases_en=("GDP Growth Rate QoQ Adv", "GDP Growth Rate QoQ"),
    ),
    MacroEventDefinition(
        event_key="ISM_MANUFACTURING",
        name_zh="ISM 製造業採購經理人指數",
        growth_sign=1,
        names_zh=("ISM 製造業 PMI",),
        aliases_en=("ISM Manufacturing PMI",),
    ),
    MacroEventDefinition(
        event_key="ISM_SERVICES",
        name_zh="ISM 非製造業採購經理人指數",
        growth_sign=1,
        names_zh=("ISM 服務業 PMI", "ISM 非製造業 PMI"),
        aliases_en=("ISM Services PMI", "ISM Non-Manufacturing PMI"),
    ),
    MacroEventDefinition(
        event_key="PPI_MOM",
        name_zh="生產者物價指數 (MoM)",
        growth_sign=-1,
        names_zh=("PPI 月增率",),
        aliases_en=("PPI MoM", "Producer Price Index MoM"),
    ),
    MacroEventDefinition(
        event_key="INITIAL_CLAIMS",
        name_zh="每週初領失業金人數",
        growth_sign=-1,
        names_zh=("初領失業救濟金人數",),
        aliases_en=("Initial Jobless Claims",),
    ),
)


def _normalize_event_name(raw: str) -> str:
    """正規化事件名稱：去頭尾空白、連續空白收斂為單一空白、轉小寫。"""
    return " ".join(raw.split()).lower()


def _build_event_lookup() -> dict[str, MacroEventDefinition]:
    lookup: dict[str, MacroEventDefinition] = {}
    for defn in MACRO_EVENT_REGISTRY:
        for name in (*defn.names_zh, *defn.aliases_en):
            key = _normalize_event_name(name)
            existing = lookup.get(key)
            if existing is not None and existing.event_key != defn.event_key:
                raise ValueError(
                    f"總經事件名稱 {name!r} 同時對應 {existing.event_key} 與 {defn.event_key}"
                )
            lookup[key] = defn
    return lookup


_EVENT_LOOKUP: dict[str, MacroEventDefinition] = _build_event_lookup()


def match_macro_event(raw_event_name: str) -> MacroEventDefinition | None:
    """將日曆事件名稱以「正規化後精確相等」對應至註冊表中的總經事件。

    刻意不使用子字串比對：「核心 CPI 月增率」包含「CPI 月增率」、「初領失業金
    4 週移動平均」與「初領失業救濟金人數」共享字首，子字串加先到先得會把它們
    錯歸到同一 event_key，並在主鍵 (event_key, release_time_utc) 相撞時互相覆寫。
    """
    if not raw_event_name:
        return None
    return _EVENT_LOOKUP.get(_normalize_event_name(raw_event_name))


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
