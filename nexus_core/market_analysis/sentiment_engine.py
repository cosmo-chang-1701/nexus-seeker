import logging
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from services import market_data_service
import database.core as db_core
import sqlite3
import config

logger = logging.getLogger(__name__)

class SentimentEngine:
    """
    期權市場情緒引擎：負責計算 Skew, PCR, Max Pain 與 UOA 偵測。
    """

    @staticmethod
    async def calculate_skew(symbol: str) -> Dict[str, Any]:
        """
        計算期權偏斜 (Option Skew)。
        邏輯：取最近一個月 (Monthly) 的 OTM Put IV 與 OTM Call IV 之差。
        Skew = IV (25-Delta Put) - IV (25-Delta Call)
        """
        try:
            expiries = await market_data_service.get_all_option_expiries(symbol)
            if not expiries:
                return {"symbol": symbol, "skew": 0, "state": "N/A", "error": "No expiries"}

            # 尋找最近的月期權 (假設距離今天 20-45 天)
            today = datetime.now()
            target_expiry = None
            for exp in expiries:
                exp_dt = datetime.strptime(exp, '%Y-%m-%d')
                days_to_expiry = (exp_dt - today).days
                if 20 <= days_to_expiry <= 45:
                    target_expiry = exp
                    break
            
            if not target_expiry:
                target_expiry = expiries[0] # 回退到最近的一個

            chain = await market_data_service.get_option_chain(symbol, target_expiry)
            if not chain:
                return {"symbol": symbol, "skew": 0, "state": "N/A"}

            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get('c', 0)
            if spot_price == 0:
                return {"symbol": symbol, "skew": 0, "state": "N/A"}

            calls = chain.calls
            puts = chain.puts

            # 尋找 OTM 25-Delta 附近的期權 (簡化版：使用距離現價一定比例的 Strike)
            # 實務上應使用 py_vollib 計算 Delta，此處先用 Strike 偏移量作為代理
            # 25 Delta Call 通常在現價 + 5~10%
            # 25 Delta Put 通常在現價 - 5~10%
            
            otm_call = calls[calls['strike'] > spot_price * 1.05].iloc[0] if not calls[calls['strike'] > spot_price * 1.05].empty else None
            otm_put = puts[puts['strike'] < spot_price * 0.95].iloc[-1] if not puts[puts['strike'] < spot_price * 0.95].empty else None

            if otm_call is None or otm_put is None:
                return {"symbol": symbol, "skew": 0, "state": "數據不足"}

            iv_call = otm_call['impliedVolatility']
            iv_put = otm_put['impliedVolatility']
            skew_val = (iv_put - iv_call) * 100 # 以百分點表示

            state = "正常"
            if skew_val > 5:
                state = "⚠️ 預警性對沖 (Put 昂貴)"
            elif skew_val < -2:
                state = "🚀 看多情緒濃厚 (Call 昂貴)"

            # 儲存到資料庫以便後續計算百分位
            SentimentEngine.save_sentiment_history(symbol, 'SKEW', skew_val)

            return {
                "symbol": symbol,
                "skew": round(skew_val, 2),
                "iv_put": round(iv_put, 4),
                "iv_call": round(iv_call, 4),
                "state": state,
                "expiry": target_expiry
            }

        except Exception as e:
            logger.error(f"[{symbol}] Skew 計算失敗: {e}")
            return {"symbol": symbol, "skew": 0, "state": "ERROR"}

    @staticmethod
    async def calculate_pcr(symbol: str) -> Dict[str, Any]:
        """
        計算買賣權比率 (Put/Call Ratio)。
        邏輯：總成交量 (Volume) 或 未平倉量 (Open Interest) 的 P/C 比。
        """
        try:
            expiries = await market_data_service.get_all_option_expiries(symbol)
            if not expiries:
                return {"symbol": symbol, "pcr": 0, "state": "N/A"}

            # 彙整前三個到期日的數據
            total_put_vol = 0
            total_call_vol = 0
            
            for exp in expiries[:3]:
                chain = await market_data_service.get_option_chain(symbol, exp)
                if not chain: continue
                total_put_vol += chain.puts['volume'].sum()
                total_call_vol += chain.calls['volume'].sum()

            if total_call_vol == 0:
                return {"symbol": symbol, "pcr": 0, "state": "N/A"}

            pcr_val = total_put_vol / total_call_vol
            
            state = "平衡"
            if pcr_val > 1.0:
                state = "🐻 偏向空頭"
            elif pcr_val < 0.6:
                state = "🐂 市場過熱 (Extreme Greed)"

            SentimentEngine.save_sentiment_history(symbol, 'PCR', pcr_val)

            return {
                "symbol": symbol,
                "pcr": round(pcr_val, 2),
                "put_vol": total_put_vol,
                "call_vol": total_call_vol,
                "state": state
            }
        except Exception as e:
            logger.error(f"[{symbol}] PCR 計算失敗: {e}")
            return {"symbol": symbol, "pcr": 0, "state": "ERROR"}

    @staticmethod
    async def calculate_max_pain(symbol: str, expiry: Optional[str] = None) -> Dict[str, Any]:
        """
        計算最大痛點 (Max Pain)。
        邏輯：尋找讓所有期權買家總價值最小化的標的價格。
        """
        try:
            if not expiry:
                expiries = await market_data_service.get_all_option_expiries(symbol)
                if not expiries: return {"error": "No expiries"}
                expiry = expiries[0]

            chain = await market_data_service.get_option_chain(symbol, expiry)
            if not chain: return {"error": "No chain data"}

            calls = chain.calls.dropna(subset=['openInterest'])
            puts = chain.puts.dropna(subset=['openInterest'])

            # 彙整所有履約價
            strikes = sorted(list(set(calls['strike']) | set(puts['strike'])))
            
            # 計算每個履約價下的總痛點 (Dollar Value if expired there)
            pains = []
            for s in strikes:
                # Call Pain: max(0, spot - strike) * OI
                call_pain = calls[calls['strike'] < s].apply(lambda x: (s - x['strike']) * x['openInterest'], axis=1).sum()
                # Put Pain: max(0, strike - spot) * OI
                put_pain = puts[puts['strike'] > s].apply(lambda x: (x['strike'] - s) * x['openInterest'], axis=1).sum()
                pains.append(call_pain + put_pain)

            max_pain_strike = strikes[pains.index(min(pains))]
            
            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get('c', 0)
            
            dist_pct = (spot_price - max_pain_strike) / spot_price * 100 if spot_price > 0 else 0
            
            return {
                "symbol": symbol,
                "expiry": expiry,
                "max_pain": max_pain_strike,
                "current_price": spot_price,
                "distance_pct": round(dist_pct, 2),
                "is_converging": abs(dist_pct) < 2.0
            }
        except Exception as e:
            logger.error(f"[{symbol}] Max Pain 計算失敗: {e}")
            return {"error": str(e)}

    @staticmethod
    async def detect_uoa(symbol: str) -> List[Dict[str, Any]]:
        """
        偵測異常期權活動 (Unusual Options Activity)。
        邏輯：尋找成交量 (Volume) 遠大於 未平倉量 (Open Interest) 的合約。
        """
        try:
            expiries = await market_data_service.get_all_option_expiries(symbol)
            if not expiries: return []

            uoa_list = []
            # 檢查前兩個到期日
            for exp in expiries[:2]:
                chain = await market_data_service.get_option_chain(symbol, exp)
                if not chain: continue
                
                for _, row in pd.concat([chain.calls, chain.puts]).iterrows():
                    # 門檻：Volume > 5 * OI 且 Volume > 500
                    if row['volume'] > 5 * row['openInterest'] and row['volume'] > 500:
                        uoa_list.append({
                            "symbol": symbol,
                            "expiry": exp,
                            "strike": row['strike'],
                            "type": "CALL" if row['strike'] in chain.calls['strike'].values else "PUT",
                            "volume": int(row['volume']),
                            "oi": int(row['openInterest']),
                            "ratio": round(row['volume'] / max(row['openInterest'], 1), 2),
                            "iv": round(row['impliedVolatility'], 4)
                        })
            
            return sorted(uoa_list, key=lambda x: x['volume'], reverse=True)[:5]
        except Exception as e:
            logger.error(f"[{symbol}] UOA 偵測失敗: {e}")
            return []

    @staticmethod
    def save_sentiment_history(symbol: str, indicator: str, value: float):
        """將情緒指標存入資料庫。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sentiment_history (symbol, indicator, value)
                VALUES (?, ?, ?)
            """, (symbol, indicator, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"儲存情緒歷史失敗: {e}")

    @staticmethod
    def get_indicator_percentile(symbol: str, indicator: str, current_value: float) -> float:
        """計算目前值在歷史數據中的百分位數。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM sentiment_history 
                WHERE symbol = ? AND indicator = ? 
                ORDER BY timestamp DESC LIMIT 100
            """, (symbol, indicator))
            values = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if not values: return 50.0 # 預設中值
            
            count = sum(1 for v in values if v < current_value)
            return (count / len(values)) * 100
        except Exception:
            return 50.0
