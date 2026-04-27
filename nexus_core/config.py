import os
from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_ADMIN_USER_ID = int(os.getenv("DISCORD_ADMIN_USER_ID", 0))
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()

# 系統與模型參數
RISK_FREE_RATE = 0.042
DB_NAME = "data/nexus_data.db"
LLM_API_BASE = os.getenv("LLM_API_BASE", None)
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", None)
API_KEY = os.getenv("API_KEY", None)
TUNNEL_URL = os.getenv("TUNNEL_URL", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# 策略目標 Delta 參數
TARGET_DELTAS = {
    "STO_PUT": -0.20,
    "STO_CALL": 0.20,
    "BTO_PUT": -0.50,
    "BTO_CALL": 0.50
}

# ---------------------------------------------------------------------------
# VIX 戰情階梯系統 (VIX Battle Ladder)
# 根據 VIX 即時水位動態調整 STO Delta 上限、倉位大小與 VTR 建倉權限。
# 每個 tier 由 [vix_floor, vix_ceil) 半開區間定義，清單需按 vix_floor 升序排列。
# ---------------------------------------------------------------------------
VIX_LADDER_CONFIG = [
    {
        "name": "休兵 (Dormant)",
        "vix_floor": 0.0,
        "vix_ceil": 15.0,
        "allow_signal": False,
        "sto_delta_cap": 0.0,
        "sizing_multiplier": 0.0,
        "kelly_fraction_override": None,
        "vtr_entry_allowed": False,
        "emoji": "⚪",
        "color_hex": 0x808080,
    },
    {
        "name": "少買 (Caution)",
        "vix_floor": 15.0,
        "vix_ceil": 18.0,
        "allow_signal": True,
        "sto_delta_cap": -0.12,
        "sizing_multiplier": 0.5,
        "kelly_fraction_override": None,
        "vtr_entry_allowed": True,
        "emoji": "🟡",
        "color_hex": 0xFFD700,
    },
    {
        "name": "摩拳擦掌 (Ready)",
        "vix_floor": 18.0,
        "vix_ceil": 24.0,
        "allow_signal": True,
        "sto_delta_cap": -0.20,
        "sizing_multiplier": 1.0,
        "kelly_fraction_override": None,
        "vtr_entry_allowed": True,
        "emoji": "🟠",
        "color_hex": 0xFF8C00,
    },
    {
        "name": "大買 (Aggressive)",
        "vix_floor": 24.0,
        "vix_ceil": 30.0,
        "allow_signal": True,
        "sto_delta_cap": -0.20,
        "sizing_multiplier": 1.2,
        "kelly_fraction_override": None,
        "vtr_entry_allowed": True,
        "emoji": "🔴",
        "color_hex": 0xFF0000,
    },
    {
        "name": "重砲進場 (Heavy)",
        "vix_floor": 30.0,
        "vix_ceil": 35.0,
        "allow_signal": True,
        "sto_delta_cap": -0.25,
        "sizing_multiplier": 1.5,
        "kelly_fraction_override": None,
        "vtr_entry_allowed": True,
        "emoji": "🔴",
        "color_hex": 0xCC0000,
    },
    {
        "name": "All-in (Extreme)",
        "vix_floor": 35.0,
        "vix_ceil": 999.0,
        "allow_signal": True,
        "sto_delta_cap": -0.35,
        "sizing_multiplier": 2.0,
        "kelly_fraction_override": 0.50,
        "vtr_entry_allowed": True,
        "emoji": "🟥",
        "color_hex": 0x8B0000,
    },
]

# VIX 歷史分位數邊界 (供 PSQ 動能標記使用)
VIX_QUANTILE_BOUNDS = {
    "lower_10": 13.9,
    "lower_4": 15.3,
    "lower_3": 16.8,
    "upper_3": 24.6,
    "upper_4": 26.1,
    "upper_10": 29.5,
}


import math


def get_vix_tier(vix_spot: float) -> dict:
    """根據 VIX 即時價格回傳對應的戰情階梯 tier 配置。

    若 vix_spot 為 None、NaN 或無效值，回傳 Ready 階梯作為安全預設值。
    """
    if vix_spot is None or math.isnan(vix_spot) or vix_spot < 0:
        # 預設回傳 Ready 階梯 (index=2)，避免因資料遺失而硬拒所有訊號
        return VIX_LADDER_CONFIG[2]

    for tier in VIX_LADDER_CONFIG:
        if tier["vix_floor"] <= vix_spot < tier["vix_ceil"]:
            return tier

    # Fallback: 若 vix_spot 超出所有範圍 (理論上不會發生)
    return VIX_LADDER_CONFIG[-1]