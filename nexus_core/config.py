from typing import Any
import os
import math
from typing import List, Literal, TypedDict, Optional
from dotenv import load_dotenv

# 載入 .env 檔案（若非正式環境）
if os.getenv("NEXUS_ENV") != "production":
    load_dotenv()


class VixTier(TypedDict):
    name: str
    vix_floor: float
    vix_ceil: float
    allow_signal: bool
    sto_delta_cap: float
    sizing_multiplier: float
    # 方向性做空 (Long Put / Bear Call Spread / 空頭現貨) 專用倉位乘數。
    # 賣方係數 (sizing_multiplier) 隨 VIX 單調遞增到 2.0，因為高隱波是賣方的
    # 權利金溢價來源；但對追空而言，VIX 極端區是投降賣壓與政策干預引發暴力
    # 軋空的區域。故做空採「保守倒 U 形」：中段 (18–30) 最大 1.0，兩端收斂，
    # VIX >= 35 直接為 0 (禁止新開空單)。上限 1.0，校準前不放大。
    short_sizing_multiplier: float
    kelly_fraction_override: Optional[float]
    vtr_entry_allowed: bool
    emoji: str
    color_hex: int


def get_env_or_secret(key: Any, default: Any = None):  # type: ignore
    """取得環境變數，若不存在則嘗試從 Docker Secret 讀取。"""
    value = os.getenv(key)
    if value:
        return value

    # 嘗試從 Docker Secret 讀取 (/run/secrets/<key>)
    secret_path = f"/run/secrets/{key}"
    if os.path.exists(secret_path):
        try:
            with open(secret_path, "r") as f:
                return f.read().strip()
        except Exception:
            pass

    return default


DISCORD_TOKEN = get_env_or_secret("DISCORD_TOKEN")
_raw_admin_id = get_env_or_secret("DISCORD_ADMIN_USER_ID", 0)
try:
    DISCORD_ADMIN_USER_ID = int(_raw_admin_id)
except ValueError:
    DISCORD_ADMIN_USER_ID = 0
LOG_LEVEL = get_env_or_secret("LOG_LEVEL", "WARNING").upper()

# 系統與模型參數
RISK_FREE_RATE = 0.042
DB_NAME = get_env_or_secret("NEXUS_DB_NAME", "data/nexus_data.db")
LLM_API_BASE = get_env_or_secret("LLM_API_BASE", None)
LLM_MODEL_NAME = get_env_or_secret("LLM_MODEL_NAME", None)
API_KEY = get_env_or_secret("API_KEY", None)
TUNNEL_URL = get_env_or_secret("TUNNEL_URL", "")
FINNHUB_API_KEY = get_env_or_secret("FINNHUB_API_KEY", "")

# 記憶體與 Swap 緊急警報門檻 (百分比)
try:
    MEMORY_ALERT_THRESHOLD = float(get_env_or_secret("MEMORY_ALERT_THRESHOLD", 90.0))
except (ValueError, TypeError):
    MEMORY_ALERT_THRESHOLD = 90.0

try:
    MEMORY_SWAP_ALERT_THRESHOLD = float(
        get_env_or_secret("MEMORY_SWAP_ALERT_THRESHOLD", 50.0)
    )
except (ValueError, TypeError):
    MEMORY_SWAP_ALERT_THRESHOLD = 50.0

try:
    MEMORY_SWAP_CRITICAL_THRESHOLD = float(
        get_env_or_secret("MEMORY_SWAP_CRITICAL_THRESHOLD", 80.0)
    )
except (ValueError, TypeError):
    MEMORY_SWAP_CRITICAL_THRESHOLD = 80.0

