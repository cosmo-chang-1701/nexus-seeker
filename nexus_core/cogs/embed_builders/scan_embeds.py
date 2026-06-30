"""掃描與情緒類 Embed 建構函式。

包含：Sentiment Scan、Macro Scan、FOMC 逃頂窗口、壓力測試、Covered Call 解鎖、
財報 Embed、產業資金流 Embed 等。
"""

import discord
import logging

from datetime import datetime, timezone
from typing import List, Any, Optional

from market_analysis.uoa_telemetry import UOATradeResult, generate_uoa_ascii_table

from cogs.embed_builders._ansi_utils import (
    _pad_string,
    _truncate_with_boundary,
    _safe_float,
    _visual_truncate,
)
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    _safe_embed_codeblock_value,
    _build_watchlist_style_panel,
    _report_embed_color,
    _extract_report_batch,
    _parse_ai_report_sections,
    _append_ai_report_fields,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Visual Consistency Embed Subclass
# ============================================================================


class NexusEmbed(discord.Embed):
    """自訂 Embed 子類別，用以動態實現一致的版面設計、精緻調色盤與標準 Footer 排版。"""

    def __init__(self, *args, **kwargs):
        # 1. 統一對齊和諧且精美的高級調色盤 (Curated Aesthetic Palette)
        color = kwargs.get("color")
        if color is not None:
            if color == discord.Color.blue():
                kwargs["color"] = discord.Color(0x3498DB)
            elif color == discord.Color.red() or color == discord.Color.dark_red():
                kwargs["color"] = discord.Color(0xE74C3C)
            elif color == discord.Color.green():
                kwargs["color"] = discord.Color(0x2ECC71)
            elif color == discord.Color.orange():
                kwargs["color"] = discord.Color(0xF39C12)
            elif color == discord.Color.blurple():
                kwargs["color"] = discord.Color(0x5865F2)
        else:
            kwargs["color"] = discord.Color(0x3498DB)

        super().__init__(*args, **kwargs)


# ============================================================================
# UOA Formatter
# ============================================================================


def _format_uoa_field(uoa_data: list) -> str:
    """將 uoa_data 列表轉換為動態對齊的標準 ASCII 表格。"""
    trades = []
    for item in uoa_data:
        if "action" in item and "intent" in item:
            trade = UOATradeResult(
                expiry=str(item.get("expiry", "")),
                strike_price=float(item.get("strike", 0.0)),
                option_type=str(item.get("type", "")),
                trade_price=float(item.get("trade_price", 0.0)),
                bid_price=float(item.get("bid_price", 0.0)),
                ask_price=float(item.get("ask_price", 0.0)),
                volume=int(item.get("volume", 0)),
                open_interest=int(item.get("oi", 0)),
                ratio=float(item.get("ratio", 0.0)),
                ratio_str=str(item.get("ratio_str", f"{item.get('ratio', 0.0)}x")),
                action=str(item.get("action", "")),
                intent=str(item.get("intent", "")),
                symbol=item.get("symbol"),
            )
        else:
            expiry = str(item.get("expiry", ""))
            strike = float(item.get("strike", 0.0))
            opt_type = str(item.get("type", ""))
            volume = int(item.get("volume", 0))
            oi = int(item.get("oi", 0))
            ratio_val = float(item.get("ratio", 0.0))
            trade_type = str(item.get("trade_type", "SWEEP")).upper()
            action = (
                "🟢 BUY to OPEN (Ask)"
                if trade_type == "SWEEP"
                else "🔴 SELL to OPEN (Bid)"
            )
            # 動態意圖生成：綁定真實交易數據
            symbol_tag = f"[{item.get('symbol')}] " if item.get("symbol") else ""
            strike_tag = f"${strike:.2f}"
            vol_tag = f"{volume:,}"
            oi_tag = f"{oi:,}"
            if trade_type == "SWEEP":
                if opt_type.upper() == "CALL":
                    intent = (
                        f"🔥 {symbol_tag}機構在 {strike_tag} 主動買入 {vol_tag} 口"
                        f" CALL (OI={oi_tag})，Gamma 逼空火力集中"
                    )
                else:
                    intent = (
                        f"⚠️ {symbol_tag}機構在 {strike_tag} 急買 {vol_tag} 口"
                        f" PUT (OI={oi_tag})，恐慌性避險避雷"
                    )
            else:
                if opt_type.upper() == "CALL":
                    intent = (
                        f"🛡️ {symbol_tag}機構在 {strike_tag} 開倉賣出 {vol_tag} 口"
                        f" CALL (OI={oi_tag})，物理封頂鎖死上方天花板"
                    )
                else:
                    intent = (
                        f"🛡️ {symbol_tag}機構在 {strike_tag} 開倉賣出 {vol_tag} 口"
                        f" PUT (OI={oi_tag})，強力構築下行支撐地板"
                    )
            trade = UOATradeResult(
                expiry=expiry,
                strike_price=strike,
                option_type=opt_type,
                trade_price=0.0,
                bid_price=0.0,
                ask_price=0.0,
                volume=volume,
                open_interest=oi,
                ratio=ratio_val,
                ratio_str=f"{ratio_val:.2f}x",
                action=action,
                intent=intent,
                symbol=item.get("symbol"),
            )
        trades.append(trade)
    return generate_uoa_ascii_table(trades)


