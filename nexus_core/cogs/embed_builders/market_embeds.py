"""市場數據與風險掃描 Embed 建構函式。

包含：
- create_max_pain_embed：最大痛點分析
- create_financial_runway_embed：財務生存跑道
- create_system_health_embed：系統健康診斷
- create_asset_promotion_embed：資產晉升成功
- create_transition_simulation_embed：戰略轉軌模擬
- create_market_calendar_embed：市場事件日曆
- create_iv_risk_scan_embed：高 IV 風險掃描
- build_radar_scan_embed：量化雷達批次掃描
- build_market_macro_overview_embed：宏觀風控情報中心
"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, List

from cogs.embed_builders._ansi_utils import _safe_float, _pad_string
from cogs.embed_builders.settings_embeds import create_info_embed
from cogs.embed_builders._core import NexusEmbed


def create_max_pain_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建立最大痛點分析 Embed。"""
    embed = discord.Embed(
        title=f"📍 {symbol} 最大痛點分析 (Max Pain)",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    cb_triggered = (
        data.get("circuit_breaker_triggered", False) or data.get("max_pain") is None
    )

    mp_val = data.get("max_pain")
    mp_display = f"${mp_val:.2f}" if mp_val is not None else "N/A"

    embed.add_field(name="到期日", value=f"`{data.get('expiry', 'N/A')}`", inline=True)
    embed.add_field(
        name="最大痛點 Strike",
        value=f"**{mp_display}**",
        inline=True,
    )

    spot_val = data.get("current_price")
    spot_display = f"${spot_val:.2f}" if spot_val is not None else "N/A"
    embed.add_field(
        name="目前價格",
        value=f"`{spot_display}`",
        inline=True,
    )

    if cb_triggered:
        embed.add_field(
            name="偏離度", value="N/A (偏離度過大，已觸發自動斷路)", inline=False
        )
        # Suppress converging and execution recommendation
    else:
        distance_pct = _safe_float(data.get("distance_pct"))
        distance_text = (
            f"現價高於痛點 `{distance_pct:.2f}%`"
            if distance_pct > 0
            else f"現價低於痛點 `{abs(distance_pct):.2f}%`"
            if distance_pct < 0
            else "現價貼近最大痛點 `0.00%`"
        )
        embed.add_field(name="偏離度", value=distance_text, inline=False)

        if data.get("is_converging"):
            embed.description = "🎯 **價格正向最大痛點收斂中** (預期結算日波動縮小)"

        expiry = data.get("expiry")
        if isinstance(expiry, str):
            try:
                expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
                dte = (expiry_dt - datetime.now()).days
                if dte <= 3:
                    embed.add_field(
                        name="🚀 執行建議",
                        value="⚠️ **DTE < 3 且接近最大痛點**\n建議提升 **獲利鎖定 (Profit Lock)** 優先級，規避結算震盪。",
                        inline=False,
                    )
            except ValueError:
                pass

    embed.set_footer(text="Nexus Seeker | Execution Automation")
    return embed


def create_system_health_embed(
    *,
    memory_percent: float,
    memory_available_mb: float,
    cpu_percent: float,
    process_memory_mb: float,
    disk_percent: float,
    disk_free_gb: float,
    sma_cache_size: int,
    ema_cache_size: int,
    poly_cache_size: int = 0,
    orderbook_size: int = 0,
) -> discord.Embed:
    """建立系統健康診斷 Embed。"""
    is_healthy = memory_percent < 80 and disk_percent < 85
    embed = discord.Embed(
        title="🖥️ Nexus Seeker 系統健康診斷",
        color=discord.Color.green() if is_healthy else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="VPS 記憶體",
        value=f"`{memory_percent}%` (可用: {memory_available_mb:.1f}MB)",
        inline=True,
    )
    embed.add_field(name="CPU 負載", value=f"`{cpu_percent}%`", inline=True)
    embed.add_field(
        name="程序占用 (RSS)", value=f"`{process_memory_mb:.1f} MB`", inline=True
    )
    embed.add_field(
        name="💿 硬碟空間",
        value=f"`{disk_percent}%` (可用: {disk_free_gb:.1f}GB)",
        inline=True,
    )
    cache_info = (
        f"• SMA/EMA Cache: `{sma_cache_size}/{ema_cache_size}`\n"
        f"• Poly Markets: `{poly_cache_size}`\n"
        f"• OrderBooks: `{orderbook_size}`"
    )
    embed.add_field(name="📦 快取統計 (LRU/Bounded)", value=cache_info, inline=False)

    health_status = "✅ 狀態優良"
    if memory_percent > 85 or disk_percent > 85:
        health_status = "⚠️ **資源吃緊**"
        if memory_percent > 85:
            health_status += " (記憶體閾值已達)"
        if disk_percent > 85:
            health_status += " (磁碟空間不足)"

    if memory_percent > 95 or disk_percent > 95:
        health_status = "🆘 **極度危險**"
        if memory_percent > 95:
            health_status += " (OOM 警告)"
        if disk_percent > 95:
            health_status += " (磁碟即將滿載)"

    embed.add_field(name="🩺 健康評級", value=health_status, inline=False)
    embed.set_footer(text="Argo Optimization Engine | Low-RAM VPS Edition")
    return embed


def create_asset_promotion_embed(
    symbol: str,
    expiry: str,
    strike: float,
    opt_type: str,
    quantity: int,
    price: float,
) -> discord.Embed:
    """建立 WATCH -> TRADE 晉升成功 Embed。"""
    embed = discord.Embed(
        title="🌌 Nexus | 資產晉升成功",
        description=f"標的 **{symbol}** 已從「觀察」提升為「實單交易」。",
        color=0x00FF7F,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="合約細節",
        value=f"`{expiry}` ${strike} {opt_type.upper()}\n數量: `{quantity}` 口 | 價格: `${price}`",
    )
    embed.set_footer(text="Unified Asset Lifecycle v1.0")
    return embed


def create_transition_simulation_embed(
    *,
    symbol: str,
    current_price: float,
    initial_pnl: float,
    additional_capital_required: float,
    adjusted_cost_basis: float,
    target_cc_strike: float,
    target_cc_premium: float,
    projected_aroc: float,
    capital_efficiency_gain: float,
) -> discord.Embed:
    """建立戰略轉軌模擬 Embed。"""
    embed = discord.Embed(
        title=f"🔄 戰略轉軌模擬 (演進) | {symbol}",
        description=f"模擬將 `{symbol}` 投機期權部位演進為 **核心現股 + 備兌買權 (Covered Call)** 模型。",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="現價 (Price)", value=f"`${current_price:.2f}`", inline=True)
    embed.add_field(
        name="期權獲利 (Option PnL)", value=f"`${initial_pnl:,.2f}`", inline=True
    )
    roadmap = (
        "1. **執行動作**：平倉現有 DITM 部位，回收收益。\n"
        f"2. **購入現股**：以 `${current_price:.2f}` 購入 100 股。\n"
        f"3. **追加資本**：需額外投入 **`${additional_capital_required:,.2f}`**。\n"
        f"4. **成本調整**：調整後每股成本為 **`${adjusted_cost_basis:.2f}`**。\n"
        f"5. **建立 CC**：賣出 `${target_cc_strike}` Call，收取 `${target_cc_premium:.2f}` 權利金。"
    )
    embed.add_field(name="🚀 資本重分配路線圖 (Roadmap)", value=roadmap, inline=False)
    efficiency = (
        f"• **預期年化回報 (AROC)**：`{projected_aroc:.1f}%` "
        f"{'✅ 符合 15% 門檻' if projected_aroc >= 15 else '⚠️ 低於效率門檻'}\n"
        f"• **單次收租殖利率**：`{capital_efficiency_gain:.2f}%`"
    )
    embed.add_field(name="📊 資本效率評估", value=efficiency, inline=False)
    embed.set_footer(text="戰略轉軌引擎 v1.0 | 專業營運模式")
    return embed


def create_market_calendar_embed(
    events: List[Any],
    *,
    max_items: int = 15,
    empty_message: str = "📭 未來 7 日內無重大事件。",
) -> discord.Embed:
    """建立市場事件與財報日曆 Embed。"""
    if not events:
        return create_info_embed("查無資料", empty_message)

    embed = discord.Embed(
        title="📅 【 重大市場事件 & 財報日曆 】",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    for event in events[:max_items]:
        event_name = getattr(event, "event", None)
        symbol = getattr(event, "symbol", None)
        tte_hours = getattr(event, "tte_hours", "N/A")
        event_date = getattr(event, "date", None)
        country = getattr(event, "country", None)
        impact = str(getattr(event, "impact", "")).lower()
        event_time = getattr(event, "time", None)

        if event_name is not None:
            impact_icon = "🔴" if impact == "high" else "🟡"
            field_name = (
                f"{impact_icon} {event_name} ({country})"
                if country
                else f"{impact_icon} {event_name}"
            )
            time_part = f" | `{event_time}`" if event_time else ""
            field_value = f"⏰ TTE: `{tte_hours}`h{time_part}"
        elif symbol is not None:
            field_name = f"📊 {symbol} 財報發布"
            date_part = f" | `{event_date}`" if event_date else ""
            field_value = f"⏰ TTE: `{tte_hours}`h{date_part}"
        else:
            continue

        embed.add_field(name=field_name, value=field_value, inline=False)

    embed.set_footer(text="Calendar-Aware Guard | Nexus Seeker")
    return embed


def create_iv_risk_scan_embed(results: List[Dict[str, Any]]) -> discord.Embed:
    """建立高 IV / IV Crush 風險掃描 Embed。"""
    if not results:
        return create_info_embed("系統資訊", "🔎 未發現 IV Rank > 80% 的高波動標的。")

    embed = discord.Embed(
        title="🔥 【 高波動 & IV Crush 風險掃描 】",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    for res in results[:15]:
        risk_icon = "🚨" if res.get("is_high_risk_vol") else "⚠️"
        embed.add_field(
            name=f"{risk_icon} {res.get('symbol', 'N/A')} (IVR: {res.get('iv_rank', 0)}%)",
            value=(
                f"TTE: `{_safe_float(res.get('tte_hours')):.1f}`h | "
                f"策略: {res.get('strategy', 'N/A')}"
            ),
            inline=False,
        )
    embed.set_footer(text="IV Rank Scanner | Nexus Seeker")
    return embed


def build_radar_scan_embed(
    scan_results: List[Dict[str, Any]],
    scan_type_name: str,
    user_id: int,
) -> List[discord.Embed]:
    """
    建構持倉/掛單/期權標的的批次掃描量化與情緒彙總 Embed 列表。
    為防止 Discord embed description 超過 4096 個字元限制，每頁最多顯示 15 個標的。
    """
    title_map = {
        "HOLDINGS": "現貨持倉批次量化雷達 (Holdings)",
        "ORDERS": "待成交掛單批次量化雷達 (Pending Orders)",
        "OPTIONS": "期權持倉批次量化雷達 (Option Holdings)",
        "WATCHLIST": "自選標的批次量化雷達 (Watchlist)",
        "ALL": "核心 AI 暨持倉批次量化雷達 (ALL)",
    }
    display_type = title_map.get(scan_type_name, "自選標的")

    # 先獲取該使用者的 active orders 以便在警示中聯動
    user_orders = []
    try:
        from database.orders import get_user_active_orders

        user_orders = get_user_active_orders(user_id)
    except Exception:
        pass

    # 將 scan_results 分組，每組最多 15 個標的
    chunk_size = 15
    chunks = [
        scan_results[i : i + chunk_size]
        for i in range(0, len(scan_results), chunk_size)
    ]
    if not chunks:
        chunks = [[]]

    embeds: List[discord.Embed] = []
    total_pages = len(chunks)

    for page_idx, chunk in enumerate(chunks):
        page_num = page_idx + 1
        page_title = f"🌌 交易員終端: {display_type}"
        if total_pages > 1:
            page_title += f" (第 {page_num}/{total_pages} 頁)"

        embed = NexusEmbed(
            title=page_title,
            color=0x3498DB,  # Default to 資訊藍
        )

        # 讀取全域快取指標 (TED Spread & GEX Flip & Darkpool DIX)
        macro_ansi_header = []
        try:
            from database.cache import get_kv_cache

            gex_flip = get_kv_cache("macro_spy_gamma_flip")
            ted_spread = get_kv_cache("macro_ted_spread")
            darkpool_dix = get_kv_cache("macro_darkpool_dix")

            if (
                gex_flip is not None
                or ted_spread is not None
                or darkpool_dix is not None
            ):
                gex_str = (
                    f"SPY 零 Gamma 線 (GEX Flip): \u001b[1;35m{gex_flip:.2f}\u001b[0m"
                    if gex_flip is not None
                    else ""
                )
                ted_alert = (
                    "\u001b[1;31m⚠️ 流動性警戒\u001b[0m"
                    if ted_spread is not None and float(ted_spread) > 0.5
                    else ""
                )
                ted_color = (
                    "\u001b[1;31m"
                    if ted_spread is not None and float(ted_spread) > 0.5
                    else "\u001b[1;36m"
                )
                ted_str = (
                    f"TED Spread (流動性指標): {ted_color}{ted_spread:.2f}\u001b[0m {ted_alert}"
                    if ted_spread is not None
                    else ""
                )

                dix_str = ""
                if darkpool_dix is not None:
                    dix_val = float(darkpool_dix)
                    dix_alert = (
                        " (\u001b[1;31m🔥 機構逢低建倉\u001b[0m)"
                        if dix_val > 45.0
                        else ""
                    )
                    dix_str = f"DIX (暗池吸籌指數): \u001b[1;36m{dix_val:.1f}%\u001b[0m{dix_alert}"

                macro_parts = [p for p in (gex_str, ted_str, dix_str) if p]
                if macro_parts:
                    macro_ansi_header.append(
                        " 🌍 總經與微觀結構警戒 (Macro & Liquidity Edge)"
                    )
                    for i, part in enumerate(macro_parts):
                        prefix = " ├─ " if i < len(macro_parts) - 1 else " └─ "
                        macro_ansi_header.append(prefix + part)
                    macro_ansi_header.append("")
        except Exception:
            pass

        # 寬度與格式對齊範例
        header = f"{'標的':<8}{'價格 (漲跌)':<16}{'IVR':<8}{'本週預期區間 (EM)':<22}{'Max Pain':<11}{'SQZ MOM':<14}{'與痛點價差 (D-MP)'}"
        divider = "-" * 94

        ansi_lines = []
        if macro_ansi_header:
            ansi_lines.extend(macro_ansi_header)
        insights = []

        for r in chunk:
            sym = r["symbol"]
            quote = r["quote"] or {}
            iv_metrics = r["iv_metrics"]
            mp_data = r["max_pain"] or {}

            # 1. 價格與漲跌幅
            price_raw = quote.get("c")
            price_val = float(price_raw) if price_raw is not None else 0.0
            dp_raw = quote.get("dp")
            dp_val = float(dp_raw) if dp_raw is not None else 0.0
            if dp_val >= 0:
                price_str = f"${price_val:.2f} (+{dp_val:.1f}%)"
                price_ansi = f"${price_val:.2f} (\u001b[1;32m+{dp_val:.1f}%\u001b[0m)"
            else:
                price_str = f"${price_val:.2f} ({dp_val:.1f}%)"
                price_ansi = f"${price_val:.2f} (\u001b[1;31m{dp_val:.1f}%\u001b[0m)"

            # 2. IV Rank
            iv_rank_val = 0.0
            em_weekly = 0.0
            if iv_metrics:
                if hasattr(iv_metrics, "iv_rank"):
                    iv_rank_val = (
                        float(iv_metrics.iv_rank)
                        if iv_metrics.iv_rank is not None
                        else 0.0
                    )
                    em_weekly = (
                        float(iv_metrics.expected_move_weekly)
                        if iv_metrics.expected_move_weekly is not None
                        else 0.0
                    )
                elif isinstance(iv_metrics, dict):
                    ivr_raw = iv_metrics.get("iv_rank")
                    iv_rank_val = float(ivr_raw) if ivr_raw is not None else 0.0
                    em_raw = iv_metrics.get("expected_move_weekly")
                    em_weekly = float(em_raw) if em_raw is not None else 0.0

            ivr_str = f"{iv_rank_val:.1f}%"

            # 3. 本週預期區間 (EM)
            is_fixed_income = sym.upper() in ["BOXX", "BIL", "SHV"]
            if is_fixed_income:
                em_low = em_high = price_val
                em_str = "N/A (避險資產)"
                em_ansi = "\u001b[1;30mN/A (避險資產)\u001b[0m"
            elif price_val > 0 and em_weekly > 0:
                em_low = float(iv_metrics.get("expected_move_lower") or 0.0)
                em_high = float(iv_metrics.get("expected_move_upper") or 0.0)
                if em_high <= em_low:
                    reference_price = float(
                        iv_metrics.get("reference_price") or price_val
                    )
                    em_low = reference_price - em_weekly
                    em_high = reference_price + em_weekly
                em_str = f"${em_low:.2f} ~ ${em_high:.2f}"
                em_ansi = f"\u001b[1;33m${em_low:.2f} ~ ${em_high:.2f}\u001b[0m"
            else:
                fallback_em = price_val * 0.05
                em_low = price_val - fallback_em
                em_high = price_val + fallback_em
                em_str = f"${em_low:.2f} ~ ${em_high:.2f}"
                em_ansi = f"${em_low:.2f} ~ ${em_high:.2f}"

            # 4. Max Pain 與與痛點價差
            max_pain_strike = 0.0
            dist_pct = 0.0
            cb_triggered = False
            calculation_mode = "OI"
            is_degraded = False

            if is_fixed_income:
                max_pain_strike = 0.0
                dist_pct = 0.0
                cb_triggered = False
            elif isinstance(mp_data, dict):
                mp_val = mp_data.get("max_pain")
                max_pain_strike = float(mp_val) if mp_val is not None else 0.0
                cb_triggered = mp_data.get("circuit_breaker_triggered", False)
                calculation_mode = mp_data.get("calculation_mode", "OI")
                is_degraded = mp_data.get("is_degraded", False)
                if max_pain_strike > 0 and price_val > 0:
                    dist_pct = (price_val - max_pain_strike) / max_pain_strike * 100

            # 判定狀態標籤 (透過 InsightsEngine 核心風控鐵律)
            status_label = ""
            if max_pain_strike > 0:
                if dist_pct >= 0:
                    dmp_str = f"[{dist_pct:+.2f}%]"
                    dmp_ansi = f"[\u001b[1;32m{dist_pct:+.2f}%\u001b[0m]"
                else:
                    dmp_str = f"[{dist_pct:+.2f}%]"
                    dmp_ansi = f"[\u001b[1;31m{dist_pct:+.2f}%\u001b[0m]"

                if dist_pct < -10.0:
                    status_label = "超跌磁吸 🚀"
                elif -10.0 <= dist_pct <= -5.0:
                    status_label = "超跌磁吸 🚀" if sym == "AMD" else "磁吸回升"
                elif -5.0 < dist_pct < 0.0:
                    status_label = "磁吸回升"
                elif 0.0 <= dist_pct <= 15.0:
                    status_label = "需防壓回 ⚠️"
                else:  # > 15.0
                    status_label = "籌碼斷層 ⚠️"
            else:
                dmp_str = "[0.00%]"
                dmp_ansi = "[0.00%]"
                status_label = "正常運行"

            # -- D-MP 動態阻斷機制與 InsightsEngine --
            from market_analysis.insights_engine import (
                InsightsEngine,
                RiskInsightsContext,
            )
            import database

            ctx_db = database.get_full_user_context(user_id)

            # 解析 PutWall
            put_wall = 0.0
            if "gex_metrics" in r and isinstance(r["gex_metrics"], dict):
                put_wall = float(r["gex_metrics"].get("put_wall", 0.0))
            elif "gex_profile_data" in r and isinstance(r["gex_profile_data"], dict):
                put_wall = float(r["gex_profile_data"].get("put_wall", 0.0))
            elif "put_wall" in r:
                put_wall = float(r["put_wall"])
            else:
                # 嘗試從 metrics 提取 (如果是 Watchlist pipeline 的輸出)
                mp_val = (
                    r.get("max_pain", {}).get("max_pain")
                    if isinstance(r.get("max_pain"), dict)
                    else None
                )
                put_wall = (
                    float(mp_val) * 0.9 if mp_val is not None else 0.0
                )  # Fallback 僅供安全
                if r.get("iv_metrics") and hasattr(r["iv_metrics"], "gex_max_put_wall"):
                    val = getattr(r["iv_metrics"], "gex_max_put_wall", 0.0)
                    if val is not None:
                        put_wall = float(val)

            # 解析 UOA
            uoa_list = r.get("uoa") or []
            uoa_calls_vol = sum(
                u.get("volume", 0) for u in uoa_list if u.get("type") == "CALL"
            )
            uoa_puts_vol = sum(
                u.get("volume", 0) for u in uoa_list if u.get("type") == "PUT"
            )
            uoa_institutional_short_call = (
                uoa_puts_vol > (uoa_calls_vol * 1.5) and uoa_puts_vol > 0
            )

            # 解析 Term Structure
            term_structure = 1.0
            if (
                iv_metrics
                and hasattr(iv_metrics, "term_structure_ratio")
                and getattr(iv_metrics, "term_structure_ratio")
            ):
                term_structure = float(getattr(iv_metrics, "term_structure_ratio"))
            elif isinstance(iv_metrics, dict) and iv_metrics.get(
                "term_structure_ratio"
            ):
                val = iv_metrics.get("term_structure_ratio")
                term_structure = float(val) if val is not None else 1.0

            # 判斷 has_positive_gamma_support:
            net_gex = 0.0
            if "gex_profile_data" in r and isinstance(r["gex_profile_data"], dict):
                net_gex = float(r.get("gex_profile_data", {}).get("net_gex", 0.0))
            elif "gex_metrics" in r and isinstance(r["gex_metrics"], dict):
                net_gex = float(r.get("gex_metrics", {}).get("net_gex", 0.0))
            has_positive_gamma_support = net_gex > 10_000_000

            sqz_mom_val = r.get("psq_result", {}).get("momentum", 0.0)

            risk_ctx = RiskInsightsContext(
                symbol=sym,
                current_price=price_val,
                put_wall=put_wall,
                net_gex_status="POSITIVE_GAMMA"
                if net_gex > 0
                else "NEGATIVE_GAMMA_ZONE",
                term_structure=term_structure,
                uoa_institutional_short_call=uoa_institutional_short_call,
                iv_rank=iv_rank_val / 100.0 if iv_rank_val > 1.0 else iv_rank_val,
                max_pain_deviation_pct=dist_pct / 100.0,
                can_trade_spreads=ctx_db.can_trade_spreads,
                cash_reserve_protection=ctx_db.cash_reserve_protection,
                expected_move_lower=em_low if em_weekly > 0 else None,
                expected_move_upper=em_high if em_weekly > 0 else None,
                sqz_mom=sqz_mom_val,
                has_positive_gamma_support=has_positive_gamma_support,
                cb_triggered=cb_triggered,
            )

            override_dmp, override_status, suggestion = (
                InsightsEngine.generate_cro_insight(risk_ctx)
            )

            if override_dmp:
                dmp_str = override_dmp
                dmp_ansi = (
                    "[\u001b[1;31m底牆破位\u001b[0m]"
                    if "底牆破位" in override_dmp
                    else override_dmp
                )
            if override_status:
                status_label = override_status
                if "🛑" in status_label:
                    embed.color = 0xE74C3C  # 高危警報紅色
                elif "⚖️" in status_label:
                    if embed.color.value != 0xE74C3C:
                        embed.color = 0x3498DB  # 資訊藍色

            # Local Rules: 聯動警示 insights
            if max_pain_strike > 0 and price_val > 0:
                if price_val <= em_low * 1.02 and dist_pct < -3.0:
                    matched_order = next(
                        (
                            o
                            for o in user_orders
                            if o["symbol"].upper() == sym
                            and o.get("side", "BUY") == "BUY"
                        ),
                        None,
                    )
                    if matched_order:
                        insights.append(
                            f"• 🚀 **{sym}**: 價格已極度逼近本週波動下緣 (${em_low:.2f})，且距離 Max Pain 有 {dist_pct:+.1f}% 的多頭磁吸引力，系統已自動激活 ID: {matched_order['id']} 坑底捕獸夾 (${matched_order['limit_price']:.2f})。"
                        )
                    else:
                        insights.append(
                            f"• 🚀 **{sym}**: 價格已極度逼近本週波動下緣 (${em_low:.2f})，且距離 Max Pain 有 {dist_pct:+.1f}% 的多頭磁吸引力，建議部署限價捕獵。"
                        )

                # 穿透式 UOA 與偏離度聯動判定：當偏離度顯著時 (例如 |dist_pct| > 10%)
                if abs(dist_pct) > 10.0:
                    from market_analysis.insight_generator import (
                        compute_realtime_insights,
                    )

                    data = {
                        "symbol": sym,
                        "spot": price_val,
                        "max_pain": max_pain_strike,
                        "put_wall": put_wall,
                        "gex_status": "POSITIVE_GAMMA"
                        if net_gex > 0
                        else "NEGATIVE_GAMMA_ZONE",
                        "uoa_calls_vol": uoa_calls_vol,
                        "uoa_puts_vol": uoa_puts_vol,
                        "skew_percentile": r.get("skew_percentile"),
                    }

                    if data["skew_percentile"] is None:
                        skew_val = r.get("skew", 0.0)
                        if skew_val > 0:
                            data["skew_percentile"] = 75.0
                        elif skew_val < 0:
                            data["skew_percentile"] = 25.0
                        else:
                            data["skew_percentile"] = 50.0

                    insight_str = compute_realtime_insights(data)
                    insights.append(insight_str)

            # 連動 SQZ MOM 擠壓蓄力 (Squeeze Momentum)
            psq_result = r.get("psq_result", {})
            sqz_dir = psq_result.get("direction", "⚪")
            sqz_is_squeezing = psq_result.get("is_squeezing", False)
            sqz_mom = psq_result.get("momentum", 0.0)

            # 連動 GEX PutWall (做市商底牆)
            if put_wall > 0 and price_val > 0:
                has_putwall_warning = any(
                    "PutWall" in msg for msg in insights if sym in msg
                )
                if not has_putwall_warning:
                    pw_dist = (price_val - put_wall) / put_wall * 100
                    if 0 <= pw_dist <= 2.0:
                        insights.append(
                            f"• 🛡️ **{sym}**: 價格已逼近 GEX PutWall 做市商底牆 (${put_wall:.2f})，此處具備強大流動性支撐，若有效跌破將觸發 Delta 負向螺旋。"
                        )
                    elif pw_dist < 0 and net_gex < 0 and sqz_dir == "🔴":
                        insights.append(
                            f"• 🚨 **{sym}**: 價格已跌破 GEX PutWall 做市商底牆 (${put_wall:.2f})，進入 Delta 負向螺旋高風險區間，嚴防流動性踩踏。"
                        )

            if sqz_is_squeezing:
                if sqz_mom_val > 0:
                    insights.append(
                        f"• ⏱️ **{sym}**: SQZ 正處於動能擠壓蓄力期 (Squeezing)，當前動能偏多 ({sqz_mom_val:+.1f})，建議關注向上突破機會。"
                    )
                elif sqz_mom_val < 0:
                    insights.append(
                        f"• ⏱️ **{sym}**: SQZ 正處於動能擠壓蓄力期 (Squeezing)，當前動能偏空 ({sqz_mom_val:+.1f})，建議嚴防向下殺跌風險。"
                    )

            # 格式化一列 ANSI 表格
            sym_cell = f"\u001b[1;34m{sym:<6}\u001b[0m"
            price_cell = price_ansi + (" " * max(0, 16 - len(price_str)))
            ivr_cell = ivr_str + (" " * max(0, 8 - len(ivr_str)))
            em_cell = em_ansi + (" " * max(0, 22 - len(em_str)))
            if cb_triggered:
                mp_str_val = "CB ⚠️"
            elif max_pain_strike > 0:
                if calculation_mode == "Volume" or is_degraded:
                    mp_str_val = f"${max_pain_strike:.2f}(V)"
                else:
                    mp_str_val = f"${max_pain_strike:.2f}"
            else:
                mp_str_val = "N/A"
            mp_cell = mp_str_val + (" " * max(0, 11 - len(mp_str_val)))

            dmp_padded_raw = _pad_string(dmp_str, 12)
            dmp_cell = dmp_padded_raw.replace(dmp_str, dmp_ansi)
            label_cell = status_label

            if sqz_mom > 0:
                sqz_text = f"{sqz_dir} 多頭"
            elif sqz_mom < 0:
                sqz_text = f"{sqz_dir} 空頭"
            else:
                sqz_text = f"{sqz_dir} 中性"

            if sqz_is_squeezing:
                mom_str = f"{sqz_mom:+.1f}"
                if sqz_mom > 0:
                    combined_ansi = f"\u001b[1;32m{sqz_text} {mom_str}\u001b[0m"
                elif sqz_mom < 0:
                    combined_ansi = f"\u001b[1;31m{sqz_text} {mom_str}\u001b[0m"
                else:
                    combined_ansi = f"{sqz_text} {mom_str}"
            else:
                mom_str = "---"
                combined_ansi = f"\u001b[1;30m{sqz_text} {mom_str}\u001b[0m"

            # Use _pad_string with visual length calculation. width is 14
            # (Header 'SQZ MOM       ' is 14 spaces wide)
            combined_raw = f"{sqz_text} {mom_str}"
            padded_raw = _pad_string(combined_raw, 14)
            # Replace the raw part with ANSI inside the padded string
            sqz_mom_cell = padded_raw.replace(combined_raw, combined_ansi)

            ansi_lines.append(
                f"{sym_cell}{price_cell}{ivr_cell}{em_cell}{mp_cell}{sqz_mom_cell}{dmp_cell}{label_cell}"
            )

        ansi_table = f"```ansi\n============================= 核心 AI 暨持倉量化雷達 =============================\n{header}\n{divider}\n"
        ansi_table += "\n".join(ansi_lines)
        ansi_table += "\n=================================================================================\n"
        ansi_table += "提示: ⚠️ 代表與最大痛點偏離度過高（>10%）或具備異常籌碼結構，需點擊穿透審查。\n"
        ansi_table += "備註: (V) 期權OI毀損降級Volume。CB 偏離現貨過高觸發斷路。\n"
        ansi_table += "指標: SQZ 🟢多頭動能/🔴空頭動能。MOM 顯示數值代表處於擠壓蓄力期，需防突破或殺跌。\n```"

        embed.description = ansi_table

        if insights:
            embed.add_field(
                name="💡 即時聯動警示 (Real-time Insights)",
                value="\n".join(insights[:5]),
                inline=False,
            )
        else:
            embed.add_field(
                name="💡 即時聯動警示 (Real-time Insights)",
                value="• ✨ 所有標的當前價格與 Max Pain 及波動邊界皆無極端異常偏離。",
                inline=False,
            )

        embeds.append(embed)

    return embeds


def build_market_macro_overview_embed(macro_data: dict) -> discord.Embed:
    """
    建立美股總體經濟與大盤風險防禦指標 (Macro & Risk Dashboard) Embed。
    採用繁體中文與 ANSI 雙色調 Panel 格式進行呈現。
    """
    # 1. 依據 RAM 水位判定是否觸發降級
    is_degraded = macro_data.get("is_degraded", False)

    # 決定顏色
    color = discord.Color.green()
    if macro_data.get("recession_warning", False) or macro_data.get(
        "short_gamma_critical", False
    ):
        color = discord.Color.red()
    elif is_degraded:
        color = discord.Color.orange()

    embed = discord.Embed(
        title="🌌 全局宏觀風控情報中心 (Macro Risk Control Hub)",
        description="本面板整合全套美股總量指標、薩姆衰退防衛線與系統級流動性壓力測試紅線，為高安全邊際期權賣方提供核心營運決策指引。\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 2. 格式化數值
    spx = macro_data.get("spx") or 0.0
    vix = macro_data.get("vix") or 0.0
    us10y = macro_data.get("us10y") or 0.0
    gamma_flip = macro_data.get("gamma_flip_line") or 0.0
    wti = macro_data.get("wti") or 0.0
    rrp = macro_data.get("rrp") or 0.0
    fed_balance = macro_data.get("fed_balance") or 0.0
    cpi_nfp_calendar = macro_data.get("cpi_nfp_calendar") or "暫無數據"
    fear_greed = macro_data.get("fear_greed") or 50.0
    uer = macro_data.get("uer") or 0.0
    sahm_rule = macro_data.get("sahm_rule") or 0.0
    payout_threshold = macro_data.get("payout_threshold") or 13000.0
    rrp_change_30d = macro_data.get("rrp_change_30d") or 0.0

    # 狀態標記
    gex_is_fallback = macro_data.get("gex_is_fallback", False)
    gex_suffix = " \u001b[1;33m[備援/快取]\u001b[0m" if gex_is_fallback else ""

    short_gamma_desc = (
        "🚨 CRITICAL (網格步長 1.5x 已生效)"
        if macro_data.get("short_gamma_critical", False)
        else "🟢 NORMAL (網格步長正常)"
    )
    if gex_is_fallback:
        short_gamma_desc += " [備援估算]"

    short_gamma_status = (
        f"\u001b[1;31m{short_gamma_desc}\u001b[0m"
        if macro_data.get("short_gamma_critical", False)
        else f"\u001b[1;32m{short_gamma_desc}\u001b[0m"
    )
    recession_status = (
        "\u001b[1;31m🚨 WARNING (CC 開倉阻斷已生效)\u001b[0m"
        if macro_data.get("recession_warning", False)
        else "\u001b[1;32m🟢 NORMAL (CC 開倉正常)\u001b[0m"
    )

    # 3. 建立 ANSI 面板內容
    core_lines = [
        " 📊 大盤與核心指標 (Market & Core Indices)",
        " ----------------------------------",
        f" ├─ S&P 500 Index (SPX): \u001b[1;32m{spx:,.2f}\u001b[0m",
        f" ├─ 恐慌指數 (VIX): \u001b[1;33m{vix:.2f}\u001b[0m",
        f" ├─ 10年期美債收益率 (US10Y): \u001b[1;36m{us10y:.2f}%\u001b[0m",
        f" └─ 零 Gamma 翻轉線 (GEX Flip): \u001b[1;35m{gamma_flip:,.2f}\u001b[0m{gex_suffix}",
    ]
    core_panel = "```ansi\n" + "\n".join(core_lines) + "\n```"

    risk_lines = [
        " 🛡️ 聯動風控引擎狀態 (Risk Engine Status)",
        " ----------------------------------",
        f" ├─ 零 Gamma 踩踏: {short_gamma_status}",
        f" ├─ 經濟衰退警告: {recession_status}",
        f" └─ 安全提領紅線: \u001b[1;31m${payout_threshold:,.0f}\u001b[0m",
    ]
    risk_panel = "```ansi\n" + "\n".join(risk_lines) + "\n```"

    macro_lines = [
        " 📈 流動性與總經指標 (Liquidity & Macro)",
        " ----------------------------------",
        f" ├─ WTI 原油價格: \u001b[1;33m${wti:.2f}\u001b[0m",
        f" ├─ 聯準會逆回購 (RRP): \u001b[1;36m${rrp:,.1f}B\u001b[0m (30天變動: \u001b[1;35m{rrp_change_30d:+.1f}%\u001b[0m)",
        f" ├─ 聯準會資產負債表: \u001b[1;32m${fed_balance:.2f}T\u001b[0m",
        f" ├─ CNN 恐懼與貪婪指數: \u001b[1;36m{fear_greed:.1f}\u001b[0m",
        f" └─ 美國失業率 (UER): \u001b[1;33m{uer:.1f}%\u001b[0m (薩姆規則值: \u001b[1;31m{sahm_rule:.2f}\u001b[0m)",
    ]
    macro_panel = "```ansi\n" + "\n".join(macro_lines) + "\n```"

    calendar_panel = f"```\n 📅 總經公布日程\n ----------------------------------\n └─ {cpi_nfp_calendar}\n```"

    # 4. 加入 Fields 到 Embed
    embed.add_field(name="🏁 核心大盤與收益指標", value=core_panel, inline=False)
    embed.add_field(name="🛡️ 聯動風控引擎狀態", value=risk_panel, inline=False)
    embed.add_field(name="📈 總經與系統流動性指標", value=macro_panel, inline=False)
    embed.add_field(name="🗓️ 總經事件公布日程", value=calendar_panel, inline=False)

    # 如果有降級警告，加入說明
    if is_degraded:
        embed.add_field(
            name="⚠️ 系統降級警告",
            value="**[警告] 偵測到系統記憶體負載 > 85%，已自動啟用 LRU 降級保護機制，簡化部分動態計算以確保系統穩定。**",
            inline=False,
        )

    embed.set_footer(text="Nexus Risk Engine | 總經大盤全局防禦系統")
    return embed


def build_calendar_embed(
    macro_events: List[Any],
    earnings_events: List[Any],
    fedwatch_prob: float | None,
) -> discord.Embed:
    """建立總經與財報事件日曆 Embed。"""
    rate_high = False
    rate_cut = False
    if fedwatch_prob is not None:
        if fedwatch_prob > 0.70:
            rate_high = True
        else:
            rate_cut = True

    now_utc = datetime.now(timezone.utc)
    month_str = now_utc.strftime("%Y年%m月")

    color = discord.Color(0xE74C3C) if rate_high else discord.Color(0x3498DB)

    is_fallback = getattr(macro_events, "is_fallback", False)
    title_suffix = " [總經數據暫時無法獲取，正使用本地歷史快取]" if is_fallback else ""
    embed = NexusEmbed(
        title=f"🗓️ 總經與財報事件日曆 ({month_str}){title_suffix}",
        color=color,
        timestamp=now_utc,
    )

    if rate_high:
        embed.add_field(
            name="⚠️ 總經防護聯動 (Macro Defense)",
            value="`[風險預警] 利率維持高位，逃頂窗口已動態前移 5 個交易日`",
            inline=False,
        )
    elif rate_cut:
        embed.add_field(
            name="🟢 總經防護聯動 (Macro Defense)",
            value="`[動態調整] 預期降息，逃頂窗口已後推 5 天，風險偏好增強`",
            inline=False,
        )

    # Format Macro Events
    macro_text = ""
    if macro_events:
        for ev in macro_events[:15]:
            name = getattr(ev, "event", "Unknown")
            try:
                time_dt = datetime.fromisoformat(
                    str(getattr(ev, "time", "")).replace("Z", "+00:00")
                )
                date_str = f"<t:{int(time_dt.timestamp())}:f>"
            except Exception:
                date_str = "`??-?? ??:??`"

            macro_text += f"* 🗓️ {date_str} {name}\n"

        macro_text += (
            "*(註：總經事件由邊緣爬蟲引擎自動從 TradingView 非同步抓取更新)*\n"
        )
    else:
        macro_text = "📭 [總經數據暫時無法獲取] 或當月無重大事件。\n*(註：總經事件由邊緣爬蟲引擎自動從 TradingView 非同步抓取更新)*\n"

    embed.add_field(
        name="💡 當月重要總經事件 (Macro Events) — 數據源: Edge Scraper",
        value=macro_text,
        inline=False,
    )

    # Format Earnings Events
    earn_text = ""
    if earnings_events:
        for ev in earnings_events[:15]:
            sym = getattr(ev, "symbol", "Unknown")
            tte = getattr(ev, "tte_hours", "N/A")
            date_val = getattr(ev, "date", "N/A")
            try:
                date_dt = datetime.strptime(str(date_val), "%Y-%m-%d")
                date_str = f"<t:{int(date_dt.timestamp())}:D>"
                relative_str = f"<t:{int(date_dt.timestamp())}:R>"
                earn_text += f"* 🗓️ {date_str} **{sym}** 財報發布 ({relative_str})\n"
            except Exception:
                date_str = f"`{date_val}`"
                earn_text += f"* 🗓️ {date_str} **{sym}** 財報發布 (TTE: {tte}h)\n"
    else:
        earn_text = "📭 您的自選標的近期無財報。"

    embed.add_field(
        name="📊 自選標的財報 (Earnings Calendar) — 數據源: Finnhub",
        value=earn_text,
        inline=False,
    )

    embed.set_footer(text="Calendar-Aware Guard | Nexus Seeker")
    return embed
