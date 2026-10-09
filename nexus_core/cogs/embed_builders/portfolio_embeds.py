"""Portfolio and trading position embed builders"""

import discord
from cogs.embed_builders._core import (
    OPTION_DATA_TIMING_NOTE,
    NexusEmbed,
    format_cache_age_suffix,
)
import logging
import math
import psutil

import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Literal, Optional

from market_analysis.uoa_telemetry import UOATradeResult, generate_uoa_ascii_table
from market_analysis.index_microstructure import (
    GAMMA_FLIP_MATERIALITY_DISPLAY,
    analyze_local_gamma_regime,
    estimate_material_gamma_flip,
    estimate_symbol_gamma_flip,
    gamma_flip_materiality,
    interpolate_gamma_flip_zero,
)
from market_analysis.room_threshold import (
    _DAILY_NOISE_STOP_ATR_1D_MULT,
    _ROOM_ABSOLUTE_FLOOR_PCT,
    _ROOM_ATR_1D_MULTIPLIER,
    _ROOM_RISK_MULTIPLIER,
    _ROOM_STOP_ATR_15M_MULTIPLIER,
    _ROOM_STOP_FALLBACK_ATR_15M_MULTIPLIER,
    compute_dynamic_room_threshold,
    compute_reference_stop,
    evaluate_wall_buffer,
    is_valid_daily_atr,
    resolve_atr_15m,
)
from market_analysis.ivr_strategy_gate import is_selling_locked_by_ivr
from market_analysis.sentiment.max_pain import find_settlement_gravity
from market_analysis.gex_wall_depth import thin_wall_threshold
from market_analysis.dynamic_rollover.constants import (
    _ENTRY_VOLUME_SURGE_MULTIPLIER,
)
from market_analysis.dynamic_rollover.structural_signals import (
    _scan_gex_walls,
    _scan_resistance_wall_above_spot,
)

from cogs.embed_builders._ansi_utils import _pad_string, _safe_float
from cogs.embed_builders._embed_helpers import (
    _add_ansi_field_safely,
    format_psq_matrix_lines,
    format_runway_lines,
    _fmt_gex_notional,
    gamma_flip_noise_note,
    macro_iv_status_text,
    _chunk_ansi_table,
    _truncate_with_boundary,
)
from market_analysis.sentiment.skew_taxonomy import (
    POLYMARKET_BEARISH_PCT,
    POLYMARKET_BULLISH_PCT,
    SKEW_BULLISH_PERCENTILE,
    SKEW_DEFENSIVE_PERCENTILE,
    SKEW_DIVERGENCE_HIGH_PERCENTILE,
    SKEW_DIVERGENCE_LOW_PERCENTILE,
    SKEW_HIGH_DEFENSE_PERCENTILE,
)

logger = logging.getLogger(__name__)

# GEX PutWall 與機構大額 STO PUT 履約價的分歧門檻（相對現價的距離百分比）。
# 超過此門檻視為兩個支撐訊號互相矛盾，值得在顯示層揭露供使用者交叉參考。
_GEX_STO_DIVERGENCE_THRESHOLD_PCT = 2.0
# CallWall 貼牆帶：距牆 < max(1×ATR₁₅ₘ, 此下限%) 視為貼牆（PRE_CALIBRATION／僅呈現）。
_CALLWALL_HUG_MIN_PCT = 0.10
# 被跌破（價內）的 STO Put 往上掃描的最大範圍 %（PRE_CALIBRATION／僅呈現）。
_STO_PUT_BREACH_MAX_PCT = 5.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _sto_cap_eligible(s: Any) -> bool:
    """STO 大單是否可作方向性牆體訊號：非價差／跨式腿，或為貸方價差賣出腿。"""
    return isinstance(s, dict) and (
        not s.get("structure_leg") or bool(s.get("spread_credit"))
    )


def _sto_notional_suffix(notional: float) -> str:
    if notional >= 1_000_000:
        return f", 權利金 ${notional / 1_000_000:.2f}M"
    if notional > 0:
        return f", 權利金 ${notional / 1_000:.1f}k"
    return ""


def _sto_exp_dte(expiry: str, today: Any) -> str:
    """回傳「10-16(DTE7)」；到期日無法解析時為空字串。"""
    if not expiry:
        return ""
    try:
        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
    except ValueError:
        return f" {expiry[5:]}"
    return f" {expiry[5:]}(DTE{dte})"


