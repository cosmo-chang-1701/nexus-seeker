"""期權情緒掃描報告 Embed 建構函式（Sentiment Scan）。"""

import discord

from datetime import datetime, timezone
from typing import Any, Optional

from market_analysis.uoa_telemetry import UOATradeResult, generate_uoa_ascii_table

from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._core import NexusEmbed


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
                "🟢 買入開倉 (BTO - Ask)"
                if trade_type == "SWEEP"
                else "🔴 賣出開倉 (STO - Bid)"
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

    embed = NexusEmbed(
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
