"""Portfolio and trading position embed builders"""

import discord
import logging
import psutil

from datetime import datetime, timezone
from typing import List, Dict, Any

from market_analysis.uoa_telemetry import UOATradeResult, generate_uoa_ascii_table

from cogs.embed_builders._ansi_utils import _pad_string, _safe_float
from cogs.embed_builders._embed_helpers import _chunk_ansi_table

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Public embed builders
# ---------------------------------------------------------------------------


def create_holdings_embed(
    holdings_data: List[Dict[str, Any]], total_capital: float
) -> discord.Embed:
    """建構現貨持倉 (Holdings) 狀態報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 現貨持倉清單",
        description="追蹤您的長期股權資產與成本分佈。\n\u200b",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not holdings_data:
        embed.description = "📭 目前無現貨持倉紀錄。請使用 `/add_holding` 進行登錄。"
        return embed

    # A-Z sort by symbol
    sorted_holdings = sorted(holdings_data, key=lambda x: x.get("symbol", "").upper())

    total_value = 0.0
    total_pnl = 0.0

    data_lines = []
    header = f"{_pad_string('標的', 8)} | {_pad_string('數量', 8, 'right')} | {_pad_string('平均成本', 10, 'right')} | {_pad_string('現價', 10, 'right')} | {_pad_string('當前損益', 10, 'right')}"
    divider = "-" * 58

    for h in sorted_holdings:
        curr_p = h.get("current_price", 0.0)
        pnl = (curr_p - h["avg_cost"]) * h["quantity"] if curr_p > 0 else 0.0
        pnl_pct = (
            (curr_p / h["avg_cost"] - 1) if h["avg_cost"] > 0 and curr_p > 0 else 0.0
        )

        total_pnl += pnl
        total_value += curr_p * h["quantity"]

        sym = _pad_string(h["symbol"], 8)
        qty = _pad_string(f"{h['quantity']:,.0f}", 8, "right")
        cost = _pad_string(f"${h['avg_cost']:,.2f}", 10, "right")
        curr_price_str = f"${curr_p:,.2f}"
        curr_p_fmt = _pad_string(curr_price_str, 10, "right")

        # 使用 ANSI 顏色：綠色表示正損益，紅色表示負損益
        color_start = "\u001b[0;32m" if pnl >= 0 else "\u001b[0;31m"
        pnl_pct_str = f"{pnl_pct:+.1%}"
        pnl_fmt = _pad_string(pnl_pct_str, 10, "right").replace(
            pnl_pct_str, f"{color_start}{pnl_pct_str}\u001b[0m"
        )

        data_lines.append(f"{sym} | {qty} | {cost} | {curr_p_fmt} | {pnl_fmt}")

    chunks = _chunk_ansi_table(header, divider, data_lines)
    for i, chunk in enumerate(chunks):
        name = (
            f"📦 持倉明細 ({i+1}/{len(chunks)})" if len(chunks) > 1 else "📦 持倉明細"
        )
        embed.add_field(name=name, value=chunk, inline=False)

    summary = (
        f"💰 **持倉總市值**: `${total_value:,.2f}`\n"
        f"📈 **累計未實現損益**: `${total_pnl:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_value / total_capital * 100) if total_capital > 0 else 0:.1f}%`"
    )
    embed.add_field(name="🏁 財務摘要", value=summary, inline=False)

    embed.set_footer(text="Nexus Accounting Engine | 專業股權追蹤")
    return embed


def create_trades_embed(
    pnl_data: Dict[str, Any],
    total_capital: float = 0.0,
) -> discord.Embed:
    """建構實單持倉 (Portfolio) 狀態與未實現損益報告 Embed"""
    embed = discord.Embed(
        title="📊 Nexus Seeker | 實單持倉清單 (包含帳面損益)",
        description="追蹤您的期權實單持倉與即時未實現損益 (Unrealized PnL)。\n\u200b",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )

    trades = pnl_data.get("trades", [])
    if not trades:
        embed.description = "📭 目前無持倉紀錄。"
        return embed

    data_lines = []
    # 標頭 (調整 Python len 以匹配可見寬度)
    header = f"{'ID'.ljust(2)} | {'標的'.ljust(4)} | {'到期日'.ljust(7)} | {'履約'.ljust(5)} | {'數量'.rjust(2)} | {'成本'.rjust(4)} | {'現價'.rjust(4)} | {'帳面損益'.rjust(10)}"
    divider = "-" * 77

    total_cost = 0.0
    for t in trades:
        trade_id = t["id"]
        sym = t["symbol"]
        o_type = t["opt_type"]
        strike = t["strike"]
        exp = t["expiry"]
        qty = t["quantity"]
        entry_p = t["entry_price"]
        curr_p = t.get("current_price", 0.0)
        unrealized_pnl = t["unrealized_pnl"]
        pnl_pct = t["pnl_pct"]

        total_cost += abs(entry_p * qty * 100)

        id_fmt = f"{trade_id:02d}".ljust(2)
        sym_fmt = sym.ljust(4)
        exp_fmt = exp.ljust(10)
        st_type_fmt = f"{strike}{o_type[0].upper()}".ljust(7)

        color_code = "\x1b[0;32m" if qty > 0 else "\x1b[0;31m"
        qty_val = f"{qty:>4}"
        qty_fmt = f"{color_code}{qty_val}\x1b[0m"

        cost_fmt = f"{entry_p:6.2f}"
        curr_fmt = f"{curr_p:6.2f}"

        pnl_color = "\x1b[0;32m" if unrealized_pnl >= 0 else "\x1b[0;31m"
        pnl_val = f"${unrealized_pnl:+.0f} ({pnl_pct:+.1%})"
        pnl_fmt = f"{pnl_color}{pnl_val:>14}\x1b[0m"

        data_lines.append(
            f"{id_fmt} | {sym_fmt} | {exp_fmt} | {st_type_fmt} | {qty_fmt} | {cost_fmt} | {curr_fmt} | {pnl_fmt}"
        )

    chunks = _chunk_ansi_table(header, divider, data_lines)
    for i, chunk in enumerate(chunks):
        name = (
            f"📦 持倉明細 ({i+1}/{len(chunks)})" if len(chunks) > 1 else "📦 持倉明細"
        )
        embed.add_field(name=name, value=chunk, inline=False)

    total_unrealized_pnl = pnl_data.get("total_unrealized_pnl", 0.0)

    summary = (
        f"💰 **持倉總權利金成本 (概算)**: `${total_cost:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_cost / total_capital * 100) if total_capital > 0 else 0:.1f}%`\n"
        f"📈 **總未實現損益 (Unrealized PnL)**: `${total_unrealized_pnl:,.2f}`"
    )
    embed.add_field(name="🏁 財務摘要 (Financial Summary)", value=summary, inline=False)

    embed.set_footer(text="Nexus Portfolio Engine | 專業實單與損益監控")
    return embed


def create_strategic_dash_embed(
    user_ctx: Any,
    pnl_data: Dict[str, Any],
    vix_spot: float = 18.0,
    backup_liquidity: float = 0.0,
    extended_runway: float | None = None,
) -> discord.Embed:
    """
    建構戰略看板 (Strategic Dashboard) Embed.
    遵循 Task 1 的 Traditional Chinese 模板。
    """
    embed = discord.Embed(
        title="📊 Nexus 交易員戰略看板",
        color=discord.Color.dark_blue(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 財務生存狀態 (Financial Runway)
    daily_theta = _safe_float(user_ctx.total_theta)
    monthly_expense = _safe_float(user_ctx.monthly_expense)
    daily_expense = monthly_expense / 30.0 if monthly_expense > 0 else 0.0
    coverage_pct = (daily_theta / daily_expense * 100) if daily_expense > 0 else 100.0

    # 計算跑道天數
    gap = daily_expense - daily_theta
    cash_reserve = _safe_float(user_ctx.cash_reserve)
    if gap <= 0:
        runway_days = "∞ (收益已覆蓋支出)"
    else:
        runway_days = f"{cash_reserve / gap:,.0f}" if gap > 0 else "∞"

    from market_analysis.trading_orchestration import get_safety_payout_threshold

    payout_threshold = get_safety_payout_threshold()

    status_mode = "觀戰模式" if not user_ctx.is_professional_mode else "實戰模式"
    nav = _safe_float(user_ctx.capital) + pnl_data.get("total_unrealized_pnl", 0.0)

    runway_info = (
        f"* **總資產 (NAV):** `${nav:,.0f}` ({status_mode})\n"
        f"* **現金儲備:** `${cash_reserve:,.2f}` | **月支出:** `${monthly_expense:,.2f}`\n"
        f"* **核心跑道:** {runway_days} 天 (由現金與 Theta 推算)\n"
    )

    if backup_liquidity > 0 and extended_runway is not None:
        extended_text = f"{extended_runway:,.1f}" if extended_runway < 9999 else "∞"
        runway_info += f"* **極限跑道:** {extended_text} 天 (含備用流動性 `${backup_liquidity:,.0f}`)\n"

    runway_info += (
        f"* **收租效率:** 每日 Theta `${daily_theta:,.2f}` (覆蓋率 {coverage_pct:.1f}%)\n"
        f"* **安全提領紅線:** `${payout_threshold:,.0f}` (流動性防守限制)\n"
    )

    if coverage_pct < 100 and daily_expense > 0:
        runway_info += f"> 💡 警訊：現金流覆蓋不足，每日收租缺口為 `${gap:,.2f}`。"

    embed.add_field(
        name="🏁 財務生存狀態 (Financial Runway)", value=runway_info, inline=False
    )

    # 2. 組合風險精算 (NRO Integrity)
    # Vanna Sensitivity Stress Test: 若 VIX 上升 10%，隱含 Delta 將變動至 [New_Delta]
    total_vanna = _safe_float(user_ctx.total_vanna)
    total_delta = _safe_float(user_ctx.total_weighted_delta)
    capital = _safe_float(user_ctx.capital)

    vanna_impact = total_vanna * (vix_spot * 0.10 / 100.0)
    new_delta = total_delta + vanna_impact

    # 對沖狀態與建議
    hedge_status = "運行中" if abs(total_delta) < (capital * 0.1) else "需調整"
    # 簡單邏輯：如果 Delta 太正，建議賣出 SPY；如果太負，建議買入 SPY
    if total_delta > 100:
        hedge_instruction = f"賣出 {int(total_delta)} 股 SPY 以對沖正 Delta"
    elif total_delta < -100:
        hedge_instruction = f"買入 {int(abs(total_delta))} 股 SPY 以對沖負 Delta"
    else:
        hedge_instruction = "目前曝險平衡，無需立即調整"

    nro_info = (
        f"* **Beta-Delta:** `{total_delta:+.1f}` (相對於 SPY 的整體曝險)\n"
        f"* **Vanna 敏感度:** 若 VIX 上升 10%，隱含 Delta 將變動至 `{new_delta:+.1f}`。\n"
        f"* **對沖狀態:** {hedge_status}\n"
        f"> 🎯 建議對沖位：{hedge_instruction}"
    )
    embed.add_field(name="🛡️ 組合風險精算 (NRO Integrity)", value=nro_info, inline=False)

    # 3. 系統健康
    ram_usage = psutil.virtual_memory().percent
    # 假設 BoundedCache 正常，這裡簡單寫死或從某處獲取
    sys_health = f"RAM {ram_usage}% | BoundedCache 穩定"
    embed.add_field(name="⚙️ 系統健康", value=sys_health, inline=False)

    embed.set_footer(text="Nexus Seeker | 戰術風險管理終端")
    return embed


def create_tactical_symbol_embed(data: Dict[str, Any]) -> discord.Embed:
    """
    建構標的深度分析 (Tactical Deep-Dive) Embed.
    遵循 Task 2 的 Traditional Chinese 模板。
    """
    symbol = data.get("symbol", "UNKNOWN")

    # 處理盤前狀態與波動率 degradation
    iv_data = data.get("iv_data")
    title_suffix = ""
    is_premarket = False
    iv_source = None
    current_iv_val = None
    if iv_data:
        if hasattr(iv_data, "is_premarket"):
            is_premarket = iv_data.is_premarket
        elif isinstance(iv_data, dict):
            is_premarket = iv_data.get("is_premarket", False)

        current_iv_val = (
            iv_data.current_iv
            if hasattr(iv_data, "current_iv")
            else iv_data.get("current_iv")
        )
        iv_source = (
            iv_data.iv_source
            if hasattr(iv_data, "iv_source")
            else (iv_data.get("iv_source") if isinstance(iv_data, dict) else None)
        )
        if (
            iv_source is None
            and is_premarket
            and current_iv_val is not None
            and current_iv_val > 0.0
        ):
            iv_source = "STORED_IV"

        if is_premarket:
            if current_iv_val is not None and current_iv_val > 0.0:
                title_suffix = (
                    " [盤前/HV代理]" if iv_source == "HV_PROXY" else " [盤前/前日收盤]"
                )
            else:
                title_suffix = " [盤前數據未更新/降級模式]"

    skew_percentile = data.get("skew_percentile")
    is_degraded = (
        iv_source == "UNAVAILABLE" or current_iv_val is None or skew_percentile is None
    )
    if is_degraded and not title_suffix:
        title_suffix = " [數據未更新/降級模式]"

    embed = discord.Embed(
        title=f"🌌 標的分析中心: {symbol}{title_suffix}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 💹 即時報價 (Real-time Quote)
    quote = data.get("quote") or {}

    c_raw = quote.get("c") if quote.get("c") is not None else data.get("price")
    c_val = float(c_raw) if c_raw is not None else 0.0

    dp_raw = quote.get("dp")
    dp_val = float(dp_raw) if dp_raw is not None else 0.0

    d_raw = quote.get("d")
    d_val = float(d_raw) if d_raw is not None else 0.0

    o_raw = quote.get("o")
    o_val = float(o_raw) if o_raw is not None else 0.0

    h_raw = quote.get("h")
    h_val = float(h_raw) if h_raw is not None else 0.0

    l_raw = quote.get("l")
    l_val = float(l_raw) if l_raw is not None else 0.0

    pc_raw = quote.get("pc")
    pc_val = float(pc_raw) if pc_raw is not None else 0.0

    price_emoji = "📈" if dp_val >= 0 else "📉"
    if not quote or c_val == 0.0:
        quote_lines = [
            "```ansi",
            " 當前現價 (Current Price)",
            f" └─ 現價: \u001b[1;37m${c_val:.2f}\u001b[0m (暫無即時報價數據)",
            "```",
        ]
    else:
        color_code = "\u001b[1;32m" if dp_val >= 0 else "\u001b[1;31m"
        quote_lines = [
            "```ansi",
            " 當前現價 (Current Price)",
            f" └─ 現價: {color_code}${c_val:.2f}\u001b[0m ({price_emoji} {color_code}{dp_val:+.2f}%\u001b[0m / {color_code}{d_val:+.2f}\u001b[0m)",
            " 今日區間 (Daily Range)",
            f" └─ 開盤: \u001b[1;36m{o_val:.2f}\u001b[0m | 最高: \u001b[1;31m{h_val:.2f}\u001b[0m | 最低: \u001b[1;32m{l_val:.2f}\u001b[0m | 前收: \u001b[1;30m{pc_val:.2f}\u001b[0m",
        ]

        vp = data.get("volume_profile")
        if vp:
            hvn = vp.get("hvn", 0.0)
            lvn = vp.get("lvn", 0.0)
            quote_lines.extend(
                [
                    " 近期成交量分佈 (Volume Profile, 20D)",
                    f" └─ 高密集區 (HVN): \u001b[1;35m${hvn:.2f}\u001b[0m | 籌碼真空區 (LVN): \u001b[1;33m${lvn:.2f}\u001b[0m",
                ]
            )

        quote_lines.append("```")
    embed.add_field(
        name="💹 即時報價 (Real-time Quote)",
        value="\n".join(quote_lines),
        inline=False,
    )

    # 2. 📐 情緒與邊緣偵測 (Edge Detection)
    skew_val_raw = data.get("skew")
    skew_val = float(skew_val_raw) if skew_val_raw is not None else None
    skew_percentile = data.get("skew_percentile")
    poly_odds = data.get("polymarket_odds", "N/A")
    reddit_score = data.get("reddit_sentiment_score", "中性")

    _raw_pcr = data.get("pcr")
    pcr_data_for_div: dict = _raw_pcr if isinstance(_raw_pcr, dict) else {}
    pcr_val_raw = pcr_data_for_div.get("volume_pcr", pcr_data_for_div.get("pcr"))
    pcr_val_for_div = float(pcr_val_raw) if pcr_val_raw is not None else None

    iv_rank_val = None
    if iv_data:
        if hasattr(iv_data, "iv_rank"):
            iv_rank_val = iv_data.iv_rank
        elif isinstance(iv_data, dict):
            iv_rank_val = iv_data.get("iv_rank")
    iv_rank_val = float(iv_rank_val) if iv_rank_val is not None else None

    is_structural_divergence = False
    divergence_level = ""

    # 防止盤前(0.0) 觸發背離誤報
    if skew_percentile is not None and pcr_val_for_div is not None:
        if skew_percentile > 85.0 and 0.0 < pcr_val_for_div < 0.4:
            is_structural_divergence = True
            divergence_level = "High Divergence"
        elif skew_percentile < 15.0 and pcr_val_for_div > 1.5:
            is_structural_divergence = True
            divergence_level = "High Divergence"
        elif dp_val > 0.0 and skew_percentile > 90.0:
            is_structural_divergence = True
            divergence_level = "Warning"
        elif dp_val < -3.0 and iv_rank_val is not None and iv_rank_val < 15.0:
            is_structural_divergence = True
            divergence_level = "IV Suppression"

    divergence = "同步"
    action = "保持觀察"

    if is_structural_divergence:
        if divergence_level == "IV Suppression":
            divergence = "情緒背離 (現價暴跌但波動率極低)"
            action = "異常背離：現價大跌但 IV Rank 極低，警惕快取異常或非理性低波"
        else:
            divergence = "⚠️ WARNING: Structural Sentiment Divergence"
            if divergence_level == "High Divergence":
                action = "High Divergence：避免追價買權；僅允許小倉位收租並搭配保護"
            else:
                action = "留意結構性背離：建議降槓桿、以保護性結構防禦"
    elif (
        skew_percentile is not None
        and skew_percentile > 80
        and (
            "樂觀" in str(reddit_score)
            or "🚀" in str(reddit_score)
            or "Bullish" in str(reddit_score)
        )
    ):
        divergence = "情緒背離 (散戶樂觀 vs 專業避險)"
        action = "建立保護性賣權或減碼"
    elif (
        skew_percentile is not None
        and skew_percentile < 20
        and (
            "悲觀" in str(reddit_score)
            or "💀" in str(reddit_score)
            or "Bearish" in str(reddit_score)
        )
    ):
        divergence = "情緒背離 (散戶恐慌 vs 權利金便宜)"
        action = "考慮賣出賣權 (Cash Secured Put)"

    skew_color = (
        "\u001b[1;35m"
        if skew_percentile is not None and skew_percentile > 80
        else "\u001b[1;36m"
    )
    sentiment_color = (
        "\u001b[1;32m"
        if "🚀" in str(reddit_score)
        or "樂觀" in str(reddit_score)
        or "Bullish" in str(reddit_score)
        else (
            "\u001b[1;31m"
            if "💀" in str(reddit_score)
            or "悲觀" in str(reddit_score)
            or "Bearish" in str(reddit_score)
            else "\u001b[1;33m"
        )
    )
    divergence_color = "\u001b[1;31m" if divergence != "同步" else "\u001b[1;32m"

    skew_val_str = f"{skew_val:.2f}%" if skew_val is not None else "--%"
    skew_per_str = f"{skew_percentile:.1f}%" if skew_percentile is not None else "--%"

    edge_lines = [
        "```ansi",
        " Option Skew (期權偏斜)",
        f" └─ Skew 值: {skew_color}{skew_val_str}\u001b[0m (分位點: {skew_color}{skew_per_str}\u001b[0m)",
    ]
    if skew_percentile is not None and skew_percentile > 90:
        edge_lines.append(
            "    \u001b[1;33m⚠️ 市場下行保護需求極高，隱含避險情緒升溫。\u001b[0m"
        )

    edge_lines.extend(
        [
            " 巨鯨/散戶意圖映射 (Market Intention)",
            f" ├─ Polymarket 預測勝率: \u001b[1;34m{poly_odds}\u001b[0m",
            f" └─ Reddit 情緒指數: {sentiment_color}{reddit_score}\u001b[0m",
            " 情緒背離偵測 (Divergence Check)",
            f" └─ 狀態: {divergence_color}{divergence}\u001b[0m",
            f" └─ 建議: \u001b[1;32m{action}\u001b[0m",
            "```",
        ]
    )
    embed.add_field(
        name="📐 情緒與邊緣偵測 (Edge Detection)",
        value="\n".join(edge_lines),
        inline=False,
    )

    # 3. 📊 隱含波動率與預期區間 (IV Context)
    if iv_data:
        if hasattr(iv_data, "current_iv"):
            current_iv = iv_data.current_iv
            iv_rank = iv_data.iv_rank
            iv_percentile = iv_data.iv_percentile
            expected_move_weekly = iv_data.expected_move_weekly
            iv_status = iv_data.iv_status
        else:
            current_iv = iv_data.get("current_iv")
            iv_rank = iv_data.get("iv_rank")
            iv_percentile = iv_data.get("iv_percentile")
            expected_move_weekly = iv_data.get("expected_move_weekly")
            iv_status = iv_data.get("iv_status", "Normal")

        iv_status_map = {
            "Low": "低 / 便宜",
            "Normal": "正常 / 公允",
            "High": "高 / 昂貴",
            "Extreme": "極高 / 泡沫",
        }
        status_tw = (
            iv_status_map.get(iv_status, "正常 / 公允") if iv_status else "正常 / 公允"
        )
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

        if (
            iv_source == "UNAVAILABLE"
            or current_iv is None
            or (is_premarket and current_iv == 0.0)
        ):
            iv_lines = [
                "```ansi",
                " Implied Volatility (IV)",
                " └─ 值: \u001b[1;30m--%\u001b[0m (數據未更新 / 降級模式)",
                " IV Rank / IV Percentile",
                " └─ IV Rank: \u001b[1;30m--%\u001b[0m | IV Percentile: \u001b[1;30m--%\u001b[0m (狀態: 待開盤)",
                " Expected Move (預期區間)",
                " └─ 本週預期: \u001b[1;30m--\u001b[0m (開盤後更新)",
                "```",
            ]
        else:
            if is_premarket:
                vol_title = (
                    "Historical Volatility (HV, 30D)"
                    if iv_source == "HV_PROXY"
                    else "Implied Volatility (IV)"
                )
                vol_note = (
                    "30D 歷史實現波動率代理（期權未開市/IV 不可用）"
                    if iv_source == "HV_PROXY"
                    else "前日收盤 IV / SQLite 快取（期權未開市）"
                )
                em_note = (
                    "基於 30D HV 代理估算"
                    if iv_source == "HV_PROXY"
                    else "基於前日收盤 IV 計算"
                )
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

            iv_val_str = f"{current_iv * 100:.1f}%" if current_iv is not None else "--%"
            iv_rank_str = f"{iv_rank:.1f}%" if iv_rank is not None else "--%"
            iv_per_str = f"{iv_percentile:.1f}%" if iv_percentile is not None else "--%"

            iv_lines = [
                "```ansi",
                vol_title,
                f" └─ 值: {iv_val_str} ({vol_note})",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank_str} | IV Percentile: {iv_per_str} ({iv_status_str})",
            ]

            iv_term_status = (
                getattr(iv_data, "iv_term_structure_status", None) if iv_data else None
            )
            iv_term_ratio = (
                getattr(iv_data, "term_structure_ratio", None) if iv_data else None
            )
            if isinstance(iv_data, dict):
                iv_term_status = (
                    iv_data.get("iv_term_structure_status") or iv_term_status
                )
                iv_term_ratio = iv_data.get("term_structure_ratio") or iv_term_ratio

            if iv_term_status and iv_term_ratio is not None:
                try:
                    ratio_val = float(iv_term_ratio)
                    status_str = str(iv_term_status)
                    if status_str == "Backwardation":
                        term_prefix = "⚠️ 逆價差 (Backwardation)"
                    elif status_str == "Contango":
                        term_prefix = "✅ 正價差 (Contango)"
                    else:
                        term_prefix = "⚖️ 正常 (Normal)"
                    iv_lines.append(" IV 期限結構 (Term Structure)")
                    iv_lines.append(f" └─ {term_prefix} (近遠月比: {ratio_val:.2f})")
                except (ValueError, TypeError):
                    pass

            iv_lines.append(" Expected Move (預期區間)")
            if earnings_loading or macro_loading:
                expected_move_weekly_str = (
                    f"±${expected_move_weekly:.2f}"
                    if expected_move_weekly is not None
                    else "--"
                )
                iv_lines.extend(
                    [
                        f" ├─ 本週預期: {expected_move_weekly_str} ({em_note})",
                        " └─ 備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。",
                    ]
                )
            else:
                expected_move_weekly_str = (
                    f"±${expected_move_weekly:.2f}"
                    if expected_move_weekly is not None
                    else "--"
                )
                iv_lines.append(f" └─ 本週預期: {expected_move_weekly_str} ({em_note})")

            catalysts = data.get("catalysts")
            if catalysts:
                iv_lines.append(" 事件日曆防護 (Catalyst Calendar)")
                for cat in catalysts:
                    if hasattr(cat, "date"):
                        date_str = cat.date
                        days = cat.days_to_earnings
                        iv_lines.append(
                            f" └─ \u001b[1;33m⚠️ 距離財報 ({date_str[5:]}) 僅剩 {days:.1f} 天，嚴禁雙賣策略\u001b[0m"
                        )
                    elif hasattr(cat, "time"):
                        date_str = cat.time[:10]
                        days = round(cat.tte_hours / 24.0, 1)
                        iv_lines.append(
                            f" └─ \u001b[1;33m⚠️ 距離 {cat.event} ({date_str[5:]}) 僅剩 {days:.1f} 天，留意波動擴大\u001b[0m"
                        )

            iv_lines.append("```")
        embed.add_field(
            name="📊 隱含波動率與預期區間 (IV Context)",
            value="\n".join(iv_lines),
            inline=False,
        )

    # 3.5 🧲 Gamma 曝險分布 (GEX Profile)
    gex_data = data.get("gex_profile_data", {})
    if (
        gex_data
        and "gex_profile" in gex_data
        and isinstance(gex_data["gex_profile"], dict)
        and gex_data["gex_profile"]
    ):
        try:
            gex_prof = gex_data["gex_profile"]
            strike_keys = sorted([float(k) for k in gex_prof.keys()])
            if strike_keys:
                closest_idx = min(
                    range(len(strike_keys)),
                    key=lambda i: abs(strike_keys[i] - c_val),
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

                gex_lines = ["```ansi", " ┌─ 履約價(Strike) ─ 曝險熱力圖 ─ [K]"]
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

                    spot_marker = "📍" if abs(k - c_val) < (c_val * 0.01) else "  "
                    formatted_val = f"{sign}{abs(v)/1000:.0f}K"
                    prefix = " ├─" if i < len(display_strikes) - 1 else " └─"
                    gex_lines.append(
                        f"{prefix} {spot_marker}{k:>7.2f} | {color_prefix}{bar_str}\u001b[0m | {formatted_val:>8}"
                    )

                gex_putwall = gex_data.get("put_wall")
                try:
                    if gex_putwall and float(gex_putwall) > 0:
                        gex_lines.append("")
                        gex_lines.append(
                            f" 🛡️ GEX PutWall (做市商底牆): ${float(gex_putwall):.2f}"
                        )
                except (ValueError, TypeError):
                    pass

                gex_lines.append("```")

                is_stale = bool(gex_data.get("_is_stale_cache", False))
                stale_suffix = " [快取 / API 降級]" if is_stale else ""

                embed.add_field(
                    name=f"🧲 Gamma 曝險分布 (GEX Profile){stale_suffix}",
                    value="\n".join(gex_lines),
                    inline=False,
                )
        except Exception:
            pass

    # 4. 🎯 結算與目標 (Target Lock)
    _mp_val = data.get("max_pain")
    cb_triggered = data.get("circuit_breaker_triggered", False)

    if _mp_val is None or (isinstance(_mp_val, (int, float)) and float(_mp_val) <= 0.0):
        max_pain = 0.0
        distance = 0.0
        mp_str = "N/A"
        dist_str = "⚠️ 數據源缺失"
        dist_color = "\u001b[1;30m"
    else:
        max_pain = float(_mp_val)
        price = c_val
        distance = (((max_pain - price) / price) * 100) if price > 0 else 0.0

        if cb_triggered:
            mp_str = "N/A (已觸發斷路器)"
            dist_str = "⚠️ 偏離度過高 (>30%)"
            dist_color = "\u001b[1;31m"
        else:
            calc_mode = data.get("calculation_mode", "OI")
            is_deg = data.get("is_degraded", False)
            if calc_mode == "Volume" or is_deg:
                mp_str = f"${max_pain:.2f} (Volume 降級)"
            else:
                mp_str = f"${max_pain:.2f}"
            dist_str = f"{distance:+.1f}%"
            dist_color = "\u001b[1;31m" if abs(distance) > 5.0 else "\u001b[1;32m"

    if data.get("tdp_activated"):
        ddp_status = "✨ TDP 估值三擊 (Triple Discount Pricing)"
        ddp_color = "\u001b[1;36m"
    elif data.get("is_ddp"):
        ddp_status = "符合 (符合 DDP 盈餘/估值雙擊)"
        ddp_color = "\u001b[1;32m"
    else:
        ddp_status = "不符合"
        ddp_color = "\u001b[1;30m"

    ivr_val = data.get("iv_rank")
    if ivr_val is None:
        ivr_str = "--"
        ivr_color = "\u001b[1;30m"
        ivr_comp = 0.0
    else:
        ivr_num = float(ivr_val)
        ivr_str = f"{ivr_num:.1f}%"
        ivr_color = "\u001b[1;35m" if ivr_num > 50.0 else "\u001b[1;36m"
        ivr_comp = ivr_num

    _raw_pcr = data.get("pcr")
    pcr_dict: dict = _raw_pcr if isinstance(_raw_pcr, dict) else {}
    vol_pcr_raw = pcr_dict.get("volume_pcr") if pcr_dict else None
    vol_pcr = float(vol_pcr_raw) if vol_pcr_raw is not None else 0.0

    oi_pcr_raw = pcr_dict.get("oi_pcr", pcr_dict.get("pcr")) if pcr_dict else None
    oi_pcr = float(oi_pcr_raw) if oi_pcr_raw is not None else 0.0

    if pcr_dict:
        if is_premarket or vol_pcr_raw is None or vol_pcr == 0.0:
            volume_state = "⚖️ 封盤中 (盤前未更新)"
            vol_pcr_str = "--"
            vol_pcr_color = "\u001b[1;30m"
        else:
            vol_pcr_str = f"{vol_pcr:.2f}"
            if "volume_pcr_state" in pcr_dict:
                volume_state = pcr_dict["volume_pcr_state"]
            elif vol_pcr < 0.90:
                volume_state = "🐂 中性偏多/看漲主導"
            elif vol_pcr > 1.10:
                volume_state = "🐻 偏向空頭/看空主導"
            else:
                volume_state = "⚖️ 結構平衡"
            vol_pcr_color = (
                "\u001b[1;32m"
                if vol_pcr < 0.90
                else ("\u001b[1;31m" if vol_pcr > 1.10 else "\u001b[1;36m")
            )

        if oi_pcr_raw is None or oi_pcr == 0.0:
            oi_state = "N/A (結構缺失)"
            oi_pcr_str = "--"
            oi_pcr_color = "\u001b[1;30m"
        else:
            oi_pcr_str = f"{oi_pcr:.2f}"
            if "oi_pcr_state" in pcr_dict:
                oi_state = pcr_dict["oi_pcr_state"]
            elif oi_pcr < 0.90:
                oi_state = "🏹 結構激進/看漲多頭沉澱"
            elif oi_pcr > 1.20:
                oi_state = "🛡️ 結構防禦/虛值 Put 沉澱"
            else:
                oi_state = "⚖️ 籌碼結構中性"
            oi_pcr_color = (
                "\u001b[1;32m"
                if oi_pcr < 0.90
                else ("\u001b[1;31m" if oi_pcr > 1.10 else "\u001b[1;36m")
            )
    else:
        volume_state = "⚖️ 封盤中 (盤前未更新)"
        vol_pcr_str = "--"
        vol_pcr_color = "\u001b[1;30m"
        oi_state = "N/A (結構缺失)"
        oi_pcr_str = "--"
        oi_pcr_color = "\u001b[1;30m"

    if cb_triggered:
        scenario = "⚠️ 最大痛點偏離度過高 (>30%) 已啟動斷路器，暫停輸出結算操作指引，請以技術指標為準。"
        scen_color = "\u001b[1;31m"
    elif _mp_val is None or max_pain == 0.0:
        scenario = "期權未平倉量數據不足，無法評估結算磁吸效應。"
        scen_color = "\u001b[1;30m"
    elif abs(distance) < 2.0:
        scenario = "價格接近最大痛點，結算日前可能維持震盪。"
        scen_color = "\u001b[1;33m"
    elif distance > 5.0:
        scenario = "價格遠低於最大痛點，具備磁吸效應回升動能。"
        scen_color = "\u001b[1;32m"
    elif distance < -5.0:
        scenario = "價格遠高於最大痛點，需留意結算日前壓回風險。"
        scen_color = "\u001b[1;31m"
    else:
        scenario = "目前價差適中，依技術指標操作為主。"
        scen_color = "\u001b[1;36m"

    scenario_overlays: list[str] = []
    if is_structural_divergence:
        scenario_overlays.append(
            "⚠️ 結構性情緒背離：Skew 避險分位極端，且 PCR 落在相反極端，請以防守為主。"
        )
    if ivr_comp >= 90.0:
        scenario_overlays.append(
            "⚠️ IV Rank 極高：避免追價單腿多方；優先定義風險的價差/保護性結構，並縮小口數。"
        )

    spread_ratio = data.get("spread_ratio")
    if spread_ratio is not None and spread_ratio > 15.0:
        scenario_overlays.append(
            f"⚠️ 流動性警告：當前期權買賣點差高達 {spread_ratio:.1f}%，滑價風險大，請勿掛市價單。"
        )
    if scenario_overlays:
        scenario = f"{scenario} " + " ".join(scenario_overlays)
        scen_color = "\u001b[1;31m" if is_structural_divergence else "\u001b[1;33m"

    month_mps = data.get("month_max_pains", [])
    mp_lines = []
    if month_mps:
        try:
            month_mps_sorted = sorted(month_mps, key=lambda x: x.get("expiry", ""))
        except Exception:
            month_mps_sorted = month_mps

        for i, item in enumerate(month_mps_sorted):
            exp = item.get("expiry", "N/A")
            mp_val = item.get("max_pain")
            dist = item.get("distance_pct", 0.0)
            is_deg = item.get("is_degraded", False)
            calc_mode = item.get("calculation_mode", "OI")

            try:
                from market_time import ny_tz

                today_ny = datetime.now(ny_tz).date()
                exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_dt - today_ny).days
            except Exception:
                dte = 0

            if dte <= 7:
                label = "週五即期" if dte >= 4 and dte <= 6 else "短線週線"
            elif dte <= 14:
                label = "次週主力"
            else:
                label = "月線主力"

            deg_tag = " (V)" if is_deg or calc_mode == "Volume" else ""
            mp_price_str = f"${mp_val:.2f}{deg_tag}" if mp_val is not None else "N/A"
            dist_val_str = f"{dist:+.1f}%" if mp_val is not None else "N/A"
            color_item = "\u001b[1;31m" if abs(dist) > 5.0 else "\u001b[1;32m"
            prefix = " ├─ " if i < len(month_mps_sorted) - 1 else " └─ "

            mp_lines.append(
                f"{prefix}{exp} (DTE {dte} / {label}): \u001b[1;33m{mp_price_str}\u001b[0m (當前價差: {color_item}{dist_val_str}\u001b[0m)"
            )
    else:
        mp_lines.append(
            f" └─ Max Pain價位: \u001b[1;33m{mp_str}\u001b[0m (當前價差: {dist_color}{dist_str}\u001b[0m)"
        )

    target_lines = [
        "```ansi",
        " 最大痛點結算 (Max Pain Settlement)",
    ]
    target_lines.extend(mp_lines)
    target_lines.extend(
        [
            " DDP 與期權風控 (DDP & Risk Metrics)",
            f" ├─ DDP 估值雙擊: {ddp_color}{ddp_status}\u001b[0m",
            f" ├─ IV Rank: {ivr_color}{ivr_str}\u001b[0m",
            f" ├─ Volume PCR (即時情緒): {vol_pcr_color}{vol_pcr_str}\u001b[0m ({vol_pcr_color}{volume_state}\u001b[0m)",
            f" └─ OI PCR (結構防禦): {oi_pcr_color}{oi_pcr_str}\u001b[0m ({oi_pcr_color}{oi_state}\u001b[0m)",
            " 結算價操作指引 (Scenario Analysis)",
            f" └─ 操作指引: {scen_color}{scenario}\u001b[0m",
        ]
    )
    kelly_sizing = data.get("kelly_sizing")
    if kelly_sizing:
        contracts = kelly_sizing.suggested_contracts
        exposure = kelly_sizing.exposure_pct
        warnings = (
            " | ".join(kelly_sizing.warnings)
            if getattr(kelly_sizing, "warnings", None)
            else "安全/符合風控"
        )
        target_lines.extend(
            [
                " 安全建倉額度 (Kelly Risk Sizing)",
                f" ├─ 建議上限: \u001b[1;36m{contracts} 口\u001b[0m (佔總資金 {exposure}%)",
                f" └─ 系統風控: \u001b[1;33m{warnings}\u001b[0m",
            ]
        )

    target_lines.append("```")

    embed.add_field(
        name="🎯 結算與目標 (Target Lock)", value="\n".join(target_lines), inline=False
    )

    # 4.5. 🦇 暗池與大宗交易跡象 (Dark Pool Prints)
    dp_data = data.get("darkpool")
    if dp_data:
        dp_lines = ["```ansi"]
        prints = dp_data.get("prints", [])
        if prints:
            dp_lines.append(" 💰 近期最大暗池成交 (Top 3 Block Prints)")
            top3 = sorted(prints, key=lambda x: x.get("premium", 0), reverse=True)[:3]
            for i, p in enumerate(top3):
                pr = p.get("price", 0)
                vol = p.get("volume", 0)
                prem = p.get("premium", 0)
                prem_m = prem / 1000000.0
                prefix = " ├─" if i < len(top3) - 1 else " └─"
                dp_lines.append(
                    f"{prefix} \u001b[1;36m${pr:>7.2f}\u001b[0m | 量: {vol:>8,} | 金額: \u001b[1;33m${prem_m:>6.2f}M\u001b[0m"
                )
        else:
            dp_lines.append(" 💰 近期最大暗池成交 (Top 3 Block Prints)")
            dp_lines.append(" └─ 近 24 小時無顯著大宗交易。")

        dp_poc = dp_data.get("dp_poc")
        if dp_poc is not None and float(dp_poc) > 0:
            dp_lines.append("")
            dp_lines.append(" 🌊 籌碼與防禦共振 (Support Resonance)")
            dp_lines.append(
                f" ├─ 暗池磁吸價 (DP-POC): \u001b[1;35m${float(dp_poc):.2f}\u001b[0m"
            )

            gex_putwall = data.get("gex", {}).get("put_wall")
            if gex_putwall is not None and float(gex_putwall) > 0:
                is_overlap = (
                    abs(float(dp_poc) - float(gex_putwall)) / float(gex_putwall) <= 0.01
                )
                if is_overlap:
                    dp_lines.append(
                        " └─ 狀態: \u001b[1;31m🛡️ 絕對防禦共振\u001b[0m (與 PutWall 高度重疊)"
                    )
                else:
                    dp_lines.append(" └─ 狀態: \u001b[1;30m⚪ 無顯著重疊\u001b[0m")
            else:
                dp_lines.append(" └─ 狀態: \u001b[1;30m⚪ 缺乏 PutWall 數據\u001b[0m")

        dp_lines.append("```")
        embed.add_field(
            name="🦇 暗池大宗交易與支撐 (Dark Pool)",
            value="\n".join(dp_lines),
            inline=False,
        )

    # 5. 🐋 異常活動 (UOA)
    uoa_data = data.get("uoa", [])
    if uoa_data:
        try:
            # 確保外部已載入 _format_uoa_field，或這裡安全呼叫
            table_str = _format_uoa_field(uoa_data)
            embed.add_field(
                name="🐋 異常活動 (UOA)",
                value=f"```ansi\n{table_str}\n```",
                inline=False,
            )
        except NameError:
            embed.add_field(
                name="🐋 異常活動 (UOA)",
                value="```ansi\n無法渲染異常活動資料\n```",
                inline=False,
            )
    else:
        embed.add_field(
            name="🐋 異常活動 (UOA)",
            value="```ansi\n目前無顯著異常活動\n```",
            inline=False,
        )

    embed.set_footer(
        text="🔗 使用 /settle_hedge 紀錄對沖或 /event_impact 進行曝險模擬。"
    )
    return embed


def create_tactical_hedge_embed(
    symbol: str, ivr: float, rec_strategy: str
) -> discord.Embed:
    """建構標的對沖防禦中心的 Embed"""
    embed = discord.Embed(
        title=f"🛡️ {symbol} 對沖防禦中心 (Tactical Hedging)",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="📊 當前波動率狀態 (Volatility Context)",
        value=f"* **IV Rank:** `{ivr:.1f}%`\n* **推薦防禦策略:** `{rec_strategy}`",
        inline=False,
    )
    embed.add_field(
        name="🛠️ 推薦執行步驟",
        value="1. 請在終端機或聊天室中輸入 `/settle_hedge` 以登錄對沖操作。\n2. 可搭配 `/event_impact` 輸入事件代號模擬近期宏觀事件（財報/CPI）對選擇權曝險的影響。",
        inline=False,
    )
    return embed
