"""Watchlist 心跳與總覽 Embed 建構函式。

包含：
- create_watchlist_embed：Watchlist 分頁清單
- create_watchlist_signal_embed：每半小時標的分析心跳 Embed（2.0 版）
- create_watchlist_overview_embed：本輪 Watchlist 總覽摘要
"""

from typing import Any, Optional
import discord

from datetime import datetime, timezone, timedelta
from typing import Dict, List

from cogs.embed_builders._ansi_utils import _pad_string
from cogs.embed_builders._embed_helpers import _safe_embed_field_value
from cogs.embed_builders._core import (
    NexusEmbed,
    format_market_cache_freshness_suffix,
    format_cache_age_suffix,
)
from database.market_cache import get_market_cache


def _classify_watchlist_cache_tag(
    spot: float, max_pain: float, em_lower: Optional[float]
) -> str:
    """依 AGENTS.md 文件化的 Local Rules Engine 門檻，將快取價相對 Max Pain 的
    偏離百分比分類為戰術標籤。刻意只依賴 `market_cache` 的輕量欄位
    (spot/max_pain/expected_move_lower)，不耦合 `/x` 全量掃描才有的 GEX/UOA 資料，
    以維持 `/list_watch` 零額外運算成本的特性。
    """
    if max_pain <= 0 or spot <= 0:
        return ""
    delta_mp_pct = (spot - max_pain) / max_pain * 100.0
    if em_lower and spot <= em_lower and delta_mp_pct < -5.0:
        return "🚀"
    if abs(delta_mp_pct) > 10.0:
        return "⚠️"
    return ""


def _format_watchlist_price_info(symbol: str) -> str:
    """讀取 `market_cache`（純本地 SQLite 快取，零額外網路呼叫）並格式化為
    「快取價 (MP偏離% 標籤)」摘要；快取不存在時回傳降級提示。"""
    cache = get_market_cache(symbol)
    if not cache or not cache.get("reference_spot_price"):
        return "-- (尚未預熱)"

    spot = float(cache["reference_spot_price"])
    max_pain = float(cache.get("max_pain") or 0.0)
    em_lower = cache.get("expected_move_lower")
    em_lower_f = float(em_lower) if em_lower is not None else None

    tag = _classify_watchlist_cache_tag(spot, max_pain, em_lower_f)
    tag_suffix = f" {tag}" if tag else ""

    if max_pain > 0:
        delta_mp_pct = (spot - max_pain) / max_pain * 100.0
        return f"${spot:.2f} (MP{delta_mp_pct:+.1f}%{tag_suffix})"
    return f"${spot:.2f}"


def create_watchlist_embed(
    page_data: Any,
    current_page: Any,
    total_pages: Any,
    total_items: Any,
    sort_label: Optional[str] = None,
    query: Optional[str] = None,
) -> discord.Embed:
    """生成觀察清單的分頁 Embed，附帶快取價與 Max Pain 偏離摘要"""

    if not page_data:
        description = (
            "目前沒有追蹤任何項目" if not query else f"沒有符合「{query}」的項目"
        )
    else:
        lines = ["```ansi"]
        header = f"{_pad_string('標的 [標籤]', 24)}{_pad_string('快取價 (MP偏離)', 20)}"
        lines.append(header)
        lines.append("-" * 44)

        for sym, tags in page_data:
            display_sym = f"{sym} [{tags}]" if tags else sym
            sym_fmt = _pad_string(display_sym, 24)
            price_info = _format_watchlist_price_info(sym)
            lines.append(f"{sym_fmt}{price_info}")

        lines.append("```")
        description = "\n".join(lines)

    filter_note_parts = []
    if sort_label:
        filter_note_parts.append(f"排序: {sort_label}")
    if query:
        filter_note_parts.append(f"搜尋: 「{query}」")
    filter_note = f"（{' ｜ '.join(filter_note_parts)}）" if filter_note_parts else ""

    embed = NexusEmbed(
        title="📡 【您的專屬觀察清單】",
        description=(
            f"目前監控中的標的清單{filter_note}。系統將每 30 分鐘自動執行量化掃描。"
            f"價格為每日盤前預熱之快取參考價，非即時報價。\n\n{description}"
        ),
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=f"頁次: {current_page}/{total_pages} ｜ 📊 總項目: {total_items}"
    )
    return embed


