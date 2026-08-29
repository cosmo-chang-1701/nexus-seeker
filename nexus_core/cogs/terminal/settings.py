"""帳戶設定、通知偏好與 WTI 警報設定相關指令邏輯。"""

from typing import Any, Optional

import discord

import database
from cogs.embed_builder import create_error_embed, create_info_embed
from cogs.settings_ui import AccountSettingsView, NotificationSettingsView


async def update_settings_impl(
    interaction: discord.Interaction,
    capital: Optional[float] = None,
    risk_limit: Optional[float] = None,
    enable_vtr: Optional[bool] = None,
    enable_psq_watchlist: Optional[bool] = None,
    polymarket_threshold: Optional[float] = None,
    polymarket_use_llm: Optional[bool] = None,
    polymarket_slippage: Optional[float] = None,
    monthly_expense: Optional[float] = None,
    tax_reserve_rate: Optional[float] = None,
    cash_reserve: Optional[float] = None,
) -> Any:
    """喚起帳戶設定互動式面板，或直接配置特定參數"""
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    # 判斷是否為無參數調用（互動式表單模式）
    is_interactive = all(
        v is None
        for v in [
            capital,
            risk_limit,
            enable_vtr,
            enable_psq_watchlist,
            polymarket_threshold,
            polymarket_use_llm,
            polymarket_slippage,
            monthly_expense,
            tax_reserve_rate,
            cash_reserve,
        ]
    )

    if is_interactive:
        # 1. 互動式表單模式 (無參數調用)
        view = AccountSettingsView(user_id)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        return

    # 2. 直接更新模式 (參數化調用，供集成測試或腳本使用)
    updates = []
    db_updates = {}

    if capital is not None:
        return await interaction.followup.send(
            embed=create_error_embed(
                "總資金已改為自動計算，無法手動配置。", title="系統錯誤"
            ),
            ephemeral=True,
        )

    if risk_limit is not None:
        if 1.0 <= risk_limit <= 50.0:
            db_updates["risk_limit"] = risk_limit
            updates.append(f"🛡️ 風險限制: `{risk_limit}%`")
        else:
            return await interaction.followup.send(
                embed=create_error_embed(
                    "風險限制需介於 1.0% 至 50.0% 之間", title="系統錯誤"
                ),
                ephemeral=True,
            )

    if enable_vtr is not None:
        db_updates["enable_vtr"] = enable_vtr
        updates.append(f"👻 虛擬交易室 (VTR): `{'開啟' if enable_vtr else '關閉'}`")

    if enable_psq_watchlist is not None:
        db_updates["enable_psq_watchlist"] = enable_psq_watchlist
        updates.append(
            f"⚡ PowerSqueeze 追蹤: `{'開啟' if enable_psq_watchlist else '關閉'}`"
        )

    if polymarket_threshold is not None:
        db_updates["polymarket_threshold"] = polymarket_threshold
        status = (
            f"`${polymarket_threshold:,.0f}`" if polymarket_threshold > 0 else "`關閉`"
        )
        updates.append(f"🐋 Polymarket 監控: {status}")

    if polymarket_use_llm is not None:
        db_updates["polymarket_use_llm"] = polymarket_use_llm
        updates.append(
            f"🧠 Polymarket AI 分析: `{'開啟' if polymarket_use_llm else '關閉'}`"
        )

    if polymarket_slippage is not None:
        if 0.1 <= polymarket_slippage <= 10.0:
            db_updates["polymarket_slippage"] = polymarket_slippage
            updates.append(f"🌊 Polymarket 滑價門檻: `{polymarket_slippage}%`")
        else:
            return await interaction.followup.send(
                embed=create_error_embed(
                    "滑價門檻需介於 0.1% 至 10.0% 之間", title="系統錯誤"
                ),
                ephemeral=True,
            )

    if monthly_expense is not None:
        if monthly_expense >= 0:
            db_updates["monthly_expense"] = monthly_expense
            updates.append(f"💸 每月支出預算: `${monthly_expense:,.0f}`")
        else:
            return await interaction.followup.send(
                embed=create_error_embed("支出預算不能為負數", title="系統錯誤"),
                ephemeral=True,
            )

    if tax_reserve_rate is not None:
        if 0.0 <= tax_reserve_rate <= 1.0:
            db_updates["tax_reserve_rate"] = tax_reserve_rate
            updates.append(f"🏦 稅務預留比例: `{tax_reserve_rate:.1%}`")
        else:
            return await interaction.followup.send(
                embed=create_error_embed(
                    "稅務比例需介於 0.0 與 1.0 之間", title="系統錯誤"
                ),
                ephemeral=True,
            )

    if cash_reserve is not None:
        if cash_reserve >= 0:
            db_updates["cash_reserve"] = cash_reserve
            updates.append(f"💰 現金儲備: `${cash_reserve:,.0f}`")
        else:
            return await interaction.followup.send(
                embed=create_error_embed("現金儲備不能為負數", title="系統錯誤"),
                ephemeral=True,
            )

    success = database.upsert_user_config(user_id, **db_updates)
    if not success:
        return await interaction.followup.send(
            embed=create_error_embed("設定失敗，請稍後再試。", title="系統錯誤"),
            ephemeral=True,
        )

    msg = "✅ **帳戶設定已更新**：\n" + "\n".join(updates)
    await interaction.followup.send(
        embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
    )


async def notif_settings_impl(interaction: discord.Interaction) -> Any:
    """喚起自訂通知設定面板"""
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    view = NotificationSettingsView(user_id)
    embed = view.build_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def wti_config_impl(interaction: discord.Interaction) -> Any:
    """喚起 WTI 原油警報閾值設定彈窗"""
    from database.wti_config import get_wti_config
    from cogs.settings_ui import WtiConfigModal

    config = await get_wti_config(interaction.user.id)
    modal = WtiConfigModal(config)
    await interaction.response.send_modal(modal)
