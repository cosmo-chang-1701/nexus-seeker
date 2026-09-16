from typing import Any, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid
from market_analysis.room_threshold import resolve_atr_15m

from . import logger


def format_illiquidity_warning(bid: float, ask: float) -> Optional[str]:
    """流動性閘門警示文字格式化：期權部位若帶有 bid/ask 且點差過寬
    (is_spread_illiquid) 時，回傳附加於 reason 文字的警示片段；否則回傳
    None。判斷閾值本身沿用既有的 is_spread_illiquid，本函式僅負責文字
    格式化，避免各情境模組各自重複維護同一段警示字串 (opportunity_cost.py /
    margin_defense.py / core_deployment.py 曾各自維護逐字相同的片段)。
    呼叫端仍需自行判斷 asset_class == "OPTIONS" 等前置條件是否成立。
    """
    if not is_spread_illiquid(bid, ask):
        return None
    spread_pct = (ask - bid) / ((ask + bid) / 2)
    return (
        f"\n⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
        f"點差 {spread_pct:.1%})，建議採限價單並留意滑價。"
    )


def format_cash_impact(recovered_cash: float) -> Optional[str]:
    """資金影響金額字串格式化：正值回傳千分位美元字串，否則回傳 None
    (供 instruction dict 的 cash_impact 欄位與 database.log_rollover_instruction
    直接使用)。"""
    return f"${recovered_cash:,.0f}" if recovered_cash > 0 else None


def resolve_current_value(current_value: float, quantity: float, spot: float) -> float:
    """持倉市值解析：優先採用既有 current_value，缺失 (<=0) 時退回
    quantity * spot 估算。呼叫端須自行決定傳入的 quantity 是否已取絕對值
    (例如空頭期權部位的負股數)，本函式不代為判斷正負號語意。"""
    if current_value <= 0 and spot > 0:
        return quantity * spot
    return current_value


async def resolve_room_threshold_inputs(
    symbol: str,
    candidate_radar: Optional[dict],
    gex_profile_data: Any,
    df_15m: Optional[Any] = None,
) -> Tuple[float, float, float]:
    """解析動態空間門檻 (room_threshold.py 公式 A/B/C) 所需的三項共用輸入。

    回傳 ``(put_wall, atr_15m, atr_1d)``，全部為絕對價格量綱；任何一項解析失敗
    一律回 ``0.0``，交由 room_threshold 的降級階梯處理（它會把該項自 max() 剔除
    並標記 is_degraded），本函式不拋例外、不做判定。

    取數優先序（刻意「先用手上已有的，最後才發網路請求」）：

    * ``put_wall``：``gex_profile_data["put_wall"]``——呼叫端在六重鐵律開始前
      早已解析過同一份 dict，零額外成本。
    * ``atr_15m``：呼叫端傳入的 ``df_15m`` 就地計算 > radar 快取的
      ``atr_15m``（radar_data.py 走的是真實 ``fetch_atr_15m()``）> 由 radar 的
      日線 ``atr_14`` 依 √26 折算。**不**在此發動 ``fetch_atr_15m()``——該函式
      預設 ``force_refresh=True``，在每輪次對每個候選標的都重抓一次成本過高，
      且會與條件一手上的 K 棒取到不同快照。
    * ``atr_1d``：radar 快取的 ``atr_14`` > ``fetch_atr_1d()``（走
      ``get_history_df`` 的既有日線快取，非 force_refresh）。

    ⚠️ radar 快取的 ``atr_14`` 有可能是 ``EnhancedWatchlistMetrics`` 那條路徑寫
    入的 0.01 佔位值（該欄位 ``gt=0.0`` 無法寫 0），``resolve_atr_15m()`` 內建
    了對該佔位值的排除；``atr_1d`` 這一側則在下方顯式比對。
    """
    radar = candidate_radar or {}

    put_wall = 0.0
    if isinstance(gex_profile_data, dict):
        try:
            put_wall = float(gex_profile_data.get("put_wall", 0.0) or 0.0)
        except (ValueError, TypeError):
            put_wall = 0.0

    def _radar_float(key: str) -> float:
        try:
            return float(radar.get(key, 0.0) or 0.0)
        except (ValueError, TypeError):
            return 0.0

    atr_14 = _radar_float("atr_14")
    # 0.01 是 EnhancedWatchlistMetrics.atr_14 取不到日線 ATR 時的佔位值，
    # 直接採用會產生一個 $0.015 的假緩衝，須視為缺失。
    if atr_14 <= 0.01:
        atr_14 = 0.0

    atr_15m = 0.0
    if df_15m is not None:
        # 與條件一手上的是同一份 frame，ATR 與 K 棒在建構上保證同源。
        try:
            from market_analysis.atr_utils import compute_atr_15m_from_df

            atr_15m = compute_atr_15m_from_df(df_15m)
        except Exception as e:  # pragma: no cover - 防禦性，計算端已 fail-safe
            logger.warning(f"[{symbol}] 動態門檻 ATR₁₅ₘ 就地計算失敗: {e}")
            atr_15m = 0.0
    if atr_15m <= 0:
        atr_15m = _radar_float("atr_15m")
    atr_15m = resolve_atr_15m(atr_15m, atr_14)

    atr_1d = atr_14
    if atr_1d <= 0:
        try:
            from market_analysis.atr_utils import fetch_atr_1d

            atr_1d = await fetch_atr_1d(symbol)
        except Exception as e:  # pragma: no cover - fetch 端已 fail-safe
            logger.warning(f"[{symbol}] 動態門檻 ATR₁D 抓取失敗: {e}")
            atr_1d = 0.0

    return (put_wall, atr_15m, atr_1d)
