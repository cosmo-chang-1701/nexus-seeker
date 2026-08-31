"""到期日/合約篩選、垂直偏態計算、最佳合約搜尋。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from services import market_data_service
from market_analysis.greeks import calculate_contract_delta

logger = logging.getLogger(__name__)


def _find_target_expiry(expirations: Any, today: Any, min_dte: Any, max_dte: Any):  # type: ignore
    """尋找符合天數的到期日"""
    for exp in expirations:
        days_to_expiry = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if min_dte <= days_to_expiry <= max_dte:
            return exp, days_to_expiry
    return None, 0


def _get_best_contract_data(  # type: ignore
    opt_chain: Any,
    opt_type: Any,
    target_delta: Any,
    price: Any,
    days_to_expiry: Any,
    dividend_yield: Any = 0.0,
):
    """取得最佳合約與 Greeks"""
    try:
        if opt_chain is None:
            return None, None
        chain_data = opt_chain.calls if opt_type == "call" else opt_chain.puts
        if chain_data is None:
            return None, None
        chain_data = chain_data[chain_data["volume"] > 0].copy()

        if chain_data.empty:
            return None, None

        t_years = max(days_to_expiry, 1) / 365.0
        flag = "c" if opt_type == "call" else "p"
        chain_data["bs_delta"] = chain_data.apply(
            lambda row: calculate_contract_delta(
                row, price, t_years, flag, q=dividend_yield
            ),
            axis=1,
        )
        chain_data = chain_data[chain_data["bs_delta"] != 0.0].copy()

        if chain_data.empty:
            return None, None

        best_contract = chain_data.loc[
            (chain_data["bs_delta"] - target_delta).abs().idxmin()
        ]
        return best_contract, opt_chain  # Return opt_chain for skew calc
    except Exception:
        return None, None


def _calculate_vertical_skew(  # type: ignore
    opt_chain: Any,
    price: Any,
    days_to_expiry: Any,
    strategy: Any,
    symbol: Any,
    dividend_yield: Any = 0.0,
):
    """計算垂直波動率偏態"""
    vertical_skew = 1.0
    skew_state = "⚖️ 中性 (Neutral)"
    t_years = max(days_to_expiry, 1) / 365.0
    is_high_tail_risk = False

    try:
        calls_skew = opt_chain.calls[opt_chain.calls["volume"] > 0].copy()
        puts_skew = opt_chain.puts[opt_chain.puts["volume"] > 0].copy()

        if not calls_skew.empty and not puts_skew.empty:
            calls_skew["bs_delta"] = calls_skew.apply(
                lambda row: calculate_contract_delta(
                    row, price, t_years, "c", q=dividend_yield
                ),
                axis=1,
            )
            puts_skew["bs_delta"] = puts_skew.apply(
                lambda row: calculate_contract_delta(
                    row, price, t_years, "p", q=dividend_yield
                ),
                axis=1,
            )

            call_25_idx = (calls_skew["bs_delta"] - 0.25).abs().idxmin()
            put_25_idx = (puts_skew["bs_delta"] - (-0.25)).abs().idxmin()
            call_25 = calls_skew.loc[[call_25_idx]]
            put_25 = puts_skew.loc[[put_25_idx]]

            if not call_25.empty and not put_25.empty:
                iv_call_25 = call_25.iloc[0].get("impliedVolatility", 0.0)
                iv_put_25 = put_25.iloc[0].get("impliedVolatility", 0.0)

                if iv_call_25 > 0.01:
                    vertical_skew = iv_put_25 / iv_call_25

                if vertical_skew >= 1.30:
                    skew_state = "⚠️ 嚴重左偏 (高尾部風險)"
                    if vertical_skew >= 1.50:
                        skew_state = "🚨 極端左偏 (觸發尾部風險降規)"
                        is_high_tail_risk = True
                elif vertical_skew <= 0.90:
                    skew_state = "🚀 右偏 (看漲狂熱)"
    except Exception as e:
        logger.error(f"[{symbol}] 垂直偏態運算錯誤: {e}")

    return vertical_skew, skew_state, is_high_tail_risk


async def _fetch_opt_chain_and_best_contract(
    symbol: Any,
    target_expiry_date: Any,
    opt_type: Any,
    target_delta: Any,
    price: Any,
    days_to_expiry: Any,
    dividend_yield: Any,
) -> tuple[Any, Any]:
    """選擇權鏈抓取 -> 最佳合約篩選的既有序列管線，包成單一 coroutine 供 gather 使用。"""
    opt_chain = await market_data_service.get_option_chain(symbol, target_expiry_date)
    best_contract, opt_chain = await asyncio.to_thread(
        _get_best_contract_data,
        opt_chain,
        opt_type,
        target_delta,
        price,
        days_to_expiry,
        dividend_yield,
    )
    return best_contract, opt_chain


async def find_best_contract(
    symbol: Any, strategy_type: Any, target_delta: Any, min_dte: Any, max_dte: Any
) -> Optional[dict[str, Any]]:
    try:
        expirations = await market_data_service.get_all_option_expiries(symbol)
        today = datetime.now().date()
        target_expiry_date, days_to_expiry = _find_target_expiry(
            expirations, today, min_dte, max_dte
        )
        if not target_expiry_date:
            return None

        quote = await market_data_service.get_quote(symbol)
        price = quote.get("c", 0.0) if quote else 0.0

        opt_chain = await market_data_service.get_option_chain(
            symbol, target_expiry_date
        )
        opt_type = "call" if "CALL" in strategy_type else "put"
        best_contract, _ = await asyncio.to_thread(
            _get_best_contract_data,
            opt_chain,
            opt_type,
            target_delta,
            price,
            days_to_expiry,
        )
        if best_contract is None:
            return None

        bid, ask = best_contract.get("bid", 0.0), best_contract.get("ask", 0.0)
        mid = (
            (bid + ask) / 2.0
            if bid > 0 and ask > 0
            else best_contract.get("lastPrice", 0.0)
        )
        return {
            "strike": float(best_contract.get("strike", 0.0)),
            "expiry": target_expiry_date,
            "mid": mid,
            "bid": bid,
            "ask": ask,
        }
    except Exception as e:
        logger.error(f"find_best_contract error for {symbol}: {e}")
        return None


async def find_lowest_strike_call_above_floor(
    symbol: Any, floor_strike: Any, min_dte: Any, max_dte: Any
) -> Optional[dict[str, Any]]:
    """尋找 [min_dte, max_dte] 區間內最近一個到期日，於該到期日 Call 鏈中挑選
    strike > floor_strike 的所有合約裡履約價最低者 (最貼近下限，OTM 幅度最小、
    權利金最高)。供 Covered Call Overlay 這類「履約價下限錨定」的選股邏輯使用，
    與 find_best_contract() 的 Delta 錨定選股邏輯互補而非重複 —— find_best_contract
    無法表達「履約價下限」這種約束，只能挑選最接近目標 Delta 的單一合約。

    找不到合格到期日、合格履約價，或任何步驟失敗，一律回傳 None (fail-safe，
    絕不拋出例外)，與 find_best_contract 的降級慣例一致。
    """
    try:
        expirations = await market_data_service.get_all_option_expiries(symbol)
        today = datetime.now().date()
        target_expiry_date, _days_to_expiry = _find_target_expiry(
            expirations, today, min_dte, max_dte
        )
        if not target_expiry_date:
            return None

        opt_chain = await market_data_service.get_option_chain(
            symbol, target_expiry_date
        )
        if opt_chain is None or opt_chain.calls is None:
            return None

        chain_data = opt_chain.calls
        chain_data = chain_data[chain_data["volume"] > 0].copy()
        chain_data = chain_data[chain_data["strike"] > float(floor_strike)]
        if chain_data.empty:
            return None

        best_contract = chain_data.loc[chain_data["strike"].idxmin()]
        bid, ask = best_contract.get("bid", 0.0), best_contract.get("ask", 0.0)
        mid = (
            (bid + ask) / 2.0
            if bid > 0 and ask > 0
            else best_contract.get("lastPrice", 0.0)
        )
        return {
            "strike": float(best_contract.get("strike", 0.0)),
            "expiry": target_expiry_date,
            "mid": mid,
            "bid": bid,
            "ask": ask,
        }
    except Exception as e:
        logger.error(f"find_lowest_strike_call_above_floor error for {symbol}: {e}")
        return None
