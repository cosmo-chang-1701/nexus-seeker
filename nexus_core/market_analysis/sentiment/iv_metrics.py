from .history_storage import get_last_stored_iv, save_historical_iv
import logging
import pandas as pd
import numpy as np
import sqlite3  # noqa: F401
import time
import math
import asyncio
import yfinance as yf
from datetime import datetime, timedelta
from typing import Literal
from services import market_data_service
from models.quant import IVMetrics
from market_time import is_market_open


from .cache import _iv_cache, _IV_CACHE_TTL


logger = logging.getLogger(__name__)
_TERM_STRUCTURE_MIN_IV = 0.01


class IVContext:
    """Centralized Expected Move context builder shared by UI surfaces."""

    @staticmethod
    def _safe_float(value) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def resolve_reference_price(
        cls, quote: dict | None = None, iv_metrics=None
    ) -> float:
        if isinstance(quote, dict):
            prev_close = cls._safe_float(quote.get("pc"))
            if prev_close > 0.0:
                return prev_close

        if iv_metrics is not None:
            if hasattr(iv_metrics, "reference_spot_price"):
                ref_price = cls._safe_float(getattr(iv_metrics, "reference_spot_price"))
            elif isinstance(iv_metrics, dict):
                ref_price = cls._safe_float(iv_metrics.get("reference_spot_price"))
            else:
                ref_price = 0.0

            if ref_price > 0.0:
                return ref_price

        if isinstance(quote, dict):
            current_price = cls._safe_float(quote.get("c"))
            if current_price > 0.0:
                return current_price

        return 0.0

    @classmethod
    def build_expected_move(
        cls,
        symbol: str,
        *,
        expected_move_weekly: float | None,
        reference_price: float | None,
        current_price: float | None = None,
    ) -> dict:
        em_weekly = cls._safe_float(expected_move_weekly)
        ref_price = cls._safe_float(reference_price)
        spot_price = cls._safe_float(current_price)

        if ref_price > 0.0 and em_weekly > 0.0:
            # Fix floating point precision error ($0.01 deviation) by explicitly rounding
            # to 2 decimal places using Python's native banker's rounding (ROUND_HALF_EVEN).
            # This exactly matches the behavior of `.2f` string formatters in the UI layer.
            ref_rounded = round(ref_price, 2)
            em_rounded = round(em_weekly, 2)
            lower = round(ref_rounded - em_rounded, 2)
            upper = round(ref_rounded + em_rounded, 2)
        else:
            lower = 0.0
            upper = 0.0

        return {
            "symbol": symbol.upper(),
            "reference_price": ref_price,
            "current_price": spot_price,
            "expected_move_weekly": em_weekly,
            "expected_move_lower": lower,
            "expected_move_upper": upper,
        }

    @classmethod
    async def get_expected_move(
        cls, symbol: str, *, quote: dict | None = None, iv_metrics=None
    ) -> dict:
        symbol = symbol.upper()
        if quote is None:
            quote = await market_data_service.get_quote(symbol)
        if iv_metrics is None:
            iv_metrics = await fetch_and_calculate_iv_metrics(symbol)

        if hasattr(iv_metrics, "expected_move_weekly"):
            em_weekly = getattr(iv_metrics, "expected_move_weekly", None)
        elif isinstance(iv_metrics, dict):
            em_weekly = iv_metrics.get("expected_move_weekly")
        else:
            em_weekly = None

        reference_price = cls.resolve_reference_price(quote, iv_metrics)
        current_price = (
            cls._safe_float(quote.get("c")) if isinstance(quote, dict) else 0.0
        )

        return cls.build_expected_move(
            symbol,
            expected_move_weekly=em_weekly,
            reference_price=reference_price,
            current_price=current_price,
        )


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
        target_dte = 1
        for exp in expiries:
            try:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_dt - today_dt).days
                if dte >= 0 and dte <= 14:
                    target_expiry = exp
                    target_dte = max(1, dte)
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

        # 業界標準 0.85 因子（1-sigma 近似），並依據實際 DTE 時間平移至週預期 (7 days)
        # 修正：為了防止 DTE < 7 且包含財報等事件時被開根號錯誤放大 (Event-Jump Extrapolation)，
        # 分母使用 max(7.0, target_dte)，即只對 DTE > 7 的區間進行壓縮，小於 7 天則保持原值。
        # 不足的天數方差會由後續的 max(straddle_em, em_from_iv) 透過 30天期 IV 自動補足。
        raw_em = straddle_price * 0.85
        em = raw_em * math.sqrt(7.0 / max(7.0, target_dte))

        logger.info(
            f"[{symbol}] Straddle-Implied EM (Normalized): Call_mid=${call_mid:.2f} + "
            f"Put_mid=${put_mid:.2f} = Straddle ${straddle_price:.2f} × 0.85 (DTE: {target_dte}) -> Weekly EM ±${em:.2f}"
        )
        return em

    except Exception as e:
        logger.warning(f"[{symbol}] Straddle-Implied EM calculation failed: {e}")
        return None


