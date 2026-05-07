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
            title="🏁 Financial Runway & Survival Analysis",
            color=discord.Color.green() if income_ratio >= 1 or runway_days >= 365 else discord.Color.orange()
        )
        embed.add_field(name="Daily Theta Income", value=f"`${daily_theta:,.2f}`", inline=True)
        embed.add_field(name="Daily Budgeted Expense", value=f"`${daily_expense:,.2f}`", inline=True)
        embed.add_field(name="Cash Reserve", value=f"`${ctx.cash_reserve:,.2f}`", inline=False)
        
        status_text = "Sustainable" if income_ratio >= 1.0 else "Deficit"
        embed.add_field(name="Income/Expense Ratio", value=f"{income_ratio:.2f} ({status_text})", inline=True)
        
        runway_val = "♾️ 無限 (收益覆蓋支出)" if runway_days >= 9999 else f"{runway_days:,.1f} 天"
        embed.add_field(name="Survival Runway", value=f"`{runway_val}`", inline=True)
        embed.set_footer(text="Theta-based yield projection. Accounts for cash reserves and tax estimates.")
        
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

        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy['Close'].iloc[-1] if not df_spy.empty else 670.0
            from market_analysis.risk_engine import MacroContext
            macro_data = MacroContext(vix=macro_raw.get('vix', 18.0), oil_price=macro_raw.get('oil', 75.0), vix_change=macro_raw.get('vix_change', 0.0))
        except Exception:
            df_spy, spy_price, macro_data = None, 670.0, MacroContext(vix=22.0, oil_price=85.0, vix_change=0.0)

        result = await market_math.analyze_symbol(symbol, 0.0, df_spy, spy_price, vix_spot=macro_data.vix)
        is_option_valid = bool(result)
        if not result: result = {'symbol': symbol, 'stock_cost': 0.0}

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

async def setup(bot):
    await bot.add_cog(TerminalCog(bot))
