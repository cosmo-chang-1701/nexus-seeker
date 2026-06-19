"""Report-type Discord embed builders for Nexus Seeker.

This module groups all report-oriented embed construction functions, including:
- Portfolio risk settlement reports (create_portfolio_report_embed)
- Position transition suggestions (create_transition_suggestion_embed)
- Virtual Trading Room (VTR) performance stats (build_vtr_stats_embed)
- Quantitative scan reports (build_scan_report)
- Hedge re-entry recommendations (create_rehedge_embed)
- Davis Double Play detection reports (create_ddp_embed)
- Cheap volatility detection alerts (create_volatility_embed)
- Hedge effectiveness analysis fields (build_hedge_analysis_field)
- AI-generated post-market analysis (create_ai_analysis_embed)
- Next-day strategy reports (create_next_day_strategy_embed)

All embed rendering logic is centralized here; callers should only supply
pre-assembled data dicts / lists and receive a ready-to-send discord.Embed.
"""

import discord
import logging
import re

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _is_macro_report_marker,
    _chunk_text_blocks,
    _truncate_with_boundary,
    _safe_float,
    _pad_string,
)
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    _safe_embed_codeblock_value,
    _build_watchlist_style_panel,
    _report_embed_color,
    _parse_ai_report_sections,
    _parse_and_format_positions_table,
    get_ema_signal_ui,
)

logger = logging.getLogger(__name__)


