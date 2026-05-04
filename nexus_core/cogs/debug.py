import discord
from discord import app_commands
from discord.ext import commands
from cogs.embed_builder import create_scan_embed
import database

class DebugCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="test_risk_ui", description="🛠️ [開發者] 模擬高風險 LMND 掃描視覺成果")
    async def test_risk_ui(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # 1. 準備基礎 Mock Data
        mock_data = {
            "symbol": "LMND",
            "price": 52.15,
            "rsi": 32.5,
            "sma20": 60.10,
            "hv_rank": 78.5,
            "vrp": 0.045,
            "ts_ratio": 1.08,
            "ts_state": "🚨 恐慌 (Backwardation)",
            "v_skew": 1.55,
            "v_skew_state": "⚠️ 嚴重左偏",
            "delta": -0.22,      # 原始合約 Delta
            "iv": 0.85,
            "aroc": 45.5,
            "strategy": "STO_PUT",
            "target_date": "2026-03-20",
            "strike": 50.0,
            "bid": 2.45,
            "ask": 2.60,
            "spread_ratio": 5.9,
            "liq_status": "🟢 優良",
            "liq_msg": "流動性極佳 | 建議掛 Mid-price",
            "earnings_days": 5,
            "mmm_pct": 12.5,
            "safe_lower": 45.6,
            "safe_upper": 58.7,
            "expected_move": 6.5,
            "em_lower": 45.6,
            "em_upper": 58.7,
            "mid_price": 2.52,
            "spy_price": 500.0,
            "ai_decision": "VETO",
            "ai_reasoning": "LMND 目前處於極高 Beta 狀態，且曝險過重。"
        }

        # 🚀 2. 插入這段：修正 Mock Data 的數學一致性
        beta_val = 2.45  # LMND 的實際高 Beta
        spy_p = mock_data['spy_price']
        price_ratio = mock_data['price'] / spy_p # 標本價格比
        raw_delta = mock_data['delta']

        # 單口加權 Delta 公式：Delta * Beta * (Price / SPY_Price) * 100
        # 註：此處不再乘 -1，讓它維持負值，由 UI 層處理「部位方向」
        weighted_delta_val = round(raw_delta * beta_val * price_ratio * 100, 1)

        # 模擬總體曝險 (現有 10% + 本單 25.5%) = 35.5%
        mock_data.update({
            "beta": beta_val,
            "weighted_delta": weighted_delta_val, # 這會變成約 +5.6 (單口加權股數)
            "projected_exposure_pct": 35.5,       # 這是總體模擬結果
            "suggested_contracts": 1,
            "safe_qty": 0,
            "hedge_spy": 20.5
        })

        # 3. 獲取用戶資金並渲染
        user_capital = database.get_full_user_context(interaction.user.id).capital
        embed = create_scan_embed(mock_data, user_capital)
        
        await interaction.followup.send(
            content="📊 **這是一份經過數學修正的模擬資料，用於驗證 Beta 與加權股數的正負號邏輯。**",
            embed=embed
        )

    @app_commands.command(name="test_poly_whale", description="🛠️ [開發者] 模擬 Polymarket 巨鯨交易推播")
    @app_commands.describe(usd_value="模擬成交金額 (USD)", side="交易方向 (BUY/SELL)")
    async def test_poly_whale(self, interaction: discord.Interaction, usd_value: float = 50000.0, side: str = "BUY"):
        await interaction.response.defer(ephemeral=True)
        
        try:
            from services.polymarket_service import PolymarketService
            from services.llm_service import generate_polymarket_summary
            
            # 1. 準備 Mock Data
            mock_trade = {
                "asset_id": "0xTEST",
                "price": 0.5,
                "size": usd_value / 0.5,
                "side": side.upper(),
                "event_type": "trade"
            }
            
            mock_market = {
                "question": "Will Bitcoin reach $100,000 by end of 2026?",
                "description": "This market resolves to Yes if BTC hits $100k according to CoinGecko by Dec 31, 2026.",
                "outcome": "Yes" if side.upper() == "BUY" else "No"
            }
            
            # 2. 獲取 LLM 總結
            summary = await generate_polymarket_summary(mock_market, mock_trade, usd_value)
            
            # 3. 使用 PolymarketService 的私訊推播邏輯 (或直接模擬)
            if hasattr(self.bot, 'polymarket_service'):
                # 模擬動態門檻，預設為 $10,000 以便計算倍數
                mock_threshold = 10000.0
                await self.bot.polymarket_service._push_notification(
                    interaction.user.id, summary, mock_market, mock_trade, usd_value, mock_threshold
                )
                await interaction.followup.send(f"✅ 已成功模擬並發送巨鯨交易通知 (${usd_value:,.2f}) 到您的私訊。", ephemeral=True)
            else:
                await interaction.followup.send("❌ Polymarket 服務未啟動。", ephemeral=True)
                
        except Exception as e:
            await interaction.followup.send(f"❌ 模擬失敗: {e}", ephemeral=True)

    @app_commands.command(name="poly_status", description="🛠️ [開發者] 查看 Polymarket WebSocket 連線狀態")
    async def poly_status(self, interaction: discord.Interaction):
        if not hasattr(self.bot, 'polymarket_service'):
            await interaction.response.send_message("❌ Polymarket 服務未初始化。", ephemeral=True)
            return
            
        status = self.bot.polymarket_service.get_status()
        
        embed = discord.Embed(
            title="🐋 Polymarket 服務狀態",
            color=discord.Color.green() if status["connected"] else discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        
        status_emoji = "🟢 已連線" if status["connected"] else "🔴 斷線中"
        running_emoji = "✅ 運行中" if status["running"] else "🛑 已停止"
        
        embed.add_field(name="服務狀態", value=running_emoji, inline=True)
        embed.add_field(name="連線狀態", value=status_emoji, inline=True)
        embed.add_field(name="訂閱資產數", value=f"`{status['asset_count']}`", inline=True)
        embed.add_field(name="最後訊息時間", value=status['last_message'], inline=False)
        embed.add_field(name="異常次數", value=f"`{status['errors']}`", inline=True)
        
        embed.set_footer(text="Nexus Seeker | Polymarket Monitor")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(DebugCog(bot))