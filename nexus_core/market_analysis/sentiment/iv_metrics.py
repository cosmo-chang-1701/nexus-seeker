from typing import Any
from .history_storage import get_last_stored_iv, save_historical_iv
import logging
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
    def _safe_float(value: Any) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def resolve_reference_price(
        cls, quote: dict | None = None, iv_metrics: Any = None
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
    async def get_expected_move(  # type: ignore
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
    symbol: str, spot_price: float, force_live: bool = False
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

        chain = await market_data_service.get_option_chain(
            symbol, target_expiry, force_live=force_live
        )
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
        def _mid(row: Any):  # type: ignore
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

        # Expected Move 雙軌維度校正 (ISS-11 & 附錄 A.5)：
        # 對齊標準 1σ 預期移動（68.3% 覆蓋率）：
        # EM_1σ = sqrt(pi / 2) * Straddle ≈ 1.2533 * Straddle
        # 依據時間平方根法則平移至週度 (7 天)：
        # em = Straddle * 1.2533 * sqrt(7.0 / max(1.0, float(target_dte)))
        scale_1sigma = math.sqrt(math.pi / 2.0)
        em = (
            straddle_price * scale_1sigma * math.sqrt(7.0 / max(1.0, float(target_dte)))
        )

        logger.info(
            f"[{symbol}] Straddle-Implied EM (1-sigma 7D): Call_mid=${call_mid:.2f} + "
            f"Put_mid=${put_mid:.2f} = Straddle ${straddle_price:.2f} (DTE: {target_dte}) -> Weekly EM ±${em:.2f}"
        )
        return em  # type: ignore

    except Exception as e:
        logger.warning(f"[{symbol}] Straddle-Implied EM calculation failed: {e}")
        return None


async def _calculate_iv_term_structure(
    symbol: str, spot_price: float, force_live: bool = False
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
                # 期限結構取樣約束 (ISS-09)：近月強制要求 5 <= DTE <= 20，過濾 0-DTE/1-DTE 微觀噪聲與假倒掛；遠月 21 <= DTE <= 60
                if 5 <= days <= 20 and not near_expiry:
                    near_expiry = exp
                elif 21 <= days <= 60 and not far_expiry:
                    far_expiry = exp
            except ValueError:
                continue

        if not near_expiry or not far_expiry:
            return None, None

        near_chain, far_chain = await asyncio.gather(
            market_data_service.get_option_chain(
                symbol, near_expiry, force_live=force_live
            ),
            market_data_service.get_option_chain(
                symbol, far_expiry, force_live=force_live
            ),
        )

        def _get_atm_iv(chain: Any) -> float | None:
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


async def fetch_and_calculate_iv_metrics(
    symbol: str,
    force_refresh: bool = False,
    min_history_records: int = 60,
) -> IVMetrics:
    """
    獲取並計算隱含波動率 (IV) 相關指標，包括 IV Rank, IV Percentile, 週預期震盪區間。
    具備 15 分鐘快取（900 秒）與資料庫持久化儲存。

    force_refresh=True 時完全略過記憶體快取與 SQLite kv_cache 讀取，並要求所有
    下游期權鏈抓取略過 Edge Snapshot 分層，保證回傳即時資料。僅供已透過 Discord
    defer（不受 3 秒互動逾時限制）的深度分析路徑使用。
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
    if not force_refresh and symbol in _iv_cache:
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
                        return cached_val  # type: ignore
                    else:
                        logger.warning(
                            f"[{symbol}] Spot price shifted from {ref_price} to {spot_price} "
                            f"(dev={deviation:.2%}), invalidating memory cache"
                        )
                else:
                    return cached_val  # type: ignore

    # Check SQLite kv_cache next for same-day warm cache
    from database.cache import get_kv_cache, save_kv_cache
    from datetime import datetime

    today_str = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"iv_metrics_{symbol}_{today_str}"
    cached = None if force_refresh else get_kv_cache(cache_key)
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
                _iv_cache[symbol] = (metrics, current_time + _IV_CACHE_TTL)
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
                info = await market_data_service.call_yf(lambda: ticker.info)
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
                            symbol, expirations[0], force_live=force_refresh
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
        event_loading_applied = False

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

        # Apply Event Loading Factor (1.4x) if fallback used and event near
        if iv_source in ["STORED_IV", "HV_PROXY"]:
            if has_earnings_event or has_macro_event:
                orig = current_iv
                current_iv = current_iv * 1.4
                # 這是刻意的事件風險補償（快取/HV 代理值無法反映即將到來的事件
                # 定價），但放大後的值會一路流入 IV Rank、Expected Move 與所有
                # IVR 閘門，因此必須讓呈現層有辦法據實揭露，而不是讓使用者以為
                # 看到的是原始觀測值。
                event_loading_applied = True
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

        # 5. 取得 1y K-line history 做 HV 代理 (僅供 Expected Move fallback 使用，嚴禁混入 IV Rank)
        df_hist = await market_data_service.get_history_df(symbol, period="1y")
        if not df_hist.empty:
            df_hist["Log_Ret"] = np.log(df_hist["Close"] / df_hist["Close"].shift(1))
            df_hist["HV_20"] = df_hist["Log_Ret"].rolling(window=20).std() * np.sqrt(
                252
            )

        # 6. 計算純 IV 基準視窗 (ISS-06)
        # 嚴格隔離 HV 與 IV：禁止將已實現歷史波動率 (HV) 混入隱含波動率 (IV) 窗口中。
        # 由於 VRP (Variance Risk Premium) 恆正，HV 常態顯著小於 IV，混入會使 low_iv 虛低，
        # 人為放大 IV Rank 達 20%~40%。純以 DB 歷史 IV 計算，若歷史樣本不足 60 天則標註為 None。
        history_map = dict(db_ivs)
        history_map[today_str] = current_iv
        pure_iv_values = list(history_map.values())

        iv_rank: float | None = None
        iv_percentile: float | None = None

        if len(pure_iv_values) >= min_history_records:
            low_iv = min(pure_iv_values)
            high_iv = max(pure_iv_values)
            if high_iv > low_iv:
                iv_rank = ((current_iv - low_iv) / (high_iv - low_iv)) * 100.0
            else:
                iv_rank = 50.0

            lower_count = sum(1 for iv in pure_iv_values if iv < current_iv)
            iv_percentile = (lower_count / len(pure_iv_values)) * 100.0
            if iv_rank is not None:
                iv_rank = max(0.0, min(100.0, iv_rank))
            if iv_percentile is not None:
                iv_percentile = max(0.0, min(100.0, iv_percentile))
        else:
            logger.info(
                f"[{symbol}] 歷史 IV 樣本不足 {min_history_records} 天 (當前 {len(pure_iv_values)} 筆)，IV Rank/Percentile 處於數據積累期標註為 None。"
            )

        # Rule 4: If IV_Rank > 70%, current_iv cannot physically scale down to near-zero levels (<1.0%).
        # [ISS-14]: 放寬超低波標的 (如短債 ETF BIL、SHY) IV 衝突門檻至 1.0% (0.01)，防範超低波正常定價被誤殺。
        if iv_rank is not None and iv_rank > 70.0 and current_iv < 0.01:
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
            _calculate_straddle_implied_em(
                symbol, spot_price, force_live=force_refresh
            ),
            _calculate_iv_term_structure(symbol, spot_price, force_live=force_refresh),
        )

        if straddle_em and straddle_em > 0:
            # 優先採用真實期權市場定價之 Straddle 預期波動 (ISS-11)
            expected_move_weekly = straddle_em
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
        iv_status: Literal["Low", "Normal", "High", "Extreme"] | None
        if iv_rank is None:
            iv_status = "Normal"
        elif iv_rank < 30.0:
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
            event_loading_applied=event_loading_applied,
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
        # 用真實市場狀態判斷 is_premarket，而非無條件寫死 True：否則盤中一次暫時性
        # 抓取失敗（網路抖動/DB 讀取錯誤）也會被誤標成「盤前」徽章文案。
        return IVMetrics(
            symbol=symbol,
            current_iv=None,
            iv_rank=None,
            iv_percentile=None,
            expected_move_weekly=None,
            iv_status="Normal",
            is_premarket=not is_market_open(),
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
            is_premarket=not is_market_open(),
            iv_source="UNAVAILABLE",
            reference_spot_price=spot_price,
            has_earnings_event=False,
            has_macro_event=False,
            iv_term_structure_status=None,
            term_structure_ratio=None,
        )
