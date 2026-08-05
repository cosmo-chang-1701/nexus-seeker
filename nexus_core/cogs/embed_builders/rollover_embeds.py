import discord
from typing import Optional

from cogs.embed_builders._core import NexusEmbed
from ui.panel_renderer import truncate_with_boundary

# Discord API 字元上限常數
_EMBED_FIELD_VALUE_LIMIT = 1024
_CODE_FENCE_OVERHEAD = 6  # len("```") * 2
_EMBED_DESCRIPTION_SAFE_LIMIT = 4000


class RolloverActionView(discord.ui.View):
    """
    動態轉倉互動選單
    提供 [執行試算] 與 [忽略] 兩個按鈕。
    """

    def __init__(self, target_symbol: str, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.target_symbol = target_symbol

    @discord.ui.button(
        label="執行試算",
        style=discord.ButtonStyle.green,
        custom_id="btn_rollover_execute",
    )
    async def btn_execute_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            f"正在為 {self.target_symbol} 執行轉倉試算引擎...", ephemeral=True
        )
        # 後續實作觸發試算引擎的邏輯...

    @discord.ui.button(
        label="忽略",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_rollover_ignore",
    )
    async def btn_ignore_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="轉倉指令已忽略。", view=None)


def create_dynamic_rollover_embed(
    rollover_type: str,
    sell_symbol: str,
    sell_ratio: float,
    buy_symbol: str,
    reason: str,
    suggested_strategy: str,
    suggested_price: str,
    strike: str,
    expiry: str,
    direction: str = "BTO",
    sell_action: str = "STC",
    combo_type: Optional[str] = None,
) -> discord.Embed:
    """
    產生動態轉倉 (Dynamic Rollover) 的 Embed 推播訊息。

    :param rollover_type: 轉倉類型，例如 '原型假設破滅', '再平衡', '機會成本'
    :param sell_symbol: 建議賣出的標的 (例如 AMD)
    :param sell_ratio: 建議賣出比例 (例如 1.0 表示全部，0.5 表示 50%)
    :param buy_symbol: 建議買入/轉倉的標的 (例如 SPY)
    :param reason: 轉倉原因
    :param suggested_strategy: 建議使用的期權策略 (例如 Bull Call Spread)
    :param suggested_price: 建議成交價位 (例如 $1.25)
    :param strike: 建議履約價
    :param expiry: 建議到期日
    :param direction: 買賣方向 (如 BTO, STC)
    :param sell_action: 賣出/平倉動作 (如 STC, BTC)，預設為 STC
    :param combo_type: 組合類型 (如 Net Debit, Net Credit)，供多腳位策略使用
    """

    # 決定顏色的前綴
    title = f"🔄 動態轉倉指令: {rollover_type}"
    color = discord.Color.gold()
    if "破滅" in rollover_type or "防禦" in rollover_type:
        color = discord.Color.red()

    embed = NexusEmbed(title=title, color=color)

    # 1. 核心原因區塊 — 截斷保護避免超過 Discord Field Value 1024 字元上限
    safe_reason = truncate_with_boundary(
        reason, _EMBED_FIELD_VALUE_LIMIT - _CODE_FENCE_OVERHEAD
    )
    embed.add_field(
        name="🚨 轉倉動機 (Reason)", value=f"```{safe_reason}```", inline=False
    )

    # 2. 賣出/平倉指令區塊
    sell_action_full = (
        "STC (Sell To Close)"
        if sell_action == "STC"
        else ("BTC (Buy To Close)" if sell_action == "BTC" else sell_action)
    )
    sell_text = f"\u001b[0;31m標的: {sell_symbol}\n比例: {sell_ratio*100:.0f}%\n動作: {sell_action_full}\u001b[0m"
    embed.add_field(
        name="📤 撤出資金 / 平倉", value=f"```ansi\n{sell_text}\n```", inline=True
    )

    # 3. 買入指令區塊
    buy_text = f"\u001b[0;32m標的: {buy_symbol}\n動作: {direction} (Buy To Open)\n策略: {suggested_strategy}\u001b[0m"
    embed.add_field(
        name="📥 轉入資產 (Buy)", value=f"```ansi\n{buy_text}\n```", inline=True
    )

    # 4. 券商執行引導 (無腦執行區)
    execution_guide = (
        f"**標的:** {buy_symbol}\n"
        f"**到期日:** {expiry}\n"
        f"**履約價:** {strike}\n"
        f"**買賣方向:** {direction}\n"
        f"**建議限價 (Limit):** {suggested_price}"
    )
    if combo_type:
        execution_guide += f"\n**組合類型:** {combo_type}"

    embed.add_field(name="🎯 終端執行引導", value=execution_guide, inline=False)

    embed.set_footer(
        text="Nexus Risk & Rollover Engine • 請點擊下方 [執行試算] 以推估保證金佔用與預期報酬"
    )

    return embed


def create_thesis_passed_embed(
    symbol: str,
    reasoning: str,
    source_url: str = "",
) -> discord.Embed:
    """
    產生基本面驗證通過（護城河穩固）的 Embed。

    使用 truncate_with_boundary 防止 Discord API 字元溢出，
    將 reasoning 放入 Embed description（上限 4096 字元）而非
    message content（上限 2000 字元）。
    """
    embed = NexusEmbed(
        title=f"✅ {symbol} 基本面驗證通過",
        color=discord.Color.green(),
    )

    safe_reasoning = truncate_with_boundary(reasoning, _EMBED_DESCRIPTION_SAFE_LIMIT)
    embed.description = f"護城河評估結果：依然穩固。無需轉倉。\n\n> {safe_reasoning}"

    if source_url:
        embed.add_field(
            name="🔗 參照資料來源",
            value=source_url,
            inline=False,
        )

    return embed
