"""HOLDING（現貨持倉）資產的新增、編輯、列表與刪除指令邏輯。"""

from typing import Any, Dict, Optional
import asyncio
from datetime import datetime

import discord
from discord import app_commands

from services import market_data_service
from cogs.embed_builder import create_error_embed, create_info_embed
from database.user_settings import get_full_user_context


def _validate_holding_config_params(
    max_allocation_pct: Optional[float],
    target_allocation_pct: Optional[float],
    boxx_allocation_pct: Optional[float],
    acquired_at: Optional[str],
) -> Optional[discord.Embed]:
    """驗證 /add_holding 與 /edit_holding 共用的持倉配置參數，合法回傳 None，否則回傳錯誤 Embed。"""
    for label, val in (
        ("資產配置上限", max_allocation_pct),
        ("目標配置比例", target_allocation_pct),
        ("BOXX 防禦閾值", boxx_allocation_pct),
    ):
        if val is not None and not (0.0 < val <= 100.0):
            return create_error_embed(
                f"**{label}** 必須介於 0 (不含) 到 100 之間。", title="系統錯誤"
            )
    if (
        max_allocation_pct is not None
        and target_allocation_pct is not None
        and target_allocation_pct > max_allocation_pct
    ):
        return create_error_embed(
            "**目標配置比例** 不可大於 **資產配置上限**。", title="系統錯誤"
        )

    if acquired_at is not None:
        try:
            datetime.strptime(acquired_at, "%Y-%m-%d")
        except ValueError:
            return create_error_embed(
                "**建倉日期** 格式錯誤，請使用 `YYYY-MM-DD` (例如 `2024-03-15`)。",
                title="系統錯誤",
            )

    return None


