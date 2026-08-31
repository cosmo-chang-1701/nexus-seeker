"""單一標的雷達數據抓取（快取縫合的 Fast Track / 即時計算的 Slow Track）。"""

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SWR_REVALIDATE_SEM = asyncio.Semaphore(3)
_active_swr_tasks: set[str] = set()


class RadarDataMixin:
    async def _async_revalidate_market_cache(self, sym: str, price: float) -> Any:
        try:
            from market_analysis.sentiment_engine import SentimentEngine

            logger.info(f"🔄 [SWR] Background revalidating market cache for {sym}...")
            # This calls the unified method, calculates, saves to SQLite cache, and handles CB/degradation:
            await SentimentEngine.get_unified_max_pain(sym, force_refresh=True)
            logger.info(f"✅ [SWR] Background revalidation complete for {sym}")
        except Exception as e:
            logger.error(f"❌ [SWR] Background revalidation failed for {sym}: {e}")

    async def _fetch_sym_radar_data_fast(self, sym: str) -> Any:
        from services.single_flight import SingleFlightManager

        return await SingleFlightManager.run(
            f"analyze_{sym}_fast", self._fetch_sym_radar_data_fast_raw, sym
        )

    async def _fetch_sym_radar_data_slow(self, sym: str) -> Any:
        from services.single_flight import SingleFlightManager

        return await SingleFlightManager.run(
            f"analyze_{sym}_slow", self._fetch_sym_radar_data_slow_raw, sym
        )

    async def _fetch_sym_radar_data_fast_raw(self, sym: str) -> Any:
        """
        Fast Track 讀取：抓取即時報價並從多個 SQLite 快取 (radar, market, kv, squeeze) 縫合數據。
        """
        import time
        from services import market_data_service
        from services.market_data_service import _EDGE_SNAPSHOT_MAX_AGE_SECONDS
        from database.market_cache import get_market_cache
        from database.squeeze_cache import get_squeeze_cache
        from database.cache import get_kv_cache, get_kv_cache_with_age
        from datetime import datetime

        quote = await market_data_service.get_quote(sym)
        price = quote.get("c", 0.0) if quote else 0.0
        current_volume = quote.get("volume", 0) if quote else 0

        radar_cache = get_kv_cache(f"radar_terminal_{sym.upper()}") or {}
        market_cache = get_market_cache(sym) or {}
        squeeze_cache = get_squeeze_cache(sym)
        gex_cached = get_kv_cache(f"gex_metrics_{sym.upper()}") or {}
        gex_data = gex_cached.get("data", {}) if isinstance(gex_cached, dict) else {}
        # Fast path 讀取 gex_metrics_{sym} 為未經 fetch_symbol_gex_metrics() 的原始
        # kv_cache 讀取，繞過了該函式內建的新鮮度檢查；這裡直接利用信封本身已內含的
        # timestamp（寫入端見 index_microstructure.py）自行判斷是否過期，門檻沿用
        # 與該函式相同的 _EDGE_SNAPSHOT_MAX_AGE_SECONDS，避免此層另立不同步的門檻。
        gex_is_stale = bool(gex_cached) and (
            time.time() - gex_cached.get("timestamp", 0)
            >= _EDGE_SNAPSHOT_MAX_AGE_SECONDS
        )

        uoa_data: list[Any] = []
        uoa_cached, uoa_age_seconds = get_kv_cache_with_age(f"uoa_{sym.upper()}")
        if uoa_cached is not None and isinstance(uoa_cached, list):
            uoa_data = list(uoa_cached)
        elif radar_cache.get("uoa") is not None and isinstance(
            radar_cache.get("uoa"), list
        ):
            uoa_data = list(radar_cache["uoa"])
        else:
            # UOA 快取未命中：不阻塞主流程，啟動非同步 SWR 自癒任務寫回快取（去重與節流）
            uoa_key = f"uoa_{sym.upper()}"
            if uoa_key not in _active_swr_tasks:
                _active_swr_tasks.add(uoa_key)

                async def _revalidate_uoa(s: str, k: str) -> None:
                    try:
                        async with _SWR_REVALIDATE_SEM:
                            from market_analysis.sentiment_engine import (
                                SentimentEngine,
                            )
                            from database.cache import save_kv_cache

                            uoa_res = await SentimentEngine.detect_uoa(s)
                            await save_kv_cache(
                                f"uoa_{s.upper()}", list(uoa_res) if uoa_res else []
                            )
                    except Exception as ex:
                        logger.warning(f"[{s}] Async SWR UOA 快取自癒失敗: {ex}")
                    finally:
                        _active_swr_tasks.discard(k)

                asyncio.create_task(_revalidate_uoa(sym, uoa_key))

        # Squeeze Cache 自癒檢查：若完全未命中或已過期且無歷史數值
        if (
            not squeeze_cache
            or squeeze_cache.get("is_expired", False)
            or "momentum" not in squeeze_cache
        ):
            # 若 radar_cache 中已有歷史數值，作為即時 fallback
            sqz_is_sq = bool(radar_cache.get("is_squeezing", False))
            sqz_m = float(radar_cache.get("squeeze_momentum", 0.0) or 0.0)
            sqz_d = str(radar_cache.get("squeeze_direction", "⚪") or "⚪")
            squeeze_cache = {
                "is_squeezing": sqz_is_sq,
                "momentum": sqz_m,
                "direction": sqz_d,
                "is_expired": False,
            }

            # 啟動非同步 SWR 自癒計算，不阻塞即時互動指令（去重與節流）
            sqz_key = f"sqz_{sym.upper()}"
            if sqz_key not in _active_swr_tasks:
                _active_swr_tasks.add(sqz_key)

                async def _revalidate_sqz(s: str, k: str) -> None:
                    try:
                        async with _SWR_REVALIDATE_SEM:
                            from database.squeeze_cache import save_squeeze_cache
                            from market_analysis.psq_engine import analyze_psq

                            df_hist = await market_data_service.get_history_df(
                                s, period="6mo", interval="1d"
                            )
                            if df_hist is not None and not df_hist.empty:
                                psq_obj = analyze_psq(df_hist, vix_spot=18.0)
                                if psq_obj:
                                    p_is_sq = psq_obj.is_squeezing
                                    p_m = psq_obj.momentum_value
                                    p_d = (
                                        "🟢"
                                        if psq_obj.signal_direction == "Long"
                                        else (
                                            "🔴"
                                            if psq_obj.signal_direction == "Short"
                                            else "⚪"
                                        )
                                    )
                                    save_squeeze_cache(s, p_is_sq, p_m, p_d)
                    except Exception as ex:
                        logger.warning(f"[{s}] Async SWR SQZ 快取自癒計算失敗: {ex}")
                    finally:
                        _active_swr_tasks.discard(k)

                asyncio.create_task(_revalidate_sqz(sym, sqz_key))

        if not squeeze_cache:
            squeeze_cache = {}

        darkpool_cached = get_kv_cache(f"darkpool_{sym.upper()}") or {}
        dp_poc_val, dp_poc_age_seconds = get_kv_cache_with_age(f"dp_poc_{sym.upper()}")
        if dp_poc_val is None:
            dp_poc_val = (
                darkpool_cached.get("dp_poc")
                or radar_cache.get("hvn_price")
                or get_kv_cache(f"volume_poc_{sym.upper()}")
            )
            dp_poc_age_seconds = None
        dp_poc = float(dp_poc_val) if dp_poc_val is not None else 0.0

        today_str = datetime.now().strftime("%Y-%m-%d")
        iv_metrics = get_kv_cache(f"iv_metrics_{sym.upper()}_{today_str}") or {}

        from market_analysis.sentiment.history_storage import (
            get_last_stored_sentiment,
            get_indicator_percentile,
            get_last_stored_iv,
        )

        if not iv_metrics or "iv_rank" not in iv_metrics:
            last_iv = get_last_stored_iv(sym)
            if last_iv is not None and not iv_metrics:
                iv_metrics = {
                    "current_iv": last_iv,
                    "iv_rank": radar_cache.get("iv_rank", 50.0),
                }

        if "expected_move_lower" not in iv_metrics:
            iv_metrics["expected_move_lower"] = market_cache.get(
                "expected_move_lower", 0.0
            )
        if "expected_move_upper" not in iv_metrics:
            iv_metrics["expected_move_upper"] = market_cache.get(
                "expected_move_upper", 0.0
            )
        if "term_structure_ratio" not in iv_metrics and radar_cache.get(
            "term_structure_ratio"
        ):
            iv_metrics["term_structure_ratio"] = radar_cache.get("term_structure_ratio")
        if "iv_term_structure_status" not in iv_metrics and radar_cache.get(
            "iv_term_structure_status"
        ):
            iv_metrics["iv_term_structure_status"] = radar_cache.get(
                "iv_term_structure_status"
            )

        avg_vol_20d = radar_cache.get("avg_vol_20d", 0.0)
        rvol = (current_volume / avg_vol_20d) if avg_vol_20d > 0 else 0.0

        mp_near = radar_cache.get("mp_near") or market_cache.get("max_pain")

        # 讀取真實 Skew 與分位點
        skew_val = get_last_stored_sentiment(sym, "SKEW")
        if skew_val is not None:
            skew_percentile = get_indicator_percentile(sym, "SKEW", skew_val)
        elif "skew" in radar_cache:
            skew_val = radar_cache.get("skew", 0.0)
            skew_percentile = radar_cache.get("skew_percentile", 50.0)
        else:
            skew_val = -0.5 if radar_cache.get("is_skew_extreme") else 0.0
            skew_percentile = 50.0

        # GEX Wall 解析（優先從 gex_metrics 快取讀取）
        put_wall = gex_data.get("put_wall") or radar_cache.get("put_wall_strike")
        call_wall = gex_data.get("call_wall") or radar_cache.get("call_wall_strike")
        net_gex = (
            gex_data.get("net_gex")
            if gex_data.get("net_gex") is not None
            else radar_cache.get("net_gex")
        )

        atr_14 = float(radar_cache.get("atr_14", 0.0) or 0.0)

        # SQZ 動能與方向
        sqz_mom = squeeze_cache.get(
            "momentum", radar_cache.get("squeeze_momentum", 0.0)
        )
        sqz_is_squeezing = squeeze_cache.get(
            "is_squeezing", radar_cache.get("is_squeezing", False)
        )
        sqz_dir = squeeze_cache.get(
            "direction", radar_cache.get("squeeze_direction", "⚪")
        )

        # 讀取 PCR (買賣權成交量比 / 未平倉比)
        volume_pcr = get_last_stored_sentiment(sym, "PCR")
        if volume_pcr is None:
            volume_pcr = radar_cache.get("volume_pcr")
        oi_pcr = radar_cache.get("oi_pcr")

        # 做市商正 Gamma 深度、負 Gamma 泥淖與底牆厚度
        from market_analysis.index_microstructure import (
            calculate_positive_gex_depth_below,
            find_overhead_negative_gex_swamp,
        )

        pos_gex_below = radar_cache.get("positive_gex_below")
        if pos_gex_below is None and gex_data.get("gex_profile"):
            pos_gex_below = calculate_positive_gex_depth_below(
                gex_data["gex_profile"], price
            )

        overhead_neg_swamp = radar_cache.get("overhead_neg_gex_swamp")
        if overhead_neg_swamp is None and gex_data.get("gex_profile"):
            overhead_neg_swamp = find_overhead_negative_gex_swamp(
                gex_data["gex_profile"], price
            )

        put_wall_gex = radar_cache.get("put_wall_gex")
        if put_wall_gex is None and gex_data.get("gex_profile") and put_wall:
            put_wall_gex = float(
                gex_data["gex_profile"].get(
                    str(put_wall), gex_data["gex_profile"].get(str(int(put_wall)), 0.0)
                )
            )

        # 多週期 Max Pain (month_max_pains) 與 MA20 快取縫合
        month_max_pains = (
            radar_cache.get("month_max_pains")
            or get_kv_cache(f"month_mp_{sym.upper()}")
            or get_kv_cache(f"month_max_pains_{sym.upper()}")
            or []
        )
        if not month_max_pains and (mp_near or radar_cache.get("mp_far")):
            today_date_str = datetime.now().strftime("%Y-%m-%d")
            synth_mps: list[dict[str, Any]] = []
            if mp_near:
                synth_mps.append({"expiry": today_date_str, "max_pain": float(mp_near)})
            if radar_cache.get("mp_far"):
                synth_mps.append(
                    {"expiry": "far", "max_pain": float(radar_cache["mp_far"])}
                )
            month_max_pains = synth_mps

        ma20_val = radar_cache.get("ma20") or get_kv_cache(f"ma20_{sym.upper()}")

        return {
            "symbol": sym,
            "quote": quote,
            "rvol": rvol,
            "radar_cache": radar_cache,
            "skew": skew_val,
            "skew_percentile": skew_percentile,
            "volume_pcr": volume_pcr,
            "oi_pcr": oi_pcr,
            "positive_gex_below": pos_gex_below,
            "overhead_neg_gex_swamp": overhead_neg_swamp,
            "put_wall_gex": put_wall_gex,
            "max_pain": {
                "max_pain": mp_near,
                "distance_pct": ((price - mp_near) / mp_near) * 100
                if mp_near and mp_near > 0
                else 0.0,
                "is_stale": bool(market_cache.get("is_stale", 0)),
                "calculation_mode": market_cache.get("calculation_mode", "OI"),
                "is_degraded": bool(market_cache.get("is_degraded", 0)),
                "circuit_breaker_triggered": bool(
                    market_cache.get("circuit_breaker_triggered", 0)
                ),
                "updated_at": market_cache.get("updated_at"),
            },
            "iv_metrics": iv_metrics,
            "iv_data": iv_metrics,
            "uoa": uoa_data,
            "uoa_age_seconds": uoa_age_seconds,
            "darkpool": darkpool_cached,
            "dp_poc_age_seconds": dp_poc_age_seconds,
            "atr_14": atr_14,
            "ma20": float(ma20_val) if ma20_val is not None else None,
            "month_max_pains": month_max_pains,
            "psq_result": {
                "is_squeezing": sqz_is_squeezing,
                "momentum": sqz_mom,
                "momentum_value": sqz_mom,
                "signal_direction": sqz_dir,
                "direction": sqz_dir,
            },
            "gex_metrics": {
                "put_wall": put_wall,
                "call_wall": call_wall,
                "net_gex": net_gex,
                "put_wall_gex": put_wall_gex,
                "_is_stale_cache": gex_is_stale,
            },
            "gex_profile_data": {
                "put_wall": put_wall,
                "call_wall": call_wall,
                "net_gex": net_gex,
                "gex_profile": gex_data.get("gex_profile", {}),
                "put_wall_gex": put_wall_gex,
                "positive_gex_below": pos_gex_below,
                "overhead_neg_gex_swamp": overhead_neg_swamp,
                "_is_stale_cache": gex_is_stale,
            },
            "vp_data": {
                "hvn": radar_cache.get("hvn_price")
                or get_kv_cache(f"volume_poc_{sym.upper()}"),
                "lvn": radar_cache.get("lvn_price"),
            },
            "dp_poc": dp_poc,
        }

    async def _fetch_sym_radar_data_slow_raw(self, sym: str) -> Any:
        """
        獲取單一標的的雷達量化數據。
        採用統一的 get_unified_max_pain 方法讀取與重算快取。

        本函式僅由背景排程呼叫（15 分鐘 Watchlist 心跳、08:45 ET 盤前預熱、
        15 分鐘持倉監控），並非互動指令的即時深度分析路徑，因此刻意維持預設
        的 force_live=False / force_refresh=False，繼續吃 Edge Snapshot 與
        IV/Max Pain 自身的快取——這正是這些排程任務存在的目的，避免每輪都對
        每個使用者、每個標的重複發動即時網路請求。互動指令（`/x symbol:`、
        批次掃描的「⚡ 批次分析警示標的」按鈕）走的是完全獨立的
        `_fetch_single_symbol_data_raw`，那裡才是保證即時性的地方。
        """
        from market_analysis.sentiment_engine import SentimentEngine
        from services import market_data_service

        # 1. 取得 quote (必須即時，因為是價格)
        quote = await market_data_service.get_quote(sym)
        price = quote.get("c", 0.0) if quote else 0.0

        from market_analysis.index_microstructure import (
            fetch_symbol_gex_metrics,
            calculate_positive_gex_depth_below,
            find_overhead_negative_gex_swamp,
        )

        async def _get_uoa_with_physical_caps(
            symbol: str,
        ) -> tuple[list[Any], list[dict[str, Any]]]:
            try:
                return await SentimentEngine.detect_uoa_with_physical_caps(symbol)  # type: ignore[no-any-return]
            except Exception as e:
                logger.error(f"[{symbol}] Batch Scan 獲取 UOA/物理封頂 失敗: {e}")
                return [], []

        async def _get_far_mp_and_dte(symbol: str) -> tuple[float, Optional[int]]:
            try:
                exp = await market_data_service.get_all_option_expiries(symbol)
                if not exp:
                    return 0.0, None
                from datetime import datetime

                today_dt = datetime.now().date()
                target_mp = None
                nearest_dte = None
                for e in exp:
                    try:
                        diff = (datetime.strptime(e, "%Y-%m-%d").date() - today_dt).days
                        if diff >= 0:
                            if nearest_dte is None or diff < nearest_dte:
                                nearest_dte = diff
                            if diff >= 7 and target_mp is None:
                                target_mp = e
                    except ValueError:
                        pass

                far_mp = 0.0
                if target_mp:
                    from market_analysis.sentiment.max_pain import calculate_max_pain

                    res = await calculate_max_pain(symbol, target_mp)
                    if res and res.get("max_pain"):
                        far_mp = float(res["max_pain"])
                return far_mp, nearest_dte
            except Exception:
                pass
            return 0.0, None

        # 2. 情緒 (Skew)、UOA/物理封頂、IV、Max Pain、GEX、遠月 Max Pain、PCR
        # 彼此互不依賴，一律併入同一個 gather 平行執行，避免先前 Skew/UOA
        # 序列等待再進入 gather 造成的不必要延遲疊加（純效能修正，與快取新鮮度
        # 政策無關，背景排程任務一樣受益）。
        skew_task = SentimentEngine.calculate_skew(sym)
        uoa_task = _get_uoa_with_physical_caps(sym)
        iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(sym)
        mp_task = SentimentEngine.get_unified_max_pain(sym)
        gex_task = fetch_symbol_gex_metrics(sym)
        pcr_task = SentimentEngine.calculate_pcr(sym)
        far_mp_task = _get_far_mp_and_dte(sym)

        (
            skew_data,
            (uoa_data, physical_cap_strikes),
            iv_m,
            mp_data,
            gex_data,
            (far_mp_val, nearest_dte),
            pcr_data,
        ) = await asyncio.gather(
            skew_task, uoa_task, iv_task, mp_task, gex_task, far_mp_task, pcr_task
        )

        skew_val = skew_data.get("skew", 0.0) if isinstance(skew_data, dict) else 0.0
        skew_percentile = SentimentEngine.get_indicator_percentile(
            sym, "SKEW", skew_val
        )

        volume_pcr = (
            pcr_data.get("volume_pcr")
            if isinstance(pcr_data, dict)
            else (pcr_data.get("pcr") if isinstance(pcr_data, dict) else None)
        )
        oi_pcr = pcr_data.get("oi_pcr") if isinstance(pcr_data, dict) else None
        physical_cap_above_spot = any(
            str(s.get("type", "")).upper().startswith("C")
            and float(s.get("strike", 0.0) or 0.0) > price
            for s in physical_cap_strikes
        )
        gex_prof = gex_data.get("gex_profile", {}) if isinstance(gex_data, dict) else {}
        pos_gex_below = calculate_positive_gex_depth_below(gex_prof, price)
        overhead_neg_swamp = find_overhead_negative_gex_swamp(gex_prof, price)
        pw_strike = gex_data.get("put_wall", 0.0) if isinstance(gex_data, dict) else 0.0
        pw_gex = (
            float(gex_prof.get(str(pw_strike), gex_prof.get(str(int(pw_strike)), 0.0)))
            if pw_strike
            else 0.0
        )

        # 異步預警：若返回資料標記為 stale，啟動背景重新驗證
        if mp_data.get("is_stale"):
            asyncio.create_task(self._async_revalidate_market_cache(sym, price))

        raw_em_context = await SentimentEngine.get_expected_move(
            sym, quote=quote, iv_metrics=iv_m
        )
        em_context = raw_em_context if isinstance(raw_em_context, dict) else {}

        # 取得 IV 數據
        def _safe_em_float(value: Any) -> float:
            try:
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        iv_rank_val = 0.0
        em_weekly = _safe_em_float(em_context.get("expected_move_weekly"))
        em_lower = _safe_em_float(em_context.get("expected_move_lower"))
        em_upper = _safe_em_float(em_context.get("expected_move_upper"))

        if iv_m:
            iv_rank_val = iv_m.iv_rank if iv_m.iv_rank is not None else 0.0

        mock_iv = {
            "iv_rank": iv_rank_val,
            "expected_move_weekly": em_weekly,
            "reference_price": _safe_em_float(em_context.get("reference_price")),
            "expected_move_lower": em_lower,
            "expected_move_upper": em_upper,
            "term_structure_ratio": iv_m.term_structure_ratio if iv_m else None,
            "iv_term_structure_status": iv_m.iv_term_structure_status if iv_m else None,
        }

        # 取得 DTE-ER (距離財報天數)
        dte_er = None
        try:
            from database.calendar_cache import get_cached_earnings

            earnings = get_cached_earnings(sym)
            if earnings and earnings.get("earnings_date"):
                from datetime import datetime

                earn_date_str = earnings["earnings_date"][:10]
                earn_date = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                today_dt = datetime.now().date()
                days = (earn_date - today_dt).days
                if days >= 0:
                    dte_er = days
        except Exception:
            pass

        # 取得 PSQ 與簡易 EMA 21
        from services.market_data_service import get_history_df

        df_hist = await get_history_df(sym, period="1y", interval="1d")
        psq_res = {}
        ema_21 = 0.0
        atr_14 = 0.0
        if df_hist is not None and not df_hist.empty:
            ema_21 = df_hist["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
            try:
                import pandas_ta as ta

                atr_series = ta.atr(
                    df_hist["High"], df_hist["Low"], df_hist["Close"], length=14
                )
                if atr_series is not None and not atr_series.empty:
                    atr_14 = float(atr_series.iloc[-1])
            except Exception:
                pass
            from database.squeeze_cache import get_squeeze_cache, save_squeeze_cache
            from market_analysis.psq_engine import analyze_psq

            sc = get_squeeze_cache(sym)
            if sc:
                psq_res = {
                    "is_squeezing": sc.get("is_squeezing", False),
                    "momentum_value": sc.get("momentum", 0.0),
                    "signal_direction": sc.get("direction", "⚪"),
                }
            else:
                psq_obj = analyze_psq(df_hist, vix_spot=18.0)
                if psq_obj:
                    psq_res = {
                        "is_squeezing": psq_obj.is_squeezing,
                        "momentum_value": psq_obj.momentum_value,
                        "signal_direction": "🟢"
                        if psq_obj.signal_direction == "Long"
                        else ("🔴" if psq_obj.signal_direction == "Short" else "⚪"),
                        "squeeze_level": psq_obj.squeeze_level,
                    }
                    save_squeeze_cache(
                        sym,
                        psq_res["is_squeezing"],
                        psq_res["momentum_value"],
                        psq_res["signal_direction"],
                    )

        # 讀取 DP-POC (暗池共振)
        from database.cache import get_kv_cache_with_age

        dp_poc_val, dp_poc_age_seconds = get_kv_cache_with_age(f"dp_poc_{sym.upper()}")
        dp_poc = float(dp_poc_val) if dp_poc_val is not None else 0.0

        # 重複利用 df_hist 計算 Volume Profile (HVN/LVN)
        from market_analysis.volume_profile import calculate_volume_profile_from_df

        vp_data = (
            calculate_volume_profile_from_df(df_hist, days=20, is_hourly=False)
            if df_hist is not None
            else None
        )

        # 計算 20 日均量與當前 K 棒成交量
        vol_data = {"current_volume": 0.0, "avg_volume_20": 0.0}
        if df_hist is not None and not df_hist.empty and "Volume" in df_hist.columns:
            try:
                vol_data["current_volume"] = float(df_hist["Volume"].iloc[-1])
                if len(df_hist) >= 20:
                    vol_data["avg_volume_20"] = float(df_hist["Volume"].tail(20).mean())
                else:
                    vol_data["avg_volume_20"] = float(df_hist["Volume"].mean())
            except Exception:
                pass

        result = {
            "symbol": sym,
            "quote": quote,
            "iv_metrics": mock_iv,
            "dte_er": dte_er,
            "expected_move_context": em_context,
            "skew": skew_val,
            "skew_percentile": skew_percentile,
            "volume_pcr": volume_pcr,
            "oi_pcr": oi_pcr,
            "positive_gex_below": pos_gex_below,
            "overhead_neg_gex_swamp": overhead_neg_swamp,
            "put_wall_gex": pw_gex,
            "max_pain": mp_data,
            "uoa": uoa_data,
            "uoa_age_seconds": 0.0,
            "gex_profile_data": {
                "put_wall": gex_data.get("put_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "call_wall": gex_data.get("call_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "net_gex": gex_data.get("net_gex")
                if isinstance(gex_data, dict)
                else 0.0,
                "gex_profile": gex_prof,
                "put_wall_gex": pw_gex,
                "positive_gex_below": pos_gex_below,
                "overhead_neg_gex_swamp": overhead_neg_swamp,
                "_is_stale_cache": bool(gex_data.get("_is_stale_cache", False))
                if isinstance(gex_data, dict)
                else False,
            },
            "gex_metrics": {
                "put_wall": gex_data.get("put_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "call_wall": gex_data.get("call_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "net_gex": gex_data.get("net_gex")
                if isinstance(gex_data, dict)
                else 0.0,
                "put_wall_gex": pw_gex,
                "_is_stale_cache": bool(gex_data.get("_is_stale_cache", False))
                if isinstance(gex_data, dict)
                else False,
            },
            "psq_result": psq_res,
            "dp_poc": dp_poc,
            "dp_poc_age_seconds": dp_poc_age_seconds,
            "ma20": ema_21,
            "atr_14": atr_14,
            "nearest_dte": nearest_dte,
            "vp_data": vp_data or {},
            "vol_data": vol_data,
        }

        # Save to kv_cache
        from database.cache import save_kv_cache
        from datetime import datetime, timezone

        await save_kv_cache(f"uoa_{sym.upper()}", uoa_data or [])

        await save_kv_cache(
            f"radar_terminal_{sym.upper()}",
            {
                "put_wall_strike": gex_data.get("put_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "call_wall_strike": gex_data.get("call_wall")
                if isinstance(gex_data, dict)
                else 0.0,
                "net_gex": gex_data.get("net_gex")
                if isinstance(gex_data, dict)
                else 0.0,
                "put_wall_gex": pw_gex,
                "positive_gex_below": pos_gex_below,
                "overhead_neg_gex_swamp": overhead_neg_swamp,
                "volume_pcr": volume_pcr,
                "oi_pcr": oi_pcr,
                "sto_strikes": physical_cap_strikes,
                "physical_cap_above_spot": physical_cap_above_spot,
                "mp_near": mp_data.get("max_pain")
                if isinstance(mp_data, dict)
                else 0.0,
                "mp_far": far_mp_val,
                "is_divergence": skew_percentile > 85.0
                and psq_res.get("momentum_value", 0.0) > 0,
                "is_skew_extreme": skew_percentile > 85.0 or skew_percentile < 15.0,
                "hvn_price": (vp_data or {}).get("hvn", 0.0),
                "lvn_price": (vp_data or {}).get("lvn", 0.0),
                "avg_vol_20d": vol_data.get("avg_volume_20", 0.0),
                "skew": skew_val,
                "skew_percentile": skew_percentile,
                "atr_14": atr_14,
                "iv_rank": iv_rank_val,
                "term_structure_ratio": iv_m.term_structure_ratio if iv_m else None,
                "iv_term_structure_status": iv_m.iv_term_structure_status
                if iv_m
                else None,
                "squeeze_momentum": psq_res.get("momentum_value", 0.0),
                "is_squeezing": psq_res.get("is_squeezing", False),
                "squeeze_direction": psq_res.get("signal_direction", "⚪"),
                "uoa": uoa_data,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        return result
