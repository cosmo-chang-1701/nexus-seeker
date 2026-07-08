"""Watchlist 心跳與總覽 Embed 建構函式。

包含：
- create_watchlist_embed：Watchlist 分頁清單
- create_watchlist_signal_embed：每半小時標的分析心跳 Embed（2.0 版）
- create_watchlist_overview_embed：本輪 Watchlist 總覽摘要
"""

import discord

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._embed_helpers import _safe_embed_field_value
from cogs.embed_builders._core import NexusEmbed


def create_watchlist_embed(page_data, current_page, total_pages, total_items):
    """生成觀察清單的分頁 Embed (移除成本欄位)"""

    if not page_data:
        description = "目前沒有追蹤任何項目"
    else:
        lines = ["```ansi"]
        # 1. 標頭修改為兩欄
        header = f"{_pad_string('標的 [標籤]', 20)} | {_pad_string('AI 分析 (LLM)', 12, 'right')}"
        lines.append(header)

        # 2. 分隔線
        lines.append("-" * 35)

        for sym, use_llm, tags in page_data:
            display_sym = f"{sym} [{tags}]" if tags else sym
            sym_fmt = _pad_string(display_sym, 20)
            llm_text = "開啟 (ON)" if use_llm else "關閉 (OFF)"
            llm_fmt = _pad_string(llm_text, 12, "right")
            lines.append(f"{sym_fmt} | {llm_fmt}")

        lines.append("```")
        description = "\n".join(lines)

    embed = discord.Embed(
        title="📡 【您的專屬觀察清單】",
        description=f"目前監控中的標的清單。系統將每 30 分鐘自動執行量化掃描。\n\n{description}",
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}"
    )
    return embed


