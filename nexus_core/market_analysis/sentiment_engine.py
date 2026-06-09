import logging
import pandas as pd
import numpy as np
import sqlite3
import time
import math
import asyncio
import yfinance as yf
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Literal, Optional
from services import market_data_service
from services.market_data_service import BoundedCache
import config
from models.quant import IVMetrics
from market_time import is_market_open
from market_analysis.uoa_telemetry import UOATradeInput, classify_uoa_trade


_iv_cache = BoundedCache(max_size=500)
_IV_CACHE_TTL = 1200  # 20 minutes


logger = logging.getLogger(__name__)


def _current_week_friday() -> date:
    """取得本週五日期。若今天已過週五（週六/週日），則取下週五。"""
    today = date.today()
    days_ahead = 4 - today.weekday()  # Friday = 4
    if days_ahead < 0:  # Today is Saturday or Sunday
        days_ahead += 7
    return today + timedelta(days=days_ahead)


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

            iv_call = float(otm_call["impliedVolatility"])
            iv_put = float(otm_put["impliedVolatility"])

            # --- Rigid definition (must not drift) ---
            # Option Skew = IV(OTM Put) - IV(OTM Call)
            skew_val = (iv_put - iv_call) * 100  # percentage points

            # 儲存到資料庫以便後續計算百分位
            SentimentEngine.save_sentiment_history(symbol, "SKEW", skew_val)
            skew_percentile = SentimentEngine.get_indicator_percentile(
                symbol, "SKEW", skew_val
            )

            # --- Rigid label mapping (sign + percentile must be consistent) ---
            # +Skew @ high percentile => Put expensive => downside hedging demand
            # -Skew @ low percentile  => Call expensive => upside chasing demand
            if skew_val > 0 and skew_percentile >= 80.0:
                state = (
                    "⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）"
                )
            elif skew_val < 0 and skew_percentile <= 20.0:
                state = "🔥 市場上行看漲需求爆發，動能抄底/追高情緒極端亢奮（散戶搶購末日 Call）"
            elif skew_val > 0:
                state = "左偏 (Put 昂貴)"
            elif skew_val < 0:
                state = "右偏 (Call 昂貴)"
            else:
                state = "平穩"

            return {
                "symbol": symbol,
                "skew": round(skew_val, 2),
                "skew_percentile": float(round(skew_percentile, 2)),
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
        symbol: str, expiry: Optional[str] = None, _retry: bool = False
    ) -> Dict[str, Any]:
        """
        計算最大痛點 (Max Pain)。
        邏輯：尋找讓所有期權買家總價值最小化的標的價格。
        """
        from database.cache import get_kv_cache, save_kv_cache
        from datetime import datetime

        # 0. 預先取得現價，用於快取失效判定
        spot_price = 0.0
        try:
            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0.0) if quote else 0.0
        except Exception as e:
            logger.warning(f"[{symbol}] calculate_max_pain 預先取得現價失敗: {e}")

        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")
        cache_key = f"max_pain_{symbol.upper()}_{expiry or 'first'}_{today_str}"
        cached = get_kv_cache(cache_key)
        if cached is not None:
            cached_price = cached.get("current_price", 0.0)
            if cached_price > 0 and spot_price > 0:
                deviation = abs(spot_price - cached_price) / cached_price
                if deviation > 0.02:
                    logger.warning(
                        f"[{symbol}] Spot price shifted from {cached_price} to {spot_price} "
                        f"(dev={deviation:.2%}), invalidating max_pain kv_cache"
                    )
                    cached = None
            if cached is not None:
                return cached

        try:
            if expiry:
                try:
                    exp_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
                    if (exp_dt - today).days > 30:
                        logger.warning(
                            f"[{symbol}] 指定到期日 {expiry} 超過 30 天，物理阻斷"
                        )
                        return {
                            "symbol": symbol,
                            "max_pain": None,
                            "current_price": spot_price,
                            "distance_pct": 0.0,
                            "is_converging": False,
                            "data_status": "Data_Missing",
                            "error": "Data_Missing",
                        }
                except ValueError:
                    pass

            if not expiry:
                expiries = await market_data_service.get_all_option_expiries(symbol)
                if not expiries:
                    return {"error": "No expiries"}

                # 嚴格到期日過濾：鎖定本週五即期到期合約，物理剔除 LEAPs
                target_friday = _current_week_friday()
                # 也接受半週到期 (Monday/Wednesday mini-options)
                acceptable_dates = [
                    target_friday,
                    target_friday - timedelta(days=2),  # Wednesday
                    target_friday - timedelta(days=4),  # Monday
                ]
                weekly_expiries: list[str] = []
                near_term_expiries: list[str] = []
                for exp in expiries:
                    try:
                        exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                        if exp_dt < today:
                            continue
                        if exp_dt in acceptable_dates:
                            weekly_expiries.append(exp)
                        elif (exp_dt - today).days <= 14:
                            near_term_expiries.append(exp)
                    except ValueError:
                        continue

                if weekly_expiries:
                    # 優先選擇本週五
                    friday_str = target_friday.strftime("%Y-%m-%d")
                    expiry = (
                        friday_str
                        if friday_str in weekly_expiries
                        else weekly_expiries[0]
                    )
                elif near_term_expiries:
                    expiry = near_term_expiries[0]
                    logger.warning(
                        f"[{symbol}] 無本週五到期合約，回退至最近到期日: {expiry}"
                    )
                else:
                    logger.warning(
                        f"[{symbol}] 無本週及 14 天內到期合約，物理性阻斷並標記為 Data_Missing"
                    )
                    return {
                        "symbol": symbol,
                        "max_pain": None,
                        "current_price": spot_price,
                        "distance_pct": 0.0,
                        "is_converging": False,
                        "data_status": "Data_Missing",
                        "error": "Data_Missing",
                    }

            chain = await market_data_service.get_option_chain(symbol, expiry)
            if not chain:
                return {
                    "symbol": symbol,
                    "max_pain": None,
                    "current_price": spot_price,
                    "distance_pct": 0.0,
                    "is_converging": False,
                    "data_status": "Data_Missing",
                    "error": "No chain data",
                }

            calls = chain.calls.copy()
            puts = chain.puts.copy()

            # 確保欄位存在並填補空值
            for df in [calls, puts]:
                if "openInterest" not in df.columns:
                    df["openInterest"] = 0.0
                else:
                    df["openInterest"] = df["openInterest"].fillna(0.0)
                if "volume" not in df.columns:
                    df["volume"] = 0.0
                else:
                    df["volume"] = df["volume"].fillna(0.0)

            # 檢測當期未平倉量 (OI) 是否低於閾值 (例如 100)
            total_oi = calls["openInterest"].sum() + puts["openInterest"].sum()
            if total_oi < 100:
                logger.warning(
                    f"[{symbol}] Expiry {expiry} total open interest ({total_oi}) is below threshold 100. "
                    "Forcing Data_Missing status to prevent stale data pollution."
                )
                return {
                    "symbol": symbol,
                    "max_pain": None,
                    "current_price": spot_price,
                    "distance_pct": 0.0,
                    "is_converging": False,
                    "data_status": "Data_Missing",
                    "error": "Data_Missing",
                }

            # 取得即時股價
            if spot_price <= 0.0:
                quote = await market_data_service.get_quote(symbol)
                spot_price = quote.get("c", 0.0) if quote else 0.0

            # 確定性拆股因子校準 (Deterministic Split-Adjustment)
            splits = await market_data_service.get_stock_splits(symbol)
            if splits is not None and not splits.empty and spot_price > 0:
                # 計算累積拆股因子：所有歷史拆股比率的乘積
                cumulative_factor = 1.0
                for split_date, ratio in splits.items():
                    if ratio > 0.0 and ratio != 1.0:
                        cumulative_factor *= ratio

                if cumulative_factor > 1.0:
                    # 偵測並清洗未經調整的歷史 Strike
                    for df in [calls, puts]:
                        if df.empty:
                            continue
                        new_strikes = []
                        new_oi = []
                        new_vol = []
                        for _, row in df.iterrows():
                            k = row["strike"]
                            oi = row["openInterest"]
                            vol = row["volume"]
                            # 若 Strike 明顯偏離現價（超過 2 倍），嘗試以累積因子校準
                            if k > spot_price * 2.0:
                                adjusted_k = k / cumulative_factor
                                # 驗證調整後 Strike 落在合理區間內 (現價 ±100%)
                                if 0.5 * spot_price <= adjusted_k <= 2.0 * spot_price:
                                    logger.info(
                                        f"[{symbol}] Split-adj: Strike ${k:.2f} → "
                                        f"${adjusted_k:.2f} (factor={cumulative_factor:.1f})"
                                    )
                                    k = adjusted_k
                                    oi = oi * cumulative_factor
                                    vol = vol * cumulative_factor
                            new_strikes.append(k)
                            new_oi.append(oi)
                            new_vol.append(vol)
                        df["strike"] = new_strikes
                        df["openInterest"] = new_oi
                        df["volume"] = new_vol

            # 過濾掉 OI 為 0 且 Volume 為 0 的死合約
            calls = calls[(calls["openInterest"] > 0) | (calls["volume"] > 0)]
            puts = puts[(puts["openInterest"] > 0) | (puts["volume"] > 0)]

            # 檢查 OI 總量是否大於 0，否則退化回使用成交量 (Volume) 作為權重
            total_oi = calls["openInterest"].sum() + puts["openInterest"].sum()
            use_volume = False
            if total_oi == 0:
                total_vol = calls["volume"].sum() + puts["volume"].sum()
                if total_vol > 0:
                    use_volume = True
                else:
                    return {
                        "error": "No active options contracts (OI and Volume are both 0)"
                    }

            # 彙整所有履約價
            strikes = sorted(list(set(calls["strike"]) | set(puts["strike"])))
            if not strikes:
                return {"error": "No strikes available"}

            # 過濾極端履約價防止數據扭曲 (現價 25% ~ 400% 區間)
            if spot_price > 0:
                strikes = [
                    s for s in strikes if spot_price * 0.25 <= s <= spot_price * 4.0
                ]
                if not strikes:
                    strikes = sorted(list(set(calls["strike"]) | set(puts["strike"])))

            # 計算每個履約價下的總痛點 (Dollar Value if expired there)
            pains = []
            weight_col = "volume" if use_volume else "openInterest"
            for s in strikes:
                # Call Pain: max(0, spot - strike) * Weight
                call_sub = calls[calls["strike"] < s]
                call_pain = (call_sub[weight_col] * (s - call_sub["strike"])).sum()

                # Put Pain: max(0, strike - spot) * Weight
                put_sub = puts[puts["strike"] > s]
                put_pain = (put_sub[weight_col] * (put_sub["strike"] - s)).sum()

                pains.append(call_pain + put_pain)

            max_pain_strike = strikes[pains.index(min(pains))]

            # 30% 偏離度異常防禦
            from services.market_data_service import (
                check_and_reconcile_max_pain_anomaly,
            )

            if (
                spot_price > 0
                and abs(max_pain_strike - spot_price) / spot_price > 0.30
                and not _retry
            ):
                # 觸發警告與資料庫快取清理
                check_and_reconcile_max_pain_anomaly(
                    symbol, max_pain_strike, spot_price
                )
                logger.info(
                    f"[{symbol}] Retrying calculate_max_pain after cache purge..."
                )
                return await SentimentEngine.calculate_max_pain(
                    symbol, expiry, _retry=True
                )

            dist_pct = (
                (max_pain_strike - spot_price) / spot_price * 100
                if spot_price > 0
                else 0
            )

            result = {
                "symbol": symbol,
                "expiry": expiry,
                "max_pain": max_pain_strike,
                "current_price": spot_price,
                "distance_pct": round(dist_pct, 2),
                "is_converging": abs(dist_pct) < 2.0,
            }
            save_kv_cache(cache_key, result)
            return result
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

            # 取得即時現價以提供 UOA 的 ITM/OTM 分類
            spot_price = 0.0
            try:
                quote = await market_data_service.get_quote(symbol)
                spot_price = quote.get("c", 0.0) if quote else 0.0
            except Exception as e:
                logger.warning(f"[{symbol}] detect_uoa 取得現價失敗: {e}")

            uoa_list = []
            # 檢查前兩個到期日
            for exp in expiries[:2]:
                chain = await market_data_service.get_option_chain(symbol, exp)
                if not chain:
                    continue

                for _, row in pd.concat([chain.calls, chain.puts]).iterrows():
                    # 門檻：Volume > 5 * OI 且 Volume > 500
                    if row["volume"] > 5 * row["openInterest"] and row["volume"] > 500:
                        trade_type = row.get("trade_type")
                        if not trade_type:
                            trade_type = (
                                "BLOCK"
                                if (row["volume"] > 1500 and row["volume"] % 100 == 0)
                                else "SWEEP"
                            )

                        oi_change_net = row.get("oi_change_net")
                        if oi_change_net is None:
                            oi_change_net = int(row["volume"] - row["openInterest"])
                        else:
                            oi_change_net = int(oi_change_net)

                        opt_type = (
                            "CALL"
                            if row["strike"] in chain.calls["strike"].values
                            else "PUT"
                        )
                        trade_price = (
                            float(row["lastPrice"])
                            if "lastPrice" in row and pd.notna(row["lastPrice"])
                            else 0.0
                        )
                        bid_price = (
                            float(row["bid"])
                            if "bid" in row and pd.notna(row["bid"])
                            else 0.0
                        )
                        ask_price = (
                            float(row["ask"])
                            if "ask" in row and pd.notna(row["ask"])
                            else 0.0
                        )

                        trade_input = UOATradeInput(
                            expiry=exp,
                            strike_price=float(row["strike"]),
                            option_type=opt_type,
                            trade_price=trade_price,
                            bid_price=bid_price,
                            ask_price=ask_price,
                            volume=int(row["volume"]),
                            open_interest=int(row["openInterest"]),
                            symbol=symbol,
                        )
                        result = classify_uoa_trade(
                            trade_input, current_price=spot_price
                        )

                        uoa_list.append(
                            {
                                "symbol": symbol,
                                "expiry": exp,
                                "strike": result.strike_price,
                                "type": result.option_type,
                                "volume": result.volume,
                                "oi": result.open_interest,
                                "ratio": result.ratio,
                                "ratio_str": result.ratio_str,
                                "trade_price": result.trade_price,
                                "bid_price": result.bid_price,
                                "ask_price": result.ask_price,
                                "action": result.action,
                                "intent": result.intent,
                                "iv": round(row["impliedVolatility"], 4)
                                if "impliedVolatility" in row
                                and pd.notna(row["impliedVolatility"])
                                else 0.0,
                                "trade_type": trade_type,
                                "oi_change_net": oi_change_net,
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
    async def _calculate_straddle_implied_em(
        symbol: str, spot_price: float
    ) -> float | None:
        """以 ATM Straddle 權利金總和計算預期區間。

        公式: Expected Move ≈ ATM Straddle Price × 0.85
        此為業界標準的 1-sigma 近似法，直接反映造市商對短期波動的定價。
        """
        try:
            expiries = await market_data_service.get_all_option_expiries(symbol)
            if not expiries:
                return None

            # 選擇最近且尚未到期的到期日
            today_dt = datetime.now().date()
            target_expiry = None
            for exp in expiries:
                try:
                    exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                    if exp_dt >= today_dt and (exp_dt - today_dt).days <= 14:
                        target_expiry = exp
                        break
                except ValueError:
                    continue
            if target_expiry is None:
                return None

            chain = await market_data_service.get_option_chain(symbol, target_expiry)
            if chain is None:
                return None

            calls = chain.calls
            puts = chain.puts
            if calls is None or calls.empty or puts is None or puts.empty:
                return None

            # 尋找最接近 ATM 的 Call 和 Put
            call_atm_idx = (calls["strike"] - spot_price).abs().idxmin()
            put_atm_idx = (puts["strike"] - spot_price).abs().idxmin()

            call_atm = calls.loc[call_atm_idx]
            put_atm = puts.loc[put_atm_idx]

            # 使用 mid price (bid+ask)/2，若無 bid/ask 則用 lastPrice
            def _mid(row):
                bid = float(row.get("bid", 0.0) or 0.0)
                ask = float(row.get("ask", 0.0) or 0.0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
                return float(row.get("lastPrice", 0.0) or 0.0)

            call_mid = _mid(call_atm)
            put_mid = _mid(put_atm)

            if call_mid <= 0 and put_mid <= 0:
                return None

            straddle_price = call_mid + put_mid
            if straddle_price <= 0:
                return None

            # 業界標準 0.85 因子（1-sigma 近似）
            em = straddle_price * 0.85

            logger.info(
                f"[{symbol}] Straddle-Implied EM: Call_mid=${call_mid:.2f} + "
                f"Put_mid=${put_mid:.2f} = Straddle ${straddle_price:.2f} × 0.85 = ±${em:.2f}"
            )
            return em

        except Exception as e:
            logger.warning(f"[{symbol}] Straddle-Implied EM calculation failed: {e}")
            return None

    @staticmethod
    async def fetch_and_calculate_iv_metrics(symbol: str) -> IVMetrics:
        """
        獲取並計算隱含波動率 (IV) 相關指標，包括 IV Rank, IV Percentile, 週預期震盪區間。
        具備 30 分鐘快取與資料庫持久化儲存。
        """
        symbol = symbol.upper()
        current_time = time.time()

        # 0. 預先獲取現價，用於快取失效比對
        spot_price = 0.0
        try:
            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0.0) if quote else 0.0
            if spot_price <= 0.0:
                # 嘗試 yfinance fallback 價格
                df_temp = await market_data_service.get_history_df(symbol, period="2d")
                if not df_temp.empty:
                    spot_price = float(df_temp["Close"].iloc[-1])
        except Exception as e:
            logger.warning(f"[{symbol}] 預先取得現價失敗: {e}")

        # Check cache
        if symbol in _iv_cache:
            cached_val, expiry = _iv_cache[symbol]
            if current_time < expiry:
                ref_price = getattr(cached_val, "reference_spot_price", None)
                if ref_price and ref_price > 0 and spot_price > 0:
                    deviation = abs(spot_price - ref_price) / ref_price
                    if deviation <= 0.02:
                        return cached_val
                    else:
                        logger.warning(
                            f"[{symbol}] Spot price shifted from {ref_price} to {spot_price} "
                            f"(dev={deviation:.2%}), invalidating memory cache"
                        )
                else:
                    return cached_val

        # Check SQLite kv_cache next for same-day warm cache
        from database.cache import get_kv_cache, save_kv_cache
        from datetime import datetime

        today_str = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"iv_metrics_{symbol}_{today_str}"
        cached = get_kv_cache(cache_key)
        if cached is not None:
            try:
                metrics = IVMetrics(**cached)
                ref_price = getattr(metrics, "reference_spot_price", None)
                use_cache = True
                if ref_price and ref_price > 0 and spot_price > 0:
                    deviation = abs(spot_price - ref_price) / ref_price
                    if deviation > 0.02:
                        logger.warning(
                            f"[{symbol}] Spot price shifted from {ref_price} to {spot_price} "
                            f"(dev={deviation:.2%}), invalidating kv_cache"
                        )
                        use_cache = False
                if use_cache:
                    _iv_cache[symbol] = (metrics, current_time + 1800)
                    return metrics
            except Exception as e:
                logger.warning(
                    f"[{symbol}] Failed to restore IVMetrics from kv_cache: {e}"
                )

        try:
            # 1. 取得現價
            if spot_price <= 0.0:
                quote = await market_data_service.get_quote(symbol)
                spot_price = quote.get("c", 0.0)
                if spot_price <= 0.0:
                    # 嘗試 yfinance fallback 價格
                    df_temp = await market_data_service.get_history_df(
                        symbol, period="2d"
                    )
                    if not df_temp.empty:
                        spot_price = float(df_temp["Close"].iloc[-1])

            if spot_price <= 0.0:
                raise ValueError(f"無法取得 {symbol} 的現價，無法計算預期震盪區間")

            # 2. 獲取當前 IV
            current_iv: float | None = None
            iv_source: Literal["LIVE_IV", "STORED_IV", "HV_PROXY", "UNAVAILABLE"] = (
                "UNAVAILABLE"
            )
            is_market_active = is_market_open()

            # 盤前優先嘗試取得前一交易日存入的 IV (歷史最近一筆紀錄)
            if not is_market_active:
                last_db_iv = SentimentEngine.get_last_stored_iv(symbol)
                if last_db_iv and last_db_iv > 0:
                    current_iv = last_db_iv
                    iv_source = "STORED_IV"
                    logger.info(
                        f"[{symbol}] 偵測為非交易時段，優先採用前日收盤 SQLite 歷史 IV: {current_iv}"
                    )

            if not current_iv or current_iv <= 0:
                ticker = yf.Ticker(symbol)
                try:
                    info = await asyncio.to_thread(lambda: ticker.info)
                    current_iv = info.get("impliedVolatility")
                    if current_iv and current_iv > 0:
                        iv_source = "LIVE_IV" if is_market_active else "STORED_IV"
                except Exception as e:
                    logger.warning(f"[{symbol}] yfinance ticker.info 獲取異常: {e}")

            if not current_iv or current_iv <= 0:
                # VIX-style forward-looking weighted average implied volatility calculation
                # across the nearest front-month options chains
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
                            puts = chain.puts
                            all_options = []
                            for df in [calls, puts]:
                                if df is not None and not df.empty:
                                    for _, row in df.iterrows():
                                        iv_val = float(
                                            row.get("impliedVolatility", 0.0)
                                        )
                                        strike_val = float(row.get("strike", 0.0))
                                        oi = float(row.get("openInterest", 0.0))
                                        vol = float(row.get("volume", 0.0))
                                        if iv_val > 0.01 and strike_val > 0.0:
                                            # Filter to options near-the-money (within 20% of spot)
                                            if (
                                                abs(strike_val - spot_price)
                                                / spot_price
                                                <= 0.20
                                            ):
                                                # VIX-style weight: liquid options close to ATM are weighted higher
                                                dist = abs(strike_val - spot_price)
                                                weight = (oi + vol + 1.0) / (dist + 1.0)
                                                all_options.append((iv_val, weight))
                            if all_options:
                                total_weight = sum(w for _, w in all_options)
                                if total_weight > 0:
                                    current_iv = (
                                        sum(iv * w for iv, w in all_options)
                                        / total_weight
                                    )
                                    iv_source = (
                                        "LIVE_IV" if is_market_active else "STORED_IV"
                                    )
                                    logger.info(
                                        f"[{symbol}] VIX-style weighted average IV computed: {current_iv:.4f} "
                                        f"across {len(all_options)} liquid contracts."
                                    )
                except Exception as opt_err:
                    logger.warning(
                        f"[{symbol}] VIX-style weighted average IV calculation failed: {opt_err}"
                    )

            if not current_iv or current_iv <= 0:
                # Fallback to DB or historical volatility (HV)
                last_db_iv = SentimentEngine.get_last_stored_iv(symbol)
                if last_db_iv and last_db_iv > 0:
                    current_iv = last_db_iv
                    iv_source = "STORED_IV"
                else:
                    # Fallback to 30-day historical volatility (HV proxy)
                    df_temp = await market_data_service.get_history_df(
                        symbol, period="1mo"
                    )
                    if not df_temp.empty and len(df_temp) >= 20:
                        df_temp["Log_Ret"] = np.log(
                            df_temp["Close"] / df_temp["Close"].shift(1)
                        )
                        current_iv = float(df_temp["Log_Ret"].std() * np.sqrt(252))
                        iv_source = "HV_PROXY"
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

            # Rule 4: If IV_Rank > 70%, current_iv cannot physically scale down to near-zero levels (<5%).
            # Trigger an extraction error flag if conflict occurs.
            if iv_rank > 70.0 and current_iv < 0.05:
                raise ValueError(
                    f"Conflict detected: IV Rank is high ({iv_rank:.1f}%) but Implied Volatility is suspiciously low ({current_iv * 100:.1f}%)."
                )

            # 10. 計算 Expected Move Weekly — 多層降級防禦
            # 優先級: (1) Straddle-Implied EM → (2) IV-based EM → (3) HV-20 proxy EM
            em_from_iv = (
                spot_price * current_iv * math.sqrt(7.0 / 365.0)
                if current_iv > 0.001
                else 0.0
            )

            straddle_em = await SentimentEngine._calculate_straddle_implied_em(
                symbol, spot_price
            )

            if straddle_em and straddle_em > 0:
                expected_move_weekly = straddle_em
                # 交叉驗證：若 IV-based EM 也可用，取兩者較大值以反映尾部風險
                if em_from_iv > 0:
                    expected_move_weekly = max(straddle_em, em_from_iv)
            elif em_from_iv > 0:
                expected_move_weekly = em_from_iv
            else:
                # 最終降級：使用 HV-20 代理
                hv_proxy = 0.0
                if not df_hist.empty and "HV_20" in df_hist.columns:
                    last_hv = df_hist["HV_20"].dropna()
                    if not last_hv.empty:
                        hv_proxy = float(last_hv.iloc[-1])
                expected_move_weekly = (
                    spot_price * max(hv_proxy, 0.15) * math.sqrt(7.0 / 365.0)
                )
                logger.warning(
                    f"[{symbol}] Zero-IV fallback: using HV-20 proxy ({hv_proxy:.4f}) "
                    f"for EM calculation. EM=${expected_move_weekly:.2f}"
                )

            # 最終安全閥：若所有降級路徑皆產出零寬度 EM，強制使用 15% 年化波動率底線
            # 防止 $X ~ $X 的零寬度預期區間顯示 (e.g. DELL $400.77 ~ $400.77)
            if expected_move_weekly <= 0 and spot_price > 0:
                expected_move_weekly = spot_price * 0.15 * math.sqrt(7.0 / 365.0)
                logger.warning(
                    f"[{symbol}] CRITICAL: All EM fallbacks exhausted, "
                    f"using 15% floor. EM=${expected_move_weekly:.2f}"
                )

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
                is_premarket=not is_market_active,
                iv_source=iv_source,
                reference_spot_price=spot_price,
            )

            # 12. 寫入快取
            _iv_cache[symbol] = (metrics, current_time + _IV_CACHE_TTL)
            try:
                save_kv_cache(cache_key, metrics.model_dump())
            except Exception as e:
                logger.warning(f"[{symbol}] Failed to save IVMetrics to kv_cache: {e}")
            return metrics

        except ValueError as ve:
            logger.warning(f"[{symbol}] IV 指標計算退級: {ve}")
            return IVMetrics(
                symbol=symbol,
                current_iv=0.0,
                iv_rank=0.0,
                iv_percentile=0.0,
                expected_move_weekly=0.0,
                iv_status="Normal",
                is_premarket=True,
                iv_source="UNAVAILABLE",
                reference_spot_price=spot_price,
            )
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
                is_premarket=True,
                iv_source="UNAVAILABLE",
                reference_spot_price=spot_price,
            )