# 動態轉倉引擎：真實期權持倉併入 15 分鐘評估迴圈 (Feature Flag)
# 預設關閉，避免 OPTIONS 快速通道/流動性警告等「首次真正被生產環境觸發」的
# 分支在未經觀察期前意外對使用者發送大量清倉指令。開啟後預設仍為 dry-run
# (僅記錄不推播)，須另外關閉 OPTIONS_ROLLOVER_DRY_RUN 才會實際發送 DM。
ENABLE_OPTIONS_ROLLOVER_INGESTION = (
    get_env_or_secret("ENABLE_OPTIONS_ROLLOVER_INGESTION", "false").lower() == "true"
)
OPTIONS_ROLLOVER_DRY_RUN = (
    get_env_or_secret("OPTIONS_ROLLOVER_DRY_RUN", "true").lower() == "true"
)
# SHORT_ENTRY 做空進場訊號 dry-run (預設開啟)：只寫入 rollover_audit_log 稽核
# 軌跡、不推播 DM。做空進場的倉位與 VIX 係數皆未經回測校準，觀察稽核紀錄的
# 觸發分佈合理後再關閉。
SHORT_ENTRY_DRY_RUN = get_env_or_secret("SHORT_ENTRY_DRY_RUN", "true").lower() == "true"
# Regime／進場鐵律評估紀錄的前向蒐集 (market_analysis/evaluation_recorder.py)。
# GEX 相關門檻無法回測，這是唯一的校準資料來源，預設開啟 (~260 列/日)。
ENABLE_REGIME_EVALUATION_LOG = (
    get_env_or_secret("ENABLE_REGIME_EVALUATION_LOG", "true").lower() == "true"
)

# 策略目標 Delta 參數
TARGET_DELTAS = {"STO_PUT": -0.20, "STO_CALL": 0.20, "BTO_PUT": -0.50, "BTO_CALL": 0.50}

# ---------------------------------------------------------------------------
# VIX 戰情階梯系統 (VIX Battle Ladder)
# 根據 VIX 即時水位動態調整 STO Delta 上限、倉位大小與 VTR 建倉權限。
# 每個 tier 由 [vix_floor, vix_ceil) 半開區間定義，清單需按 vix_floor 升序排列。
# ---------------------------------------------------------------------------
VIX_LADDER_CONFIG: List[VixTier] = [
    {
        "name": "休兵 (Dormant)",
        "vix_floor": 0.0,
        "vix_ceil": 15.0,
        "allow_signal": False,
        "sto_delta_cap": 0.0,
        "sizing_multiplier": 0.0,
        "short_sizing_multiplier": 0.5,
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
        "short_sizing_multiplier": 0.75,
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
        "short_sizing_multiplier": 1.0,
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
        "short_sizing_multiplier": 1.0,
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
        "short_sizing_multiplier": 0.5,
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
        "short_sizing_multiplier": 0.0,
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


def get_vix_tier(vix_spot: Optional[float]) -> VixTier:
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


# 交易意圖分類 (market_analysis/risk_engine.py::classify_trade_intent 的輸出)。
# 放在 config 而非 risk_engine：risk_engine 已匯入 config，反向匯入會形成循環。
TradeIntent = Literal["PREMIUM_SELL", "DIRECTIONAL_LONG", "DIRECTIONAL_SHORT"]

# VIX 未知時的做空乘數。刻意**不**沿用 get_vix_tier(None) 的 Ready (1.0)：
# 那個預設是為了「資料遺失時不要硬拒所有賣方訊號」，但對做空而言，VIX 抓取
# 失敗絕不能是最寬鬆的情況。
SHORT_VIX_UNKNOWN_MULTIPLIER: float = 0.5


def get_short_vix_multiplier(vix_spot: Optional[float]) -> float:
    """方向性做空的 VIX 倉位乘數 (保守倒 U 形，上限 1.0)。VIX 未知回傳 0.5。"""
    if vix_spot is None or math.isnan(vix_spot) or vix_spot < 0:
        return SHORT_VIX_UNKNOWN_MULTIPLIER
    return float(get_vix_tier(vix_spot)["short_sizing_multiplier"])


def get_vix_sizing_multiplier(vix_spot: Optional[float], intent: TradeIntent) -> float:
    """依交易意圖回傳 VIX 倉位乘數：方向性做空走倒 U 形，其餘沿用賣方階梯。"""
    if intent == "DIRECTIONAL_SHORT":
        return get_short_vix_multiplier(vix_spot)
    return float(get_vix_tier(vix_spot)["sizing_multiplier"])