# ============================================================================
# Sentiment Scan Embed
# ============================================================================


def create_sentiment_scan_embed(
    symbol: str,
    skew_data: dict,
    pcr_data: dict,
    uoa_data: list,
    max_pain_data: dict,
    iv_data: Optional[Any] = None,
) -> discord.Embed:
    """建立期權情緒掃描報告 Embed (繁體中文)"""
    title_suffix = ""
    is_premarket = False
    iv_source = None

    if iv_data:
        if hasattr(iv_data, "is_premarket"):
            is_premarket = iv_data.is_premarket
        elif isinstance(iv_data, dict):
            is_premarket = iv_data.get("is_premarket", False)

        current_iv_val = (
            iv_data.current_iv
            if hasattr(iv_data, "current_iv")
            else iv_data.get("current_iv", 0.0)
        )

        iv_source = (
            iv_data.iv_source
            if hasattr(iv_data, "iv_source")
            else (iv_data.get("iv_source") if isinstance(iv_data, dict) else None)
        )

        if iv_source is None:
            if is_premarket and current_iv_val > 0.0:
                iv_source = "STORED_IV"
            elif current_iv_val > 0.0:
                iv_source = "LIVE_IV"
            else:
                iv_source = "UNAVAILABLE"

        if is_premarket:
            if current_iv_val > 0.0:
                title_suffix = (
                    " [盤前/HV代理]" if iv_source == "HV_PROXY" else " [盤前/前日收盤]"
                )
            else:
                title_suffix = " [盤前數據未更新]"

    embed = discord.Embed(
        title=f"📊 {symbol} 期權情緒掃描 (Sentiment Scan){title_suffix}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    if iv_data:
        if hasattr(iv_data, "current_iv"):
            current_iv = iv_data.current_iv
            iv_rank = iv_data.iv_rank
            iv_percentile = iv_data.iv_percentile
            expected_move_weekly = iv_data.expected_move_weekly
            iv_status = iv_data.iv_status
        else:
            current_iv = iv_data.get("current_iv", 0.0)
            iv_rank = iv_data.get("iv_rank", 0.0)
            iv_percentile = iv_data.get("iv_percentile", 0.0)
            expected_move_weekly = iv_data.get("expected_move_weekly", 0.0)
            iv_status = iv_data.get("iv_status", "Normal")

        iv_status_map = {
            "Low": "低 / 便宜",
            "Normal": "正常 / 公允",
            "High": "高 / 昂貴",
            "Extreme": "極高 / 泡沫",
        }
        status_tw = iv_status_map.get(iv_status, "正常 / 公允")
        earnings_loading = getattr(iv_data, "has_earnings_event", False) or (
            isinstance(iv_data, dict) and iv_data.get("has_earnings_event", False)
        )
        macro_loading = getattr(iv_data, "has_macro_event", False) or (
            isinstance(iv_data, dict) and iv_data.get("has_macro_event", False)
        )
        legacy_event_warning = getattr(iv_data, "has_event_warning_applied", False) or (
            isinstance(iv_data, dict)
            and iv_data.get("has_event_warning_applied", False)
        )

        if legacy_event_warning and not earnings_loading and not macro_loading:
            macro_loading = True

        if iv_source in ["STORED_IV", "HV_PROXY"] and not earnings_loading:
            try:
                from database.calendar_cache import get_cached_earnings
                from datetime import timedelta

                earnings = get_cached_earnings(symbol)
                if earnings and earnings.get("earnings_date"):
                    today_dt = datetime.now().date()
                    earn_date = datetime.strptime(
                        earnings["earnings_date"][:10], "%Y-%m-%d"
                    ).date()
                    if today_dt <= earn_date <= today_dt + timedelta(days=14):
                        earnings_loading = True
            except Exception:
                pass

        if earnings_loading:
            status_tw = "⚠️ 臨近財報/快取波動率可能低估"
        elif macro_loading:
            status_tw = "⚠️ 臨近總經大事件/快取波動率已校正"

        iv_status_str = f"狀態: {status_tw}"

        if is_premarket and current_iv == 0.0:
            iv_lines = [
                "```ansi",
                f" 🌌 {symbol} 期權情緒掃描 (Sentiment Scan)",
                " ----------------------------------",
                " Implied Volatility (IV)",
                " └─ 值: \u001b[1;30m--%\u001b[0m (等待開盤 / 盤前未開市)",
                " IV Rank / IV Percentile",
                " └─ IV Rank: \u001b[1;30m--%\u001b[0m | IV Percentile: \u001b[1;30m--%\u001b[0m (狀態: 待開盤)",
                " Expected Move (預期震盪區間)",
                " └─ 本週預期: \u001b[1;30m--\u001b[0m (開盤後更新)",
                "```",
            ]
        else:
            if is_premarket:
                if iv_source == "HV_PROXY":
                    vol_title = "Historical Volatility (HV, 30D)"
                    vol_note = "30D 歷史實現波動率代理（期權未開市/IV 不可用）"
                    em_note = "基於 30D HV 代理估算"
                else:
                    vol_title = "Implied Volatility (IV)"
                    vol_note = "前日收盤 IV / SQLite 快取（期權未開市）"
                    em_note = "基於前日收盤 IV 計算"
            else:
                vol_title = "Implied Volatility (IV)"
                vol_note = (
                    "當前 30 天平值期權隱含波動率"
                    if iv_source != "STORED_IV"
                    else "SQLite 快取 IV（非即時）"
                )
                em_note = (
                    "基於當前 IV 計算"
                    if iv_source != "STORED_IV"
                    else "基於快取 IV 計算"
                )

            iv_lines = [
                "```ansi",
                f" 🌌 {symbol} 期權情緒掃描 (Sentiment Scan)",
                " ----------------------------------",
                vol_title,
                f" └─ 值: {current_iv * 100:.1f}% ({vol_note})",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank:.1f}% | IV Percentile: {iv_percentile:.1f}% ({iv_status_str})",
                " Expected Move (預期震盪區間)",
            ]
            if earnings_loading or macro_loading:
                iv_lines.extend(
                    [
                        f" ├─ 本週預期: ±${expected_move_weekly:.2f} ({em_note})",
                        " └─ 備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。",
                    ]
                )
            else:
                iv_lines.append(
                    f" └─ 本週預期: ±${expected_move_weekly:.2f} ({em_note})"
                )
            iv_lines.append("```")
        embed.add_field(
            name="📊 隱含波動率與預期區間", value="\n".join(iv_lines), inline=False
        )

    skew_val = skew_data.get("skew", 0) if skew_data else 0
    skew_state = skew_data.get("state", "N/A") if skew_data else "N/A"

    pcr_dict = pcr_data if isinstance(pcr_data, dict) else {}
    vol_pcr = pcr_dict.get("volume_pcr", 0.0)
    oi_pcr = pcr_dict.get("oi_pcr", pcr_dict.get("pcr", 0.0))

    if pcr_dict:
        if is_premarket or vol_pcr == 0.0:
            vol_pcr_state = "⚖️ 封盤中 (盤前未更新)"
            vol_pcr_str = "--"
        else:
            vol_pcr_str = f"{vol_pcr:.2f}"
            if "volume_pcr_state" in pcr_dict:
                vol_pcr_state = pcr_dict["volume_pcr_state"]
            elif vol_pcr < 0.90:
                vol_pcr_state = "🐂 中性偏多/看漲主導"
            elif vol_pcr > 1.10:
                vol_pcr_state = "🐻 偏向空頭/看空主導"
            else:
                vol_pcr_state = "⚖️ 結構平衡"

        if oi_pcr == 0.0:
            oi_pcr_state = "N/A (結構缺失)"
            oi_pcr_str = "--"
        else:
            oi_pcr_str = f"{oi_pcr:.2f}"
            if "oi_pcr_state" in pcr_dict:
                oi_pcr_state = pcr_dict["oi_pcr_state"]
            elif oi_pcr < 0.90:
                oi_pcr_state = "🏹 結構激進/看漲多頭沉澱"
            elif oi_pcr > 1.20:
                oi_pcr_state = "🛡️ 結構防禦/虛值 Put 沉澱"
            else:
                oi_pcr_state = "⚖️ 籌碼結構中性"
    else:
        vol_pcr_state = "⚖️ 封盤中 (盤前未更新)"
        vol_pcr_str = "--"
        oi_pcr_state = "N/A (結構缺失)"
        oi_pcr_str = "--"

    mp_strike = max_pain_data.get("max_pain", "N/A") if max_pain_data else "N/A"

    if mp_strike == "N/A" or (isinstance(mp_strike, (int, float)) and mp_strike <= 0.0):
        mp_strike_str = "N/A"
        is_conv = "⚠️ 數據源缺失"
    else:
        if max_pain_data.get("is_circuit_breaker_triggered", False):
            mp_strike_str = "N/A (已觸發斷路器)"
            is_conv = "⚠️ 偏離度過高 (>30%)"
        else:
            mp_strike_str = (
                f"${mp_strike:.2f}"
                if isinstance(mp_strike, (int, float))
                else f"${mp_strike}"
            )
            is_conv = (
                "🎯 趨於收斂" if max_pain_data.get("is_converging") else "⏳ 尚有距離"
            )

    metrics_lines = ["```ansi"]
    m_headers = ["指標項目", "數據值", "狀態 / 備註"]
    m_widths = [14, 20, 24]
    metrics_lines.append(
        " | ".join(
            _pad_string(h, w, "left" if i == 0 or i == 2 else "right")
            for i, (h, w) in enumerate(zip(m_headers, m_widths))
        )
    )
    metrics_lines.append("-" * (sum(m_widths) + 3 * (len(m_widths) - 1)))

    # Skew 渲染
    skew_val_str = (
        f"{skew_val:.2f}%" if isinstance(skew_val, (int, float)) else f"{skew_val}%"
    )
    metrics_lines.append(
        f"{_pad_string('Option Skew', m_widths[0])} | {_pad_string(skew_val_str, m_widths[1], 'right')} | {_pad_string(skew_state, m_widths[2])}"
    )
    # Volume PCR 渲染
    metrics_lines.append(
        f"{_pad_string('Volume PCR', m_widths[0])} | {_pad_string(vol_pcr_str, m_widths[1], 'right')} | {_pad_string(vol_pcr_state, m_widths[2])}"
    )
    # OI PCR 渲染
    metrics_lines.append(
        f"{_pad_string('OI PCR', m_widths[0])} | {_pad_string(oi_pcr_str, m_widths[1], 'right')} | {_pad_string(oi_pcr_state, m_widths[2])}"
    )
    # Max Pain 渲染
    metrics_lines.append(
        f"{_pad_string('Max Pain', m_widths[0])} | {_pad_string(mp_strike_str, m_widths[1], 'right')} | {_pad_string(is_conv, m_widths[2])}"
    )
    metrics_lines.append("```")

    embed.add_field(
        name="📐 期權情緒指標", value="\n".join(metrics_lines), inline=False
    )

    # UOA 渲染
    if uoa_data:
        table_str = _format_uoa_field(uoa_data)
        embed.add_field(
            name="🐋 異常活動 (UOA)", value=f"```ansi\n{table_str}\n```", inline=False
        )
    else:
        embed.add_field(
            name="🐋 異常活動 (UOA)",
            value="```ansi\n目前無顯著異常活動\n```",
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Volatility Strategist")
    return embed


# ============================================================================
# Macro Scan Embed
# ============================================================================


def create_macro_scan_embed(
    macro_data: dict, alerts: Optional[List[Any]] = None
) -> discord.Embed:
    """建立巨觀環境與隔夜市場掃描 Embed (繁體中文)"""
    base_color = discord.Color.red() if alerts else discord.Color.blue()
    embed = discord.Embed(
        title="🌍 巨觀環境與隔夜市場掃描 (Macro Scan)",
        color=base_color,
        timestamp=datetime.now(timezone.utc),
    )

    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change = macro_data.get("tnx_change_bps", 0.0)
    us2y = macro_data.get("us2y", 0.0)
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    spread = tnx - us2y

    # Consolidate into monospace table
    vix_emoji = "🔥" if vix > 25 else ("⚠️" if vix > 20 else "🟢")

    lines = ["```ansi"]
    headers = ["指標", "數值", "變動 / 備註"]
    widths = [14, 8, 14]
    lines.append(
        " | ".join(
            _pad_string(h, w, "left" if i == 0 else "right")
            for i, (h, w) in enumerate(zip(headers, widths))
        )
    )
    lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))

    # 1. DXY
    lines.append(
        f"{_pad_string('DXY 美元指數', widths[0])} | {_pad_string(f'{dxy:.2f}', widths[1], 'right')} | {_pad_string('-', widths[2], 'right')}"
    )
    # 2. TNX
    lines.append(
        f"{_pad_string('TNX 10Y 公債', widths[0])} | {_pad_string(f'{tnx:.2f}%', widths[1], 'right')} | {_pad_string(f'{tnx_change:+.1f} bps', widths[2], 'right')}"
    )
    # 3. US2Y
    lines.append(
        f"{_pad_string('US2Y 2Y 公債', widths[0])} | {_pad_string(f'{us2y:.2f}%', widths[1], 'right')} | {_pad_string(f'利差 {spread:+.2f}%', widths[2], 'right')}"
    )
    # 4. VIX
    vix_color_start = " [0;31m" if vix > 25 else (" [0;33m" if vix > 20 else " [0;32m")
    vix_note = f"{vix_change:+.2f} ({vix_emoji})"
    vix_note_colored = f"{vix_change:+.2f} ({vix_color_start}{vix_emoji} [0m)"
    vix_val_str = f"{vix:.2f}"
    lines.append(
        f"{_pad_string('VIX 恐慌指數', widths[0])} | {_pad_string(vix_val_str, widths[1], 'right')} | {_pad_string(vix_note, widths[2], 'right').replace(vix_note, vix_note_colored)}"
    )
    lines.append("```")

    embed.add_field(name="🌍 巨觀數據指標", value="\n".join(lines), inline=False)

    # 結論與警示
    if alerts:
        alert_text = "\n".join([f"• {a}" for a in alerts])
        embed.add_field(
            name="🚨 風險警示 (Macro Alerts)", value=alert_text, inline=False
        )
    else:
        embed.add_field(
            name="✅ 巨觀狀態",
            value="殖利率曲線、匯率與波動率未見極端異常。維持標準市場部位。",
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker | Global Macro Intelligence")
    return embed


# ============================================================================
# FOMC Escape Window Embed
# ============================================================================


def create_fomc_escape_window_embed(
    prob: float,
    direction: str,
    shift_days: int,
    adjusted_start: str,
    adjusted_end: str,
    reason: str,
    is_fallback: bool = False,
) -> discord.Embed:
    """建立方案 C 逃頂窗口推演 Embed (繁體中文)"""
    color = discord.Color.red() if direction == "後推" else discord.Color.green()
    title_text = "📅 方案 C 逃頂窗口推演 (FOMC / FedWatch 宏觀警報)"
    if is_fallback:
        title_text += " [歷史快取/備援]"
    embed = NexusEmbed(title=title_text, color=color)

    prob_suffix = " *(歷史快取/備援)*" if is_fallback else ""
    embed.add_field(
        name="📊 利率機率定價 (FedWatch)",
        value=f"下週 FOMC 維持高利率/加息機率：**{prob * 100:.1f}%**{prob_suffix}",
        inline=False,
    )

    embed.add_field(
        name="🔄 逃頂窗口調整方向",
        value=f"調整方向：**{direction} {shift_days} 個交易日**",
        inline=True,
    )

    embed.add_field(
        name="📆 調整後逃頂窗口預期",
        value=f"預估窗口：**{adjusted_start}** 至 **{adjusted_end}**",
        inline=True,
    )

    embed.add_field(name="💡 推演邏輯與風險分析", value=reason, inline=False)

    embed.set_footer(text="方案 C 逃頂推演引擎")
    return embed


# ============================================================================
# Stress Test Embed
# ============================================================================


def create_stress_test_embed(results: dict) -> discord.Embed:
    """建立 GTC 掛單現金赤字壓力測試 Embed (繁體中文)"""
    is_critical = results.get("is_critical", False)
    color = discord.Color.red() if is_critical else discord.Color.green()

    embed = NexusEmbed(
        title="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)", color=color
    )

    total_deficit = results.get("total_deficit", 0.0)
    cash_reserve = results.get("cash_reserve", 0.0)
    boxx_shares = results.get("boxx_shares", 0.0)
    boxx_cash = results.get("boxx_cash", 0.0)
    net_deficit = results.get("net_deficit", 0.0)
    order_count = results.get("gtc_buy_orders_count", 0)

    embed.add_field(
        name="📊 壓測摘要",
        value=f"• 活躍 GTC 網格買單筆數：**{order_count} 筆**\n"
        f"• 100% 全數成交所需總美金 (Total Cash Deficit)：**${total_deficit:,.2f}**\n"
        f"• 常規可用現金 (cash_reserve)：**${cash_reserve:,.2f}**\n"
        f"• BOXX 持倉股數：**{boxx_shares:.1f} 股** (常規清算上限 180 股)\n"
        f"• BOXX 最大套現金額：**${boxx_cash:,.2f}**\n"
        f"• 壓測後淨赤字/淨值：**${net_deficit:,.2f}**",
        inline=False,
    )

    if is_critical:
        embed.add_field(
            name="🔥 CRITICAL WARNING",
            value=f"**警告：當前 GTC 網格單潛在赤字已大於可用流動性！**\n"
            f"在極端無差別踩踏情境下，若所有掛單 100% 全數成交，將會**抽乾 BOXX 水壩**，"
            f"**破壞 +${cash_reserve:,.0f} 的安全常規現金水位**，且**危及 7 月底 $13,000 實體提領紅線**！\n"
            f"建議立即取消部分 GTC 掛單，或注入額外資金以維持安全邊際。",
            inline=False,
        )
    else:
        embed.add_field(
            name="✅ 系統安全狀態",
            value="目前可用現金儲備與 BOXX 備用流動性充裕，足以覆蓋所有活躍 GTC 掛單全數成交之極端情境，未威脅到提領紅線。",
            inline=False,
        )

    embed.set_footer(text="現金赤字壓力測試模組")
    return embed


# ============================================================================
# Covered Call Unlock Embed
# ============================================================================


def create_covered_call_unlock_embed(data: dict) -> discord.Embed:
    """建立物理死鎖解除與備兌建單指引 Embed (繁體中文)"""
    symbol = data.get("symbol", "")
    current_shares = data.get("current_shares", 0.0)
    current_cost = data.get("current_cost", 0.0)
    new_cost_basis = data.get("new_cost_basis", 0.0)
    current_price = data.get("current_price", 0.0)
    recs = data.get("recommendations", [])

    embed = NexusEmbed(
        title=f"🔓 {symbol} 物理死鎖解除與備兌建單指引",
        color=discord.Color.green() if recs else discord.Color.orange(),
    )

    # 計算現價與成本價差比率
    diff_pct = (
        ((current_price - current_cost) / current_cost * 100.0)
        if current_cost > 0
        else 0.0
    )
    diff_color = "\u001b[1;32m" if diff_pct >= 0 else "\u001b[1;31m"

    spot_lines = [
        "```ansi",
        " 現貨持倉狀態 (Spot Position Details)",
        f" ├─ 持股數量: \u001b[1;36m{current_shares:.0f} 股\u001b[0m",
        f" ├─ 原始均價: \u001b[1;33m${current_cost:,.2f}\u001b[0m",
        f" └─ 當前現價: \u001b[1;32m${current_price:,.2f}\u001b[0m (與均價價差: {diff_color}{diff_pct:+.2f}%\u001b[0m)",
        "",
        " 模擬吸籌後成本 (Simulated Cost Basis After Accumulation)",
        f" └─ 加權平均成本: \u001b[1;35m${new_cost_basis:,.2f}\u001b[0m",
        "```",
        "*(已計入所有活躍 GTC 買入網格單模擬成交後的成本調整)*",
    ]

    embed.add_field(
        name="💼 現貨與吸籌模擬 (Spot & Accumulation)",
        value="\n".join(spot_lines),
        inline=False,
    )

    if recs:
        # 建立 ANSI 備兌推薦合約表格
        rec_table_lines = [
            "```ansi",
            " 到期日     | 履約價    | 預估 Delta | 參考權利金 | 年化收益率",
            " -----------------------------------------------------------",
        ]
        for r in recs:
            exp = r.get("expiration", "")
            strike = r.get("strike", 0.0)
            d_val = r.get("delta", 0.0)
            premium = r.get("premium", 0.0)
            ann_yield = r.get("annualized_yield", 0.0)

            # 預先格式化字串，保持欄位對齊
            exp_str = f"{exp:<10}"
            strike_str = f"${strike:<7.2f}"
            delta_str = f"{d_val:<10.3f}"
            premium_str = f"${premium:<9.2f}"

            if "annualized_yield" in r:
                yield_str = f"{ann_yield:>9.2f}%"
                color_yield = "\u001b[1;32m" if ann_yield >= 10.0 else "\u001b[1;35m"
            else:
                yield_str = "      N/A"
                color_yield = "\u001b[1;30m"

            rec_table_lines.append(
                f" {exp_str} | \u001b[1;33m{strike_str}\u001b[0m | \u001b[1;36m{delta_str}\u001b[0m | \u001b[1;32m{premium_str}\u001b[0m | {color_yield}{yield_str}\u001b[0m"
            )
        rec_table_lines.append("```")

        embed.add_field(
            name="🎯 推薦 Covered Call 備兌合約 (Recommended Contracts)",
            value="\n".join(rec_table_lines),
            inline=False,
        )

        embed.add_field(
            name="💡 物理死鎖解鎖說明 (Recovery Guidance)",
            value="現貨大跌至低位網格吸籌完成後，透過建立**高於新成本線且 Delta < 0.15 且年化收益率 >= 10%** 的極虛值備兌 Call，可以在安全保護現貨（防止被平價收回）的同時收取權利金，加速降低整體套牢部位的持有成本，實現物理死鎖解鎖。",
            inline=False,
        )
    else:
        status_lines = [
            "```ansi",
            " ⚠️ 解鎖警告 (Unlock Alert)",
            " └─ 狀態: \u001b[1;31m未尋獲符合條件之極虛值 Covered Call 合約\u001b[0m",
            "",
            " 篩選門檻 (Criteria)",
            " ├─ 履約價 > 模擬加權成本",
            " ├─ 預估 Delta < 0.15",
            " └─ 年化收益率 >= 10.0% 或單次權利金 >= 現貨 1.0%",
            "```",
            "💡 **策略建議**：目前市場隱含波動率低迷或現貨價格過低，不宜盲目開倉。建議等待現貨反彈或波動率回升，拉開與成本線之空間後再行評估。",
        ]
        embed.add_field(
            name="⚠️ 解鎖狀態與策略建議 (Unlock Status & Strategy)",
            value="\n".join(status_lines),
            inline=False,
        )

    embed.set_footer(text="物理死鎖解除策略模組")
    return embed


# ============================================================================
# Earnings Report Embed
# ============================================================================


def create_earnings_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """
    建立盤前財報與估值調整 Embed，沿用欄位化戰報風格。
    """
    upcoming = raw_data.get("upcoming_earnings", {})
    sentiment = raw_data.get("earnings_sentiment_scan", {})
    analyzed_symbols = int(raw_data.get("analyzed_symbols", 0) or 0)
    batch_label = _extract_report_batch(report_type)

    embed = discord.Embed(
        title="📊 Nexus Seeker 盤前財報與估值調整",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**掃描標的：** `{analyzed_symbols}` 檔 ｜ "
            f"**即將財報：** `{len(upcoming)}` 檔\n"
            "盤前聚焦財報日期、情緒與估值風險，維持與其他核心戰報一致的欄位式呈現。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    if upcoming:
        earnings_lines = ["```ansi"]
        headers = ["標的", "財報日", "情緒覆蓋"]
        widths = [8, 14, 12]
        earnings_lines.append(
            " | ".join(_pad_string(h, w) for h, w in zip(headers, widths))
        )
        earnings_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        for sym, events in upcoming.items():
            for event in events:
                date = event.get("date", "未知日期")
                sentiment_status = "新聞+社群" if sym in sentiment else "日曆"
                earnings_lines.append(
                    " | ".join(
                        [
                            _pad_string(_visual_truncate(sym, widths[0]), widths[0]),
                            _pad_string(_visual_truncate(date, widths[1]), widths[1]),
                            _pad_string(
                                _visual_truncate(sentiment_status, widths[2]), widths[2]
                            ),
                        ]
                    )
                )
        earnings_lines.append("```")
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value("\n".join(earnings_lines), "近期無財報事件"),
            inline=False,
        )
    else:
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value(
                "近期無需調整倉位的財報事件。", "近期無財報事件"
            ),
            inline=False,
        )

    sentiment_lines = []
    for sym, payload in list(sentiment.items())[:3]:
        news = str(payload.get("news", "無相關資訊"))
        reddit = str(payload.get("reddit_sentiment", "無相關資訊"))
        sentiment_lines.append(f"**{sym}**")
        sentiment_lines.append(f"📰 新聞：{_truncate_with_boundary(news, 140)}")
        sentiment_lines.append(f"💬 社群：{_truncate_with_boundary(reddit, 140)}")
        sentiment_lines.append("")
    if sentiment_lines:
        sentiment_lines.pop()
    else:
        sentiment_lines = ["目前無額外新聞 / Reddit 情緒補充。"]
    embed.add_field(
        name="🧠 情緒 / 估值快照",
        value=_safe_embed_field_value("\n".join(sentiment_lines), "目前無額外情緒資訊"),
        inline=False,
    )

    note = raw_data.get("note", "")
    if note:
        embed.add_field(
            name="🧾 掃描備註",
            value=_safe_embed_field_value(str(note), "無補充備註"),
            inline=False,
        )

    _append_ai_report_fields(embed, report_content)
    embed.set_footer(text="Nexus Seeker AI Analyst | 盤前財報與估值調整")
    return embed