def create_bulk_watchlist_result_embed(
    action_label: str,
    succeeded: List[str],
    skipped: Optional[Dict[str, List[str]]] = None,
) -> discord.Embed:
    """生成批次新增/移除觀察清單標的的結果摘要 Embed。

    Args:
        action_label: 操作名稱，例如「加入」或「移除」。
        succeeded: 成功處理的標的代號列表。
        skipped: 依原因分類、未成功處理的標的代號，例如
            {"已存在": [...], "無效代號": [...], "已達上限": [...], "找不到": [...]}。
    """
    lines: List[str] = []
    if succeeded:
        lines.append(
            f"✅ **已{action_label}** ({len(succeeded)} 檔): {', '.join(succeeded)}"
        )
    else:
        lines.append(f"⚠️ 沒有任何標的被{action_label}。")

    for reason, symbols in (skipped or {}).items():
        if symbols:
            lines.append(f"❌ **{reason}** ({len(symbols)} 檔): {', '.join(symbols)}")

    color = discord.Color.green() if succeeded else discord.Color.orange()
    embed = NexusEmbed(
        title=f"📡 觀察清單批次{action_label}結果",
        description="\n".join(lines),
        color=color,
    )
    return embed


def _build_execution_suggestion_lines(
    *,
    has_position: bool,
    suitable_buy_price: Any,
    suitable_buy_shares: int | None,
    suitable_sell_price: Any,
    suitable_sell_shares: int | None,
    holding_quantity: float | None,
) -> list[str]:
    """組出「🎯 執行建議」區塊的 ANSI 行。

    未持倉走建倉側（買價 / 股數），已持倉走減碼側（賣價 / 股數）。
    `suitable_buy_price` 在風控戒嚴時會是一段中文字串（如
    「N/A（風控鎖定，暫不推薦開倉買方策略）」）而非數字，需原樣呈現而不是
    格式化成 $0.00 讓使用者誤以為有可執行價位。四個值都缺時回傳空 list，
    呼叫端據此整個略過此欄位。
    """
    lines: list[str] = []

    if not has_position:
        if isinstance(suitable_buy_price, str):
            lines.append(f" ├─ 建議買入價位: {suitable_buy_price}")
        elif suitable_buy_price is not None and float(suitable_buy_price) > 0.0:
            lines.append(f" ├─ 建議買入價位: ${float(suitable_buy_price):.2f}")

        if suitable_buy_shares is not None and int(suitable_buy_shares) > 0:
            shares = int(suitable_buy_shares)
            budget_note = ""
            if isinstance(suitable_buy_price, (int, float)) and suitable_buy_price > 0:
                budget_note = f" (約 ${shares * float(suitable_buy_price):,.0f})"
            lines.append(f" ├─ 建議建倉股數: {shares} 股{budget_note}")
        elif lines:
            lines.append(" ├─ 建議建倉股數: 0 股 (風控鎖定或預算不足)")
    else:
        if suitable_sell_price is not None and float(suitable_sell_price) > 0.0:
            lines.append(f" ├─ 建議止盈價位: ${float(suitable_sell_price):.2f}")

        if suitable_sell_shares is not None and int(suitable_sell_shares) > 0:
            shares = int(suitable_sell_shares)
            ratio_note = ""
            if holding_quantity is not None and float(holding_quantity) > 0.0:
                ratio_note = f" (佔持倉 {shares / float(holding_quantity) * 100:.0f}%)"
            lines.append(f" ├─ 建議減碼股數: {shares} 股{ratio_note}")

    if not lines:
        return []

    lines.append(
        " └─ ⚠️ 上述價位為量化參考值，非委託指令；請以「15 分鐘 K 線實體收盤」為最終確認。"
    )
    # 最後一行改用結尾角後，把前面所有行統一為分叉角。
    return [line.replace(" └─ ", " ├─ ", 1) for line in lines[:-1]] + [lines[-1]]


