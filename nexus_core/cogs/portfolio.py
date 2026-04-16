import discord
from discord.ext import commands
from discord import app_commands
import logging
from typing import Optional

from market_analysis.ghost_trader import GhostTrader
from cogs.embed_builder import build_vtr_stats_embed

import database
import database.virtual_trading as vtr_db

logger = logging.getLogger(__name__)


class PortfolioCog(commands.Cog):
    """持倉 (Portfolio) 管理指令 — 綁定 user_id"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("PortfolioCog loaded.")

    @app_commands.command(name="add_trade", description="將新的選擇權部位加入您的專屬監控庫")
    @app_commands.choices(opt_type=[
        app_commands.Choice(name="Put (賣權)", value="put"),
        app_commands.Choice(name="Call (買權)", value="call")
    ])
    @app_commands.describe(
        symbol="股票代號 (如 TSLA)",
        opt_type="買方或賣方策略",
        strike="履約價",
        expiry="到期日 (YYYY-MM-DD)",
        entry_price="成交價格 (權利金)",
        quantity="口數",
        stock_cost="預設 0。輸入您的持有現股平均成本 (將精確計算防禦區間)",
        category="部位類別 (SPECULATIVE/HEDGE)，預設為 SPECULATIVE"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="SPECULATIVE (投機部位)", value="SPECULATIVE"),
        app_commands.Choice(name="HEDGE (對沖部位)", value="HEDGE")
    ])
    async def add_trade(self, interaction: discord.Interaction, symbol: str, opt_type: app_commands.Choice[str], strike: float, expiry: str, entry_price: float, quantity: int, stock_cost: float = 0.0, category: app_commands.Choice[str] = None):
        symbol = symbol.upper()
        user_id = interaction.user.id
        trade_category = category.value if category else "SPECULATIVE"
        
        # 自動優化：通常 SPY Short Call/Put 或 BTO Put 可能是對沖
        if not category and symbol == "SPY":
            if quantity < 0 or (opt_type.value == "put" and quantity > 0):
                trade_category = "HEDGE"

        try:
            trade_id = database.add_portfolio_record(
                user_id, symbol, opt_type.value, strike, expiry, entry_price, quantity, stock_cost,
                trade_category=trade_category
            )
            action_text = "賣出 (STO)" if quantity < 0 else "買入 (BTO)"
            # 私訊回覆使用者
            cost_str = f" | 現股成本: ${stock_cost:.2f}" if stock_cost > 0.0 else ""
            await interaction.response.send_message(
                f"✅ **新增成功 (ID: {trade_id})**: {action_text} {abs(quantity)} 口 `{symbol}` ${strike} {opt_type.value.upper()} ({expiry} 到期){cost_str}", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ 寫入失敗: {e}", ephemeral=True)

    @app_commands.command(name="settings", description="配置帳戶全域參數 (資金與風險限制)")
    @app_commands.describe(
        capital="更新帳戶總資金 (USD)",
        risk_limit="更新基準風險上限 % (1.0 - 50.0)",
        enable_option_alerts="是否接收選項策略推播",
        enable_vtr="是否啟用虛擬交易室 GhostTrader 自動建倉",
        enable_psq_watchlist="是否對 watchlist 開啟 PowerSqueeze 戰情追蹤"
    )
    async def update_settings(
        self, 
        interaction: discord.Interaction, 
        capital: Optional[float] = None, 
        risk_limit: Optional[float] = None,
        enable_option_alerts: Optional[bool] = None,
        enable_vtr: Optional[bool] = None,
        enable_psq_watchlist: Optional[bool] = None
    ):
        user_id = interaction.user.id
        updates = []
        kwargs = {}

        # 1. 驗證並準備更新資金
        if capital is not None:
            if capital > 0:
                kwargs['capital'] = capital
                updates.append(f"💰 總資金: `${capital:,.2f}`")
            else:
                return await interaction.response.send_message("❌ 資金必須大於 0", ephemeral=True)

        # 2. 驗證並準備更新風險限制
        if risk_limit is not None:
            if 1.0 <= risk_limit <= 50.0:
                kwargs['risk_limit_pct'] = risk_limit
                updates.append(f"🛡️ 風險限制: `{risk_limit}%`")
            else:
                return await interaction.response.send_message("❌ 風險限制需介於 1.0% 至 50.0% 之間", ephemeral=True)

        # 3. 設定切換
        if enable_option_alerts is not None:
            kwargs['enable_option_alerts'] = enable_option_alerts
            updates.append(f"🔔 選項策略推播: `{'開啟' if enable_option_alerts else '關閉'}`")
            
        if enable_vtr is not None:
            kwargs['enable_vtr'] = enable_vtr
            updates.append(f"👻 虛擬交易室 (VTR): `{'開啟' if enable_vtr else '關閉'}`")

        if enable_psq_watchlist is not None:
            kwargs['enable_psq_watchlist'] = enable_psq_watchlist
            updates.append(f"⚡ PowerSqueeze 追蹤: `{'開啟' if enable_psq_watchlist else '關閉'}`")

        # 4. 執行資料庫更新
        if not kwargs:
            return await interaction.response.send_message("請至少選擇並輸入一個要修改的參數。", ephemeral=True)

        success = database.upsert_user_config(user_id, **kwargs)
        if not success:
            return await interaction.response.send_message("❌ 設定失敗，請稍後再試。", ephemeral=True)

        msg = "✅ **帳戶設定已更新**：\n" + "\n".join(updates)
        await interaction.response.send_message(msg, ephemeral=True)
    @app_commands.command(name="list_trades", description="列出您目前資料庫中的所有持倉")
    async def list_trades(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = database.get_user_portfolio(user_id)
        if not rows:
            await interaction.response.send_message("📭 您目前無持倉紀錄。", ephemeral=True)
            return
        msg = "📊 **【您的專屬持倉清單】**\n"
        for row in rows:
            # 根據 portfolio.py 的 get_user_portfolio 回傳的欄位
            # id(0), symbol(1), opt_type(2), strike(3), expiry(4), entry_price(5), quantity(6), stock_cost(7), 
            # weighted_delta(8), theta(9), gamma(10), trade_category(11)
            trade_id, sym, o_type, strike, exp, price, qty, stock_cost = row[:8]
            category = row[11] if len(row) > 11 else "SPEC"
            cat_tag = f" | `{category}`"
            
            action = "賣出 (STO)" if qty < 0 else "買入 (BTO)"
            cov_str = f" | 現股成本: ${stock_cost:.2f}" if stock_cost > 0.0 else ""
            msg += f"`ID:{trade_id:02d}` | **{sym}** | {exp} 到期 | ${strike} {o_type.upper()} | {action} {abs(qty)}口 | 成本: ${price}{cov_str}{cat_tag}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="remove_trade", description="將部位從您的監控庫中移除")
    async def remove_trade(self, interaction: discord.Interaction, trade_id: int):
        user_id = interaction.user.id
        record = database.delete_portfolio_record(user_id, trade_id)
        if record:
            await interaction.response.send_message(f"🗑️ **已刪除紀錄 (ID: {trade_id})**: `{record[0]}` ${record[1]} {record[2].upper()} 已移除。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 找不到屬於您的 ID `{trade_id}`。", ephemeral=True)

    @app_commands.command(name="vtr_list", description="列出目前虛擬交易室 (VTR) 的所有持倉")
    async def vtr_list(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        rows = vtr_db.get_virtual_trades(user_id=user_id, status='OPEN')
        if not rows:
            await interaction.response.send_message("📭 目前虛擬交易室無任何開啟中的持倉。", ephemeral=True)
            return
            
        msg = "👻 **【虛擬交易室 (VTR) 開啟部位】**\n"
        for row in rows:
            action = "賣出 (STO)" if row['quantity'] < 0 else "買入 (BTO)"
            msg += f"`ID:{row['id']:03d}` | **{row['symbol']}** | {row['expiry']} 到期 | ${row['strike']} {row['opt_type'].upper()} | {action} {abs(row['quantity'])}口 | 建倉價: ${row['entry_price']:.2f}\n"
            
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="vtr_stats", description="檢視虛擬交易室的績效統計")
    async def vtr_stats(self, interaction: discord.Interaction):
        # 1. 延遲回覆 (Defer)，因為計算績效需要 Database I/O
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 2. 呼叫 GhostTrader 統計引擎
            stats = await GhostTrader.get_vtr_performance_stats(interaction.user.id)
            
            # 3. 渲染 UI
            embed = build_vtr_stats_embed(interaction.user.display_name, stats)
            
            # 4. 回傳臨時訊息
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"執行 /vtr_stats 失敗: {e}")
            await interaction.followup.send("❌ 無法獲取績效數據，請確認是否有已結算的虛擬部位。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(PortfolioCog(bot))
