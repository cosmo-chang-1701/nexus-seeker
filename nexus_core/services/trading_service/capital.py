"""使用者可用資本調整計算（BOXX 折算現金）。"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def get_adjusted_user_capital(user_id: int, base_capital: float) -> float:
    """
    計算調整後的用戶可用資本。
    若持倉中有 BOXX，則視為等同現金的資產，並套用 90% 的安全折價率（Collateral Haircut），
    將其折算價值（量 * 市價 * 0.90）加回可用資本（Capital）中。
    """
    from database.holdings import get_user_holdings
    from services.market_data_service import get_quote

    try:
        holdings = await asyncio.to_thread(get_user_holdings, user_id)
        boxx_holding = next(
            (h for h in holdings if h["symbol"].upper() == "BOXX"), None
        )
        if boxx_holding and boxx_holding.get("quantity", 0) > 0:
            qty = boxx_holding["quantity"]
            quote = await get_quote("BOXX")
            price = quote.get("c", 0.0) if quote else 0.0
            if price <= 0:
                price = boxx_holding.get("avg_cost", 210.0)

            # Apply 90% collateral haircut
            boxx_cash_value = qty * price * 0.90
            return base_capital + boxx_cash_value  # type: ignore
    except Exception as e:
        logger.warning(f"計算 BOXX 折算資本時發生錯誤: {e}")

    return base_capital
