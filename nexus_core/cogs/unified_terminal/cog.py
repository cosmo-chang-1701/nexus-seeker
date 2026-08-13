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

from .utils import get_macro_overview_data, find_matching_polymarket_odds
from .batch_scan_view import BatchScanView
from .symbol_view import SymbolHubView
from .portfolio_view import PortfolioHubView
from .pulse_view import PulseHubView

logger = logging.getLogger(__name__)


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
            scan_results = await asyncio.gather(
                *(self._fetch_sym_radar_data_fast(s) for s in unique_symbols),
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

            from typing import Any

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

                # 2. exclude_martial_law
                if "exclude_martial_law" in quant_filters:
                    mp_data = r.get("max_pain")
                    if isinstance(mp_data, dict):
                        dist = mp_data.get("distance_pct", 0.0)
                        if abs(dist) > max_pain_threshold:
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

            chunk_size = 10
            for idx, emb in enumerate(embeds):
                chunk_results = filtered_results[
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
        from market_time import ny_tz
        from datetime import datetime

        ddp_inspector = DDPInspector(self.bot)
        poly_service = getattr(self.bot, "polymarket_service", None)

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

        async def _safe_get_poly_markets() -> list:
            if poly_service:
                return await poly_service.get_market_snapshot(limit=0)  # type: ignore
            return []

        poly_task = _safe_get_poly_markets()

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

            result = await market_math.analyze_symbol(
                symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not isinstance(result, dict) or not result:
                result = {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}

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

            # Polymarket odds
            poly_odds = await find_matching_polymarket_odds(symbol, poly_markets)
            result["polymarket_odds"] = poly_odds
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
        squeeze_cache = get_squeeze_cache(sym) or {}

        today_str = datetime.now().strftime("%Y-%m-%d")
        iv_metrics = get_kv_cache(f"iv_metrics_{sym.upper()}_{today_str}") or {}

        if "expected_move_lower" not in iv_metrics:
            iv_metrics["expected_move_lower"] = market_cache.get(
                "expected_move_lower", 0.0
            )
        if "expected_move_upper" not in iv_metrics:
            iv_metrics["expected_move_upper"] = market_cache.get(
                "expected_move_upper", 0.0
            )

        avg_vol_20d = radar_cache.get("avg_vol_20d", 0.0)
        rvol = (current_volume / avg_vol_20d) if avg_vol_20d > 0 else 0.0

        mp_near = radar_cache.get("mp_near") or market_cache.get("max_pain")

        return {
            "symbol": sym,
            "quote": quote,
            "rvol": rvol,
            "radar_cache": radar_cache,
            "skew": -0.5 if radar_cache.get("is_skew_extreme") else 0.0,
            "max_pain": {
                "max_pain": mp_near,
                "distance_pct": ((price - mp_near) / mp_near) * 100
                if mp_near and mp_near > 0
                else 0.0,
            },
            "iv_metrics": iv_metrics,
            "iv_data": iv_metrics,
            "uoa": [],
            "psq_result": {
                "is_squeezing": squeeze_cache.get("is_squeezing", False),
                "momentum_value": squeeze_cache.get("momentum", 0.0),
                "signal_direction": squeeze_cache.get("direction", "⚪"),
            },
            "gex_metrics": {"put_wall": radar_cache.get("put_wall_strike")},
            "gex_profile_data": {"put_wall": radar_cache.get("put_wall_strike")},
            "vp_data": {
                "hvn": radar_cache.get("hvn_price")
                or get_kv_cache(f"volume_poc_{sym.upper()}"),
                "lvn": radar_cache.get("lvn_price"),
            },
            "dp_poc": radar_cache.get("hvn_price")
            or get_kv_cache(f"volume_poc_{sym.upper()}"),
        }

    async def _fetch_sym_radar_data_slow_raw(self, sym: str) -> Any:
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

        far_mp_task = _get_far_mp_and_dte(sym)

        iv_m, mp_data, gex_data, (far_mp_val, nearest_dte) = await asyncio.gather(
            iv_task, mp_task, gex_task, far_mp_task
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
            "max_pain": mp_data,
            "uoa": uoa_data,
            "gex_profile_data": gex_data,
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

        await save_kv_cache(
            f"radar_terminal_{sym.upper()}",
            {
                "put_wall_strike": gex_data.get("put_wall")
                if isinstance(gex_data, dict)
                else 0.0,
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
