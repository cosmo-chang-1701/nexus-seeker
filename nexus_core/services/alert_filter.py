"""
AlertFilter — 條件式訊號降噪引擎。

作為推播前的最後一道關卡，過濾低價值雜訊，
僅在符合「高衝擊事件」條件時才觸發 Discord 通知。

三大過濾維度：
1. 宏觀衝擊 (Macro Shock): VIX 變動超過閾值
2. 技術突破 (Technical Breakout): EMA 金叉/死叉 (CROSSOVER)
3. 風控預警 (Risk Opportunity): AI APPROVE + VRP 高溢酬
"""

import logging
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)

# --- 可調整的過濾閾值 (集中管理，方便未來抽成 config) ---
VIX_CHANGE_THRESHOLD = 0.10   # VIX 變動超過 10% 視為宏觀衝擊
VRP_PREMIUM_THRESHOLD = 0.05  # VRP 溢酬超過 5% 視為高勝率賣方機會


def should_send_priority_alert(
    result: Dict[str, Any],
    prev_macro: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    判定單筆掃描結果是否符合「緊急推播」條件。

    Args:
        result: 單筆標的掃描結果字典，需包含 ema_signals, ai_decision, vrp 等欄位。
        prev_macro: 前一輪掃描的宏觀環境快照 (含 vix)。
                     若為 None 則跳過宏觀衝擊判定。

    Returns:
        Tuple[bool, str]: (是否推播, 推播理由)。
                          若不推播，理由為空字串。
    """
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

    # --- 維度 2: 技術突破 — EMA 金叉/死叉 (CROSSOVER) ---
    ema_signals = result.get('ema_signals', [])
    for sig in ema_signals:
        if sig.get('type') == "CROSSOVER":
            symbol = result.get('symbol', 'N/A')
            window = sig.get('window', '?')
            reason = f"🚀 趨勢突破：{symbol} 穿透 EMA {window}"
            logger.info(f"[AlertFilter] PASS — {reason}")
            return True, reason

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
        f"[AlertFilter] SKIP — {result.get('symbol', 'N/A')} "
        f"(VIX=穩定, EMA=無交叉, VRP={result.get('vrp', 0):.1%})"
    )
    return False, ""
