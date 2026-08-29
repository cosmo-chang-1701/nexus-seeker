"""TRADE（實單期權）資產的新增、編輯、列表、刪除與結算指令邏輯。"""

from typing import Any, Dict, Optional
import logging
from datetime import datetime

import discord
from discord import app_commands

import database
from services import market_data_service
from cogs.embed_builder import create_error_embed, create_info_embed

logger = logging.getLogger(__name__)


async def add_trade_impl(
    interaction: discord.Interaction,
    symbol: str,
    opt_type: app_commands.Choice[str],
    strike: float,
    expiry: str,
    entry_price: float,
    quantity: int,
) -> Any:
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

        if (is_market_etf and (is_short_position or is_long_put)) or is_covered_call:
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


async def edit_trade_impl(
    interaction: discord.Interaction,
    trade_id: int,
    strike: Optional[float] = None,
    expiry: Optional[str] = None,
    price: Optional[float] = None,
    quantity: Optional[int] = None,
    category: Optional[app_commands.Choice[str]] = None,
) -> Any:
    await interaction.response.defer(ephemeral=True)
    from services.asset_manager import AssetManager

    manager = AssetManager()

    updates: Dict[str, Any] = {}
    if strike is not None:
        updates["strike"] = strike
    if expiry is not None:
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


async def list_trades_impl(bot: Any, interaction: discord.Interaction) -> Any:
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    from services.trading_service import TradingService

    trading_service = TradingService(bot)

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
            embed=create_info_embed(title="查無資料", message="📭 您目前無持倉紀錄。"),
            ephemeral=True,
        )
        return

    ctx = database.get_full_user_context(user_id)
    from cogs.embed_builder import create_trades_embed

    embed = create_trades_embed(pnl_data, ctx.capital)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def remove_trade_impl(interaction: discord.Interaction, trade_id: int) -> Any:
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


async def settle_trade_impl(
    interaction: discord.Interaction, asset_id: int, execution_price: float
) -> Any:
    await interaction.response.defer(ephemeral=True)
    from services.asset_manager import AssetManager

    manager = AssetManager()

    success = manager.settle_to_holding(interaction.user.id, asset_id, execution_price)
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
