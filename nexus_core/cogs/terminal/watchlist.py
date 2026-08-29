"""WATCH（觀察清單）資產的新增、移除、列表與提升為 TRADE 指令邏輯。"""

from typing import Any, Optional
import asyncio
import re

import discord
from discord import app_commands

from services import market_data_service
from cogs.embed_builder import (
    create_asset_promotion_embed,
    create_error_embed,
    create_info_embed,
)


def _parse_symbol_list(raw: str) -> list[str]:
    """將以逗號或空白分隔的標的代號字串解析為去重、大寫的代號列表。"""
    if not raw:
        return []
    tokens = re.split(r"[,\s]+", raw.strip())
    symbols: list[str] = []
    for t in tokens:
        sym = t.strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols


async def add_watch_impl(interaction: discord.Interaction, symbol: str) -> Any:
    await interaction.response.defer(ephemeral=True)

    symbols = _parse_symbol_list(symbol)
    if not symbols:
        return await interaction.followup.send(
            embed=create_error_embed(
                "請輸入至少一個有效的股票代號。", title="系統錯誤"
            ),
            ephemeral=True,
        )

    from services.asset_manager import AssetManager, WatchlistLimitExceededError
    from models.asset import Asset, ContextType

    if len(symbols) == 1:
        # 單一代號：維持原有訊息文案不變 (向後相容)
        sym = symbols[0]
        if not await market_data_service.validate_symbol(sym):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"**無效的標的代號**: `{sym}`。請輸入正確的美股代號。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )

        manager = AssetManager()
        asset = Asset(
            user_id=interaction.user.id,
            symbol=sym,
            context_type=ContextType.WATCH,
            metadata={},
        )

        try:
            success = manager.add_asset(asset)
        except WatchlistLimitExceededError as e:
            return await interaction.followup.send(
                embed=create_error_embed(str(e), title="系統警告"), ephemeral=True
            )

        if success:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="操作成功",
                    message=f"✅ **已加入觀察清單**: `{sym}`",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"`{sym}` 已在您的資產清單中或發生錯誤。", title="系統警告"
                ),
                ephemeral=True,
            )
        return

    # 多代號批次新增
    from cogs.embed_builders.watchlist_embeds import (
        create_bulk_watchlist_result_embed,
    )

    sem = asyncio.Semaphore(3)

    async def _validate(sym: str) -> tuple[str, bool]:
        async with sem:
            return sym, await market_data_service.validate_symbol(sym)

    validation_results = await asyncio.gather(*[_validate(s) for s in symbols])

    manager = AssetManager()
    added: list[str] = []
    invalid: list[str] = []
    duplicates: list[str] = []
    capped: list[str] = []
    limit_hit = False

    for sym, is_valid in validation_results:
        if not is_valid:
            invalid.append(sym)
            continue
        if limit_hit:
            capped.append(sym)
            continue

        asset = Asset(
            user_id=interaction.user.id,
            symbol=sym,
            context_type=ContextType.WATCH,
            metadata={},
        )
        try:
            success = manager.add_asset(asset)
        except WatchlistLimitExceededError:
            limit_hit = True
            capped.append(sym)
            continue

        if success:
            added.append(sym)
        else:
            duplicates.append(sym)

    embed = create_bulk_watchlist_result_embed(
        "加入",
        added,
        {"已存在": duplicates, "無效代號": invalid, "已達上限": capped},
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def remove_watch_impl(interaction: discord.Interaction, symbol: str) -> Any:
    await interaction.response.defer(ephemeral=True)

    symbols = _parse_symbol_list(symbol)
    if not symbols:
        return await interaction.followup.send(
            embed=create_error_embed(
                "請輸入至少一個有效的股票代號。", title="系統錯誤"
            ),
            ephemeral=True,
        )

    from services.asset_manager import AssetManager
    from models.asset import ContextType

    manager = AssetManager()

    if len(symbols) == 1:
        # 單一代號：維持原有訊息文案不變 (向後相容)
        sym = symbols[0]
        success = manager.delete_asset_by_symbol(
            interaction.user.id, sym, ContextType.WATCH
        )

        if success:
            await interaction.followup.send(
                embed=create_info_embed(
                    title="移除成功", message=f"✅ **已移除觀察標的**: `{sym}`"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_error_embed(
                    f"您的觀察清單中找不到 `{sym}`。", title="系統錯誤"
                ),
                ephemeral=True,
            )
        return

    # 多代號批次移除
    from cogs.embed_builders.watchlist_embeds import (
        create_bulk_watchlist_result_embed,
    )

    removed: list[str] = []
    not_found: list[str] = []
    for sym in symbols:
        success = manager.delete_asset_by_symbol(
            interaction.user.id, sym, ContextType.WATCH
        )
        if success:
            removed.append(sym)
        else:
            not_found.append(sym)

    embed = create_bulk_watchlist_result_embed("移除", removed, {"找不到": not_found})
    await interaction.followup.send(embed=embed, ephemeral=True)


async def list_watch_impl(
    interaction: discord.Interaction,
    sort: Optional[app_commands.Choice[str]] = None,
    query: Optional[str] = None,
) -> Any:
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

    symbols_data = [(a.symbol, getattr(a, "tags", None), a.created_at) for a in assets]

    from ui.watchlist import WatchlistPagination

    view = WatchlistPagination(
        symbols_data,
        original_interaction=interaction,
        sort_key=sort.value if sort else None,
        query=query,
    )
    view.update_buttons()
    await interaction.followup.send(
        embed=view.create_embed(), view=view, ephemeral=True
    )


async def promote_watch_impl(
    interaction: discord.Interaction,
    symbol: str,
    opt_type: str,
    strike: float,
    expiry: str,
    price: float,
    qty: int,
) -> Any:
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
