"""
AlertFilter — 條件式訊號降噪引擎。

作為推播前的最後一道關卡，過濾低價值雜訊，
僅在符合「高衝擊事件」條件時才觸發 Discord 通知。

三大過濾維度：
1. 宏觀衝擊 (Macro Shock): VIX 變動超過閾值
2. 技術突破 (Technical Breakout): EMA 金叉/死叉 (CROSSOVER)
3. 風控預警 (Risk Opportunity): AI APPROVE + VRP 高溢酬

防騙線 (Anti-Whipsaw) — 針對 EMA CROSSOVER 的二階過濾：
- 時間門檻：同方向訊號在冷卻期內不重複發送
- 價格門檻：價格偏離不足時視為均線糾纏，抑制通知
"""

import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any, Tuple, Optional

from services import market_data_service

logger = logging.getLogger(__name__)

class TrendState(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

@dataclass
class MTFResult:
    is_aligned: bool
    anchor_trend: TrendState
    trigger_signal: str # e.g., "CROSSOVER"
    confirmed_direction: TrendState

# --- 可調整的過濾閾值 (集中管理，方便未來抽成 config) ---
VIX_CHANGE_THRESHOLD = 0.10   # VIX 變動超過 10% 視為宏觀衝擊
VRP_PREMIUM_THRESHOLD = 0.05  # VRP 溢酬超過 5% 視為高勝率賣方機會

# --- 防騙線 (Anti-Whipsaw) 參數 ---
WHIPSAW_COOLDOWN_SECONDS = 14400  # 同方向訊號冷卻期 (4 小時)
WHIPSAW_PRICE_BAND_PCT = 0.015    # 價格偏離門檻 (1.5%)


def is_whipsaw_noise(
    symbol: str,
    current_price: float,
    current_dir: str,
    last_state: Optional[Dict[str, Any]],
) -> bool:
    """
    判定目前的 CROSSOVER 訊號是否為騙線噪音 (Whipsaw Noise)。

    兩道防線：
    1. 時間過濾：同方向訊號在冷卻期 (預設 4 小時) 內直接抑制。
    2. 價格過濾：價格與上次觸發價差距不足門檻 (預設 1.5%) 時，
       視為均線糾纏 (EMA 附近的無效震盪)，抑制通知。

    Args:
        symbol: 標的代號 (僅用於日誌)。
        current_price: 當前標的價格。
        current_dir: 本次穿透方向 ("BULLISH" 或 "BEARISH")。
        last_state: 上次觸發狀態字典，包含：
                    - last_cross_dir (str)
                    - last_cross_price (float)
                    - last_cross_time (int, Unix Timestamp)
                    若為 None 或缺少 last_cross_time，表示首次觸發，直接放行。

    Returns:
        bool: True 表示是騙線噪音，應抑制推播；False 表示放行。
    """
    # 首次觸發 — 無歷史紀錄，直接放行
    if not last_state or not last_state.get('last_cross_time'):
        return False

    last_dir = last_state['last_cross_dir']
    last_price = last_state['last_cross_price']
    last_time = last_state['last_cross_time']
    now = time.time()

    # 防線 1：時間過濾 — 同方向訊號在冷卻期內直接抑制
    if current_dir == last_dir and (now - last_time) < WHIPSAW_COOLDOWN_SECONDS:
        elapsed_min = (now - last_time) / 60
        logger.info(
            f"🛡️ [AntiWhipsaw] {symbol} 同方向 ({current_dir}) 訊號被抑制 "
            f"— 距上次僅 {elapsed_min:.0f} 分鐘 (門檻: {WHIPSAW_COOLDOWN_SECONDS // 3600} 小時)"
        )
        return True

    # 防線 2：價格過濾 — 價格偏離不足，視為均線糾纏
    if last_price and last_price > 0:
        price_diff_pct = abs(current_price - last_price) / last_price
        if price_diff_pct < WHIPSAW_PRICE_BAND_PCT:
            logger.info(
                f"🛡️ [AntiWhipsaw] {symbol} 價格偏離僅 {price_diff_pct:.2%} "
                f"(門檻: {WHIPSAW_PRICE_BAND_PCT:.1%})，視為均線糾纏噪音，已抑制。"
            )
            return True

    return False


def validate_mtf_trend(symbol: str, trigger_sig: Dict[str, Any]) -> MTFResult:
    """
    執行多週期趨勢確認邏輯 (Trend Resonance)。
    判定大週期 (Daily) EMA 趨勢與小週期 (1-Hour) 突破訊號是否方向一致。
    
    Args:
        symbol: 標的代號。
        trigger_sig: 小週期的 EMA 訊號 (由 detect_ema_signals 產生)。
        
    Returns:
        MTFResult: 包含是否對齊及確認方向的結果。
    """
    # 1. 獲取大週期 (Daily) 數據作為錨定趨勢
    df_daily = market_data_service.get_history_df(symbol, interval="1d", period="60d")
    if df_daily.empty or len(df_daily) < 22:
        return MTFResult(False, TrendState.NEUTRAL, "NONE", TrendState.NEUTRAL)

    # 計算 Daily EMA 21
    ema21_daily_series = df_daily['Close'].ewm(span=21, adjust=False).mean()
    ema21_daily = ema21_daily_series.iloc[-1]
    price_daily = df_daily['Close'].iloc[-1]

    # 2. 判定大週期趨勢狀態 (Anchor Trend)
    # 多頭：Price > EMA21 ; 空頭：Price < EMA21
    anchor_trend = TrendState.BULLISH if price_daily > ema21_daily else TrendState.BEARISH
    
    # 3. 判定小週期訊號方向 (Trigger Signal)
    trigger_dir = TrendState.BULLISH if trigger_sig.get('direction') == "BULLISH" else TrendState.BEARISH

    # 4. 執行共振校驗 (Alignment Check)
    is_aligned = (anchor_trend == trigger_dir)

    return MTFResult(
        is_aligned=is_aligned,
        anchor_trend=anchor_trend,
        trigger_signal=trigger_sig.get('type', 'UNKNOWN'),
        confirmed_direction=trigger_dir if is_aligned else TrendState.NEUTRAL
    )


def should_send_priority_alert(
    result: Dict[str, Any],
    prev_macro: Optional[Dict[str, Any]] = None,
    last_alert_state: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    判定單筆掃描結果是否符合「緊急推播」條件。

    Args:
        result: 單筆標的掃描結果字典，需包含 ema_signals, ai_decision, vrp 等欄位。
        prev_macro: 前一輪掃描的宏觀環境快照 (含 vix)。
                     若為 None 則跳過宏觀衝擊判定。
        last_alert_state: 該標的上次觸發 CROSSOVER 的狀態 (來自資料庫)。
                          傳入後自動啟用防騙線過濾。若為 None 則跳過防騙線判定。

    Returns:
        Tuple[bool, str]: (是否推播, 推播理由)。
                          若不推播，理由為空字串。
    """
    symbol = result.get('symbol', 'N/A')

    # --- 維度 1: 宏觀衝擊 — VIX 單日劇烈變動 ---
    if prev_macro is not None:
        current_vix = result.get('macro_vix', 18.0)
        prev_vix = prev_macro.get('vix', 18.0)

        if prev_vix > 0:
            vix_change = abs(current_vix - prev_vix) / prev_vix
            if vix_change >= VIX_CHANGE_THRESHOLD:
                reason = f"🌪️ 宏觀波動：VIX 變動達 {vix_change:.1%} ({prev_vix:.1f} → {current_vix:.1f})"
                logger.info(f"[AlertFilter] PASS — {reason}")
                return True, reason

    # --- 維度 2: 技術突破 — EMA 金叉/死叉 (CROSSOVER) + 防騙線 ---
    ema_signals = result.get('ema_signals', [])
    current_price = result.get('price', 0.0)

    for sig in ema_signals:
        if sig.get('type') == "CROSSOVER":
            direction = sig.get('direction', 'UNKNOWN')

            # 🛡️ 防騙線二階過濾 (Anti-Whipsaw)
            if is_whipsaw_noise(symbol, current_price, direction, last_alert_state):
                logger.info(f"[AlertFilter] WHIPSAW — {symbol} CROSSOVER ({direction}) 被防騙線攔截。")
                continue

            # 🚀 執行多週期確認 (MTF Resonance)
            mtf = validate_mtf_trend(symbol, sig)
            if mtf.is_aligned:
                dir_cn = "多頭" if mtf.confirmed_direction == TrendState.BULLISH else "空頭"
                reason = f"🔥 **時框共振確認**：{symbol} 於大週期趨勢中觸發 {dir_cn} 突破"
                logger.info(f"[AlertFilter] PASS — {reason}")
                return True, reason
            else:
                logger.info(f"[AlertFilter] MTF_MISALIGN — {symbol} 觸發小週期突破，但與 Daily 趨勢不符，已抑制。")
                continue

    # --- 維度 3: 風控預警 — AI APPROVE + VRP 高溢酬 ---
    ai_decision = result.get('ai_decision', '')
    if ai_decision == 'APPROVE':
        vrp = result.get('vrp', 0)
        if vrp > VRP_PREMIUM_THRESHOLD:
            reason = f"💰 高勝率機會：VRP 溢酬達 {vrp:.1%}"
            logger.info(f"[AlertFilter] PASS — {reason}")
            return True, reason

    # --- 全部未通過：靜默處理 ---
    logger.debug(
        f"[AlertFilter] SKIP — {symbol} "
        f"(VIX=穩定, EMA=無交叉, VRP={result.get('vrp', 0):.1%})"
    )
    return False, ""
