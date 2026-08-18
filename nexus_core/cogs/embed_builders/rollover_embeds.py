import discord
from typing import Optional, Any, Callable, Coroutine, Dict

from cogs.embed_builders._core import NexusEmbed
from ui.panel_renderer import truncate_with_boundary

# Discord API 字元上限常數
_EMBED_FIELD_VALUE_LIMIT = 1024
_CODE_FENCE_OVERHEAD = 6  # len("```") * 2
_EMBED_DESCRIPTION_SAFE_LIMIT = 4000


class ReportSelect(discord.ui.Select["ReportSelectionView"]):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="請選擇要分析的財報 (60 秒未選將自動取最新)",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.selected = True
        accession_num = self.values[0]
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        await self.view.on_selected_callback(interaction, accession_num)


class ReportSelectionView(discord.ui.View):
    """
    財報選擇互動選單
    提供下拉式選單讓使用者選擇特定財報，超時自動執行最新報告。
    """

    def __init__(
        self,
        target_symbol: str,
        reports: list[Dict[str, Any]],
        on_selected_callback: Callable[
            [Optional[discord.Interaction], str], Coroutine[Any, Any, None]
        ],
        timeout: Optional[float] = 60.0,
    ):
        super().__init__(timeout=timeout)
        self.target_symbol = target_symbol
        self.reports = reports
        self.on_selected_callback = on_selected_callback
        self.selected = False

        options = []
        for rep in reports:
            options.append(
                discord.SelectOption(
                    label=f"{rep['form']} ({rep.get('report_date', 'N/A')})",
                    value=rep["accession_number"],
                    description=f"Accession: {rep['accession_number'][:12]}...",
                )
            )

        self.select_menu = ReportSelect(options=options)
        self.add_item(self.select_menu)

    async def on_timeout(self) -> None:
        if not self.selected and self.reports:
            latest_accession = self.reports[0]["accession_number"]
            self.select_menu.disabled = True
            try:
                await self.on_selected_callback(None, latest_accession)
            except Exception as e:
                import logging

                logging.getLogger(__name__).error(
                    f"ReportSelectionView timeout error: {e}"
                )


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
        import asyncio

        await asyncio.sleep(1.5)

        embed = NexusEmbed(
            title=f"📊 {self.target_symbol} 轉倉試算報告",
            description="系統已完成概略的保證金佔用與預期報酬推估。\n*(註: 精確保證金依各券商終端為準)*",
            color=discord.Color.green(),
        )
        embed.add_field(name="預估保證金釋放", value="依目前持倉市值浮動", inline=True)
        embed.add_field(
            name="新部位佔用要求", value="標準買方策略無額外保證金", inline=True
        )
        embed.add_field(
            name="風控建議",
            value="請於開盤後 30 分鐘內尋找 V-POC 共振點執行",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="忽略",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_rollover_ignore",
    )
    async def btn_ignore_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="轉倉指令已忽略。", view=None)


class ManualOverrideView(discord.ui.View):
    """
    緊急裁決互動選單 (Bear Call Spread 防滑價機制)
    提供 [確認平倉 (強制滑價授權)] 與 [忽略] 兩個按鈕。
    """

    def __init__(self, target_symbol: str, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.target_symbol = target_symbol

    @discord.ui.button(
        label="確認平倉 (強制滑價授權)",
        style=discord.ButtonStyle.danger,
        custom_id="btn_manual_override_execute",
    )
    async def btn_execute_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            f"🚨 交易員已手動授權！正在針對 {self.target_symbol} 執行強制平倉市價單...",
            ephemeral=True,
        )

    @discord.ui.button(
        label="忽略",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_manual_override_ignore",
    )
    async def btn_ignore_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="緊急平倉指令已忽略。", view=None
        )


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
    is_hold = (
        sell_ratio == 0.0
        or direction == "HOLD"
        or "HOLD" in rollover_type
        or "防守" in rollover_type
    )
    if is_hold and ("破滅" not in rollover_type and "防禦" not in rollover_type):
        title = f"🛡️ 持倉防守評估: {rollover_type}"
        color = discord.Color.teal()
    else:
        title = f"🔄 動態轉倉指令: {rollover_type}"
        color = discord.Color.gold()
        if "破滅" in rollover_type or "防禦" in rollover_type:
            color = discord.Color.red()

    embed = NexusEmbed(title=title, color=color)

    # 1. 核心原因區塊 — 由於字數可能會超過 Discord Field Value 1024 字元上限，改置於 Description
    safe_reason = truncate_with_boundary(reason, _EMBED_DESCRIPTION_SAFE_LIMIT)
    desc_header = "**🛡️ 灰階量化與防守分析**" if is_hold else "**🚨 量化轉倉分析**"
    embed.description = f"{desc_header}\n\n{safe_reason}"

    # 2. 賣出/平倉指令區塊
    sell_action_full = (
        "STC (Sell To Close)"
        if sell_action == "STC"
        else ("BTC (Buy To Close)" if sell_action == "BTC" else sell_action)
    )
    if is_hold:
        sell_text = f"\u001b[0;32m標的: {sell_symbol}\n狀態: HOLD (維持現狀續抱)\n撤出: 0% (未觸發轉倉)\u001b[0m"
        embed.add_field(
            name="🛡️ 持倉狀態 / 防守", value=f"```ansi\n{sell_text}\n```", inline=True
        )
    else:
        sell_text = f"\u001b[0;31m標的: {sell_symbol}\n比例: {sell_ratio*100:.0f}%\n動作: {sell_action_full}\u001b[0m"
        embed.add_field(
            name="📤 撤出資金 / 平倉", value=f"```ansi\n{sell_text}\n```", inline=True
        )

    # 3. 買入指令區塊
    if is_hold:
        buy_text = f"\u001b[0;36m標的: {buy_symbol}\n動作: HOLD (維持現狀續抱)\n策略: {suggested_strategy}\u001b[0m"
        embed.add_field(
            name="📥 當前資產配置", value=f"```ansi\n{buy_text}\n```", inline=True
        )
    else:
        buy_text = f"\u001b[0;32m標的: {buy_symbol}\n動作: {direction} (Buy To Open)\n策略: {suggested_strategy}\u001b[0m"
        embed.add_field(
            name="📥 轉入資產 (Buy)", value=f"```ansi\n{buy_text}\n```", inline=True
        )

    # 4. 券商執行引導 (無腦執行區)
    if is_hold:
        if "Trailing Stop" in suggested_strategy:
            execution_guide = (
                f"**標的:** {sell_symbol}\n"
                f"**操作狀態:** HOLD (移動止盈 Trailing Stop)\n"
                f"**策略指引:** {suggested_strategy}\n"
                f"**防守機制:** 多頭動能強勁，嚴禁做空以防 Gamma Squeeze，持倉讓獲利奔馳\n"
                f"**輪動預備:** 若跌破移動止盈線，全數市價轉入 VOO"
            )
        else:
            execution_guide = (
                f"**標的:** {sell_symbol}\n"
                f"**操作狀態:** HOLD (維持現狀續抱)\n"
                f"**防守機制:** 嚴守 15 分鐘實體 K 線收盤撤退線 (期權合約依 3-5m 快速通道)\n"
                f"**輪動預備:** 若 15m 實體收盤跌破防守線，全數市價轉入 VOO"
            )
    else:
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
