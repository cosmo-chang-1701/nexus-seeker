import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime
from typing import Any

import database
from services import reddit_service, news_service, market_data_service, llm_service
from cogs.embed_builder import (
    create_error_embed,
    create_info_embed,
    create_polymarket_list_embed,
    create_news_scan_embed,
    create_reddit_scan_embed,
    create_scan_embed,
)

logger = logging.getLogger(__name__)


class IntelligenceCog(commands.Cog):
    """
    [Intelligence] Market Intelligence & Edge Detection Terminal.
    Handles Polymarket whale tracking, social sentiment, and news intelligence.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("IntelligenceCog loaded.")

    @app_commands.command(
        name="poly_list", description="顯示目前監控中的 Polymarket 活躍市場清單"
    )
    async def poly_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if not hasattr(self.bot, "polymarket_service"):
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "Polymarket 服務未初始化。", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

            markets = self.bot.polymarket_service.get_active_markets(limit=20)
            embed = create_polymarket_list_embed(markets)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"獲取 Polymarket 清單失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed(
                    "獲取 Polymarket 資訊時發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="poly_status", description="🛠️ [開發者] 查看 Polymarket WebSocket 連線狀態"
    )
    async def poly_status(self, interaction: discord.Interaction):
        if not hasattr(self.bot, "polymarket_service"):
            await interaction.response.send_message(
                embed=create_error_embed("Polymarket 服務未初始化。", title="系統錯誤"),
                ephemeral=True,
            )
            return

        status = self.bot.polymarket_service.get_status()

        embed = discord.Embed(
            title="【 🐋 Polymarket 服務狀態 】",
            color=discord.Color.green() if status["connected"] else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

        status_emoji = "🟢 已連線" if status["connected"] else "🔴 斷線中"
        running_emoji = "✅ 運行中" if status["running"] else "🛑 已停止"

        content = [
            "## 🖥️ 監控系統運行資訊",
            "---",
            f"**服務狀態：** {running_emoji}",
            f"**連線狀態：** {status_emoji}",
            f"**訂閱資產：** `{status['asset_count']}` 個標的",
            f"**最後訊息：** {status['last_message']}",
            f"**異常計數：** `{status['errors']}` 次",
            "---",
        ]

        embed.description = "\n".join(content)
        embed.set_footer(text="Nexus Seeker | Polymarket Monitor")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="test_poly_whale", description="🛠️ [開發者] 模擬 Polymarket 巨鯨交易推播"
    )
    @app_commands.describe(usd_value="模擬成交金額 (USD)", side="交易方向 (BUY/SELL)")
    async def test_poly_whale(
        self,
        interaction: discord.Interaction,
        usd_value: float = 50000.0,
        side: str = "BUY",
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            # 1. 準備 Mock Data
            mock_trade = {
                "asset_id": "0xTEST",
                "price": 0.5,
                "size": usd_value / 0.5,
                "side": side.upper(),
                "event_type": "trade",
            }

            mock_market = {
                "question": "Will Bitcoin reach $100,000 by end of 2026?",
                "description": "This market resolves to Yes if BTC hits $100k according to CoinGecko by Dec 31, 2026.",
                "outcome": "Yes" if side.upper() == "BUY" else "No",
            }

            # 2. 獲取 LLM 總結
            summary = await llm_service.generate_polymarket_summary(
                mock_market, mock_trade, usd_value
            )

            # 3. 使用 PolymarketService 的私訊推播邏輯 (或直接模擬)
            if hasattr(self.bot, "polymarket_service"):
                # 模擬動態門檻，預設為 $10,000 以便計算倍數
                mock_threshold = 10000.0
                await self.bot.polymarket_service._push_notification(
                    interaction.user.id,
                    summary,
                    mock_market,
                    mock_trade,
                    usd_value,
                    mock_threshold,
                )
                await interaction.followup.send(
                    embed=create_info_embed(
                        title="操作成功",
                        message=f"✅ 已成功模擬並發送巨鯨交易通知 (${usd_value:,.2f}) 到您的私訊。",
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "Polymarket 服務未啟動。", title="系統錯誤"
                    ),
                    ephemeral=True,
                )

        except Exception as e:
            logger.error(f"Polymarket 模擬失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"模擬失敗: {e}", title="系統錯誤"),
                ephemeral=True,
            )

    @app_commands.command(
        name="test_risk_ui", description="🛠️ [開發者] 模擬高風險 LMND 掃描視覺成果"
    )
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
            "delta": -0.22,  # 原始合約 Delta
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
            "ai_reasoning": "LMND 目前處於極高 Beta 狀態，且曝險過重。",
        }

        # 🚀 2. 插入這段：修正 Mock Data 的數學一致性
        from typing import cast

        beta_val = float(cast(Any, mock_data["beta"])) if "beta" in mock_data else 2.45
        spy_p = float(cast(Any, mock_data["spy_price"]))
        price_ratio = float(cast(Any, mock_data["price"])) / spy_p  # 標本價格比
        raw_delta = float(cast(Any, mock_data["delta"]))

        # 單口加權 Delta 公式：Delta * Beta * (Price / SPY_Price) * 100
        weighted_delta_val = round(raw_delta * beta_val * price_ratio * 100, 1)

        # 模擬總體曝險 (現有 10% + 本單 25.5%) = 35.5%
        mock_data.update(
            {
                "beta": beta_val,
                "weighted_delta": weighted_delta_val,  # 這會變成約 +5.6 (單口加權股數)
                "projected_exposure_pct": 35.5,  # 這是總體模擬結果
                "suggested_contracts": 1,
                "safe_qty": 0,
                "hedge_spy": 20.5,
            }
        )

        # 3. 獲取用戶資金並渲染
        user_ctx = database.get_full_user_context(interaction.user.id)
        user_capital = user_ctx.capital
        mock_data["risk_limit"] = user_ctx.risk_limit
        embed = create_scan_embed(mock_data, user_capital)

        await interaction.followup.send(
            content="📊 **這是一份經過數學修正的模擬資料，用於驗證 Beta 與加權股數的正負號邏輯。**",
            embed=embed,
        )

    @app_commands.command(name="scan_news", description="掃描特定標的之最新官方新聞")
    async def scan_news(
        self, interaction: discord.Interaction, symbol: str, limit: int = 5
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        try:
            news_text = await news_service.fetch_recent_news(symbol, limit)
            await interaction.followup.send(
                embed=create_news_scan_embed(symbol, news_text), ephemeral=True
            )
        except Exception as e:
            logger.error(f"[{symbol}] 新聞掃描失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed(
                    f"獲取 {symbol} 新聞時發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(
        name="scan_reddit", description="掃描特定標的之 Reddit 散戶情緒 (過去 24 小時)"
    )
    async def scan_reddit(
        self, interaction: discord.Interaction, symbol: str, limit: int = 5
    ):
        await interaction.response.defer(ephemeral=True)
        symbol = symbol.upper()
        try:
            reddit_text = await reddit_service.get_reddit_context(symbol, limit)
            await interaction.followup.send(
                embed=create_reddit_scan_embed(symbol, reddit_text), ephemeral=True
            )
        except Exception as e:
            logger.error(f"[{symbol}] Reddit 掃描失敗: {e}")
            await interaction.followup.send(
                embed=create_error_embed(
                    f"獲取 {symbol} Reddit 情緒時發生錯誤。", title="系統錯誤"
                ),
                ephemeral=True,
            )

    @app_commands.command(name="quote", description="獲取標的即時報價 (Finnhub)")
    async def quote(self, interaction: discord.Interaction, symbol: str):
        symbol = symbol.upper()
        await interaction.response.defer(ephemeral=True)
        data = await market_data_service.get_quote(symbol)
        if not data:
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"無法取得 `{symbol}` 的報價，請檢查代碼是否正確。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"💹 {symbol} 即時報價 (Real-time Quote)",
            color=discord.Color.blue() if data["dp"] >= 0 else discord.Color.red(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="現價 (Current)", value=f"**${data['c']}**", inline=True)
        embed.add_field(name="漲跌幅 (%)", value=f"`{data['dp']}%`", inline=True)
        embed.add_field(
            name="今日高/低",
            value=f"H: `${data['h']}` / L: `${data['l']}`",
            inline=False,
        )
        embed.add_field(name="前收盤 (PC)", value=f"`${data['pc']}`", inline=True)
        embed.set_footer(text="Nexus Seeker | Market Intelligence Feed")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(IntelligenceCog(bot))
