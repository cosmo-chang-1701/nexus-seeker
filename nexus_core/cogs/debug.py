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

async def setup(bot):
    await bot.add_cog(DebugCog(bot))