"""Telemetry 委託單實時對齊警報 Embed 建構函式。"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import discord

from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _display_width,
    _to_float_or_none,
    _wrap_visual,
    fmt_or_dash,
)
from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    no_title_suffix,
    split_embed_by_fields,
)


# Discord 欄位值上限 1024；扣除 `_safe_embed_field_value` 附加的尾端間距後，卡片須 <= 1020
_CARD_CHAR_BUDGET = 1020
_MOBILE_LINE_WIDTH = 40

# 調色盤語意色（皆屬 CURATED_PALETTE）
_COLOR_SPECIAL = 0x8E44AD  # 尾端風險控倉 (size-down)
_COLOR_WARNING = 0xF39C12  # 有調整建議
_COLOR_INFO = 0x3498DB  # 無須調整
_COLOR_NEUTRAL = 0x99AAB5  # 無即時數據

_RESET = "\u001b[0m"

_SYSTEM_FLAG_TRANSLATIONS: dict[str, str] = {
    "TELEMETRY ACTIVE": "遙測運作中",
    "FORTRESS RE-LOCKED": "防線鎖定（暫停追價）",
    "BUY-ONLY ENGINE": "僅支援買單",
    "DATA DEGRADED": "數據降級",
}

_IV_STATUS_TRANSLATIONS: dict[str, str] = {
    "Low": "偏低",
    "Normal": "正常",
    "High": "偏高",
    "Extreme": "極高",
    "UNAVAILABLE": "不可用",
}

_RADAR_STATUS_TRANSLATIONS: dict[str, str] = {
    "FORTRESS RE-LOCKED": "防線鎖定（暫停追價）",
}


def _c(code: str, text: Any) -> str:
    """以 ANSI 粗體色碼包裹文字。"""
    return f"\u001b[1;{code}m{text}{_RESET}"


def _suggestion_changed(item: Dict[str, Any]) -> bool:
    """該筆是否有調整建議；缺 `suggestion_changed` 時退回比較價量。"""
    if item.get("alignment_available") is False:
        return False
    flag = item.get("suggestion_changed")
    if flag is not None:
        return bool(flag)
    sug_price = _to_float_or_none(item.get("suggested_price"))
    sug_qty = _to_float_or_none(item.get("suggested_qty"))
    order_price = _to_float_or_none(item.get("current_price"))
    order_qty = _to_float_or_none(item.get("original_qty"))
    if sug_price is not None and order_price is not None:
        if abs(sug_price - order_price) >= 0.01:
            return True
    if sug_qty is not None and order_qty is not None:
        if int(round(sug_qty)) != int(round(order_qty)):
            return True
    return False


def _order_type_zh(order_type: str) -> str:
    if order_type == "STOP_LIMIT":
        return "停損限價"
    if "LIMIT" in order_type:
        return "限價"
    if "STOP" in order_type:
        return "停損"
    return order_type


def _truncate_text(text: str, max_len: int) -> str:
    """截斷文字並以 `…` 收尾；max_len 內不動。"""
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def _build_card_and_tags(item: Dict[str, Any]) -> Tuple[str, List[str]]:
    """建構 Telemetry 實時對齊快照卡片。回傳 (卡片內容, 狀態／來源標籤清單)。"""
    sym = str(item.get("symbol") or "").upper()
    # 注意：item["current_price"] 是委託單限價，不是現價；缺 live_price 時不得以限價冒充
    live_price_val = _to_float_or_none(item.get("live_price"))
    live_price = live_price_val if live_price_val and live_price_val > 0 else None

    avg_cost_val = _to_float_or_none(item.get("avg_cost"))
    avg_cost = avg_cost_val if avg_cost_val is not None and avg_cost_val > 0 else None
    gain_loss_pct = _to_float_or_none(item.get("gain_loss_pct"))

    put_wall_val = _to_float_or_none(item.get("put_wall"))
    put_wall = put_wall_val if put_wall_val is not None and put_wall_val > 0 else None
    wall_dist_pct = _to_float_or_none(item.get("wall_dist_pct"))
    max_pain = _to_float_or_none(item.get("max_pain_price"))
    if max_pain is not None and max_pain <= 0:
        max_pain = None

    order_price = _to_float_or_none(item.get("current_price"))
    shares = int(round(float(_to_float_or_none(item.get("original_qty")) or 0.0)))
    proximity_pct = _to_float_or_none(item.get("proximity_pct"))

    skew_val = _to_float_or_none(item.get("skew_val"))
    skew_pct = _to_float_or_none(item.get("skew_pct"))
    iv_val = _to_float_or_none(item.get("iv_val"))
    iv_rank = _to_float_or_none(item.get("iv_rank"))

    skew_val_str = f"{skew_val:+.2f}%" if skew_val is not None else "--%"
    skew_pct_str = f"{skew_pct:.2f}%" if skew_pct is not None else "--%"
    wall_dist_str = (
        f"{wall_dist_pct:+.2f}%"
        if wall_dist_pct is not None and put_wall is not None
        else "--%"
    )
    proximity_str = fmt_or_dash(proximity_pct, ".2f", suffix="%", placeholder="--%")

    is_premarket = bool(item.get("is_premarket", False))
    iv_source = item.get("iv_source")
    raw_iv_status = str(item.get("iv_status") or "Normal")

    iv_unavailable = (
        iv_val is None
        or iv_source == "UNAVAILABLE"
        or raw_iv_status == "UNAVAILABLE"
        or (is_premarket and iv_val == 0.0)
    )

    is_degraded = (
        live_price is None
        or put_wall is None
        or skew_val is None
        or iv_unavailable
        or item.get("skew_status") in ("ERROR", "N/A")
    )

    # 狀態／來源標籤：§2.3 用語；盤前（非正常盤）加「盤前/」前綴
    tags: List[str] = []
    prefix = "盤前/" if is_premarket else ""
    iv_tail = ""  # IV 值的來源標註
    if iv_unavailable:
        iv_val_str = "--%"
        iv_rank_str = "--%"
        if is_premarket:
            tags.append("盤前數據未更新")
            iv_status = "等待開盤"
        else:
            tags.append("數據未更新/降級模式")
            iv_status = "UNAVAILABLE"
    else:
        if iv_source == "STORED_IV":
            tags.append(f"{prefix}前日收盤")
            iv_tail = " (前日收盤)"
        elif iv_source == "HV_PROXY":
            tags.append(f"{prefix}HV代理")
            iv_tail = " (歷史波動率代理)"
        if item.get("iv_event_loading_applied"):
            iv_tail += " (含事件加載)"
        iv_val_str = f"{iv_val:.2f}%" if iv_val is not None else "--%"
        iv_rank_str = f"{iv_rank:.2f}%" if iv_rank is not None else "--%"
        iv_status = raw_iv_status
        if is_degraded:
            tags.append("數據未更新/降級模式")

    if is_degraded and not tags:
        tags.append("數據未更新/降級模式")

    # ---- ANSI 著色與文字轉換 ----
    holding_shares = int(_to_float_or_none(item.get("holding_shares")) or 0)
    holding_status = str(item.get("holding_status") or "待確認")
    if "空頭" in holding_status:
        holding_status_color = _c("31", holding_status)
    elif "空倉" in holding_status:
        holding_status_color = _c("30", holding_status)
    elif "持倉" in holding_status or "ACTIVE" in holding_status:
        holding_status_color = _c("32", holding_status)
    else:
        holding_status_color = _c("33", holding_status)
    holding_type_label = str(item.get("holding_type_label") or "--")

    if gain_loss_pct is None:
        pnl_color = _c("30", "--%")
    else:
        pnl_text = f"{gain_loss_pct:+.2f}%"
        if gain_loss_pct > 0.0:
            pnl_color = _c("32", pnl_text)
        elif gain_loss_pct < 0.0:
            pnl_color = _c("31", pnl_text)
        else:
            pnl_color = _c("30", pnl_text)

    wall_status = str(item.get("wall_status") or "待確認")
    if "跌破" in wall_status or "BREAK" in wall_status or "CRITICAL" in wall_status:
        wall_status_color = _c("31", wall_status)
    elif "上方" in wall_status or "SAFE" in wall_status or "BUFFER" in wall_status:
        wall_status_color = _c("32", wall_status)
    else:
        wall_status_color = _c("33", wall_status)

    skew_status = str(item.get("skew_status") or "平穩")
    if any(k in skew_status for k in ("極端", "警告", "ERROR", "下行")):
        skew_status_color_code = "31"
    elif "平穩" in skew_status or "Normal" in skew_status:
        skew_status_color_code = "32"
    else:
        skew_status_color_code = "33"
    skew_status_color = _c(skew_status_color_code, skew_status)

    iv_status_up = iv_status.upper()
    if "等待開盤" in iv_status or "樣本累積" in iv_status:
        iv_status_color_code = "33"
    elif (
        "UNAVAILABLE" in iv_status_up
        or "ERROR" in iv_status_up
        or "HIGH" in iv_status_up
        or "EXTREME" in iv_status_up
        or "OVERHEATED" in iv_status_up
        or "泡沫" in iv_status
    ):
        iv_status_color_code = "31"
    else:
        iv_status_color_code = "32"
    iv_status_zh = _IV_STATUS_TRANSLATIONS.get(iv_status, iv_status)
    iv_status_color = _c(iv_status_color_code, iv_status_zh)

    if iv_rank is not None and not iv_unavailable:
        if iv_rank >= 80.0:
            iv_rank_color = _c("31", iv_rank_str)
        elif iv_rank >= 50.0:
            iv_rank_color = _c("33", iv_rank_str)
        else:
            iv_rank_color = _c("32", iv_rank_str)
    else:
        iv_rank_color = _c("30", iv_rank_str)

    radar_status = str(item.get("radar_status") or "監控中")
    if any(k in radar_status for k in ("FORTRESS", "SUPPRESSED", "偏離", "缺失")):
        radar_status_color_code = "31"
    elif "鎖定" in radar_status or "LOCKED" in radar_status:
        radar_status_color_code = "32"
    else:
        radar_status_color_code = "33"
    radar_status_zh = _RADAR_STATUS_TRANSLATIONS.get(radar_status, radar_status)
    radar_status_color = _c(radar_status_color_code, radar_status_zh)

    sys_flag = str(item.get("system_status_flag") or "TELEMETRY ACTIVE")
    if "ACTIVE" in sys_flag or "RUNNING" in sys_flag:
        sys_flag_color_code = "32"
    elif "LOCKED" in sys_flag or "SUPPRESSED" in sys_flag or "DEGRADED" in sys_flag:
        sys_flag_color_code = "31"
    else:
        sys_flag_color_code = "33"
    sys_flag_zh = _SYSTEM_FLAG_TRANSLATIONS.get(sys_flag, sys_flag)
    sys_flag_color = _c(sys_flag_color_code, sys_flag_zh)

    # ---- 對齊建議 ----
    is_changed = _suggestion_changed(item)
    if item.get("alignment_available") is False:
        suggestion_val_str = _c("33", "暫停（現價缺失）")
        suggestion_adj_line = None
    elif is_changed:
        sug_price = _to_float_or_none(item.get("suggested_price"))
        sug_qty_val = _to_float_or_none(item.get("suggested_qty"))
        sug_qty = int(round(sug_qty_val)) if sug_qty_val is not None else shares
        is_size_down = item.get("is_size_down")
        if is_size_down is None:
            is_size_down = sug_qty < shares
        qty_code = "35" if is_size_down else "37"

        delta_str = "--%"
        if sug_price is not None and order_price is not None and order_price > 0:
            delta_str = f"{(sug_price - order_price) / order_price * 100:+.2f}%"

        suggestion_val_str = (
            f"{_c('33', fmt_or_dash(sug_price, prefix='$'))} × "
            f"{_c(qty_code, sug_qty)} 股"
        )

        adj_parts = [f"Δ{delta_str}"]
        if is_size_down and shares > 0:
            qty_pct = (sug_qty - shares) / shares * 100
            qty_pct_str = (
                f"{qty_pct:.0f}%"
                if abs(qty_pct - round(qty_pct)) < 1e-6
                else f"{qty_pct:.2f}%"
            )
            adj_parts.append(_c("35", f"縮量 {qty_pct_str}"))
        suggestion_adj_line = f" ├─ 調整: {' ｜ '.join(adj_parts)}"
    else:
        suggestion_val_str = _c("30", "維持原價原量")
        suggestion_adj_line = None

    # ---- 指引清洗 ----
    raw_dir = " ".join(
        str(
            item.get("system_instruction_directive") or "通過實時防線，維持紀律掛單。"
        ).split()
    )
    if sym:
        exact_prefix = f"🛡️ 標的: {sym} | "
        if raw_dir.startswith(exact_prefix):
            raw_dir = raw_dir[len(exact_prefix) :]

    wall_tail = " [快取]" if item.get("put_wall_is_stale") else ""

    def _assemble(directive_text: str, include_divider: bool = True) -> List[str]:
        lines = ["```ansi"]
        lines.append("🎯 \u001b[1;36m對齊建議\u001b[0m")
        lines.append(
            f" ├─ 原掛單: {_c('33', fmt_or_dash(order_price, prefix='$'))} × "
            f"{_c('37', shares)} 股"
        )
        if suggestion_adj_line is not None:
            lines.append(f" ├─ 建議值: {suggestion_val_str}")
            lines.append(suggestion_adj_line)
        else:
            lines.append(f" ├─ 建議值: {suggestion_val_str}")
        lines.append(f" └─ 引擎: {sys_flag_color}")

        lines.append("💬 \u001b[1;36m指引\u001b[0m")
        dir_wrapped = _wrap_visual(directive_text, _MOBILE_LINE_WIDTH - 4)
        if not dir_wrapped:
            dir_wrapped = [""]
        lines.append(f" └─ {_c('33', dir_wrapped[0])}")
        for dw in dir_wrapped[1:]:
            lines.append(f"    {_c('33', dw)}")

        divider_idx = len(lines)

        lines.append("🛡️ \u001b[1;36m現價與持倉\u001b[0m")
        lines.append(
            f" ├─ 現價: {_c('37', fmt_or_dash(live_price, prefix='$'))} ｜ "
            f"損益: {pnl_color}"
        )

        prox_line = f" ├─ 距成交: {_c('37', proximity_str)} [{radar_status_color}]"
        if _display_width(_clean_ansi(prox_line)) <= _MOBILE_LINE_WIDTH:
            lines.append(prox_line)
        else:
            lines.append(f" ├─ 距成交: {_c('37', proximity_str)}")
            lines.append(f"    [{radar_status_color}]")

        holding_prefix = " ├─ 持倉: "
        if _display_width(holding_prefix + holding_type_label) <= _MOBILE_LINE_WIDTH:
            lines.append(f"{holding_prefix}{_c('36', holding_type_label)}")
        else:
            avail = _MOBILE_LINE_WIDTH - _display_width(holding_prefix)
            ht_wrapped = _wrap_visual(holding_type_label, avail)
            if ht_wrapped:
                lines.append(f"{holding_prefix}{_c('36', ht_wrapped[0])}")
                for hw in ht_wrapped[1:]:
                    lines.append(f"    {_c('36', hw)}")
            else:
                lines.append(f"{holding_prefix}{_c('36', holding_type_label)}")

        lines.append(
            f" ├─ 持股: {_c('37', f'{holding_shares} 股')} [{holding_status_color}]"
        )
        lines.append(f" └─ 均價: {_c('33', fmt_or_dash(avg_cost, prefix='$'))}")

        lines.append("🧱 \u001b[1;36m做市商防線\u001b[0m")
        lines.append(
            f" ├─ GEX PutWall: {_c('33', fmt_or_dash(put_wall, prefix='$'))}{wall_tail}"
        )
        wall_dist_line = (
            f" ├─ 距硬支撐: {_c('37', wall_dist_str)} [{wall_status_color}]"
        )
        if _display_width(_clean_ansi(wall_dist_line)) <= _MOBILE_LINE_WIDTH:
            lines.append(wall_dist_line)
        else:
            lines.append(f" ├─ 距硬支撐: {_c('37', wall_dist_str)}")
            lines.append(f"    [{wall_status_color}]")
        lines.append(f" └─ 最大痛點: {_c('33', fmt_or_dash(max_pain, prefix='$'))}")

        lines.append("📐 \u001b[1;36m波動與偏斜\u001b[0m")
        lines.append(
            f" ├─ Skew: {_c('37', skew_val_str)} ｜ " f"分位: {_c('37', skew_pct_str)}"
        )
        skew_prefix = " ├─ Skew 狀態: "
        if _display_width(skew_prefix + skew_status) <= _MOBILE_LINE_WIDTH:
            lines.append(f"{skew_prefix}{skew_status_color}")
        else:
            avail = _MOBILE_LINE_WIDTH - _display_width(skew_prefix)
            ss_wrapped = _wrap_visual(skew_status, avail)
            if ss_wrapped:
                lines.append(
                    f"{skew_prefix}{_c(skew_status_color_code, ss_wrapped[0])}"
                )
                for sw in ss_wrapped[1:]:
                    lines.append(f"    {_c(skew_status_color_code, sw)}")
            else:
                lines.append(f"{skew_prefix}{skew_status_color}")

        iv_line = f" ├─ IV: {_c('37', iv_val_str)}{iv_tail}"
        if _display_width(_clean_ansi(iv_line)) <= _MOBILE_LINE_WIDTH:
            lines.append(iv_line)
        else:
            tail_wrapped = _wrap_visual(iv_tail.strip(), _MOBILE_LINE_WIDTH - 4)
            lines.append(f" ├─ IV: {_c('37', iv_val_str)}")
            for tw in tail_wrapped:
                lines.append(f"    {tw}")

        iv_rank_line = f" └─ IV Rank: {iv_rank_color} ｜ 狀態: {iv_status_color}"
        if _display_width(_clean_ansi(iv_rank_line)) <= _MOBILE_LINE_WIDTH:
            lines.append(iv_rank_line)
        else:
            lines.append(f" ├─ IV Rank: {iv_rank_color}")
            lines.append(f" └─ 狀態: {iv_status_color}")

        if include_divider:
            content_max = max(
                (_display_width(_clean_ansi(ln)) for ln in lines[1:]),
                default=0,
            )
            div_len = min(content_max, _MOBILE_LINE_WIDTH) - 1
            if div_len >= 10:
                lines.insert(divider_idx, " " + "-" * div_len)

        lines.append("```")
        return lines

    directive = raw_dir
    lines = _assemble(directive, include_divider=True)
    card = "\n".join(lines)
    if len(card) > _CARD_CHAR_BUDGET:
        over = len(card) - _CARD_CHAR_BUDGET
        directive = _truncate_text(directive, max(8, len(directive) - over))
        lines = _assemble(directive, include_divider=True)
        card = "\n".join(lines)
    if len(card) > _CARD_CHAR_BUDGET:
        lines = _assemble(directive, include_divider=False)
        card = "\n".join(lines)
    if len(card) > _CARD_CHAR_BUDGET:
        over = len(card) - _CARD_CHAR_BUDGET
        directive = _truncate_text(directive, max(1, len(directive) - over))
        lines = _assemble(directive, include_divider=False)
        card = "\n".join(lines)

    return card, tags


def _build_telemetry_alignment_ansi_card(item: Dict[str, Any]) -> Tuple[str, str]:
    """建構 Telemetry 實時對齊快照卡片。回傳 (卡片內容, 數據降級／來源狀態後綴)。"""
    card, tags = _build_card_and_tags(item)
    return card, "".join(f" [{t}]" for t in tags)


def _title_tags(all_tags: List[List[str]]) -> str:
    """彙整所有委託單的狀態／來源標籤（去重、保序）為標題後綴。"""
    seen: List[str] = []
    for tags in all_tags:
        for tag in tags:
            norm = "降級模式" if "降級" in tag else tag
            if norm not in seen:
                seen.append(norm)
    return "".join(f" [{t}]" for t in seen)


def create_telemetry_alignment_embeds(
    alignment_items: List[Dict[str, Any]],
    truncated: bool = False,
    include_apply_button_hint: bool = True,
    scheduled_mode: bool = False,
) -> List[discord.Embed]:
    """建構 Telemetry 對齊警報 (清單整合；超過字數限制自動拆訊息)。

    - 版面參照 `create_active_orders_embed()`（清單欄位式）
    - scheduled_mode=True: 盤中每半小時自動推播版本（不含按鈕）
    - 標題彙整所有委託單的來源／狀態標籤；顏色依語意：尾端縮量 > 有調整建議 >
      無須調整 > 全數無即時數據
    """

    title = "🌌 快照：待成交委託單實時對齊"
    if scheduled_mode:
        title += " (盤中每半小時)"

    built = [_build_card_and_tags(i) for i in alignment_items]
    all_cards = [card for card, _ in built]
    all_tags = [tags for _, tags in built]
    title += _title_tags(all_tags)

    changed_count = sum(1 for i in alignment_items if _suggestion_changed(i))
    has_live = [
        i
        for i in alignment_items
        if i.get("alignment_available") is not False
        and _to_float_or_none(i.get("live_price")) is not None
    ]
    if alignment_items and not has_live:
        color_value = _COLOR_NEUTRAL
    elif any(
        i.get("is_size_down") and i.get("alignment_available") is not False
        for i in alignment_items
    ):
        color_value = _COLOR_SPECIAL
    elif changed_count > 0:
        color_value = _COLOR_WARNING
    else:
        color_value = _COLOR_INFO
    color = discord.Color(color_value)

    if scheduled_mode:
        description_lines = [
            f"`{len(alignment_items)}` 筆委託單出現新的對齊建議（同一組建議當日只推播一次）。"
        ]
    else:
        description_lines = [
            f"共 `{len(alignment_items)}` 筆委託單，其中 `{changed_count}` 筆有調整建議。"
        ]

    if truncated:
        description_lines.append(
            "⚠️ 由於委託單數量較多，部分標的已被安全省略；可先用 `/list_orders` 查看完整列表。"
        )

    if include_apply_button_hint and not scheduled_mode:
        description_lines.append(
            "點擊下方按鈕可一鍵套用遙測建議價（將同步調整價格與股數）。"
        )
    else:
        description_lines.append(
            "此為通知版本（不含按鈕）。若要一鍵套用，請使用 `/telemetry_alert` 開啟互動面板。"
        )

    # 實施分頁原則：每頁最多封裝 10 個標的
    chunked_groups = [
        alignment_items[i : i + 10] for i in range(0, len(alignment_items), 10)
    ]
    all_embeds = []

    for chunk_idx, chunk in enumerate(chunked_groups):
        embed = NexusEmbed(
            title=title,
            description="\n".join(description_lines) + "\n​",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        for idx, item in enumerate(chunk):
            global_idx = chunk_idx * 10 + idx + 1
            sym = str(item.get("symbol") or "").upper()
            order_id = item.get("order_id")
            card_content = all_cards[global_idx - 1]
            degraded_suffix = "".join(f" [{t}]" for t in all_tags[global_idx - 1])

            side = str(item.get("side") or "BUY").upper()
            side_zh = "買入" if side == "BUY" else "賣出"
            order_type = str(item.get("order_type") or "LIMIT").upper()
            type_zh = _order_type_zh(order_type)

            desc_parts = []
            if sym:
                desc_parts.append(sym)
            desc_parts.append(f"{side_zh}{type_zh}")
            order_desc = " ".join(desc_parts)

            id_part = f" (ID: {order_id})" if order_id is not None else ""

            if order_desc:
                field_name = (
                    f"📦 #{global_idx} ｜ {order_desc}{id_part}{degraded_suffix}"
                )
            else:
                field_name = f"📦 #{global_idx}{id_part}{degraded_suffix}"

            embed.add_field(
                name=field_name,
                value=_safe_embed_field_value(card_content, "暫無遙測對齊詳情"),
                inline=False,
            )

        embed.set_footer(text="待成交委託單管理系統")
        sub_embeds = split_embed_by_fields(embed, title_suffix=no_title_suffix)
        all_embeds.extend(sub_embeds)

    # 動態頁碼標記：當分頁數大於 1 時，Embed Title 尾部動態添加 ` (第 X/Y 頁)` 標記。
    if len(all_embeds) > 1:
        for idx, emb in enumerate(all_embeds, start=1):
            emb.title = f"{emb.title or ''} (第 {idx}/{len(all_embeds)} 頁)"

    return all_embeds


def create_telemetry_alignment_embed(
    alignment_items: List[Dict[str, Any]],
    truncated: bool = False,
    include_apply_button_hint: bool = True,
    scheduled_mode: bool = False,
) -> discord.Embed:
    """相容舊呼叫：回傳第一頁 Embed。"""
    embeds = create_telemetry_alignment_embeds(
        alignment_items,
        truncated=truncated,
        include_apply_button_hint=include_apply_button_hint,
        scheduled_mode=scheduled_mode,
    )
    return embeds[0] if embeds else NexusEmbed(title="🌌 快照：待成交委託單實時對齊")
