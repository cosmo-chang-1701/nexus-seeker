import logging
import asyncio
import math
import yfinance as yf
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional
from py_vollib.black_scholes_merton.greeks.analytical import delta
from config import RISK_FREE_RATE
from database.holdings import get_user_holdings
from database.orders import get_user_active_orders
from market_analysis.sentiment_engine import SentimentEngine
from services.market_data_service import get_history_df, get_quote

logger = logging.getLogger(__name__)


def calculate_new_cost_basis(
    current_shares: float, current_cost: float, grid_orders: List[Dict[str, Any]]
) -> float:
    """計算模擬網格吸籌後的加權平均成本 (New Cost Basis)。"""
    total_shares = current_shares
    total_cost_spent = current_shares * current_cost

    for o in grid_orders:
        validity = o.get("validity", "").upper()
        side = o.get("side", "").upper()
        # 篩選 GTC / 買入掛單
        if "GTC" in validity and side == "BUY":
            price = o.get("limit_price", 0.0)
            if price <= 0.0:
                price = o.get("stop_price", 0.0)
            qty = o.get("quantity", 0.0)
            if qty > 0 and price > 0:
                total_shares += qty
                total_cost_spent += qty * price

    if total_shares <= 0:
        return 0.0
    return round(total_cost_spent / total_shares, 2)


async def get_hv_30(symbol: str) -> float:
    """計算個股 30 天歷史波動率 (HV) 作為 IV 備用代理。"""
    try:
        df = await get_history_df(symbol, period="3mo")
        if df.empty or len(df) < 30:
            return 0.30
        import numpy as np

        close = df["Close"].astype(float)
        log_returns = np.log(close / close.shift(1))
        std = log_returns.tail(30).std()
        if math.isnan(std) or std <= 0:
            return 0.30
        hv = float(std * math.sqrt(252))
        return hv if hv > 0.01 else 0.30
    except Exception as e:
        logger.warning(f"計算 {symbol} 30天歷史波動率失敗: {e}")
        return 0.30


async def recommend_covered_calls(
    user_id: int, symbol: str
) -> Optional[Dict[str, Any]]:
    """尋找 7 月中下旬到期、符合 Strike > New Cost Basis 且 Delta < 0.15 的 Covered Call 推薦合約。"""
    symbol = symbol.upper()

    # 1. 取得現有持倉
    holdings = get_user_holdings(user_id)
    current_shares = 0.0
    current_cost = 0.0
    for h in holdings:
        if h.get("symbol", "").upper() == symbol:
            current_shares = h.get("quantity", 0.0)
            current_cost = h.get("avg_cost", 0.0)
            break

    if current_shares <= 0:
        logger.info(f"使用者 {user_id} 目前無 {symbol} 持倉，無需生成解鎖建議。")
        return None

    # 2. 取得活躍 GTC 網格買單
    orders = get_user_active_orders(user_id)
    grid_orders = [
        o
        for o in orders
        if o.get("symbol", "").upper() == symbol
        and "GTC" in o.get("validity", "").upper()
        and o.get("side", "").upper() == "BUY"
    ]

    # 3. 計算新加權成本
    new_cost_basis = calculate_new_cost_basis(current_shares, current_cost, grid_orders)

    # 4. 取得現貨當前價與波動率代理
    quote = await get_quote(symbol)
    current_price = quote.get("c", 0.0) if quote else 0.0
    if current_price <= 0:
        logger.warning(f"無法獲取 {symbol} 即時價，無法生成 Covered Call 建議。")
        return None

    # 取得波動率 (IV 優先，儲存之歷史 IV 次之，最後以 30天 HV 代理)
    stored_iv = SentimentEngine.get_last_stored_iv(symbol)
    if stored_iv and stored_iv > 0.01:
        fallback_iv = stored_iv
    else:
        fallback_iv = await get_hv_30(symbol)

    # 5. 基於到期天數 (DTE 30-50 天) 進行動態篩選
    today = datetime.now().date()
    ticker = yf.Ticker(symbol)
    try:
        expirations = await asyncio.to_thread(lambda: ticker.options)
    except Exception as e:
        logger.error(f"獲取 {symbol} 期權到期日失敗: {e}")
        return None

    target_expirations = []
    for exp in expirations:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            # 篩選 DTE 30-50 天的期權
            if 30 <= dte <= 50:
                target_expirations.append(exp)
        except ValueError:
            continue

    # Fallback：若 30-50 天區間無到期日，取最近一個 DTE >= 30 的到期日以保持系統運作
    if not target_expirations and expirations:
        for exp in expirations:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                if (exp_date - today).days >= 30:
                    target_expirations.append(exp)
                    break
            except ValueError:
                continue

    recommendations = []
    today_dt = datetime.now()

    for exp in target_expirations:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d")
            t_years = (exp_date - today_dt).days / 365.0
            if t_years <= 0:
                continue

            # 抓取 option chain
            opt_chain = await asyncio.to_thread(lambda: ticker.option_chain(exp))
            calls = opt_chain.calls

            for _, row in calls.iterrows():
                strike = float(row["strike"])
                # 篩選 Strike > New Cost Basis
                if strike <= new_cost_basis:
                    continue

                # 估計合約 Delta 值
                iv = float(row["impliedVolatility"])
                if pd.isna(iv) or iv <= 0.01:
                    iv = fallback_iv

                try:
                    d_val = delta(
                        "c", current_price, strike, t_years, RISK_FREE_RATE, iv, q=0.0
                    )
                except Exception:
                    d_val = 0.0

                # 篩選 Delta < 0.15
                if 0.0 < d_val < 0.15:
                    premium = float(row.get("lastPrice", 0.0))
                    bid = float(row.get("bid", 0.0))
                    ask = float(row.get("ask", 0.0))

                    # 取得合理的權利金參考 (Mid-price 優先)
                    ref_premium = (
                        (bid + ask) / 2.0 if (bid > 0 and ask > bid) else premium
                    )
                    if ref_premium <= 0:
                        ref_premium = premium

                    # 計算年化收益率
                    ann_yield = (
                        (ref_premium / current_price) / t_years * 100.0
                        if current_price > 0
                        else 0.0
                    )

                    # 篩選條件：年化收益率 >= 10.0% 或單次收租權利金大於現貨的 1%
                    if ann_yield >= 10.0 or ref_premium >= (0.01 * current_price):
                        recommendations.append(
                            {
                                "expiration": exp,
                                "strike": strike,
                                "delta": round(d_val, 3),
                                "premium": round(ref_premium, 2),
                                "bid": bid,
                                "ask": ask,
                                "annualized_yield": round(ann_yield, 2),
                                "contractSymbol": row.get("contractSymbol", ""),
                            }
                        )
        except Exception as ex:
            logger.warning(f"處理期權到期日 {exp} 鏈失敗: {ex}")
            continue

    # 按履約價由低到高排序，篩選出最佳選擇 (最高權利金或最貼近 Delta 0.15)
    recommendations.sort(key=lambda x: x["strike"])

    return {
        "symbol": symbol,
        "current_shares": current_shares,
        "current_cost": current_cost,
        "new_cost_basis": new_cost_basis,
        "current_price": current_price,
        "fallback_iv": fallback_iv,
        "recommendations": recommendations[:3],  # 最多推薦前 3 個合約
    }
