import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Optional, List

from services import market_data_service, reddit_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
import market_math
import database

from cogs.embed_builder import (
    create_error_embed,
    build_radar_scan_embed,
    create_strategic_dash_embed,
    build_market_macro_overview_embed,
    create_tactical_symbol_embed,
)

from .utils import get_macro_overview_data, find_matching_polymarket_odds
from .batch_scan_view import BatchScanView
from .symbol_view import SymbolHubView
from .portfolio_view import PortfolioHubView
from .pulse_view import PulseHubView

logger = logging.getLogger(__name__)


class UnifiedTerminalCog(commands.Cog):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標體分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(
        symbol="股票代號 (如 NVDA，與 scan_type 二擇一)",
        scan_type="批次掃描類型 (HOLDINGS:持倉, ORDERS:掛單, OPTIONS:持有期權, WATCHLIST:自選, ALL:四者全部)",
        tag="Watchlist 標籤過濾 (僅在 scan_type 為 WATCHLIST 時生效)",
        squeeze="僅顯示正處於擠壓狀態的標的",
    )
    @app_commands.choices(
        scan_type=[
            app_commands.Choice(name="💼 掃描持倉標的 (Holdings)", value="HOLDINGS"),
            app_commands.Choice(
                name="⏳ 掃描掛單標的 (Pending Orders)", value="ORDERS"
            ),
            app_commands.Choice(
                name="📜 掃描期權持倉標的 (Option Holdings)", value="OPTIONS"
            ),
            app_commands.Choice(name="🌟 掃描自選標的 (Watchlist)", value="WATCHLIST"),
            app_commands.Choice(name="🌀 掃描全部 (持倉+掛單+期權標的)", value="ALL"),
        ]
    )
    async def symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: Optional[str] = None,
        scan_type: Optional[app_commands.Choice[str]] = None,
        tag: Optional[str] = None,
        squeeze: Optional[bool] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = interaction.user.id

            # 🚀 Task 2 Hook: Proactive Warmup during pre-market window (08:30 - 09:30 ET)
            if hasattr(self.bot, "memory_manager"):
                coro = self.bot.memory_manager.proactive_warmup()
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)

            # 1. 參數驗證
            if not symbol and not scan_type:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "請輸入 `symbol` 參數，或選擇 `scan_type` 進行批次掃描。",
                        title="輸入錯誤",
                    ),
                    ephemeral=True,
                )

            # 2. 單一標的深度分析
            if symbol:
                symbol = symbol.upper()
                await self._run_single_symbol_hub(interaction, symbol, user_id)
                return

            # 3. 批次掃描邏輯
            if not scan_type:
                return
            scan_value = scan_type.value
            target_symbols = set()

            if scan_value in ("HOLDINGS", "ALL"):
                from services.asset_manager import AssetManager
                from models.asset import ContextType

                manager = AssetManager()
                holding_assets = manager.get_assets(user_id, ContextType.HOLDING)
                for a in holding_assets:
                    target_symbols.add(a.symbol.upper())

            if scan_value in ("ORDERS", "ALL"):
                from database.orders import get_user_active_orders

                active_orders = await asyncio.to_thread(get_user_active_orders, user_id)
                for o in active_orders:
                    target_symbols.add(o["symbol"].upper())

            if scan_value in ("OPTIONS", "ALL"):
                from database.portfolio import get_user_portfolio

                portfolio_rows = await asyncio.to_thread(get_user_portfolio, user_id)
                for row in portfolio_rows:
                    target_symbols.add(row[1].upper())

            if scan_value == "WATCHLIST":
                import database
                from database.watchlist_tags import get_watchlist_tags

                watchlist_items = await asyncio.to_thread(
                    database.get_user_watchlist, user_id
                )
                for item in watchlist_items:
                    sym = item[0].upper()
                    if tag:
                        tags = await asyncio.to_thread(
                            get_watchlist_tags, str(user_id), sym
                        )
                        if tag.upper() not in tags:
                            continue
                    target_symbols.add(sym)

            unique_symbols = sorted(list(target_symbols))

            if not unique_symbols:
                scan_names = {
                    "HOLDINGS": "現貨持倉",
                    "ORDERS": "待成交掛單",
                    "OPTIONS": "期權持倉",
                    "WATCHLIST": "自選標的",
                    "ALL": "持倉、掛單或期權",
                }
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"您目前沒有任何{scan_names.get(scan_value, '相關')}標的，無法進行批次掃描。",
                        title="無標的資料",
                    ),
                    ephemeral=True,
                )

            try:
                # 並行獲取所有標的的雷達數據 (Cache-Aside)
                scan_results = await asyncio.gather(
                    *(self._fetch_sym_radar_data(s) for s in unique_symbols),
                    return_exceptions=True,
                )
                # 過濾 Exception 並確保是 dict 類型以滿足 mypy
                valid_results = [r for r in scan_results if isinstance(r, dict)]

                if squeeze:
                    valid_results = [
                        r
                        for r in valid_results
                        if r.get("psq_result", {}).get("is_squeezing", False)
                    ]

                if not valid_results:
                    return await interaction.followup.send(
                        embed=create_error_embed(
                            "掃描完成，但無符合條件的標的。", title="無結果"
                        ),
                        ephemeral=True,
                    )

                embeds = build_radar_scan_embed(valid_results, scan_value, user_id)
                if not isinstance(embeds, list):
                    embeds = [embeds]

                chunk_size = 15
                for idx, emb in enumerate(embeds):
                    chunk_results = valid_results[
                        idx * chunk_size : (idx + 1) * chunk_size
                    ]
                    chunk_symbols = [r["symbol"].upper() for r in chunk_results]
                    page_view = BatchScanView(chunk_symbols, self, self.bot)
                    await interaction.followup.send(
                        embed=emb, view=page_view, ephemeral=True
                    )

            except Exception as e:
                logger.error(f"Batch Scan Error for {scan_value}: {e}")
                await interaction.followup.send(
                    embed=create_error_embed(f"執行批次掃描時發生錯誤: {e}"),
                    ephemeral=True,
                )
        except Exception as outer_err:
            logger.error(f"Outer Symbol Hub Error: {outer_err}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"執行 `/x` 指令時發生未預期錯誤: {outer_err}"
                    ),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"Failed to send outer error followup: {follow_err}")

    @symbol_hub.autocomplete("tag")
    async def tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        from database.watchlist_tags import get_user_unique_tags
        import asyncio

        user_id_str = str(interaction.user.id)

        try:
            tags = await asyncio.to_thread(get_user_unique_tags, user_id_str)
        except Exception:
            tags = []

        return [
            app_commands.Choice(name=t, value=t)
            for t in tags
            if current.lower() in t.lower()
        ][:25]

    async def _fetch_single_symbol_data_raw(
        self, symbol: str, enable_local_tunnel: bool
    ) -> dict:
        """
        獲取單一標的所需的所有重型量化數據與外部情緒分析。
        供 SingleFlightManager 調度使用。
        """
        from market_analysis.ddp_inspector import DDPInspector
        from services.polymarket_service import PolymarketService
        from market_time import ny_tz
        from datetime import datetime

        ddp_inspector = DDPInspector(self.bot)
        poly_service = PolymarketService(self.bot)

        # 1. 取得所有到期日以規劃一個月內的所有 Max Pain 計算任務
        expiries = []
        try:
            expiries = await market_data_service.get_all_option_expiries(symbol)
        except Exception as e:
            logger.warning(f"[{symbol}] Failed to fetch expiries: {e}")

        today = datetime.now(ny_tz).date()
        valid_expiries = []
        if expiries:
            for exp in expiries:
                try:
                    exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                    # 篩選一個月 (30天) 內的到期日
                    if 0 <= (exp_dt - today).days <= 30:
                        valid_expiries.append(exp)
                except ValueError:
                    continue

        # 針對這一個月內的所有到期日，建立獨立的 Max Pain 計算任務
        mp_month_tasks = {}
        for exp in valid_expiries:
            mp_month_tasks[exp] = SentimentEngine.get_unified_max_pain(
                symbol, expiry=exp
            )

        keys_mp = list(mp_month_tasks.keys())
        tasks_mp = list(mp_month_tasks.values())

        spy_task = market_data_service.get_spy_history_df("1y")
        macro_task = market_data_service.get_macro_environment()
        quote_task = market_data_service.get_quote(symbol)
        skew_task = SentimentEngine.calculate_skew(symbol)
        pcr_task = SentimentEngine.calculate_pcr(symbol)
        uoa_task = SentimentEngine.detect_uoa(symbol)
        mp_task = SentimentEngine.calculate_max_pain(symbol)
        iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        reddit_task = reddit_service.get_reddit_context(
            symbol, enable_tunnel=enable_local_tunnel
        )
        poly_task = poly_service.get_market_snapshot(limit=0)
        ddp_task = ddp_inspector.inspect_symbol(symbol)
        df_hist_task = market_data_service.get_history_df(
            symbol, period="1y", interval="1d"
        )
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics

        gex_profile_task = fetch_symbol_gex_metrics(symbol)

        from market_analysis.volume_profile import calculate_volume_profile
        from market_analysis.dark_pool_engine import fetch_darkpool_prints

        vp_task = asyncio.to_thread(calculate_volume_profile, symbol)
        dp_task = fetch_darkpool_prints(symbol)

        base_results_task = asyncio.gather(
            spy_task,
            macro_task,
            quote_task,
            skew_task,
            pcr_task,
            uoa_task,
            mp_task,
            iv_task,
            reddit_task,
            poly_task,
            ddp_task,
            df_hist_task,
            gex_profile_task,
            vp_task,
            dp_task,
        )

        if tasks_mp:
            results_all = await asyncio.gather(
                base_results_task, asyncio.gather(*tasks_mp)
            )
            base_results, mp_month_results = results_all
        else:
            base_results = await base_results_task
            mp_month_results = []

        (
            df_spy,
            macro_raw,
            quote,
            skew_data,
            pcr_data,
            uoa_data,
            max_pain_data,
            iv_metrics,
            reddit_text,
            poly_markets,
            ddp_report,
            df_hist_1d,
            gex_profile_data,
            vp_data,
            dp_data,
        ) = base_results

        month_max_pains = []
        for exp, res in zip(keys_mp, mp_month_results):
            if res and isinstance(res, dict) and "error" not in res:
                month_max_pains.append(
                    {
                        "expiry": exp,
                        "max_pain": res.get("max_pain"),
                        "distance_pct": res.get("distance_pct", 0.0),
                        "is_degraded": bool(res.get("is_degraded", 0)),
                        "calculation_mode": res.get("calculation_mode", "OI"),
                    }
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
            "reddit_text": reddit_text,
            "poly_markets": poly_markets,
            "ddp_report": ddp_report,
            "df_hist_1d": df_hist_1d,
            "month_max_pains": month_max_pains,
            "gex_profile_data": gex_profile_data,
            "volume_profile": vp_data,
            "darkpool": dp_data,
        }

    async def _run_single_symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: str,
        user_id: int,
        embeds_accumulator: Optional[List[discord.Embed]] = None,
    ):
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
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost = next(
                (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
                0.0,
            )

            ctx = database.get_full_user_context(user_id)

            # 🚀 Task 2 Hook: Coalesced fetch using SingleFlightManager
            from services.single_flight import SingleFlightManager

            data = await SingleFlightManager.run(
                f"single_hub_{symbol}",
                self._fetch_single_symbol_data_raw,
                symbol,
                ctx.enable_local_tunnel,
            )

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
            dp_data = data.get("darkpool")

            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            safe_macro = macro_raw or {}
            macro_data = MacroContext(
                vix=safe_macro.get("vix", 18.0),
                oil_price=safe_macro.get("oil", 75.0),
                vix_change=safe_macro.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}

            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if is_df_valid
                    else result.get("price", 0.0)
                )

            result["quote"] = quote

            safe_skew = skew_data or {}
            result["skew"] = safe_skew.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                symbol, "SKEW", result["skew"]
            )

            result["pcr"] = pcr_data if pcr_data is not None else {}
            result["uoa"] = uoa_data if uoa_data is not None else []

            result["iv_data"] = iv_metrics
            result["iv_rank"] = iv_metrics.iv_rank if iv_metrics else 0.0
            result["expected_move_context"] = await SentimentEngine.get_expected_move(
                symbol, quote=quote, iv_metrics=iv_metrics
            )

            safe_mp = max_pain_data or {}
            result["max_pain"] = safe_mp.get("max_pain", 0.0)
            result["month_max_pains"] = data.get("month_max_pains", [])
            result["gex_profile_data"] = gex_profile_data

            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score
            safe_reddit_text = reddit_text or ""
            if "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            # Polymarket odds
            poly_odds = await find_matching_polymarket_odds(symbol, poly_markets)
            result["polymarket_odds"] = poly_odds
            result["volume_profile"] = vp_data
            result["darkpool"] = dp_data

            # TDP 估值三擊判斷: 現價 < EMA 21 且 現價 < Max Pain 且 現價 < V-POC 且 現價 < DP-POC
            ema_21 = (
                df_hist_1d["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
                if df_hist_1d is not None and not df_hist_1d.empty
                else 0.0
            )
            vpoc = vp_data.get("hvn", 0.0) if vp_data else 0.0
            dp_poc = dp_data.get("dp_poc", 0.0) if dp_data else 0.0
            max_pain = result.get("max_pain", 0.0)
            price = result.get("price", 0.0)

            if result.get("is_ddp"):
                if (
                    price > 0
                    and ema_21 > 0
                    and max_pain > 0
                    and vpoc > 0
                    and dp_poc > 0
                ):
                    if (
                        price < ema_21
                        and price < max_pain
                        and price < vpoc
                        and price < dp_poc
                    ):
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
            logger.error(f"Symbol Hub Error for {symbol}: {e}")
            error_emb = create_error_embed(f"載入 `{symbol}` 資料時發生錯誤: {e}")
            if embeds_accumulator is not None:
                embeds_accumulator.append(error_emb)
            else:
                await interaction.followup.send(
                    embed=error_emb,
                    ephemeral=True,
                )

    async def _async_revalidate_market_cache(self, sym: str, price: float):
        try:
            from market_analysis.sentiment_engine import SentimentEngine

            logger.info(f"🔄 [SWR] Background revalidating market cache for {sym}...")
            # This calls the unified method, calculates, saves to SQLite cache, and handles CB/degradation:
            await SentimentEngine.get_unified_max_pain(sym, force_refresh=True)
            logger.info(f"✅ [SWR] Background revalidation complete for {sym}")
        except Exception as e:
            logger.error(f"❌ [SWR] Background revalidation failed for {sym}: {e}")

    async def _fetch_sym_radar_data(self, sym: str):
        from services.single_flight import SingleFlightManager

        return await SingleFlightManager.run(
            f"analyze_{sym}", self._fetch_sym_radar_data_raw, sym
        )

    async def _fetch_sym_radar_data_raw(self, sym: str):
        """
        獲取單一標的的雷達量化數據。
        採用統一的 get_unified_max_pain 方法讀取與重算快取。
        """
        from market_analysis.sentiment_engine import SentimentEngine
        from services import market_data_service

        # 1. 取得 quote (必須即時，因為是價格)
        quote = await market_data_service.get_quote(sym)
        price = quote.get("c", 0.0) if quote else 0.0

        # 2. 取得 Skew (情緒)
        skew_data = await SentimentEngine.calculate_skew(sym)
        skew_val = skew_data.get("skew", 0.0) if isinstance(skew_data, dict) else 0.0
        skew_percentile = SentimentEngine.get_indicator_percentile(
            sym, "SKEW", skew_val
        )

        # 取得 UOA (異常期權活動) 資料
        uoa_data = []
        try:
            uoa_data = await SentimentEngine.detect_uoa(sym)
        except Exception as e:
            logger.error(f"[{sym}] Batch Scan 獲取 UOA 失敗: {e}")

        iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(sym)
        mp_task = SentimentEngine.get_unified_max_pain(sym)
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics

        gex_task = fetch_symbol_gex_metrics(sym)

        iv_m, mp_data, gex_data = await asyncio.gather(iv_task, mp_task, gex_task)

        # 異步預警：若返回資料標記為 stale，啟動背景重新驗證
        if mp_data.get("is_stale"):
            asyncio.create_task(self._async_revalidate_market_cache(sym, price))

        em_context = await SentimentEngine.get_expected_move(
            sym, quote=quote, iv_metrics=iv_m
        )

        # 取得 IV 數據
        iv_rank_val = 0.0
        em_weekly = float(em_context.get("expected_move_weekly") or 0.0)
        em_lower = float(em_context.get("expected_move_lower") or 0.0)
        em_upper = float(em_context.get("expected_move_upper") or 0.0)

        if iv_m:
            iv_rank_val = iv_m.iv_rank if iv_m.iv_rank is not None else 0.0

        mock_iv = {
            "iv_rank": iv_rank_val,
            "expected_move_weekly": em_weekly,
            "reference_price": em_context.get("reference_price", 0.0),
            "expected_move_lower": em_lower,
            "expected_move_upper": em_upper,
        }

        # 取得 PSQ
        from services.market_data_service import get_history_df

        df_hist = await get_history_df(sym, period="1y", interval="1d")
        psq_res = {}
        if df_hist is not None and not df_hist.empty:
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

        return {
            "symbol": sym,
            "quote": quote,
            "iv_metrics": mock_iv,
            "expected_move_context": em_context,
            "skew": skew_val,
            "skew_percentile": skew_percentile,
            "max_pain": mp_data,
            "uoa": uoa_data,
            "gex_profile_data": gex_data,
            "psq_result": psq_res,
        }

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        from services.trading_service import TradingService
        from services.asset_manager import AssetManager
        from models.asset import ContextType, HoldingMetadata
        from market_analysis.pro_management import calculate_financial_runway

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)

        manager = AssetManager()
        holdings = manager.get_assets(user_id, ContextType.HOLDING)
        total_holding_value = 0.0
        for h in holdings:
            meta = HoldingMetadata(**h.metadata)
            quote = await market_data_service.get_quote(h.symbol)
            total_holding_value += (
                quote.get("c", 0.0) if quote else 0.0
            ) * meta.quantity
        backup_liq = total_holding_value * 0.8
        ext_runway = calculate_financial_runway(
            ctx.cash_reserve + backup_liq, ctx.monthly_expense, ctx.total_theta
        )

        # 獲取 VIX 資訊
        macro_raw = await market_data_service.get_macro_environment()
        vix_spot = macro_raw.get("vix", 18.0)

        embed = create_strategic_dash_embed(
            ctx,
            pnl_data,
            vix_spot=vix_spot,
            backup_liquidity=backup_liq,
            extended_runway=ext_runway,
        )

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        macro_data = await get_macro_overview_data(interaction.user.id)
        embed = build_market_macro_overview_embed(macro_data)

        view = PulseHubView(interaction.user.id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="stress_test",
        description="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)",
    )
    async def stress_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        try:
            from database.orders import get_user_active_orders

            orders = get_user_active_orders(user_id)
            total_deficit = 0.0
            gtc_buy_orders = []
            for o in orders:
                validity = o.get("validity", "").upper()
                side = o.get("side", "").upper()
                if "GTC" in validity and side == "BUY":
                    price = o.get("limit_price", 0.0)
                    if price <= 0.0:
                        price = o.get("stop_price", 0.0)
                    qty = o.get("quantity", 0.0)
                    total_deficit += price * qty
                    gtc_buy_orders.append(o)
            ctx = database.get_full_user_context(user_id)
            cash_reserve = ctx.cash_reserve if ctx else 0.0

            from database.holdings import get_user_holdings

            holdings = get_user_holdings(user_id)
            boxx_shares = 0.0
            for h in holdings:
                if h.get("symbol", "").upper() == "BOXX":
                    boxx_shares = h.get("quantity", 0.0)
                    break
            boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
            net_deficit = cash_reserve + boxx_cash - total_deficit
            is_critical = total_deficit > (cash_reserve + boxx_cash)

            results = {
                "total_deficit": total_deficit,
                "cash_reserve": cash_reserve,
                "boxx_shares": boxx_shares,
                "boxx_cash": boxx_cash,
                "net_deficit": net_deficit,
                "is_critical": is_critical,
                "gtc_buy_orders_count": len(gtc_buy_orders),
            }
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )
