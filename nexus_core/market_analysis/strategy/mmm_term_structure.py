"""財報日 MMM (Market Maker Move) 與波動率期限結構計算。"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from services import market_data_service
from market_analysis.data import get_next_earnings_date

logger = logging.getLogger(__name__)


async def _calculate_mmm(symbol: Any, price: Any, today: Any, is_etf: Any):  # type: ignore
    """計算財報日 MMM (Market Maker Move)"""
    earnings_date = None if is_etf else await get_next_earnings_date(symbol)
    days_to_earnings = -1
    mmm_pct, safe_lower, safe_upper = 0.0, 0.0, 0.0

    if earnings_date:
        if isinstance(earnings_date, datetime):
            earnings_date = earnings_date.date()
        days_to_earnings = (earnings_date - today).days

        if 0 <= days_to_earnings <= 14:
            target_exp_for_mmm = None
            try:
                options = await market_data_service.get_all_option_expiries(symbol)
                for exp in options:
                    if datetime.strptime(exp, "%Y-%m-%d").date() >= earnings_date:
                        target_exp_for_mmm = exp
                        break
            except Exception:
                pass

            if target_exp_for_mmm:
                try:
                    chain_mmm = await market_data_service.get_option_chain(
                        symbol, target_exp_for_mmm
                    )
                    if chain_mmm is not None:
                        calls_mmm = chain_mmm.calls
                        c_price = 0
                        if calls_mmm is not None and not calls_mmm.empty:
                            atm_call_idx = (calls_mmm["strike"] - price).abs().idxmin()
                            atm_call = calls_mmm.loc[atm_call_idx]
                            c_bid, c_ask, c_last = (
                                atm_call.get("bid", 0.0),
                                atm_call.get("ask", 0.0),
                                atm_call.get("lastPrice", 0.0),
                            )
                            c_price = (
                                (c_bid + c_ask) / 2
                                if (c_bid > 0 and c_ask > 0)
                                else c_last
                            )

                        puts_mmm = chain_mmm.puts
                        p_price = 0
                        if puts_mmm is not None and not puts_mmm.empty:
                            atm_put_idx = (puts_mmm["strike"] - price).abs().idxmin()
                            atm_put = puts_mmm.loc[atm_put_idx]
                            p_bid, p_ask, p_last = (
                                atm_put.get("bid", 0.0),
                                atm_put.get("ask", 0.0),
                                atm_put.get("lastPrice", 0.0),
                            )
                            p_price = (
                                (p_bid + p_ask) / 2
                                if (p_bid > 0 and p_ask > 0)
                                else p_last
                            )

                        if price > 0:
                            mmm_pct = ((c_price + p_price) / price) * 100
                            safe_lower = price * (1 - mmm_pct / 100)
                            safe_upper = price * (1 + mmm_pct / 100)
                except Exception as e:
                    logger.error(f"[{symbol}] MMM 運算失敗: {e}")

    return mmm_pct, safe_lower, safe_upper, days_to_earnings


async def _calculate_term_structure(
    symbol: Any, expirations: Any, price: Any, today: Any
) -> tuple[float, str]:
    """計算波動率期限結構"""
    front_date, back_date = None, None
    front_diff, back_diff = 9999, 9999

    for exp in expirations:
        days_to_expiry = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if abs(days_to_expiry - 30) < front_diff:
            front_diff, front_date = abs(days_to_expiry - 30), exp
        if abs(days_to_expiry - 60) < back_diff:
            back_diff, back_date = abs(days_to_expiry - 60), exp

    ts_ratio, ts_state = 1.0, "平滑 (Flat)"
    if front_date and back_date and front_date != back_date:
        try:
            front_task = market_data_service.get_option_chain(symbol, front_date)
            back_task = market_data_service.get_option_chain(symbol, back_date)
            front_chain, back_chain = await asyncio.gather(front_task, back_task)

            if front_chain is not None and back_chain is not None:
                front_puts = front_chain.puts
                back_puts = back_chain.puts

                if (
                    front_puts is not None
                    and back_puts is not None
                    and not front_puts.empty
                    and not back_puts.empty
                ):
                    front_iv_idx = (front_puts["strike"] - price).abs().idxmin()
                    back_iv_idx = (back_puts["strike"] - price).abs().idxmin()
                    front_iv = front_puts.loc[front_iv_idx].get(
                        "impliedVolatility", 0.0
                    )
                    back_iv = back_puts.loc[back_iv_idx].get("impliedVolatility", 0.0)

                    if back_iv > 0.01:
                        ts_ratio = front_iv / back_iv

                    if ts_ratio >= 1.05:
                        ts_state = "🚨 恐慌 (Backwardation)"
                    elif ts_ratio <= 0.95:
                        ts_state = "🌊 正常 (Contango)"
        except Exception:
            pass

    return ts_ratio, ts_state
