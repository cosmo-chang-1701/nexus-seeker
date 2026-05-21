import logging
import pandas as pd
import numpy as np
import sqlite3
import time
import math
import asyncio
import yfinance as yf
from datetime import datetime
from typing import Dict, Any, List, Literal, Optional
from services import market_data_service
from services.market_data_service import BoundedCache
import config
from models.quant import IVMetrics

_iv_cache = BoundedCache(max_size=500)
_IV_CACHE_TTL = 1800  # 30 minutes

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
                return {
                    "symbol": symbol,
                    "skew": 0,
                    "state": "N/A",
                    "error": "No expiries",
                }

            # 尋找最近的月期權 (假設距離今天 20-45 天)
            today = datetime.now()
            target_expiry = None
            for exp in expiries:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d")
                days_to_expiry = (exp_dt - today).days
                if 20 <= days_to_expiry <= 45:
                    target_expiry = exp
                    break

            if not target_expiry:
                target_expiry = expiries[0]  # 回退到最近的一個

            chain = await market_data_service.get_option_chain(symbol, target_expiry)
            if not chain:
                return {"symbol": symbol, "skew": 0, "state": "N/A"}

            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0)
            if spot_price == 0:
                return {"symbol": symbol, "skew": 0, "state": "N/A"}

            calls = chain.calls
            puts = chain.puts

            # 尋找 OTM 25-Delta 附近的期權 (簡化版：使用距離現價一定比例的 Strike)
            # 實務上應使用 py_vollib 計算 Delta，此處先用 Strike 偏移量作為代理
            # 25 Delta Call 通常在現價 + 5~10%
            # 25 Delta Put 通常在現價 - 5~10%

            otm_call = (
                calls[calls["strike"] > spot_price * 1.05].iloc[0]
                if not calls[calls["strike"] > spot_price * 1.05].empty
                else None
            )
            otm_put = (
                puts[puts["strike"] < spot_price * 0.95].iloc[-1]
                if not puts[puts["strike"] < spot_price * 0.95].empty
                else None
            )

            if otm_call is None or otm_put is None:
                return {"symbol": symbol, "skew": 0, "state": "數據不足"}

            iv_call = otm_call["impliedVolatility"]
            iv_put = otm_put["impliedVolatility"]
            skew_val = (iv_put - iv_call) * 100  # 以百分點表示

            state = "正常"
            if skew_val > 5:
                state = "⚠️ 預警性對沖 (Put 昂貴)"
            elif skew_val < -2:
                state = "🚀 看多情緒濃厚 (Call 昂貴)"

            # 儲存到資料庫以便後續計算百分位
            SentimentEngine.save_sentiment_history(symbol, "SKEW", skew_val)

            return {
                "symbol": symbol,
                "skew": round(skew_val, 2),
                "iv_put": round(iv_put, 4),
                "iv_call": round(iv_call, 4),
                "state": state,
                "expiry": target_expiry,
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
                if not chain:
                    continue
                total_put_vol += chain.puts["volume"].sum()
                total_call_vol += chain.calls["volume"].sum()

            if total_call_vol == 0:
                return {"symbol": symbol, "pcr": 0, "state": "N/A"}

            pcr_val = total_put_vol / total_call_vol

            state = "平衡"
            if pcr_val > 1.0:
                state = "🐻 偏向空頭"
            elif pcr_val < 0.6:
                state = "🐂 市場過熱 (Extreme Greed)"

            SentimentEngine.save_sentiment_history(symbol, "PCR", pcr_val)

            return {
                "symbol": symbol,
                "pcr": round(pcr_val, 2),
                "put_vol": total_put_vol,
                "call_vol": total_call_vol,
                "state": state,
            }
        except Exception as e:
            logger.error(f"[{symbol}] PCR 計算失敗: {e}")
            return {"symbol": symbol, "pcr": 0, "state": "ERROR"}

    @staticmethod
    async def calculate_max_pain(
        symbol: str, expiry: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        計算最大痛點 (Max Pain)。
        邏輯：尋找讓所有期權買家總價值最小化的標的價格。
        """
        try:
            if not expiry:
                expiries = await market_data_service.get_all_option_expiries(symbol)
                if not expiries:
                    return {"error": "No expiries"}
                expiry = expiries[0]

            chain = await market_data_service.get_option_chain(symbol, expiry)
            if not chain:
                return {"error": "No chain data"}

            calls = chain.calls.dropna(subset=["openInterest"])
            puts = chain.puts.dropna(subset=["openInterest"])

            # 彙整所有履約價
            strikes = sorted(list(set(calls["strike"]) | set(puts["strike"])))

            # 計算每個履約價下的總痛點 (Dollar Value if expired there)
            pains = []
            for s in strikes:
                # Call Pain: max(0, spot - strike) * OI
                call_pain = (
                    calls[calls["strike"] < s]
                    .apply(lambda x: (s - x["strike"]) * x["openInterest"], axis=1)
                    .sum()
                )
                # Put Pain: max(0, strike - spot) * OI
                put_pain = (
                    puts[puts["strike"] > s]
                    .apply(lambda x: (x["strike"] - s) * x["openInterest"], axis=1)
                    .sum()
                )
                pains.append(call_pain + put_pain)

            max_pain_strike = strikes[pains.index(min(pains))]

            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0)

            dist_pct = (
                (spot_price - max_pain_strike) / spot_price * 100
                if spot_price > 0
                else 0
            )

            return {
                "symbol": symbol,
                "expiry": expiry,
                "max_pain": max_pain_strike,
                "current_price": spot_price,
                "distance_pct": round(dist_pct, 2),
                "is_converging": abs(dist_pct) < 2.0,
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
            if not expiries:
                return []

            uoa_list = []
            # 檢查前兩個到期日
            for exp in expiries[:2]:
                chain = await market_data_service.get_option_chain(symbol, exp)
                if not chain:
                    continue

                for _, row in pd.concat([chain.calls, chain.puts]).iterrows():
                    # 門檻：Volume > 5 * OI 且 Volume > 500
                    if row["volume"] > 5 * row["openInterest"] and row["volume"] > 500:
                        uoa_list.append(
                            {
                                "symbol": symbol,
                                "expiry": exp,
                                "strike": row["strike"],
                                "type": "CALL"
                                if row["strike"] in chain.calls["strike"].values
                                else "PUT",
                                "volume": int(row["volume"]),
                                "oi": int(row["openInterest"]),
                                "ratio": round(
                                    row["volume"] / max(row["openInterest"], 1), 2
                                ),
                                "iv": round(row["impliedVolatility"], 4),
                            }
                        )

            return sorted(uoa_list, key=lambda x: x["volume"], reverse=True)[:5]
        except Exception as e:
            logger.error(f"[{symbol}] UOA 偵測失敗: {e}")
            return []

    @staticmethod
    def save_sentiment_history(symbol: str, indicator: str, value: float):
        """將情緒指標存入資料庫。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sentiment_history (symbol, indicator, value)
                VALUES (?, ?, ?)
            """,
                (symbol, indicator, value),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"儲存情緒歷史失敗: {e}")

    @staticmethod
    def get_indicator_percentile(
        symbol: str, indicator: str, current_value: float
    ) -> float:
        """計算目前值在歷史數據中的百分位數。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT value FROM sentiment_history
                WHERE symbol = ? AND indicator = ?
                ORDER BY timestamp DESC LIMIT 100
            """,
                (symbol, indicator),
            )
            values = [row[0] for row in cursor.fetchall()]
            conn.close()

            if not values:
                return 50.0  # 預設中值

            count = sum(1 for v in values if v < current_value)
            return (count / len(values)) * 100
        except Exception:
            return 50.0

    @staticmethod
    def get_last_stored_iv(symbol: str) -> Optional[float]:
        """從資料庫中取得最後一次記錄的 IV。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                (symbol,),
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception as e:
            logger.error(f"取得資料庫最後 IV 失敗: {e}")
        return None

    @staticmethod
    def save_historical_iv(symbol: str, iv: float, date_str: str):
        """將每日 IV 存入 database。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO historical_iv (symbol, iv, date)
                VALUES (?, ?, ?)
                """,
                (symbol, iv, date_str),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"儲存歷史 IV 失敗: {e}")

    @staticmethod
    async def fetch_and_calculate_iv_metrics(symbol: str) -> IVMetrics:
        """
        獲取並計算隱含波動率 (IV) 相關指標，包括 IV Rank, IV Percentile, 週預期震盪區間。
        具備 30 分鐘快取與資料庫持久化儲存。
        """
        symbol = symbol.upper()
        current_time = time.time()

        # Check cache
        if symbol in _iv_cache:
            cached_val, expiry = _iv_cache[symbol]
            if current_time < expiry:
                return cached_val

        try:
            # 1. 取得現價
            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0.0)
            if spot_price <= 0.0:
                # 嘗試 yfinance fallback 價格
                df_temp = await market_data_service.get_history_df(symbol, period="2d")
                if not df_temp.empty:
                    spot_price = float(df_temp["Close"].iloc[-1])

            if spot_price <= 0.0:
                raise ValueError(f"無法取得 {symbol} 的現價，無法計算預期震盪區間")

            # 2. 獲取當前 IV
            ticker = yf.Ticker(symbol)
            info = await asyncio.to_thread(lambda: ticker.info)
            current_iv = info.get("impliedVolatility")

            if not current_iv or current_iv <= 0:
                # Fallback to ATM option implied volatility from nearest chain
                try:
                    expirations = await market_data_service.get_all_option_expiries(
                        symbol
                    )
                    if expirations:
                        chain = await market_data_service.get_option_chain(
                            symbol, expirations[0]
                        )
                        if chain:
                            calls = chain.calls
                            if not calls.empty:
                                atm_call_idx = (
                                    (calls["strike"] - spot_price).abs().idxmin()
                                )
                                current_iv = float(
                                    calls.loc[atm_call_idx].get(
                                        "impliedVolatility", 0.0
                                    )
                                )
                except Exception as opt_err:
                    logger.warning(f"[{symbol}] ATM 期權鏈 IV 獲取失敗: {opt_err}")

            if not current_iv or current_iv <= 0:
                # Fallback to DB or historical volatility (HV)
                last_db_iv = SentimentEngine.get_last_stored_iv(symbol)
                if last_db_iv:
                    current_iv = last_db_iv
                else:
                    # Fallback to 30-day historical volatility
                    df_temp = await market_data_service.get_history_df(
                        symbol, period="1mo"
                    )
                    if not df_temp.empty and len(df_temp) >= 20:
                        df_temp["Log_Ret"] = np.log(
                            df_temp["Close"] / df_temp["Close"].shift(1)
                        )
                        current_iv = float(df_temp["Log_Ret"].std() * np.sqrt(252))
                    else:
                        raise ValueError(
                            f"無法獲取 {symbol} 的 IV，且歷史波動率數據不足"
                        )

            # 3. 儲存至 database historical_iv
            today_str = datetime.now().strftime("%Y-%m-%d")
            SentimentEngine.save_historical_iv(symbol, current_iv, today_str)

            # 4. 取得 DB 歷史 IV
            db_ivs = {}
            try:
                conn = sqlite3.connect(config.DB_NAME)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT date, iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 252",
                    (symbol,),
                )
                db_rows = cursor.fetchall()
                conn.close()
                db_ivs = {row[0]: row[1] for row in db_rows}
            except Exception as e:
                logger.error(f"讀取資料庫歷史 IV 失敗: {e}")

            # 5. 取得 1y K-line history 做 HV 代理 (補足資料庫歷史不足部分)
            df_hist = await market_data_service.get_history_df(symbol, period="1y")
            history_map = {}
            if not df_hist.empty:
                df_hist["Log_Ret"] = np.log(
                    df_hist["Close"] / df_hist["Close"].shift(1)
                )
                df_hist["HV_20"] = df_hist["Log_Ret"].rolling(
                    window=20
                ).std() * np.sqrt(252)
                for dt, row in df_hist.iterrows():
                    date_str = dt.strftime("%Y-%m-%d")
                    if not pd.isna(row["HV_20"]):
                        history_map[date_str] = float(row["HV_20"])

            # 6. 合併 DB 實際 IV 至 history_map
            for date_str, db_iv in db_ivs.items():
                history_map[date_str] = db_iv

            # 確保今天的值存在
            history_map[today_str] = current_iv

            history_values = list(history_map.values())
            if not history_values:
                history_values = [current_iv]

            # 7. 計算 IV Rank
            low_iv = min(history_values)
            high_iv = max(history_values)
            if high_iv > low_iv:
                iv_rank = ((current_iv - low_iv) / (high_iv - low_iv)) * 100.0
            else:
                iv_rank = 50.0

            # 8. 計算 IV Percentile
            lower_count = sum(1 for iv in history_values if iv < current_iv)
            iv_percentile = (lower_count / len(history_values)) * 100.0

            # 9. 限制範圍 0.0 - 100.0
            iv_rank = max(0.0, min(100.0, iv_rank))
            iv_percentile = max(0.0, min(100.0, iv_percentile))

            # 10. 計算 Expected Move Weekly (Stock Price * IV * sqrt(7/365))
            expected_move_weekly = spot_price * current_iv * math.sqrt(7.0 / 365.0)

            # 11. 判斷狀態
            iv_status: Literal["Low", "Normal", "High", "Extreme"]
            if iv_rank < 30.0:
                iv_status = "Low"
            elif iv_rank <= 70.0:
                iv_status = "Normal"
            elif iv_rank <= 90.0:
                iv_status = "High"
            else:
                iv_status = "Extreme"

            metrics = IVMetrics(
                symbol=symbol,
                current_iv=current_iv,
                iv_rank=iv_rank,
                iv_percentile=iv_percentile,
                expected_move_weekly=expected_move_weekly,
                iv_status=iv_status,
            )

            # 12. 寫入快取
            _iv_cache[symbol] = (metrics, current_time + _IV_CACHE_TTL)
            return metrics

        except Exception as e:
            logger.error(f"[{symbol}] IV 指標計算失敗: {e}")
            # 回傳預設降級指標
            return IVMetrics(
                symbol=symbol,
                current_iv=0.0,
                iv_rank=0.0,
                iv_percentile=0.0,
                expected_move_weekly=0.0,
                iv_status="Normal",
            )
