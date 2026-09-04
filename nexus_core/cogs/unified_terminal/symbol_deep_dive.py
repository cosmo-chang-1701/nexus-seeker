"""單一標的深度分析（/x symbol: 互動指令與批次分析警示標的共用資料來源）。"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, List, Optional

import discord

from services import market_data_service, reddit_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
from market_analysis.atr_utils import fetch_atr_15m
from market_analysis.vwap_utils import fetch_session_vwap
from market_analysis.price_volume_alert import get_confirmed_15m_bar
import market_math

from cogs.embed_builder import create_error_embed, create_tactical_symbol_embed
from .symbol_view import SymbolHubView

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class SymbolDeepDiveMixin:
    if TYPE_CHECKING:
        bot: Any

    async def _fetch_single_symbol_data_raw(self, symbol: str) -> dict:
        """
        獲取單一標的所需的所有重型量化數據與外部情緒分析。
        供 SingleFlightManager 調度使用。

        這是 `/x symbol:` 互動指令、批次掃描「⚡ 批次分析警示標的」按鈕，以及
        SymbolHubView 分頁切換共用的唯一深度分析資料來源，呼叫端一律已透過
        Discord `interaction.response.defer()` 取得最長 15 分鐘的 followup
        視窗，不再受 3 秒互動逾時限制。因此期權鏈/GEX/IV/Max Pain/Skew/PCR/UOA
        等量化數據一律以 force_live=True 或等效的 force_refresh=True 抓取，
        略過 Edge Snapshot（最舊可能 30 分鐘）與各自的記憶體/SQLite 快取層，
        保證回傳即時資料。現價/SPY 歷史/總經/Reddit/Polymarket/暗池/基本面
        論點等非期權數據維持既有快取策略不變。
        """
        from market_analysis.ddp_inspector import DDPInspector
        from market_time import ny_tz
        from datetime import datetime
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics
        from market_analysis.volume_profile import calculate_volume_profile

        ddp_inspector = DDPInspector(self.bot)
        poly_service = getattr(self.bot, "polymarket_service", None)

        async def _safe_get_poly_markets() -> list:
            if poly_service:
                return await poly_service.get_market_snapshot(limit=0)  # type: ignore
            return []

        # 1. 啟動基礎行情與總經數據任務 (t=0 並行)
        expiries_task = asyncio.create_task(
            market_data_service.get_all_option_expiries(symbol)
        )
        spy_task = asyncio.create_task(market_data_service.get_spy_history_df("1y"))
        macro_task = asyncio.create_task(market_data_service.get_macro_environment())
        quote_task = asyncio.create_task(market_data_service.get_quote(symbol))
        df_hist_task = asyncio.create_task(
            market_data_service.get_history_df(symbol, period="1y", interval="1d")
        )
        gex_profile_task = asyncio.create_task(
            fetch_symbol_gex_metrics(symbol, force_live=True)
        )
        vp_task = asyncio.create_task(
            asyncio.to_thread(calculate_volume_profile, symbol)
        )
        atr_15m_task = asyncio.create_task(fetch_atr_15m(symbol))
        vwap_task = asyncio.create_task(fetch_session_vwap(symbol))
        bar_15m_task = asyncio.create_task(get_confirmed_15m_bar(symbol))
        from services.calendar_service import calendar_service

        catalysts_task = asyncio.create_task(
            calendar_service.get_symbol_catalysts(symbol, days=14)
        )
        reddit_task = asyncio.create_task(reddit_service.get_reddit_details(symbol))
        poly_task = asyncio.create_task(_safe_get_poly_markets())
        ddp_task = asyncio.create_task(ddp_inspector.inspect_symbol(symbol))

        # 2. 啟動期權結構分析任務 (SingleFlight 自動合併相同到期日)
        # 深度分析路徑：一律 force_live/force_refresh=True，保證即時性。
        skew_task = asyncio.create_task(
            SentimentEngine.calculate_skew(symbol, force_live=True)
        )
        pcr_task = asyncio.create_task(
            SentimentEngine.calculate_pcr(symbol, force_live=True)
        )
        uoa_task = asyncio.create_task(
            SentimentEngine.detect_uoa(symbol, force_live=True)
        )
        mp_task = asyncio.create_task(
            SentimentEngine.calculate_max_pain(symbol, _retry=True)
        )
        iv_task = asyncio.create_task(
            SentimentEngine.fetch_and_calculate_iv_metrics(symbol, force_refresh=True)
        )

        # 3. 取得 30 天內到期日之 Max Pain
        async def _fetch_month_max_pains() -> list[dict[str, Any]]:
            try:
                expiries = await expiries_task
            except Exception as e:
                logger.warning(f"[{symbol}] Failed to fetch expiries: {e}")
                return []

            if not expiries:
                return []

            today = datetime.now(ny_tz).date()
            valid_expiries: list[str] = []
            for exp in expiries:
                try:
                    exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                    if 0 <= (exp_dt - today).days <= 30:
                        valid_expiries.append(exp)
                except ValueError:
                    continue

            if not valid_expiries:
                return []

            mp_tasks = [
                SentimentEngine.get_unified_max_pain(
                    symbol, expiry=exp, force_refresh=True
                )
                for exp in valid_expiries
            ]
            mp_results = await asyncio.gather(*mp_tasks, return_exceptions=True)

            month_max_pains: list[dict[str, Any]] = []
            for exp, res in zip(valid_expiries, mp_results):
                if isinstance(res, dict) and "error" not in res:
                    month_max_pains.append(
                        {
                            "expiry": exp,
                            "max_pain": res.get("max_pain"),
                            "distance_pct": res.get("distance_pct", 0.0),
                            "is_degraded": bool(res.get("is_degraded", 0)),
                            "calculation_mode": res.get("calculation_mode", "OI"),
                        }
                    )
            return month_max_pains

        month_mp_task = asyncio.create_task(_fetch_month_max_pains())

        # 4. 全量 Gather
        (
            df_spy,
            macro_raw,
            quote,
            df_hist_1d,
            gex_profile_data,
            vp_data,
            atr_15m_data,
            session_vwap_data,
            bar_15m_data,
            catalysts,
            reddit_details,
            poly_markets,
            ddp_report,
            skew_data,
            pcr_data,
            uoa_data,
            max_pain_data,
            iv_metrics,
            month_max_pains,
        ) = await asyncio.gather(
            spy_task,
            macro_task,
            quote_task,
            df_hist_task,
            gex_profile_task,
            vp_task,
            atr_15m_task,
            vwap_task,
            bar_15m_task,
            catalysts_task,
            reddit_task,
            poly_task,
            ddp_task,
            skew_task,
            pcr_task,
            uoa_task,
            mp_task,
            iv_task,
            month_mp_task,
        )

        safe_reddit_text = (
            reddit_details[0] if isinstance(reddit_details, tuple) else reddit_details
        )
        safe_reddit_posts = (
            reddit_details[1] if isinstance(reddit_details, tuple) else []
        )

        return {
            "df_spy": df_spy,
            "macro_raw": macro_raw,
            "quote": quote,
            "skew_data": skew_data,
            "pcr_data": pcr_data,
            "uoa_data": uoa_data,
            "max_pain_data": max_pain_data,
            "iv_metrics": iv_metrics,
            "reddit_text": safe_reddit_text,
            "reddit_posts": safe_reddit_posts,
            "poly_markets": poly_markets,
            "ddp_report": ddp_report,
            "df_hist_1d": df_hist_1d,
            "month_max_pains": month_max_pains,
            "gex_profile_data": gex_profile_data,
            "volume_profile": vp_data,
            "atr_15m": atr_15m_data,
            "session_vwap": session_vwap_data,
            "bar_15m": bar_15m_data,
            "catalysts": catalysts,
        }

    async def _process_symbol_hub_data(
        self, symbol: str, user_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """將原始抓取的量化與社群數據轉換為標的深度分析 (Tactical Deep-Dive) 統一資料模型。"""
        from services.asset_manager import AssetManager
        from models.asset import ContextType
        from market_analysis.risk_engine import optimize_position_risk
        from cogs.unified_terminal.utils import (
            find_matching_polymarket_odds,
            calculate_polymarket_weighted_odds,
        )
        import database

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.HOLDING)
        stock_cost_raw = next(
            (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
            0.0,
        )
        stock_cost = _safe_float(stock_cost_raw, 0.0)

        df_spy = data["df_spy"]
        macro_raw = data["macro_raw"]
        quote = data["quote"]
        skew_data = data["skew_data"]
        pcr_data = data["pcr_data"]
        uoa_data = data["uoa_data"]
        max_pain_data = data["max_pain_data"]
        iv_metrics = data["iv_metrics"]
        reddit_text = data["reddit_text"]
        poly_markets = data["poly_markets"]
        ddp_report = data["ddp_report"]
        df_hist_1d = data["df_hist_1d"]
        gex_profile_data = data.get("gex_profile_data")
        vp_data = data.get("volume_profile")
        catalysts = data.get("catalysts", [])

        spy_price = _safe_float(
            (df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0),
            670.0,
        )
        safe_macro = macro_raw or {}
        macro_data = MacroContext(
            vix=_safe_float(safe_macro.get("vix"), 18.0),
            oil_price=_safe_float(safe_macro.get("oil"), 75.0),
            vix_change=_safe_float(safe_macro.get("vix_change"), 0.0),
        )

        # 並行執行技術指標分析與 Polymarket 機率解析
        math_task = market_math.analyze_symbol(
            symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
        )
        poly_task = find_matching_polymarket_odds(symbol, poly_markets, bot=self.bot)
        poly_summary_task = calculate_polymarket_weighted_odds(
            symbol, poly_markets, bot=self.bot
        )

        result_math, poly_odds, poly_summary = await asyncio.gather(
            math_task, poly_task, poly_summary_task
        )
        result: dict[str, Any] = (
            result_math
            if isinstance(result_math, dict) and result_math
            else {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}
        )

        psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
        if psq_result:
            result["psq_result"] = psq_result
            is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
            result["price"] = (
                _safe_float(df_hist_1d["Close"].iloc[-1], 0.0)
                if is_df_valid
                else _safe_float(result.get("price"), 0.0)
            )

        result["quote"] = quote

        safe_skew = skew_data if isinstance(skew_data, dict) else {}
        result["skew"] = _safe_float(safe_skew.get("skew"), 0.0)
        result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
            symbol, "SKEW", result["skew"]
        )

        result["pcr"] = pcr_data if pcr_data is not None else {}
        result["uoa"] = uoa_data if uoa_data is not None else []

        result["iv_data"] = iv_metrics
        iv_rank_raw = (
            iv_metrics.get("iv_rank")
            if isinstance(iv_metrics, dict)
            else getattr(iv_metrics, "iv_rank", None)
        )
        result["iv_rank"] = _safe_float(iv_rank_raw, 0.0)
        raw_em_context = await SentimentEngine.get_expected_move(
            symbol, quote=quote, iv_metrics=iv_metrics
        )
        result["expected_move_context"] = (
            raw_em_context if isinstance(raw_em_context, dict) else {}
        )

        safe_mp = max_pain_data if isinstance(max_pain_data, dict) else {}
        result["max_pain"] = _safe_float(safe_mp.get("max_pain"), 0.0)
        result["month_max_pains"] = data.get("month_max_pains", [])
        result["gex_profile_data"] = gex_profile_data
        result["catalysts"] = catalysts

        safe_ddp = ddp_report if isinstance(ddp_report, dict) else {}
        result["is_ddp"] = bool(safe_ddp.get("is_ddp", False))
        result["vix"] = macro_data.vix
        result["spy_price"] = spy_price

        # Reddit sentiment score
        safe_reddit_text = reddit_text or ""
        if any(err in safe_reddit_text for err in ["錯誤", "異常", "超時", "尚未配置"]):
            result["reddit_sentiment_score"] = "⚠️ 抓取失敗 (邊緣節點異常)"
        elif "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
            result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
        elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
            result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
        else:
            result["reddit_sentiment_score"] = "⚖️ 中性"

        result["reddit_posts"] = data.get("reddit_posts", [])
        result["polymarket_odds"] = poly_odds
        result["polymarket_summary"] = poly_summary

        safe_vp = vp_data if isinstance(vp_data, dict) else {}
        result["volume_profile"] = safe_vp
        result["atr_15m"] = _safe_float(data.get("atr_15m"), 0.0)
        result["session_vwap"] = _safe_float(data.get("session_vwap"), 0.0)

        bar_15m = data.get("bar_15m")
        result["bar_15m"] = bar_15m
        if bar_15m is not None:

            def _extract_val(k: str) -> Any:
                if hasattr(bar_15m, k):
                    return getattr(bar_15m, k, None)
                if isinstance(bar_15m, dict):
                    return bar_15m.get(k)
                return None

            import math

            def _clean_float(v: Any) -> Optional[float]:
                if v is None:
                    return None
                try:
                    f = float(v)
                    return None if math.isnan(f) else f
                except (TypeError, ValueError):
                    return None

            c_15m = _clean_float(_extract_val("close"))
            o_15m = _clean_float(_extract_val("open"))
            h_15m = _clean_float(_extract_val("high"))
            l_15m = _clean_float(_extract_val("low"))
            v_15m = _clean_float(_extract_val("volume"))
            sma_15m = _clean_float(
                _extract_val("avg_volume")
                if _extract_val("avg_volume") is not None
                else _extract_val("volume_15m_sma20")
            )
            rvol = (
                (v_15m / sma_15m)
                if (v_15m is not None and sma_15m is not None and sma_15m > 0)
                else None
            )

            result["open_15m"] = o_15m
            result["high_15m"] = h_15m
            result["low_15m"] = l_15m
            result["close_15m"] = c_15m
            result["volume_15m"] = v_15m
            result["volume_15m_sma20"] = sma_15m
            result["rvol_15m"] = rvol

        # TDP 估值三擊判斷: 現價 < EMA 21 且 現價 < Max Pain 且 現價 < V-POC
        ema_21 = (
            df_hist_1d["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
            if df_hist_1d is not None and not df_hist_1d.empty
            else 0.0
        )
        vpoc = _safe_float(safe_vp.get("hvn"), 0.0)
        max_pain = _safe_float(result.get("max_pain"), 0.0)
        price = _safe_float(result.get("price"), 0.0)

        if result.get("is_ddp"):
            if price > 0 and ema_21 > 0 and max_pain > 0 and vpoc > 0:
                if price < ema_21 and price < max_pain and price < vpoc:
                    result["is_ddp"] = True
                    result["tdp_activated"] = True

                    psq_res = result.get("psq_result", {})
                    is_sqz = (
                        psq_res.get("is_squeezing", False)
                        if isinstance(psq_res, dict)
                        else getattr(psq_res, "is_squeezing", False)
                    )
                    if is_sqz:
                        result["tdpq_activated"] = True

        try:
            ctx = database.get_full_user_context(user_id)
            user_capital = _safe_float(getattr(ctx, "capital", 100000.0), 100000.0)
            risk_limit = _safe_float(getattr(ctx, "risk_limit", 0.05), 0.05)
            raw_stock_iv = (
                iv_metrics.get("current_iv")
                if isinstance(iv_metrics, dict)
                else getattr(iv_metrics, "current_iv", None)
            )
            stock_iv_val = _safe_float(raw_stock_iv, 0.0)
            stock_iv = stock_iv_val if stock_iv_val > 0 else 0.40
            vol_pcr = (
                _safe_float(pcr_data.get("volume_pcr"), 0.8)
                if isinstance(pcr_data, dict)
                else 0.8
            )
            skew_val = _safe_float(safe_skew.get("skew"), 0.0)

            opt_result = optimize_position_risk(
                current_delta=0.0,
                unit_weighted_delta=0.16,
                user_capital=user_capital,
                spy_price=spy_price,
                stock_iv=stock_iv,
                strategy="STO",
                macro_data=macro_data,
                risk_limit=risk_limit,
                vix_spot=macro_data.vix,
                pcr=vol_pcr,
                skew=skew_val,
            )
            result["kelly_sizing"] = opt_result
        except Exception as e:
            logger.warning(f"[{symbol}] Kelly sizing calculation skipped: {e}")

        return result

    @market_data_service.interactive
    async def _run_single_symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: str,
        user_id: int,
        embeds_accumulator: Optional[List[discord.Embed]] = None,
    ) -> Any:
        symbol = symbol.upper()
        if not await market_data_service.validate_symbol(symbol):
            error_emb = create_error_embed(
                f"無效的標的代號: `{symbol}`", title="輸入錯誤"
            )
            if embeds_accumulator is not None:
                embeds_accumulator.append(error_emb)
                return
            else:
                return await interaction.followup.send(
                    embed=error_emb,
                    ephemeral=True,
                )

        try:
            # 🚀 Task 2 Hook: Coalesced fetch using SingleFlightManager
            from services.single_flight import SingleFlightManager

            data = await SingleFlightManager.run(
                f"single_hub_{symbol}",
                self._fetch_single_symbol_data_raw,
                symbol,
            )

            result = await self._process_symbol_hub_data(symbol, user_id, data)

            main_embed = create_tactical_symbol_embed(result)

            view = SymbolHubView(symbol, user_id, self.bot)
            view.base_data = result

            if embeds_accumulator is not None:
                embeds_accumulator.append(main_embed)
            else:
                await interaction.followup.send(
                    embed=main_embed, view=view, ephemeral=True
                )

        except Exception as e:
            logger.exception(f"Symbol Hub Error for {symbol}: {e}")
            error_emb = create_error_embed(f"載入 `{symbol}` 資料時發生錯誤: {e}")
            if embeds_accumulator is not None:
                embeds_accumulator.append(error_emb)
            else:
                await interaction.followup.send(
                    embed=error_emb,
                    ephemeral=True,
                )