# ============================================================================
# Sector Flow Report Embed
# ============================================================================


def create_sector_flow_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """建立收盤資金流向與板塊輪動報告 Embed。"""
    batch_label = _extract_report_batch(report_type)
    vix = _safe_float(raw_data.get("vix"))
    spy_price = _safe_float(raw_data.get("spy_price"))
    vix_tier_name = str(raw_data.get("vix_tier_name", "Unknown"))
    sectors = raw_data.get("sectors", []) or []
    poly_events = raw_data.get("poly_events", []) or []
    spy_max_pain = raw_data.get("spy_max_pain", {}) or {}

    embed = discord.Embed(
        title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**SPY 現價：** `${spy_price:.2f}` ｜ "
            f"**VIX：** `{vix:.2f}` (`{vix_tier_name}`)\n"
            "沿用欄位式收盤戰報版型，彙整板塊輪動、事件定價與 AI 收斂結論。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    market_panel_body = "\n".join(
        [
            f"SPY 現價: ${spy_price:.2f}",
            f"VIX: {vix:.2f} ({vix_tier_name})",
            f"掃描板塊數: {len(sectors)}",
            f"Polymarket 訊號: {len(poly_events)}",
        ]
    )
    market_panel = _build_watchlist_style_panel(
        "🌐 收盤市場快照 (Close Snapshot)",
        market_panel_body,
        width=45,
        empty_msg="暫無市場快照",
    )
    embed.add_field(
        name="🌐 收盤市場快照",
        value=_safe_embed_codeblock_value(market_panel, "暫無市場快照", lang="ansi"),
        inline=False,
    )

    if sectors:
        sector_lines = ["```ansi"]
        sector_lines.append(" 🔄 板塊輪動快照 (Sector Rotation)")
        sector_lines.append(" ----------------------------------")
        headers = ["ETF", "板塊", "日變動", "量比", "Skew", "UOA"]
        widths = [5, 18, 8, 6, 8, 4]
        sector_lines.append(
            " ".join(
                [
                    " | ".join(_pad_string(h, w) for h, w in zip(headers, widths)),
                ]
            )
        )
        sector_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        sorted_sectors = sorted(
            sectors,
            key=lambda item: abs(_safe_float(item.get("pct_change"))),
            reverse=True,
        )
        for item in sorted_sectors:
            sector_lines.append(
                " | ".join(
                    [
                        _pad_string(
                            _visual_truncate(str(item.get("symbol", "N/A")), widths[0]),
                            widths[0],
                        ),
                        _pad_string(
                            _visual_truncate(str(item.get("name", "N/A")), widths[1]),
                            widths[1],
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('pct_change')):+.2f}%",
                            widths[2],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('rel_vol')):.2f}x",
                            widths[3],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('skew')):+.1f}",
                            widths[4],
                            "right",
                        ),
                        _pad_string(
                            str(int(item.get("uoa_count", 0))), widths[5], "right"
                        ),
                    ]
                )
            )
        sector_lines.append("```")
        sector_value = "\n".join(sector_lines)
    else:
        sector_panel = _build_watchlist_style_panel(
            "🔄 板塊輪動快照 (Sector Rotation)",
            "",
            width=45,
            empty_msg="目前無板塊輪動資料。",
        )
        sector_value = f"```ansi\n{sector_panel}\n```"
    embed.add_field(
        name="🔄 板塊輪動快照",
        value=_safe_embed_field_value(sector_value, "目前無板塊輪動資料。"),
        inline=False,
    )

    event_bullets: list[str] = []
    max_pain_value = spy_max_pain.get("max_pain")
    if max_pain_value is not None:
        event_bullets.append(f"SPY Max Pain: ${_safe_float(max_pain_value):.2f}")

    for event in poly_events[:3]:
        question = _truncate_with_boundary(str(event.get("question", "N/A")), 140)
        event_bullets.append(f"Polymarket: {question}")

    event_panel = _build_watchlist_style_panel(
        "🐋 事件定價與關鍵參考 (Event Pricing)",
        "\n".join(event_bullets),
        width=45,
        empty_msg="目前無顯著 Polymarket / Max Pain 補充訊號。",
    )
    embed.add_field(
        name="🐋 事件定價與關鍵參考",
        value=_safe_embed_codeblock_value(
            event_panel, "目前無事件定價資料", lang="ansi"
        ),
        inline=False,
    )

    sections = _parse_ai_report_sections(report_content)
    if sections:
        for header, content in sections:
            panel = _build_watchlist_style_panel(
                header,
                content,
                width=45,
                empty_msg="無詳細資訊",
            )
            embed.add_field(
                name=header,
                value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
                inline=False,
            )
    else:
        panel = _build_watchlist_style_panel(
            "🤖 AI 分析摘要 (AI Summary)",
            report_content,
            width=45,
            empty_msg="無詳細資訊",
        )
        embed.add_field(
            name="🤖 AI 分析摘要",
            value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
            inline=False,
        )
    embed.set_footer(text="Nexus Seeker AI Analyst | 收盤資金流向與板塊輪動")
    return embed