async def _calculate_iv_term_structure(
    symbol: str, spot_price: float
) -> tuple[str | None, float | None]:
    """計算 IV 期限結構 (Term Structure)。

    提取近月 (Near Term: <= 14 days) 與遠月 (Far Term: 15-60 days) 的 ATM IV。
    若 Near IV > Far IV * 1.05，視為 Backwardation (倒掛，短期風險極高，買 Call 易受 IV Crush)。
    若 Near IV < Far IV * 0.95，視為 Contango (正價差)。
    否則為 Normal。
    """
    try:
        expiries = await market_data_service.get_all_option_expiries(symbol)
        if not expiries:
            return None, None

        today_dt = datetime.now().date()
        near_expiry = None
        far_expiry = None

        for exp in expiries:
            try:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                days = (exp_dt - today_dt).days
                if 0 <= days <= 14 and not near_expiry:
                    near_expiry = exp
                elif 15 <= days <= 60 and not far_expiry:
                    far_expiry = exp
            except ValueError:
                continue

        if not near_expiry or not far_expiry:
            return None, None

        near_chain, far_chain = await asyncio.gather(
            market_data_service.get_option_chain(symbol, near_expiry),
            market_data_service.get_option_chain(symbol, far_expiry),
        )

        def _get_atm_iv(chain) -> float | None:
            if chain is None or chain.calls.empty or chain.puts.empty:
                return None
            call_idx = (chain.calls["strike"] - spot_price).abs().idxmin()
            put_idx = (chain.puts["strike"] - spot_price).abs().idxmin()
            call_iv = float(
                chain.calls.loc[call_idx].get("impliedVolatility", 0.0) or 0.0
            )
            put_iv = float(chain.puts.loc[put_idx].get("impliedVolatility", 0.0) or 0.0)
            if call_iv > 0 and put_iv > 0:
                return (call_iv + put_iv) / 2.0
            return call_iv if call_iv > 0 else (put_iv if put_iv > 0 else None)

        near_iv = _get_atm_iv(near_chain)
        far_iv = _get_atm_iv(far_chain)

        if not near_iv or near_iv < _TERM_STRUCTURE_MIN_IV:
            return None, None

        if not far_iv or far_iv < _TERM_STRUCTURE_MIN_IV:
            logger.warning(
                f"[{symbol}] IV Term Structure degraded: far IV missing or below threshold "
                f"({far_iv if far_iv is not None else 'None'} < {_TERM_STRUCTURE_MIN_IV:.2f})"
            )
            return None, None

        ratio = near_iv / far_iv
        if ratio > 1.05:
            status = "Backwardation"
        elif ratio < 0.95:
            status = "Contango"
        else:
            status = "Normal"

        logger.info(
            f"[{symbol}] IV Term Structure: Near({near_expiry})={near_iv:.1%}, Far({far_expiry})={far_iv:.1%}, Ratio={ratio:.2f} -> {status}"
        )
        return status, ratio

    except Exception as e:
        logger.warning(f"[{symbol}] IV Term Structure calculation failed: {e}")
        return None, None


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
            # If cached during pre-market, but now the market is open, bypass memory cache
            if getattr(cached_val, "is_premarket", False) and is_market_open():
                logger.info(
                    f"[{symbol}] Cached IV metrics are from pre-market, but market is now open. "
                    f"Bypassing memory cache to get fresh live IV."
                )
            else:
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
            # If cached during pre-market, but now the market is open, bypass kv_cache
            if getattr(metrics, "is_premarket", False) and is_market_open():
                logger.info(
                    f"[{symbol}] Cached IV metrics in SQLite are from pre-market, but market is now open. "
                    f"Bypassing kv_cache to get fresh live IV."
                )
                use_cache = False
            else:
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
            logger.warning(f"[{symbol}] Failed to restore IVMetrics from kv_cache: {e}")

    try:
        if spot_price <= 0.0:
            raise ValueError(f"無法取得 {symbol} 的現價，無法計算預期震盪區間")

        # 2. 獲取當前 IV
        current_iv: float | None = None
        iv_source: Literal["LIVE_IV", "STORED_IV", "HV_PROXY", "UNAVAILABLE"] = (
            "UNAVAILABLE"
        )
        is_market_active = is_market_open()

        # A. Live IV Calculation (Preferred)
        if is_market_active:
            ticker = yf.Ticker(symbol)
            try:
                info = await asyncio.to_thread(lambda: ticker.info)
                current_iv = info.get("impliedVolatility")
                if current_iv and current_iv > 0:
                    iv_source = "LIVE_IV"
            except Exception as e:
                logger.warning(f"[{symbol}] yfinance ticker.info 獲取異常: {e}")

            if not current_iv or current_iv <= 0:
                try:
                    expirations = await market_data_service.get_all_option_expiries(
                        symbol
                    )
                    if expirations:
                        chain = await market_data_service.get_option_chain(
                            symbol, expirations[0]
                        )
                        if chain:
                            all_options = []
                            for df in [chain.calls, chain.puts]:
                                if df is not None and not df.empty:
                                    for _, row in df.iterrows():
                                        iv_val = float(
                                            row.get("impliedVolatility", 0.0)
                                        )
                                        strike_val = float(row.get("strike", 0.0))
                                        oi = float(row.get("openInterest", 0.0))
                                        vol = float(row.get("volume", 0.0))
                                        if iv_val > 0.01 and strike_val > 0.0:
                                            distance_pct = (
                                                abs(strike_val - spot_price)
                                                / spot_price
                                            )
                                            if distance_pct <= 0.20:
                                                weight = (oi + vol + 1.0) / (
                                                    distance_pct * 100.0 + 1.0
                                                )
                                                all_options.append((iv_val, weight))
                            if all_options:
                                total_weight = sum(w for _, w in all_options)
                                current_iv = (
                                    sum(iv * w for iv, w in all_options) / total_weight
                                )
                                iv_source = "LIVE_IV"
                except Exception as opt_err:
                    logger.warning(
                        f"[{symbol}] VIX-style weighted IV calculation failed: {opt_err}"
                    )

        # B. Fallback path
        if not current_iv or math.isnan(current_iv) or current_iv <= 0:
            last_db_iv = get_last_stored_iv(symbol)
            if last_db_iv and not math.isnan(last_db_iv) and last_db_iv > 0:
                current_iv = last_db_iv
                iv_source = "STORED_IV"
            else:
                df_temp = await market_data_service.get_history_df(symbol, period="1mo")
                if not df_temp.empty and len(df_temp) >= 20:
                    df_temp["Log_Ret"] = np.log(
                        df_temp["Close"] / df_temp["Close"].shift(1)
                    )
                    current_iv = float(df_temp["Log_Ret"].std() * np.sqrt(252))
                    iv_source = "HV_PROXY"

        if not current_iv or math.isnan(current_iv) or current_iv <= 0:
            raise ValueError(f"無法獲取 {symbol} 的 IV，且歷史波動率數據不足")

        # 3. 儲存至 database historical_iv (儲存原始 IV，防範閉市期間重複乘算與歷史數據污染)
        today_str = datetime.now().strftime("%Y-%m-%d")
        await save_historical_iv(symbol, current_iv, today_str)

        has_earnings_event = False
        has_macro_event = False

        # Apply Event Loading Factor (1.4x) if fallback used and event near
        if iv_source in ["STORED_IV", "HV_PROXY"]:
            try:
                from database.calendar_cache import (
                    get_cached_earnings,
                    get_macro_events_between,
                )

                today_dt = datetime.now().date()

                earnings = get_cached_earnings(symbol)
                if earnings and earnings.get("earnings_date"):
                    try:
                        earn_date = datetime.strptime(
                            earnings["earnings_date"][:10], "%Y-%m-%d"
                        ).date()
                        if today_dt <= earn_date <= today_dt + timedelta(days=14):
                            has_earnings_event = True
                    except Exception:
                        pass

                start_date_str = today_dt.strftime("%Y-%m-%d")
                end_date_str = (today_dt + timedelta(days=14)).strftime("%Y-%m-%d")
                macro_events = get_macro_events_between(start_date_str, end_date_str)
                for evt in macro_events:
                    event_name = evt.get("event", "").upper()
                    if evt.get("impact", "").upper() == "HIGH" or any(
                        term in event_name
                        for term in [
                            "FOMC",
                            "INTEREST RATE",
                            "CPI",
                            "NFP",
                            "FED DECISION",
                        ]
                    ):
                        has_macro_event = True
                        break
            except Exception:
                pass

            if has_earnings_event or has_macro_event:
                orig = current_iv
                current_iv = current_iv * 1.4
                logger.warning(
                    f"[{symbol}] Real-time IV missing. Applied 1.4x Event Loading Factor to {iv_source}: {orig:.4f} -> {current_iv:.4f}"
                )

        # 4. 取得 DB 歷史 IV
        db_ivs = {}
        try:
            from database.connection import get_read_connection

            conn = get_read_connection()
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

        # 5. 取得 1y K-line history 做 HV 代理
        df_hist = await market_data_service.get_history_df(symbol, period="1y")
        history_map = {}
        if not df_hist.empty:
            df_hist["Log_Ret"] = np.log(df_hist["Close"] / df_hist["Close"].shift(1))
            df_hist["HV_20"] = df_hist["Log_Ret"].rolling(window=20).std() * np.sqrt(
                252
            )
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
        if iv_rank > 70.0 and current_iv < 0.05:
            raise ValueError(
                f"Conflict detected: IV Rank is high ({iv_rank:.1f}%) but Implied Volatility is suspiciously low ({current_iv * 100:.1f}%)."
            )

        # 10. 計算 Expected Move Weekly
        em_from_iv = (
            spot_price * current_iv * math.sqrt(7.0 / 365.0)
            if current_iv > 0.001
            else 0.0
        )

        straddle_em, (term_status, term_ratio) = await asyncio.gather(
            _calculate_straddle_implied_em(symbol, spot_price),
            _calculate_iv_term_structure(symbol, spot_price),
        )

        if straddle_em and straddle_em > 0:
            expected_move_weekly = straddle_em
            if em_from_iv > 0:
                expected_move_weekly = max(straddle_em, em_from_iv)
        elif em_from_iv > 0:
            expected_move_weekly = em_from_iv
        else:
            hv_proxy = 0.0
            if not df_hist.empty and "HV_20" in df_hist.columns:
                last_hv = df_hist["HV_20"].dropna()
                if not last_hv.empty:
                    hv_proxy = float(last_hv.iloc[-1])
            expected_move_weekly = (
                spot_price * max(hv_proxy, 0.15) * math.sqrt(7.0 / 365.0)
            )

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
            has_earnings_event=has_earnings_event,
            has_macro_event=has_macro_event,
            iv_term_structure_status=term_status,
            term_structure_ratio=term_ratio,
        )

        # 12. 寫入快取
        _iv_cache[symbol] = (metrics, current_time + _IV_CACHE_TTL)
        try:
            await save_kv_cache(cache_key, metrics.model_dump())
        except Exception as e:
            logger.warning(f"[{symbol}] Failed to save IVMetrics to kv_cache: {e}")
        return metrics

    except ValueError as ve:
        logger.warning(f"[{symbol}] IV 指標計算退級: {ve}")
        return IVMetrics(
            symbol=symbol,
            current_iv=None,
            iv_rank=None,
            iv_percentile=None,
            expected_move_weekly=None,
            iv_status="Normal",
            is_premarket=True,
            iv_source="UNAVAILABLE",
            reference_spot_price=spot_price,
            has_earnings_event=False,
            has_macro_event=False,
            iv_term_structure_status=None,
            term_structure_ratio=None,
        )
    except Exception as e:
        logger.error(f"[{symbol}] IV 指標計算失敗: {e}")
        return IVMetrics(
            symbol=symbol,
            current_iv=None,
            iv_rank=None,
            iv_percentile=None,
            expected_move_weekly=None,
            iv_status="Normal",
            is_premarket=True,
            iv_source="UNAVAILABLE",
            reference_spot_price=spot_price,
            has_earnings_event=False,
            has_macro_event=False,
            iv_term_structure_status=None,
            term_structure_ratio=None,
        )