def create_portfolio_report_embed(
    report_lines, hedge_analysis=None, survival_runway=None
):
    """
    將 check_portfolio_status_logic 產出的 report_lines 轉換為漂亮的 Discord Embed
    """
    # 處理完全為空的狀況
    if not report_lines:
        embed = discord.Embed(
            title="📊 Nexus Seeker 盤後風險結算報告",
            description="目前無持倉部位，亦無風險數據。\n\u200b",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")
        return embed

    # 1. 分割資料：將個別持倉與宏觀報告分開
    # 尋找分割點：🌐 【宏觀風險與資金水位報告】
    macro_index = -1
    for i, line in enumerate(report_lines):
        if _is_macro_report_marker(line):
            macro_index = i
            break

    # 2. 處理持倉細節與宏觀報告區隔
    if macro_index != -1:
        positions_list = [
            line.strip() for line in report_lines[:macro_index] if line.strip()
        ]
        macro_text = "\n".join(
            line.strip() for line in report_lines[macro_index:] if line.strip()
        )
    else:
        # 如果找不到宏觀報告區塊，將所有內容視為持倉明細
        positions_list = [line.strip() for line in report_lines if line.strip()]
        macro_text = "目前無宏觀風險數據。"

    # 使用 \n\n 分隔部位
    if positions_list:
        positions_text = _parse_and_format_positions_table(
            positions_list, survival_runway
        )
    else:
        positions_text = "目前無持倉部位。"

    # 二次確認，防止 macro_text 全空或只包含空白，Discord Embed value 必須為非空字串
    if not macro_text:
        macro_text = "無宏觀風險數據。"

    # 4. 判斷顏色：如果有任何 "🚨" 或 "🆘"，就用紅色，否則用藍色
    embed_color = discord.Color.blue()
    if "🚨" in macro_text or "🆘" in macro_text:
        embed_color = discord.Color.red()
    elif "⚠️" in macro_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="📊 Nexus Seeker 盤後風險結算報告",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 🚀 [Professional Investor] 財務生存跑道 (Priority Header)
    if survival_runway is not None:
        runway_text = (
            "無限 (收益已覆蓋支出)"
            if survival_runway >= 9999
            else f"`{survival_runway:,.1f}` 天"
        )
        embed.add_field(
            name="🏁 財務生存跑道 (Financial Runway)",
            value=f"```yaml\n預估剩餘天數: {runway_text}\n(基於現有現金儲備與 Theta 收益)\n```\n\u200b",
            inline=False,
        )

    # 🚀 欄位一：個別持倉細節
    if (
        positions_list
        and "positions_text" in locals()
        and positions_text != "目前無持倉部位。"
    ):
        # 如果包含財務摘要，將其分離出來單獨處理
        if "財務摘要 (Financial Summary)" in positions_text:
            table_part, summary_part = positions_text.split(
                "財務摘要 (Financial Summary)"
            )
            summary_text = "財務摘要 (Financial Summary)" + summary_part
            debit_match = re.search(r"Debit Cost.*:\s*(.*)", summary_text)
            credit_match = re.search(r"Credit Cash.*:\s*(.*)", summary_text)
            pnl_match = re.search(r"Unrealized PnL.*:\s*(.*)", summary_text)

            debit_cost_val = (
                _clean_ansi(debit_match.group(1).strip())
                if debit_match
                else "$0.00 USD"
            )
            credit_cash_val = (
                _clean_ansi(credit_match.group(1).strip())
                if credit_match
                else "$0.00 USD"
            )
            pnl_val_str = (
                _clean_ansi(pnl_match.group(1).strip()) if pnl_match else "$0.00 USD"
            )

            # Add inline fields for financial summary
            embed.add_field(
                name="💰 實質暴露 (Debit Cost)", value=debit_cost_val, inline=True
            )
            embed.add_field(
                name="💵 收取權利金 (Credit Cash)", value=credit_cash_val, inline=True
            )
            embed.add_field(
                name="📊 未實現損益 (Unrealized PnL)", value=pnl_val_str, inline=True
            )
        else:
            table_part = positions_text
            summary_text = ""

        # Remove any formatting wrappers
        table_part = table_part.strip().strip("`").strip()
        summary_text = summary_text.strip().strip("`").strip()

        # Split positions_text by double-newline to get individual position blocks
        blocks = [b.strip() for b in table_part.split("\n\n") if b.strip()]
        chunks = _chunk_text_blocks(blocks, max_len=1024)

        for i, chunk in enumerate(chunks):
            name = (
                f"📦 當前持倉明細 ({i+1}/{len(chunks)})"
                if len(chunks) > 1
                else "📦 當前持倉明細"
            )
            embed.add_field(name=name, value=chunk, inline=False)
    else:
        embed.add_field(name="📦 當前持倉明細", value="目前無持倉部位。", inline=False)

    # 🚀 欄位二：全帳戶宏觀風險與對沖指令 (核心！)
    macro_text = _safe_embed_field_value(macro_text, "無宏觀風險數據。")
    embed.add_field(name="🛡️ 風控管線評估與對沖決策", value=macro_text, inline=False)

    # 🚀 欄位三：對沖有效性分析 (新增)
    if hedge_analysis:
        build_hedge_analysis_field(embed, hedge_analysis)

        # 如果有 Tau 係數更新資訊，則額外提示
        dynamic_tau = getattr(embed, "dynamic_tau", None) or (
            hedge_analysis.get("dynamic_tau")
            if isinstance(hedge_analysis, dict)
            else None
        )
        if dynamic_tau is not None:
            dynamic_tau = _safe_float(dynamic_tau, 1.0)
            embed.add_field(
                name="🧬 STHE 自動優化狀態",
                value=f"目前對沖調教因子 τ: `{dynamic_tau:.2f}`",
                inline=False,
            )

    embed.set_footer(text="Argo Risk Engine v2.6 | 基準標的: SPY")

    return embed


def create_transition_suggestion_embed(data: Dict[str, Any]) -> discord.Embed:
    """建構倉位演進建議 (Transition Suggestion) 的 Discord Embed"""
    sym = data["symbol"]
    pnl_pct = data["pnl_pct"] * 100
    pnl_usd = data["pnl_usd"]
    res = data["transition_result"]
    stock_p = data["stock_price"]

    embed = discord.Embed(
        title=f"🔄 倉位演進建議 | {sym}",
        description="偵測到投機部位獲利豐厚，建議轉向「收租模式」以鎖定長期收益。",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    # 目前獲利狀況
    embed.add_field(
        name="📈 目前獲利狀況",
        value=f"獲利率: `{pnl_pct:+.1f}%` / `${pnl_usd:,.2f}`\n\u200b",
        inline=True,
    )

    # 演進後效益
    embed.add_field(
        name="🧬 演進後預期效益",
        value=(
            f"調整後持股成本: **`${res.adjusted_cost_basis:.2f}`**\n"
            f"相對現價折讓: `{res.capital_efficiency_gain:.1f}%` (現價: `${stock_p:.2f}`)\n"
            f"預期 CC 年化報酬 (AROC): `{res.projected_aroc:.1f}%` (@${res.cc_strike})\n\u200b"
        ),
        inline=False,
    )

    # 操作指令建議
    embed.add_field(
        name="🛠️ 建議操作路徑",
        value=(
            f"1. **Synthetic Exit**: 獲利平倉目前 Option 部位。\n"
            f"2. **Core Equity**: 使用獲利抵扣，購入 100 股現股。\n"
            f"3. **Covered Call**: 賣出 `${res.cc_strike}` Call 啟動收租。\n\u200b"
        ),
        inline=False,
    )

    embed.set_footer(text="Nexus Position Evolution Engine | 專業投資者建議")
    return embed


def build_vtr_stats_embed(
    user_name: str, stats: dict, attribution_lines: Optional[List[str]] = None
) -> discord.Embed:
    """
    建構 VTR 績效統計 Embed 面板，含對沖效能歸因。
    """
    # 根據勝率決定顏色
    win_rate = stats.get("win_rate", 0)
    if win_rate >= 60:
        color = 0x2ECC71  # 綠色 (Success)
        status_icon = "🟢"
    elif win_rate >= 40:
        color = 0xF1C40F  # 黃色 (Warning)
        status_icon = "🟡"
    else:
        color = 0xE74C3C  # 紅色 (Danger)
        status_icon = "🔴"

    embed = discord.Embed(
        title="📈 Nexus Seeker | 虛擬交易室 (VTR) 績效總結",
        description=f"{status_icon} 使用者: **{user_name}** 的系統歸因分析",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # 績效統計指標 table
    pnl = stats.get("total_pnl", 0.0)
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    avg_pnl = stats.get("avg_pnl", 0.0)
    avg_pnl_str = f"+${avg_pnl:.2f}" if avg_pnl >= 0 else f"-${abs(avg_pnl):.2f}"

    if win_rate >= 60:
        win_color = " \033[0;32m"
    elif win_rate >= 40:
        win_color = " \033[0;33m"
    else:
        win_color = " \033[0;31m"

    pnl_color = " \033[0;32m" if pnl >= 0 else " \033[0;31m"
    avg_pnl_color = " \033[0;32m" if avg_pnl >= 0 else " \033[0;31m"

    vtr_lines = ["```ansi"]
    headers = ["績效指標", "數據值"]
    widths = [14, 18]
    vtr_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(headers, widths)))
    vtr_lines.append("-" * (sum(widths) + 3))

    vtr_lines.append(
        f"{_pad_string('總結算次數', widths[0])} | {_pad_string(str(stats.get('total_trades', 0)), widths[1])}"
    )

    win_rate_val = f"{win_rate}%"
    vtr_lines.append(
        f"{_pad_string('勝率', widths[0])} | {_pad_string(win_rate_val, widths[1]).replace(win_rate_val, f'{win_color}{win_rate_val}\033[0m')}"
    )

    vtr_lines.append(
        f"{_pad_string('累計總損益', widths[0])} | {_pad_string(pnl_str, widths[1]).replace(pnl_str, f'{pnl_color}{pnl_str}\033[0m')}"
    )

    vtr_lines.append(
        f"{_pad_string('平均單筆損益', widths[0])} | {_pad_string(avg_pnl_str, widths[1]).replace(avg_pnl_str, f'{avg_pnl_color}{avg_pnl_str}\033[0m')}"
    )
    vtr_lines.append("```")

    embed.add_field(name="📊 VTR 績效指標", value="\n".join(vtr_lines), inline=False)

    # 對沖歸因報告
    if attribution_lines:
        attr_text = "\n".join(attribution_lines)
        # 截斷過長內容
        if len(attr_text) > 1024:
            attr_text = attr_text[:1021] + "..."
        embed.add_field(name="🛡️ 對沖效能與自我進化", value=attr_text, inline=False)

    embed.set_footer(text="Nexus Sandbox Engine | 數據包含已平倉之虛擬部位")

    return embed


def build_scan_report(result: Dict[str, Any]):
    """
    量化掃描報告Embed，整合 Greeks, NRO 與 EMA 訊號。
    """
    ai_decision = result.get("ai_decision", "SKIP")
    color = (
        0x2ECC71
        if ai_decision == "APPROVE"
        else (0xE74C3C if ai_decision == "VETO" else 0x3498DB)
    )

    embed = discord.Embed(
        title=f"📡 量化掃描報告: {result['symbol']}",
        description=f"策略: `{result.get('strategy', 'N/A')}` | 履約價: `${result.get('strike', 'N/A')}` | 到期日: `{result.get('target_date', 'N/A')}`",
        color=color,
    )

    # 1. Greeks 區塊 (從 result 中提取)
    g_delta = result.get("delta", 0.0)
    g_theta = result.get("theta", 0.0)
    g_gamma = result.get("gamma", 0.0)
    g_iv = result.get("iv", 0.0)

    greeks_lines = ["```ansi"]
    g_headers = ["希臘字母", "數據值"]
    g_widths = [14, 18]
    greeks_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(g_headers, g_widths))
    )
    greeks_lines.append("-" * (sum(g_widths) + 3))

    greeks_lines.append(
        f"{_pad_string('Delta', g_widths[0])} | {_pad_string(f'{g_delta:+.3f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('Theta', g_widths[0])} | {_pad_string(f'{g_theta:+.4f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('Gamma', g_widths[0])} | {_pad_string(f'{g_gamma:+.6f}', g_widths[1])}"
    )
    greeks_lines.append(
        f"{_pad_string('IV (隱含波動率)', g_widths[0])} | {_pad_string(f'{g_iv:.1%}', g_widths[1])}"
    )
    greeks_lines.append("```")
    greeks_info = "\n".join(greeks_lines)
    embed.add_field(name="🧬 Greeks 希臘字母", value=greeks_info, inline=False)

    # 2. NRO 風控區塊
    safe_qty = result.get("safe_qty", 0)
    projected = result.get("projected_exposure_pct", 0.0)
    risk_limit = result.get("risk_limit", 15.0)

    proj_color = " \033[0;31m" if projected > risk_limit else " \033[0;32m"
    proj_str = f"{projected:+.1f}%"

    nro_lines = ["```ansi"]
    n_headers = ["風控項目", "數據值"]
    n_widths = [14, 18]
    nro_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(n_headers, n_widths)))
    nro_lines.append("-" * (sum(n_widths) + 3))

    nro_lines.append(
        f"{_pad_string('建議口數', n_widths[0])} | {_pad_string(f'{safe_qty} 口', n_widths[1])}"
    )
    nro_lines.append(
        f"{_pad_string('預期總曝險', n_widths[0])} | {_pad_string(proj_str, n_widths[1]).replace(proj_str, f'{proj_color}{proj_str}\033[0m')}"
    )
    nro_lines.append(
        f"{_pad_string('曝險風控紅線', n_widths[0])} | {_pad_string(f'{risk_limit:.1f}%', n_widths[1])}"
    )
    nro_lines.append("```")
    nro_info = "\n".join(nro_lines)
    embed.add_field(name="🛡️ NRO 風控判定", value=nro_info, inline=False)

    # 3. 🚀 整合 EMA 訊號區塊
    ema_ui = get_ema_signal_ui(result.get("ema_signals", []))
    embed.add_field(name="📈 趨勢與指標動態", value=ema_ui, inline=False)

    # 4. 加上宏觀背景燈號 (VIX/Oil)
    vix = result.get("macro_vix", result.get("vix", 0))
    oil = result.get("macro_oil", result.get("oil", 0))
    vix_status = "🔴" if vix > 25 else "🟢"

    embed.set_footer(
        text=f"環境感知: VIX {vix} {vix_status} | WTI ${oil} | 基準 SPY: ${result.get('spy_price', 0):.1f}"
    )

    return embed