def create_cc_recovery_embed(data: dict) -> discord.Embed:
    """建立 Covered Call 備兌合約防禦性收租指引 Embed (繁體中文)"""
    symbol = data.get("symbol", "")
    current_price = data.get("current_price", 0.0)
    recs = data.get("recommendations", [])
    fallback_iv = data.get("fallback_iv", 0.0)

    # Note: color parameter triggers appropriate NexusEmbed palette mapping automatically
    embed = NexusEmbed(
        title=f"🛡️ {symbol} Covered Call 防禦性收租篩選結果",
        color=discord.Color.blue() if recs else discord.Color.orange(),
    )

    spot_lines = [
        "```ansi",
        " 標的現貨狀態 (Spot Asset Status)",
        f" ├─ 當前現價: \u001b[1;32m${current_price:,.2f}\u001b[0m",
        f" └─ 波動率參考值: \u001b[1;35m{fallback_iv * 100.0:.2f}%\u001b[0m",
        "```",
    ]

    embed.add_field(
        name="💼 標的行情 (Spot Market)",
        value="\n".join(spot_lines),
        inline=False,
    )

    if recs:
        # 建立 ANSI 備兌推薦合約表格
        rec_table_lines = [
            "```ansi",
            " 到期日     | 履約價    | 預估 Delta | 參考權利金 | 年化收益率",
            " -----------------------------------------------------------",
        ]
        for r in recs:
            exp = r.get("expiration", "")
            strike = r.get("strike", 0.0)
            d_val = r.get("delta", 0.0)
            premium = r.get("premium", 0.0)
            ann_yield = r.get("annualized_yield", 0.0)

            # 預先格式化字串，保持欄位對齊
            exp_str = f"{exp:<10}"
            strike_str = f"${strike:<7.2f}"
            delta_str = f"{d_val:<10.3f}"
            premium_str = f"${premium:<9.2f}"

            yield_str = f"{ann_yield:>9.2f}%"
            color_yield = "\u001b[1;32m" if ann_yield >= 10.0 else "\u001b[1;35m"

            rec_table_lines.append(
                f" {exp_str} | \u001b[1;33m{strike_str}\u001b[0m | \u001b[1;36m{delta_str}\u001b[0m | \u001b[1;32m{premium_str}\u001b[0m | {color_yield}{yield_str}\u001b[0m"
            )
        rec_table_lines.append("```")

        rec_table_str = "\n".join(rec_table_lines)
        if any(r.get("has_earnings_risk") for r in recs):
            rec_table_str += "\n🔴 **警示標籤**：此合約橫跨財報日，隱含波動率（IV）可能於選後崩跌（IV Crush），請謹慎開倉。"

        embed.add_field(
            name="🎯 推薦 Covered Call 備兌合約 (Recommended Contracts)",
            value=rec_table_str,
            inline=False,
        )

        embed.add_field(
            name="💡 防禦性收租指引 (Defensive Yield Guidance)",
            value="篩選出滿足 **DTE 30-50 天、預估 Delta < 0.15 且年化收益率 >= 10%** 的極虛值 Covered Call 合約。此策略適合持股套牢或欲進行防禦性收租之交易，在安全保護現貨（降低被平價收回機率）的同時收取權利金，藉以降低持股成本。",
            inline=False,
        )
    else:
        status_lines = [
            "```ansi",
            " ⚠️ 篩選警告 (Filter Alert)",
            " └─ 狀態: \u001b[1;31m未尋獲符合條件之極虛值 Covered Call 合約\u001b[0m",
            "",
            " 篩選門檻 (Criteria)",
            " ├─ 到期天數 (DTE) 介於 30 至 50 天之間",
            " ├─ 預估 Delta < 0.15",
            " └─ 年化收益率 >= 10.0%",
            "```",
            "💡 **策略建議**：目前市場隱含波動率低迷或無符合條件的期權合約，不宜盲目開倉。建議等待現貨反彈或波動率回升，拉開空間後再行評估。",
        ]
        embed.add_field(
            name="⚠️ 篩選狀態與策略建議 (Status & Strategy)",
            value="\n".join(status_lines),
            inline=False,
        )

    embed.set_footer(text="Covered Call 收租策略模組")
    return embed
