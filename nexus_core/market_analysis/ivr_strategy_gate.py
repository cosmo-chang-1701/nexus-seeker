"""
ivr_strategy_gate.py — IVR 策略硬鎖閘門。

集中式 IVR 閘門邏輯：
  - IVR < _IVR_SELLING_LOCKOUT (10%) 時，絕對禁止所有 STO/賣方策略
  - 僅允許現貨操作或 ITM Call BTO (Delta >= 0.70)

所有策略產生出口（strategy.py、option_guidance.py、
execution_router.py、dynamic_rollover.py）應統一引用本模組
的 is_selling_locked_by_ivr() 判定。
"""

import logging

logger = logging.getLogger(__name__)

# ⚠️ 底層硬鎖門檻：低於此值則絕對禁止所有賣方策略
_IVR_SELLING_LOCKOUT: float = 10.0

# 允許的買方策略最低 Delta（ITM Call）
_ITM_CALL_MIN_DELTA: float = 0.70


def is_selling_locked_by_ivr(ivr: float) -> bool:
    """
    判斷當前 IVR 是否觸發賣方策略鎖死。

    當 IVR 極低時，期權權利金過於廉價，賣方策略（CSP、Covered Call、
    Credit Spread）的 risk/reward 嚴重不利：收取的權利金微乎其微，
    卻承擔完整的 Vega 擴張與方向性風險。

    Args:
        ivr: 當前 IV Rank (0.0 ~ 100.0)。注意 ivr == 0.0 可能代表
             數據缺失，此時不觸發鎖死（由其他降級邏輯處理）。

    Returns:
        True 如果 IVR 有效且低於鎖死門檻 (應封鎖所有賣方策略)。
    """
    if ivr <= 0.0:
        # IVR == 0.0 通常代表數據缺失或盤前，不由此閘門處理
        return False
    return ivr < _IVR_SELLING_LOCKOUT


def get_ivr_lockout_allowed_strategies() -> list[str]:
    """返回 IVR 鎖死狀態下允許的策略列表。

    Returns:
        允許策略的字串列表：
        - SPOT_BUY: 現貨買入
        - BTO_CALL_ITM: 價內買方看漲期權 (Delta >= 0.70)
        - DEBIT_SPREAD: 借方價差 (net debit，非賣方策略)
    """
    return [
        "SPOT_BUY",
        "BTO_CALL_ITM",
        "DEBIT_SPREAD",
    ]