def create_rehedge_embed(rehedge_info: Dict[str, Any]) -> discord.Embed:
    """
    建構「自動避險回補建議」的 Discord Embed 面板。
    """
    priority = rehedge_info.get("priority", "NORMAL")
    color = 0xF1C40F if priority == "NORMAL" else 0xE74C3C  # 黃色或紅色

    symbol = rehedge_info.get("symbol", "SPY")
    suggested_qty = rehedge_info.get("suggested_spy_qty", 0)
    reason = rehedge_info.get("reason", "偵測到曝險異常或市場轉弱")

    embed = discord.Embed(
        title="🛡️ 防禦啟動：自動避險回補建議",
        color=color,
        description=f"標的: **{symbol}**",
    )

    embed.add_field(name="觸發原因", value=f"```\n{reason}\n```", inline=False)

    action_val = (
        f"賣出 (Short) `{suggested_qty}` 股 SPY"
        if suggested_qty > 0
        else f"買入 (Long) `{abs(suggested_qty)}` 股 SPY"
    )
    embed.add_field(name="建議動作", value=action_val, inline=True)

    embed.set_footer(text="提示：當前趨勢已走弱，掛回避險可鎖定現有獲利。")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_ddp_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構 Davis Double Play (DDP) 預警 Embed"""
    sym = report["symbol"]
    curr_pe = report["current_pe"]
    pe_mean = report["pe_mean_3y"]
    eps_growth = report["eps_growth"] * 100
    rev_accel = "加速 (Accelerating)" if report["rev_accel"] else "穩定 (Stable)"
    score = report["confidence_score"]

    # 計算 P/E 均值回歸空間 (預期漲幅)
    pe_upside = (pe_mean / curr_pe - 1) * 100 if curr_pe > 0 else 0

    embed = discord.Embed(
        title=f"🌌 Nexus 戴維斯雙擊預警: {sym}",
        description="偵測到標的符合 **Davis Double Play (DDP)** 條件：盈餘增長與估值擴張的雙重共振。",
        color=0x00FF7F,  # SpringGreen
        timestamp=datetime.now(timezone.utc),
    )

    ddp_lines = ["```ansi"]
    headers = ["DDP 量化指標", "數據值"]
    widths = [24, 20]
    ddp_lines.append(" | ".join(_pad_string(h, w) for h, w in zip(headers, widths)))
    ddp_lines.append("-" * (sum(widths) + 3))

    ddp_lines.append(
        f"{_pad_string('目前本益比 (TTM P/E)', widths[0])} | {_pad_string(f'{curr_pe:.2f}', widths[1])}"
    )
    ddp_lines.append(
        f"{_pad_string('3 年本益比均值 (3Y Mean)', widths[0])} | {_pad_string(f'{pe_mean:.2f}', widths[1])}"
    )

    upside_str = f"+{pe_upside:.1f}%" if pe_upside >= 0 else f"{pe_upside:.1f}%"
    ddp_lines.append(
        f"{_pad_string('本益比估值回歸空間', widths[0])} | {_pad_string(upside_str, widths[1]).replace(upside_str, f'\033[0;32m{upside_str}\033[0m' if pe_upside >= 0 else f'\033[0;31m{upside_str}\033[0m')}"
    )

    growth_str = f"{eps_growth:+.1f}%"
    ddp_lines.append(
        f"{_pad_string('預估 EPS 成長率', widths[0])} | {_pad_string(growth_str, widths[1]).replace(growth_str, f'\033[0;32m{growth_str}\033[0m' if eps_growth >= 0 else f'\033[0;31m{growth_str}\033[0m')}"
    )

    ddp_lines.append(
        f"{_pad_string('營收加速狀態', widths[0])} | {_pad_string(rev_accel, widths[1])}"
    )

    score_str = f"{score:.0f}/100"
    ddp_lines.append(
        f"{_pad_string('DDP 信心評分', widths[0])} | {_pad_string(score_str, widths[1]).replace(score_str, f'\033[0;32m{score_str}\033[0m' if score >= 70 else (f'\033[0;33m{score_str}\033[0m' if score >= 50 else f'\033[0;31m{score_str}\033[0m'))}"
    )
    ddp_lines.append("```")

    embed.add_field(name="📊 DDP 量化指標", value="\n".join(ddp_lines), inline=False)

    # 邏輯解釋
    logic_text = (
        f"1. **盈餘動能**: YoY 成長率 `{eps_growth:.1f}%` 超過 15% 門檻。\n"
        f"2. **估值壓縮**: 目前 P/E 處於 3 年歷史低位 (25% 分位數以下)。\n"
        f"3. **前瞻預期**: Forward P/E `{report['forward_pe']:.2f}` 低於目前 TTM P/E。\n"
        f"4. **動能確認**: 營收成長較前一週期加速。"
    )
    embed.add_field(name="🧐 量化篩選邏輯", value=logic_text, inline=False)

    embed.set_footer(text="Nexus Quantitative Research | 戴維斯雙擊引擎 v1.0")
    return embed


def create_volatility_embed(report: Dict[str, Any]) -> discord.Embed:
    """建構波動率優勢偵測 (Cheap Volatility) 預警 Embed"""
    sym = report["symbol"]
    price = report["price"]
    iv = report["iv"]
    iv_p = report["iv_p"]
    hv = report["hv"]
    status = report["status"]
    strategy = report["strategy"]
    trigger_logic = report["trigger_logic"]
    days_to_earnings = report["days_to_earnings"]
    stop_loss = report["stop_loss"]
    daily_theta = report["daily_theta"]
    runway_impact = report["runway_impact"]

    # 決定顏色 (Buy Signal = Green, Watchlist Alert = Yellow)
    color = 0x00FF00 if status == "波動率極低" else 0xFFFF00

    embed = discord.Embed(
        title="📊 Nexus Seeker | 波動率優勢偵測",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Field 1: [Symbol] 戰略評估
    v1_headers = ["評估指標", "數據值"]
    v1_widths = [20, 20]
    val1_lines = ["```ansi"]
    val1_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(v1_headers, v1_widths))
    )
    val1_lines.append("-" * (sum(v1_widths) + 3))
    val1_lines.append(
        f"{_pad_string('當前價格 (Price)', v1_widths[0])} | {_pad_string(f'${price:.2f}', v1_widths[1])}"
    )
    val1_lines.append(
        f"{_pad_string('IV / IV Percentile', v1_widths[0])} | {_pad_string(f'{iv}% ({iv_p}%)', v1_widths[1])}"
    )
    val1_lines.append(
        f"{_pad_string('HV (252-day)', v1_widths[0])} | {_pad_string(f'{hv}%', v1_widths[1])}"
    )
    status_colored = (
        f"\033[0;32m{status}\033[0m"
        if status == "波動率極低"
        else f"\033[0;33m{status}\033[0m"
    )
    val1_lines.append(
        f"{_pad_string('目前狀態 (Status)', v1_widths[0])} | {_pad_string(status, v1_widths[1]).replace(status, status_colored)}"
    )
    val1_lines.append("```")
    val1 = "\n".join(val1_lines)
    embed.add_field(name=f"🔍 {sym} 戰略評估", value=val1, inline=False)

    # Field 2: 買入時機分析
    catalyst = (
        f"距離財報 {days_to_earnings} 天" if days_to_earnings <= 90 else "無近期財報"
    )
    val2_lines = ["```ansi"]
    val2_lines.append(f"建議策略 (Strategy): {strategy}")
    val2_lines.append(f"觸發邏輯 (Trigger) : {trigger_logic}")
    val2_lines.append(f"催化因子 (Catalyst): {catalyst}")
    val2_lines.append("```")
    val2 = "\n".join(val2_lines)
    embed.add_field(name="🎯 買入時機分析", value=val2, inline=False)

    # Field 3: 風險管理 (NRO)
    v3_headers = ["風控指標", "評估數據"]
    v3_widths = [22, 18]
    val3_lines = ["```ansi"]
    val3_lines.append(
        " | ".join(_pad_string(h, w) for h, w in zip(v3_headers, v3_widths))
    )
    val3_lines.append("-" * (sum(v3_widths) + 3))

    val3_lines.append(
        f"{_pad_string('建議停損 (Stop Loss)', v3_widths[0])} | {_pad_string(f'${stop_loss:.2f}', v3_widths[1])}"
    )

    theta_str = f"-${daily_theta:.2f}/day"
    val3_lines.append(
        f"{_pad_string('Theta 每日損耗', v3_widths[0])} | {_pad_string(theta_str, v3_widths[1]).replace(theta_str, f'\033[0;31m{theta_str}\033[0m')}"
    )

    runway_str = f"{runway_impact} 天"
    val3_lines.append(
        f"{_pad_string('預估跑道影響', v3_widths[0])} | {_pad_string(runway_str, v3_widths[1])}"
    )
    val3_lines.append("```")
    val3 = "\n".join(val3_lines)
    embed.add_field(name="🛡️ 風險管理 (NRO)", value=val3, inline=False)

    embed.set_footer(text="Nexus Volatility Strategist Agent v1.0 | 專注廉價波動率偵測")
    return embed


def build_hedge_analysis_field(embed, analysis):
    """
    在 embed 中加入對沖分析區塊。
    """
    if not isinstance(analysis, dict):
        embed.add_field(
            name="🛡️ 對沖有效性診斷",
            value=_safe_embed_field_value(
                "目前無法取得對沖分析資料。", "目前無法取得對沖分析資料。"
            ),
            inline=False,
        )
        return

    status = str(analysis.get("status", "UNKNOWN"))
    status_emoji = "✅" if status == "OPTIMAL" else "⚠️"
    effectiveness = _safe_float(analysis.get("effectiveness", 0.0), 0.0)
    alpha_contribution = _safe_float(analysis.get("alpha_contribution", 0.0), 0.0)
    hedge_contribution = _safe_float(analysis.get("hedge_contribution", 0.0), 0.0)
    hedge_ratio = _safe_float(analysis.get("hedge_ratio", 0.0), 0.0)
    net_pnl = _safe_float(analysis.get("net_pnl", 0.0), 0.0)

    # 決定有效性評價
    if effectiveness >= 0.8:
        eff_text = "🎯 精準"
    elif effectiveness >= 0.6:
        eff_text = "⚖️ 適中"
    else:
        eff_text = "🌪️ 偏差"

    content = (
        f"🔹 **個股 Alpha 損益**: `${alpha_contribution:,.2f}`\n"
        f"🔸 **對沖 Beta 損益**: `${hedge_contribution:,.2f}`\n"
        f"📊 **對沖比率 (HR)**: `{hedge_ratio:.2%}` {status_emoji}\n"
        f"🧩 **對沖有效性 (ES)**: `{effectiveness:.2%}` ({eff_text})\n"
        f"🏁 **最終淨損益**: **`${net_pnl:,.2f}`**"
    )

    embed.add_field(
        name="🛡️ 對沖有效性診斷",
        value=_safe_embed_field_value(content, "對沖分析資料不足。"),
        inline=False,
    )


def create_ai_analysis_embed(
    ai_report_text: str, title: str = "📊 Nexus Seeker 盤後 AI 深度分析"
) -> discord.Embed:
    """
    將 AI 產出的盤後深度分析轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed = discord.Embed(
        title=title,
        color=_report_embed_color(ai_report_text),
        timestamp=datetime.now(timezone.utc),
    )

    sections = _parse_ai_report_sections(ai_report_text)
    if sections:
        for header, content in sections:
            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
    else:
        embed.description = _truncate_with_boundary(ai_report_text, 4000)

    embed.set_footer(text="Nexus Seeker AI Analyst v1.0 | 智慧投研管線")
    return embed