def create_watchlist_signal_embed(
    symbol: str,
    report_body: str = "",
    option_guidance: str = "",
    event_risk_summary: str = "",
    skew_state: str = "",
    alert_level: str = "",
    option_plan: Any | None = None,
    skew_commentary: str | None = None,
    has_position: bool = False,
    holding_quantity: float | None = None,
    holding_avg_cost: float | None = None,
    holding_pnl_pct: float | None = None,
    suitable_buy_price: float | None = None,
    suitable_buy_shares: int | None = None,
    suitable_sell_price: float | None = None,
    suitable_sell_shares: int | None = None,
    buy_rationale: str | None = None,
    sell_rationale: str | None = None,
    telemetry_alignment_note: str | None = None,
    # Upgraded Heartbeat Parameters
    metrics: Any | None = None,
    quote: dict | None = None,
    iv_metrics: Any | None = None,
    max_pain_data: dict | None = None,
    pcr_data: dict | None = None,
    uoa_list: list[dict] | None = None,
    symbol_gex: dict | None = None,
    toggles: dict[str, bool] | None = None,
    symbol_tags: list[str] | None = None,
) -> discord.Embed:
    """建立標的分析中心 2.0 • 戰場心跳快照 (Watchlist Heartbeat) 的 Markdown-ASCII 統一模板。"""
    from services.llm_service import is_memory_safe

    taipei_tz = timezone(timedelta(hours=8))
    timestamp_str = datetime.now(taipei_tz).strftime("%Y-%m-%d %H:%M:%S")

    toggles = toggles or {}
    show_live_price = toggles.get("hb_live_price", True)

    # 整合 2, 3, 4 -> hb_options_structure
    hb_options = toggles.get("hb_options_structure", True)
    show_market_footprints = hb_options
    show_iv_context = hb_options
    show_target_lock = hb_options

    # 獨立 5 -> hb_uoa
    show_uoa = toggles.get("hb_uoa", True)

    # 整合 6, 7 -> hb_execution_risk
    hb_exec = toggles.get("hb_execution_risk", True)
    show_risk_alignment = hb_exec
    show_telemetry = hb_exec

    sys_status = "TELEMETRY RUNNING"
    if not is_memory_safe():
        sys_status = "TELEMETRY RUNNING (⚠️ LOW RAM DEGRADED)"

    # 🛡️ 提取盤前狀態 (為後續 PCR 與 IV 降級防禦做準備)
    is_premarket = False
    if iv_metrics is not None and hasattr(iv_metrics, "is_premarket"):
        is_premarket = iv_metrics.is_premarket

    # Extract Live Price Metrics
    if metrics is not None:
        live_price = metrics.current_price
        gex_putwall = metrics.gex_max_put_wall
        vol_poc = metrics.volume_poc
        skew_val = metrics.option_skew
        skew_per = metrics.skew_percentile
    else:
        live_price_val = (
            suitable_buy_price if not isinstance(suitable_buy_price, str) else None
        )
        live_price = live_price_val or suitable_sell_price or 100.0
        gex_putwall = None
        vol_poc = 100.0
        skew_val = None
        skew_per = None

    # Extract Finnhub Quote Data
    if quote is not None:
        open_val = float(quote.get("o") or 0.0)
        high_val = float(quote.get("h") or 0.0)
        low_val = float(quote.get("l") or 0.0)
        prev_close = float(quote.get("pc") or 0.0)
        change_raw = float(quote.get("d") or 0.0)
        change_pct = float(quote.get("dp") or 0.0)
    else:
        open_val = live_price
        high_val = live_price
        low_val = live_price
        prev_close = live_price
        change_raw = 0.0
        change_pct = 0.0

    # Extract IV metrics
    earnings_loading = False
    macro_loading = False
    legacy_event_warning = False
    iv_source = "UNAVAILABLE"

    if iv_metrics is not None:
        iv_val = (
            iv_metrics.current_iv * 100.0 if iv_metrics.current_iv is not None else None
        )
        iv_rank = iv_metrics.iv_rank
        iv_status = iv_metrics.iv_status.upper() if iv_metrics.iv_status else "NORMAL"
        expected_move = iv_metrics.expected_move_weekly
        earnings_loading = getattr(iv_metrics, "has_earnings_event", False)
        macro_loading = getattr(iv_metrics, "has_macro_event", False)
        legacy_event_warning = getattr(iv_metrics, "has_event_warning_applied", False)
        iv_source = iv_metrics.iv_source
        iv_term_status = getattr(iv_metrics, "iv_term_structure_status", None)
        iv_term_ratio = getattr(iv_metrics, "term_structure_ratio", None)
    else:
        iv_val = None
        iv_rank = None
        iv_status = "NORMAL"
        expected_move = None
        iv_term_status = None
        iv_term_ratio = None

    if metrics is not None:
        if hasattr(metrics, "has_earnings_event") and metrics.has_earnings_event:
            earnings_loading = True
        if hasattr(metrics, "has_macro_event") and metrics.has_macro_event:
            macro_loading = True
        if (
            hasattr(metrics, "has_event_warning_applied")
            and metrics.has_event_warning_applied
        ):
            legacy_event_warning = True

        if hasattr(metrics, "iv_source") and metrics.iv_source:
            iv_source = metrics.iv_source

    if legacy_event_warning and not earnings_loading and not macro_loading:
        macro_loading = True

    if iv_source in ["STORED_IV", "HV_PROXY"] and not earnings_loading:
        try:
            from database.calendar_cache import get_cached_earnings

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

    iv_status_str = f"狀態: {iv_status}"
    if earnings_loading:
        iv_status_str = "狀態: ⚠️ 臨近財報/快取波動率可能低估"
    elif macro_loading:
        iv_status_str = "狀態: ⚠️ 臨近總經大事件/快取波動率已校正"

    if max_pain_data is not None:
        mp_val = max_pain_data.get("max_pain")
        if max_pain_data.get("circuit_breaker_triggered", False):
            max_pain_str = "N/A (已觸發斷路器, ⚠️ 偏離度過高 >30%)"
        elif mp_val is None or (
            isinstance(mp_val, (int, float)) and float(mp_val) <= 0.0
        ):
            max_pain_str = "N/A (⚠️ 數據源缺失)"
        else:
            max_pain = float(mp_val)
            pain_dist = float(max_pain_data.get("distance_pct") or 0.0)
            max_pain_str = f"${max_pain:.2f} (當前價差: {pain_dist:+.2f}%)"
    else:
        max_pain_str = "N/A (⚠️ 數據源缺失)"

    vol_pcr_val = metrics.volume_pcr if metrics else None
    oi_pcr_val = metrics.oi_pcr if metrics else None

    pcr_dict = pcr_data if isinstance(pcr_data, dict) else {}
    if pcr_dict:
        vol_pcr_val = pcr_dict.get("volume_pcr", vol_pcr_val)
        oi_pcr_val = pcr_dict.get("oi_pcr", pcr_dict.get("pcr", oi_pcr_val))

    if is_premarket or vol_pcr_val == 0.0 or vol_pcr_val is None:
        vol_pcr_status = "⚖️ 封盤中 (盤前未更新)"
        vol_pcr_str = "--"
    else:
        vol_pcr_str = f"{vol_pcr_val:.2f}"
        if "volume_pcr_state" in pcr_dict:
            vol_pcr_status = pcr_dict["volume_pcr_state"]
        elif vol_pcr_val < 0.90:
            vol_pcr_status = "🐂 中性偏多/看漲主導"
        elif vol_pcr_val > 1.10:
            vol_pcr_status = "🐻 偏向空頭/看空主導"
        else:
            vol_pcr_status = "⚖️ 結構平衡"

    if oi_pcr_val == 0.0 or oi_pcr_val is None:
        oi_pcr_status = "N/A (結構缺失)"
        oi_pcr_str = "--"
    else:
        oi_pcr_str = f"{oi_pcr_val:.2f}"
        if "oi_pcr_state" in pcr_dict:
            oi_pcr_status = pcr_dict["oi_pcr_state"]
        elif oi_pcr_val < 0.90:
            oi_pcr_status = "🏹 結構激進/看漲多頭沉澱"
        elif oi_pcr_val > 1.20:
            oi_pcr_status = "🛡️ 結構防禦/虛值 Put 沉澱"
        else:
            oi_pcr_status = "⚖️ 籌碼結構中性"

    gex_dist = (
        ((live_price - gex_putwall) / gex_putwall * 100.0)
        if gex_putwall and gex_putwall > 0
        else None
    )
    change_emoji = "📈" if change_raw >= 0.0 else "📉"

    # Unusual Options Activity (UOA) table formatting
    uoa_table_lines = []
    if uoa_list:
        for item in uoa_list[:3]:
            exp = item.get("expiry", "")
            strike_val = float(item.get("strike", 0.0))
            opt_type = str(item.get("type", "")).upper()
            action = item.get("action", "")
            vol_val = int(item.get("volume", 0))
            ratio_str = item.get("ratio_str", "0.00x")
            intent = item.get("intent", "")

            uoa_table_lines.append(
                f" {exp:<10} | ${strike_val:<9.2f} | {opt_type:<4} | {action:<21} | +{vol_val:<8,} | {ratio_str:<6} | {intent}"
            )
    if not uoa_table_lines:
        uoa_table_lines.append(" (目前未偵測到符合篩選標準的異常期權交易流量)")

    # Holding status
    shares_str = f"{int(holding_quantity)}" if holding_quantity is not None else "0"
    avg_cost_str = (
        f"${holding_avg_cost:.2f}" if holding_avg_cost is not None else "$0.00"
    )
    pnl_str = (
        f"{holding_pnl_pct * 100:+.2f}%" if holding_pnl_pct is not None else "0.00%"
    )

    inst_str = (
        option_guidance
        or "價格仍在防守框架內，維持現貨 $1.00×$ 零槓桿死守，將雙手嚴格離開期權開倉鍵。"
    )

    if option_plan:
        strat_name = getattr(option_plan, "strategy_name", "")
        if strat_name:
            inst_str += f"\n\n【動態期權計畫 (僅供參考)】\n ├─ 策略路由: {strat_name}"
            legs = getattr(option_plan, "legs", [])
            for leg in legs:
                action = getattr(leg, "action", "")
                opt_type = getattr(leg, "opt_type", "")
                strike = getattr(leg, "strike", 0.0)
                mid = getattr(leg, "mid_price", 0.0)
                expiry = getattr(leg, "expiry", "")
                inst_str += f"\n ├─ 合約: {action} {opt_type} {expiry} ${strike:.2f} (Mid: ${mid:.2f})"
            contracts = getattr(option_plan, "suggested_contracts", 0)
            risk = getattr(option_plan, "max_risk_amount", 0.0)
            inst_str += f"\n └─ 建議口數上限: {contracts} 口 (風險配額: ${risk:.2f})"

    is_degraded = (
        is_premarket
        or iv_source == "UNAVAILABLE"
        or iv_val is None
        or iv_rank is None
        or gex_putwall is None
        or skew_val is None
        or skew_per is None
    )

    gex_putwall_str = (
        f"${gex_putwall:.2f}" if gex_putwall is not None and gex_putwall > 0 else "N/A"
    )
    gex_dist_str = f"{gex_dist:+.2f}%" if gex_dist is not None else "--%"
    vol_poc_str = f"${vol_poc:.2f}" if vol_poc is not None else "N/A"
    skew_val_str = f"{skew_val:+.2f}%" if skew_val is not None else "--%"
    skew_per_str = f"{skew_per:.1f}%" if skew_per is not None else "--%"
    iv_val_str = f"{iv_val:.1f}%" if iv_val is not None else "--%"
    iv_rank_str = f"{iv_rank:.1f}%" if iv_rank is not None else "--%"
    expected_move_str = (
        f"±${expected_move:.2f}"
        if expected_move is not None and expected_move > 0
        else "N/A"
    )

    degraded_tag = " [數據未更新/降級模式]" if is_degraded else ""

    lines = [
        "```ansi",
        f" 標的分析中心 2.0: {symbol} 每半小時戰場心跳 (Watchlist Heartbeat){degraded_tag}",
        f" [{timestamp_str} - UTC+8] ｜ 系統狀態: {sys_status}",
    ]

    if show_live_price:
        lines.extend(
            [
                "",
                " 🏷️ 當前現價 (Current Price)",
                f" ├─ 現價: ${live_price:.2f} ({change_emoji} {change_pct:+.2f}% / ${change_raw:+.2f})",
                f" └─ 今日區間: 開盤: {open_val:.2f} | 最高: {high_val:.2f} | 最低: {low_val:.2f} | 前收: {prev_close:.2f}",
            ]
        )

    if show_market_footprints:
        lines.extend(
            [
                "",
                " 🧱 物理籌碼牆與邊緣偵測 (Market Footprints)",
                f" ├─ GEX PutWall (做市商底牆): {gex_putwall_str} (當前價差: {gex_dist_str})",
                f" ├─ Vol POC (籌碼控制中心): {vol_poc_str}",
                f" └─ Option Skew (期權偏斜): {skew_val_str} (分位點: {skew_per_str})",
            ]
        )

    if show_market_footprints and (
        symbol_gex
        and "gex_profile" in symbol_gex
        and isinstance(symbol_gex["gex_profile"], dict)
        and symbol_gex["gex_profile"]
    ):
        try:
            gex_prof = symbol_gex["gex_profile"]
            strike_keys = sorted([float(k) for k in gex_prof.keys()])
            if strike_keys:
                closest_idx = min(
                    range(len(strike_keys)),
                    key=lambda i: abs(strike_keys[i] - live_price),
                )
                start_idx = max(0, closest_idx - 3)
                end_idx = min(len(strike_keys), closest_idx + 4)
                display_strikes = strike_keys[start_idx:end_idx]

                def _safe_gex(k_val: float) -> float:
                    val = gex_prof.get(str(k_val), gex_prof.get(k_val))
                    try:
                        return float(val) if val is not None else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                max_abs_gex = max([abs(_safe_gex(k)) for k in display_strikes])
                max_abs_gex = max(max_abs_gex, 1.0)

                is_stale = bool(symbol_gex.get("_is_stale_cache", False))
                stale_suffix = " [快取 / API 降級]" if is_stale else ""
                lines.append("")
                lines.append(f" 🧲 Gamma 曝險分布 (GEX Profile Matrix){stale_suffix}")
                lines.append(" ┌─ 履約價(Strike) ─ 曝險熱力圖 ─ [K]")
                for i, k in enumerate(reversed(display_strikes)):
                    v = _safe_gex(k)
                    bars = int((abs(v) / max_abs_gex) * 10)
                    bar_str = "█" * bars + "░" * (10 - bars)
                    if v > 0:
                        color_prefix = "\u001b[1;32m"
                        sign = "+"
                    elif v < 0:
                        color_prefix = "\u001b[1;31m"
                        sign = "-"
                    else:
                        color_prefix = "\u001b[1;30m"
                        sign = " "

                    spot_marker = (
                        "📍" if abs(k - live_price) < (live_price * 0.01) else "  "
                    )
                    formatted_val = f"{sign}{abs(v)/1000:.0f}K"
                    prefix = " ├─" if i < len(display_strikes) - 1 else " └─"
                    lines.append(
                        f"{prefix} {spot_marker}{k:>7.2f} | {color_prefix}{bar_str}\u001b[0m | {formatted_val:>8}"
                    )
        except Exception as e:
            lines.append(f" └─ [GEX 面板載入失敗: {e}]")

    if show_iv_context:
        lines.extend(
            [
                "",
                " 📉 隱含波動率與預期空間 (IV Context)",
                f" ├─ Implied Volatility (IV): {iv_val_str} ｜ IV Rank: {iv_rank_str} ({iv_status_str})",
            ]
        )

        if iv_term_status and iv_term_ratio is not None:
            if iv_term_status == "Backwardation":
                term_prefix = "⚠️ [Backwardation]"
            elif iv_term_status == "Contango":
                term_prefix = "🟩 [Contango]"
            else:
                term_prefix = "⚖️ [Normal]"
            lines.append(
                f" ├─ IV Term Structure (期限結構): {term_prefix} (近遠月比: {iv_term_ratio:.2f})"
            )

        if earnings_loading or macro_loading:
            lines.extend(
                [
                    f" ├─ 本週預期波幅 (Expected Move): {expected_move_str}",
                    " └─ 備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。",
                ]
            )
        else:
            lines.append(f" └─ 本週預期波幅 (Expected Move): {expected_move_str}")

    if show_target_lock:
        lines.extend(
            [
                "",
                " 🎯 結算與目標 (Target Lock)",
                f" ├─ 最大痛點結算 (Max Pain): {max_pain_str}",
                f" ├─ Volume PCR (即時情緒): {vol_pcr_str} (狀態: {vol_pcr_status})",
                f" └─ OI PCR (結構防禦): {oi_pcr_str} (狀態: {oi_pcr_status})",
            ]
        )

    if show_uoa:
        lines.extend(
            [
                "",
                " 🔎 異常活動穿透 (Directional UOA - Bid/Ask Track)",
                " 到期日     | 履約價      | 類型 | 交易流向 [買/賣]      | 機構/OI    | 比例   | 戰略意圖映射",
                " ---------------------------------------------------------------------------------------",
            ]
        )
        lines.extend(uoa_table_lines)

    if show_risk_alignment:
        lines.extend(
            [
                "",
                " 💼 賬戶股權防護指引 (Holding & Risk Alignment Guide)",
                f" ├─ 既有現貨持倉: {shares_str} 股 ｜ 平均成本: {avg_cost_str} ｜ 當前損益: {pnl_str}",
            ]
        )
        if not has_position and buy_rationale:
            lines.append(f" ├─ 量化建倉解讀: {buy_rationale}")
        if has_position and sell_rationale:
            lines.append(f" ├─ 量化止盈解讀: {sell_rationale}")

        lines.extend(
            [
                f" └─ 操盤執行指南: {inst_str}",
            ]
        )

    lines.append("```")
    description = "\n".join(lines)

    color_val = {
        "red": discord.Color.red(),
        "yellow": discord.Color.orange(),
        "green": discord.Color.green(),
    }.get(alert_level, discord.Color.blurple())

    tag_str = f" 🏷️ {' | '.join(symbol_tags)}" if symbol_tags else ""
    embed_title = f"標的分析中心 2.0: {symbol} 每半小時戰場心跳{tag_str}"
    if is_degraded:
        embed_title += " [數據未更新/降級模式]"

    embed: discord.Embed
    try:
        embed = NexusEmbed(
            title=embed_title,
            description=description,
            color=color_val,
            timestamp=datetime.now(timezone.utc),
        )
    except NameError:
        embed = discord.Embed(
            title=embed_title,
            description=description,
            color=color_val,
            timestamp=datetime.now(timezone.utc),
        )

    if show_telemetry and telemetry_alignment_note:
        try:
            val = _safe_embed_field_value(telemetry_alignment_note, "暫無對齊建議")
        except NameError:
            val = (
                telemetry_alignment_note
                if len(telemetry_alignment_note) <= 1024
                else telemetry_alignment_note[:1021] + "..."
            )

        embed.add_field(
            name="📡 Telemetry 待成交委託單實時對齊建議",
            value=val,
            inline=False,
        )

    embed.set_footer(text="Watchlist Heartbeat | 核心作戰雷達每 30 分鐘自動校準")
    return embed


