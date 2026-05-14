import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import database
import market_math
from services import market_data_service
from cogs.embed_builder import create_scan_embed
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

    @app_commands.command(
        name="settings", description="配置帳戶全域參數 (資金、風險與專業營運指標)"
    )
    @app_commands.describe(
        capital="更新帳戶總資金 (USD)",
        risk_limit="更新基準風險上限 % (1.0 - 50.0)",
        alert_mode="期權警報模式: OFF(關閉), ALL(所有訊號), PORTFOLIO_ONLY(僅限持倉標的)",
        enable_vtr="是否啟用虛擬交易室 GhostTrader 自動建倉",
        enable_psq_watchlist="是否對 watchlist 開啟 PowerSqueeze 戰情追蹤",
        enable_analyst_agent="是否啟用 Wall Street Analyst Agent 每日推播",
        polymarket_threshold="Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
        polymarket_use_llm="Polymarket 交易是否使用 AI 分析總結",
        polymarket_slippage="Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
        monthly_expense="每月生存支出預算 (USD, 用於財務跑道分析)",
        tax_reserve_rate="稅務預留比例 (0.0 - 1.0)",
        cash_reserve="現金儲備金額 (USD, 用於生存天數計算)",
    )
    @app_commands.choices(
        alert_mode=[
            app_commands.Choice(name="OFF (關閉)", value=0),
            app_commands.Choice(name="ALL (所有掃描訊號)", value=1),
            app_commands.Choice(name="PORTFOLIO_ONLY (僅限持倉標的)", value=2),
        ]
    )
    async def update_settings(
        self,
        interaction: discord.Interaction,
        capital: Optional[float] = None,
        risk_limit: Optional[float] = None,
        alert_mode: Optional[int] = None,
        enable_vtr: Optional[bool] = None,
        enable_psq_watchlist: Optional[bool] = None,
        enable_analyst_agent: Optional[bool] = None,
        polymarket_threshold: Optional[float] = None,
        polymarket_use_llm: Optional[bool] = None,
        polymarket_slippage: Optional[float] = None,
        monthly_expense: Optional[float] = None,
        tax_reserve_rate: Optional[float] = None,
        cash_reserve: Optional[float] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        updates = []
        kwargs = {}

        if capital is not None:
            if capital > 0:
                kwargs["capital"] = capital
                updates.append(f"💰 總資金: `${capital:,.2f}`")
            else:
                return await interaction.followup.send(
                    "❌ 資金必須大於 0", ephemeral=True
                )

        if risk_limit is not None:
            if 1.0 <= risk_limit <= 50.0:
                kwargs["risk_limit"] = risk_limit
                updates.append(f"🛡️ 風險限制: `{risk_limit}%`")
            else:
                return await interaction.followup.send(
                    "❌ 風險限制需介於 1.0% 至 50.0% 之間", ephemeral=True
                )

        if alert_mode is not None:
            kwargs["option_alert_mode"] = alert_mode
            mode_names = {
                0: "OFF (關閉)",
                1: "ALL (所有掃描訊號)",
                2: "PORTFOLIO_ONLY (僅限持倉標的)",
            }
            updates.append(f"🔔 期權警報模式: `{mode_names[alert_mode]}`")

        if enable_vtr is not None:
            kwargs["enable_vtr"] = enable_vtr
            updates.append(f"👻 虛擬交易室 (VTR): `{'開啟' if enable_vtr else '關閉'}`")

        if enable_psq_watchlist is not None:
            kwargs["enable_psq_watchlist"] = enable_psq_watchlist
            updates.append(
                f"⚡ PowerSqueeze 追蹤: `{'開啟' if enable_psq_watchlist else '關閉'}`"
            )

        if enable_analyst_agent is not None:
            kwargs["enable_analyst_agent"] = enable_analyst_agent
            updates.append(
                f"🤖 Analyst Agent 每日推播: `{'開啟' if enable_analyst_agent else '關閉'}`"
            )

        if polymarket_threshold is not None:
            kwargs["polymarket_threshold"] = polymarket_threshold
            status = (
                f"`${polymarket_threshold:,.0f}`"
                if polymarket_threshold > 0
                else "`關閉`"
            )
            updates.append(f"🐋 Polymarket 監控: {status}")

        if polymarket_use_llm is not None:
            kwargs["polymarket_use_llm"] = polymarket_use_llm
            updates.append(
                f"🧠 Polymarket AI 分析: `{'開啟' if polymarket_use_llm else '關閉'}`"
            )

        if polymarket_slippage is not None:
            if 0.1 <= polymarket_slippage <= 10.0:
                kwargs["polymarket_slippage"] = polymarket_slippage
                updates.append(f"🌊 Polymarket 滑價門檻: `{polymarket_slippage}%`")
            else:
                return await interaction.followup.send(
                    "❌ 滑價門檻需介於 0.1% 至 10.0% 之間", ephemeral=True
                )

        if monthly_expense is not None:
            if monthly_expense >= 0:
                kwargs["monthly_expense"] = monthly_expense
                updates.append(f"💸 每月支出預算: `${monthly_expense:,.0f}`")
            else:
                return await interaction.followup.send(
                    "❌ 支出預算不能為負數", ephemeral=True
                )

        if tax_reserve_rate is not None:
            if 0.0 <= tax_reserve_rate <= 1.0:
                kwargs["tax_reserve_rate"] = tax_reserve_rate
                updates.append(f"🏦 稅務預留比例: `{tax_reserve_rate:.1%}`")
            else:
                return await interaction.followup.send(
                    "❌ 稅務比例需介於 0.0 與 1.0 之間", ephemeral=True
                )

        if cash_reserve is not None:
            if cash_reserve >= 0:
                kwargs["cash_reserve"] = cash_reserve
                updates.append(f"💰 現金儲備: `${cash_reserve:,.0f}`")
            else:
                return await interaction.followup.send(
                    "❌ 現金儲備不能為負數", ephemeral=True
                )

        if not kwargs:
            return await interaction.followup.send(
                "請至少選擇並輸入一個要修改的參數。", ephemeral=True
            )

        success = database.upsert_user_config(user_id, **kwargs)
        if not success:
            return await interaction.followup.send(
                "❌ 設定失敗，請稍後再試。", ephemeral=True
            )

        msg = "✅ **帳戶設定已更新**：\n" + "\n".join(updates)
        await interaction.followup.send(msg, ephemeral=True)

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
                "⚠️ 請先使用 `/settings` 配置您的每月支出 (monthly_expense)，才能計算跑道。",
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

        embed = discord.Embed(
            title="🏁 財務生存跑道分析 (zh-tw)",
            color=discord.Color.green()
            if runway_days > 180
            else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        runway_str = (
            f"`{runway_days:,.1f}` 天"
            if runway_days < 9999
            else "♾️ 無限 (收益已覆蓋支出)"
        )
        ext_runway_str = (
            f"`{extended_runway:,.1f}` 天" if extended_runway < 9999 else "♾️ 無限"
        )

        embed.add_field(
            name="💰 現金儲備 (Cash)", value=f"`${ctx.cash_reserve:,.2f}`", inline=True
        )
        embed.add_field(
            name="📉 每月支出", value=f"`${ctx.monthly_expense:,.2f}`", inline=True
        )
        embed.add_field(
            name="💸 每日 Theta 收益",
            value=f"`+${ctx.total_theta:,.2f}/day`",
            inline=True,
        )

        embed.add_field(name="⌛ 核心生存跑道", value=f"**{runway_str}**", inline=False)

        if backup_liquidity > 0:
            embed.add_field(
                name="🛡️ 備用流動性 (HOLDING 淨值)",
                value=f"`${total_holding_value:,.2f}` (折價後: `${backup_liquidity:,.2f}`)\n預計可將跑道延長至: **{ext_runway_str}**",
                inline=False,
            )

        ratio = (
            (ctx.total_theta * 30) / ctx.monthly_expense
            if ctx.monthly_expense > 0
            else 0
        )
        embed.add_field(
            name="📊 收益支出比 (Theta/Expense)", value=f"`{ratio:.2%}`", inline=True
        )

        embed.set_footer(text="Nexus Risk Engine | 跑道計算含 20% 流動性折價")
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
        stock_cost="持有現股平均成本 (可選)",
        category="部位類別 (SPECULATIVE/HEDGE)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="SPECULATIVE", value="SPECULATIVE"),
            app_commands.Choice(name="HEDGE", value="HEDGE"),
        ]
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
        stock_cost: float = 0.0,
        category: app_commands.Choice[str] = None,
    ):
        symbol = symbol.upper()
        user_id = interaction.user.id
        trade_category = category.value if category else "SPECULATIVE"
        await interaction.response.defer(ephemeral=True)

        # 🚀 驗證標的合法性
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                f"❌ **無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                ephemeral=True,
            )

        # 🛡️ Defensive Programming: Validate Expiry Date Format
        from datetime import datetime

        try:
            # Only capture the first 10 characters (YYYY-MM-DD) to prevent trailing argument capture
            expiry_clean = expiry.split(" ")[0]
            datetime.strptime(expiry_clean, "%Y-%m-%d")
            expiry = expiry_clean  # Standardized format
        except Exception:
            await interaction.followup.send(
                f"❌ **日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。",
                ephemeral=True,
            )
            return

        if not category and symbol == "SPY":
            if quantity < 0 or (opt_type.value == "put" and quantity > 0):
                trade_category = "HEDGE"

        try:
            from services.asset_manager import AssetManager
            from models.asset import Asset, ContextType

            manager = AssetManager()

            trade_details = {
                "opt_type": opt_type.value,
                "strike": strike,
                "expiry": expiry,
                "entry_price": entry_price,
                "quantity": quantity,
                "category": trade_category,
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
                    f"✅ **新增交易成功**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ 新增交易失敗，請稍後再試。", ephemeral=True
                )

        except Exception as e:
            logger.error(f"Add trade failed: {e}")
            await interaction.followup.send(f"❌ **發生錯誤**: {e}", ephemeral=True)

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
                    f"❌ **日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。",
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
                "請提供至少一個要修改的參數。", ephemeral=True
            )

        success = manager.update_asset_metadata(interaction.user.id, trade_id, updates)
        if success:
            from market_analysis.portfolio import refresh_portfolio_greeks

            await refresh_portfolio_greeks(interaction.user.id)
            await interaction.followup.send(
                f"✅ **交易紀錄已更新 (ID: {trade_id})**", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 找不到交易 ID `{trade_id}` 或發生錯誤。", ephemeral=True
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
                f"❌ **無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
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
            await interaction.followup.send(f"📊 目前 `{symbol}` 查無有效訊號。")

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
            await interaction.followup.send(f"❌ 無法獲取績效數據: {e}", ephemeral=True)

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

        embed = discord.Embed(
            title="🖥️ Nexus Seeker 系統健康診斷",
            color=discord.Color.green()
            if (mem.percent < 80 and disk.percent < 85)
            else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="VPS 記憶體",
            value=f"`{mem.percent}%` (可用: {mem.available / (1024**2):.1f}MB)",
            inline=True,
        )
        embed.add_field(name="CPU 負載", value=f"`{cpu_load}%`", inline=True)
        embed.add_field(
            name="程序占用 (RSS)", value=f"`{proc_mem:.1f} MB`", inline=True
        )
        embed.add_field(
            name="💿 硬碟空間",
            value=f"`{disk.percent}%` (可用: {disk.free / (1024**3):.1f}GB)",
            inline=True,
        )

        cache_info = (
            f"• SMA/EMA Cache: `{sma_count}/{ema_count}`\n"
            f"• Poly Markets: `{poly_cache_count}`\n"
            f"• OrderBooks: `{orderbook_count}`"
        )
        embed.add_field(
            name="📦 快取統計 (LRU/Bounded)", value=cache_info, inline=False
        )

        health_status = "✅ 狀態優良"
        if mem.percent > 85 or disk.percent > 85:
            health_status = "⚠️ **資源吃緊**"
            if mem.percent > 85:
                health_status += " (記憶體閾值已達)"
            if disk.percent > 85:
                health_status += " (磁碟空間不足)"

        if mem.percent > 95 or disk.percent > 95:
            health_status = "🆘 **極度危險**"
            if mem.percent > 95:
                health_status += " (OOM 警告)"
            if disk.percent > 95:
                health_status += " (磁碟即將滿載)"

        embed.add_field(name="🩺 健康評級", value=health_status, inline=False)
        embed.set_footer(text="Argo Optimization Engine | Low-RAM VPS Edition")

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
                "📭 虛擬交易室目前無任何紀錄。", ephemeral=True
            )

        msg = "👻 **【虛擬交易室 (VTR) 紀錄清單】**\n"
        for row in rows[:20]:  # 限制顯示最近 20 筆
            status_emoji = "🟢" if row["status"] == "OPEN" else "⚪"
            pnl_str = f" | PnL: `{row['pnl']:+.2f}`" if row["status"] != "OPEN" else ""
            msg += f"{status_emoji} `ID:{row['id']:02d}` | **{row['symbol']}** | ${row['strike']} {row['opt_type'].upper()} | {row['status']}{pnl_str}\n"

        if len(rows) > 20:
            msg += f"\n*(僅顯示最近 20 筆，總計 {len(rows)} 筆)*"

        await interaction.followup.send(msg, ephemeral=True)

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
                f"❌ **無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
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

            embed = discord.Embed(
                title="🌌 Nexus | 資產晉升成功",
                description=f"標的 **{symbol}** 已從「觀察」提升為「實單交易」。",
                color=0x00FF7F,
            )
            embed.add_field(
                name="合約細節",
                value=f"`{expiry}` ${strike} {opt_type.upper()}\n數量: `{qty}` 口 | 價格: `${price}`",
            )
            embed.set_footer(text="Unified Asset Lifecycle v1.0")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ 提升失敗。請確認 `{symbol}` 是否在您的觀察清單中，且參數格式正確。",
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
                f"✅ **交易結算完成**：資產 ID `{asset_id}` 已轉換為「現貨持倉」。平均成本已更新為 `${execution_price:.2f}`。",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ 結算失敗。請檢查資產 ID 是否正確且屬於「實單交易」狀態。",
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
                f"❌ **無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
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
                f"✅ **已加入觀察清單**: `{symbol}` (AI 分析: `{'開啟' if use_llm else '關閉'}`)",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ `{symbol}` 已在您的資產清單中或發生錯誤。", ephemeral=True
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
                "請提供要修改的參數 (如 use_llm)。", ephemeral=True
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
                f"✅ **已更新觀察設定**: `{symbol}` (AI 分析: `{'開啟' if use_llm else '關閉'}`)",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ 找不到標的 `{symbol}` 或發生錯誤。", ephemeral=True
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
                f"❌ **無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                ephemeral=True,
            )

        if quantity <= 0 or avg_cost < 0:
            return await interaction.followup.send(
                "❌ 數量必須大於 0 且成本不能為負數。", ephemeral=True
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
                f"✅ **現貨持倉已{action_text}**: `{symbol}` | `{quantity:,.0f}` 股 | 成本 `${avg_cost:,.2f}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ {action_text}失敗，請檢查輸入數據或稍後再試。", ephemeral=True
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
                "請提供要修改的參數 (數量或成本)。", ephemeral=True
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
                f"✅ **現貨持倉已更新**: `{symbol}`", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 找不到標的 `{symbol}` 的現貨紀錄或發生錯誤。", ephemeral=True
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
                "📭 您目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。",
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
                f"🗑️ **已移除現貨紀錄**: `{symbol}`", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 找不到標的 `{symbol}` 的現貨紀錄。", ephemeral=True
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
                f"🗑️ **已移除觀察標的**: `{symbol}`", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 您的觀察清單中找不到 `{symbol}`。", ephemeral=True
            )

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        assets = manager.get_assets(interaction.user.id, ContextType.WATCH)

        if not assets:
            await interaction.followup.send("📭 您的觀察清單是空的。", ephemeral=True)
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
                f"❌ 計算未實現損益時發生錯誤: {e}", ephemeral=True
            )

        if not pnl_data["trades"]:
            await interaction.followup.send("📭 您目前無持倉紀錄。", ephemeral=True)
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
                f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{asset.symbol}` 已移除。",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ 找不到 ID `{trade_id}`。", ephemeral=True
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
                    f"❌ 無法獲取 `{symbol}` 即時報價。", ephemeral=True
                )

            from market_analysis.pro_management import simulate_pro_transition

            res = simulate_pro_transition(
                current_option_pnl=current_option_pnl,
                current_stock_price=current_price,
                target_cc_strike=target_cc_strike,
                target_cc_premium=target_cc_premium,
            )

            embed = discord.Embed(
                title=f"🔄 戰略轉軌模擬 (演進) | {symbol}",
                description=f"模擬將 `{symbol}` 投機期權部位演進為 **核心現股 + 備兌買權 (Covered Call)** 模型。",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )

            embed.add_field(
                name="現價 (Price)", value=f"`${current_price:.2f}`", inline=True
            )
            embed.add_field(
                name="期權獲利 (Option PnL)",
                value=f"`${res.initial_pnl:,.2f}`",
                inline=True,
            )

            roadmap = (
                f"1. **執行動作**：平倉現有 DITM 部位，回收收益。\n"
                f"2. **購入現股**：以 `${current_price:.2f}` 購入 100 股。\n"
                f"3. **追加資本**：需額外投入 **`${res.additional_capital_required:,.2f}`**。\n"
                f"4. **成本調整**：調整後每股成本為 **`${res.adjusted_cost_basis:.2f}`**。\n"
                f"5. **建立 CC**：賣出 `${target_cc_strike}` Call，收取 `${target_cc_premium:.2f}` 權利金。"
            )
            embed.add_field(
                name="🚀 資本重分配路線圖 (Roadmap)", value=roadmap, inline=False
            )

            efficiency = (
                f"• **預期年化回報 (AROC)**：`{res.projected_aroc:.1f}%` "
                f"{'✅ 符合 15% 門檻' if res.projected_aroc >= 15 else '⚠️ 低於效率門檻'}\n"
                f"• **單次收租殖利率**：`{res.capital_efficiency_gain:.2f}%`"
            )
            embed.add_field(name="📊 資本效率評估", value=efficiency, inline=False)

            embed.set_footer(text="戰略轉軌引擎 v1.0 | 專業營運模式")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Transition Simulation failed: {e}")
            await interaction.followup.send(
                "❌ 模擬執行失敗，請檢查輸入數據。", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(TerminalCog(bot))