def create_watchlist_signal_embed(
    symbol: str,
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
    iv_metrics: Any | None = None,
    max_pain_data: dict | None = None,
    pcr_data: dict | None = None,
    uoa_list: list[dict] | None = None,
    symbol_gex: dict | None = None,
    toggles: dict[str, bool] | None = None,
    symbol_tags: list[str] | None = None,
) -> discord.Embed | None:
    """建立標的分析中心 2.0 • 戰場心跳快照 (Watchlist Heartbeat) 的 Markdown-ASCII 統一模板。"""
    has_meaningful_content = False

    toggles = toggles or {}
    # 本 embed 屬 30 分鐘個股深度心跳，開關為 heartbeat_symbol_deep；
    # 15 分鐘批次雷達 (build_radar_scan_embed) 才是 heartbeat_watchlist。
    hb_enabled = toggles.get("heartbeat_symbol_deep", True)
    show_market_footprints = hb_enabled
    show_iv_context = hb_enabled
    show_target_lock = hb_enabled
    show_uoa = hb_enabled
    show_risk_alignment = hb_enabled
    show_telemetry = hb_enabled

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
        skew_samples = getattr(metrics, "skew_sample_size", None)
        skew_is_fallback = bool(getattr(metrics, "skew_is_fallback", False))
    else:
        live_price_val = (
            suitable_buy_price if not isinstance(suitable_buy_price, str) else None
        )
        live_price = live_price_val or suitable_sell_price or 100.0
        gex_putwall = None
        vol_poc = 100.0
        skew_val = None
        skew_per = None
        skew_samples = None
        skew_is_fallback = False

    # Extract IV metrics
    earnings_loading = False
    macro_loading = False
    event_loading_applied = False
    iv_source = "UNAVAILABLE"

    if iv_metrics is not None:
        iv_val = (
            iv_metrics.current_iv * 100.0 if iv_metrics.current_iv is not None else None
        )
        iv_rank = iv_metrics.iv_rank
        iv_status_raw = (
            iv_metrics.iv_status.upper() if iv_metrics.iv_status else "NORMAL"
        )
        iv_status_map = {
            "LOW": "低 / 便宜",
            "NORMAL": "正常 / 公允",
            "HIGH": "高 / 昂貴",
            "EXTREME": "極高 / 泡沫",
        }
        iv_status = iv_status_map.get(iv_status_raw, "正常 / 公允")
        expected_move = iv_metrics.expected_move_weekly
        earnings_loading = getattr(iv_metrics, "has_earnings_event", False)
        macro_loading = getattr(iv_metrics, "has_macro_event", False)
        event_loading_applied = getattr(iv_metrics, "event_loading_applied", False)
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
        if getattr(metrics, "event_loading_applied", False):
            event_loading_applied = True

        if hasattr(metrics, "iv_source") and metrics.iv_source:
            iv_source = metrics.iv_source

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

    # 事件加載揭露：iv_metrics 在「即時 IV 缺失 + 14 天內有財報/總經事件」時，
    # 會把 fallback IV 乘上 1.4x 事件加載係數，放大值接著流入 IV Rank、
    # Expected Move 與所有 IVR 閘門。
    #
    # 過去這裡對財報顯示「快取波動率可能低估」、對總經顯示「已校正」，但程式對
    # 兩者其實都套用同一個 1.4x——「可能低估」等於在值已被放大之後還告訴使用者
    # 它偏低，方向剛好相反。現在改讀 iv_metrics 上的顯式 event_loading_applied
    # 旗標（而非從 iv_source 反推），據實說明數值是否經過加載。
    iv_status_str = f"狀態: {iv_status}"
    if event_loading_applied:
        event_label = "財報" if earnings_loading else "總經大事件"
        iv_status_str = (
            f"狀態: ⚠️ 臨近{event_label}／即時 IV 缺失，"
            "已套用 1.4x 事件加載係數 (非原始觀測值)"
        )
    elif earnings_loading:
        iv_status_str = "狀態: ⚠️ 臨近財報，波動率定價可能尚未反映事件風險"
    elif macro_loading:
        iv_status_str = "狀態: ⚠️ 臨近總經大事件，波動率定價可能尚未反映事件風險"

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
            mp_updated_at = max_pain_data.get("updated_at")
            mp_age_seconds = None
            if mp_updated_at:
                try:
                    mp_age_seconds = (
                        datetime.now(timezone.utc)
                        - datetime.strptime(mp_updated_at, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                    ).total_seconds()
                except Exception:
                    mp_age_seconds = None
            freshness_suffix = format_market_cache_freshness_suffix(
                is_stale=bool(max_pain_data.get("is_stale", False)),
                is_degraded=bool(max_pain_data.get("is_degraded", False)),
                calculation_mode=max_pain_data.get("calculation_mode", "OI"),
            )
            age_suffix = format_cache_age_suffix(mp_age_seconds)
            max_pain_str = f"${max_pain:.2f}{freshness_suffix}{age_suffix} (當前價差: {pain_dist:+.2f}%)"
    else:
        max_pain_str = "N/A (⚠️ 數據源缺失)"

    vol_pcr_val = metrics.volume_pcr if metrics else None
    oi_pcr_val = metrics.oi_pcr if metrics else None

    pcr_dict = pcr_data if isinstance(pcr_data, dict) else {}
    if pcr_dict:
        vol_pcr_val = pcr_dict.get("volume_pcr", vol_pcr_val)
        # 注意：pcr_dict["pcr"] 是 **volume** PCR（options_flow.calculate_pcr），
        # 不可拿來當 OI PCR 的後備值，否則會把成交量 PCR 標示成結構性 OI PCR。
        if "oi_pcr" in pcr_dict:
            oi_pcr_val = pcr_dict["oi_pcr"]

    if is_premarket or vol_pcr_val == 0.0 or vol_pcr_val is None:
        vol_pcr_status = "⚖️ 封盤中 (盤前未更新)"
        vol_pcr_str = "--"
    else:
        vol_pcr_str = f"{vol_pcr_val:.2f}"
        if pcr_dict.get("volume_pcr_state"):
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
        # 只有在上游未提供 oi_pcr_state（缺 key 或空字串）時才走本地數值分支。
        # 門檻刻意與 options_flow.calculate_pcr() 的 0.90 / 1.10 對齊——這裡原本
        # 用的是 1.20，兩套並存會讓同一個 OI PCR 依資料來源得到不同判讀。
        if pcr_dict.get("oi_pcr_state"):
            oi_pcr_status = pcr_dict["oi_pcr_state"]
        elif oi_pcr_val < 0.90:
            oi_pcr_status = "🏹 結構激進/看漲多頭沉澱"
        elif oi_pcr_val > 1.10:
            oi_pcr_status = "🛡️ 結構防禦/虛值 Put 沉澱"
        else:
            oi_pcr_status = "⚖️ 籌碼結構中性"

    gex_dist = (
        ((live_price - gex_putwall) / gex_putwall * 100.0)
        if gex_putwall and gex_putwall > 0
        else None
    )

    # Unusual Options Activity (UOA) table formatting
    uoa_table_lines = []
    if uoa_list:
        for item in uoa_list[:3]:
            exp = item.get("expiry", "")
            strike_val = float(item.get("strike", 0.0))
            opt_type = str(item.get("type", "")).upper()
            action = item.get("action", "")

            trade_type = str(item.get("trade_type", "SWEEP")).upper()
            oi_change = int(item.get("oi_change_net", 0))
            trade_tag = "🔥 SWEEP" if trade_type == "SWEEP" else "📦 BLOCK"

            action_display = f"{trade_tag} {action}"

            vol_val = int(item.get("volume", 0))
            oi_str = f"{vol_val:,}({oi_change:+})"

            ratio_str = item.get("ratio_str", "0.00x")
            intent = item.get("intent", "")

            uoa_table_lines.append(
                f" {exp:<10} | ${strike_val:<9.2f} | {opt_type:<4} | {action_display:<32} | {oi_str:<14} | {ratio_str:<6} | {intent}"
            )

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
            if not legs:
                # 0 口 / 空 legs 是「本輪不執行」的 WAIT 計畫（財報 event-lock、
                # 期權鏈流動性不足等）。這種情況下 rationale 才是使用者真正需要
                # 看到的內容——它說明了為什麼沒有可執行合約。
                reason = getattr(option_plan, "rationale", "")
                if reason:
                    inst_str += f"\n └─ {reason}"
            else:
                contracts = getattr(option_plan, "suggested_contracts", 0)
                risk = getattr(option_plan, "max_risk_amount", 0.0)
                inst_str += (
                    f"\n └─ 建議口數上限: {contracts} 口 (風險配額: ${risk:.2f})"
                )

    # 注意：skew_percentile (skew_per) is None 刻意不計入 is_degraded——分位數需要
    # 20 筆 sentiment_history 樣本才能算出，樣本不足時回傳 None 是設計上的
    # fail-safe 中性狀態，不代表本次抓取退化（Skew 欄位本身已用 --% 呈現，見下方
    # skew_per_str）。skew_val（真正的即時 Skew 數值計算失敗）與其他欄位仍照舊
    # 視為真降級。
    is_degraded = (
        is_premarket
        or iv_source == "UNAVAILABLE"
        or iv_val is None
        or iv_rank is None
        or gex_putwall is None
        or skew_val is None
        # 歷史快取降級（期權鏈抓取失敗、沿用上次成功值）也是降級模式的一種，
        # 過去 is_fallback 完全沒有人讀，title 因此不會帶降級後綴。
        or skew_is_fallback
    )

    gex_putwall_str = (
        f"${gex_putwall:.2f}" if gex_putwall is not None and gex_putwall > 0 else "N/A"
    )
    gex_dist_str = f"{gex_dist:+.2f}%" if gex_dist is not None else "--%"
    vol_poc_str = f"${vol_poc:.2f}" if vol_poc is not None else "N/A"
    skew_val_str = f"{skew_val:+.2f}%" if skew_val is not None else "--%"
    skew_per_str = f"{skew_per:.1f}%" if skew_per is not None else "--%"
    # 分位視窗是「最近 N 列樣本」而非固定時間視窗，把樣本數揭露出來，
    # 避免使用者把它讀成年度級的尾部風險百分位。
    skew_sample_str = f", 樣本 {skew_samples} 筆" if skew_samples is not None else ""
    iv_val_str = f"{iv_val:.1f}%" if iv_val is not None else "--%"
    iv_rank_str = f"{iv_rank:.1f}%" if iv_rank is not None else "--%"
    expected_move_str = (
        f"±${expected_move:.2f}"
        if expected_move is not None and expected_move > 0
        else "N/A"
    )

    color_val = {
        "red": discord.Color.red(),
        "yellow": discord.Color.orange(),
        "green": discord.Color.green(),
    }.get(alert_level, discord.Color.blurple())

    tag_str = f" 🏷️ {' | '.join(symbol_tags)}" if symbol_tags else ""
    embed_title = f"📊 標的分析中心 2.0: {symbol} 每半小時戰場心跳{tag_str}"
    if is_degraded:
        embed_title += " [數據未更新/降級模式]"

    description_lines = []

    if event_risk_summary:
        has_meaningful_content = True
        description_lines.append("**🗓️ 事件風控**")
        description_lines.append(f"```ansi\n{event_risk_summary}\n```")

    if skew_commentary:
        has_meaningful_content = True
        description_lines.append("**⚙️ 量化 Skew 解析**")
        # 表頭是 Skew 型態字串的唯一承載處：判讀本文不再重複附掛
        # 「（Skew 型態：⋯）」尾綴。數值與分位沿用下方 Market Footprints
        # 同一組已做過 None 降級的字串，避免兩處格式各自漂移。
        skew_header = f"Skew: {skew_val_str} (分位 {skew_per_str})"
        if skew_state:
            skew_header += f" ｜ {skew_state}"
        skew_body = f"{skew_header}\n{skew_commentary}"
        description_lines.append(f"```ansi\n{skew_body}\n```")

    embed_description = (
        "\n".join(description_lines).strip() if description_lines else None
    )

    embed = NexusEmbed(
        title=embed_title,
        description=embed_description,
        color=color_val,
        timestamp=datetime.now(timezone.utc),
    )

    if show_market_footprints:
        has_meaningful_content = True
        footprint_lines = [
            f" ├─ GEX PutWall (做市商底牆): {gex_putwall_str} (當前價差: {gex_dist_str})",
            f" ├─ Vol POC (籌碼控制中心): {vol_poc_str}",
            f" └─ Option Skew (期權偏斜): {skew_val_str} "
            f"(分位點: {skew_per_str}{skew_sample_str})",
        ]
        embed.add_field(
            name="🧱 物理籌碼牆與邊緣偵測 (Market Footprints)",
            value="```ansi\n" + "\n".join(footprint_lines) + "\n```",
            inline=False,
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
                effective_c_val = (
                    live_price
                    if live_price > 0.0
                    else float(symbol_gex.get("spot", 0.0))
                )
                if effective_c_val <= 0.0 and strike_keys:
                    effective_c_val = strike_keys[len(strike_keys) // 2]

                closest_idx = min(
                    range(len(strike_keys)),
                    key=lambda i: abs(strike_keys[i] - effective_c_val),
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
                gex_lines = [" ┌─ 履約價(Strike) ─ 曝險熱力圖 ─ [K]"]
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
                        "📍"
                        if abs(k - effective_c_val) < (effective_c_val * 0.01)
                        else "  "
                    )
                    formatted_val = f"{sign}{abs(v)/1000:.0f}K"
                    prefix = " ├─" if i < len(display_strikes) - 1 else " └─"
                    gex_lines.append(
                        f"{prefix} {spot_marker}{k:>7.2f} | {color_prefix}{bar_str}\u001b[0m | {formatted_val:>8}"
                    )
                embed.add_field(
                    name=f"🧲 Gamma 曝險分布 (GEX Profile Matrix){stale_suffix}",
                    value="```ansi\n" + "\n".join(gex_lines) + "\n```",
                    inline=False,
                )
        except Exception as e:
            embed.add_field(
                name="🧲 Gamma 曝險分布 (GEX Profile Matrix)",
                value=f"```ansi\n └─ [GEX 面板載入失敗: {e}]\n```",
                inline=False,
            )

    if show_iv_context:
        has_meaningful_content = True
        iv_lines = [
            f" ├─ Implied Volatility (IV): {iv_val_str} ｜ IV Rank: {iv_rank_str} ({iv_status_str})",
        ]

        if iv_term_status and iv_term_ratio is not None:
            if iv_term_status == "Backwardation":
                term_prefix = "⚠️ [Backwardation]"
            elif iv_term_status == "Contango":
                term_prefix = "🟩 [Contango]"
            else:
                term_prefix = "⚖️ [Normal]"
            iv_lines.append(
                f" ├─ IV Term Structure (期限結構): {term_prefix} (近遠月比: {iv_term_ratio:.2f})"
            )
        else:
            iv_lines.append(" ├─ IV Term Structure (期限結構): --")

        if earnings_loading or macro_loading:
            iv_lines.extend(
                [
                    f" ├─ 本週預期波幅 (Expected Move): {expected_move_str}",
                    " └─ 備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。",
                ]
            )
        else:
            iv_lines.append(f" └─ 本週預期波幅 (Expected Move): {expected_move_str}")

        embed.add_field(
            name="🧱 心跳：期權結構與波動率",
            value="```ansi\n" + "\n".join(iv_lines) + "\n```",
            inline=False,
        )

    if show_target_lock:
        has_meaningful_content = True
        target_lines = [
            f" ├─ 最大痛點結算 (Max Pain): {max_pain_str}",
            f" ├─ Volume PCR (即時情緒): {vol_pcr_str} (狀態: {vol_pcr_status})",
            f" └─ OI PCR (結構防禦): {oi_pcr_str} (狀態: {oi_pcr_status})",
        ]
        embed.add_field(
            name="🎯 結算與目標 (Target Lock)",
            value="```ansi\n" + "\n".join(target_lines) + "\n```",
            inline=False,
        )

    if show_target_lock:
        # 🎯 執行建議：把 calculate_dynamic_trading_signals() 實際算出來的買/賣價與
        # 股數渲染出來。過去這四個值有被計算、也有被傳進來，但 embed 只在
        # `metrics is None` 的降級分支用到價格、兩個 shares 參數完全沒被讀取，
        # AGENTS.md 文件化的「Dynamic Stock Pricing & Share Sizing」等於沒有出口。
        exec_lines = _build_execution_suggestion_lines(
            has_position=has_position,
            suitable_buy_price=suitable_buy_price,
            suitable_buy_shares=suitable_buy_shares,
            suitable_sell_price=suitable_sell_price,
            suitable_sell_shares=suitable_sell_shares,
            holding_quantity=holding_quantity,
        )
        if exec_lines:
            has_meaningful_content = True
            embed.add_field(
                name="🎯 執行建議 (Execution Suggestions)",
                value="```ansi\n" + "\n".join(exec_lines) + "\n```",
                inline=False,
            )

    if show_risk_alignment:
        has_meaningful_content = True
        risk_lines = [
            f" ├─ 既有現貨持倉: {shares_str} 股 ｜ 平均成本: {avg_cost_str} ｜ 當前損益: {pnl_str}",
        ]
        if not has_position and buy_rationale:
            risk_lines.append(f" ├─ 量化建倉解讀: {buy_rationale}")
        if has_position and sell_rationale:
            risk_lines.append(f" ├─ 量化止盈解讀: {sell_rationale}")

        risk_lines.extend(
            [
                f" └─ 操盤執行指南: {inst_str}",
            ]
        )
        embed.add_field(
            name="🛡️ 心跳：操盤指引與委託風控",
            value="```ansi\n" + "\n".join(risk_lines) + "\n```",
            inline=False,
        )

    if not has_meaningful_content and not (show_telemetry and telemetry_alignment_note):
        return None

    if show_telemetry and telemetry_alignment_note:
        val = _safe_embed_field_value(telemetry_alignment_note, "暫無對齊建議")

        embed.add_field(
            name="📡 Telemetry 待成交委託單實時對齊建議",
            value=val,
            inline=False,
        )

    if show_uoa and uoa_table_lines:
        uoa_content = (
            "```ansi\n"
            " 到期日     | 履約價      | 類型 | 標籤 & 交易流向 [買/賣]          | 成交量(ΔOI*)   | 比例   | 戰略意圖映射\n"
            " ------------------------------------------------------------------------------------------------------\n"
            + "\n".join(uoa_table_lines)
            + "\n\n ⚠️ SWEEP/BLOCK 為成交量整數手數啟發式代理判定，非真實 order-type 逐筆 tape 數據。"
            + "\n ⚠️ OI 為前一交易日收盤未平倉量，非盤中即時數據；比例欄位為當日累積量對此固定值的比值。"
            + "\n ⚠️ ΔOI* 於上游未提供實際未平倉變動時，以「當日成交量 − 前一日 OI」代理推估，非真實 ΔOI。"
            + "\n```"
        )
        embed.add_field(
            name="🎯 雷達：期權 Alpha 與 UOA 異常穿透",
            value=_safe_embed_field_value(uoa_content, "無異常大單"),
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

    embed = NexusEmbed(
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
        chunk_size = 10
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
