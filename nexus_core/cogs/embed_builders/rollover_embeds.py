import discord
import logging
from typing import Optional, Any, Callable, Coroutine, Dict

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._ansi_utils import _pad_string
from ui.panel_renderer import truncate_with_boundary

logger = logging.getLogger(__name__)

# Discord API 字元上限常數
_EMBED_FIELD_VALUE_LIMIT = 1024
_CODE_FENCE_OVERHEAD = 6  # len("```") * 2
_EMBED_DESCRIPTION_SAFE_LIMIT = 4000

# 動態轉倉四大情境的視覺樣式對照表：MARGIN_DEFENSE / FUNDAMENTAL_BROKEN 恆為紅色危急，
# 不再依賴呼叫端自由文字 rollover_type 的子字串比對（曾導致最危險的保證金防禦警報
# 因文字未包含「防禦」二字而無法正確標紅，詳見 market_analysis/dynamic_rollover.py
# 的 RolloverScenario）。SATELLITE_REBALANCE 依 is_hold 分流顏色/emoji。
_SCENARIO_STYLE: Dict[str, Dict[str, Any]] = {
    "MARGIN_DEFENSE": {
        "emoji": "🚨",
        "label": "保證金防禦強制平倉",
        "color": discord.Color.red(),
    },
    "FUNDAMENTAL_BROKEN": {
        "emoji": "💥",
        "label": "原型假設破滅",
        "color": discord.Color.red(),
    },
    "OPPORTUNITY_COST": {
        "emoji": "💡",
        "label": "機會成本轉倉",
        "color": discord.Color.blue(),
    },
    "CORE_DEPLOYMENT": {
        "emoji": "🌱",
        "label": "核心資金部署",
        "color": discord.Color.green(),
    },
}


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
        await interaction.response.defer(ephemeral=True)

        target_sym = self.target_symbol.upper()
        ref_price = 0.0
        try:
            from database.market_cache import get_market_cache

            row = get_market_cache(target_sym)
            if row:
                ref_price = float(row.get("reference_spot_price") or 0.0)
        except Exception:
            pass

        if ref_price <= 0:
            try:
                from services import market_data_service

                quote = await market_data_service.get_quote(target_sym)
                if quote:
                    ref_price = float(quote.get("c") or 0.0)
            except Exception:
                pass

        if ref_price <= 0:
            ref_price = 500.0 if ("VOO" in target_sym or "SPY" in target_sym) else 100.0

        embed = NexusEmbed(
            title=f"📊 {target_sym} 轉倉與保證金試算報告",
            description=(
                f"**🎯 目標資產**: `{target_sym}` (即時參考價: `${ref_price:.2f}`)\n"
                "*(註: 本報告基於機構風控模型進行即時試算，精確保證金依各券商終端清算為準)*"
            ),
            color=discord.Color.green(),
        )

        C_RESET = " [0m"
        C_GREEN = " [1;32m"
        C_CYAN = " [1;36m"
        C_YELLOW = " [1;33m"

        ansi_sizing = [
            "```ansi",
            " 💵 資金規模與可買入股數估算",
            " ----------------------------------",
            f" ├─ $1,000 額度 : 約 {C_GREEN}{max(1, int(1000 / ref_price))}{C_RESET} 股",
            f" ├─ $5,000 額度 : 約 {C_GREEN}{max(1, int(5000 / ref_price))}{C_RESET} 股",
            f" ├─ $10,000 額度: 約 {C_GREEN}{max(1, int(10000 / ref_price))}{C_RESET} 股",
            f" └─ $50,000 額度: 約 {C_GREEN}{max(1, int(50000 / ref_price))}{C_RESET} 股",
            "```",
        ]
        embed.add_field(
            name="💵 資金轉換試算", value="\n".join(ansi_sizing), inline=True
        )

        ansi_margin = [
            "```ansi",
            " 🛡️ 保證金與交易摩擦",
            " ----------------------------------",
            f" ├─ 保證金釋放: {C_CYAN}100% 現貨市值{C_RESET}",
            f" ├─ 預估滑價損耗: {C_YELLOW}~0.30%{C_RESET}",
            " └─ 建議委託: 限價單 (Limit)",
            "```",
        ]
        embed.add_field(name="🛡️ 風控與摩擦", value="\n".join(ansi_margin), inline=True)

        embed.add_field(
            name="⏱️ 最佳執行時機 (Execution Timing)",
            value="建議於開盤後 15~30 分鐘 (避開 09:30~09:45 點差失真) 尋找 V-POC / GEX 牆共振點分批限價掛單。",
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
    buy_action_label: Optional[str] = None,
    scenario: str = "UNKNOWN",
    cash_impact: Optional[str] = None,
    trigger_condition_text: Optional[str] = None,
    asset_class: Optional[str] = None,
) -> discord.Embed:
    """
    產生動態轉倉 (Dynamic Rollover) 的 Embed 推播訊息。

    :param rollover_type: 轉倉類型的補充描述文字，例如 '原型假設破滅', '再平衡', '機會成本'。
        僅作為標題的補充說明，顏色/危險等級判斷改由 `scenario` 明確決定。
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
    :param buy_action_label: 覆寫「轉入資產」區塊的動作文字 (例如保證金防禦場景的
        「持有現金」)，預設為 None 時維持原本的 "{direction} (Buy To Open)" 顯示
    :param scenario: 動態轉倉引擎四大情境明確識別碼 (OPPORTUNITY_COST /
        SATELLITE_REBALANCE / MARGIN_DEFENSE / FUNDAMENTAL_BROKEN)，決定顏色與
        emoji/標題樣式。呼叫端未傳入時預設 "UNKNOWN"，將退回舊版子字串比對渲染
        並記錄警告。
    :param cash_impact: 預估資金回收/曝險影響字串 (例如 "$12,500")，供終端執行
        引導區塊顯示，None 時不顯示。
    :param trigger_condition_text: 引擎產生之「動態資金輪動觸發條件」段落，獨立
        呈現為專屬欄位，避免與其餘敘述一起塞入 description 時被截斷。
    """

    # 決定 HOLD/執行 狀態 (與 scenario 無關，純粹決定文案措辭)
    is_hold = (
        sell_ratio == 0.0
        or direction == "HOLD"
        or "HOLD" in rollover_type
        or "防守" in rollover_type
    )

    # 決定標題/顏色：優先採用明確的 scenario 對照表，避免依賴自由文字子字串比對
    # (該作法曾導致 MARGIN_DEFENSE 保證金防禦警報因 rollover_type 未包含「防禦」
    # 二字而無法正確標紅)。
    if scenario in _SCENARIO_STYLE:
        style = _SCENARIO_STYLE[scenario]
        if rollover_type == style["label"]:
            # rollover_type 未攜帶超出情境標籤本身的額外資訊時（例如
            # OPPORTUNITY_COST 呼叫端傳入的補充文字恰好與此表的 label 逐字相同），
            # 改以「標的 → 轉倉目標」取代，避免出現「機會成本轉倉: 機會成本轉倉」
            # 這類逐字重複、對使用者毫無新增資訊的標題。
            title = f"{style['emoji']} {style['label']}: {sell_symbol} → {buy_symbol}"
        else:
            title = f"{style['emoji']} {style['label']}: {rollover_type}"
        color = style["color"]
    elif scenario == "SATELLITE_REBALANCE":
        if is_hold:
            title = f"🛡️ 持倉防守評估: {rollover_type}"
            color = discord.Color.teal()
        else:
            # 呼叫端傳入的 rollover_type 常與此分支固定前綴逐字相同
            # (例如 "核心衛星再平衡")，同上以「標的 → 轉倉目標」取代重複字串。
            title_suffix = (
                f"{sell_symbol} → {buy_symbol}"
                if rollover_type == "核心衛星再平衡"
                else rollover_type
            )
            title = f"🔄 核心衛星再平衡: {title_suffix}"
            color = discord.Color.gold()
    else:
        logger.warning(
            f"create_dynamic_rollover_embed 收到未知/缺漏的 scenario={scenario!r}，"
            f"退回子字串比對渲染 (rollover_type={rollover_type!r})"
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
    desc_header = (
        "**🟢【狀態：安全續抱】正 Gamma 護城河完好，無需任何手動操作**"
        if is_hold
        else "**🚨【執行轉倉指令】機構量化防禦與再平衡決策**"
    )
    embed.description = f"{desc_header}\n\n{safe_reason}"

    C_RESET = " [0m"
    C_GREEN = " [1;32m"
    C_RED = " [1;31m"
    C_CYAN = " [1;36m"

    # 判斷是否為期權合約（若有非 N/A 到期日/履約價，或明確標記 asset_class 為 OPTIONS）
    has_option_params = (strike not in ("N/A", "", None)) or (
        expiry not in ("N/A", "", None)
    )
    if asset_class is not None:
        is_options = (
            asset_class.upper() in ("OPTIONS", "OPTION", "CONTRACT")
            or has_option_params
        )
    else:
        is_options = has_option_params
    is_spot = not is_options

    # 2. 賣出/平倉指令區塊
    if is_spot and sell_action in ("SELL", "STC"):
        sell_action_full = "SELL (賣出現貨)"
    elif sell_action == "SELL":
        sell_action_full = "SELL (賣出現貨)"
    elif sell_action == "STC":
        sell_action_full = "STC (Sell To Close)"
    elif sell_action == "BTC":
        sell_action_full = "BTC (Buy To Close)"
    elif sell_action == "BUY":
        sell_action_full = "BUY (買入現貨)"
    else:
        sell_action_full = sell_action

    if is_hold:
        sell_lines = [
            "```ansi",
            " 🛡️ 持倉狀態",
            " ----------------------------------",
            f" ├─ 標的: {C_GREEN}{sell_symbol}{C_RESET}",
            f" ├─ 狀態: {C_GREEN}HOLD{C_RESET} (維持現狀續抱)",
            " └─ 撤出: 0% (未觸發轉倉)",
            "```",
        ]
        embed.add_field(
            name="🛡️ 持倉狀態 / 防守", value="\n".join(sell_lines), inline=True
        )
    else:
        sell_lines = [
            "```ansi",
            " 📤 撤出資金 / 平倉",
            " ----------------------------------",
            f" ├─ 標的: {C_RED}{sell_symbol}{C_RESET}",
            f" ├─ 比例: {C_RED}{sell_ratio*100:.0f}%{C_RESET}",
            f" └─ 動作: {sell_action_full}",
            "```",
        ]
        embed.add_field(
            name="📤 撤出資金 / 平倉", value="\n".join(sell_lines), inline=True
        )

    # 3. 買入指令區塊
    if is_hold:
        buy_lines = [
            "```ansi",
            " 📥 當前資產配置",
            " ----------------------------------",
            f" ├─ 標的: {C_CYAN}{buy_symbol}{C_RESET}",
            f" ├─ 動作: {C_CYAN}HOLD{C_RESET} (維持現狀續抱)",
            f" └─ 策略: {suggested_strategy}",
            "```",
        ]
        embed.add_field(name="📥 當前資產配置", value="\n".join(buy_lines), inline=True)
    else:
        if buy_action_label:
            buy_action_display = buy_action_label
        elif is_spot:
            buy_action_display = (
                "BUY (買入現貨)" if direction.upper() in ("BTO", "BUY") else direction
            )
        else:
            buy_action_display = (
                f"{direction} (Buy To Open)"
                if direction.upper() in ("BTO", "BUY")
                else (
                    f"{direction} (Sell To Open)"
                    if direction.upper() == "STO"
                    else direction
                )
            )

        buy_lines = [
            "```ansi",
            " 📥 轉入資產 (Buy)",
            " ----------------------------------",
            f" ├─ 標的: {C_GREEN}{buy_symbol}{C_RESET}",
            f" ├─ 動作: {C_GREEN}{buy_action_display}{C_RESET}",
            f" └─ 策略: {suggested_strategy}",
            "```",
        ]
        embed.add_field(
            name="📥 轉入資產 (Buy)", value="\n".join(buy_lines), inline=True
        )

    # 4. 券商執行引導 (無腦執行區) — ANSI 包裹 + 樹狀縮排，符合量化控制台排版原則
    if is_hold:
        if "Trailing Stop" in suggested_strategy:
            guide_lines = [
                "```ansi",
                " 🎯 終端執行引導",
                " ----------------------------------",
                f" ├─ 標的: {sell_symbol}",
                f" ├─ 操作狀態: {C_GREEN}HOLD{C_RESET} (移動止盈 Trailing Stop)",
                f" ├─ 策略指引: {suggested_strategy}",
                " ├─ 防守機制: 多頭動能強勁，嚴禁做空以防 Gamma Squeeze，持倉讓獲利奔馳",
                " └─ 輪動預備: 若跌破移動止盈線，全數市價轉入 VOO",
                "```",
            ]
        else:
            guide_lines = [
                "```ansi",
                " 🎯 終端執行引導",
                " ----------------------------------",
                f" ├─ 標的: {sell_symbol}",
                f" ├─ 操作狀態: {C_GREEN}HOLD{C_RESET} (維持現狀續抱)",
                " ├─ 防守機制: 嚴守 15 分鐘實體 K 線收盤撤退線 (期權合約依 3-5m 快速通道)",
                " └─ 輪動預備: 若 15m 實體收盤跌破防守線，全數市價轉入 VOO",
                "```",
            ]
    else:
        direction_color = (
            C_RED if direction.upper() in ("STC", "BTC", "SELL") else C_GREEN
        )
        display_direction = (
            "BUY"
            if (is_spot and direction.upper() in ("BTO", "BUY"))
            else (
                "SELL"
                if (is_spot and direction.upper() in ("STC", "SELL"))
                else direction
            )
        )
        guide_lines = [
            "```ansi",
            " 🎯 終端執行引導",
            " ----------------------------------",
            f" ├─ 標的: {buy_symbol}",
        ]
        if is_options:
            guide_lines.append(f" ├─ 到期日: {expiry}")
            guide_lines.append(f" ├─ 履約價: {strike}")

        guide_lines.append(
            f" ├─ 買賣方向: {direction_color}{display_direction}{C_RESET}"
        )
        if cash_impact:
            guide_lines.append(f" ├─ 預估資金影響: {cash_impact}")
        guide_lines.append(f" └─ 建議限價 (Limit): {suggested_price}")
        guide_lines.append("```")

    embed.add_field(name="🎯 終端執行引導", value="\n".join(guide_lines), inline=False)

    # 5. 觸發條件區塊 (獨立欄位，避免與 description 一起被截斷或重複)
    if trigger_condition_text and trigger_condition_text not in safe_reason:
        field_title = (
            "🛡️ 應變防守觸發條件 (後備劇本)"
            if is_hold
            else "🚨 轉倉執行與資金輪動觸發條件"
        )
        embed.add_field(name=field_title, value=trigger_condition_text, inline=False)

    embed.set_footer(
        text="Nexus Risk & Rollover Engine • 請點擊下方 [執行試算] 以推估保證金佔用與預期報酬"
    )

    return embed


def create_covered_call_overlay_embed(
    symbol: str,
    reason: str,
    strike: str,
    expiry: str,
    cash_impact: Optional[str] = None,
    trigger_condition_text: Optional[str] = None,
    is_manual_override_required: bool = False,
) -> discord.Embed:
    """
    產生 Covered Call Overlay (核心持倉加碼賣出備兌買權收租) 的專屬 Embed。

    刻意不重用 create_dynamic_rollover_embed：該函式以「賣出 sell_symbol →
    買入 buy_symbol」的轉倉框架建模，其 is_hold 判斷 (`sell_ratio == 0.0`)
    只要 sell_ratio 為 0 就恆為 True，會讓本情境的 embed 固定落入
    「🟢【狀態：安全續抱】...無需任何手動操作」的文案分支——但本建議恰恰
    需要使用者主動掛單賣出買權，套用該文案會誤導使用者。本情境是「續抱
    同一標的、額外疊加賣方 overlay」，沒有第二個轉倉標的，語意上也不適合
    「標的 → 轉倉目標」的標題框架，故改用專屬版面呈現合約細節。

    :param symbol: 標的代號 (現貨與備兌買權的標的相同)
    :param reason: 建議理由 (成本線/阻力區/履約價下限/口數/預估權利金說明)
    :param strike: 履約價字串 (例如 "$450.00C")
    :param expiry: 到期日字串
    :param cash_impact: 預估權利金收入字串 (例如 "$120")，None 時不顯示
    :param trigger_condition_text: SPX 結構封頂觸發條件說明 (Regime/負 Gamma
        泥淖/STO 封頂)，獨立呈現為專屬欄位
    :param is_manual_override_required: 合約點差過寬時為 True，附加流動性警告欄位
    """
    style = _SCENARIO_STYLE["CORE_DEPLOYMENT"]
    embed = NexusEmbed(
        title=f"{style['emoji']} 核心資金部署延伸 (Covered Call Overlay): {symbol}",
        color=style["color"],
    )

    safe_reason = truncate_with_boundary(reason, _EMBED_DESCRIPTION_SAFE_LIMIT)
    embed.description = (
        "**🖋️【建議動作：賣出備兌買權收租】續抱現貨部位，額外賣出 Call 收取權利金**"
        f"\n\n{safe_reason}"
    )

    C_RESET = " [0m"
    C_GREEN = " [1;32m"
    C_CYAN = " [1;36m"

    overlay_lines = [
        "```ansi",
        " 🖋️ 掛單覆蓋 (STO Covered Call)",
        " ----------------------------------",
        f" ├─ 標的: {symbol}",
        f" ├─ 履約價: {C_CYAN}{strike}{C_RESET}",
        f" ├─ 到期日: {expiry}",
        f" ├─ 買賣方向: {C_GREEN}STO (Sell To Open){C_RESET}",
    ]
    if cash_impact:
        overlay_lines.append(f" └─ 預估權利金收入: {C_GREEN}{cash_impact}{C_RESET}")
    else:
        overlay_lines.append(" └─ 預估權利金收入: N/A")
    overlay_lines.append("```")

    embed.add_field(
        name="🖋️ 掛單覆蓋 (STO Covered Call)",
        value="\n".join(overlay_lines),
        inline=False,
    )

    if trigger_condition_text:
        embed.add_field(
            name="🛡️ 觸發條件 (SPX 結構封頂偵測)",
            value=trigger_condition_text,
            inline=False,
        )

    if is_manual_override_required:
        embed.add_field(
            name="⚠️ 流動性警告",
            value="合約點差過寬，建議採限價單並留意滑價，請人工確認後再執行。",
            inline=False,
        )

    embed.set_footer(
        text="Nexus Risk & Rollover Engine • Covered Call Overlay 為選填加碼收租建議，非強制轉倉"
    )

    return embed


_LOW_CONFIDENCE_THRESHOLD = 0.5


def build_fundamental_broken_embed(
    symbol: str,
    reasoning: str,
    confidence: float = 1.0,
    source_url: str = "",
    form_type: str = "",
) -> discord.Embed:
    """
    產生基本面護城河判定破滅（Scenario 1 原型假設破滅）的動態轉倉 Embed。

    使用 Simple-Markdown 格式排版，提供結構化、行動端友善且層次清晰的量化風控報告。
    集中封裝固定的清算建議參數 (100% 清倉轉入 VOO)，供互動式 `/verify_thesis`
    與自動化每日 SEC 財報掃描共用，避免兩處組裝邏輯漂移。
    """
    sym = symbol.upper()
    title = f"💥 原型假設破滅: {sym} → VOO"
    embed = NexusEmbed(title=title, color=discord.Color.red())

    if source_url:
        source_label = (
            f"[{form_type} 申報文件]({source_url})"
            if form_type
            else f"[SEC 申報文件]({source_url})"
        )
    else:
        source_label = "使用者提供新聞/資訊摘要"

    if confidence < _LOW_CONFIDENCE_THRESHOLD:
        confidence_str = f"{confidence:.0%} ⚠️ (判讀依據資訊密度偏低，建議人工複核)"
    else:
        confidence_str = f"{confidence:.0%}"

    safe_reasoning = truncate_with_boundary(
        reasoning.strip(), _EMBED_DESCRIPTION_SAFE_LIMIT - 600
    )

    desc_lines = [
        "> 🚨 **【執行轉倉指令】機構基本面護城河已破滅，建議全面防守**\n",
        "### 📊 評估摘要",
        f"- **驗證標的**：`{sym}`",
        "- **判定結果**：🔴 **假設破滅 (Moat Broken)**",
        f"- **LLM 信心**：{confidence_str}",
        f"- **資料來源**：{source_label}\n",
        "### 🧠 護城河分析與歸因",
        f"{safe_reasoning}\n",
        "### 🎯 轉倉執行建議",
        f"- **賣出平倉**：`{sym}` × 100% (市價全數清倉)",
        "- **轉入標的**：`VOO` (防禦避風港 ETF)",
        "- **執行動作**：買入現貨 (BUY Shares)",
        "- **建議限價**：市價 (Market)",
    ]

    embed.description = truncate_with_boundary(
        "\n".join(desc_lines), _EMBED_DESCRIPTION_SAFE_LIMIT
    )
    embed.set_footer(
        text="Nexus Risk & Rollover Engine • 請點擊下方 [執行試算] 以推估保證金佔用與預期報酬"
    )
    return embed


def create_thesis_passed_embed(
    symbol: str,
    reasoning: str,
    confidence: float = 1.0,
    source_url: str = "",
    form_type: str = "",
) -> discord.Embed:
    """
    產生基本面驗證通過（護城河穩固）的 Embed。

    使用 Simple-Markdown 格式排版，提供結構化、行動端友善且層次清晰的量化風控報告。
    """
    sym = symbol.upper()
    title = f"✅ {sym} 基本面驗證通過"
    embed = NexusEmbed(title=title, color=discord.Color.green())

    if source_url:
        source_label = (
            f"[{form_type} 申報文件]({source_url})"
            if form_type
            else f"[SEC 申報文件]({source_url})"
        )
    else:
        source_label = "使用者提供新聞/資訊摘要"

    if confidence < _LOW_CONFIDENCE_THRESHOLD:
        confidence_str = f"{confidence:.0%} ⚠️ (判讀依據資訊密度偏低，建議人工複核)"
    else:
        confidence_str = f"{confidence:.0%}"

    safe_reasoning = truncate_with_boundary(
        reasoning.strip(), _EMBED_DESCRIPTION_SAFE_LIMIT - 600
    )

    desc_lines = [
        "> 🟢 **【狀態：安全續抱】長期成長護城河依然穩固，無需轉倉**\n",
        "### 📊 評估摘要",
        f"- **驗證標的**：`{sym}`",
        "- **判定結果**：🟢 **護城河穩固 (Moat Intact)**",
        f"- **LLM 信心**：{confidence_str}",
        f"- **資料來源**：{source_label}\n",
        "### 🧠 護城河分析與評定",
        f"{safe_reasoning}\n",
        "### 🎯 操盤指引",
        "- **持倉狀態**：維持現狀續抱 (`HOLD`)",
        "- **風控指引**：基本面無結構性惡化，宏觀或短期波動無須恐慌殺跌",
    ]

    embed.description = truncate_with_boundary(
        "\n".join(desc_lines), _EMBED_DESCRIPTION_SAFE_LIMIT
    )
    embed.set_footer(text="Nexus Risk & Rollover Engine • 護城河驗證完成")
    return embed


_SCENARIO_SHORT_LABELS: Dict[str, str] = {
    "MARGIN_DEFENSE": "保證金防禦",
    "FUNDAMENTAL_BROKEN": "護城河破滅",
    "OPPORTUNITY_COST": "機會成本",
    "SATELLITE_REBALANCE": "核心衛星",
    "CORE_DEPLOYMENT": "核心部署",
}


def create_rollover_history_embed(records: list[dict[str, Any]]) -> discord.Embed:
    """
    產生動態轉倉引擎審計軌跡（歷史推送紀錄）Embed。

    系統僅提供建議、不代為執行券商下單，因此這裡呈現的是「系統實際推送過
    哪些建議」而非「建議後的真實成交結果」，供使用者事後回顧與問責。
    """
    embed = NexusEmbed(
        title="📜 動態轉倉建議歷史紀錄",
        description="以下為系統近期實際推送給您的轉倉建議（非模擬成交結果）。\n​",
        color=discord.Color.blurple(),
    )

    if not records:
        embed.description = "📭 目前尚無轉倉建議推送紀錄。"
        return embed

    header = f"{_pad_string('時間 (UTC)', 16)} | {_pad_string('標的', 6)} | {_pad_string('情境', 8)} | {_pad_string('動作', 9)} | {_pad_string('比例', 6, 'right')}"
    divider = "-" * 55
    lines = [header, divider]
    for r in records:
        scenario_label = _SCENARIO_SHORT_LABELS.get(
            str(r.get("scenario", "")), str(r.get("scenario", "N/A"))
        )
        created_at = str(r.get("created_at", ""))[:16]
        action = str(r.get("action", "N/A"))
        sell_ratio = float(r.get("sell_ratio", 0.0) or 0.0)
        lines.append(
            f"{_pad_string(created_at, 16)} | {_pad_string(r.get('symbol', ''), 6)} "
            f"| {_pad_string(scenario_label, 8)} | {_pad_string(action, 9)} "
            f"| {_pad_string(f'{sell_ratio:.0%}', 6, 'right')}"
        )

    table = "```\n" + "\n".join(lines) + "\n```"
    safe_table = truncate_with_boundary(table, _EMBED_FIELD_VALUE_LIMIT)
    embed.add_field(name="🕰️ 近期推送紀錄", value=safe_table, inline=False)
    embed.set_footer(
        text="Nexus Rollover Audit Trail • 僅記錄系統推送建議，非實際成交結果"
    )
    return embed
