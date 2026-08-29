"""動態轉倉引擎相關指令邏輯：基本面假設驗證與轉倉歷史查詢。

`_execute_verify_thesis_logic` 保留為 `TerminalCog` 的實體方法（見 `cog.py`），
本模組的 `verify_thesis_impl` 因此接收 `cog` 實體並透過 `cog._execute_verify_thesis_logic(...)`
呼叫，而非直接呼叫下方的 `execute_verify_thesis_logic`：
`tests/unit/test_terminal_verify_thesis.py` 會直接對 `cog._execute_verify_thesis_logic`
做 instance-level monkeypatch，若改為直接呼叫本模組函式將繞過該 patch。
"""

from typing import Any, Optional

import discord

import database


async def execute_verify_thesis_logic(
    interaction: discord.Interaction | None,
    symbol: str,
    combined_text: str,
    source_url: str,
    target_message: discord.Message | None = None,
    form_type: str = "",
    sections: dict[str, str] | None = None,
) -> None:
    from market_analysis.dynamic_rollover import DynamicRolloverEngine
    from cogs.embed_builders.rollover_embeds import (
        build_fundamental_broken_embed,
        create_thesis_passed_embed,
        RolloverActionView,
    )

    engine = DynamicRolloverEngine()
    result = await engine.evaluate_fundamental_thesis(
        symbol, combined_text, form_type=form_type, sections=sections or {}
    )

    async def _send_or_edit(
        content: str,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        if interaction:
            if embed:
                await interaction.edit_original_response(
                    content=content, embed=embed, view=view
                )
            else:
                await interaction.edit_original_response(content=content)
        elif target_message:
            if embed:
                await target_message.edit(content=content, embed=embed, view=view)
            else:
                await target_message.edit(content=content)

    if not result:
        from services.llm_service import is_memory_safe

        if not is_memory_safe():
            await _send_or_edit("⚠️ 記憶體防禦機制觸發 (RAM > 85%)，已中止驗證。")
        else:
            await _send_or_edit("⚠️ LLM 呼叫失敗，已中止驗證。")
        return

    if result.is_broken:
        embed = build_fundamental_broken_embed(
            symbol=symbol.upper(),
            reasoning=result.reasoning,
            confidence=result.confidence,
            source_url=source_url,
            form_type=form_type,
        )
        view = RolloverActionView(target_symbol=symbol.upper())
        await _send_or_edit("", embed=embed, view=view)
    else:
        embed = create_thesis_passed_embed(
            symbol=symbol.upper(),
            reasoning=result.reasoning,
            confidence=result.confidence,
            source_url=source_url,
            form_type=form_type,
        )
        await _send_or_edit("", embed=embed)


async def verify_thesis_impl(
    cog: Any,
    interaction: discord.Interaction,
    symbol: str,
    news_context: Optional[str] = None,
) -> None:
    """手動觸發動態轉倉：情境 1 (原型假設破滅)"""
    await interaction.response.defer(ephemeral=True)
    from services.fundamental_service import (
        get_fundamental_context,
        get_fundamental_reports_list,
    )
    from cogs.embed_builders.rollover_embeds import ReportSelectionView

    if news_context:
        await interaction.edit_original_response(
            content=f"🔍 正在根據您提供的新聞摘要對 `{symbol.upper()}` 進行護城河壓力測試，請稍候..."
        )
        combined_text = f"[使用者補充新聞/資訊]:\n{news_context}\n"
        await cog._execute_verify_thesis_logic(
            interaction, symbol, combined_text, "", form_type="NEWS", sections=None
        )
        return

    reports = await get_fundamental_reports_list(symbol)
    if not reports:
        await interaction.edit_original_response(
            content=f"🔍 正在透過邊緣節點下載 `{symbol.upper()}` 最新 SEC 財報，請稍候..."
        )
        fundamental_data = await get_fundamental_context(symbol)
        if fundamental_data and "text" in fundamental_data:
            combined_text = f"[SEC 財報段落]:\n{fundamental_data['text']}\n\n"
            source_url = fundamental_data.get("source_url", "")
            form_type = fundamental_data.get("form_type", "")
            sections = fundamental_data.get("sections", {})
            await cog._execute_verify_thesis_logic(
                interaction,
                symbol,
                combined_text,
                source_url,
                form_type=form_type,
                sections=sections,
            )
        else:
            err_msg = (
                fundamental_data.get("error", "未知錯誤")
                if fundamental_data
                else "無法連線至 Edge API"
            )
            await interaction.edit_original_response(
                content=f"⚠️ 無法自動獲取 `{symbol.upper()}` 的財報資料 ({err_msg})，驗證中止。"
            )
        return

    # 找到多份報告，顯示下拉選單
    async def on_selected(
        select_interaction: discord.Interaction | None, accession_number: str
    ) -> None:
        try:
            original = await interaction.original_response()
            if select_interaction:
                await original.edit(
                    content=f"🔍 正在獲取財報 `{accession_number}` 進行驗證，請稍候...",
                    view=None,
                )
            else:
                await original.edit(
                    content=f"⚠️ 選擇超時，自動獲取最新財報 `{accession_number}` 進行驗證，請稍候...",
                    view=None,
                )
        except Exception:
            pass

        fundamental_data = await get_fundamental_context(
            symbol, accession_number=accession_number
        )
        target_msg = await interaction.original_response()
        if fundamental_data and "text" in fundamental_data:
            combined_text = f"[SEC 財報段落]:\n{fundamental_data['text']}\n\n"
            source_url = fundamental_data.get("source_url", "")
            form_type = fundamental_data.get("form_type", "")
            sections = fundamental_data.get("sections", {})
            await cog._execute_verify_thesis_logic(
                None,
                symbol,
                combined_text,
                source_url,
                target_message=target_msg,
                form_type=form_type,
                sections=sections,
            )
        else:
            err_msg = (
                fundamental_data.get("error", "未知錯誤")
                if fundamental_data
                else "無法連線至 Edge API"
            )
            await target_msg.edit(
                content=f"⚠️ 無法獲取指定的財報資料 ({err_msg})，驗證中止。"
            )

    view = ReportSelectionView(symbol, reports, on_selected, timeout=60.0)
    await interaction.edit_original_response(
        content="🔍 找到多份近期財報，請選擇要驗證的報告：", view=view
    )


async def rollover_history_impl(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    from cogs.embed_builders.rollover_embeds import create_rollover_history_embed

    records = database.get_rollover_audit_log(interaction.user.id)
    embed = create_rollover_history_embed(records)
    await interaction.followup.send(embed=embed, ephemeral=True)
