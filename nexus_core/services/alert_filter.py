"""
AlertFilter — 條件式訊號降噪引擎 (Async)。
"""

import time
import logging
import asyncio
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
    trigger_signal: str
    confirmed_direction: TrendState

VIX_CHANGE_THRESHOLD = 0.10
VRP_PREMIUM_THRESHOLD = 0.05
WHIPSAW_COOLDOWN_SECONDS = 14400
WHIPSAW_PRICE_BAND_PCT = 0.015

def is_whipsaw_noise(symbol: str, current_price: float, current_dir: str, last_state: Optional[Dict[str, Any]]) -> bool:
    if not last_state or not last_state.get('last_cross_time'): return False
    last_dir, last_price, last_time = last_state['last_cross_dir'], last_state['last_cross_price'], last_state['last_cross_time']
    now = time.time()
    if current_dir == last_dir and (now - last_time) < WHIPSAW_COOLDOWN_SECONDS:
        logger.info(f"🛡️ [AntiWhipsaw] {symbol} 同方向 ({current_dir}) 訊號被抑制")
        return True
    if last_price and last_price > 0 and abs(current_price - last_price) / last_price < WHIPSAW_PRICE_BAND_PCT:
        logger.info(f"🛡️ [AntiWhipsaw] {symbol} 價格偏離不足，視為均線糾纏噪音")
        return True
    return False

async def validate_mtf_trend(symbol: str, trigger_sig: Dict[str, Any]) -> MTFResult:
    """執行多週期趨勢確認邏輯 (Trend Resonance)。"""
    df_daily = await market_data_service.get_history_df(symbol, interval="1d", period="60d")
    if df_daily.empty or len(df_daily) < 22:
        return MTFResult(False, TrendState.NEUTRAL, "NONE", TrendState.NEUTRAL)
    ema21_daily = df_daily['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
    price_daily = df_daily['Close'].iloc[-1]
    anchor_trend = TrendState.BULLISH if price_daily > ema21_daily else TrendState.BEARISH
    trigger_dir = TrendState.BULLISH if trigger_sig.get('direction') == "BULLISH" else TrendState.BEARISH
    is_aligned = (anchor_trend == trigger_dir)
    return MTFResult(is_aligned=is_aligned, anchor_trend=anchor_trend, trigger_signal=trigger_sig.get('type', 'UNKNOWN'), confirmed_direction=trigger_dir if is_aligned else TrendState.NEUTRAL)

async def should_send_priority_alert(result: Dict[str, Any], prev_macro: Optional[Dict[str, Any]] = None, last_alert_state: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """判定單筆掃描結果是否符合「緊急推播」條件 (Async)。"""
    symbol = result.get('symbol', 'N/A')
    if prev_macro is not None:
        current_vix, prev_vix = result.get('macro_vix', 18.0), prev_macro.get('vix', 18.0)
        if prev_vix > 0 and abs(current_vix - prev_vix) / prev_vix >= VIX_CHANGE_THRESHOLD:
            reason = f"🌪️ 宏觀波動：VIX 變動達 {abs(current_vix - prev_vix) / prev_vix:.1%}"
            return True, reason

    ema_signals, current_price = result.get('ema_signals', []), result.get('price', 0.0)
    for sig in ema_signals:
        if sig.get('type') == "CROSSOVER":
            if is_whipsaw_noise(symbol, current_price, sig.get('direction', 'UNKNOWN'), last_alert_state): continue
            mtf = await validate_mtf_trend(symbol, sig)
            if mtf.is_aligned:
                dir_cn = "多頭" if mtf.confirmed_direction == TrendState.BULLISH else "空頭"
                return True, f"🔥 **時框共振確認**：{symbol} 於大週期趨勢中觸發 {dir_cn} 突破"

    ai_decision, vrp = result.get('ai_decision', ''), result.get('vrp', 0)
    if ai_decision == 'APPROVE' and vrp > VRP_PREMIUM_THRESHOLD:
        return True, f"💰 高勝率機會：VRP 溢酬達 {vrp:.1%}"
    return False, ""