def create_next_day_strategy_embed(strategy_text: str) -> discord.Embed:
    """
    將次日策略制定報告轉換為 Discord Embed 格式。
    參考盤後風險結算報告風格。
    """
    embed_color = discord.Color.blue()
    if "🚨" in strategy_text or "🆘" in strategy_text:
        embed_color = discord.Color.red()
    elif "⚠️" in strategy_text:
        embed_color = discord.Color.orange()

    embed = discord.Embed(
        title="🎯 Nexus Seeker 次日策略制定",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # 策略報告通常包含 VIX 資訊與戰術建議
    # 嘗試切分 **標題**:
    sections = re.split(r"\n(?=\*\*[^*]+\*\*[:：])", strategy_text)

    if len(sections) > 1:
        # 第一部分可能是時間標題行，放在 description
        first_part = sections[0].strip()
        # 移除時間標題與分隔線
        first_part = re.sub(r"\*\*.*次日策略制定\*\*", "", first_part)
        first_part = re.sub(r"-{10,}", "", first_part).strip()

        if first_part:
            embed.description = first_part

        # 處理後續欄位
        for section in sections[1:]:
            section = section.strip()
            if not section:
                continue

            lines = section.split("\n", 1)
            header = (
                lines[0].replace("**", "").replace(":", "").replace("：", "").strip()
            )
            content = lines[1].strip() if len(lines) > 1 else "無內容"

            panel = _build_watchlist_style_panel(
                f"{header} (Next-Day Strategy)",
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
        # 移除標題行後放入欄位 (全部內容也用文字面板呈現)
        clean_text = re.sub(r"\*\*.*次日策略制定\*\*", "", strategy_text)
        clean_text = re.sub(r"-{10,}", "", clean_text).strip()
        embed.description = "以下為次日策略摘要，建議依序閱讀各區塊。"
        panel = _build_watchlist_style_panel(
            "🎯 次日策略摘要 (Next-Day Summary)",
            clean_text,
            width=45,
            empty_msg="無詳細資訊",
        )
        embed.add_field(
            name="🎯 次日策略摘要",
            value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
            inline=False,
        )

    embed.set_footer(text="Nexus Seeker Strategy Engine v1.0 | 戰鬥階級系統")
    return embed
