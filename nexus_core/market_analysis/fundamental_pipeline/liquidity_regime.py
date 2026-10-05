"""央行淨流動性與體制分類模型（純量化純函式模組）。

參照規格 docs/macro_sentiment/05_liquidity_regime_and_macro_surprise.md：
1. 央行淨流動性 (Fed Net Liquidity): WALCL - WTREGEN - RRPONTSYD (十億美元單位)
2. 13 週季化動態變更率
3. 流動性三態 (EASY / NEUTRAL / TIGHT / UNKNOWN) 判定
4. 基於芝加哥聯準會金融狀況指數 (NFCI) 的動態股權風險溢價 (Dynamic ERP)
"""

from __future__ import annotations

import math

from market_analysis.fundamental_pipeline.models import LiquidityRegime

# 具名量化常數
DEFAULT_BASE_ERP: float = 0.045  # 4.5% 基準股權風險溢價
DEFAULT_LAMBDA_NFCI: float = 0.010  # NFCI 敏感度因子 (每 1 個標準差擾動 100 bps)
NFCI_TIGHT_THRESHOLD: float = 0.0  # NFCI >= 0.0 視為緊縮
NET_LIQ_13W_TIGHT_PCT: float = -3.0  # 13 週變更率 <= -3.0% 視為緊縮
NFCI_EASY_THRESHOLD: float = -0.5  # NFCI <= -0.5 且變更率 >= 0% 視為寬鬆
NET_LIQ_13W_EASY_PCT: float = 0.0
NFCI_CLIP_MIN: float = -1.0  # ERP 計算之 NFCI 箝制下限
NFCI_CLIP_MAX: float = 2.0  # ERP 計算之 NFCI 箝制上限


def calculate_net_liquidity(
    walcl_mil: float, wtregen_mil: float, rrp_bn: float
) -> float:
    """計算央行淨流動性（單位：十億美元）。

    WALCL 與 WTREGEN 由 FRED 提供，單位為百萬美元 (Millions of Dollars)；
    RRPONTSYD 單位為十億美元 (Billions of Dollars)。
    """
    return (walcl_mil / 1000.0) - (wtregen_mil / 1000.0) - rrp_bn


def calculate_13w_change(current: float, base_13w_ago: float | None) -> float | None:
    """計算 13 週 (約一季) 動態變更百分比。若基期數值缺失、非有限或為 0 則回傳 None。"""
    if (
        base_13w_ago is None
        or math.isnan(current)
        or math.isinf(current)
        or math.isnan(base_13w_ago)
        or math.isinf(base_13w_ago)
        or abs(base_13w_ago) < 1e-9
    ):
        return None
    return ((current - base_13w_ago) / abs(base_13w_ago)) * 100.0


def classify_liquidity_regime(
    nfci: float | None, net_liq_chg_13w: float | None
) -> LiquidityRegime:
    """根據 NFCI 與 13 週淨流動性動態變更率判定流動性體制。

    - TIGHT: NFCI >= 0.0 或 13w 變更率 <= -3.0%
    - EASY: NFCI <= -0.5 且 13w 變更率 >= 0.0%
    - NEUTRAL: 其他正常情況
    - UNKNOWN: 關鍵觀測值缺失或非有限數值
    """
    if (
        nfci is None
        or net_liq_chg_13w is None
        or math.isnan(nfci)
        or math.isinf(nfci)
        or math.isnan(net_liq_chg_13w)
        or math.isinf(net_liq_chg_13w)
    ):
        return "UNKNOWN"
    if nfci >= NFCI_TIGHT_THRESHOLD or net_liq_chg_13w <= NET_LIQ_13W_TIGHT_PCT:
        return "TIGHT"
    if nfci <= NFCI_EASY_THRESHOLD and net_liq_chg_13w >= NET_LIQ_13W_EASY_PCT:
        return "EASY"
    return "NEUTRAL"


def calculate_dynamic_erp(
    nfci: float | None,
    base_erp: float = DEFAULT_BASE_ERP,
    lambda_nfci: float = DEFAULT_LAMBDA_NFCI,
) -> float | None:
    """計算經 NFCI 線性擾動校準後的動態股權風險溢價 (ERP)。

    ERP_t = ERP_BASE + lambda_NFCI * clip(NFCI_t, -1.0, +2.0)
    若 NFCI 缺失或非有限則回傳 None。
    """
    if nfci is None or math.isnan(nfci) or math.isinf(nfci):
        return None
    clipped_nfci = min(max(nfci, NFCI_CLIP_MIN), NFCI_CLIP_MAX)
    return base_erp + lambda_nfci * clipped_nfci
