from datetime import datetime
from typing import Any, Dict, List

from ._shared import format_cash_impact
from .constants import (
    _COVERED_CALL_PROFIT_LOCK_FULL_DECAY_PCT,
    _COVERED_CALL_PROFIT_LOCK_PARTIAL_DECAY_PCT,
    _COVERED_CALL_PROFIT_LOCK_PARTIAL_RATIO,
    _HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD,
)
from .models import RolloverInstruction, RolloverScenario
from .structural_signals import evaluate_option_dte_tier


class _CoveredCallProfitLockMixin:
    """Covered Call 權利金衰減停利 (Premium Decay Profit-Lock)。

    嚴格限定為既有的空頭 CALL 部位 (Covered Call)；空頭 PUT (CSP) 不在本次
    範圍內，留待未來視需要用同一機制擴充。與 Scenario 2/3/5 的機會成本/
    再平衡/核心部署完全獨立，只回答「既有空頭 CALL 部位是否該提前 BTC 回補
    了結」這一個問題，不涉及任何轉倉/開倉決策，因此不參與 already_flagged_
    symbols 排除邏輯。

    呼叫端 (cogs/trading/portfolio_monitor.py) 需預先透過既有
    market_analysis.portfolio.get_option_chain_mid_iv() 批次抓取每筆部位的
    current_premium 併入 short_call_positions 各筆 dict 再傳入（比照既有
    long_option_trades 的 Semaphore(3) 批次抓取模式，避免序列化 I/O）；本函式
    本身純運算、零額外網路請求。
    """

    async def evaluate_covered_call_profit_lock(
        self,
        user_id: int,
        short_call_positions: List[Dict[str, Any]],
    ) -> List[RolloverInstruction]:
        del user_id  # 目前判斷邏輯不依賴，保留供未來個人化門檻擴充
        instructions: List[RolloverInstruction] = []

        for pos in short_call_positions:
            symbol = str(pos.get("symbol", "")).upper()
            entry_premium = float(pos.get("entry_price", 0.0) or 0.0)
            if entry_premium <= 0:
                continue  # fail-safe：無有效原始權利金，無法計算衰減幅度

            expiry = str(pos.get("expiry", ""))
            try:
                exp_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
                dte = (exp_dt - datetime.now().date()).days
            except (ValueError, TypeError):
                continue  # fail-safe：到期日無法解析，安全起見不產生指令

            strike = pos.get("strike")
            quantity = float(pos.get("quantity", 0.0) or 0.0)
            current_premium = float(pos.get("current_premium", 0.0) or 0.0)
            decay_pct = (
                (entry_premium - current_premium) / entry_premium
                if current_premium > 0
                else 0.0
            )

            dte_tier = evaluate_option_dte_tier(dte, "MANAGE_EXISTING")
            if dte_tier == "EXPIRATION_SETTLEMENT_ALERT":
                # 末日結算保護：無論權利金衰減幅度或報價是否可得，一律強制
                # 100% BTC 回補，與變更一的結算保護邏輯一致。
                btc_ratio = 1.0
                premium_desc = (
                    f"現價權利金 ${current_premium:.2f}（衰減 {decay_pct:.0%}）"
                    if current_premium > 0
                    else "現價權利金報價暫缺"
                )
                reason = (
                    "🆘 **末日結算保護 (Covered Call Forced Settlement)**\n"
                    f"{symbol} Covered Call DTE={dte}"
                    f"（<= {_HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD}），"
                    f"無論權利金衰減幅度，強制 100% BTC 回補了結。{premium_desc}"
                )
            elif current_premium <= 0:
                continue  # fail-safe：報價缺失，不猜測衰減幅度
            elif decay_pct >= _COVERED_CALL_PROFIT_LOCK_FULL_DECAY_PCT:
                btc_ratio = 1.0
                reason = (
                    "💡 **Covered Call 權利金衰減停利 (全額)**\n"
                    f"{symbol} 原始權利金 ${entry_premium:.2f} → 現價權利金 "
                    f"${current_premium:.2f}，衰減 {decay_pct:.0%} "
                    f"(達 {_COVERED_CALL_PROFIT_LOCK_FULL_DECAY_PCT:.0%} 全額門檻)，"
                    "時間價值收租已完成，建議全額 BTC 回補鎖定收益。"
                )
            elif decay_pct >= _COVERED_CALL_PROFIT_LOCK_PARTIAL_DECAY_PCT:
                btc_ratio = _COVERED_CALL_PROFIT_LOCK_PARTIAL_RATIO
                reason = (
                    "💡 **Covered Call 權利金衰減停利 (局部)**\n"
                    f"{symbol} 原始權利金 ${entry_premium:.2f} → 現價權利金 "
                    f"${current_premium:.2f}，衰減 {decay_pct:.0%} "
                    f"(達 {_COVERED_CALL_PROFIT_LOCK_PARTIAL_DECAY_PCT:.0%} 局部門檻)，"
                    f"建議 BTC 回補 {btc_ratio:.0%} 部位局部鎖定收益。"
                )
            else:
                continue  # 未達任何衰減門檻，不產生指令

            cash_impact = format_cash_impact(
                abs(quantity) * btc_ratio * current_premium * 100
            )

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE" if btc_ratio >= 1.0 else "REDUCE",
                    "sell_ratio": btc_ratio,
                    "target_core": symbol,
                    "reason": reason,
                    "suggested_strategy": f"{btc_ratio:.0%} BTC (Buy To Close)",
                    "sell_action": "BTC",
                    "scenario": RolloverScenario.COVERED_CALL_PROFIT_LOCK.value,
                    "is_manual_override_required": False,
                    "cash_impact": cash_impact,
                    "limit_price": current_premium if current_premium > 0 else None,
                    "extreme_stop_loss": None,
                    "is_extreme_tick_breach": False,
                    "extreme_breach_detail_block": None,
                    "instrument_type": "OPTIONS",
                    "strike": f"${float(strike):.2f}" if strike else "N/A",
                    "expiry": expiry or "N/A",
                    "is_covered_call_profit_lock": True,
                    "entry_premium": entry_premium,
                    "current_premium": current_premium,
                    "decay_pct": decay_pct,
                    "dte": dte,
                }
            )
        return instructions
