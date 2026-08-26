from typing import Any
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

from .utils import (
    get_macro_overview_data,
    find_matching_polymarket_odds,
    calculate_polymarket_weighted_odds,
)
from .batch_scan_view import BatchScanPaginatedView
from .symbol_view import SymbolHubView
from .portfolio_view import PortfolioHubView
from .pulse_view import PulseHubView

logger = logging.getLogger(__name__)

# 限制 /x 批次雷達掃描的併發標的數，適度提高至 15 以加速大清單處理
_RADAR_SCAN_SEM = asyncio.Semaphore(15)
_SWR_REVALIDATE_SEM = asyncio.Semaphore(3)
_active_swr_tasks: set[str] = set()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class UnifiedTerminalCog(commands.Cog):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標體分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(
        symbol="股票代號 (如 NVDA，與 scan_type 二擇一)",
        scan_type="批次掃描類型 (留空則開啟量化雷達面板)",
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
    ) -> Any:
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = interaction.user.id

            # 🚀 Task 2 Hook: Proactive Warmup during pre-market window (08:30 - 09:30 ET)
            if hasattr(self.bot, "memory_manager"):
                coro = self.bot.memory_manager.proactive_warmup()
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)

            # 1. 參數驗證
            # (移除了 symbol 與 scan_type 的強制驗證，因為現在沒有帶參數會開啟控制面板)

            # 2. 單一標的深度分析
            if symbol:
                symbol = symbol.upper()
                await self._run_single_symbol_hub(interaction, symbol, user_id)
                return

            # 3. 批次掃描邏輯 / 開啟面板
            if not scan_type:
                from .radar_view import UnifiedRadarView
                from cogs.embed_builders.scan_embeds import (
                    build_unified_radar_panel_embed,
                )

                view = UnifiedRadarView(self, user_id)
                embed = build_unified_radar_panel_embed(view.get_state_dict())
                return await interaction.followup.send(
                    embed=embed, view=view, ephemeral=True
                )

            scan_value = scan_type.value

            # 建立相容舊參數的 State Dict 供引擎使用
            state = {
                "scope": scan_value,
                "quant_filters": ["squeeze_mode"] if squeeze else [],
                "params": {
                    "max_pain_threshold": 10.0,
                    "abs_support_tolerance": 1.0,
                    "silent_period_days": 5,
                },
                "selected_tag": tag,
            }

            await self.execute_unified_scan(interaction, state, user_id)

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

    @market_data_service.interactive
    async def execute_unified_scan(
        self, interaction: discord.Interaction, state: dict, user_id: int
    ) -> Any:
        scan_value = state.get("scope", "WATCHLIST")
        tag = state.get("selected_tag")
        quant_filters = set(state.get("quant_filters", []))
        params = state.get("params", {})

        target_symbols = set()

        try:
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

            # 並行獲取所有標的的雷達數據 (Cache-Aside)
            # 使用 _RADAR_SCAN_SEM 限制併發數，避免大清單 (ALL) 造成請求洪峰。
            async def _throttled_fetch(sym: str) -> Any:
                async with _RADAR_SCAN_SEM:
                    return await self._fetch_sym_radar_data_fast(sym)

            scan_results = await asyncio.gather(
                *(_throttled_fetch(s) for s in unique_symbols),
                return_exceptions=True,
            )
            # 過濾 Exception 並確保是 dict 類型以滿足 mypy
            valid_results = [r for r in scan_results if isinstance(r, dict)]

            # 根據 Unified Radar Panel 的量化過濾條件進行篩選
            filtered_results = []
            max_pain_threshold = params.get("max_pain_threshold", 10.0) / 100.0

            from models.schemas import ScanParams
            from market_analysis.intraday_pipeline import evaluate_advanced_filters
            import types

            scan_params_kwargs: dict[str, Any] = {}
            if "tdp_mode" in quant_filters or "require_tdp_signal" in quant_filters:
                scan_params_kwargs["require_tdp_signal"] = True
            if "squeeze_mode" in quant_filters:
                scan_params_kwargs["require_squeeze_firing"] = True
            if "uoa_mode" in quant_filters:
                scan_params_kwargs["min_net_uoa_delta"] = 1.0
                scan_params_kwargs["dark_pool_skew_floor"] = -0.2

            advanced_active = bool(scan_params_kwargs)
            adv_params = ScanParams(**scan_params_kwargs)

            for r in valid_results:
                passed = True

                # 1. dp_skew_defense (防護派發風險)
                if "dp_skew_defense" in quant_filters:
                    skew_val = r.get("skew", 0.0)
                    if skew_val < -0.3:
                        passed = False

                # 2. exclude_martial_law (排除底牆破位 / 負 Gamma / 痛點極端偏離)
                if "exclude_martial_law" in quant_filters:
                    gex_data = r.get("gex_profile_data", {}) or r.get("gex_metrics", {})
                    pw_val = gex_data.get("put_wall") if gex_data else None
                    put_wall_val = float(pw_val) if pw_val is not None else 0.0
                    net_gex_val = (
                        float(gex_data.get("net_gex", 0.0) or 0.0) if gex_data else 0.0
                    )

                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    mp_data = r.get("max_pain")
                    dist = (
                        mp_data.get("distance_pct", 0.0)
                        if isinstance(mp_data, dict)
                        else 0.0
                    )
                    if (
                        (
                            put_wall_val > 0
                            and current_price > 0
                            and current_price < put_wall_val
                        )
                        or net_gex_val < 0
                        or abs(dist) > max_pain_threshold
                    ):
                        passed = False

                # 3. avoid_silent_period (規避財報/總經靜默期)
                if "avoid_silent_period" in quant_filters:
                    iv_data = r.get("iv_data")
                    if iv_data:
                        earnings_loading = getattr(
                            iv_data, "has_earnings_event", False
                        ) or (
                            isinstance(iv_data, dict)
                            and iv_data.get("has_earnings_event", False)
                        )
                        macro_loading = getattr(iv_data, "has_macro_event", False) or (
                            isinstance(iv_data, dict)
                            and iv_data.get("has_macro_event", False)
                        )
                        if earnings_loading or macro_loading:
                            passed = False

                # 4. magnetic_filters (高階磁吸過濾)
                if "magnetic_filters" in quant_filters:
                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    mp_data = r.get("max_pain")
                    mp_val = (
                        mp_data.get("max_pain") if isinstance(mp_data, dict) else 0.0
                    )
                    max_pain_val = float(mp_val) if mp_val is not None else 0.0

                    gex_data = r.get("gex_profile_data", {})
                    pw_val = gex_data.get("put_wall") if gex_data else 0.0
                    putwall = float(pw_val) if pw_val is not None else 0.0

                    dp_val = r.get("dp_poc")
                    dp_poc = float(dp_val) if dp_val is not None else 0.0

                    min_dev = params.get("min_max_pain_dev", 0.10)
                    tolerance = params.get("abs_support_tolerance", 1.0) / 100.0

                    if current_price > 0 and max_pain_val > 0:
                        if abs(current_price - max_pain_val) / max_pain_val <= min_dev:
                            passed = False
                    else:
                        passed = False

                    if current_price > 0 and putwall > 0 and current_price < putwall:
                        passed = False

                    if dp_poc > 0 and putwall > 0:
                        if abs(dp_poc - putwall) / putwall >= tolerance:
                            passed = False
                    else:
                        passed = False

                # 5. Advanced Filters (ScanParams)
                if passed and advanced_active:
                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    psq_res = r.get("psq_result", {})
                    gex_data = r.get("gex_profile_data", {})

                    pw_val = gex_data.get("put_wall") if gex_data else None
                    put_wall = float(pw_val) if pw_val is not None else None

                    mp_data = r.get("max_pain")
                    mp_val = (
                        mp_data.get("max_pain") if isinstance(mp_data, dict) else 0.0
                    )
                    max_pain_val = float(mp_val) if mp_val is not None else 0.0

                    pseudo_metrics = types.SimpleNamespace(
                        squeeze_status=psq_res.get("is_squeezing", False),
                        squeeze_momentum=psq_res.get(
                            "momentum_value", psq_res.get("momentum", 0.0)
                        ),
                        current_price=current_price,
                        dark_pool_skew=r.get("skew", 0.0),
                        volume_poc=None,  # volume profile may not be fully available in batch scan
                        gex_max_put_wall=put_wall,
                        ma20=r.get("ma20"),
                        max_pain=max_pain_val,
                        dp_poc=r.get("dp_poc", 0.0),
                    )

                    is_adv_passed, adv_tags = evaluate_advanced_filters(
                        metrics=pseudo_metrics,
                        symbol_gex=gex_data,
                        uoa_data=r.get("uoa", []),
                        params=adv_params,
                    )
                    if not is_adv_passed:
                        passed = False
                    else:
                        r["advanced_tags"] = adv_tags

                if passed:
                    filtered_results.append(r)

            if not filtered_results:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "掃描完成，但無符合條件的標的。", title="無結果"
                    ),
                    ephemeral=True,
                )

            embeds = build_radar_scan_embed(filtered_results, scan_value, user_id)
            if not isinstance(embeds, list):
                embeds = [embeds]

            # 多頁結果一律封裝進單一則訊息的換頁 View（BatchScanPaginatedView），
            # 只送出一次 interaction.followup.send()，翻頁改由使用者點擊 ◀/▶
            # 就地編輯同一則訊息。無論結果有幾頁，都不會再逐頁呼叫 followup.send()
            # 而撞上 Discord 互動的隱性 followup 訊息數量上限（錯誤碼 40094）。
            pager_view = BatchScanPaginatedView(
                embeds, self, self.bot, total_items=len(filtered_results)
            )
            await interaction.followup.send(
                embed=embeds[0], view=pager_view, ephemeral=True
            )

        except Exception as e:
            logger.error(f"Batch Scan Error for {scan_value}: {e}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(f"執行批次掃描時發生錯誤: {e}"),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"Failed to send batch scan error followup: {follow_err}")

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
        from market_analysis.dark_pool_engine import fetch_darkpool_prints

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
        dp_task = asyncio.create_task(fetch_darkpool_prints(symbol))
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
            valid_expiries = []
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

            month_max_pains = []
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
            dp_data,
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
            dp_task,
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
            "darkpool": dp_data,
        }

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
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost_raw = next(
                (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
                0.0,
            )
            stock_cost = _safe_float(stock_cost_raw, 0.0)

            # 🚀 Task 2 Hook: Coalesced fetch using SingleFlightManager
            from services.single_flight import SingleFlightManager

            data = await SingleFlightManager.run(
                f"single_hub_{symbol}",
                self._fetch_single_symbol_data_raw,
                symbol,
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
            poly_task = find_matching_polymarket_odds(
                symbol, poly_markets, bot=self.bot
            )
            poly_summary_task = calculate_polymarket_weighted_odds(
                symbol, poly_markets, bot=self.bot
            )

            result_math, poly_odds, poly_summary = await asyncio.gather(
                math_task, poly_task, poly_summary_task
            )
            result = (
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

            safe_ddp = ddp_report if isinstance(ddp_report, dict) else {}
            result["is_ddp"] = bool(safe_ddp.get("is_ddp", False))
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score
            safe_reddit_text = reddit_text or ""
            if any(
                err in safe_reddit_text for err in ["錯誤", "異常", "超時", "尚未配置"]
            ):
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
            safe_dp = dp_data if isinstance(dp_data, dict) else {}
            result["volume_profile"] = safe_vp
            result["darkpool"] = safe_dp

            # TDP 估值三擊判斷: 現價 < EMA 21 且 現價 < Max Pain 且 現價 < V-POC 且 現價 < DP-POC
            ema_21 = (
                df_hist_1d["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
                if df_hist_1d is not None and not df_hist_1d.empty
                else 0.0
            )
            vpoc = _safe_float(safe_vp.get("hvn"), 0.0)
            dp_poc = _safe_float(safe_dp.get("dp_poc"), 0.0)
            max_pain = _safe_float(result.get("max_pain"), 0.0)
            price = _safe_float(result.get("price"), 0.0)

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
            logger.exception(f"Symbol Hub Error for {symbol}: {e}")
            error_emb = create_error_embed(f"載入 `{symbol}` 資料時發生錯誤: {e}")
            if embeds_accumulator is not None:
                embeds_accumulator.append(error_emb)
            else:
                await interaction.followup.send(
                    embed=error_emb,
                    ephemeral=True,
                )

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
        from services import market_data_service
        from database.market_cache import get_market_cache
        from database.squeeze_cache import get_squeeze_cache
        from database.cache import get_kv_cache
        from datetime import datetime

        quote = await market_data_service.get_quote(sym)
        price = quote.get("c", 0.0) if quote else 0.0
        current_volume = quote.get("volume", 0) if quote else 0

        radar_cache = get_kv_cache(f"radar_terminal_{sym.upper()}") or {}
        market_cache = get_market_cache(sym) or {}
        squeeze_cache = get_squeeze_cache(sym)
        gex_cached = get_kv_cache(f"gex_metrics_{sym.upper()}") or {}
        gex_data = gex_cached.get("data", {}) if isinstance(gex_cached, dict) else {}

        uoa_data: list[Any] = []
        uoa_cached = get_kv_cache(f"uoa_{sym.upper()}")
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
        dp_poc_val = get_kv_cache(f"dp_poc_{sym.upper()}")
        if dp_poc_val is None:
            dp_poc_val = (
                darkpool_cached.get("dp_poc")
                or radar_cache.get("hvn_price")
                or get_kv_cache(f"volume_poc_{sym.upper()}")
            )
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
            },
            "iv_metrics": iv_metrics,
            "iv_data": iv_metrics,
            "uoa": uoa_data,
            "darkpool": darkpool_cached,
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
            },
            "gex_profile_data": {
                "put_wall": put_wall,
                "call_wall": call_wall,
                "net_gex": net_gex,
                "gex_profile": gex_data.get("gex_profile", {}),
                "put_wall_gex": put_wall_gex,
                "positive_gex_below": pos_gex_below,
                "overhead_neg_gex_swamp": overhead_neg_swamp,
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
        from database.cache import get_kv_cache

        dp_poc_val = get_kv_cache(f"dp_poc_{sym.upper()}")
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
            },
            "psq_result": psq_res,
            "dp_poc": dp_poc,
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

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction) -> Any:
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
    async def pulse_hub(self, interaction: discord.Interaction) -> Any:
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
    async def stress_test(self, interaction: discord.Interaction) -> Any:
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
