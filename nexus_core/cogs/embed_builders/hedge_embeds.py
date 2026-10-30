"""對沖警報與系統監控 Embed 建構函式。

包含：
- create_event_impact_embed：事件風險 What-if 模擬
- create_hedge_settlement_embed：對沖結算完成
- create_hedge_list_embed：對沖警報列表
- create_hedge_alert_embed：自動化對沖警報
- create_proactive_event_alert_embed：重大事件主動預警
- create_memory_alert_embed：記憶體不足警報
- create_polymarket_whale_alert_embed：Polymarket 巨鯨戰報
- create_option_defense_alert_embed：期權防禦與結算
- create_volatility_risk_alert_embed：波動率風險警報
"""

import discord
from cogs.embed_builders._core import NexusEmbed

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._ansi_utils import (
    _to_float_or_none,
    _truncate_with_boundary,
    fmt_or_dash,
)
from cogs.embed_builders._embed_helpers import (
    _ANSI_0,
    _ANSI_G,
    _ANSI_R,
    _ANSI_W,
    _add_ansi_field_safely,
    _ansi_tree_lines,
    split_embed_by_fields,
)


def create_event_impact_embed(
    symbol: str,
    vol_move: float,
    total_delta: float,
    total_vanna: float,
    adjusted_delta: float,
    delta_shift: float,
    exposure_shift_dollars: float,
) -> discord.Embed:
    """建立事件風險 What-if 模擬 Embed。"""
    embed = NexusEmbed(
        title=f"🎲 【 {symbol} 事件風險模擬 (What-if) 】",
        description=f"假設波動率變動 `{vol_move}%` 時，部位 Greeks 的動態偏移：",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    _add_ansi_field_safely(
        embed,
        "📊 Greeks 偏移模擬",
        _ansi_tree_lines(
            [
                f"目前 Beta-Weighted Delta: {_ANSI_W}{total_delta:.2f}{_ANSI_0}",
                f"目前 Vanna (曝險變化率) : {_ANSI_W}{total_vanna:.2f}{_ANSI_0}",
                f"預期 Hidden Delta       : {_ANSI_W}{adjusted_delta:.2f}{_ANSI_0}",
                f"Delta 偏移量            : {_ANSI_W}{delta_shift:+.2f}{_ANSI_0}",
                f"等值曝險變動 (USD)      : {_ANSI_W}${exposure_shift_dollars:,.2f}{_ANSI_0}",
            ]
        ),
    )

    is_danger = abs(adjusted_delta) > 100
    risk_status = "🔴 危險" if is_danger else "🟢 安全"
    risk_color = _ANSI_R if is_danger else _ANSI_G
    _add_ansi_field_safely(
        embed,
        "🚦 風險狀態判定",
        _ansi_tree_lines([f"{risk_color}{risk_status}{_ANSI_0}"]),
    )
    embed.set_footer(text="NRO Vanna Simulation")
    return embed


def create_hedge_settlement_embed(
    alert_id: int, hedge_instrument: str, executed_quantity: int
) -> discord.Embed:
    """建立對沖結算完成 Embed。"""
    embed = NexusEmbed(
        title="✅ 對沖結算完成",
        description=f"已成功記錄警報 `#{alert_id}` 的對沖執行紀錄。",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    _add_ansi_field_safely(
        embed,
        "🧾 執行紀錄",
        _ansi_tree_lines(
            [
                f"執行標的: {_ANSI_W}{hedge_instrument}{_ANSI_0}",
                f"執行數量: {_ANSI_W}{executed_quantity}{_ANSI_0}",
            ]
        ),
    )
    embed.set_footer(text="數據已同步至 SQLite 持久化層，可用於歸因分析。")
    return embed


def create_hedge_list_embed(rows: List[Any]) -> discord.Embed:
    """建立最近對沖警報列表 Embed。"""
    embed = NexusEmbed(
        title="📜 最近對沖警報列表",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    content: List[str] = []
    for row in rows:
        status_emoji = (
            "⏳" if row[3] == "PENDING" else "✅" if row[3] == "EXECUTED" else "❌"
        )
        content.append(
            f"`#{row[0]}` | {status_emoji} | VIX: `{row[1]:.2f}` | 建議: `{row[2]}`股 | {row[4][:16]}"
        )

    embed.description = "\n".join(content) if content else "📭 目前無對沖警報紀錄。"
    embed.set_footer(text="Hedge Ledger")
    return embed


def create_hedge_alert_embed(
    *,
    vix: float,
    stage_move: int,
    tier_name: str,
    tier_emoji: str,
    color_hex: int,
    total_beta_delta: float,
    adjusted_delta: float,
    total_vega: float,
    hedge_quantity: int,
    instruction_text: str,
    narration: str,
    alert_id: int,
    poly_snapshot: Optional[List[Dict[str, Any]]] = None,
) -> discord.Embed:
    """建立自動化對沖警報 Embed。"""
    embed = NexusEmbed(
        title="🚨 【戰位報告：自動化對沖警報】",
        description=f"**警報等級：** {tier_emoji} {tier_name} (移動 `{stage_move:+} 階`)",
        color=discord.Color(color_hex),
        timestamp=datetime.now(timezone.utc),
    )

    _add_ansi_field_safely(
        embed,
        "📊 風險指標",
        _ansi_tree_lines(
            [
                f"即時 VIX     : {_ANSI_W}{vix:.2f}{_ANSI_0}",
                f"淨 Delta     : {_ANSI_W}{total_beta_delta:+.1f}{_ANSI_0}",
                f"調整後 Delta : {_ANSI_W}{adjusted_delta:+.1f}{_ANSI_0} (Hidden Delta)",
                f"Vega 脆弱性  : {_ANSI_W}{total_vega:+.2f}{_ANSI_0}",
            ]
        ),
    )

    if poly_snapshot:
        snapshot_lines: List[str] = []
        for event in poly_snapshot:
            question = str(event.get("question", "N/A"))
            truncated = _truncate_with_boundary(question, 40)
            odds = event.get("odds_distribution", [])

            def _odds_pct(item: Dict[str, Any]) -> str:
                o = _to_float_or_none(item.get("odds"))
                return fmt_or_dash(
                    o * 100 if o is not None else None,
                    ".0f",
                    suffix="%",
                    placeholder="--%",
                )

            odds_str = " | ".join(
                f"{item.get('outcome')}: {_ANSI_W}{_odds_pct(item)}{_ANSI_0}"
                for item in odds[:2]
            )
            snapshot_lines.append(f"{truncated}｜{odds_str}")

        if snapshot_lines:
            _add_ansi_field_safely(
                embed,
                "🌐 [快取快照] Polymarket 即時機率",
                _ansi_tree_lines(snapshot_lines),
            )

    embed.add_field(name="🤖 AI 風險敘述", value=f"*{narration}*", inline=False)
    _add_ansi_field_safely(
        embed,
        "🛡️ 對沖建議指令",
        _ansi_tree_lines(
            [f"{_ANSI_W}{ln}{_ANSI_0}" for ln in instruction_text.split("\n")]
        ),
    )
    _add_ansi_field_safely(
        embed,
        "📈 預期效果",
        _ansi_tree_lines(
            [
                f"執行後淨 Delta 將回歸至 {_ANSI_W}{adjusted_delta + (hedge_quantity * -1.0):+.1f}{_ANSI_0} 附近，"
                "顯著降低系統性回撤風險。"
            ]
        ),
    )
    embed.set_footer(text=f"Battle Station | Alert ID: {alert_id}")
    return embed


def create_proactive_event_alert_embed(events: List[Any]) -> List[discord.Embed]:
    """建立重大事件主動預警 Embed。"""
    embed = NexusEmbed(
        title="🛡️ 【 預警：重大事件即時防護 】",
        description=(
            "偵測到您的持倉標的即將迎來重大波動事件；以下 NRO 指令已依事件類型、"
            "剩餘時間與目前持倉風險狀態調整。"
        ),
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )

    if not events:
        embed.description = "📭 目前沒有偵測到即將來臨的重大波動事件。"
        embed.set_footer(text="Proactive Event Monitor")
        return [embed]

    for event in events:
        if isinstance(event, dict):
            name = str(event.get("name", "⚠️ 重大事件"))
            tte_hours = _to_float_or_none(event.get("tte_hours"))
            risk_status = str(event.get("risk_status", "持倉風險狀態暫不可用"))
            instruction = str(event.get("instruction", "請先降低曝險並觀察事件落地。"))
        else:
            if event.type == "ECONOMIC":
                name = f"🔴 經濟數據: {event.event}"
                instruction = "增加 Vanna 權重，縮減賣方曝險。"
            else:
                name = f"📊 財報預警: {event.symbol}"
                instruction = "已啟動 IV Crush 防護機制。"
            tte_hours = _to_float_or_none(getattr(event, "tte_hours", None))
            risk_status = "持倉風險狀態未提供，請搭配組合 Greeks 自行覆核。"

        # Determine colors for TTE
        # TTE 缺值時不判斷緊急度（不上紅），改中性灰
        if tte_hours is None:
            tte_color = "\u001b[1;30m"
        elif tte_hours <= 12.0:
            tte_color = "\u001b[1;31m"  # Urgent Red
        elif tte_hours <= 24.0:
            tte_color = "\u001b[1;33m"  # Warning Yellow
        else:
            tte_color = "\u001b[1;36m"  # Safe Cyan

        # Determine colors for Risk Status
        risk_str = str(risk_status)
        if any(
            w in risk_str
            for w in ["危險", "警告", "高", "🚨", "⚠️", "賣方偏重", "短 Gamma"]
        ):
            risk_color = "\u001b[1;31m"  # High Risk Red
        elif any(w in risk_str for w in ["安全", "低", "良好", "正常"]):
            risk_color = "\u001b[1;32m"  # Low Risk Green
        else:
            risk_color = "\u001b[1;33m"  # Neutral Yellow

        event_lines = [
            "```ansi",
            " 距離發布 (Time to Event)",
            f" └─ 剩餘時間: {tte_color}{fmt_or_dash(tte_hours, '.1f')} 小時\u001b[0m",
            " 持倉風險狀態 (Position Risk Status)",
            f" └─ 狀態: {risk_color}{risk_status}\u001b[0m",
            " NRO 指令 (NRO Instruction)",
            f" └─ 指令: \u001b[1;36m{instruction}\u001b[0m",
            "```",
        ]
        value = "\n".join(event_lines)

        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Proactive Event Monitor")
    return split_embed_by_fields(embed)


def create_memory_alert_embed(
    total_usage: float,
    process_memory_mb: float,
    sma_cache_size: int,
    ema_cache_size: int,
    swap_usage: float = 0.0,
    source: str = "Droplet (主節點)",
) -> discord.Embed:
    """建立記憶體不足緊急警報 Embed。"""
    embed = NexusEmbed(
        title=f"🆘 【系統緊急警報：記憶體不足】 - {source}",
        description=(
            f"{source} 記憶體與 Swap 使用量已達臨界值 (RAM: `{total_usage}%`, Swap: `{swap_usage}%`)，"
            "可能導致程序被 OOM Killer 終止。"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    mem_rows = [f"當前總占用: {_ANSI_W}{total_usage}%{_ANSI_0}"]
    if swap_usage > 0:
        mem_rows.append(f"Swap 占用 : {_ANSI_W}{swap_usage}%{_ANSI_0}")
    if process_memory_mb > 0:
        mem_rows.append(f"程序占用 (RSS): {_ANSI_W}{process_memory_mb:.1f} MB{_ANSI_0}")
    mem_rows.append(
        f"快取消費者 SMA/EMA: {_ANSI_W}{sma_cache_size}/{ema_cache_size}{_ANSI_0} 筆"
    )
    _add_ansi_field_safely(embed, "🖥️ 記憶體概況", _ansi_tree_lines(mem_rows))
    embed.set_footer(text="建議重啟服務或增加 Swap 分區。")
    return embed


def create_power_alert_embed(
    percent: float,
    secsleft: Optional[float],
    source: str = "邊緣節點",
    window_minutes: Optional[int] = None,
) -> discord.Embed:
    """建立邊緣節點電力不足警報 Embed。

    secsleft 為 core 依實測耗電速率推估的剩餘秒數（None＝取樣不足）；
    不採用 OS 回報值（macOS 恆為 1200 分鐘）。
    """
    if secsleft is None:
        time_left_str = "估算中（未插電取樣不足）"
    else:
        mins = int(secsleft // 60)
        if mins <= 0:
            time_left_str = "即將耗盡"
        else:
            if mins >= 60:
                dur = f"{mins // 60} 小時 {mins % 60} 分鐘"
            else:
                dur = f"{mins} 分鐘"
            basis = (
                f"（依近 {window_minutes} 分鐘耗電速率推估）"
                if window_minutes is not None
                else "（依實測耗電速率推估）"
            )
            time_left_str = f"約 {dur}{basis}"

    embed = NexusEmbed(
        title=f"🔋 【系統緊急警報：電力不足】 - {source}",
        description=(
            f"{source} 目前處於未插電狀態，且剩餘電量過低。\n"
            "若節點斷電，將無法執行依賴該節點的自動化任務（如 Playwright 爬蟲、SEC 財報解析等）。"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    _add_ansi_field_safely(
        embed,
        "🔋 電力狀態",
        _ansi_tree_lines(
            [
                f"剩餘電量    : {_ANSI_W}{percent}%{_ANSI_0}",
                f"預估剩餘時間: {_ANSI_W}{time_left_str}{_ANSI_0}",
            ]
        ),
    )

    return embed


def create_polymarket_whale_alert_embed(
    *,
    intent_emoji: str,
    intent_label: str,
    market_question: str,
    usd_value: float,
    dynamic_threshold: float,
    win_rate: float,
    is_high_conviction: bool,
    is_bullish: bool,
    summary: str,
    event_slug: Optional[str] = None,
    uoa_correlation: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 Polymarket 巨鯨戰報 Embed。"""
    embed = NexusEmbed(
        title="🐳 警報：巨鯨交易異動" + (" (高信心訊號)" if is_high_conviction else ""),
        color=(
            discord.Color.gold()
            if is_high_conviction
            else (discord.Color.blue() if is_bullish else discord.Color.red())
        ),
        timestamp=datetime.now(timezone.utc),
    )

    _add_ansi_field_safely(
        embed,
        "📊 交易概況",
        _ansi_tree_lines(
            [
                f"市場問題  : {_ANSI_W}{market_question}{_ANSI_0}",
                f"交易金額  : {_ANSI_W}${usd_value:,.2f}{_ANSI_0}",
                f"流動性倍數: {_ANSI_W}{usd_value / dynamic_threshold:.2f}x{_ANSI_0}",
                f"當前勝率  : {_ANSI_W}{win_rate:.1f}%{_ANSI_0}",
            ]
        ),
    )

    if uoa_correlation:
        uoa = uoa_correlation["uoa"]
        classification = uoa_correlation["classification"]
        _add_ansi_field_safely(
            embed,
            f"🔍 UOA 關聯偵測 ({uoa['symbol']})",
            _ansi_tree_lines(
                [
                    f"合約: {_ANSI_W}{uoa['expiry']}{_ANSI_0} {_ANSI_W}${uoa['strike']}{_ANSI_0} {uoa['type']}",
                    f"性質: {_ANSI_W}{classification['classification']}{_ANSI_0} (信心: {_ANSI_W}{classification['confidence']:.2f}{_ANSI_0})",
                    f"理由: {classification['explanation']}",
                ]
            ),
        )

    if win_rate > 70 or win_rate < 30:
        embed.add_field(
            name="🛡️ 預測性對沖建議 (Predictive Hedge)",
            value=(
                f"偵測到預測市場對 `{market_question}` 的機率激增至 `{win_rate:.1f}%`，"
                "建議提前在 VTR 執行 Delta 對沖，以應對潛在的波動率跳空。"
            ),
            inline=False,
        )

    market_url = (
        f"https://polymarket.com/event/{event_slug}"
        if event_slug
        else "https://polymarket.com"
    )
    description = f"## {intent_emoji} {intent_label}\n[🔗 前往市場]({market_url})"
    if summary and summary != "（未啟用 AI 分析）":
        # AI 總結放 description（上限 4096），避免被 field 的 1024 上限截斷
        room = 4096 - len(description) - 2
        body = summary if len(summary) <= room else summary[: room - 1] + "…"
        description = f"{description}\n\n{body}"
    embed.description = description
    embed.set_footer(text=f"監測系統 | 動態門檻: ${dynamic_threshold:,.0f}")
    return embed


def create_option_defense_alert_embed(
    *,
    is_live: bool,
    symbol: str,
    status_icon: str,
    action_taken: str,
    pnl: float,
    exposure_pct: float,
    exit_reason: Optional[str] = None,
    regime: Optional[str] = None,
    target_delta: Optional[float] = None,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立期權轉倉防禦與結算警報 Embed (合併實盤與 VTR)"""
    title_prefix = "🛡️ 【實盤防禦】" if is_live else "🤖 【VTR 自動處置結果】"
    color = (
        discord.Color.gold()
        if is_live or "轉倉" in action_taken
        else discord.Color.red()
    )
    if pnl < 0:
        color = discord.Color.red()
    elif pnl > 0:
        color = discord.Color.green()

    embed = NexusEmbed(
        title=f"🔄 警報：動態轉倉、再平衡與結算防護 | {symbol}",
        description=f"**{title_prefix}**\n標的 **{symbol}** 已執行平倉或防禦性轉倉處置。\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    _add_ansi_field_safely(
        embed,
        "📋 處置結果",
        _ansi_tree_lines(
            [
                f"處置狀態        : {status_icon} {_ANSI_W}{action_taken}{_ANSI_0}",
                f"實現損益        : 💰 {_ANSI_W}${pnl:.2f}{_ANSI_0}",
                f"目前帳戶總曝險  : {_ANSI_W}{exposure_pct:.2f}%{_ANSI_0} (Beta-Weighted Delta)",
            ]
        ),
    )

    if exit_reason:
        _add_ansi_field_safely(
            embed,
            "🔎 觸發原因 / 指標",
            _ansi_tree_lines(exit_reason.split("\n")),
        )

    if regime is not None and target_delta is not None:
        _add_ansi_field_safely(
            embed,
            "🧠 系統自主位階判定",
            _ansi_tree_lines(
                [
                    f"位階判定      : {_ANSI_W}{regime}{_ANSI_0}",
                    f"理想總曝險目標: {_ANSI_W}{target_delta:.1f} Delta{_ANSI_0}",
                ]
            ),
        )

    if hedge:
        _add_ansi_field_safely(
            embed,
            "🛡️ 自動對沖決策",
            _ansi_tree_lines(
                [f"{hedge['action']} (缺口: {_ANSI_W}{hedge['gap']}{_ANSI_0})"]
            ),
        )

    embed.set_footer(text="期權轉倉防禦與結算警報")
    return embed


def create_volatility_risk_alert_embed(
    *,
    alert_type: str,
    title_text: str,
    description_text: str,
    color_hex: int,
    fields_data: List[Dict[str, Any]],
    footer_text: str = "Volatility Risk Alert",
) -> discord.Embed:
    """建立波動率與重大事件對沖警報 Embed (合併主動與被動)"""
    embed = NexusEmbed(
        title=f"🛡️ 警報：重大事件即時防護 | {title_text}",
        description=description_text,
        color=discord.Color(color_hex),
        timestamp=datetime.now(timezone.utc),
    )
    for field in fields_data:
        embed.add_field(
            name=field["name"],
            value=field["value"],
            inline=field.get("inline", False),
        )
    embed.set_footer(text=footer_text)
    return embed
