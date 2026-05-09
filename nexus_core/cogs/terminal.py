import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Optional

import database
import market_math
from services import market_data_service, news_service, llm_service
from market_analysis.ghost_trader import GhostTrader
from cogs.embed_builder import create_scan_embed, build_vtr_stats_embed
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

    @app_commands.command(name="settings", description="配置帳戶全域參數 (資金、風險與專業營運指標)")
    @app_commands.describe(
        capital="更新帳戶總資金 (USD)",
        risk_limit="更新基準風險上限 % (1.0 - 50.0)",
        enable_option_alerts="是否接收選項策略推播",
        enable_vtr="是否啟用虛擬交易室 GhostTrader 自動建倉",
        enable_psq_watchlist="是否對 watchlist 開啟 PowerSqueeze 戰情追蹤",
        enable_analyst_agent="是否啟用 Wall Street Analyst Agent 每日推播",
        polymarket_threshold="Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
        polymarket_use_llm="Polymarket 交易是否使用 AI 分析總結",
        polymarket_slippage="Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
        monthly_expense="每月生存支出預算 (USD, 用於財務跑道分析)",
        tax_reserve_rate="稅務預留比例 (0.0 - 1.0)",
        cash_reserve="現金儲備金額 (USD, 用於生存天數計算)"
    )
    async def update_settings(
        self, 
        interaction: discord.Interaction, 
        capital: Optional[float] = None, 
        risk_limit: Optional[float] = None,
        enable_option_alerts: Optional[bool] = None,
        enable_vtr: Optional[bool] = None,
        enable_psq_watchlist: Optional[bool] = None,
        enable_analyst_agent: Optional[bool] = None,
        polymarket_threshold: Optional[float] = None,
        polymarket_use_llm: Optional[bool] = None,
        polymarket_slippage: Optional[float] = None,
        monthly_expense: Optional[float] = None,
        tax_reserve_rate: Optional[float] = None,
        cash_reserve: Optional[float] = None
    ):
        user_id = interaction.user.id
        updates = []
        kwargs = {}

        if capital is not None:
            if capital > 0:
                kwargs['capital'] = capital
                updates.append(f"💰 總資金: `${capital:,.2f}`")
            else:
                return await interaction.response.send_message("❌ 資金必須大於 0", ephemeral=True)

        if risk_limit is not None:
            if 1.0 <= risk_limit <= 50.0:
                kwargs['risk_limit_pct'] = risk_limit
                updates.append(f"🛡️ 風險限制: `{risk_limit}%`")
            else:
                return await interaction.response.send_message("❌ 風險限制需介於 1.0% 至 50.0% 之間", ephemeral=True)

        if enable_option_alerts is not None:
            kwargs['enable_option_alerts'] = enable_option_alerts
            updates.append(f"🔔 選項策略推播: `{'開啟' if enable_option_alerts else '關閉'}`")
            
        if enable_vtr is not None:
            kwargs['enable_vtr'] = enable_vtr
            updates.append(f"👻 虛擬交易室 (VTR): `{'開啟' if enable_vtr else '關閉'}`")

        if enable_psq_watchlist is not None:
            kwargs['enable_psq_watchlist'] = enable_psq_watchlist
            updates.append(f"⚡ PowerSqueeze 追蹤: `{'開啟' if enable_psq_watchlist else '關閉'}`")

        if enable_analyst_agent is not None:
            kwargs['enable_analyst_agent'] = enable_analyst_agent
            updates.append(f"🤖 Analyst Agent 每日推播: `{'開啟' if enable_analyst_agent else '關閉'}`")

        if polymarket_threshold is not None:
            kwargs['polymarket_threshold'] = polymarket_threshold
            status = f"`${polymarket_threshold:,.0f}`" if polymarket_threshold > 0 else "`關閉`"
            updates.append(f"🐋 Polymarket 監控: {status}")

        if polymarket_use_llm is not None:
            kwargs['polymarket_use_llm'] = polymarket_use_llm
            updates.append(f"🧠 Polymarket AI 分析: `{'開啟' if polymarket_use_llm else '關閉'}`")

        if polymarket_slippage is not None:
            if 0.1 <= polymarket_slippage <= 10.0:
                kwargs['polymarket_slippage'] = polymarket_slippage
                updates.append(f"🌊 Polymarket 滑價門檻: `{polymarket_slippage}%`")
            else:
                return await interaction.response.send_message("❌ 滑價門檻需介於 0.1% 至 10.0% 之間", ephemeral=True)

        if monthly_expense is not None:
            if monthly_expense >= 0:
                kwargs['monthly_expense'] = monthly_expense
                updates.append(f"💸 每月支出預算: `${monthly_expense:,.0f}`")
            else:
                return await interaction.response.send_message("❌ 支出預算不能為負數", ephemeral=True)

        if tax_reserve_rate is not None:
            if 0.0 <= tax_reserve_rate <= 1.0:
                kwargs['tax_reserve_rate'] = tax_reserve_rate
                updates.append(f"🏦 稅務預留比例: `{tax_reserve_rate:.1%}`")
            else:
                return await interaction.response.send_message("❌ 稅務比例需介於 0.0 與 1.0 之間", ephemeral=True)

        if cash_reserve is not None:
            if cash_reserve >= 0:
                kwargs['cash_reserve'] = cash_reserve
                updates.append(f"💰 現金儲備: `${cash_reserve:,.0f}`")
            else:
                return await interaction.response.send_message("❌ 現金儲備不能為負數", ephemeral=True)

        if not kwargs:
            return await interaction.response.send_message("請至少選擇並輸入一個要修改的參數。", ephemeral=True)

        success = database.upsert_user_config(user_id, **kwargs)
        if not success:
            return await interaction.response.send_message("❌ 設定失敗，請稍後再試。", ephemeral=True)

        msg = "✅ **帳戶設定已更新**：\n" + "\n".join(updates)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="runway_check", description="根據投資組合收益與現金儲備計算財務跑道")
    async def runway_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        
        # 🚀 執行即時 Greeks 刷新，確保數據最新
        from market_analysis.portfolio import refresh_portfolio_greeks
        await refresh_portfolio_greeks(user_id)
        
        ctx = get_full_user_context(user_id)
        
        if ctx.monthly_expense <= 0:
            await interaction.followup.send("❌ 每月支出未設定。請於 `/settings` 中更新。", ephemeral=True)
            return

        gross_monthly_yield = ctx.total_theta * 30
        net_monthly_yield = gross_monthly_yield * (1 - ctx.tax_reserve_rate)
        income_ratio = net_monthly_yield / ctx.monthly_expense if ctx.monthly_expense > 0 else 0
        
        from market_analysis.pro_management import calculate_survival_runway
        runway_days = calculate_survival_runway(
            cash_reserve=ctx.cash_reserve,
            monthly_expenses=ctx.monthly_expense,
            daily_theta=ctx.total_theta
        )
        
        daily_theta = ctx.total_theta
        daily_expense = ctx.monthly_expense / 30.0
        
        embed = discord.Embed(
            title="🏁 財務生存與跑道分析",
            color=discord.Color.green() if income_ratio >= 1 or runway_days >= 365 else discord.Color.orange()
        )
        embed.add_field(name="每日 Theta 收租額", value=f"`${daily_theta:,.2f}`", inline=True)
        embed.add_field(name="每日預算支出", value=f"`${daily_expense:,.2f}`", inline=True)
        embed.add_field(name="現金儲備健康度", value=f"`${ctx.cash_reserve:,.2f}`", inline=False)
        
        status_text = "可持續" if income_ratio >= 1.0 else "入不敷出"
        embed.add_field(name="收益支出比", value=f"`{income_ratio:.2%}` ({status_text})", inline=True)
        
        runway_val = "♾️ 無限 (收益覆蓋支出)" if runway_days >= 9999 else f"{runway_days:,.1f} 天"
        embed.add_field(name="預估生存天數", value=f"`{runway_val}`", inline=True)
        embed.set_footer(text="基於 Theta 的收益預測。已計入現金儲備與稅務估計。")
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="add_trade", description="將新的選擇權部位加入監控管線")
    @app_commands.choices(opt_type=[
        app_commands.Choice(name="Put (賣權)", value="put"),
        app_commands.Choice(name="Call (買權)", value="call")
    ])
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)", opt_type="策略類型", strike="履約價",
        expiry="到期日 (YYYY-MM-DD)", entry_price="成交價格", quantity="口數",
        stock_cost="持有現股平均成本 (可選)", category="部位類別 (SPECULATIVE/HEDGE)"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="SPECULATIVE", value="SPECULATIVE"),
        app_commands.Choice(name="HEDGE", value="HEDGE")
    ])
    async def add_trade(self, interaction: discord.Interaction, symbol: str, opt_type: app_commands.Choice[str], strike: float, expiry: str, entry_price: float, quantity: int, stock_cost: float = 0.0, category: app_commands.Choice[str] = None):
        symbol = symbol.upper()
        user_id = interaction.user.id
        trade_category = category.value if category else "SPECULATIVE"
        
        # 🛡️ Defensive Programming: Validate Expiry Date Format
        from datetime import datetime
        try:
            # Only capture the first 10 characters (YYYY-MM-DD) to prevent trailing argument capture
            expiry_clean = expiry.split(' ')[0]
            datetime.strptime(expiry_clean, '%Y-%m-%d')
            expiry = expiry_clean # Standardized format
        except Exception:
            await interaction.response.send_message(
                f"❌ **日期格式錯誤**: `{expiry}`。請確保為 `YYYY-MM-DD` 格式。", 
                ephemeral=True
            )
            return

        if not category and symbol == "SPY":
            if quantity < 0 or (opt_type.value == "put" and quantity > 0):
                trade_category = "HEDGE"

        try:
            trade_id = database.add_portfolio_record(
                user_id, symbol, opt_type.value, strike, expiry, entry_price, quantity, stock_cost,
                trade_category=trade_category
            )
            action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
            await interaction.response.send_message(
                f"✅ **新增成功 (ID: {trade_id})**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()}", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ 寫入失敗: {e}", ephemeral=True)

    @app_commands.command(name="scan", description="手動執行量化掃描與 What-if 曝險模擬")
    async def manual_scan(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer(ephemeral=True)
        user_id, symbol = interaction.user.id, symbol.upper()

        # 🚀 獲取用戶現貨成本 (如果有)
        from database.holdings import get_user_holdings
        holdings = await asyncio.to_thread(get_user_holdings, user_id)
        stock_cost = next((h['avg_cost'] for h in holdings if h['symbol'] == symbol), 0.0)

        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy['Close'].iloc[-1] if not df_spy.empty else 670.0
            from market_analysis.risk_engine import MacroContext
            macro_data = MacroContext(vix=macro_raw.get('vix', 18.0), oil_price=macro_raw.get('oil', 75.0), vix_change=macro_raw.get('vix_change', 0.0))
        except Exception:
            df_spy, spy_price, macro_data = None, 670.0, MacroContext(vix=22.0, oil_price=85.0, vix_change=0.0)

        result = await market_math.analyze_symbol(symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix)
        is_option_valid = bool(result)
        if not result: result = {'symbol': symbol, 'stock_cost': stock_cost}

        # 🚀 執行 Gap & Fill 跳空分析 (New)
        try:
            from market_analysis.gap_analysis import GapAnalyzer
            df_gap = await market_data_service.get_history_df(symbol, period="5d", interval="1d")
            if not df_gap.empty and len(df_gap) >= 2:
                gap_metrics = GapAnalyzer.analyze_gap(df_gap)
                if gap_metrics:
                    result['gap_status'] = gap_metrics
        except Exception as gap_e:
            logger.warning(f"手動掃描 Gap 分析失敗 for {symbol}: {gap_e}")

        df_hist_1d = await market_data_service.get_history_df(symbol, period="1y", interval="1d")
        from market_analysis.psq_engine import analyze_psq
        from cogs.embed_builder import create_psq_embed
        psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
        if psq_result: result['psq_result'] = psq_result

        embeds_to_send = []
        if is_option_valid:
            from services import llm_service, news_service, reddit_service
            from market_analysis.risk_engine import optimize_position_risk
            
            # 使用快取 Reddit 資料
            from database.cache import get_kv_cache
            reddit_text = get_kv_cache(f"reddit_sentiment_{symbol}") or "暫無快取情緒資料。"
            news_text = await news_service.fetch_recent_news(symbol)
            
            ai_verdict = await llm_service.evaluate_trade_risk(symbol, result.get('strategy', ''), news_text, reddit_text)
            result.update({'news_text': news_text, 'reddit_text': reddit_text, 'ai_decision': ai_verdict.get('decision', 'APPROVE'), 'ai_reasoning': ai_verdict.get('reasoning', '無資料'), 'vix': macro_data.vix, 'oil': macro_data.oil_price})

            user_context = database.get_full_user_context(user_id)
            safe_qty, hedge_spy = optimize_position_risk(current_delta=user_context.total_weighted_delta, unit_weighted_delta=result.get('weighted_delta', 0.0), user_capital=user_context.capital, spy_price=spy_price, stock_iv=result.get('iv', 0.15), strategy=result.get('strategy', ''), macro_data=macro_data, base_risk_limit_pct=user_context.risk_limit_base, vix_spot=macro_data.vix)

            projected_total_delta = user_context.total_weighted_delta + (result.get('weighted_delta', 0.0) * (-1 if "STO" in result.get('strategy', '') else 1) * safe_qty)
            projected_exposure_pct = (projected_total_delta * spy_price / user_context.capital) * 100 if user_context.capital > 0 else 0
            
            result.update({'projected_exposure_pct': round(projected_exposure_pct, 2), 'safe_qty': safe_qty, 'hedge_spy': hedge_spy, 'spy_price': spy_price})
            embeds_to_send.append(create_scan_embed(result, user_context.capital))

        if psq_result:
            result['price'] = df_hist_1d['Close'].iloc[-1] if not df_hist_1d.empty else 0.0
            embeds_to_send.append(create_psq_embed(result))
            
        if embeds_to_send:
            await interaction.followup.send(embeds=embeds_to_send)
        else:
            await interaction.followup.send(f"📊 目前 `{symbol}` 查無有效訊號。")

    @app_commands.command(name="vtr_stats", description="檢視虛擬交易室的績效統計與盈虧歸因")
    async def vtr_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            stats = await GhostTrader.get_vtr_performance_stats(interaction.user.id)
            embed = build_vtr_stats_embed(interaction.user.display_name, stats)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ 無法獲取績效數據。", ephemeral=True)

    @app_commands.command(name="vtr_list", description="列出虛擬交易室中的所有持倉與歷史紀錄")
    async def vtr_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from database.virtual_trading import get_all_virtual_trades
        rows = get_all_virtual_trades(interaction.user.id)
        if not rows:
            return await interaction.followup.send("📭 虛擬交易室目前無任何紀錄。", ephemeral=True)
        
        msg = "👻 **【虛擬交易室 (VTR) 紀錄清單】**\n"
        for row in rows[:20]: # 限制顯示最近 20 筆
            status_emoji = "🟢" if row['status'] == 'OPEN' else "⚪"
            pnl_str = f" | PnL: `{row['pnl']:+.2f}`" if row['status'] != 'OPEN' else ""
            msg += f"{status_emoji} `ID:{row['id']:02d}` | **{row['symbol']}** | ${row['strike']} {row['opt_type'].upper()} | {row['status']}{pnl_str}\n"
        
        if len(rows) > 20:
            msg += f"\n*(僅顯示最近 20 筆，總計 {len(rows)} 筆)*"
            
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="add_watch", description="將標的加入自動化量化監控清單")
    @app_commands.describe(symbol="股票代號 (如 TSLA)", use_llm="是否啟用 AI 輔助分析")
    async def add_watch(self, interaction: discord.Interaction, symbol: str, use_llm: bool = True):
        symbol = symbol.upper()
        from database.watchlist import add_watchlist_symbol
        success = add_watchlist_symbol(interaction.user.id, symbol, use_llm)
        if success:
            await interaction.response.send_message(f"✅ **已加入觀察清單**: `{symbol}` (AI 分析: `{'開啟' if use_llm else '關閉'}`)", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ `{symbol}` 已在您的觀察清單中。", ephemeral=True)

    @app_commands.command(name="edit_watch", description="修改觀察清單中的標的參數")
    @app_commands.describe(symbol="要修改的股票代號", use_llm="更新 AI 輔助分析開關 (選填)")
    async def edit_watch(self, interaction: discord.Interaction, symbol: str, use_llm: Optional[bool] = None):
        symbol = symbol.upper()
        from database.watchlist import update_user_watchlist
        success = update_user_watchlist(interaction.user.id, symbol, use_llm)
        if success:
            await interaction.response.send_message(f"✅ **已更新觀察設定**: `{symbol}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到標的 `{symbol}` 或未提供任何修改參數。", ephemeral=True)

    @app_commands.command(name="add_holding", description="登錄實際現貨持倉 (用於資產會計與 Delta 曝險精算)")
    @app_commands.describe(symbol="股票代號", quantity="持有股數", avg_cost="平均買入成本 (USD)")
    async def add_holding(self, interaction: discord.Interaction, symbol: str, quantity: float, avg_cost: float):
        symbol = symbol.upper()
        user_id = interaction.user.id
        
        if quantity <= 0 or avg_cost < 0:
            return await interaction.response.send_message("❌ 數量必須大於 0 且成本不能為負數。", ephemeral=True)
            
        from database.holdings import add_holding as db_add_holding
        success = db_add_holding(user_id, symbol, quantity, avg_cost)
        
        if success:
            # 🚀 立即刷新 Greeks 以確保曝險精算同步
            from market_analysis.portfolio import refresh_portfolio_greeks
            await refresh_portfolio_greeks(user_id)
            await interaction.response.send_message(f"✅ **現貨持倉已登錄**: `{symbol}` | `{quantity:,.0f}` 股 | 成本 `${avg_cost:,.2f}`", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 登錄失敗，請檢查輸入數據或稍後再試。", ephemeral=True)

    @app_commands.command(name="list_holdings", description="列出目前所有現貨持倉、分配比例與即時損益估計")
    async def list_holdings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        
        from database.holdings import get_user_holdings
        holdings = get_user_holdings(user_id)
        
        if not holdings:
            return await interaction.followup.send("📭 您目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。", ephemeral=True)
            
        # 獲取即時價格以計算損益
        for h in holdings:
            sym = h['symbol']
            quote = await market_data_service.get_quote(sym)
            h['current_price'] = quote.get('c', 0.0) if quote else 0.0
            
        ctx = get_full_user_context(user_id)
        from cogs.embed_builder import create_holdings_embed
        embed = create_holdings_embed(holdings, ctx.capital)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="remove_holding", description="從資產清單中移除特定的現貨紀錄")
    @app_commands.describe(symbol="要移除的股票代號")
    async def remove_holding(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        from database.holdings import delete_holding
        success = delete_holding(interaction.user.id, symbol)
        
        if success:
            # 🚀 刷新 Greeks
            from market_analysis.portfolio import refresh_portfolio_greeks
            await refresh_portfolio_greeks(interaction.user.id)
            await interaction.response.send_message(f"🗑️ **已移除現貨紀錄**: `{symbol}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到標的 `{symbol}` 的現貨紀錄。", ephemeral=True)

    @app_commands.command(name="remove_watch", description="將標的從觀察清單中移除")
    async def remove_watch(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        from database.watchlist import delete_watchlist_symbol
        success = delete_watchlist_symbol(interaction.user.id, symbol)
        if success:
            await interaction.response.send_message(f"🗑️ **已移除觀察標的**: `{symbol}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 您的觀察清單中找不到 `{symbol}`。", ephemeral=True)

    @app_commands.command(name="list_watch", description="列出您的雷達觀察清單")
    async def list_watch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from database.watchlist import get_user_watchlist
        symbols_data = get_user_watchlist(interaction.user.id)
        if not symbols_data:
            await interaction.followup.send("📭 您的觀察清單是空的。", ephemeral=True)
            return
        
        from ui.watchlist import WatchlistPagination
        view = WatchlistPagination(symbols_data)
        view.update_buttons()
        await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)

    @app_commands.command(name="list_trades", description="列出目前資料庫中的所有實單持倉")
    async def list_trades(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = database.get_user_portfolio(user_id)
        if not rows:
            await interaction.response.send_message("📭 您目前無持倉紀錄。", ephemeral=True)
            return
        msg = "📊 **【您的實單持倉清單】**\n"
        for row in rows:
            trade_id, sym, o_type, strike, exp, price, qty, stock_cost = row[:8]
            category = row[11] if len(row) > 11 else "SPEC"
            cat_tag = f" | `{category}`"
            action = "賣出 (STO)" if qty < 0 else "買入 (BTO)"
            msg += f"`ID:{trade_id:02d}` | **{sym}** | {exp} | ${strike} {o_type.upper()} | {action} {abs(qty)}口 | ${price}{cat_tag}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從監控管線中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        user_id = interaction.user.id
        record = database.delete_portfolio_record(user_id, trade_id)
        if record:
            await interaction.response.send_message(f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{record[0]}` 已移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到 ID `{trade_id}`。", ephemeral=True)

    @app_commands.command(name="transition_sim", description="模擬投機部位向 Core Equity/Covered Call 演進")
    @app_commands.describe(
        symbol="標的代號",
        current_option_pnl="目前該部位累計未實現損益 (USD)",
        target_cc_strike="預計轉換後的 Covered Call 履約價",
        target_cc_premium="預計單次收租權利金 (USD)"
    )
    async def transition_sim(
        self, 
        interaction: discord.Interaction, 
        symbol: str, 
        current_option_pnl: float, 
        target_cc_strike: float, 
        target_cc_premium: float
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        
        try:
            quote = await market_data_service.get_quote(symbol)
            current_price = quote.get('c', 0.0) if quote else 0.0
            
            if current_price <= 0:
                return await interaction.followup.send(f"❌ 無法獲取 `{symbol}` 即時報價。", ephemeral=True)

            from market_analysis.pro_management import simulate_pro_transition
            res = simulate_pro_transition(
                current_option_pnl=current_option_pnl,
                current_stock_price=current_price,
                target_cc_strike=target_cc_strike,
                target_cc_premium=target_cc_premium
            )

            embed = discord.Embed(
                title=f"🔄 戰略轉軌模擬 (演進) | {symbol}",
                description=f"模擬將 `{symbol}` 投機期權部位演進為 **核心現股 + 備兌買權 (Covered Call)** 模型。",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow()
            )
            
            embed.add_field(name="現價 (Price)", value=f"`${current_price:.2f}`", inline=True)
            embed.add_field(name="期權獲利 (Option PnL)", value=f"`${res.initial_pnl:,.2f}`", inline=True)
            
            roadmap = (
                f"1. **執行動作**：平倉現有 DITM 部位，回收收益。\n"
                f"2. **購入現股**：以 `${current_price:.2f}` 購入 100 股。\n"
                f"3. **追加資本**：需額外投入 **`${res.additional_capital_required:,.2f}`**。\n"
                f"4. **成本調整**：調整後每股成本為 **`${res.adjusted_cost_basis:.2f}`**。\n"
                f"5. **建立 CC**：賣出 `${target_cc_strike}` Call，收取 `${target_cc_premium:.2f}` 權利金。"
            )
            embed.add_field(name="🚀 資本重分配路線圖 (Roadmap)", value=roadmap, inline=False)
            
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
            await interaction.followup.send("❌ 模擬執行失敗，請檢查輸入數據。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(TerminalCog(bot))