def _sto_best(cands: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    cands = [
        c
        for c in cands
        if _num(c.get("strike"), 0.0) > 0 and _num(c.get("volume"), 0.0) > 0
    ]
    if not cands:
        return None
    return max(
        cands,
        key=lambda c: _num(c.get("notional_value"), _num(c.get("volume"), 0.0)),
    )


# 「極端高波」採用的 IV 至少要有的 DTE：太短的到期日含結算 Gamma，IV 被墊高。
# 跨式反推 IV 與 LIVE_IV 共用同一門檻。
_EXTREME_VOL_MIN_DTE = 5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_gex_expiry(
    gex_data: Any, data: Dict[str, Any], now_ny: datetime
) -> str | None:
    """/x GEX 區塊所涵蓋的到期日：實際值優先、推定後備。

    1. 過期快取（`_is_stale_cache`）不標到期日。
    2. edge 回傳的 `expiry`（被計入 GEX 的合約最常見到期日）。
    3. 後備：完整期權到期日清單的最近一檔未結算者（Yahoo 預設頁）；
       到期當日 16:00 ET 後無法確定，回傳 None。
    """
    if not isinstance(gex_data, dict) or gex_data.get("_is_stale_cache"):
        return None
    actual = gex_data.get("expiry")
    if actual:
        return str(actual)
    expiries = data.get("option_expiries")
    if not isinstance(expiries, list) or not expiries:
        return None
    from market_analysis.sentiment.max_pain import next_unsettled_expiry

    return next_unsettled_expiry(expiries, now_ny)


def _session_phase(now_ny: datetime) -> Literal["盤中", "盤前", "盤後", "休市"]:
    """標題與 IV 文案共用的交易時段判定（美東時間）。

    已過 09:30 但市場未開 = 已收盤（涵蓋提前收盤日），不需另查收盤時刻。
    """
    import market_time as _mt

    if _mt.is_market_open():
        return "盤中"
    if not _mt.is_nyse_trading_day(now_ny.date()):
        return "休市"
    if (now_ny.hour, now_ny.minute) >= (9, 30):
        return "盤後"
    return "盤前"


def get_scenario_guidance(
    current_price: float,
    max_pain: float,
    threshold: float = 0.03,
    *,
    sigma_to_expiry: float | None = None,
    pin_strike: float | None = None,
) -> str:
    """Generate Max Pain settlement guidance from the spot/Max Pain spread."""
    if max_pain <= 0.0:
        return "期權未平倉量數據不足，無法評估結算磁吸效應。"
    if pin_strike is not None:
        return (
            f"Long Gamma 釘住 ${pin_strike:.2f} 為主；痛點 ${max_pain:.2f} 僅供參考。"
        )
    if sigma_to_expiry is not None and abs(current_price - max_pain) > sigma_to_expiry:
        return (
            f"痛點 ${max_pain:.2f} 超出結算前 1σ (±${sigma_to_expiry:.2f})，"
            "收斂機率低。"
        )

    spread_pct = (current_price - max_pain) / max_pain

    if spread_pct > threshold:
        return "價格高於最大痛點，結算日前需防範向痛點震盪拉回的壓制力。"
    if spread_pct < -threshold:
        return "價格遠低於最大痛點，具備磁吸效應回升動能。"
    return "目前價差適中，依技術指標操作為主。"


def _format_uoa_field(uoa_data: list) -> str:
    """將 uoa_data 列表轉換為動態對齊的標準 ASCII 表格。"""
    trades = []
    for item in uoa_data:
        if "action" in item and "intent" in item:
            action_str = str(item.get("action", ""))
            trade_type_str = str(item.get("trade_type", "SWEEP")).upper()
            if action_str.startswith("⚖️ MIDPOINT"):
                flow_tag = "❔ 未定" if item.get("direction_note") else "⚖️ CROSS"
            elif item.get("trade_type_inferred"):
                flow_tag = "📊 日累積"
            elif trade_type_str == "BLOCK":
                flow_tag = "📦 BLOCK"
            else:
                flow_tag = "🔥 SWEEP"
            _ratio_f = float(item.get("ratio", 0.0))
            _paced = item.get("paced_ratio")
            _ratio_str = str(item.get("ratio_str", f"{_ratio_f}x"))
            if _paced is not None and abs(float(_paced) - _ratio_f) >= 0.05:
                _ratio_str = f"{_ratio_f:.2f}x→{float(_paced):.2f}x"
            trade = UOATradeResult(
                expiry=str(item.get("expiry", "")),
                strike_price=float(item.get("strike", 0.0)),
                option_type=str(item.get("type", "")),
                trade_price=float(item.get("trade_price", 0.0)),
                bid_price=float(item.get("bid_price", 0.0)),
                ask_price=float(item.get("ask_price", 0.0)),
                volume=int(item.get("volume", 0)),
                open_interest=int(item.get("oi", 0)),
                ratio=_ratio_f,
                ratio_str=_ratio_str,
                action=f"{flow_tag} {action_str}",
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


# ---------------------------------------------------------------------------
# Public embed builders
# ---------------------------------------------------------------------------


def create_holdings_embed(
    holdings_data: List[Dict[str, Any]], total_capital: float
) -> discord.Embed:
    """建構現貨持倉 (Holdings) 狀態報告 Embed"""
    embed = NexusEmbed(
        title="💰 Nexus Seeker | 現貨持倉清單",
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
    header = f"{_pad_string('標的', 8)} | {_pad_string('數量', 8, 'right')} | {_pad_string('平均成本', 10, 'right')} | {_pad_string('現價', 10, 'right')} | {_pad_string('當前損益', 10, 'right')} | {_pad_string('配置', 20, 'right')} | {_pad_string('建倉日', 10, 'right')}"
    divider = "-" * len(header)
    # 核心資金部署引擎 (Scenario 5) target_allocation_pct 總經自動建議值：僅供參考，
    # 不會自動套用生效，獨立列於表格外的提示區塊，避免與已實際生效的配置欄位混淆。
    target_alloc_suggestions: List[str] = []

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

        # 核心/衛星分類與配置上限：透過 /edit_holding 設定，供動態轉倉引擎
        # (Scenario 3 核心衛星再平衡) 判斷是否超限。target 僅在使用者設定過
        # 目標配置比例時才附加顯示。
        asset_class_label = "CORE" if h.get("asset_class") == "CORE" else "SAT"
        max_alloc = h.get("max_allocation_pct")
        alloc_str = (
            f"{asset_class_label} {max_alloc * 100:.0f}%"
            if max_alloc is not None
            else asset_class_label
        )
        target_alloc = h.get("target_allocation_pct")
        if target_alloc is not None:
            alloc_str += f"→{target_alloc * 100:.0f}%"
        else:
            suggested_target = h.get("suggested_target_allocation_pct")
            if suggested_target is not None:
                target_alloc_suggestions.append(
                    f" • {h['symbol']}：建議目標配置 {suggested_target:.0f}%"
                    "（依當前總經市況自動評估，未生效，需自行以 /edit_holding 設定）"
                )
        alloc_fmt = _pad_string(alloc_str, 20, "right")
        acquired_fmt = _pad_string(h.get("acquired_at") or "—", 10, "right")

        data_lines.append(
            f"{sym} | {qty} | {cost} | {curr_p_fmt} | {pnl_fmt} | {alloc_fmt} | {acquired_fmt}"
        )

    chunks = _chunk_ansi_table(header, divider, data_lines)
    for i, chunk in enumerate(chunks):
        name = (
            f"📦 持倉明細 ({i+1}/{len(chunks)})" if len(chunks) > 1 else "📦 持倉明細"
        )
        embed.add_field(name=name, value=chunk, inline=False)

    if target_alloc_suggestions:
        embed.add_field(
            name="💡 核心資金部署建議 (Scenario 5)",
            value="\n".join(target_alloc_suggestions),
            inline=False,
        )

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
    embed = NexusEmbed(
        title="💰 Nexus Seeker | 實單持倉清單 (包含帳面損益)",
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
        # 報價缺失 (current_price None) 顯示 `--`，不再顯示 ±100% 損益；
        # ask/2 估算值加註 `*`。
        if curr_p is None or unrealized_pnl is None or pnl_pct is None:
            curr_fmt = f"{'--':>6}"
            pnl_fmt = f"\x1b[0;33m{'報價缺失':>10}\x1b[0m"
        else:
            est_mark = "*" if t.get("quote_source") == "ASK_HALF" else ""
            curr_fmt = f"{curr_p:6.2f}{est_mark}"
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
    missing_quotes = int(pnl_data.get("missing_quote_count", 0) or 0)
    has_estimate = any(t.get("quote_source") == "ASK_HALF" for t in trades)

    summary = (
        f"💰 **持倉總權利金成本 (概算)**: `${total_cost:,.2f}`\n"
        f"⚖️ **佔總預算比例**: `{ (total_cost / total_capital * 100) if total_capital > 0 else 0:.1f}%`\n"
        f"📈 **總未實現損益 (Unrealized PnL)**: `${total_unrealized_pnl:,.2f}`"
    )
    if missing_quotes:
        summary += f"\n⚠️ {missing_quotes} 筆部位報價缺失，未計入損益"
    if has_estimate:
        summary += "\n`*` 零 bid，現價以 ask/2 估算"
    embed.add_field(name="🏁 財務摘要 (Financial Summary)", value=summary, inline=False)

    embed.set_footer(text="Nexus Portfolio Engine | 專業實單與損益監控")
    return embed


# /dash 對沖狀態：淨 Beta-Delta 美元名目 (|Δ| × SPY) 低於資本的 10% 視為平衡。
_DASH_HEDGE_NOTIONAL_RATIO: float = 0.1


def create_strategic_dash_embed(
    user_ctx: Any,
    pnl_data: Dict[str, Any],
    vix_spot: Optional[float] = None,
    runway: Any = None,
    runway_stale: bool = False,
    nav_data: Optional[Dict[str, Any]] = None,
    spy_price: Optional[float] = None,
) -> discord.Embed:
    """
    建構戰略看板 (Strategic Dashboard) Embed.
    遵循 Task 1 的 Traditional Chinese 模板。
    """
    embed = NexusEmbed(
        title="📊 Nexus 交易員戰略看板",
        color=discord.Color.dark_blue(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 提領跑道 (Withdrawal Runway；docs/risk_portfolio/05)
    daily_theta = _safe_float(user_ctx.total_theta)
    monthly_expense = _safe_float(user_ctx.monthly_expense)
    daily_expense = monthly_expense / 30.0 if monthly_expense > 0 else 0.0
    coverage_pct = (daily_theta / daily_expense * 100) if daily_expense > 0 else 100.0
    cash_reserve = _safe_float(user_ctx.cash_reserve)

    from market_analysis.trading_orchestration import get_safety_payout_threshold

    payout_threshold = get_safety_payout_threshold()

    status_mode = "觀戰模式" if not user_ctx.is_professional_mode else "實戰模式"
    # NAV 按市價：現金 + 現貨市值 + 期權市值 (賣方為負債)，見
    # ReportsMixin.get_market_nav。未提供 nav_data 時顯示 `--` (不再用成本
    # 資本 + 未實現損益——那會忽略現貨漲跌並重複計入賣方權利金)。
    if nav_data is not None:
        nav_line = f"`${_safe_float(nav_data.get('nav')):,.0f}` (按市價，{status_mode})"
        missing_parts: List[str] = []
        if nav_data.get("missing_spot_symbols"):
            missing_parts.append(
                "現貨 " + "、".join(nav_data.get("missing_spot_symbols") or [])
            )
        if nav_data.get("missing_option_quotes"):
            missing_parts.append(f"期權 {nav_data.get('missing_option_quotes')} 筆")
        if missing_parts:
            nav_line += f"\n  ⚠️ 報價缺失未計入：{'；'.join(missing_parts)}"
    else:
        nav_line = f"`--` ({status_mode})"

    runway_info = (
        f"* **總資產 (NAV):** {nav_line}\n" f"* **現金儲備:** `${cash_reserve:,.2f}`\n"
    )
    for line in format_runway_lines(runway, stale=runway_stale):
        runway_info += f"* {line}\n"
    runway_info += (
        f"* **收租效率:** 每日 Theta `${daily_theta:,.2f}`"
        + (f" (覆蓋期權熔斷月支出 {coverage_pct:.1f}%)" if daily_expense > 0 else "")
        + "\n"
        f"* **安全提領紅線:** `${payout_threshold:,.0f}` (流動性防守限制)\n"
    )

    embed.add_field(
        name="🏁 提領跑道 (Withdrawal Runway)", value=runway_info, inline=False
    )

    # 2. 組合風險精算 (NRO Integrity)
    # Vanna Sensitivity Stress Test: 若 VIX 上升 10%，隱含 Delta 將變動至 [New_Delta]
    total_vanna = _safe_float(user_ctx.total_vanna)
    total_delta = _safe_float(user_ctx.total_weighted_delta)
    capital = _safe_float(user_ctx.capital)

    # VIX 未知時不做 Vanna 壓力測試（過去以 18.0 冒充真實 VIX）。
    new_delta: Optional[float] = None
    if vix_spot is not None and vix_spot > 0:
        vanna_impact = total_vanna * (vix_spot * 0.10 / 100.0)
        new_delta = total_delta + vanna_impact

    # 對沖狀態與建議：Beta-Delta 是 SPY 等值「股數」，須乘上 SPY 價格換成
    # 美元名目才能與資本比較 (過去把股數直接和美元比，永遠顯示「運行中」)。
    if spy_price is not None and spy_price > 0:
        hedge_status = (
            "運行中"
            if abs(total_delta) * spy_price < capital * _DASH_HEDGE_NOTIONAL_RATIO
            else "需調整"
        )
    else:
        hedge_status = "資料不足 (SPY 現價未知)"
    # 簡單邏輯：如果 Delta 太正，建議賣出 SPY；如果太負，建議買入 SPY
    if total_delta > 100:
        hedge_instruction = f"賣出 {int(total_delta)} 股 SPY 以對沖正 Delta"
    elif total_delta < -100:
        hedge_instruction = f"買入 {int(abs(total_delta))} 股 SPY 以對沖負 Delta"
    else:
        hedge_instruction = "目前曝險平衡，無需立即調整"

    nro_info = (
        f"* **Beta-Delta:** `{total_delta:+.1f}` (相對於 SPY 的整體曝險)\n"
        + (
            f"* **Vanna 敏感度:** 若 VIX 上升 10%，隱含 Delta 將變動至 `{new_delta:+.1f}`。\n"
            if new_delta is not None
            else "* **Vanna 敏感度:** `--` (VIX 資料不足，無法壓力測試)\n"
        )
        + f"* **對沖狀態:** {hedge_status}\n"
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


# CallWall 空間門檻 max(2.2×停損風險, 1.5×ATR₁D, 3.5%) 中實際生效的那一項
_ROOM_BINDING_TERM_LABELS: Dict[str, str] = {
    "RISK": f"{_ROOM_RISK_MULTIPLIER:g}×停損風險",
    "ATR_1D": f"{_ROOM_ATR_1D_MULTIPLIER:g}×ATR₁D",
    "FLOOR": f"{_ROOM_ABSOLUTE_FLOOR_PCT:.1%} 底線",
}

# 釘住效應判定帶寬：ATR₁D 不可得時，現價距 CallWall 在此百分比內即視為貼牆。
_PIN_FALLBACK_BAND_PCT: float = 1.0

# 15m 量比「放量」門檻；沿用右側鐵律條件一的放量倍數，避免兩處漂移。
RVOL_EXPANSION_THRESHOLD: float = _ENTRY_VOLUME_SURGE_MULTIPLIER


_POLY_PCT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)%\s*(?:巨鯨看多|巨鯨偏空|中性分歧)")


def _parse_poly_bullish_pct(summary: Any) -> Optional[float]:
    """自 `calculate_polymarket_weighted_odds()` 的標籤字串解析加權看多機率 (0~100)。"""
    if not summary:
        return None
    m = _POLY_PCT_PATTERN.search(str(summary))
    if m is None:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if 0.0 <= val <= 100.0 else None


def _format_psq_freshness(
    bar_date: Any, is_live: bool, fetched_at: Any
) -> Optional[str]:
    """擠壓欄位的資料時間行：計算所用的最後一根日線＋日線抓取時刻 (ET)。

    盤中最後一根是今日仍在成型的 K 棒（數值會隨盤中變動），須與已收盤定案的
    K 棒區分；兩項資訊皆缺時回傳 None，不顯示該行。
    """
    parts: List[str] = []
    if bar_date is not None and hasattr(bar_date, "isoformat"):
        bar_str = bar_date.isoformat()
        parts.append(
            f"日線 {bar_str}（盤中成型中）" if is_live else f"日線 {bar_str} 收盤"
        )
    if isinstance(fetched_at, datetime):
        parts.append(f"抓取於 {fetched_at.strftime('%m/%d %H:%M')} ET")
    if not parts:
        return None
    return " 🕒 資料時間: " + " │ ".join(parts)


def create_tactical_symbol_embed(data: Dict[str, Any]) -> discord.Embed:
    """
    建構標的深度分析 (Tactical Deep-Dive) Embed.
    遵循 Task 2 的 Traditional Chinese 模板。
    """
    symbol = data.get("symbol", "UNKNOWN")

    def _to_float_or_none(value: Any) -> float | None:
        try:
            if value is None:
                return None
            val = float(value)
            import math

            return None if (math.isnan(val) or math.isinf(val)) else val
        except (TypeError, ValueError):
            return None

    def _to_float(value: Any, default: float = 0.0) -> float:
        casted = _to_float_or_none(value)
        return casted if casted is not None else default

    # 處理盤前狀態與波動率 degradation
    iv_data = data.get("iv_data")
    current_iv: Any = None
    iv_rank: Any = None
    iv_percentile: Any = None
    expected_move_weekly: Any = None
    title_suffix = ""
    is_premarket = False
    iv_source = None
    current_iv_val = None
    _phase: str = "盤前"
    # IV 區塊判定的事件加載（財報／總經）；供 Kelly 賣方前提共用，False＝無事件或無 IV 區塊
    _event_loading_outer = False

    def _iv_attr(name: str, default: Any = None) -> Any:
        if iv_data is None:
            return default
        if isinstance(iv_data, dict):
            return iv_data.get(name, default)
        return getattr(iv_data, name, default)

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
        current_iv_num = _to_float_or_none(current_iv_val)
        if (
            iv_source is None
            and is_premarket
            and current_iv_num is not None
            and current_iv_num > 0.0
        ):
            iv_source = "STORED_IV"

        if is_premarket:
            import market_time as _mt_title

            _now_title = datetime.now(_mt_title.ny_tz)
            _phase = _session_phase(_now_title)
            _session_word = "" if _phase == "盤中" else _phase
            if current_iv_num is not None and current_iv_num > 0.0:
                _tag = "HV代理" if iv_source == "HV_PROXY" else "前日收盤"
                title_suffix = (
                    f" [{_session_word}/{_tag}]" if _session_word else f" [{_tag}]"
                )
            else:
                title_suffix = " [盤前數據未更新/降級模式]"

    current_iv_num = _to_float_or_none(current_iv_val)
    # 注意：skew_percentile is None 刻意不計入 is_degraded——分位數需要 20 筆
    # sentiment_history 樣本才能算出，樣本不足時回傳 None 是設計上的 fail-safe
    # 中性狀態，不代表本次抓取退化或使用了快取值（Skew 欄位本身已用 --% 呈現，
    # 見下方 skew_val_str/skew_per_str）。只有整體 IV 抓取真的失敗
    # （iv_source == "UNAVAILABLE" 或 current_iv_num is None）才應該讓標題顯示
    # 「數據未更新/降級模式」。
    is_degraded = iv_source == "UNAVAILABLE" or current_iv_num is None
    if is_degraded and not title_suffix:
        title_suffix = " [數據未更新/降級模式]"

    embed = NexusEmbed(
        title=f"🌌 標的分析中心: {symbol}{title_suffix}",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 💹 即時報價 (Real-time Quote)
    quote = data.get("quote") or {}

    c_raw = quote.get("c") if quote.get("c") is not None else data.get("price")
    c_val = _to_float(c_raw)

    dp_raw = quote.get("dp")
    dp_val = _to_float(dp_raw)

    d_raw = quote.get("d")
    d_val = _to_float(d_raw)

    o_raw = quote.get("o")
    o_val = _to_float(o_raw)

    h_raw = quote.get("h")
    h_val = _to_float(h_raw)

    l_raw = quote.get("l")
    l_val = _to_float(l_raw)

    pc_raw = quote.get("pc")
    pc_val = _to_float(pc_raw)

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
            hvn = _to_float(vp.get("hvn"))
            lvn = _to_float(vp.get("lvn"))
            quote_lines.extend(
                [
                    " 近期成交量分佈 (Volume Profile, 20D)",
                    f" └─ 高密集區 (HVN): \u001b[1;35m${hvn:.2f}\u001b[0m | 籌碼真空區 (LVN): \u001b[1;33m${lvn:.2f}\u001b[0m",
                ]
            )

        quote_lines.append("```")

    _add_ansi_field_safely(embed, "💹 即時報價 (Real-time Quote)", quote_lines)

    # 1.5 ⏱️ 15分鐘微觀結構 (15m Microstructure)
    atr_15m_display_val = _to_float_or_none(data.get("atr_15m"))
    if atr_15m_display_val is not None and atr_15m_display_val > 0:
        # pandas_ta.atr 預設 mamode="rma"（Wilder 平滑），不是 EMA
        atr_15m_line = f" ├─ 15m ATR (Wilder 14): ${atr_15m_display_val:.2f}"
    else:
        atr_15m_line = " ├─ 15m ATR (Wilder 14): --"

    from market_time import ny_tz as _ny_tz

    _today_ny = datetime.now(_ny_tz).date()
    session_vwap_val = _to_float_or_none(data.get("session_vwap"))
    if session_vwap_val is not None and session_vwap_val > 0 and c_val > 0:
        vwap_dev_pct = (c_val - session_vwap_val) / session_vwap_val * 100
        # 盤前／休市時 yfinance period=1d 回傳前一交易日，須標明不是今日錨點
        _vwap_date = data.get("session_vwap_date")
        vwap_date_note = (
            f" [前一交易日 {_vwap_date:%m-%d}]"
            if _vwap_date is not None
            and hasattr(_vwap_date, "strftime")
            and _vwap_date != _today_ny
            else ""
        )
        session_vwap_line = (
            f" └─ 日內錨點 (Session VWAP): ${session_vwap_val:.2f}{vwap_date_note}"
            f" (現價偏離: {vwap_dev_pct:+.2f}%)"
        )
    else:
        session_vwap_line = " └─ 日內錨點 (Session VWAP): -- (現價偏離: --)"

    # 供背離偵測（#情緒與邊緣偵測）沿用：最新已收盤 15m K 棒方向與有效量比
    rvol_effective: Optional[float] = None
    bar_body_dir = 0  # +1 實體陽線 / -1 實體陰線 / 0 未知或十字

    bar_15m = data.get("bar_15m")
    has_explicit_15m = (
        data.get("volume_15m") is not None
        or data.get("close_15m") is not None
        or data.get("open_15m") is not None
        or data.get("rvol_15m") is not None
    )
    if bar_15m is not None or has_explicit_15m or "bar_15m" in data:
        o_15m = None
        h_15m = None
        l_15m = None
        c_15m = None
        vol_15m = None
        sma20_15m = None

        if bar_15m is not None:
            if hasattr(bar_15m, "open"):
                o_15m = _to_float_or_none(getattr(bar_15m, "open", None))
                h_15m = _to_float_or_none(getattr(bar_15m, "high", None))
                l_15m = _to_float_or_none(getattr(bar_15m, "low", None))
                c_15m = _to_float_or_none(getattr(bar_15m, "close", None))
                vol_15m = _to_float_or_none(getattr(bar_15m, "volume", None))
                sma20_15m = _to_float_or_none(getattr(bar_15m, "avg_volume", None))
            elif isinstance(bar_15m, dict):
                o_15m = _to_float_or_none(bar_15m.get("open"))
                h_15m = _to_float_or_none(bar_15m.get("high"))
                l_15m = _to_float_or_none(bar_15m.get("low"))
                c_15m = _to_float_or_none(bar_15m.get("close"))
                vol_15m = _to_float_or_none(bar_15m.get("volume"))
                sma20_15m = _to_float_or_none(
                    bar_15m.get("avg_volume", bar_15m.get("volume_15m_sma20"))
                )

        if o_15m is None:
            o_15m = _to_float_or_none(data.get("open_15m"))
        if h_15m is None:
            h_15m = _to_float_or_none(data.get("high_15m"))
        if l_15m is None:
            l_15m = _to_float_or_none(data.get("low_15m"))
        if c_15m is None:
            c_15m = _to_float_or_none(data.get("close_15m"))
        if vol_15m is None:
            vol_15m = _to_float_or_none(data.get("volume_15m"))
        if sma20_15m is None:
            sma20_15m = _to_float_or_none(
                data.get("volume_15m_sma20", data.get("avg_volume_15m"))
            )

        raw_bar_notes = data.get("bar_15m_notes")
        bar_notes: List[str] = (
            [str(n) for n in raw_bar_notes] if isinstance(raw_bar_notes, list) else []
        )
        rvol_raw = _to_float_or_none(data.get("rvol_15m"))
        if bar_notes:
            # 凍結／合併的 K 棒不得重算出「放量」結論（見 intraday_consistency.py）
            rvol_val = None
        elif rvol_raw is not None and sma20_15m is not None and sma20_15m > 0:
            rvol_val = rvol_raw
        elif vol_15m is not None and sma20_15m is not None and sma20_15m > 0:
            rvol_val = vol_15m / sma20_15m
        else:
            rvol_val = None
        rvol_tod_val = (
            _to_float_or_none(data.get("rvol_15m_tod"))
            if rvol_val is not None
            else None
        )
        tod_samples = int(_to_float(data.get("tod_sample_count"), 0.0))
        _tod_stat_word = "平均" if data.get("tod_stat") == "mean" else "中位數"
        _bt = data.get("bar_15m_time")
        is_auction_bar = isinstance(_bt, datetime) and (
            (_bt.hour, _bt.minute) in ((9, 30), (15, 45))
        )
        # 供下方背離偵測沿用同一個量比判定基準
        rvol_effective = rvol_tod_val if rvol_tod_val is not None else rvol_val

        if (
            o_15m is None
            and h_15m is None
            and l_15m is None
            and c_15m is None
            and vol_15m is None
        ):
            micro_lines = [
                "```ansi",
                " ├─ 最新 15m K棒: -- (暫無數據 / 待開盤)",
                " ├─ 15m 成交量: -- 股",
                " ├─ 15m 均量 (SMA20): -- 股",
                " ├─ 即時量比 (RVOL_15m): -- (狀態: ⚠️ 數據源缺失)",
                atr_15m_line,
                session_vwap_line,
                "```",
            ]
        else:
            # 1. K-line real body verification
            if c_15m is not None and o_15m is not None and c_15m > 0 and o_15m > 0:
                if c_15m > o_15m:
                    kline_type = "實體陽線"
                    bar_body_dir = 1
                elif c_15m < o_15m:
                    kline_type = "實體陰線"
                    bar_body_dir = -1
                else:
                    kline_type = "平盤十字"
                o_str = f"{o_15m:.2f}"
                h_str = f"{h_15m:.2f}" if (h_15m is not None and h_15m > 0) else "--"
                l_str = f"{l_15m:.2f}" if (l_15m is not None and l_15m > 0) else "--"
                c_str = f"{c_15m:.2f}"
                bar_time_val = data.get("bar_15m_time")
                if isinstance(bar_time_val, datetime):
                    if bar_time_val.date() != _today_ny:
                        bar_time_str = f" @{bar_time_val:%m-%d %H:%M} [前一交易日]"
                    else:
                        bar_time_str = f" @{bar_time_val:%H:%M}"
                else:
                    bar_time_str = ""
                kline_line = f" ├─ 最新 15m K棒{bar_time_str}: 開 {o_str} | 高 {h_str} | 低 {l_str} | 收 {c_str} ({kline_type})"
            else:
                kline_line = " ├─ 最新 15m K棒: -- (數據不全)"

            # 2. 15m 成交量
            vol_str = f"{vol_15m:,.0f}" if vol_15m is not None else "--"
            vol_line = f" ├─ 15m 成交量: {vol_str} 股"

            # 3. 15m 均量 (SMA20)
            sma20_str = f"{sma20_15m:,.0f}" if sma20_15m is not None else "--"
            sma20_line = f" ├─ 15m 均量 (SMA20): {sma20_str} 股"

            # 4. 即時量比 (RVOL_15m)
            if bar_notes:
                rvol_line = (
                    " ├─ 即時量比 (RVOL_15m): -- (狀態: ⚠️ " + "；".join(bar_notes) + ")"
                )
            elif rvol_val is not None:
                # 量比只代表量能，不代表價位突破（是否站上阻力見 GEX 區塊）。
                # 狀態優先以同時段量比判定：20 根滾動均量跨越日內 U 型量能曲線，
                # 開盤／收盤競價 K 棒對它必然「放量」。
                status_basis = rvol_tod_val if rvol_tod_val is not None else rvol_val
                if status_basis >= RVOL_EXPANSION_THRESHOLD:
                    status_str = f"🟢 放量 >= {RVOL_EXPANSION_THRESHOLD}x"
                elif bar_body_dir < 0:
                    status_str = (
                        f"🟡 縮量回檔 < {RVOL_EXPANSION_THRESHOLD}x（賣壓未放大）"
                    )
                elif bar_body_dir > 0:
                    status_str = f"❌ 缺乏放量代償 < {RVOL_EXPANSION_THRESHOLD}x（上漲未獲量能確認）"
                else:
                    status_str = f"⚪ 量能平淡 < {RVOL_EXPANSION_THRESHOLD}x"
                tod_str = ""
                if rvol_tod_val is not None:
                    tod_str = (
                        f"｜同時段量比 {rvol_tod_val:.2f}x"
                        f" (前 {tod_samples} 日{_tod_stat_word}"
                        + ("，樣本少" if tod_samples < 3 else "")
                        + ")"
                    )
                elif is_auction_bar:
                    tod_str = "｜⚠ 開盤／收盤競價時段，量比天然偏高"
                rvol_line = (
                    f" ├─ 即時量比 (RVOL_15m): {rvol_val:.2f}x{tod_str}"
                    f" (狀態: {status_str})"
                )
            else:
                rvol_line = " ├─ 即時量比 (RVOL_15m): -- (狀態: ⚠️ 數據源缺失)"

            micro_lines = [
                "```ansi",
                kline_line,
                vol_line,
                sma20_line,
                rvol_line,
                atr_15m_line,
                session_vwap_line,
                "```",
            ]

        _add_ansi_field_safely(
            embed, "⏱️ 15分鐘微觀結構 (15m Microstructure)", micro_lines
        )

    # 2. 📐 情緒與邊緣偵測 (Edge Detection)
    skew_val_raw = data.get("skew")
    skew_val = _to_float_or_none(skew_val_raw)
    skew_percentile = _to_float_or_none(data.get("skew_percentile"))
    poly_odds = data.get("polymarket_odds", "N/A")
    reddit_score = data.get("reddit_sentiment_score", "中性")

    _raw_pcr = data.get("pcr")
    pcr_data_for_div: dict = _raw_pcr if isinstance(_raw_pcr, dict) else {}
    pcr_val_raw = pcr_data_for_div.get("volume_pcr", pcr_data_for_div.get("pcr"))
    pcr_val_for_div = _to_float_or_none(pcr_val_raw)

    iv_rank_val = None
    if iv_data:
        if hasattr(iv_data, "iv_rank"):
            iv_rank_val = iv_data.iv_rank
        elif isinstance(iv_data, dict):
            iv_rank_val = iv_data.get("iv_rank")
    iv_rank_val = _to_float_or_none(iv_rank_val)

    is_structural_divergence = False
    divergence_level = ""

    # 防止盤前(0.0) 觸發背離誤報
    if skew_percentile is not None and pcr_val_for_div is not None:
        if (
            skew_percentile > SKEW_DIVERGENCE_HIGH_PERCENTILE
            and 0.0 < pcr_val_for_div < 0.4
        ):
            is_structural_divergence = True
            divergence_level = "High Divergence"
        elif skew_percentile < SKEW_DIVERGENCE_LOW_PERCENTILE and pcr_val_for_div > 1.5:
            is_structural_divergence = True
            divergence_level = "High Divergence"
        elif dp_val > 0.0 and skew_percentile > SKEW_HIGH_DEFENSE_PERCENTILE:
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
            divergence = "⚠️ 警告：結構性情緒背離"
            if divergence_level == "High Divergence":
                action = "高度背離：避免追價買權；僅允許小倉位收租並搭配保護"
            else:
                action = "留意結構性背離：建議降槓桿、以保護性結構防禦"
    elif (
        skew_percentile is not None
        and skew_percentile > SKEW_DEFENSIVE_PERCENTILE
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
        and skew_percentile < SKEW_BULLISH_PERCENTILE
        and (
            "悲觀" in str(reddit_score)
            or "💀" in str(reddit_score)
            or "Bearish" in str(reddit_score)
        )
    ):
        divergence = "情緒背離 (散戶恐慌 vs 權利金便宜)"
        action = "考慮賣出賣權 (Cash Secured Put)"

    # 「同步」只能代表「已判定且一致」：Skew 分位或 PCR 缺失時上方結構性分支
    # 全數跳過，舊版仍印「同步」等於把「沒資料」說成「沒背離」。
    structural_inputs_missing = skew_percentile is None or pcr_val_for_div is None
    divergence_data_missing = False
    if divergence == "同步":
        # 參考級：已收盤 15m 實體 K 棒放量方向 vs Polymarket 加權看多機率。
        # Polymarket 多為目標價型合約，Yes 機率≠方向偏好，因此只作參考，不觸發
        # is_structural_divergence 與下方的結構背離 overlay。
        poly_pct = _parse_poly_bullish_pct(data.get("polymarket_summary") or poly_odds)
        is_volume_expansion = (
            rvol_effective is not None and rvol_effective >= RVOL_EXPANSION_THRESHOLD
        )
        ref_note = ""
        if is_volume_expansion and poly_pct is not None:
            if bar_body_dir > 0 and poly_pct <= POLYMARKET_BEARISH_PCT:
                ref_note = f"量價偏多 vs Polymarket 偏空 ({poly_pct:.1f}%)"
            elif bar_body_dir < 0 and poly_pct >= POLYMARKET_BULLISH_PCT:
                ref_note = f"量價偏空 vs Polymarket 偏多 ({poly_pct:.1f}%)"
        if ref_note:
            divergence = f"量價／預測市場背離（參考）：{ref_note}"
            action = (
                "Polymarket 多為目標價合約，機率≠方向偏好；僅供參考，勿單獨據以操作"
            )
            if structural_inputs_missing:
                action += "（Skew 分位／PCR 缺失，結構背離未判定）"
        elif structural_inputs_missing:
            divergence_data_missing = True
            divergence = "資料不足（Skew 分位／PCR 缺失）"
            action = ""

    skew_color = (
        "\u001b[1;35m"
        if skew_percentile is not None and skew_percentile > SKEW_DEFENSIVE_PERCENTILE
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
    if divergence_data_missing:
        divergence_color = "\u001b[1;30m"
    elif divergence.startswith("量價／預測市場背離"):
        divergence_color = "\u001b[1;33m"
    else:
        divergence_color = "\u001b[1;31m" if divergence != "同步" else "\u001b[1;32m"

    skew_val_str = f"{skew_val:.2f}%" if skew_val is not None else "--%"
    skew_note = " Call溢價" if skew_val is not None and skew_val < 0 else ""
    if skew_percentile is not None:
        skew_per_str = f"{skew_percentile:.1f}%"
    elif data.get("skew_sample_size") is not None:
        from market_analysis.sentiment.history_storage import _MIN_PERCENTILE_SAMPLES

        skew_per_str = (
            f"--%（樣本 {data.get('skew_sample_size')}/{_MIN_PERCENTILE_SAMPLES}）"
        )
    else:
        skew_per_str = "--%"

    edge_lines = [
        "```ansi",
        " Option Skew (期權偏斜)",
        f" └─ Skew (P−C): {skew_color}{skew_val_str}{skew_note}\u001b[0m (分位點: {skew_color}{skew_per_str}\u001b[0m)",
    ]
    if skew_percentile is not None and skew_percentile > SKEW_HIGH_DEFENSE_PERCENTILE:
        edge_lines.append(
            "    \u001b[1;33m⚠️ 市場下行保護需求極高，隱含避險情緒升溫。\u001b[0m"
        )

    # 動態決定是否顯示巨鯨/散戶意圖映射
    poly_summary_raw = data.get("polymarket_summary") or poly_odds
    has_market_intention = (str(poly_summary_raw).strip() != "N/A") or (
        "中性" not in reddit_score
        and "抓取失敗" not in reddit_score
        and "無法" not in reddit_score
    )

    if has_market_intention:
        poly_ansi_summary = (
            re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", str(poly_summary_raw)).split("\n")[
                0
            ]
            if poly_summary_raw
            else "N/A"
        )
        poly_ansi_summary = _truncate_with_boundary(poly_ansi_summary, 50)
        edge_lines.extend(
            [
                " 巨鯨/散戶意圖映射 (Market Intention)",
                f" ├─ Polymarket: \u001b[1;34m{poly_ansi_summary}\u001b[0m",
                f" └─ Reddit: {sentiment_color}{reddit_score}\u001b[0m",
            ]
        )

    edge_lines.extend(
        [
            " 情緒背離偵測 (Divergence Check)",
            f" └─ 狀態: {divergence_color}{divergence}\u001b[0m",
        ]
    )
    if action:
        edge_lines.append(f" └─ 建議: \u001b[1;32m{action}\u001b[0m")
    edge_lines.append("```")

    _add_ansi_field_safely(embed, "📐 情緒與邊緣偵測 (Edge Detection)", edge_lines)

    # 3. 📊 隱含波動率與預期區間 (IV Context)
    raw_em_context = data.get("expected_move_context")
    em_context = raw_em_context if isinstance(raw_em_context, dict) else {}
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

        current_iv_num = _to_float_or_none(current_iv)
        iv_rank_num = _to_float_or_none(iv_rank)
        iv_percentile_num = _to_float_or_none(iv_percentile)
        expected_move_weekly_num = _to_float_or_none(expected_move_weekly)

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

        _event_loading_outer = bool(earnings_loading or macro_loading)

        earnings_date_val = _iv_attr("earnings_date")
        earnings_tag = f" {str(earnings_date_val)[5:10]}" if earnings_date_val else ""
        if earnings_loading:
            if iv_source in ["STORED_IV", "HV_PROXY"]:
                status_tw = "⚠️ 臨近財報/快取波動率可能低估"
            elif _iv_attr("earnings_after_near_term", False):
                # 近月到期日早於財報：近月 IV 本就不含事件溢價，Contango 不代表
                # 市場忽視財報，只是期限結構量不到它。
                status_tw = (
                    f"⚠️ 臨近財報{earnings_tag}（晚於近月到期，期限結構未含事件溢價）"
                )
            else:
                status_tw = f"⚠️ 臨近財報{earnings_tag}（近月 IV 含事件溢價）"
        elif macro_loading:
            status_tw = macro_iv_status_text(
                iv_source,
                bool(_iv_attr("event_loading_applied", False)),
                omit_loading_note=True,  # 值行已揭露 ×1.4，狀態不重複
            )

        iv_status_str = f"狀態: {status_tw}"

        if (
            iv_source == "UNAVAILABLE"
            or current_iv_num is None
            or (is_premarket and current_iv_num == 0.0)
        ):
            iv_lines = [
                "```ansi",
                " Implied Volatility (IV)",
                " └─ 值: \u001b[1;30m--%\u001b[0m (數據未更新 / 降級模式)",
                " IV Rank / IV Percentile",
                " └─ IV Rank: \u001b[1;30m--%\u001b[0m | IV Percentile: \u001b[1;30m--%\u001b[0m (狀態: 待開盤)",
                " Expected Move (預期區間)",
                " └─ 7 日 1σ: \u001b[1;30m--\u001b[0m (開盤後更新)",
                "```",
            ]
        else:
            if is_premarket:
                vol_title = (
                    "Historical Volatility (HV, 30D)"
                    if iv_source == "HV_PROXY"
                    else "Implied Volatility (IV)"
                )
                _after_close = _phase == "盤後" and iv_source != "HV_PROXY"
                if iv_source == "HV_PROXY":
                    vol_note = "30D 歷史實現波動率代理（期權未開市/IV 不可用）"
                elif _after_close:
                    vol_note = "當日盤中 IV 快取（已收盤）"
                else:
                    vol_note = "前日收盤 IV / SQLite 快取（期權未開市）"
                if iv_source == "HV_PROXY":
                    em_note = "基於 30D HV 代理估算"
                elif _after_close:
                    em_note = "基於當日快取 IV 計算"
                else:
                    em_note = "基於前日收盤 IV 計算"
            else:
                if iv_source == "HV_PROXY":
                    vol_title = "Historical Volatility (HV, 30D)"
                    vol_note = "30D 歷史實現波動率代理（即時 IV 不可用）"
                    em_note = "基於 30D HV 代理估算"
                elif iv_source == "LIVE_IV":
                    vol_title = "Implied Volatility (IV) 🟢即時"
                    _ie = _iv_attr("current_iv_expiry")
                    _id = _iv_attr("current_iv_dte")
                    if (
                        _iv_attr("iv_scale_corrected", False)
                        and _ie
                        and _id is not None
                    ):
                        vol_note = (
                            f"跨式反推 {str(_ie)[5:]} (DTE {_id})，最近到期 IV 尺度異常"
                        )
                    elif _ie and _id is not None:
                        vol_note = f"最近到期 {str(_ie)[5:]} (DTE {_id}) ±20% OI 加權"
                        if int(_id) <= 2:
                            vol_note += "，含結算 Gamma 偏高"
                    else:
                        vol_note = "最近到期 ±20% OI 加權"
                    em_note = "基於當前 IV 計算"
                else:
                    vol_title = "Implied Volatility (IV)"
                    vol_note = (
                        "最近到期 ±20% OI 加權"
                        if iv_source != "STORED_IV"
                        else "SQLite 快取 IV（非即時）"
                    )
                    em_note = (
                        "基於當前 IV 計算"
                        if iv_source != "STORED_IV"
                        else "基於快取 IV 計算"
                    )

            iv_val_str = (
                f"{current_iv_num * 100:.1f}%" if current_iv_num is not None else "--%"
            )
            iv_rank_str = f"{iv_rank_num:.1f}%" if iv_rank_num is not None else "--%"
            iv_per_str = (
                f"{iv_percentile_num:.1f}%" if iv_percentile_num is not None else "--%"
            )

            _hist_cnt = int(_to_float(_iv_attr("iv_history_count", 0), 0.0))
            _hist_req = int(_to_float(_iv_attr("iv_history_required", 60), 60.0))
            if iv_rank_num is None and _hist_cnt > 0:
                iv_status_str = f"樣本累積中 {_hist_cnt}/{_hist_req} 日，10/01 母體重置；{status_tw}"
            if _iv_attr("event_loading_applied", False) and current_iv_num:
                iv_val_str = (
                    f"{current_iv_num / 1.4 * 100:.1f}% ×1.4 事件加載 = {iv_val_str}"
                )
            iv_lines = [
                "```ansi",
                vol_title,
                f" └─ 值: {iv_val_str} ({vol_note})",
                " IV Rank / IV Percentile",
                f" └─ IV Rank: {iv_rank_str} | IV Percentile: {iv_per_str} ({iv_status_str})",
            ]
            _hv20 = _to_float_or_none(_iv_attr("hv_20"))
            _st_iv_hv = _to_float_or_none(_iv_attr("straddle_implied_iv"))
            if iv_rank_num is None and _hv20:
                if iv_source == "HV_PROXY" or _iv_attr("event_loading_applied", False):
                    # 值由 HV 推得、或被 ×1.4 事件加載，IV/HV20 已失去意義，不輸出。
                    if _st_iv_hv:
                        iv_lines.append(
                            f" └─ 跨式 IV/HV20: {_st_iv_hv * 100:.1f}% / "
                            f"{_hv20 * 100:.1f}% = {_st_iv_hv / _hv20:.2f}x（參考）"
                        )
                elif current_iv_num:
                    iv_lines.append(
                        f" └─ IV/HV20: {current_iv_num * 100:.1f}% / {_hv20 * 100:.1f}% = "
                        f"{current_iv_num / _hv20:.2f}x（IVR 累積期參考，>1 偏貴）"
                    )

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

            iv_lines.append(" IV 期限結構 (Term Structure)")
            if iv_term_status and iv_term_ratio is not None:
                try:
                    ratio_val = float(iv_term_ratio)
                    status_str = str(iv_term_status)
                    if status_str == "Backwardation":
                        term_prefix = "⚠️ 逆價差 (Backwardation)"
                    elif status_str == "Contango":
                        term_prefix = "✅ 正價差 (Contango)"
                    else:
                        # 0.95~1.05 為刻意的死區：近遠月 IV 大致持平，方向不具意義
                        term_prefix = "⚖️ 持平 (Flat, 0.95~1.05)"
                    _tn = _iv_attr("term_near_expiry")
                    _tf = _iv_attr("term_far_expiry")
                    _term_lbl = (
                        f"近 {str(_tn)[5:]}／遠 {str(_tf)[5:]} 比"
                        if _tn and _tf
                        else "近遠月比"
                    )
                    iv_lines.append(f" └─ {term_prefix} ({_term_lbl}: {ratio_val:.2f})")
                except (ValueError, TypeError):
                    iv_lines.append(" └─ --")
            else:
                iv_lines.append(" └─ --")

            iv_lines.append(" Expected Move (預期區間)")

            em_reference = _to_float_or_none(em_context.get("reference_price"))
            safe_em_weekly = expected_move_weekly_num

            if (
                em_reference is not None
                and em_reference > 0
                and safe_em_weekly is not None
            ):
                em_ref_rounded = round(em_reference, 2)
                em_weekly_rounded = round(safe_em_weekly, 2)
                em_low_calc = round(em_ref_rounded - em_weekly_rounded, 2)
                em_high_calc = round(em_ref_rounded + em_weekly_rounded, 2)
                _ref_label = str(em_context.get("reference_label") or "前收")
                expected_move_weekly_str = (
                    f"{_ref_label} ${em_ref_rounded:.2f} ±${em_weekly_rounded:.2f} "
                    f"(${em_low_calc:.2f} ~ ${em_high_calc:.2f})"
                )
            else:
                expected_move_weekly_str = (
                    f"±${round(safe_em_weekly, 2):.2f}"
                    if safe_em_weekly is not None
                    else "--"
                )
            straddle_iv_num = _to_float_or_none(_iv_attr("straddle_implied_iv"))
            if straddle_iv_num is not None and straddle_iv_num > 0:
                # EM 優先採跨式定價，與上方 IV 並非同源；並列揭露反推 IV，避免
                # 使用者拿「IV × √t」去驗算 EM 時誤以為兩者數量級斷層。
                em_note = f"跨式定價，隱含 IV {straddle_iv_num * 100:.1f}%"
            if _iv_attr("iv_scale_corrected", False):
                iv_lines.append(
                    " ⚠️ 原始 IV 與跨式定價相差 >4 倍（資料源尺度錯誤），已改用跨式反推值"
                )
            _st_exp = _iv_attr("straddle_expiry")
            _st_dte = _iv_attr("straddle_dte")
            em_title = (
                f"7 日 1σ（{str(_st_exp)[5:]} 跨式 ×√(7/{_st_dte})）"
                if _st_exp and _st_dte
                else "7 日 1σ"
            )
            iv_lines.append(f" └─ {em_title}: {expected_move_weekly_str} ({em_note})")

            catalysts = data.get("catalysts")
            if catalysts:
                iv_lines.append(" 事件日曆防護 (Catalyst Calendar)")
                # 事件清單含發布後冷卻期 (tte_hours < 0)；已公布的不得印成「僅剩 -0.1 天」，
                # 合併為一行置於未到事件之後，且不佔 3 筆上限。
                released_names: List[str] = []
                # (tier, tte 排序鍵, 時間戳, 事件名, 日期, 距今天數)
                upcoming: List[tuple[int, float, str, str, str, float]] = []
                _key_events = ("FOMC", "CPI", "PCE", "非農", "利率決議", "GDP")
                for cat in catalysts:
                    if hasattr(cat, "date"):
                        date_str = cat.date
                        days = _to_float(getattr(cat, "days_to_earnings", None), 0.0)
                        if days < 0:
                            released_names.append(f"財報 ({date_str[5:]})")
                            continue
                        upcoming.append((0, days, "", "財報", date_str, days))
                    elif hasattr(cat, "time"):
                        date_str = cat.time[:10]
                        tte_hours = _to_float_or_none(getattr(cat, "tte_hours", None))
                        # Ensure the event name is not excessively long
                        event_name = (
                            cat.event
                            if len(cat.event) <= 20
                            else cat.event[:17] + "..."
                        )
                        if tte_hours is not None and tte_hours < 0:
                            released_names.append(event_name)
                            continue
                        days = round((tte_hours or 0.0) / 24.0, 1)
                        is_key = any(k in str(cat.event).upper() for k in _key_events)
                        upcoming.append(
                            (
                                1 if is_key else 2,
                                tte_hours or 0.0,
                                str(cat.time),
                                event_name,
                                date_str,
                                days,
                            )
                        )
                # 同一時間戳的 CPI 家族（核心／月增／年增）合併成一行
                merged: List[tuple[int, float, str, str, str, float]] = []
                cpi_groups: dict[str, int] = {}
                for ev_item in upcoming:
                    if ev_item[0] == 1 and "CPI" in ev_item[3].upper():
                        idx = cpi_groups.get(ev_item[2])
                        if idx is not None:
                            t, o, ts, nm, d, dy = merged[idx]
                            cnt = int(nm.split(" 等 ")[1].rstrip(" 項")) + 1
                            merged[idx] = (t, o, ts, f"CPI 等 {cnt} 項", d, dy)
                            continue
                        cpi_groups[ev_item[2]] = len(merged)
                        merged.append(
                            (
                                ev_item[0],
                                ev_item[1],
                                ev_item[2],
                                "CPI 等 1 項",
                                ev_item[4],
                                ev_item[5],
                            )
                        )
                    else:
                        merged.append(ev_item)
                merged = [
                    (t, o, ts, "CPI" if nm == "CPI 等 1 項" else nm, d, dy)
                    for t, o, ts, nm, d, dy in merged
                ]
                # 依重要性取前 3 筆，再依時間順序顯示
                chosen = sorted(
                    sorted(merged, key=lambda x: (x[0], x[1]))[:3], key=lambda x: x[1]
                )
                upcoming_omitted = len(merged) > 3
                for tier, _o, _ts, name, date_str, days in chosen:
                    if name == "財報":
                        iv_lines.append(
                            f" └─ \u001b[1;33m⚠️ 距離財報 ({date_str[5:]}) 僅剩 {days:.1f} 天，嚴禁雙賣策略\u001b[0m"
                        )
                    else:
                        iv_lines.append(
                            f" └─ \u001b[1;33m⚠️ 距離 {name} ({date_str[5:]}) 僅剩 {days:.1f} 天，留意波動擴大\u001b[0m"
                        )
                if upcoming_omitted:
                    iv_lines.append(" └─ \u001b[1;30m...及其他事件 (已省略)\u001b[0m")
                if released_names:
                    iv_lines.append(
                        " └─ \u001b[1;32m✅ 已公布："
                        + "、".join(dict.fromkeys(released_names))
                        + "（市場消化中）\u001b[0m"
                    )

            iv_lines.append("```")

        _add_ansi_field_safely(embed, "📊 隱含波動率與預期區間 (IV Context)", iv_lines)

    # 釘住成立時的釘住履約價（GEX 區塊內賦值，供 Max Pain 指引取用）。
    _pin_strike: Optional[float] = None
    # 供 Kelly「賣方前提」與日內轉折使用（GEX 區塊內賦值；None 表示未取得）。
    _put_wall_weak_outer: Optional[bool] = None  # 助跌或紙牆（None=未取得）
    _call_wall_hug_outer: Optional[float] = None  # 貼牆時的距離 %
    _local_short_gamma_outer: Optional[bool] = None
    _gamma_flip_outer: Optional[float] = None
    _gex_price_outer: Optional[float] = None  # GEX 區塊採用的現價（與 Flip 同價）

    # 3.5 🧲 Gamma 曝險分布 (GEX Profile)
    gex_data = data.get("gex_profile_data")
    if (
        isinstance(gex_data, dict)
        and isinstance(gex_data.get("gex_profile"), dict)
        and gex_data.get("gex_profile")
    ):
        try:
            gex_prof = gex_data["gex_profile"]
            from market_time import ny_tz as _gex_tz

            _gex_exp = _resolve_gex_expiry(gex_data, data, datetime.now(_gex_tz))
            _gex_exp_label = f"{_gex_exp[5:]} 到期" if _gex_exp else "最近到期"
            strike_keys: list[float] = []
            # 履約價鍵可能是 "370" 或 "370.0"；一律以 float 正規化查表，避免整數鍵
            # 被查成 0 而顯示假的「+0K 紙牆」。
            _prof_by_strike: dict[float, float] = {}
            for k in gex_prof.keys():
                try:
                    f = float(k)
                    if math.isfinite(f):
                        strike_keys.append(f)
                        _prof_by_strike[f] = _to_float(gex_prof[k], 0.0)
                except (ValueError, TypeError):
                    continue
            strike_keys.sort()
            if strike_keys:
                effective_c_val = (
                    c_val if c_val > 0.0 else _to_float(gex_data.get("spot"), 0.0)
                )
                if effective_c_val <= 0.0 and strike_keys:
                    effective_c_val = strike_keys[
                        len(strike_keys) // 2
                    ]  # Fallback to middle strike

                closest_idx = min(
                    range(len(strike_keys)),
                    key=lambda i: abs(strike_keys[i] - effective_c_val),
                )
                start_idx = max(0, closest_idx - 3)
                end_idx = min(len(strike_keys), closest_idx + 4)
                display_strikes = strike_keys[start_idx:end_idx]

                def _safe_gex(k_val: float) -> float:
                    return _prof_by_strike.get(float(k_val), 0.0)

                is_gex_empty = all(abs(_safe_gex(k)) == 0.0 for k in display_strikes)
                gex_putwall = gex_data.get("put_wall")
                has_putwall = False
                try:
                    if gex_putwall and float(gex_putwall) > 0:
                        has_putwall = True
                except (ValueError, TypeError):
                    pass

                if is_gex_empty and has_putwall:
                    gex_lines = [
                        "```ansi",
                        " ⚠️ [GEX 鏈盤前未刷新] 期權鏈造市商曝險尚未更新",
                        f" 🛡️ 靜態 GEX PutWall (快取底牆): ${_to_float(gex_putwall):.2f}",
                        "```",
                    ]
                else:
                    max_abs_gex = max([abs(_safe_gex(k)) for k in display_strikes])
                    max_abs_gex = max(max_abs_gex, 1.0)

                    gex_lines = [
                        "```ansi",
                        " ┌─ 履約價(Strike) ─ 曝險熱力圖 (現價±3檔，非全鏈) ─ [每 1% 避險名目]",
                    ]
                    _bto_by_strike: dict[float, int] = {}
                    if _gex_exp:
                        for _u in data.get("uoa") or []:
                            if not isinstance(_u, dict):
                                continue
                            if str(
                                _u.get("expiry", "")
                            ) != _gex_exp or "BTO" not in str(_u.get("action", "")):
                                continue
                            _uk = _to_float(_u.get("strike"), 0.0)
                            if _uk in display_strikes:
                                _bto_by_strike[_uk] = _bto_by_strike.get(_uk, 0) + int(
                                    _to_float(_u.get("volume"), 0.0)
                                )
                    for i, k in enumerate(reversed(display_strikes)):
                        v = _safe_gex(k)
                        bars = int((abs(v) / max_abs_gex) * 10)
                        bar_str = "█" * bars + "░" * (10 - bars)
                        if v > 0:
                            color_prefix = "\u001b[1;32m"
                        elif v < 0:
                            color_prefix = "\u001b[1;31m"
                        else:
                            color_prefix = "\u001b[1;30m"

                        # 只標最接近現價的那一檔：高價股的履約價間距相對小，1% 容差內常
                        # 落進兩檔以上，會同時出現多個 📍。1% 容差保留，現價遠離所有
                        # 履約價時不標。
                        spot_marker = (
                            "📍"
                            if k == strike_keys[closest_idx]
                            and abs(k - effective_c_val) < (effective_c_val * 0.01)
                            else "  "
                        )
                        formatted_val = _fmt_gex_notional(v)
                        prefix = " ├─" if i < len(display_strikes) - 1 else " └─"
                        _bto_n = _bto_by_strike.get(k, 0)
                        _bto_tag = f" ⚠買{_bto_n / 1000:.1f}k" if _bto_n > 0 else ""
                        gex_lines.append(
                            f"{prefix} {spot_marker}{k:>7.2f} | {color_prefix}{bar_str}\u001b[0m | {formatted_val:>9}{_bto_tag}"
                        )
                    if _bto_by_strike:
                        gex_lines.append(" ⚠買＝今日買入未計入前日OI，實際Γ可能偏低")

                    def _append_tree_block(
                        lines: List[str], header: str, items: List[str]
                    ) -> None:
                        lines.append("")
                        lines.append(f" ── {header} ──")
                        for i, item in enumerate(items):
                            prefix = " ├─ " if i < len(items) - 1 else " └─ "
                            lines.append(prefix + item)

                    put_wall_float = _to_float(gex_putwall, 0.0) if has_putwall else 0.0
                    put_reanchored = False
                    # 下檔／上檔兩個區塊延後到上檔空間算完才輸出：「進場甜蜜點」與盈虧比
                    # 必須和 CallWall 空間交叉判定（docs/strategies/06 §5）。
                    put_block_items: Optional[List[str]] = None
                    call_block_items: Optional[List[str]] = None
                    sweet_spot_idx: Optional[int] = None
                    sweet_spot_prefix = ""
                    put_stop_for_rr = 0.0
                    # 停損距離過窄時的合格下界 (2.5×ATR₁₅ₘ，佔現價比例)；盈虧比須
                    # 另以此重算，否則過窄停損會把比值灌水成 ✅。
                    put_min_stop_frac: Optional[float] = None
                    # TOO_TIGHT（含降級、min_pct 為 None）時盈虧比須標示比值虛高。
                    put_too_tight = False
                    # PutWall 為助跌區或紙牆（淨 GEX 不構成支撐）。
                    put_wall_weak = False
                    alt_stop_for_rr = 0.0
                    alt_anchor_for_rr = 0.0
                    upside_room_pct: Optional[float] = None
                    upside_threshold_pct: Optional[float] = None

                    if (
                        effective_c_val > 0
                        and isinstance(gex_data, dict)
                        and isinstance(gex_data.get("gex_profile"), dict)
                    ):
                        if (
                            not has_putwall
                            or put_wall_float >= effective_c_val
                            or math.isclose(
                                put_wall_float, effective_c_val, abs_tol=1e-4
                            )
                        ):
                            dyn_supp, _, _, _ = _scan_gex_walls(
                                symbol, gex_data, spot=effective_c_val
                            )
                            if dyn_supp > 0:
                                put_wall_float = dyn_supp
                                has_putwall = True
                                put_reanchored = True

                    if has_putwall:
                        put_reanchor_note = " (動態重錨)" if put_reanchored else ""
                        put_items = [
                            f"PutWall: ${put_wall_float:.2f}{put_reanchor_note}"
                        ]
                        # edge 的 PutWall 取「Put 端 Gamma 最大」的履約價，熱力圖畫的
                        # 卻是淨 GEX；兩者不同源。PutWall 處淨 GEX 為負＝助跌區，介於
                        # 0 與薄牆門檻＝紙牆，都不是支撐——於 PutWall 行內嵌據實揭露，
                        # 並另列現價下方淨 GEX 最大的正支撐。僅影響呈現，引擎閘門仍以
                        # edge PutWall 為準（docs/microstructure/02 §7）。
                        put_wall_net = _safe_gex(put_wall_float)
                        _pw_thin = thin_wall_threshold(gex_data.get("adv_dollar_20d"))
                        if put_wall_net < 0:
                            put_wall_kind = "助跌區"
                            put_items[0] += (
                                f"〔淨GEX {_fmt_gex_notional(put_wall_net)}，實為助跌區〕"
                            )
                        elif put_wall_net < _pw_thin:
                            put_wall_kind = "紙牆"
                            put_items[0] += (
                                f"〔紙牆：淨GEX {_fmt_gex_notional(put_wall_net)}"
                                f" < 門檻 {_fmt_gex_notional(_pw_thin)}〕"
                            )
                        else:
                            put_wall_kind = ""
                        put_wall_weak = bool(put_wall_kind)
                        _put_wall_weak_outer = put_wall_weak
                        # PutWall 落在淨 GEX 助跌區時，供下方結構停損並列「以淨
                        # GEX 支撐為錨」的參考停損；僅呈現，閘門不變。
                        alt_net_supp: float = 0.0
                        if effective_c_val > 0:
                            net_supp, _, _, _ = _scan_gex_walls(
                                symbol, gex_data, spot=effective_c_val
                            )
                            if (
                                net_supp > 0
                                and not math.isclose(
                                    net_supp, put_wall_float, abs_tol=1e-4
                                )
                                and (net_supp > put_wall_float or put_wall_weak)
                            ):
                                net_supp_pct = (
                                    (effective_c_val - net_supp) / effective_c_val * 100
                                )
                                put_items.append(
                                    f"淨 GEX 最大支撐: ${net_supp:.2f} (↓{net_supp_pct:.2f}%)"
                                )
                                if put_wall_weak and net_supp < effective_c_val:
                                    alt_net_supp = net_supp
                        _pw_atr_15m: float = 0.0

                        if effective_c_val > 0:
                            put_buffer_pct = (
                                (effective_c_val - put_wall_float)
                                / effective_c_val
                                * 100
                            )
                            # 下行緩衝判定自固定 5% 升級為 room_threshold.py
                            # 公式 B。量的是**停損距離**（現價到 PutWall −
                            # 0.5×ATR₁₅ₘ，即引擎軌道一真正會掛的那條線）：
                            #   過窄 (< 2.5×ATR₁₅ₘ)：停損落在日內雜訊帶內，
                            #        會先被掃穿再回頭，即 Liquidity Sweep。
                            #   過寬 (> 絕對 8%)：絕對風險上限兜底。
                            # 「PutWall 已高於現價」的資料異常分支優先於三態判定
                            # 保留——那是資料問題，不是緩衝問題。
                            _pw_atr_1d = _to_float(data.get("atr_14"), 0.0)
                            if _pw_atr_1d <= 0.0:
                                _pw_atr_1d = _to_float(data.get("atr_1d"), 0.0)
                            _pw_atr_15m = resolve_atr_15m(
                                _to_float(data.get("atr_15m"), 0.0), _pw_atr_1d
                            )
                            put_buffer_eval = evaluate_wall_buffer(
                                effective_c_val,
                                put_wall_float,
                                _pw_atr_15m,
                                _pw_atr_1d,
                                profile="RIGHT",
                            )
                            is_put_zero = math.isclose(
                                put_buffer_pct, 0.0, abs_tol=1e-4
                            )
                            put_arrow = (
                                "↓" if (put_buffer_pct >= 0 or is_put_zero) else "↑"
                            )
                            # 已把 degrade_reason 內嵌進文案的分支不再重複輸出
                            put_degrade_inline = False
                            # 判定量的是停損距離（牆 − 0.5×ATR₁₅ₘ），不是牆距；兩者
                            # 並列，避免「7.46% 被判 > 8%」這種看似比較式寫反的誤讀。
                            stop_dist_tag = (
                                f"｜停損距離 {put_buffer_eval.buffer_pct * 100:.2f}%"
                                if put_buffer_eval.min_pct is not None
                                else ""
                            )
                            if put_buffer_pct < 0 and not is_put_zero:
                                put_space_flag = " ⚠️ [數據異常：PutWall已高於現價]"
                                put_items.append(
                                    f"距現價空間 (下行緩衝): {put_arrow}"
                                    f"{abs(put_buffer_pct):.2f}%{put_space_flag}"
                                )
                            elif put_buffer_eval.state == "SWEET_SPOT":
                                sweet_spot_prefix = (
                                    f"距現價空間 (下行緩衝): {put_arrow}"
                                    f"{abs(put_buffer_pct):.2f}%{stop_dist_tag}"
                                )
                                sweet_spot_idx = len(put_items)
                                put_items.append(f"{sweet_spot_prefix} ✅ 進場甜蜜點")
                            elif put_buffer_eval.state == "TOO_TIGHT":
                                # 邊界不可得時不得印出「< 2.5×ATR₁₅ₘ = 下界」這種
                                # 有公式卻無數值的誤導文案——該情境走的是降級的
                                # 固定 5% 判定，應改為據實揭露降級原因。
                                put_too_tight = True
                                if put_buffer_eval.min_pct is not None:
                                    put_min_stop_frac = put_buffer_eval.min_pct
                                    _note = (
                                        f"❌ 過窄 (< 2.5×ATR₁₅ₘ = "
                                        f"{put_buffer_eval.min_pct * 100:.2f}%)，易遭掃損"
                                    )
                                else:
                                    _note = (
                                        f"❌ 緩衝過窄，易遭掃損"
                                        f"（⚠ {put_buffer_eval.degrade_reason}）"
                                    )
                                    put_degrade_inline = True
                                put_items.append(
                                    f"距現價空間 (下行緩衝): {put_arrow}"
                                    f"{abs(put_buffer_pct):.2f}%{stop_dist_tag}\n │  {_note}"
                                )
                            else:
                                if put_buffer_eval.max_pct is not None:
                                    _note = (
                                        f"⚠ 停損距離過寬 (> 絕對上限 "
                                        f"{put_buffer_eval.max_pct * 100:.2f}%)，停損距離過遠"
                                    )
                                else:
                                    _note = (
                                        f"⚠ 緩衝過寬，停損距離過遠"
                                        f"（⚠ {put_buffer_eval.degrade_reason}）"
                                    )
                                    put_degrade_inline = True
                                put_items.append(
                                    f"距現價空間 (下行緩衝): {put_arrow}"
                                    f"{abs(put_buffer_pct):.2f}%{stop_dist_tag}\n │  {_note}"
                                )

                            # 降級一律揭露，不限於判定不利時：門檻／邊界本身是
                            # 降級值時，即使結論是「甜蜜點」使用者同樣有權知道那
                            # 是在缺數據下算出來的（docs/strategies/06 §5 的強制
                            # 揭露義務）。
                            if (
                                put_buffer_eval.degrade_reason
                                and not put_degrade_inline
                            ):
                                put_items[-1] += (
                                    f"\n │  ⚠ {put_buffer_eval.degrade_reason}"
                                )

                        atr_15m_val = (
                            _pw_atr_15m
                            if _pw_atr_15m > 0
                            else _to_float(data.get("atr_15m"), 0.0)
                        )
                        if atr_15m_val > 0 and effective_c_val > 0:
                            # 與上方「停損距離」同一條線：引擎軌道一的結構停損
                            # (PutWall − 0.5×ATR₁₅ₘ)。不得改回自選心跳買點緩衝的
                            # 1.5×（ANTI_WASHOUT_ATR_MULT）——那不是停損，並列時
                            # 會出現停損價與停損距離對不上的矛盾。
                            structural_stop = compute_reference_stop(
                                effective_c_val, put_wall_float, atr_15m_val, "LONG"
                            )
                            fallback_marker = ""
                            if (
                                put_wall_float
                                - _ROOM_STOP_ATR_15M_MULTIPLIER * atr_15m_val
                                >= effective_c_val
                            ):
                                fallback_marker = (
                                    " [PutWall異常降級：改用現價−"
                                    f"{_ROOM_STOP_FALLBACK_ATR_15M_MULTIPLIER:.0f}×ATR₁₅ₘ]"
                                )
                            stop_dist_pct = (
                                (effective_c_val - structural_stop)
                                / effective_c_val
                                * 100
                            )
                            put_stop_for_rr = structural_stop
                            stop_item = (
                                f"結構停損 (PutWall−0.5×ATR₁₅ₘ): "
                                f"${structural_stop:.2f} (↓{stop_dist_pct:.2f}%)"
                                f"{fallback_marker}"
                            )
                            # docs/microstructure/02 §7：PutWall 定義統一為淨 GEX
                            # 前須先經 calibration 比對，引擎閘門與停損一律仍以
                            # edge PutWall 為準。PutWall 落在助跌區時呈現上改以
                            # 淨 GEX 最大支撐為錨的停損作主行，閘門停損降為次行。
                            if alt_net_supp > 0:
                                alt_stop = compute_reference_stop(
                                    effective_c_val, alt_net_supp, atr_15m_val, "LONG"
                                )
                                alt_stop_pct = (
                                    (effective_c_val - alt_stop) / effective_c_val * 100
                                )
                                alt_stop_for_rr = alt_stop
                                alt_anchor_for_rr = alt_net_supp
                                stop_item = (
                                    f"參考停損 (淨 GEX 支撐 ${alt_net_supp:.2f}−0.5×ATR₁₅ₘ): "
                                    f"${alt_stop:.2f} (↓{alt_stop_pct:.2f}%)"
                                    f"\n │  引擎閘門停損 (PutWall−0.5×ATR₁₅ₘ): "
                                    f"${structural_stop:.2f} (↓{stop_dist_pct:.2f}%)"
                                    f"{fallback_marker}"
                                    f"\n │  ⚠ PutWall 為{put_wall_kind}，閘門仍以 PutWall 為準"
                                )
                            # docs/strategies/06 §5：日線噪音帶參考停損，僅並列於
                            # 停損行尾，閘門不動，待 calibration 停損墊片比較。
                            # ATR₁D 先 atr_1d 後 atr_14，排除 0.01 佔位值；PutWall
                            # 高於現價、降級分支、助跌區（PutWall 已宣告不可靠）不輸出。
                            _daily_atr = next(
                                (
                                    v
                                    for v in (
                                        _to_float(data.get("atr_1d"), 0.0),
                                        _to_float(data.get("atr_14"), 0.0),
                                    )
                                    if is_valid_daily_atr(v)
                                ),
                                0.0,
                            )
                            if (
                                _daily_atr > 0
                                and put_wall_float < effective_c_val
                                and not fallback_marker
                                and alt_net_supp <= 0
                            ):
                                _daily_stop = (
                                    put_wall_float
                                    - _DAILY_NOISE_STOP_ATR_1D_MULT * _daily_atr
                                )
                                if _daily_stop < effective_c_val:
                                    _daily_pct = (
                                        (effective_c_val - _daily_stop)
                                        / effective_c_val
                                        * 100
                                    )
                                    stop_item += f" ｜日線參考 ${_daily_stop:.2f} (↓{_daily_pct:.2f}%)"
                            put_items.append(stop_item)

                        if effective_c_val > 0:
                            sto_strikes = data.get("sto_physical_cap_strikes") or []
                            put_sto_candidates = [
                                s
                                for s in sto_strikes
                                if isinstance(s, dict)
                                and str(s.get("type", "")).upper() == "PUT"
                                # 價內 STO Put 不是地板；跨式／價差腿非方向性
                                and _to_float(s.get("strike"), 0.0) < effective_c_val
                                and _sto_cap_eligible(s)
                            ]
                            if put_sto_candidates:
                                best_sto = max(
                                    put_sto_candidates,
                                    key=lambda s: _to_float(
                                        s.get("notional_value"),
                                        _to_float(s.get("volume"), 0.0),
                                    ),
                                )
                                sto_strike = _to_float(best_sto.get("strike"), 0.0)
                                sto_volume = int(_to_float(best_sto.get("volume"), 0.0))
                                sto_notional = _to_float(
                                    best_sto.get("notional_value"), 0.0
                                )
                                if sto_strike > 0 and sto_volume > 0:
                                    sto_divergence_pct = (
                                        abs(sto_strike - put_wall_float)
                                        / effective_c_val
                                        * 100
                                    )
                                    if (
                                        sto_divergence_pct
                                        > _GEX_STO_DIVERGENCE_THRESHOLD_PCT
                                    ):
                                        if sto_notional >= 1_000_000:
                                            notional_str = (
                                                f"${sto_notional / 1_000_000:.2f}M"
                                            )
                                        elif sto_notional > 0:
                                            notional_str = (
                                                f"${sto_notional / 1_000:.1f}k"
                                            )
                                        else:
                                            notional_str = ""
                                        notional_suffix = (
                                            f", 權利金 {notional_str}"
                                            if notional_str
                                            else ""
                                        )
                                        _sto_exp = str(best_sto.get("expiry", ""))
                                        _sto_dte = ""
                                        try:
                                            _sto_dte = f"(DTE{(datetime.strptime(_sto_exp, '%Y-%m-%d').date() - datetime.now(_gex_tz).date()).days})"
                                        except ValueError:
                                            pass
                                        _in_table = any(
                                            isinstance(_u, dict)
                                            and str(_u.get("expiry", "")) == _sto_exp
                                            and _to_float(_u.get("strike"), 0.0)
                                            == sto_strike
                                            and str(_u.get("type", "")).upper() == "PUT"
                                            for _u in (data.get("uoa") or [])
                                        )
                                        _sto_tags = (
                                            "" if _in_table else "〔全鏈掃描，表外〕"
                                        ) + (
                                            "〔跨到期〕"
                                            if _gex_exp and _sto_exp != _gex_exp
                                            else ""
                                        )
                                        if best_sto.get("spread_credit"):
                                            _sto_tags += "〔貸方價差〕"
                                        put_items.append(
                                            f"⚠️ 大單 ${sto_strike:.2f}"
                                            f"{' ' + _sto_exp[5:] + _sto_dte if _sto_exp else ''}"
                                            f" STO PUT {sto_volume:,}口{notional_suffix}"
                                            f" 與 PutWall 分歧{_sto_tags}"
                                        )
                            # 被跌破的 STO Put：賣方已轉虧，承接失效（啟發式）
                            _breach = _sto_best(
                                [
                                    s
                                    for s in sto_strikes
                                    if isinstance(s, dict)
                                    and str(s.get("type", "")).upper() == "PUT"
                                    and effective_c_val
                                    < _to_float(s.get("strike"), 0.0)
                                    <= effective_c_val
                                    * (1 + _STO_PUT_BREACH_MAX_PCT / 100)
                                    and _sto_cap_eligible(s)
                                ]
                            )
                            if _breach is not None:
                                put_items.append(
                                    f"⚠️ STO PUT ${_to_float(_breach.get('strike'), 0.0):.2f}"
                                    f"{_sto_exp_dte(str(_breach.get('expiry', '')), datetime.now(_gex_tz).date())}"
                                    f" {int(_to_float(_breach.get('volume'), 0.0)):,}口"
                                    "已跌破(價內)：賣方轉虧，承接失效，續跌恐回補/指派拋壓（啟發式）"
                                )

                        put_block_items = put_items

                    gex_callwall = gex_data.get("call_wall")
                    has_callwall = False
                    call_reanchored = False
                    try:
                        if gex_callwall and float(gex_callwall) > 0:
                            has_callwall = True
                    except (ValueError, TypeError):
                        pass

                    call_wall_float = (
                        _to_float(gex_callwall, 0.0) if has_callwall else 0.0
                    )

                    if (
                        effective_c_val > 0
                        and isinstance(gex_data, dict)
                        and isinstance(gex_data.get("gex_profile"), dict)
                    ):
                        if (
                            not has_callwall
                            or call_wall_float <= effective_c_val
                            or math.isclose(
                                call_wall_float, effective_c_val, abs_tol=1e-4
                            )
                        ):
                            dyn_res, _ = _scan_resistance_wall_above_spot(
                                symbol, gex_data, effective_c_val
                            )
                            if dyn_res > 0:
                                call_wall_float = dyn_res
                                has_callwall = True
                                call_reanchored = True

                    call_vacuum = (
                        has_callwall
                        and effective_c_val > 0
                        and call_wall_float < effective_c_val
                        and not math.isclose(
                            call_wall_float, effective_c_val, abs_tol=1e-4
                        )
                    )
                    if call_vacuum:
                        # 重錨也找不到現價上方的正 GEX 牆：上方全是負 Gamma。舊牆
                        # 已被突破，不再是壓力；照印舊牆加「數據異常」只會誤導。
                        call_block_items = [
                            "CallWall: -- 上方無正 Gamma 牆（負 Gamma 真空，助漲助跌）",
                            f"原 CallWall ${call_wall_float:.2f} 已被突破，不再構成壓力",
                        ]
                    elif has_callwall and effective_c_val > 0:
                        call_wall_depth = _safe_gex(call_wall_float)
                        call_wall_dist_pct = (
                            (call_wall_float - effective_c_val) / effective_c_val * 100
                        )
                        # 上檔空間門檻自固定 5% 升級為動態自適應波動率門檻
                        # (room_threshold.py 公式 A)：由該標的「下方實際要冒多少
                        # 風險」反推「上方要留多少空間」，使 2.2:1 盈虧比成為
                        # 結構性保證。資料缺失時退回 3.5% 絕對底線，並在旗標
                        # 下方獨立一行揭露降級原因（使用者有權知道看到的門檻
                        # 不是完整推導出來的）。
                        _cw_atr_1d = _to_float(data.get("atr_14"), 0.0)
                        if _cw_atr_1d <= 0.0:
                            _cw_atr_1d = _to_float(data.get("atr_1d"), 0.0)
                        _cw_atr_15m = resolve_atr_15m(
                            _to_float(data.get("atr_15m"), 0.0), _cw_atr_1d
                        )
                        valid_put_stop_wall = (
                            put_wall_float
                            if (
                                has_putwall
                                and put_wall_float < effective_c_val
                                and not math.isclose(
                                    put_wall_float, effective_c_val, abs_tol=1e-4
                                )
                            )
                            else 0.0
                        )
                        _cw_room = compute_dynamic_room_threshold(
                            effective_c_val,
                            valid_put_stop_wall,
                            _cw_atr_15m,
                            _cw_atr_1d,
                            direction="LONG",
                        )
                        _cw_threshold_pct = _cw_room.threshold_pct * 100
                        space_flag = ""
                        degrade_line: Optional[str] = None
                        is_call_zero = math.isclose(
                            call_wall_dist_pct, 0.0, abs_tol=1e-4
                        )
                        if call_wall_dist_pct < 0 and not is_call_zero:
                            space_flag = " ⚠️ [數據異常：CallWall已低於現價]"
                        elif call_wall_dist_pct < _cw_threshold_pct or is_call_zero:
                            space_flag = f" ❌ 不足 {_cw_threshold_pct:.2f}%"
                            if not _cw_room.degrade_reason:
                                # 標出決定門檻的那一項，免得與盈虧比 ✅ 看似矛盾
                                _binding_label = _ROOM_BINDING_TERM_LABELS.get(
                                    _cw_room.binding_term
                                )
                                space_flag += (
                                    f" (動態門檻：{_binding_label})"
                                    if _binding_label
                                    else " (動態門檻)"
                                )
                        # 降級一律揭露，不限於門檻未達時（docs/strategies/06 §5）：
                        # 空間充足的結論若建立在降級門檻上（例如底牆已失效、
                        # valid_put_stop_wall 歸 0 使 RISK 項被剔除），靜默通過等於
                        # 讓使用者誤以為那是完整數據下的判定。
                        if _cw_room.degrade_reason:
                            degrade_line = f" │  ⚠ {_cw_room.degrade_reason}"
                        call_arrow = (
                            "↑" if (call_wall_dist_pct >= 0 or is_call_zero) else "↓"
                        )
                        _space_line = (
                            f"距現價空間: {call_arrow}"
                            f"{abs(call_wall_dist_pct):.2f}%{space_flag}"
                        )
                        _cw_band_pct = max(
                            _cw_atr_15m / effective_c_val * 100, _CALLWALL_HUG_MIN_PCT
                        )
                        if (call_wall_dist_pct >= 0 or is_call_zero) and (
                            call_wall_dist_pct < _cw_band_pct
                        ):
                            _space_line += " 📌 貼牆(<1×ATR₁₅ₘ)"
                            _call_wall_hug_outer = max(call_wall_dist_pct, 0.0)
                        if degrade_line:
                            _space_line += f"\n{degrade_line}"
                        call_reanchor_note = " (動態重錨)" if call_reanchored else ""
                        call_items = [
                            f"CallWall: ${call_wall_float:.2f}{call_reanchor_note}",
                            _space_line,
                            f"深度: {_fmt_gex_notional(call_wall_depth)}",
                        ]
                        _cap_call = _sto_best(
                            [
                                s
                                for s in (data.get("sto_physical_cap_strikes") or [])
                                if isinstance(s, dict)
                                and str(s.get("type", "")).upper() == "CALL"
                                and _to_float(s.get("strike"), 0.0) > effective_c_val
                                and (
                                    (_to_float(s.get("strike"), 0.0) - call_wall_float)
                                    / effective_c_val
                                    * 100
                                    <= _GEX_STO_DIVERGENCE_THRESHOLD_PCT
                                )
                                and _sto_cap_eligible(s)
                            ]
                        )
                        if _cap_call is not None:
                            _cc_exp = str(_cap_call.get("expiry", ""))
                            _cc_k = _to_float(_cap_call.get("strike"), 0.0)
                            _cc_row = next(
                                (
                                    u
                                    for u in (data.get("uoa") or [])
                                    if isinstance(u, dict)
                                    and str(u.get("expiry", "")) == _cc_exp
                                    and _to_float(u.get("strike"), 0.0) == _cc_k
                                    and str(u.get("type", "")).upper() == "CALL"
                                ),
                                None,
                            )
                            _cc_label = str((_cc_row or {}).get("spread_label") or "")
                            if _cc_label:
                                _cc_tag = (
                                    "〔"
                                    + _cc_label.replace(
                                        "熊市價差 (Bear Call Spread)", "熊市Call價差"
                                    )
                                    + "〕"
                                )
                            elif _cc_row is None:
                                _cc_tag = (
                                    "〔貸方價差，表外〕"
                                    if _cap_call.get("spread_credit")
                                    else "〔全鏈掃描，表外〕"
                                )
                            else:
                                _cc_tag = ""
                            call_items.append(
                                f"⚠️ 大單 ${_cc_k:.2f}"
                                f"{_sto_exp_dte(_cc_exp, datetime.now(_gex_tz).date())}"
                                f" STO CALL {int(_to_float(_cap_call.get('volume'), 0.0)):,}口"
                                f"{_sto_notional_suffix(_to_float(_cap_call.get('notional_value'), 0.0))}"
                                f" 封頂{_cc_tag}"
                            )
                        call_block_items = call_items
                        if call_wall_dist_pct >= 0 or is_call_zero:
                            upside_room_pct = max(call_wall_dist_pct, 0.0)
                            upside_threshold_pct = _cw_threshold_pct

                    # ── 下檔 × 上檔交叉判定 ──
                    if put_block_items is not None:
                        upside_short = (
                            upside_room_pct is not None
                            and upside_threshold_pct is not None
                            and upside_room_pct < upside_threshold_pct
                        )
                        if sweet_spot_idx is not None and upside_short:
                            # 停損距離合格不等於可進場：上方空間不足以支撐 2.2:1
                            put_block_items[sweet_spot_idx] = (
                                f"{sweet_spot_prefix} ✅ 停損距離合格"
                                f"\n │  ❌ 上檔空間 {upside_room_pct:.2f}% 不足 "
                                f"{upside_threshold_pct:.2f}%，非進場點"
                            )
                        if (
                            upside_room_pct is not None
                            and effective_c_val > 0
                            and 0 < put_stop_for_rr < effective_c_val
                        ):
                            reward = call_wall_float - effective_c_val
                            rr_parts: List[str] = []
                            if _call_wall_hug_outer is not None:
                                # 貼牆：上檔已封頂，盈虧比無意義，單行帶過以省字數
                                put_block_items.append(
                                    "短線盈虧比: ⛔ 上檔已封頂（距 CallWall "
                                    f"{_call_wall_hug_outer:.2f}% < 1×ATR₁₅ₘ），不計"
                                )
                            else:
                                _rr_cases = (
                                    (
                                        "閘門"
                                        if (put_wall_weak and alt_stop_for_rr > 0)
                                        else "",
                                        put_stop_for_rr,
                                    ),
                                    (
                                        f"淨 GEX 支撐錨 ${alt_anchor_for_rr:.2f}",
                                        alt_stop_for_rr,
                                    ),
                                )
                                for _label, _stop in _rr_cases:
                                    if not (0 < _stop < effective_c_val):
                                        continue
                                    _rr = max(reward, 0.0) / (effective_c_val - _stop)
                                    _flag = (
                                        "✅" if _rr >= _ROOM_RISK_MULTIPLIER else "❌"
                                    )
                                    if (
                                        not _label.startswith("淨 GEX")
                                        and put_too_tight
                                        and _flag == "✅"
                                    ):
                                        _flag = "⚠ 停損過窄、比值虛高"
                                    _prefix = f"{_label} " if _label else ""
                                    rr_parts.append(f"{_prefix}{_rr:.2f}:1 {_flag}")
                                if (
                                    put_min_stop_frac is not None
                                    and put_min_stop_frac > 0
                                ):
                                    _rr_min = max(reward, 0.0) / (
                                        effective_c_val * put_min_stop_frac
                                    )
                                    _flag_min = (
                                        "✅"
                                        if _rr_min >= _ROOM_RISK_MULTIPLIER
                                        else "❌"
                                    )
                                    rr_parts.append(
                                        f"合格停損 ↓{put_min_stop_frac * 100:.2f}% "
                                        f"{_rr_min:.2f}:1 {_flag_min}"
                                    )
                                put_block_items.append(
                                    f"短線盈虧比 (期權視角，至 CallWall ${call_wall_float:.2f}): "
                                    + "｜".join(rr_parts)
                                    + f" (門檻 {_ROOM_RISK_MULTIPLIER:.1f}:1)"
                                )
                        _append_tree_block(gex_lines, "🛡️ 下檔支撐", put_block_items)
                    if call_block_items is not None:
                        _append_tree_block(gex_lines, "🚧 上檔壓力", call_block_items)

                    regime_items: List[str] = []
                    net_gex_raw = gex_data.get("net_gex")
                    try:
                        net_gex_float = (
                            float(net_gex_raw) if net_gex_raw is not None else None
                        )
                    except (ValueError, TypeError):
                        net_gex_float = None
                    if net_gex_float is not None:
                        # ISSUE-4.4: 增設 [-50k, +50k] 中性死區 (Neutral Deadband)，防範 0 軸微幅跳動引發頻繁閃爍
                        if abs(net_gex_float) <= 50000.0:
                            regime_label = "⚖️ NEUTRAL_GAMMA (中性均衡)"
                        elif net_gex_float > 50000.0:
                            regime_label = "🟢 LONG_GAMMA (自穩定壓制波動)"
                        else:
                            regime_label = "🔴 SHORT_GAMMA (助漲助跌)"
                        regime_items.append(
                            f"Net GEX Regime ({_gex_exp_label}): {_fmt_gex_notional(net_gex_float)} ({regime_label})"
                        )
                        # 釘住效應：全鏈 Long Gamma 時做市商逆勢避險（漲賣跌買），現價貼近
                        # 上方 CallWall 時突破難以延續——放量只代表量能，不代表能穿牆。
                        if (
                            net_gex_float > 50000.0
                            and upside_room_pct is not None
                            and effective_c_val > 0
                        ):
                            _pin_atr_1d = _to_float(data.get("atr_14"), 0.0)
                            if _pin_atr_1d <= 0.0 or math.isclose(
                                _pin_atr_1d, 0.01, abs_tol=1e-6
                            ):
                                _pin_atr_1d = _to_float(data.get("atr_1d"), 0.0)
                            pin_band_pct = (
                                _pin_atr_1d / effective_c_val * 100
                                if _pin_atr_1d > 0.01
                                else _PIN_FALLBACK_BAND_PCT
                            )
                            if upside_room_pct <= pin_band_pct:
                                _pin_strike = call_wall_float
                                regime_items.append(
                                    f"📌 釘住效應：Long Gamma 且距 CallWall "
                                    f"${call_wall_float:.2f} 僅 {upside_room_pct:.2f}%"
                                    f"（≤ 1×ATR₁D {pin_band_pct:.2f}%），壓制突破延續"
                                )

                    gamma_flip_val = estimate_symbol_gamma_flip(
                        gex_prof, effective_c_val
                    )
                    if gamma_flip_val > 0 and effective_c_val > 0:
                        flip_buffer_pct = (
                            (effective_c_val - gamma_flip_val) / effective_c_val * 100
                        )
                        flip_item = (
                            f"Gamma Flip (轉正履約價): ${gamma_flip_val:.2f}"
                            f" (緩衝: {flip_buffer_pct:+.2f}%)"
                        )
                        # 負側量級過小的交叉屬雜訊（docs/microstructure/03 §5.6）：
                        # 揭露排除後的 Flip，閘門仍以原值為準；內插零軸此時無意義。
                        noise_note = gamma_flip_noise_note(
                            gamma_flip_materiality(gex_prof, effective_c_val),
                            estimate_material_gamma_flip(
                                gex_prof,
                                effective_c_val,
                                GAMMA_FLIP_MATERIALITY_DISPLAY,
                            ),
                        )
                        # 閘門取離散履約價格點（docs/microstructure/03 §2）；真正的
                        # 零軸落在前一檔與該檔之間，並列內插值避免誤讀為零軸本身。
                        flip_zero = interpolate_gamma_flip_zero(
                            gex_prof, gamma_flip_val
                        )
                        if not noise_note:
                            # 雜訊交叉不構成「跌破 Flip」；比較用現價與本區塊同源
                            _gamma_flip_outer = gamma_flip_val
                            _gex_price_outer = effective_c_val
                        if noise_note:
                            flip_item += f"\n │  {noise_note}"
                        elif flip_zero > 0 and not math.isclose(
                            flip_zero, gamma_flip_val, abs_tol=0.005
                        ):
                            flip_item += (
                                f"\n │  相鄰履約價內插零軸 ≈ ${flip_zero:.2f}"
                                "（閘門以履約價格點為準）"
                            )
                        regime_items.append(flip_item)
                    else:
                        regime_items.append("Gamma Flip: -- (無法估算)")

                    local_regime = analyze_local_gamma_regime(gex_prof, effective_c_val)
                    if local_regime is not None:
                        _local_short_gamma_outer = bool(local_regime.is_short_gamma)
                        total_long = (
                            net_gex_float is not None and net_gex_float > 50000.0
                        )
                        total_short = (
                            net_gex_float is not None and net_gex_float < -50000.0
                        )
                        local_label = (
                            "🔴 SHORT_GAMMA"
                            if local_regime.is_short_gamma
                            else "🟢 LONG_GAMMA"
                        )
                        conflict = (total_long and local_regime.is_short_gamma) or (
                            total_short and not local_regime.is_short_gamma
                        )
                        if conflict:
                            local_note = (
                                "（與全鏈體制相反：現價已落入負 Gamma 區，單邊擴散風險）"
                                if local_regime.is_short_gamma
                                else "（與全鏈體制相反：現價附近仍有正 Gamma 緩衝）"
                            ) + "；局部值為相鄰履約價 GEX 內插，現價跨一檔即可能翻號"
                        else:
                            local_note = ""
                        regime_items.append(
                            f"局部體制 (現價處): {local_label}{local_note}"
                        )
                        if local_regime.flip_strike > 0 and gamma_flip_val <= 0:
                            side_note = (
                                "以上轉負 Gamma"
                                if local_regime.flip_side == "SHORT_ABOVE"
                                else "以下轉負 Gamma"
                            )
                            regime_items.append(
                                f"局部 Gamma 翻轉線 ≈ ${local_regime.flip_strike:.2f}（{side_note}）"
                            )

                    _append_tree_block(gex_lines, "⚙️ 體制判讀", regime_items)

                    gex_lines.append("```")

                is_stale = bool(gex_data.get("_is_stale_cache", False))
                stale_suffix = " [快取 / API 降級]" if is_stale else ""

                _add_ansi_field_safely(
                    embed, f"🧲 Gamma 曝險分布 (GEX Profile){stale_suffix}", gex_lines
                )
        except Exception as e:
            logger.debug(f"GEX Profile rendering skipped: {e}")

    # 3.8 🌊 動能與擠壓狀態 (Momentum & Squeeze)
    # 以每次 /x 強制重抓的 1y 日線計算（盤中含今日成型中的 K 棒，數值會隨盤中
    # 變動）；批次雷達讀的是 squeeze_cache，兩者時點不同，不保證同值。
    # 資料時間行標明所用 K 棒日期與抓取時刻。
    psq_raw = data.get("psq_result") or {}
    # Support both PSQResult dataclass and plain dict (e.g. from squeeze cache)
    if hasattr(psq_raw, "is_squeezing"):
        sqz_is_squeezing: bool = bool(getattr(psq_raw, "is_squeezing", False))
        sqz_momentum: float = _to_float(getattr(psq_raw, "momentum_value", 0.0))
        sqz_squeeze_level: str = str(getattr(psq_raw, "squeeze_level", "Release"))
        sqz_signal_dir: str = str(getattr(psq_raw, "signal_direction", "Neutral"))
        sqz_vix_label: str = str(getattr(psq_raw, "vix_momentum_label", "NORMAL"))
        sqz_vix_note: str = str(getattr(psq_raw, "vix_timeframe_note", ""))
    else:
        sqz_is_squeezing = bool(psq_raw.get("is_squeezing", False))
        sqz_momentum = _to_float(
            psq_raw.get("momentum", psq_raw.get("momentum_value", 0.0))
        )
        sqz_squeeze_level = str(psq_raw.get("squeeze_level", "Release"))
        sqz_signal_dir = str(
            psq_raw.get("direction", psq_raw.get("signal_direction", "Neutral"))
        )
        sqz_vix_label = str(psq_raw.get("vix_momentum_label", "NORMAL"))
        sqz_vix_note = str(psq_raw.get("vix_timeframe_note", ""))

    if psq_raw:
        # Map squeeze level to a human-readable label
        _squeeze_level_map: dict[str, str] = {
            "High": "🔴 高強度擠壓 (High)",
            "Mid": "🟠 中強度擠壓 (Mid)",
            "Normal": "🟡 一般擠壓 (Normal)",
            "Release": "⚪ 解除擠壓 (Release)",
        }
        sqz_level_label = _squeeze_level_map.get(
            sqz_squeeze_level, f"⚪ {sqz_squeeze_level}"
        )

        from cogs.embed_builders._embed_helpers import get_sqz_status_display

        _mom_color = (
            getattr(psq_raw, "momentum_color", None)
            if hasattr(psq_raw, "is_squeezing")
            else psq_raw.get("momentum_color")
        )
        directional_status, sqz_ansi_color = get_sqz_status_display(
            sqz_is_squeezing,
            sqz_momentum,
            sqz_signal_dir,
            momentum_color=str(_mom_color) if _mom_color else None,
        )

        # 日內轉折：日線動能為正（與「方向狀態」同源的即時值 sqz_momentum），
        # 但 65m／15m 動能轉負或價格跌破 Gamma Flip。
        _turn_note = ""
        _mtx = getattr(data.get("squeeze_eval"), "matrix", None)
        if isinstance(_mtx, dict):
            try:
                if sqz_momentum > 0:
                    if any(
                        tf in _mtx and _mtx[tf].momentum_value < 0
                        for tf in ("65m", "15m")
                    ):
                        _turn_note = "65m/15m 動能<0"
                    elif (
                        _gamma_flip_outer is not None
                        and _gamma_flip_outer > 0
                        and _gex_price_outer is not None
                        and 0 < _gex_price_outer < _gamma_flip_outer
                    ):
                        _turn_note = "跌破 Gamma Flip"
            except (AttributeError, TypeError):
                _turn_note = ""

        sqz_lines = [
            "```ansi",
            " 日線即時",
            f" ├─ 擠壓強度: {sqz_level_label}",
            f" {'├─' if _turn_note else '└─'} 方向狀態: {sqz_ansi_color}{directional_status}\u001b[0m",
        ]
        if _turn_note:
            sqz_lines.append(f" └─ \u001b[1;33m⚠ 日內轉弱: {_turn_note}\u001b[0m")

        _gd_ago = (
            getattr(psq_raw, "green_dot_bars_ago", None)
            if hasattr(psq_raw, "is_squeezing")
            else psq_raw.get("green_dot_bars_ago")
        )
        if _gd_ago is not None:
            sqz_lines.insert(3, f" ├─ Green Dot: {_gd_ago} 根前（日線擠壓解除點）")

        if sqz_vix_label != "NORMAL":
            if sqz_vix_label == "OVEREXTENDED_RISK":
                # 觸發條件是**大盤** VIX < 15（psq_engine.py），不是個股 IV
                sqz_lines.append(
                    " ⚠️ VIX 標記: 大盤低波 (VIX<15) 過度延伸風險，謹慎追多"
                )
            elif sqz_vix_label == "HIGH_CONVICTION_RECOVERY":
                sqz_lines.append(" 🔥 VIX 標記: 高波動期高確信反彈訊號")

        # Dynamic timeframe volatility regime evaluation
        # IV Rank 缺失（樣本不足）時必須維持「未知」，不可補 0 當成低波。
        # 「極端高波」只認市場隱含 IV：跨式 IV；或 DTE>=5 的即時 IV。HV 代理／快取值不算。
        # 跨式 DTE 未知（舊快取）時不排除；已知且 < 5 則含結算 Gamma，不算。
        sqz_iv_pct: float | None = None
        _st_dte_x = _iv_attr("straddle_dte")
        if _to_float_or_none(_iv_attr("straddle_implied_iv")) is not None and (
            _st_dte_x is None or int(_st_dte_x) >= _EXTREME_VOL_MIN_DTE
        ):
            sqz_iv_pct = _to_float_or_none(_iv_attr("straddle_implied_iv"))
        elif _iv_attr("iv_source") == "LIVE_IV":
            _live_dte = _iv_attr("current_iv_dte")
            if _live_dte is not None and int(_live_dte) >= _EXTREME_VOL_MIN_DTE:
                sqz_iv_pct = _to_float_or_none(current_iv)
        iv_val_num = sqz_iv_pct * 100 if sqz_iv_pct is not None else None
        iv_rank_known = _to_float_or_none(iv_rank)

        high_vol_reasons: List[str] = []
        if iv_rank_known is not None and iv_rank_known > 50.0:
            high_vol_reasons.append(f"IVR {iv_rank_known:.0f}%")
        if iv_val_num is not None and iv_val_num > 80.0:
            high_vol_reasons.append(f"跨式 IV {iv_val_num:.0f}%")

        if high_vol_reasons:
            # 賣方建議必須服從風控：VIX 戰情階梯禁用 STO 時不得在同一張卡片
            # 上建議賣方策略（兩者結論互斥）。
            _kelly = data.get("kelly_sizing")
            _kelly_warnings = [
                str(w) for w in (getattr(_kelly, "warnings", None) or [])
            ]
            sto_locked = any("STO 禁用" in w for w in _kelly_warnings)
            hedge_advice = (
                "建議縮小部位（賣方策略目前受風控禁用）"
                if sto_locked
                else "建議縮小部位或使用期權賣方策略保護"
            )
            _ivr_unknown = "（IVR 累積中，無歷史分位）" if iv_rank_known is None else ""
            sqz_vix_note = (
                f"極端高波環境 ({' / '.join(high_vol_reasons)})，提防劇烈洗盤，"
                f"{hedge_advice}。{_ivr_unknown}"
            )
        elif (
            iv_rank_known is not None
            and iv_rank_known < 30.0
            and abs(sqz_momentum) < 0.5
        ):
            sqz_vix_note = "低波期，建議以日K/4H為主，忽略30m雜訊"
        else:
            sqz_vix_note = ""

        if sqz_vix_note:
            sqz_lines.append(f" 💡 時框建議: {sqz_vix_note}")

        _sq_eval = data.get("squeeze_eval")
        _matrix_lines = format_psq_matrix_lines(getattr(_sq_eval, "matrix", None))
        if _matrix_lines:
            # 空行讓 _add_ansi_field_safely 超過欄位上限時以整段換欄
            sqz_lines.append("")
            sqz_lines.extend(_matrix_lines)
            sqz_lines.append("")

        freshness_line = _format_psq_freshness(
            data.get("psq_bar_date"),
            bool(data.get("psq_bar_is_live", False)),
            data.get("psq_fetched_at"),
        )
        if freshness_line:
            sqz_lines.append(freshness_line)

        sqz_lines.append("```")
        _add_ansi_field_safely(
            embed, "🌊 動能與擠壓狀態 (Momentum & Squeeze)", sqz_lines
        )

    # 4. 🎯 結算與目標 (Target Lock)

    _mp_val = data.get("max_pain")
    cb_triggered = data.get("circuit_breaker_triggered", False)
    # Initialize price here so all branches below (including the _mp_val is None
    # branch) can safely reference it (e.g. in get_scenario_guidance calls).
    price = c_val

    max_pain_raw = _to_float_or_none(_mp_val)
    if max_pain_raw is None or max_pain_raw <= 0.0:
        max_pain = 0.0
        distance = 0.0
        mp_str = "N/A"
        dist_str = "⚠️ 數據源缺失"
        dist_color = "\u001b[1;30m"
    else:
        max_pain = max_pain_raw
        distance = (((price - max_pain) / max_pain) * 100) if price > 0 else 0.0

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

    if data.get("tdpq_activated"):
        ddp_status = "⚡ TDPQ 突破共振 (Triple Discount + Squeeze)"
        ddp_color = "\u001b[1;36m"
    elif data.get("tdp_activated"):
        ddp_status = "✨ TDP 估值三擊 (Triple Discount Pricing)"
        ddp_color = "\u001b[1;36m"
    elif data.get("is_ddp"):
        ddp_status = "符合 (符合 DDP 盈餘/估值雙擊)"
        ddp_color = "\u001b[1;32m"
    else:
        _ddp_reason = data.get("ddp_reason")
        ddp_status = f"不符合（{_ddp_reason}）" if _ddp_reason else "不符合"
        ddp_color = "\u001b[1;30m"

    ivr_val = data.get("iv_rank")
    ivr_num = _to_float_or_none(ivr_val)
    if ivr_num is None:
        ivr_str = "--"
        ivr_color = "\u001b[1;30m"
        ivr_comp = 0.0
    else:
        ivr_str = f"{ivr_num:.1f}%"
        ivr_color = "\u001b[1;35m" if ivr_num > 50.0 else "\u001b[1;36m"
        ivr_comp = ivr_num

    _raw_pcr = data.get("pcr")
    pcr_dict: dict = _raw_pcr if isinstance(_raw_pcr, dict) else {}
    vol_pcr_raw = pcr_dict.get("volume_pcr") if pcr_dict else None
    vol_pcr = _to_float(vol_pcr_raw)

    oi_pcr_raw = pcr_dict.get("oi_pcr", pcr_dict.get("pcr")) if pcr_dict else None
    oi_pcr = _to_float(oi_pcr_raw)

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

    _st_iv_f = _to_float_or_none(_iv_attr("straddle_implied_iv"))

    def _sigma_to_dte(dte_days: int) -> Optional[float]:
        if _st_iv_f is None or _st_iv_f <= 0 or price <= 0 or dte_days < 1:
            return None
        return _st_iv_f * price * math.sqrt(dte_days / 365.0)

    # 結算前 1σ 必須對齊頭條 Max Pain 實際鎖定的到期日（週五當天會滾到下週五），
    # 不可取 month_max_pains 的最近一檔。舊快取無 expiry 時不做 1σ 判定。
    _sigma_guard: Optional[float] = None
    _mp_exp = data.get("max_pain_expiry")
    if _mp_exp:
        try:
            from market_time import ny_tz as _sg_tz

            _d0 = (
                datetime.strptime(str(_mp_exp), "%Y-%m-%d").date()
                - datetime.now(_sg_tz).date()
            ).days
            _sigma_guard = _sigma_to_dte(_d0)
        except Exception:
            _sigma_guard = None

    if cb_triggered:
        scenario = "⚠️ 最大痛點偏離度過高 (>30%) 已啟動斷路器，暫停輸出結算操作指引，請以技術指標為準。"
        scen_color = "\u001b[1;31m"
    elif _mp_val is None or max_pain == 0.0:
        scenario = get_scenario_guidance(price, max_pain)
        scen_color = "\u001b[1;30m"
    else:
        scenario = get_scenario_guidance(
            price, max_pain, sigma_to_expiry=_sigma_guard, pin_strike=_pin_strike
        )
        spread_pct = ((price - max_pain) / max_pain) if max_pain > 0 else 0.0
        if "釘住" in scenario:
            scen_color = "\u001b[1;36m"
        elif "收斂機率低" in scenario:
            scen_color = "\u001b[1;30m"
        elif spread_pct > 0.03:
            scen_color = "\u001b[1;31m"
        elif spread_pct < -0.03:
            scen_color = "\u001b[1;32m"
        elif abs(spread_pct) < 0.02:
            scen_color = "\u001b[1;33m"
        else:
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

    gravity: Optional[Dict[str, Any]] = None
    if not cb_triggered:
        from market_time import ny_tz as _gravity_tz

        gravity = find_settlement_gravity(
            data.get("month_max_pains"), datetime.now(_gravity_tz)
        )
        if gravity is not None:
            scenario_overlays.append(
                f"⚠️ 結算日引力：{gravity['expiry']} (DTE {gravity['dte']}) 痛點 "
                f"${gravity['max_pain']:.2f}，現價偏離 {gravity['distance_pct']:+.1f}%，"
                "結算前 Gamma 釘住效應最強，避免追價新倉"
            )

    spread_ratio = data.get("spread_ratio")
    if spread_ratio is not None and spread_ratio > 15.0:
        scenario_overlays.append(
            f"⚠️ 流動性警告：當前期權買賣點差高達 {spread_ratio:.1f}%，滑價風險大，請勿掛市價單。"
        )
    # Removed concatenation to allow structured multi-line formatting below

    month_mps = data.get("month_max_pains", [])
    mp_lines = []
    _any_sigma = False
    if month_mps:
        try:
            month_mps_sorted = sorted(month_mps, key=lambda x: x.get("expiry", ""))
        except Exception:
            month_mps_sorted = month_mps

        # Limit to nearest 4 expirations to prevent Discord embed field limit error (1024 chars)
        month_mps_sorted = month_mps_sorted[:4]

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
                is_friday = exp_dt.weekday() == 4
            except Exception:
                dte = 0
                is_friday = False

            if dte <= 7:
                if is_friday:
                    label = "週五即期"
                else:
                    label = "期中特約/末日週線"
            elif dte <= 14:
                label = "次週主力"
            else:
                label = "月線主力"

            deg_tag = " (V)" if is_deg or calc_mode == "Volume" else ""
            mp_price_str = f"${mp_val:.2f}{deg_tag}" if mp_val is not None else "N/A"
            dist_val_str = f"{dist:+.1f}%" if mp_val is not None else "N/A"
            color_item = "\u001b[1;31m" if abs(dist) > 5.0 else "\u001b[1;32m"
            prefix = " ├─ " if i < len(month_mps_sorted) - 1 else " └─ "

            _sig_i = _sigma_to_dte(dte)
            sig_str = f" ±${_sig_i:.1f}" if _sig_i is not None else ""
            _any_sigma = _any_sigma or _sig_i is not None
            mp_lines.append(
                f"{prefix}{exp} (DTE {dte} / {label}): \u001b[1;33m{mp_price_str}\u001b[0m (當前價差: {color_item}{dist_val_str}\u001b[0m){sig_str}"
            )
    else:
        mp_lines.append(
            f" └─ Max Pain價位: \u001b[1;33m{mp_str}\u001b[0m (當前價差: {dist_color}{dist_str}\u001b[0m)"
        )

    target_lines = [
        "```ansi",
        " 最大痛點結算 (Max Pain Settlement" + ("，±結算前1σ)" if _any_sigma else ")"),
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
        ]
    )
    if not scenario_overlays:
        target_lines.append(f" └─ 操作指引: {scen_color}{scenario}\u001b[0m")
    else:
        target_lines.append(f" ├─ 基本指引: {scen_color}{scenario}\u001b[0m")
        for i, overlay in enumerate(scenario_overlays):
            prefix = " ├─" if i < len(scenario_overlays) - 1 else " └─"
            if "結構性情緒背離" in overlay:
                label = "結構背離"
                ov_color = "\u001b[1;31m"
            elif "IV Rank" in overlay:
                label = "高波預警"
                ov_color = "\u001b[1;33m"
            elif "流動性警告" in overlay:
                label = "流動風險"
                ov_color = "\u001b[1;31m"
            elif "結算日引力" in overlay:
                label = "結算引力"
                ov_color = "\u001b[1;31m"
            else:
                label = "附加預警"
                ov_color = "\u001b[1;33m"
            target_lines.append(f"{prefix} {label}: {ov_color}{overlay}\u001b[0m")
    kelly_sizing = data.get("kelly_sizing")
    if kelly_sizing:
        contracts = kelly_sizing.suggested_contracts
        exposure = kelly_sizing.exposure_pct
        warnings = (
            " | ".join(kelly_sizing.warnings)
            if getattr(kelly_sizing, "warnings", None)
            else "安全/符合風控"
        )
        _k_beta = data.get("kelly_beta")
        _k_unit = _to_float_or_none(data.get("kelly_unit_weighted_delta"))
        _beta_str = f"{_k_beta:.2f}" if _k_beta is not None else "—"
        kelly_lines = [
            " 賣 Put 曝險上限 (Kelly·16Δ，非現貨進場許可)",
            f" ├─ 建議上限: \u001b[1;36m{contracts} 口\u001b[0m (β 加權 Delta 佔總資金 {exposure}%)",
        ]
        if _k_unit:
            kelly_lines.append(
                f" ├─ 每口(未指定到期): β={_beta_str} → ≈{_k_unit:.1f} 股 SPY 等值"
            )
        # IVR：與 ivr_strategy_gate 同一判定（含門檻與未知／異常值語意）
        _pre_ivr = _to_float_or_none(ivr_val)
        if _pre_ivr is None or _pre_ivr < 0.0:
            _pre_ivr_mark = "⚠"
        else:
            _pre_ivr_mark = "❌" if is_selling_locked_by_ivr(_pre_ivr) else "✅"
        # 引力：沿用上方結算指引已算好的 gravity；斷路器觸發時不評估
        _pre_grav = "—" if cb_triggered else ("❌" if gravity is not None else "✅")
        _pre_wall = (
            "—"
            if _put_wall_weak_outer is None
            else ("❌" if _put_wall_weak_outer else "✅")
        )
        _pre_local = (
            "—"
            if _local_short_gamma_outer is None
            else ("❌" if _local_short_gamma_outer else "✅")
        )
        _pre_event = "⚠" if _event_loading_outer else "✅"
        kelly_lines.append(
            f" ├─ 賣方前提: IVR{_pre_ivr_mark} 引力{_pre_grav} 牆淨GEX{_pre_wall}"
            f" 局部Γ{_pre_local} 事件{_pre_event}"
        )
        if contracts == 0 and not any(
            k in warnings for k in ("禁用", "fail-closed", "暫停")
        ):
            kelly_lines.append(" ├─ 0 口原因: 單口 β 加權 Delta 已超過可用風險額度")
        if getattr(kelly_sizing, "warnings", None):
            kelly_lines.append(f" └─ 系統風控: \u001b[1;33m{warnings}\u001b[0m")
        else:
            # 無風控警示時不輸出「安全/符合風控」整行（省字數），改由末項收尾。
            kelly_lines[-1] = kelly_lines[-1].replace(" ├─", " └─", 1)
        target_lines.extend(kelly_lines)

    _sq_ev = data.get("squeeze_eval")
    _sq_res = getattr(_sq_ev, "result", None)
    if _sq_res is not None:
        from market_analysis.squeeze_entry import rules as _sq_rules

        _status_map = {
            _sq_rules.STATUS_ENTRY: "🟢 可進場",
            _sq_rules.STATUS_WATCH: "🟡 觀察",
            _sq_rules.STATUS_NONE: "⚪ 無訊號",
            _sq_rules.STATUS_VETOED: "⛔ 否決",
            _sq_rules.STATUS_NO_DATA: "— 資料不足",
        }
        _tier = getattr(_sq_res, "tier", None)
        _size = getattr(_sq_res, "size_pct", None)
        _tier_txt = f" T{_tier} → 建議部位 {_size}%" if _tier else ""
        target_lines.append(" 現貨擠壓進場 (Squeeze Entry · strategies/10)")
        target_lines.append(
            f" ├─ 判定: {_status_map.get(str(_sq_res.status), str(_sq_res.status))}{_tier_txt}"
        )
        _sq_stop: Optional[float] = getattr(_sq_res, "stop", None)
        _px_now = _to_float(data.get("price"), 0.0)
        if _sq_stop:
            _stop_pct = (
                f" (↓{(_px_now - _sq_stop) / _px_now * 100:.2f}%)"
                if _px_now > 0
                else ""
            )
            target_lines.append(f" ├─ 參考停損: ${_sq_stop:.2f}{_stop_pct}")
        target_lines.append(
            f" ├─ 觸發: {'、'.join(getattr(_sq_res, 'triggers', []) or []) or '—'}"
        )
        target_lines.append(
            f" └─ {getattr(_sq_res, 'resistance_warning', None) or _sq_res.reason}"
        )

    target_lines.append("```")

    _add_ansi_field_safely(embed, "🎯 結算與目標 (Target Lock)", target_lines)

    uoa_data = data.get("uoa", [])
    # 15 分鐘心跳週期的 2 倍緩衝，與 market_embeds.py 的
    # _UOA_SNAPSHOT_MAX_AGE_SECONDS 保持一致，避免兩處門檻各自漂移。
    uoa_field_name = "🐋 異常活動 (UOA)" + format_cache_age_suffix(
        data.get("uoa_age_seconds"), stale_threshold_seconds=1800.0
    )
    if uoa_data:
        try:
            table_str = _format_uoa_field(uoa_data)
            table_lines = table_str.split("\n")
            table_lines.append("")
            table_lines.append(
                "⚠️ 方向／SWEEP 為 Bid-Ask 啟發式；OI 為前日；📊日累積 ❔未定"
            )
            _add_ansi_field_safely(embed, uoa_field_name, table_lines)
        except Exception:
            pass
    else:
        embed.add_field(
            name=uoa_field_name,
            value="```ansi\n目前無顯著異常活動\n```",
            inline=False,
        )

    embed.set_footer(
        text="🔗 使用 /settle_hedge 紀錄對沖或 /event_impact 進行曝險模擬。\n"
        f"⏱️ {OPTION_DATA_TIMING_NOTE}"
    )
    return embed


def create_tactical_hedge_embed(
    symbol: str, ivr: Optional[float], rec_strategy: str
) -> discord.Embed:
    """建構標的對沖防禦中心的 Embed（ivr 為 None 代表樣本不足/未知，不捏造數值）"""
    embed = NexusEmbed(
        title=f"🛡️ {symbol} 對沖防禦中心 (Tactical Hedging)",
        color=discord.Color.red(),
    )
    ivr_str = f"{ivr:.1f}%" if ivr is not None else "--% (資料不足)"
    embed.add_field(
        name="📊 當前波動率狀態 (Volatility Context)",
        value=f"* **IV Rank:** `{ivr_str}`\n* **推薦防禦策略:** `{rec_strategy}`",
        inline=False,
    )
    embed.add_field(
        name="🛠️ 推薦執行步驟",
        value="1. 請在終端機或聊天室中輸入 `/settle_hedge` 以登錄對沖操作。\n2. 可搭配 `/event_impact` 輸入事件代號模擬近期宏觀事件（財報/CPI）對選擇權曝險的影響。",
        inline=False,
    )
    return embed


# 左側交易六重鐵律的條件說明區塊 (create_entry_rules_embed 使用)。與右側說明
# 分開維護：兩套鐵律的技術定義方向相反，共用同一份說明會讓使用者看到與實際
# 判定結果自相矛盾的解釋。
_ENTRY_RULES_DETAIL_LEFT = [
    "```ansi",
    " 條件一～四為核心結構性進場門檻；條件五、六屬總經財報與",
    " candidate 自身 DTE 雜訊安全閥，僅於前置條件皆通過後才會真正發動判定",
    " (未發動時上方仍會列出「⏭️ 略過」標記，六項條件永遠完整列出)",
    " ----------------------------------",
    "條件一",
    "•結構性空頭力竭與極值乖離確認",
    "•現價 ≤ Session VWAP − 1.5 × ATR₁₅ₘ (極度負向乖離)，且 15m RSI ≤ 30",
    "•K棒形態須拒絕灌壓：排除大陰線實體灌破 (Close≈Low)；須出現錘頭/Pin Bar",
    "  (下影線 ≥ 實體 1.5 倍)、蜻蜓十字 (實體 ≤ 全距 10% 且下影線 ≥ 全距 60%)",
    "  或連續 2 根實體收窄且未破前低",
    "•量能二擇一：縮量窒息 (≤ 前20根均量 × 0.7) 或恐慌吸收 (≥ × 2.0 但未收最低點)",
    "•一律以「已收盤」15m K 棒判定：成型中 K 棒只累積部分量能，會讓縮量條件偽陽性",
    "",
    "條件二",
    "•做市商 Put Wall / 負 Gamma 吸附牆密著截擊",
    "•現價距 Put Wall 須落在 −1.0% ~ +1.5% 區間 (允許微幅穿刺洗盤或提前掛單)",
    "•防禦厚度：該履約價絕對 GEX 曝險量級 ≥ $5,000,000",
    "•下檔緩衝雙邊界 (防 Liquidity Sweep)：停損距離 (現價−(PutWall−0.5×ATR₁₅ₘ))/現價",
    "  須落在 [0.5×ATR₁₅ₘ, 絕對 8%] 之內。量停損距離而非牆距——條件二本來",
    "  就要求密著 Put Wall，牆距趨近於零，量牆距會把理想進場點誤判為緩衝過窄",
    "•左側下界 0.5× 與停損墊片 0.5× 綁定：貼牆時停損距離恆等於 0.5×ATR₁₅ₘ，",
    "  下界一旦高於它，策略的設計中心點本身就永遠無法通過",
    "•⚠️ 厚度為 GEX 曝險量級代理值，非真實 Put OI 名目金額",
    "  (現有 GEX 資料源沒有逐履約價的 OI 名目金額欄位)",
    "",
    "條件三",
    "•下檔無恐慌踩踏斷崖 + 向上均值回歸空間",
    "•UOA 不得存在 strike < Put Wall 且 ratio > 1.2x、權利金 ≥ $200,000 的",
    "  PUT BTO 追空踩踏單 (防做市商破牆後進入負 Gamma 螺旋式拋售)",
    "•(min(Session VWAP, Gamma Flip) − 現價)/現價 ≥ 動態自適應波動率門檻",
    "  門檻 = max(2.2 × Risk, 1.5 × ATR₁D/現價, 3.5%)",
    "  Risk = (現價 − (PutWall − 0.5×ATR₁₅ₘ)) / 現價 (與引擎軌道一停損一致)",
    "•即「上方要留多少空間」由「下方實際要冒多少風險」反推，使 2.2:1 盈虧比",
    "  成為結構性保證；3.5% 僅為資料缺失時的絕對底線，非主要判據",
    "",
    "條件四",
    "•主力大額吸收認證 (二擇一即可)",
    "•PUT STO 護盤：DTE ≥ 14、ratio ≥ 1.0x、名目 ≥ $300,000、strike ≤ 現價",
    "•CALL BTO 長天期潛伏：DTE ≥ 30、ratio ≥ 0.8x、名目 ≥ $200,000",
    "•⚠️ 規格的「過去 4 小時內」時間窗無法實作：UOA 為選擇權鏈的當日累計快照",
    "  (volume 為當日累計、oi 為前一日收盤)，並非逐筆成交，故不帶時間戳",
    "",
    "條件五",
    "•總經流動性危機與財報黑天鵝安全閥",
    "•前四項須全數通過才會真正發動，否則列「⏭️ 略過」",
    "•重用右側條件五的財報緩衝期與大盤 Regime 子檢查 (門檻校準單一來源)",
    "•額外疊加 VIX 期限結構倒掛防禦：vts_ratio ≥ 1.10 直接判定未通過",
    "•任一外部資料抓取失敗一律 fail-safe 判定未通過 (不預設放行)",
    "",
    "條件六",
    "•Candidate 自身 Theta 磨底防禦",
    "•前五項須全數通過才會真正發動，否則列「⏭️ 略過」",
    "•最近效期 DTE ≥ 21：左側須承受底部震盪整理期，嚴禁 0~7 DTE 合約",
    "•IVR ≤ 50 建議輕度 ITM/ATM Call 買進",
    "•IVR > 50 強制改以 Bull Call Spread 或 Short Put (避免恐慌插針時高買隱波)",
    "```",
]


_ENTRY_RULES_DETAIL_SHORT: List[str] = [
    "```ansi",
    " 做空六重鐵律：結構破位追空／做市商負 Gamma 順勢助跌。",
    " ⚠️ 這是本系統唯一的空頭方向進場路徑——左側交易雖然技術定義與右側相反，",
    "    本質仍是做多 (Put Wall 底牆接刀、向上回歸空間)。",
    " 條件一～四為核心結構性門檻；條件五、六屬事件安全閥與 candidate 自身效期",
    " 檢查，僅於前置條件皆通過後才真正發動 (未發動時列「⏭️ 略過」)",
    " ----------------------------------",
    "條件一",
    "•結構性放量破位確認 (全面鏡像右側條件一)",
    "•15m 實體『陰線』收盤價 < Gamma Flip 估算門檻",
    "•若全鏈 Net GEX > 0 且無交叉點：做市商正 Gamma 買跌賣漲吸收波動，",
    "  結構性多頭直接判定未通過 (右側「Net GEX < 0 直接不通過」的鏡像)",
    "•若全鏈 Net GEX < 0 且無交叉點：改以跌破 Session VWAP − 0.5×ATR₁₅ₘ 確認",
    "•15m 成交量 ≥ 前20根均量 × 1.5 倍，且收盤須跌破 Session VWAP",
    "",
    "條件二",
    "•做市商負 Gamma 壓制頂牆完好",
    "•阻力牆強制約束在現價上方 (K > Spot)，即 Resistance Wall = argmax_{K > Spot} (Net GEX(K))",
    "•曝險低於 500k 薄紙牆門檻視為未偵測到有效頂牆 (未通過)",
    "•停損距離 (現價→頂牆+0.5×ATR₁₅ₘ) 落在 [2.5×ATR₁₅ₘ, 絕對 8%]：下界防停損",
    "  落在日內雜訊帶內遭反抽掃損，上界為絕對風險兜底",
    "  (空單停損設在頂牆上方，牆太近時任何反抽都會先掃穿停損)",
    "",
    "條件三",
    "•下行獲利空間充足 + 無主力大額接刀",
    "•UOA 不得存在 ratio > 1.2x、權利金 ≥ $200,000、strike ≤ 現價的 PUT STO",
    "  接刀單 (主力賣 PUT 為下跌提供接盤，拋壓路徑會被墊住)",
    "•[區間內做空] 現價 > Put Wall：(現價−PutWall)/現價 ≥ 動態門檻",
    "  門檻 = max(2.2 × Risk, 1.5 × ATR₁D/現價, 3.5%)",
    "  Risk = ((頂牆 + 0.5×ATR₁₅ₘ) − 現價)/現價 (停損設在頂牆上方)",
    "•[破位追空] 現價 ≤ Put Wall：改判次級節點空間 ≥ 2.0 × ATR₁D，",
    "  收復剛跌破的 Put Wall 即論點失效",
    "",
    "條件四",
    "•主力跨週期賣壓認證 (二擇一即可，皆須 DTE ≥ 7、ratio ≥ 0.8x、名目 ≥ $200,000)",
    "•PUT BTO 方向性押注：strike ≥ 現價 × 0.85 (排除深度價外低成本尾部樂透單)",
    "•CALL STO 上方築頂：strike ≥ 現價 (排除賣出價內 Call 的平倉單)",
    "",
    "條件五",
    "•總經與財報事件安全閥",
    "•完全重用右側條件五 (財報緩衝期 + 大盤 Regime)：二元事件風險對做多做空",
    "  同樣致命，空單遇上優於預期的財報會被跳空軋空",
    "•刻意不疊加 VIX 倒掛檢查：左側視倒掛為流動性凍結風險，對做空卻是順風，",
    "  兩者語意相反，硬套會方向性地誤殺",
    "",
    "條件六",
    "•Candidate 自身效期防禦與 IVR 結構分流",
    "•最近效期 DTE ≥ 14：破位後常有劇烈反抽回測，短天期會被 Theta/Vega 雙殺",
    "•IVR ≤ 50 建議 Long Put (輕度 OTM)",
    "•IVR > 50 強制改 Bear Call Spread (改當賣方收取恐慌溢價，避免 Vega 崩塌)",
    "```",
]


def create_entry_rules_embed(
    symbol: str,
    six_rule_passed: Optional[bool],
    six_rule_reasons: Optional[List[str]],
    trading_strategy: Optional[str] = None,
    dynamic_regime: Optional[str] = None,
    dynamic_regime_reason: Optional[str] = None,
    structure_directive: Optional[str] = None,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
    rr_ratio: Optional[float] = None,
) -> discord.Embed:
    """
    建構標的深度分析中心「🔐 進場鐵律檢核」頁籤 Embed。

    彙總呈現左側／做空六重鐵律的即時 Pass/Fail 判定。多頭建倉（右側）已改由
    多時間框架擠壓規則判定，改用 `squeeze_entry_embeds.create_squeeze_entry_embed`。

    :param trading_strategy: 呼叫端使用者當前 /settings 選擇的交易策略模式
        (RIGHT_SIDE/LEFT_SIDE/SHORT_SIDE/DYNAMIC)。僅 "DYNAMIC" 且提供 dynamic_regime 時
        才會額外渲染「當前 Regime」欄位；其餘情境維持既有輸出格式不變
        (向下相容既有呼叫端)。
    :param dynamic_regime: `regime_classifier.py::classify_dynamic_regime`
        回傳的 DynamicRegime 值 (例如 "REGIME_I_LEFT_CATCH")。
    :param dynamic_regime_reason: 對應的分類理由文字。
    :param structure_directive: 鐵律條件六依「當下市況」現算的建議合約天期與
        部位結構字串 (左側來自 left_side_entry.py 的 IVR 分流)。天期刻意設計為每輪重評都重算的輸出參數，而非在進場當下蓋章後
        永不更新的部位標籤。預設 None 時不渲染此欄位，向下相容既有呼叫端。
    :param entry_price / stop_loss / target / rr_ratio: 自選標的進場顧問推播
        (intraday_pipeline/entry_advisor.py) 附帶的價位建議。四者皆為 None (預設，
        `/x` 頁籤的既有呼叫端) 時不渲染「價位建議」欄位；只要任一有值即渲染，
        缺值者顯示 N/A。`rr_ratio` 為 None 代表分母 <= 0 或缺資料，不代表 0。
    """
    embed = NexusEmbed(
        title=f"🔐 {symbol} 進場鐵律檢核 (Entry Ironclad Rules)",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    # 本 Embed 只剩左側與做空兩套六重鐵律使用（多頭建倉已改由多時間框架擠壓
    # 規則判定，見 squeeze_entry_embeds.py）：SHORT_SIDE／DYNAMIC Regime V 走做空，
    # 其餘（LEFT_SIDE／DYNAMIC Regime I）走左側。
    if trading_strategy == "SHORT_SIDE" or (
        trading_strategy == "DYNAMIC" and dynamic_regime == "REGIME_V_BREAKDOWN_CHASE"
    ):
        _effective_gate = "SHORT"
        _gate_label = "做空交易：結構破位追空"
    else:
        _effective_gate = "LEFT"
        _gate_label = "左側交易：逆勢均值回歸 (做多)"

    if trading_strategy == "DYNAMIC" and dynamic_regime:
        _regime_display = {
            "REGIME_I_LEFT_CATCH": "🩹 Regime I：左側接刀態 (極端負乖離吸籌)",
            "REGIME_II_CHAOS_STANDASIDE": "⚪ Regime II：混沌泥淖態 (全系統休眠)",
            "REGIME_III_RIGHT_MOMENTUM": "🎯 Regime III：右側動能態 (結構突破伽馬擠壓)",
            "REGIME_III_B_TREND_CONTINUATION": (
                "🚀 Regime III-B：右側趨勢延續態 (持續站穩結構，非突破瞬間)"
            ),
            "REGIME_IV_STRUCTURAL_CAP_CRISIS": "🔴 Regime IV：結構封頂／危機態 (強制鎖定)",
            "REGIME_V_BREAKDOWN_CHASE": "🐻 Regime V：破位追空態 (負 Gamma 順勢助跌)",
        }.get(dynamic_regime, dynamic_regime)
        regime_lines = [
            "```ansi",
            f" ├─ 目前 Regime: {_regime_display}",
            f" └─ 分類理由: {dynamic_regime_reason or 'N/A'}",
            "```",
        ]
        _add_ansi_field_safely(
            embed, "🔀 當前 Regime (交易策略：動態調整)", regime_lines
        )

    six_lines = ["```ansi"]
    if six_rule_reasons:
        for reason in six_rule_reasons:
            six_lines.append(f" ├─ {reason}")
        overall_mark = (
            "\u001b[1;32m✅ 全數通過\u001b[0m"
            if six_rule_passed
            else "\u001b[1;31m❌ 未全數通過\u001b[0m"
        )
        six_lines.append(f" └─ 綜合判定: {overall_mark}")
    else:
        six_lines.append(" └─ 尚無足夠數據進行判定")
    six_lines.append("```")
    _add_ansi_field_safely(embed, f"🔐 進場六重鐵律 ({_gate_label})", six_lines)

    if structure_directive:
        _add_ansi_field_safely(
            embed,
            "🎯 建議進場結構 (依當下市況現算)",
            [
                "```ansi",
                f" ├─ {structure_directive}",
                " └─ 天期依當下 Call Wall 空間/IVR/財報距離每輪重算，非進場時固定標籤",
                "```",
            ],
        )

    if any(v is not None for v in (entry_price, stop_loss, target, rr_ratio)):

        def _px(v: Optional[float]) -> str:
            return f"${v:,.2f}" if v is not None else "N/A"

        _rr_text = f"{rr_ratio:.2f} : 1" if rr_ratio is not None else "N/A"
        _add_ansi_field_safely(
            embed,
            "💰 價位建議 (Entry / Stop / Target)",
            [
                "```ansi",
                f" ├─ 進場價: {_px(entry_price)}",
                f" ├─ 停損價: {_px(stop_loss)}",
                f" ├─ 目標價: {_px(target)}",
                f" └─ 盈虧比: {_rr_text}",
                "```",
            ],
        )

    detail_lines = (
        _ENTRY_RULES_DETAIL_SHORT
        if _effective_gate == "SHORT"
        else _ENTRY_RULES_DETAIL_LEFT
    )
    _add_ansi_field_safely(
        embed, f"📖 條件一～六判定說明 ({_gate_label}指標定義)", detail_lines
    )

    embed.set_footer(text="🔗 進場六重鐵律（左側／做空），僅供進場前快速核對。")
    return embed