async def add_holding_impl(
    interaction: discord.Interaction,
    symbol: str,
    quantity: float,
    avg_cost: float,
    asset_class: Optional[app_commands.Choice[str]] = None,
    max_allocation_pct: Optional[float] = None,
    target_allocation_pct: Optional[float] = None,
    boxx_allocation_pct: Optional[float] = None,
    acquired_at: Optional[str] = None,
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

    if quantity <= 0 or avg_cost < 0:
        return await interaction.followup.send(
            embed=create_error_embed(
                "數量必須大於 0 且成本不能為負數。", title="系統錯誤"
            ),
            ephemeral=True,
        )

    config_error = _validate_holding_config_params(
        max_allocation_pct, target_allocation_pct, boxx_allocation_pct, acquired_at
    )
    if config_error is not None:
        return await interaction.followup.send(embed=config_error, ephemeral=True)

    from services.asset_manager import AssetManager
    from models.asset import Asset, ContextType

    manager = AssetManager()

    # 🚀 檢查是否已存在，若存在則更新 (Upsert 邏輯)
    existing_asset = manager.get_asset_by_symbol(user_id, symbol, ContextType.HOLDING)

    if existing_asset:
        existing_asset.metadata["quantity"] = quantity
        existing_asset.metadata["avg_cost"] = avg_cost
        if asset_class is not None:
            existing_asset.metadata["asset_class"] = asset_class.value
        if max_allocation_pct is not None:
            existing_asset.metadata["max_allocation_pct"] = max_allocation_pct / 100.0
        if target_allocation_pct is not None:
            existing_asset.metadata["target_allocation_pct"] = (
                target_allocation_pct / 100.0
            )
        if boxx_allocation_pct is not None:
            existing_asset.metadata["boxx_allocation_pct"] = boxx_allocation_pct / 100.0
        if acquired_at is not None:
            existing_asset.metadata["acquired_at"] = acquired_at
        success = manager.update_asset(existing_asset)
        action_text = "更新"
    else:
        # 首次登錄時記錄建倉日期，供動態轉倉引擎粗估長/短期資本利得稅率區間；
        # 使用者可直接透過 acquired_at 參數指定實際開倉日，未提供時預設為今天。
        # /edit_holding 的 acquired_at 參數仍可用於事後回填校正。
        metadata: Dict[str, Any] = {
            "quantity": quantity,
            "avg_cost": avg_cost,
            "acquired_at": acquired_at or datetime.now().strftime("%Y-%m-%d"),
        }
        if asset_class is not None:
            metadata["asset_class"] = asset_class.value
        if max_allocation_pct is not None:
            metadata["max_allocation_pct"] = max_allocation_pct / 100.0
        if target_allocation_pct is not None:
            metadata["target_allocation_pct"] = target_allocation_pct / 100.0
        if boxx_allocation_pct is not None:
            metadata["boxx_allocation_pct"] = boxx_allocation_pct / 100.0
        asset = Asset(
            user_id=user_id,
            symbol=symbol,
            context_type=ContextType.HOLDING,
            metadata=metadata,
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


async def edit_holding_impl(
    interaction: discord.Interaction,
    symbol: str,
    quantity: Optional[float] = None,
    avg_cost: Optional[float] = None,
    asset_class: Optional[app_commands.Choice[str]] = None,
    max_allocation_pct: Optional[float] = None,
    target_allocation_pct: Optional[float] = None,
    boxx_allocation_pct: Optional[float] = None,
    acquired_at: Optional[str] = None,
) -> Any:
    symbol = symbol.upper()
    if (
        quantity is None
        and avg_cost is None
        and asset_class is None
        and max_allocation_pct is None
        and target_allocation_pct is None
        and boxx_allocation_pct is None
        and acquired_at is None
    ):
        return await interaction.response.send_message(
            embed=create_info_embed(title="系統資訊", message=" 請提供要修改的參數。"),
            ephemeral=True,
        )

    config_error = _validate_holding_config_params(
        max_allocation_pct, target_allocation_pct, boxx_allocation_pct, acquired_at
    )
    if config_error is not None:
        return await interaction.response.send_message(
            embed=config_error, ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    from services.asset_manager import AssetManager
    from models.asset import ContextType

    manager = AssetManager()

    updates: Dict[str, Any] = {}
    if quantity is not None:
        updates["quantity"] = quantity
    if avg_cost is not None:
        updates["avg_cost"] = avg_cost
    if asset_class is not None:
        updates["asset_class"] = asset_class.value
    if max_allocation_pct is not None:
        updates["max_allocation_pct"] = max_allocation_pct / 100.0
    if target_allocation_pct is not None:
        updates["target_allocation_pct"] = target_allocation_pct / 100.0
    if boxx_allocation_pct is not None:
        updates["boxx_allocation_pct"] = boxx_allocation_pct / 100.0
    if acquired_at is not None:
        updates["acquired_at"] = acquired_at

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


async def list_holdings_impl(interaction: discord.Interaction) -> Any:
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

    from market_analysis.dynamic_rollover import CORE_DEFENSE_ETF_SYMBOLS

    # 併發批次拉取各標的即時報價 (Semaphore(3) 上限，避免逐筆序列 await 拖慢回應)
    quote_sem = asyncio.Semaphore(3)

    async def _fetch_quote(sym: str) -> tuple[str, Dict[str, Any]]:
        async with quote_sem:
            quote = await market_data_service.get_quote(sym)
            return sym, (quote or {})

    unique_symbols = {a.symbol for a in assets}
    quotes_by_symbol = dict(
        await asyncio.gather(*[_fetch_quote(sym) for sym in unique_symbols])
    )

    holdings = []
    # 核心資金部署引擎 (Scenario 5) target_allocation_pct 總經自動建議值：
    # 僅供顯示參考，不會自動套用生效（target_allocation_pct 是嚴格 opt-in
    # 閘門，仍須使用者自行透過 /edit_holding 設定才會真正影響部署行為）。
    # 同一次 /list_holdings 呼叫內，跨多個未設定的 CORE 持倉只需評估一次。
    suggested_target_alloc: Optional[float] = None
    for a in assets:
        sym = a.symbol
        quote = quotes_by_symbol.get(sym, {})
        current_price = quote.get("c", 0.0)

        default_class = "CORE" if sym in CORE_DEFENSE_ETF_SYMBOLS else "SATELLITE"
        asset_class = a.metadata.get("asset_class") or default_class
        default_max_alloc = 1.0 if asset_class == "CORE" else 0.3
        max_alloc = a.metadata.get("max_allocation_pct")
        max_alloc = max_alloc if max_alloc is not None else default_max_alloc
        target_alloc = a.metadata.get("target_allocation_pct")

        h_data = {
            "id": a.id,
            "symbol": a.symbol,
            "quantity": a.metadata.get("quantity", 0.0),
            "avg_cost": a.metadata.get("avg_cost", 0.0),
            "weighted_delta": a.metadata.get("weighted_delta", 0.0),
            "current_price": current_price,
            "asset_class": asset_class,
            "max_allocation_pct": max_alloc,
            "target_allocation_pct": target_alloc,
            "boxx_allocation_pct": a.metadata.get("boxx_allocation_pct"),
            "acquired_at": a.metadata.get("acquired_at"),
        }
        if asset_class == "CORE" and target_alloc is None:
            if suggested_target_alloc is None:
                from market_analysis.index_microstructure import (
                    suggest_target_allocation_pct,
                )

                suggested_target_alloc = await suggest_target_allocation_pct()
            h_data["suggested_target_allocation_pct"] = suggested_target_alloc
        holdings.append(h_data)

    ctx = get_full_user_context(user_id)
    from cogs.embed_builder import create_holdings_embed

    embed = create_holdings_embed(holdings, ctx.capital)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def remove_holding_impl(interaction: discord.Interaction, symbol: str) -> Any:
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
