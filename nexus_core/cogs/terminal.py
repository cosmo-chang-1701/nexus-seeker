import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import database
import market_math
from services import market_data_service
from cogs.embed_builder import (
    create_asset_promotion_embed,
    create_error_embed,
    create_financial_runway_embed,
    create_info_embed,
    create_scan_embed,
    create_system_health_embed,
    create_transition_simulation_embed,
    create_notification_settings_embed,
    create_account_settings_embed,
)
from database.user_settings import get_full_user_context

logger = logging.getLogger(__name__)


class TerminalCog(commands.Cog):
    """
    [Core] Nexus Seeker Professional Terminal Interface.
    Retains only the high-impact commands for professional operations.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("TerminalCog loaded.")

        # 為了相容集成測試與 Embed 驗證，我們動態將 update_settings.callback 包裝成支援關鍵字參數的形式
        # 這樣一來，斜線指令在 Discord 註冊時仍然是完全不帶參數的（使用者點選即可喚起面板），但 Python 測試可以直接傳參
        async def compat_callback(cog, interaction, **kwargs):
            return await cog._update_settings_impl(interaction, **kwargs)

        self.update_settings._callback = compat_callback

    @app_commands.command(
        name="settings", description="配置帳戶全域參數 (資金、風險與專業營運指標)"
    )
    async def update_settings(self, interaction: discord.Interaction):
        """喚起帳戶設定互動式面板"""
        await self._update_settings_impl(interaction)

    async def _update_settings_impl(
        self,
        interaction: discord.Interaction,
        capital: Optional[float] = None,
        risk_limit: Optional[float] = None,
        enable_vtr: Optional[bool] = None,
        enable_psq_watchlist: Optional[bool] = None,
        enable_local_tunnel: Optional[bool] = None,
        polymarket_threshold: Optional[float] = None,
        polymarket_use_llm: Optional[bool] = None,
        polymarket_slippage: Optional[float] = None,
        monthly_expense: Optional[float] = None,
        tax_reserve_rate: Optional[float] = None,
        cash_reserve: Optional[float] = None,
    ):
        """喚起帳戶設定互動式面板，或直接配置特定參數"""
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 判斷是否為無參數調用（互動式表單模式）
        is_interactive = all(
            v is None
            for v in [
                capital,
                risk_limit,
                enable_vtr,
                enable_psq_watchlist,
                enable_local_tunnel,
                polymarket_threshold,
                polymarket_use_llm,
                polymarket_slippage,
                monthly_expense,
                tax_reserve_rate,
                cash_reserve,
            ]
        )

        if is_interactive:
            # 1. 互動式表單模式 (無參數調用)
            view = AccountSettingsView(user_id)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return

        # 2. 直接更新模式 (參數化調用，供集成測試或腳本使用)
        updates = []
        db_updates = {}

        if capital is not None:
            if capital > 0:
                db_updates["capital"] = capital
                updates.append(f"💰 總資金: `${capital:,.2f}`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed("資金必須大於 0", title="系統錯誤"),
                    ephemeral=True,
                )

        if risk_limit is not None:
            if 1.0 <= risk_limit <= 50.0:
                db_updates["risk_limit"] = risk_limit
                updates.append(f"🛡️ 風險限制: `{risk_limit}%`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "風險限制需介於 1.0% 至 50.0% 之間", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

        if enable_vtr is not None:
            db_updates["enable_vtr"] = enable_vtr
            updates.append(f"👻 虛擬交易室 (VTR): `{'開啟' if enable_vtr else '關閉'}`")

        if enable_psq_watchlist is not None:
            db_updates["enable_psq_watchlist"] = enable_psq_watchlist
            updates.append(
                f"⚡ PowerSqueeze 追蹤: `{'開啟' if enable_psq_watchlist else '關閉'}`"
            )

        if enable_local_tunnel is not None:
            db_updates["enable_local_tunnel"] = enable_local_tunnel
            updates.append(
                f"🛜 本地 Tunnel 呼叫: `{'開啟' if enable_local_tunnel else '關閉'}`"
            )

        if polymarket_threshold is not None:
            db_updates["polymarket_threshold"] = polymarket_threshold
            status = (
                f"`${polymarket_threshold:,.0f}`"
                if polymarket_threshold > 0
                else "`關閉`"
            )
            updates.append(f"🐋 Polymarket 監控: {status}")

        if polymarket_use_llm is not None:
            db_updates["polymarket_use_llm"] = polymarket_use_llm
            updates.append(
                f"🧠 Polymarket AI 分析: `{'開啟' if polymarket_use_llm else '關閉'}`"
            )

        if polymarket_slippage is not None:
            if 0.1 <= polymarket_slippage <= 10.0:
                db_updates["polymarket_slippage"] = polymarket_slippage
                updates.append(f"🌊 Polymarket 滑價門檻: `{polymarket_slippage}%`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "滑價門檻需介於 0.1% 至 10.0% 之間", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

        if monthly_expense is not None:
            if monthly_expense >= 0:
                db_updates["monthly_expense"] = monthly_expense
                updates.append(f"💸 每月支出預算: `${monthly_expense:,.0f}`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed("支出預算不能為負數", title="系統錯誤"),
                    ephemeral=True,
                )

        if tax_reserve_rate is not None:
            if 0.0 <= tax_reserve_rate <= 1.0:
                db_updates["tax_reserve_rate"] = tax_reserve_rate
                updates.append(f"🏦 稅務預留比例: `{tax_reserve_rate:.1%}`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "稅務比例需介於 0.0 與 1.0 之間", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

        if cash_reserve is not None:
            if cash_reserve >= 0:
                db_updates["cash_reserve"] = cash_reserve
                updates.append(f"💰 現金儲備: `${cash_reserve:,.0f}`")
            else:
                return await interaction.followup.send(
                    embed=create_error_embed("現金儲備不能為負數", title="系統錯誤"),
                    ephemeral=True,
                )

        success = database.upsert_user_config(user_id, **db_updates)
        if not success:
            return await interaction.followup.send(
                embed=create_error_embed("設定失敗，請稍後再試。", title="系統錯誤"),
                ephemeral=True,
            )

        msg = "✅ **帳戶設定已更新**：\n" + "\n".join(updates)
        await interaction.followup.send(
            embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
        )

    @app_commands.command(
        name="runway_check", description="執行財務生存跑道與 Theta 收益分析"
    )
    async def runway_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 🚀 執行即時 Greeks 刷新，確保數據最新
        from market_analysis.portfolio import refresh_portfolio_greeks

        await refresh_portfolio_greeks(user_id)

        ctx = get_full_user_context(user_id)

        if ctx.monthly_expense <= 0:
            return await interaction.followup.send(
                embed=create_error_embed(
                    "請先使用 `/settings` 配置您的每月支出 (monthly_expense)，才能計算跑道。",
                    title="系統警告",
                ),
                ephemeral=True,
            )

        from market_analysis.pro_management import calculate_financial_runway

        runway_days = calculate_financial_runway(
            cash_reserve=ctx.cash_reserve,
            monthly_expense=ctx.monthly_expense,
            daily_theta=ctx.total_theta,
        )

        # 🚀 [Unified Asset Lifecycle] 計算 HOLDING 資產的備用流動性 (含 20% Haircut)
        from services.asset_manager import AssetManager
        from models.asset import ContextType, HoldingMetadata

        manager = AssetManager()
        holdings = manager.get_assets(user_id, ContextType.HOLDING)

        total_holding_value = 0.0
        for h in holdings:
            meta = HoldingMetadata(**h.metadata)
            quote = await market_data_service.get_quote(h.symbol)
            price = quote.get("c", 0.0) if quote else 0.0
            total_holding_value += price * meta.quantity

        backup_liquidity = total_holding_value * 0.8  # 20% Haircut

        # 計算含備用流動性的跑道
        extended_runway = calculate_financial_runway(
            cash_reserve=ctx.cash_reserve + backup_liquidity,
            monthly_expense=ctx.monthly_expense,
            daily_theta=ctx.total_theta,
        )

        ratio = (
            (ctx.total_theta * 30) / ctx.monthly_expense
            if ctx.monthly_expense > 0
            else 0
        )
        embed = create_financial_runway_embed(
            cash_reserve=ctx.cash_reserve,
            monthly_expense=ctx.monthly_expense,
            total_theta=ctx.total_theta,
            runway_days=runway_days,
            backup_liquidity=backup_liquidity,
            extended_runway=extended_runway,
            total_holding_value=total_holding_value,
            ratio=ratio,
            footer_text="Nexus Risk Engine | 跑道計算含 20% 流動性折價",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="add_trade", description="將新的選擇權部位加入監控管線")
    @app_commands.choices(
        opt_type=[
            app_commands.Choice(name="Put (賣權)", value="put"),
            app_commands.Choice(name="Call (買權)", value="call"),
        ]
    )
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        opt_type="策略類型",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        entry_price="成交價格",
        quantity="口數",
    )
    async def add_trade(
        self,
        interaction: discord.Interaction,
        symbol: str,
        opt_type: app_commands.Choice[str],
        strike: float,
        expiry: str,
        entry_price: float,
        quantity: int,
    ):
        symbol = symbol.upper()
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True)

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        # 🛡️ Defensive Programming: Validate Expiry Date Format

        try:
            # Only capture the first 10 characters (YYYY-MM-DD) to prevent trailing argument capture
            expiry_clean = expiry.split(" ")[0]
            datetime.strptime(expiry_clean, "%Y-%m-%d")
            expiry = expiry_clean  # Standardized format
        except Exception:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"**日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )
            return

        try:
            from services.asset_manager import AssetManager
            from models.asset import Asset, ContextType

            manager = AssetManager()

            # 🚀 自動抓取目前持倉數據以取得平均成本 (stock_cost) 與持倉量
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost = 0.0
            holding_qty = 0.0
            for a in assets:
                if a.symbol == symbol:
                    stock_cost = float(a.metadata.get("avg_cost", 0.0))
                    holding_qty = float(a.metadata.get("quantity", 0.0))
                    break

            # 🚀 根據相關數據自動判定部位分類 (Auto-classify trade category)
            is_market_etf = symbol in ("SPY", "QQQ", "IWM")
            is_short_position = quantity < 0
            is_long_put = opt_type.value == "put" and quantity > 0

            # 備兌買權特徵 (Covered Call): 賣出 Call 且用戶持有足夠現貨 (HOLDING)
            is_covered_call = False
            if opt_type.value == "call" and quantity < 0:
                needed_shares = abs(quantity) * 100
                if holding_qty >= needed_shares:
                    is_covered_call = True

            if (
                is_market_etf and (is_short_position or is_long_put)
            ) or is_covered_call:
                trade_category = "HEDGE"
            else:
                trade_category = "SPECULATIVE"

            trade_details = {
                "opt_type": opt_type.value,
                "strike": strike,
                "expiry": expiry,
                "entry_price": entry_price,
                "quantity": quantity,
                "category": trade_category,
                "stock_cost": stock_cost,
            }

            asset = Asset(
                user_id=user_id,
                symbol=symbol,
                context_type=ContextType.TRADE,
                metadata=trade_details,
            )

            success = manager.add_asset(asset)
            if success:
                from market_analysis.portfolio import refresh_portfolio_greeks

                await refresh_portfolio_greeks(user_id)
                action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
                await interaction.followup.send(
                    embed=create_info_embed(
                        title="操作成功",
                        message=f"✅ **新增交易成功**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()}",
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "新增交易失敗，請稍後再試。", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

        except Exception as e:
            logger.error(f"Add trade failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"**發生錯誤**: {e}", title="操作失敗"),
                ephemeral=True,
            )

    @app_commands.command(
        name="edit_trade", description="修改實單交易參數 (履約價、到期日、價格或口數)"
    )
    @app_commands.describe(
        trade_id="資產 ID (從 /list_trades 獲取)",
        strike="更新履約價 (選填)",
        expiry="更新到期日 YYYY-MM-DD (選填)",
        price="更新成交價格 (選填)",
        quantity="更新口數 (選填)",
        category="更新類別 SPECULATIVE/HEDGE (選填)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="SPECULATIVE", value="SPECULATIVE"),
            app_commands.Choice(name="HEDGE", value="HEDGE"),
        ]
    )
    async def edit_trade(
        self,
        interaction: discord.Interaction,
        trade_id: int,
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        category: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager

        manager = AssetManager()

        updates: Dict[str, Any] = {}
        if strike is not None:
            updates["strike"] = strike
        if expiry is not None:
            from datetime import datetime

            try:
                expiry_clean = expiry.split(" ")[0]
                datetime.strptime(expiry_clean, "%Y-%m-%d")
                updates["expiry"] = expiry_clean
            except Exception:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"**日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。",
                        title="系統錯誤",
                    ),
                    ephemeral=True,
                )
        if price is not None:
            updates["entry_price"] = price
        if quantity is not None:
            updates["quantity"] = quantity
        if category is not None:
            updates["category"] = category.value

        if not updates:
            return await interaction.followup.send(
                embed=create_info_embed(
                    title="系統資訊", message=" 請提供至少一個要修改的參數。"
                ),
                ephemeral=True,
            )

        success = manager.update_asset_metadata(interaction.user.id, trade_id, updates)
        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功", message=f"✅ **交易紀錄已更新 (ID: {trade_id})**"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"找不到交易 ID `{trade_id}` 或發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="scan", description="手動執行量化掃描與 What-if 曝險模擬"
    )
    async def manual_scan(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        user_id, symbol = interaction.user.id, symbol.upper()

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        # 🚀 獲取用戶現貨成本 (如果有)
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.HOLDING)
        stock_cost = next(
            (
                float(a.metadata.get("avg_cost", 0.0))
                for a in assets
                if a.symbol == symbol
            ),
            0.0,
        )

        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            from market_analysis.risk_engine import MacroContext

            macro_data = MacroContext(
                vix=macro_raw.get("vix", 18.0),
                oil_price=macro_raw.get("oil", 75.0),
                vix_change=macro_raw.get("vix_change", 0.0),
            )
        except Exception:
            df_spy, spy_price, macro_data = (
                None,
                670.0,
                MacroContext(vix=22.0, oil_price=85.0, vix_change=0.0),
            )

        result = await market_math.analyze_symbol(
            symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
        )
        is_option_valid = bool(result)
        if not result:
            result = {"symbol": symbol, "stock_cost": stock_cost}

        # 🚀 執行 Gap & Fill 跳空分析 (New)
        try:
            from market_analysis.gap_analysis import GapAnalyzer

            df_gap = await market_data_service.get_history_df(
                symbol, period="5d", interval="1d"
            )
            if not df_gap.empty and len(df_gap) >= 2:
                gap_metrics = GapAnalyzer.analyze_gap(df_gap)
                if gap_metrics:
                    result["gap_status"] = gap_metrics
        except Exception as gap_e:
            logger.warning(f"手動掃描 Gap 分析失敗 for {symbol}: {gap_e}")

        df_hist_1d = await market_data_service.get_history_df(
            symbol, period="1y", interval="1d"
        )
        from market_analysis.psq_engine import analyze_psq
        from cogs.embed_builder import create_psq_embed

        psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
        if psq_result:
            result["psq_result"] = psq_result

        embeds_to_send = []
        if is_option_valid:
            from services import llm_service, news_service
            from market_analysis.risk_engine import optimize_position_risk
            from market_analysis.sentiment_engine import SentimentEngine

            # 使用快取 Reddit 資料
            from database.cache import get_kv_cache

            reddit_text = (
                get_kv_cache(f"reddit_sentiment_{symbol}") or "暫無快取情緒資料。"
            )
            news_text = await news_service.fetch_recent_news(symbol)

            # 並行獲取期權情緒指標
            skew_task = SentimentEngine.calculate_skew(symbol)
            pcr_task = SentimentEngine.calculate_pcr(symbol)
            uoa_task = SentimentEngine.detect_uoa(symbol)

            skew_data, pcr_data, uoa_list = await asyncio.gather(
                skew_task, pcr_task, uoa_task
            )
            pcr_val = pcr_data.get("pcr", 0.8)
            skew_val = skew_data.get("skew", 0.0)

            ai_verdict = await llm_service.evaluate_trade_risk(
                symbol, result.get("strategy", ""), news_text, reddit_text
            )
            result.update(
                {
                    "news_text": news_text,
                    "reddit_text": reddit_text,
                    "ai_decision": ai_verdict.get("decision", "APPROVE"),
                    "ai_reasoning": ai_verdict.get("reasoning", "無資料"),
                    "vix": macro_data.vix,
                    "oil": macro_data.oil_price,
                    "pcr": pcr_val,
                    "skew": skew_val,
                    "uoa_list": uoa_list,
                }
            )

            user_context = database.get_full_user_context(user_id)
            opt_res = optimize_position_risk(
                current_delta=user_context.total_weighted_delta,
                unit_weighted_delta=result.get("weighted_delta", 0.0),
                user_capital=user_context.capital,
                spy_price=spy_price,
                stock_iv=result.get("iv", 0.15),
                strategy=result.get("strategy", ""),
                macro_data=macro_data,
                risk_limit=user_context.risk_limit,
                vix_spot=macro_data.vix,
                pcr=pcr_val,
                skew=skew_val,
            )
            safe_qty = opt_res.suggested_contracts
            hedge_spy = opt_res.suggested_hedge_spy
            projected_exposure_pct = opt_res.exposure_pct

            result.update(
                {
                    "projected_exposure_pct": round(projected_exposure_pct, 2),
                    "safe_qty": safe_qty,
                    "hedge_spy": hedge_spy,
                    "spy_price": spy_price,
                    "risk_limit": user_context.risk_limit,
                }
            )
            embeds_to_send.append(create_scan_embed(result, user_context.capital))

        if psq_result:
            result["price"] = (
                df_hist_1d["Close"].iloc[-1] if not df_hist_1d.empty else 0.0
            )
            embeds_to_send.append(create_psq_embed(result))

        if embeds_to_send:
            await interaction.followup.send(embeds=embeds_to_send)
        else:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="系統資訊", message=f" 目前 `{symbol}` 查無有效訊號。"
                )
            )

    @app_commands.command(
        name="vtr_stats", description="檢視虛擬交易室的績效統計與對沖歸因"
    )
    async def vtr_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from market_analysis.ghost_trader import GhostTrader
            from market_analysis.attribution import AttributionEngine

            # 0. 結算目前的對沖日誌 (歸因分析)
            await AttributionEngine.finalize_vtr_attribution(interaction.user.id)

            # 1. 獲取基礎統計
            stats = await GhostTrader.get_vtr_performance_stats(interaction.user.id)

            # 2. 獲取對沖歸因報告
            attr_lines = AttributionEngine.format_attribution_report(
                interaction.user.id
            )

            # 3. 建立 Embed
            from cogs.embed_builder import build_vtr_stats_embed

            embed = build_vtr_stats_embed(
                interaction.user.display_name, stats, attr_lines
            )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"VTR Stats failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"無法獲取績效數據: {e}", title="操作失敗"),
                ephemeral=True,
            )

    @app_commands.command(
        name="sys_health", description="[Hidden] 檢查系統資源狀態與記憶體健康度"
    )
    async def sys_health(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import psutil
        import os
        from services import market_data_service

        # 1. 系統資源
        mem = psutil.virtual_memory()
        cpu_load = psutil.cpu_percent()
        disk = psutil.disk_usage("/")
        process = psutil.Process(os.getpid())
        proc_mem = process.memory_info().rss / (1024 * 1024)  # MB

        # 2. 快取狀態
        # 注意：這裡直接存取 private 變數僅供監控
        sma_count = len(market_data_service._sma_cache)
        ema_count = len(market_data_service._ema_cache)
        poly_cache_count = 0
        orderbook_count = 0

        if hasattr(self.bot, "polymarket_service"):
            poly_cache_count = len(self.bot.polymarket_service._market_cache)
            orderbook_count = len(self.bot.polymarket_service._order_books)

        embed = create_system_health_embed(
            memory_percent=mem.percent,
            memory_available_mb=mem.available / (1024**2),
            cpu_percent=cpu_load,
            process_memory_mb=proc_mem,
            disk_percent=disk.percent,
            disk_free_gb=disk.free / (1024**3),
            sma_cache_size=sma_count,
            ema_cache_size=ema_count,
            poly_cache_size=poly_cache_count,
            orderbook_size=orderbook_count,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="vtr_list", description="列出虛擬交易室中的所有持倉與歷史紀錄"
    )
    async def vtr_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from database.virtual_trading import get_all_virtual_trades

        rows = get_all_virtual_trades(interaction.user.id)
        if not rows:
            return await interaction.followup.send(
                embed=create_info_embed(
                    title="查無資料", message="📭 虛擬交易室目前無任何紀錄。"
                ),
                ephemeral=True,
            )

        msg = "👻 **【虛擬交易室 (VTR) 紀錄清單】**\n"
        for row in rows[:20]:  # 限制顯示最近 20 筆
            status_emoji = "🟢" if row["status"] == "OPEN" else "⚪"
            pnl_str = f" | PnL: `{row['pnl']:+.2f}`" if row["status"] != "OPEN" else ""
            msg += f"{status_emoji} `ID:{row['id']:02d}` | **{row['symbol']}** | ${row['strike']} {row['opt_type'].upper()} | {row['status']}{pnl_str}\n"

        if len(rows) > 20:
            msg += f"\n*(僅顯示最近 20 筆，總計 {len(rows)} 筆)*"

        await interaction.followup.send(
            embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
        )

    @app_commands.command(
        name="promote_watch", description="將觀察標的提升為實單交易 (WATCH -> TRADE)"
    )
    @app_commands.describe(
        symbol="股票代號",
        opt_type="期權類型 (call/put)",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        price="成交價格",
        qty="口數",
    )
    async def promote_watch(
        self,
        interaction: discord.Interaction,
        symbol: str,
        opt_type: str,
        strike: float,
        expiry: str,
        price: float,
        qty: int,
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        from services.asset_manager import AssetManager

        manager = AssetManager()

        trade_details = {
            "opt_type": opt_type.lower(),
            "strike": strike,
            "expiry": expiry,
            "entry_price": price,
            "quantity": qty,
            "category": "SPEC",
        }

        success = manager.promote_to_trade(interaction.user.id, symbol, trade_details)
        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)

            embed = create_asset_promotion_embed(
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                opt_type=opt_type,
                quantity=qty,
                price=price,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"提升失敗。請確認 `{symbol}` 是否在您的觀察清單中，且參數格式正確。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="settle_trade", description="將實單交易結算為現貨持倉 (TRADE -> HOLDING)"
    )
    @app_commands.describe(
        asset_id="資產 ID (從 /list_trades 獲取)",
        execution_price="最終執行價格 (用於計算平均成本)",
    )
    async def settle_trade(
        self, interaction: discord.Interaction, asset_id: int, execution_price: float
    ):
        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager

        manager = AssetManager()

        success = manager.settle_to_holding(
            interaction.user.id, asset_id, execution_price
        )
        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)

            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功",
                    message=f"✅ **交易結算完成**：資產 ID `{asset_id}` 已轉換為「現貨持倉」。平均成本已更新為 `${execution_price:.2f}`。",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    "結算失敗。請檢查資產 ID 是否正確且屬於「實單交易」狀態。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="add_watch", description="將標的加入自動化量化監控清單 (WATCH)"
    )
    @app_commands.describe(symbol="股票代號 (如 TSLA)", use_llm="是否啟用 AI 輔助分析")
    async def add_watch(
        self, interaction: discord.Interaction, symbol: str, use_llm: bool = True
    ):
        symbol = symbol.upper()
        await interaction.response.defer(ephemeral=True)

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        from services.asset_manager import AssetManager
        from models.asset import Asset, ContextType

        manager = AssetManager()

        asset = Asset(
            user_id=interaction.user.id,
            symbol=symbol,
            context_type=ContextType.WATCH,
            metadata={"use_llm": use_llm},
        )

        success = manager.add_asset(asset)
        if success:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功",
                    message=f"✅ **已加入觀察清單**: `{symbol}` (AI 分析: `{'開啟' if use_llm else '關閉'}`)",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"`{symbol}` 已在您的資產清單中或發生錯誤。", title="系統警告"
                ),
                ephemeral=True,
            )

    @app_commands.command(name="edit_watch", description="修改觀察清單中的標的參數")
    @app_commands.describe(
        symbol="要修改的股票代號", use_llm="更新 AI 輔助分析開關 (選填)"
    )
    async def edit_watch(
        self,
        interaction: discord.Interaction,
        symbol: str,
        use_llm: Optional[bool] = None,
    ):
        symbol = symbol.upper()
        if use_llm is None:
            return await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message=" 請提供要修改的參數 (如 use_llm)。"
                ),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()

        success = manager.update_asset_metadata_by_symbol(
            interaction.user.id, symbol, ContextType.WATCH, {"use_llm": use_llm}
        )

        if success:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功",
                    message=f"✅ **已更新觀察設定**: `{symbol}` (AI 分析: `{'開啟' if use_llm else '關閉'}`)",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"找不到標的 `{symbol}` 或發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(name="add_holding", description="登錄實際現貨持倉 (HOLDING)")
    @app_commands.describe(
        symbol="股票代號", quantity="持有股數", avg_cost="平均買入成本 (USD)"
    )
    async def add_holding(
        self,
        interaction: discord.Interaction,
        symbol: str,
        quantity: float,
        avg_cost: float,
    ):
        symbol = symbol.upper()
        user_id = interaction.user.id
        await interaction.response.defer(ephemeral=True)

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        if quantity <= 0 or avg_cost < 0:
            return await interaction.followup.send(
                embed=create_error_embed(
                    "數量必須大於 0 且成本不能為負數。", title="系統錯誤"
                ),
                ephemeral=True,
            )

        from services.asset_manager import AssetManager
        from models.asset import Asset, ContextType

        manager = AssetManager()

        # 🚀 檢查是否已存在，若存在則更新 (Upsert 邏輯)
        existing_asset = manager.get_asset_by_symbol(
            user_id, symbol, ContextType.HOLDING
        )

        if existing_asset:
            existing_asset.metadata["quantity"] = quantity
            existing_asset.metadata["avg_cost"] = avg_cost
            success = manager.update_asset(existing_asset)
            action_text = "更新"
        else:
            asset = Asset(
                user_id=user_id,
                symbol=symbol,
                context_type=ContextType.HOLDING,
                metadata={"quantity": quantity, "avg_cost": avg_cost},
            )
            success = manager.add_asset(asset)
            action_text = "登錄"

        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(user_id)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功",
                    message=f"✅ **現貨持倉已{action_text}**: `{symbol}` | `{quantity:,.0f}` 股 | 成本 `${avg_cost:,.2f}`",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"{action_text}失敗，請檢查輸入數據或稍後再試。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="edit_holding", description="修改現貨持倉參數 (數量或成本)"
    )
    @app_commands.describe(
        symbol="股票代號",
        quantity="更新後的持有股數 (選填)",
        avg_cost="更新後的平均成本 (選填)",
    )
    async def edit_holding(
        self,
        interaction: discord.Interaction,
        symbol: str,
        quantity: Optional[float] = None,
        avg_cost: Optional[float] = None,
    ):
        symbol = symbol.upper()
        if quantity is None and avg_cost is None:
            return await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message=" 請提供要修改的參數 (數量或成本)。"
                ),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()

        updates = {}
        if quantity is not None:
            updates["quantity"] = quantity
        if avg_cost is not None:
            updates["avg_cost"] = avg_cost

        success = manager.update_asset_metadata_by_symbol(
            interaction.user.id, symbol, ContextType.HOLDING, updates
        )

        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功", message=f"✅ **現貨持倉已更新**: `{symbol}`"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"找不到標的 `{symbol}` 的現貨紀錄或發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="list_holdings", description="列出目前所有現貨持倉、分配比例與即時損益估計"
    )
    async def list_holdings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.HOLDING)

        if not assets:
            return await interaction.followup.send(
                embed=create_info_embed(
                    title="查無資料",
                    message="📭 您目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。",
                ),
                ephemeral=True,
            )

        holdings = []
        for a in assets:
            sym = a.symbol
            quote = await market_data_service.get_quote(sym)
            current_price = quote.get("c", 0.0) if quote else 0.0

            h_data = {
                "id": a.id,
                "symbol": a.symbol,
                "quantity": a.metadata.get("quantity", 0.0),
                "avg_cost": a.metadata.get("avg_cost", 0.0),
                "weighted_delta": a.metadata.get("weighted_delta", 0.0),
                "current_price": current_price,
            }
            holdings.append(h_data)

        ctx = get_full_user_context(user_id)
        from cogs.embed_builder import create_holdings_embed

        embed = create_holdings_embed(holdings, ctx.capital)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="remove_holding", description="從資產清單中移除特定的現貨紀錄"
    )
    @app_commands.describe(symbol="要移除的股票代號")
    async def remove_holding(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        success = manager.delete_asset_by_symbol(
            interaction.user.id, symbol, ContextType.HOLDING
        )

        if success:
            # 🚀 刷新 Greeks
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="移除成功", message=f"✅ **已移除現貨紀錄**: `{symbol}`"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"找不到標的 `{symbol}` 的現貨紀錄。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(name="remove_watch", description="將標的從觀察清單中移除")
    async def remove_watch(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        success = manager.delete_asset_by_symbol(
            interaction.user.id, symbol, ContextType.WATCH
        )

        if success:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="移除成功", message=f"✅ **已移除觀察標的**: `{symbol}`"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"您的觀察清單中找不到 `{symbol}`。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(interaction.user.id, ContextType.WATCH)

        if not assets:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="查無資料", message="📭 您的觀察清單是空的。"
                ),
                ephemeral=True,
            )
            return

        symbols_data = [(a.symbol, a.metadata.get("use_llm", True)) for a in assets]

        from ui.watchlist import WatchlistPagination

        view = WatchlistPagination(symbols_data)
        view.update_buttons()
        await interaction.followup.send(
            embed=view.create_embed(), view=view, ephemeral=True
        )

    @app_commands.command(
        name="list_trades", description="列出目前資料庫中的所有實單持倉與未實現損益"
    )
    async def list_trades(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        from services.trading_service import TradingService

        trading_service = TradingService(self.bot)

        try:
            pnl_data = await trading_service.get_portfolio_pnl(user_id)
        except Exception as e:
            logger.error(f"Failed to calculate PnL: {e}")
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"計算未實現損益時發生錯誤: {e}", title="系統錯誤"
                ),
                ephemeral=True,
            )

        if not pnl_data["trades"]:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="查無資料", message="📭 您目前無持倉紀錄。"
                ),
                ephemeral=True,
            )
            return

        ctx = database.get_full_user_context(user_id)
        from cogs.embed_builder import create_trades_embed

        embed = create_trades_embed(pnl_data, ctx.capital)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從監控管線中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        from services.asset_manager import AssetManager

        manager = AssetManager()
        asset = manager.get_asset_by_id(user_id, trade_id)
        if asset and manager.delete_asset_by_id(user_id, trade_id):
            # 🚀 刷新 Greeks
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(user_id)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="移除成功",
                    message=f"✅ **已刪除紀錄 (ID: {trade_id})**: `{asset.symbol}` 已移除。",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(f"找不到 ID `{trade_id}`。", title="系統錯誤"),
                ephemeral=True,
            )

    @app_commands.command(
        name="transition_sim",
        description="模擬投機部位向 Core Equity/Covered Call 演進",
    )
    @app_commands.describe(
        symbol="標的代號",
        current_option_pnl="目前該部位累計未實現損益 (USD)",
        target_cc_strike="預計轉換後的 Covered Call 履約價",
        target_cc_premium="預計單次收租權利金 (USD)",
    )
    async def transition_sim(
        self,
        interaction: discord.Interaction,
        symbol: str,
        current_option_pnl: float,
        target_cc_strike: float,
        target_cc_premium: float,
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()

        try:
            quote = await market_data_service.get_quote(symbol)
            current_price = quote.get("c", 0.0) if quote else 0.0

            if current_price <= 0:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"無法獲取 `{symbol}` 即時報價。", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

            from market_analysis.pro_management import simulate_pro_transition

            res = simulate_pro_transition(
                current_option_pnl=current_option_pnl,
                current_stock_price=current_price,
                target_cc_strike=target_cc_strike,
                target_cc_premium=target_cc_premium,
            )

            embed = create_transition_simulation_embed(
                symbol=symbol,
                current_price=current_price,
                initial_pnl=res.initial_pnl,
                additional_capital_required=res.additional_capital_required,
                adjusted_cost_basis=res.adjusted_cost_basis,
                target_cc_strike=target_cc_strike,
                target_cc_premium=target_cc_premium,
                projected_aroc=res.projected_aroc,
                capital_efficiency_gain=res.capital_efficiency_gain,
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Transition Simulation failed: {e}")
            await interaction.followup.send(
                embed=create_error_embed(
                    "模擬執行失敗，請檢查輸入數據。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="notif_settings",
        description="自訂通知偏好設定中心 (開啟或關閉背景定時報告與即時風控警報)",
    )
    async def notif_settings(self, interaction: discord.Interaction):
        """喚起自訂通知設定面板"""
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        view = NotificationSettingsView(user_id)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ============================================================================
# 🔔 使用者自訂通知開關 UI (Notification Toggles UI)
# ============================================================================

SCHEDULED_LABELS = {
    "watchlist_heartbeat_alignment": "📊 自選心跳與委託對齊",
    "pre_market_briefing": "🌅 盤前綜合宏觀與自選股報告",
    "intraday_decision_scan": "⚡ 盤中量化掃描與執行指南",
    "post_market_intelligence": "📋 盤後綜合風險與 AI 策略報告",
    "weekly_vtr_report": "📅 每週 VTR 績效週報",
}

REALTIME_LABELS = {
    "profit_lock_alert": "💰 期權實單利潤鎖定警報",
    "gamma_fragility_alert": "⚠️ 組合 Gamma 脆弱性警報",
    "option_defense_alert": "🛡️ 期權轉倉防禦與結算警報",
    "ddp_cheap_vol_alert": "🌌 雙擊與便宜波動率預警",
    "volatility_risk_alert": "🌪️ 波動率與重大事件對沖警報",
}

POLYMARKET_SETTINGS_LABELS = {
    "polymarket_whale_alert": (
        "🐳 巨鯨交易異動警報",
        "切換巨鯨交易異動警報開啟/關閉狀態",
        None,
    ),
    "polymarket_threshold": (
        "🐋 巨鯨監控門檻",
        "Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
        "輸入大於等於 0 的金額",
    ),
    "polymarket_use_llm": (
        "🧠 Polymarket AI 分析",
        "Polymarket 交易是否使用 AI 分析總結",
        None,
    ),
    "polymarket_slippage": (
        "🌊 Polymarket 滑價門檻",
        "Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
        "輸入 0.1 - 10.0 之間的百分比",
    ),
}


class NotificationSettingsModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        key: str,
        label: str,
        current_value: float,
        placeholder: str,
        view: discord.ui.View,
    ):
        super().__init__(title=f"設定 - {label}")
        self.user_id = user_id
        self.key = key
        self.label = label
        self.view = view

        self.input_field: discord.ui.TextInput = discord.ui.TextInput(
            label=f"請輸入新的數值 (目前: {current_value})",
            placeholder=placeholder,
            default=str(current_value),
            required=True,
            max_length=50,
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        value_str = self.input_field.value.strip()
        try:
            val = float(value_str)
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "輸入無效，必須是有效的數字或小數。", title="輸入錯誤"
                ),
                ephemeral=True,
            )
            return

        # 數值邊界驗證與防錯
        if self.key == "polymarket_threshold":
            if val < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("金額不能為負數", title="驗證失敗"),
                    ephemeral=True,
                )
                return
        elif self.key == "polymarket_slippage":
            if not (0.1 <= val <= 10.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "滑價門檻需介於 0.1% 至 10.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return

        # 更新資料庫
        success = database.upsert_user_config(self.user_id, **{self.key: val})
        if not success:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "設定更新失敗，請稍後再試。", title="系統錯誤"
                ),
                ephemeral=True,
            )
            return

        # 刷新檢視
        if (
            self.view is not None
            and hasattr(self.view, "refresh_items")
            and hasattr(self.view, "build_embed")
        ):
            getattr(self.view, "refresh_items")()
            embed = getattr(self.view, "build_embed")()
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message="✅ 設定已成功更新！"
                ),
                ephemeral=True,
            )


class NotificationSettingsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        # 1. 定時與掃描背景通知下拉選單
        scheduled_options = []
        for key, label in SCHEDULED_LABELS.items():
            state_emoji = "🟢" if settings.get(key, True) else "🔴"
            scheduled_options.append(
                discord.SelectOption(
                    label=f"{state_emoji} {label}",
                    value=key,
                    description="點擊切換開啟/關閉狀態",
                )
            )
        scheduled_select = discord.ui.Select(
            placeholder="⚙️ 設定 定時與掃描背景通知...",
            options=scheduled_options,
            custom_id="select_scheduled",
            row=0,
        )
        scheduled_select.callback = self.on_select_callback
        self.add_item(scheduled_select)

        # 2. 即時風險與事件警報下拉選單
        realtime_options = []
        for key, label in REALTIME_LABELS.items():
            state_emoji = "🟢" if settings.get(key, True) else "🔴"
            realtime_options.append(
                discord.SelectOption(
                    label=f"{state_emoji} {label}",
                    value=key,
                    description="點擊切換開啟/關閉狀態",
                )
            )
        realtime_select = discord.ui.Select(
            placeholder="🚨 設定 即時風險與事件警報...",
            options=realtime_options,
            custom_id="select_realtime",
            row=1,
        )
        realtime_select.callback = self.on_select_callback
        self.add_item(realtime_select)

        # 3. Polymarket 巨鯨與 AI 監控設定下拉選單
        polymarket_options = []

        # (a) 巨鯨交易異動警報
        whale_alert_enabled = settings.get("polymarket_whale_alert", True)
        whale_alert_emoji = "🟢" if whale_alert_enabled else "🔴"
        polymarket_options.append(
            discord.SelectOption(
                label="🐳 巨鯨交易異動警報",
                value="polymarket_whale_alert",
                description=f"目前: {whale_alert_emoji} {'開啟' if whale_alert_enabled else '關閉'} | 切換開關狀態"[
                    :100
                ],
            )
        )

        # (b) 巨鯨監控門檻
        threshold_val = ctx.polymarket_threshold
        threshold_emoji = "🟢" if threshold_val > 0 else "🔴"
        threshold_display = f"${threshold_val:,.0f}" if threshold_val > 0 else "關閉"
        polymarket_options.append(
            discord.SelectOption(
                label="🐋 巨鯨監控門檻",
                value="polymarket_threshold",
                description=f"目前: {threshold_emoji} {threshold_display} | 設定門檻金額"[
                    :100
                ],
            )
        )

        # (c) AI 分析
        use_llm_val = ctx.polymarket_use_llm
        use_llm_emoji = "🟢" if use_llm_val else "🔴"
        polymarket_options.append(
            discord.SelectOption(
                label="🧠 Polymarket AI 分析",
                value="polymarket_use_llm",
                description=f"目前: {use_llm_emoji} {'開啟' if use_llm_val else '關閉'} | 切換開關狀態"[
                    :100
                ],
            )
        )

        # (d) 滑價門檻
        slippage_val = ctx.polymarket_slippage
        polymarket_options.append(
            discord.SelectOption(
                label="🌊 Polymarket 滑價門檻",
                value="polymarket_slippage",
                description=f"目前: {slippage_val}% | 設定判定滑價門檻"[:100],
            )
        )

        polymarket_select = discord.ui.Select(
            placeholder="🐳 設定 Polymarket 巨鯨與 AI 監控...",
            options=polymarket_options,
            custom_id="select_polymarket",
            row=2,
        )
        polymarket_select.callback = self.on_select_callback
        self.add_item(polymarket_select)

        # 4. 按鈕
        btn_enable_all = discord.ui.Button(
            label="⚡ 全部開啟",
            style=discord.ButtonStyle.green,
            custom_id="btn_enable_all",
            row=3,
        )
        btn_enable_all.callback = self.on_enable_all
        self.add_item(btn_enable_all)

        btn_disable_all = discord.ui.Button(
            label="💤 全部關閉",
            style=discord.ButtonStyle.red,
            custom_id="btn_disable_all",
            row=3,
        )
        btn_disable_all.callback = self.on_disable_all
        self.add_item(btn_disable_all)

    async def on_select_callback(self, interaction: discord.Interaction):
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        ctx = database.get_full_user_context(self.user_id)

        # 1. 處理 Polymarket 的非開關設定 (Modal)
        if key in ["polymarket_threshold", "polymarket_slippage"]:
            current_val = getattr(ctx, key, 0.0)
            label, desc, placeholder = POLYMARKET_SETTINGS_LABELS[key]
            modal = NotificationSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=current_val,
                placeholder=placeholder or "",
                view=self,
            )
            await interaction.response.send_modal(modal)
            return

        # 2. 處理 Polymarket AI 分析 (User settings boolean toggle)
        elif key == "polymarket_use_llm":
            current_val = getattr(ctx, key, False)
            new_val = not current_val
            database.upsert_user_config(self.user_id, **{key: new_val})

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            return

        # 3. 處理一般的通知 ON/OFF 開關
        else:
            settings = database.get_user_notification_settings(self.user_id)
            new_state = not settings.get(key, True)
            database.set_user_notification_setting(self.user_id, key, new_state)

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_enable_all(self, interaction: discord.Interaction):
        database.set_all_user_notification_settings(self.user_id, True)
        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_disable_all(self, interaction: discord.Interaction):
        database.set_all_user_notification_settings(self.user_id, False)
        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        scheduled_list = []
        for key, label in SCHEDULED_LABELS.items():
            status = "🟢 開啟" if settings.get(key, True) else "🔴 關閉"
            scheduled_list.append(f"* {label}: **{status}**")

        realtime_list = []
        for key, label in REALTIME_LABELS.items():
            status = "🟢 開啟" if settings.get(key, True) else "🔴 關閉"
            realtime_list.append(f"* {label}: **{status}**")

        polymarket_list = [
            f"* 🐳 巨鯨交易異動警報: **{'🟢 開啟' if settings.get('polymarket_whale_alert', True) else '🔴 關閉'}**",
            f"* 🐋 巨鯨監控門檻金額: **{'🟢 $' + f'{ctx.polymarket_threshold:,.0f}' if ctx.polymarket_threshold > 0 else '🔴 關閉'}**",
            f"* 🧠 Polymarket AI 深度分析: **{'🟢 開啟' if ctx.polymarket_use_llm else '🔴 關閉'}**",
            f"* 🌊 巨鯨判定滑價門檻: **`{ctx.polymarket_slippage}%`**",
        ]

        return create_notification_settings_embed(
            scheduled_list=scheduled_list,
            realtime_list=realtime_list,
            polymarket_list=polymarket_list,
        )


# ============================================================================
# ⚙️ 使用者全域參數設定 UI (Interactive Account Settings UI)
# ============================================================================

SETTINGS_LABELS = {
    "capital": ("💰 總資金", "更新帳戶總資金 (USD)", "輸入資金數字，必須大於 0"),
    "risk_limit": (
        "🛡️ 基準風險上限 %",
        "更新基準風險上限 % (1.0 - 50.0)",
        "輸入 1.0 - 50.0 之間的數值",
    ),
    "enable_vtr": (
        "👻 虛擬交易室 (VTR)",
        "是否啟用虛擬交易室 GhostTrader 自動建倉",
        None,
    ),
    "enable_psq_watchlist": (
        "⚡ PowerSqueeze 追蹤",
        "是否對自選股開啟 PowerSqueeze 戰情追蹤",
        None,
    ),
    "enable_local_tunnel": (
        "🛜 本地 Tunnel 呼叫",
        "是否允許呼叫本地 Tunnel/Edge Scraper（關閉時將不做任何 Tunnel I/O）",
        None,
    ),
    "monthly_expense": (
        "💸 每月支出預算",
        "每月生存支出預算 (USD, 用於財務跑道分析)",
        "輸入大於等於 0 的預算",
    ),
    "tax_reserve_rate": (
        "🏦 稅務預留比例",
        "稅務預留比例 (0.0 - 1.0)",
        "輸入 0.0 - 1.0 之間的數值",
    ),
    "cash_reserve": (
        "💰 現金儲備金額",
        "現金儲備金額 (USD, 用於生存天數計算)",
        "輸入大於等於 0 的現金儲備",
    ),
}


class AccountSettingsModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        key: str,
        label: str,
        current_value: float,
        placeholder: str,
        view: discord.ui.View,
    ):
        super().__init__(title=f"設定 - {label}")
        self.user_id = user_id
        self.key = key
        self.label = label
        self.view = view

        self.input_field: discord.ui.TextInput = discord.ui.TextInput(
            label=f"請輸入新的數值 (目前: {current_value})",
            placeholder=placeholder,
            default=str(current_value),
            required=True,
            max_length=50,
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        value_str = self.input_field.value.strip()
        try:
            val = float(value_str)
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "輸入無效，必須是有效的數字或小數。", title="輸入錯誤"
                ),
                ephemeral=True,
            )
            return

        # 數值邊界驗證與防錯
        if self.key == "capital":
            if val <= 0:
                await interaction.response.send_message(
                    embed=create_error_embed("總資金必須大於 0", title="驗證失敗"),
                    ephemeral=True,
                )
                return
        elif self.key == "risk_limit":
            if not (1.0 <= val <= 50.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "風險限制需介於 1.0% 至 50.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return
        elif self.key in ["polymarket_threshold", "monthly_expense", "cash_reserve"]:
            if val < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("金額不能為負數", title="驗證失敗"),
                    ephemeral=True,
                )
                return
        elif self.key == "polymarket_slippage":
            if not (0.1 <= val <= 10.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "滑價門檻需介於 0.1% 至 10.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return
        elif self.key == "tax_reserve_rate":
            # 支援百分比輸入 (例如輸入 20 轉換成 0.20)
            if val > 1.0:
                val = val / 100.0
            if not (0.0 <= val <= 1.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "稅務比例需介於 0.0 與 1.0 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return

        # 更新資料庫
        success = database.upsert_user_config(self.user_id, **{self.key: val})
        if not success:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "設定更新失敗，請稍後再試。", title="系統錯誤"
                ),
                ephemeral=True,
            )
            return

        # 刷新檢視
        if (
            self.view is not None
            and hasattr(self.view, "refresh_items")
            and hasattr(self.view, "build_embed")
        ):
            getattr(self.view, "refresh_items")()
            embed = getattr(self.view, "build_embed")()
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message="✅ 設定已成功更新！"
                ),
                ephemeral=True,
            )


class AccountSettingsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        ctx = database.get_full_user_context(self.user_id)

        # 動態生成下拉選單選項
        options = []
        for key, (label, desc, placeholder) in SETTINGS_LABELS.items():
            # 獲取當前設定值
            raw_val = getattr(ctx, key, None)

            # 美化展示格式
            if isinstance(raw_val, bool):
                val_display = "開啟" if raw_val else "關閉"
            elif key == "capital":
                val_display = f"${raw_val:,.2f}"
            elif key == "risk_limit":
                val_display = f"{raw_val}%"
            elif key in ["polymarket_threshold", "monthly_expense", "cash_reserve"]:
                val_display = f"${raw_val:,.0f}" if raw_val > 0 else "關閉/未設定"
            elif key == "polymarket_slippage":
                val_display = f"{raw_val}%"
            elif key == "tax_reserve_rate":
                val_display = f"{raw_val:.1%}"
            else:
                val_display = str(raw_val)

            options.append(
                discord.SelectOption(
                    label=label,
                    value=key,
                    description=f"目前: {val_display} | {desc}"[:100],
                )
            )

        select = discord.ui.Select(
            placeholder="⚙️ 請選擇要配置的帳戶全域參數...",
            options=options,
            custom_id="select_account_settings",
            row=0,
        )
        select.callback = self.on_select_callback
        self.add_item(select)

    async def on_select_callback(self, interaction: discord.Interaction):
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        ctx = database.get_full_user_context(self.user_id)

        # 針對布林值，直接切換狀態
        if key in [
            "enable_vtr",
            "enable_psq_watchlist",
            "enable_local_tunnel",
            "polymarket_use_llm",
        ]:
            current_val = getattr(ctx, key, False)
            new_val = not current_val
            database.upsert_user_config(self.user_id, **{key: new_val})

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # 針對數值類型，彈出 Modal 視窗
            current_val = getattr(ctx, key, 0.0)
            label, desc, placeholder = SETTINGS_LABELS[key]
            modal = AccountSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=current_val,
                placeholder=placeholder or "",
                view=self,
            )
            await interaction.response.send_modal(modal)

    def build_embed(self) -> discord.Embed:
        ctx = database.get_full_user_context(self.user_id)

        # 分類展示當前設定
        basic_settings = [
            f"💰 **總資金**: `${ctx.capital:,.2f}`",
            f"🛡️ **基準風險上限**: `{ctx.risk_limit}%`",
            f"👻 **虛擬交易室 (VTR) 跟單**: `{'🟢 開啟' if ctx.enable_vtr else '🔴 關閉'}`",
            f"⚡ **PowerSqueeze 追蹤**: `{'🟢 開啟' if ctx.enable_psq_watchlist else '🔴 關閉'}`",
            f"🛜 **本地 Tunnel 呼叫**: `{'🟢 開啟' if ctx.enable_local_tunnel else '🔴 關閉'}`",
        ]

        runway_settings = [
            f"💸 **每月生存支出預算**: `${ctx.monthly_expense:,.0f}`",
            f"🏦 **稅務預留比例**: `{ctx.tax_reserve_rate:.1%}`",
            f"💰 **現金儲備金額**: `${ctx.cash_reserve:,.0f}`",
        ]

        return create_account_settings_embed(
            basic_settings=basic_settings, runway_settings=runway_settings
        )


async def setup(bot):
    await bot.add_cog(TerminalCog(bot))
