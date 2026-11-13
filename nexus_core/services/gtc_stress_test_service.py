"""GTC 掛單現金赤字壓力測試服務（`/stress_test` 與 /dash「壓測」按鈕共用）。

與 MARGIN_DEFENSE 同一套算法：
緩衝 ＝ 手填現金儲備 ＋ 現金等價物（BOXX/BIL/SHV/SGOV）即時市值；
赤字 ＝ 所有有效 GTC 買單 100% 成交所需現金。
提領紅線取自使用者 /settings 的提領跑道快照，不再寫死金額。
資料源失敗時不拋例外：缺價以 None 表示，由 Embed 降級顯示。
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Mapping, Optional, Sequence

import database
from market_analysis.dynamic_rollover._shared import (
    count_gtc_buy_orders,
    sum_gtc_buy_commitment,
)
from market_analysis.dynamic_rollover.regime_hedge_sizing import (
    CASH_EQUIVALENT_SYMBOLS,
)

logger = logging.getLogger(__name__)

CashEquivPosition = tuple[str, float, Optional[float]]


def _usable_price(price: Any) -> Optional[float]:
    """價格為有限正數才可用，否則回傳 None（未知價格不得當 0）。"""
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def build_stress_test_results(
    orders: Sequence[Mapping[str, Any]],
    cash_reserve: Optional[float],
    cash_equiv_positions: Sequence[CashEquivPosition],
    next_withdrawal: Optional[float] = None,
    next_withdrawal_date: Optional[str] = None,
) -> dict[str, Any]:
    """純函式：由已取得的資料組出壓測結果 dict（供 `create_stress_test_embed`）。"""
    total_deficit = sum_gtc_buy_commitment(orders)
    equiv_value: Optional[float] = 0.0
    details: list[str] = []
    for symbol, qty, price in cash_equiv_positions:
        details.append(f"{symbol} {qty:g} 股")
        p = _usable_price(price)
        if p is None or equiv_value is None:
            equiv_value = None
        else:
            equiv_value += abs(qty * p)

    if cash_reserve is None or equiv_value is None:
        net_deficit: Optional[float] = None
        is_critical = False
    else:
        buffer = cash_reserve + equiv_value
        net_deficit = buffer - total_deficit
        is_critical = total_deficit > buffer

    return {
        "total_deficit": total_deficit,
        "gtc_buy_orders_count": count_gtc_buy_orders(orders),
        "cash_reserve": cash_reserve,
        "cash_equiv_value": equiv_value,
        "cash_equiv_detail": "、".join(details),
        "net_deficit": net_deficit,
        "is_critical": is_critical,
        "next_withdrawal": next_withdrawal,
        "next_withdrawal_date": next_withdrawal_date,
        "load_failed": False,
    }


async def _quote_price(symbol: str) -> Optional[float]:
    from services import market_data_service

    try:
        quote = await market_data_service.get_quote(symbol)
    except Exception as e:
        logger.warning(f"[stress_test] 取得 {symbol} 報價失敗: {e}")
        return None
    if not isinstance(quote, Mapping):
        return None
    return _usable_price(quote.get("c"))


async def run_gtc_stress_test(user_id: int) -> dict[str, Any]:
    """讀取使用者資料並執行壓測；資料源失敗不拋例外。"""
    from database.holdings import get_user_holdings
    from database.orders import get_user_active_orders

    try:
        orders = await asyncio.to_thread(get_user_active_orders, user_id)
    except Exception as e:
        logger.warning(f"[stress_test] 讀取活躍委託失敗，視為無委託: {e}")
        orders = []

    ctx = await asyncio.to_thread(database.get_full_user_context, user_id)
    if getattr(ctx, "load_failed", False) is True:
        # 帳戶資料讀取失敗：cash_reserve 為佔位 0，不可據此判定。
        logger.warning(f"[stress_test] 使用者 {user_id} 帳戶資料讀取失敗")
        res = build_stress_test_results(orders, None, [])
        res["load_failed"] = True
        return res
    cash_reserve = float(ctx.cash_reserve) if ctx else 0.0

    held: dict[str, float] = {}
    try:
        holdings = await asyncio.to_thread(get_user_holdings, user_id)
    except Exception as e:
        logger.warning(f"[stress_test] 讀取持倉失敗: {e}")
        res = build_stress_test_results(orders, cash_reserve, [])
        res["cash_equiv_value"] = None
        res["net_deficit"] = None
        res["is_critical"] = False
        return await _attach_runway(user_id, res)
    for h in holdings:
        sym = str(h.get("symbol") or "").upper()
        try:
            qty = float(h.get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        if sym in CASH_EQUIVALENT_SYMBOLS and qty != 0:
            held[sym] = held.get(sym, 0.0) + qty

    prices = await asyncio.gather(*[_quote_price(s) for s in held])
    positions: list[CashEquivPosition] = [
        (s, q, p) for (s, q), p in zip(held.items(), prices)
    ]
    res = build_stress_test_results(orders, cash_reserve, positions)
    return await _attach_runway(user_id, res)


async def _attach_runway(user_id: int, res: dict[str, Any]) -> dict[str, Any]:
    """附上 /settings 提領跑道的下次提領資訊（失敗或未啟用則為 None）。"""
    from services.withdrawal_runway_service import get_runway_display

    try:
        snap, stale = await get_runway_display(user_id)
    except Exception as e:
        logger.warning(f"[stress_test] 讀取提領跑道失敗: {e}")
        return res
    if snap is not None:
        res["next_withdrawal"] = snap.next_withdrawal
        res["next_withdrawal_date"] = snap.next_date
        res["next_withdrawal_stale"] = bool(stale)
    return res