def create_watchlist_overview_embed(
    summary_items: List[Dict[str, str]],
    llm_overview: str | None = None,
) -> discord.Embed:
    """建立單一使用者本輪 watchlist 總覽摘要 Embed。"""
    scenario_labels = {
        "hard-hedge": "防守對沖",
        "premium-harvest": "權利金佈局",
        "wait": "觀望待機",
    }
    priority = {"red": 0, "yellow": 1, "green": 2}
    icon_map = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
    ordered_items = sorted(
        summary_items,
        key=lambda item: (
            priority.get(item.get("alert_level", ""), 3),
            item.get("symbol", ""),
        ),
    )
    counts = {
        level: sum(1 for item in ordered_items if item.get("alert_level") == level)
        for level in ("red", "yellow", "green")
    }
    embed_color = (
        discord.Color.red()
        if counts["red"] > 0
        else discord.Color.orange()
        if counts["yellow"] > 0
        else discord.Color.green()
    )

    embed = discord.Embed(
        title="🧭 本輪 Watchlist 總覽",
        description=(
            f"**追蹤標的：** `{len(ordered_items)}` ｜ "
            f"🔴 `{counts['red']}` ｜ 🟡 `{counts['yellow']}` ｜ 🟢 `{counts['green']}`\n"
            "先看高優先標的，再回頭逐則檢查個別 heartbeat。"
        ),
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    focus_lines = []
    for item in ordered_items[:3]:
        icon = icon_map.get(item.get("alert_level", ""), "🔵")
        scenario_label = scenario_labels.get(
            item.get("scenario", ""),
            item.get("scenario", "觀望待機"),
        )
        pnl_suffix = ""
        if "holding_pnl_pct" in item and item["holding_pnl_pct"] is not None:
            pnl_val = float(item["holding_pnl_pct"]) * 100
            pnl_icon = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
            pnl_suffix = f" ｜ {pnl_icon} 現貨損益: `{pnl_val:+.2f}%`"
        focus_lines.append(
            f"{icon} {item.get('symbol', 'N/A')}｜{item.get('skew_state', 'N/A')}｜{scenario_label}{pnl_suffix}"
        )
        focus_lines.append(
            f"事件：{item.get('event_risk_summary', '未偵測到近期重大事件')}"
        )
    if not focus_lines:
        focus_lines = ["本輪無可用 watchlist 評估結果。"]
    embed.add_field(
        name="🎯 本輪焦點",
        value=_safe_embed_field_value("\n".join(focus_lines), "暫無重點"),
        inline=False,
    )

    overview_lines = []
    for item in ordered_items:
        icon = icon_map.get(item.get("alert_level", ""), "🔵")
        scenario_label = scenario_labels.get(
            item.get("scenario", ""),
            item.get("scenario", "觀望待機"),
        )
        pnl_suffix = ""
        if "holding_pnl_pct" in item and item["holding_pnl_pct"] is not None:
            pnl_val = float(item["holding_pnl_pct"]) * 100
            pnl_icon = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
            pnl_suffix = f" ｜ {pnl_icon} 現貨損益: `{pnl_val:+.2f}%`"
        overview_lines.append(
            f"{icon} {item.get('symbol', 'N/A')}｜{item.get('skew_state', 'N/A')}｜{scenario_label}{pnl_suffix}"
        )
    if not overview_lines:
        embed.add_field(
            name="📋 全標的速覽",
            value=_safe_embed_field_value("", "暫無總覽"),
            inline=False,
        )
    else:
        chunk_size = 15
        total_chunks = (len(overview_lines) + chunk_size - 1) // chunk_size
        for i in range(total_chunks):
            chunk = overview_lines[i * chunk_size : (i + 1) * chunk_size]
            field_name = (
                "📋 全標的速覽"
                if total_chunks == 1
                else f"📋 全標的速覽 ({i+1}/{total_chunks})"
            )
            embed.add_field(
                name=field_name,
                value=_safe_embed_field_value("\n".join(chunk), "暫無總覽"),
                inline=False,
            )
    embed.add_field(
        name="🤖 LLM 本輪摘要",
        value=_safe_embed_field_value(
            llm_overview or "",
            "暫無本輪 LLM 摘要，請優先查看紅 / 黃燈標的與事件風控。",
        ),
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker Watchlist Roundup | 每 30 分鐘更新")
    return embed
