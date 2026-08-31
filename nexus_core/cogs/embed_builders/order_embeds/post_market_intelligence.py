"""盤後綜合風險與 AI 策略報告 Embed 建構函式。"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _safe_float,
    _is_macro_report_marker,
    _is_correlation_report_marker,
    _chunk_text_blocks,
    _format_macro_report_ansi,
)
from cogs.embed_builders._embed_helpers import (
    _parse_and_format_positions_table,
    split_embed_by_fields,
)


def _parse_post_market_ai_commentary(ai_commentary: str) -> dict[str, str]:
    patterns = {
        "market": r"(?:#+\s*)?\*?\*?(?:1\.\s*)?📊\s*多空大盤交叉驗證解讀\*?\*?",
        "risk": r"(?:#+\s*)?\*?\*?(?:2\.\s*)?⚠️\s*潛在陷阱與風險提示\*?\*?",
        "strategy": r"(?:#+\s*)?\*?\*?(?:3\.\s*)?🛡️\s*高勝率交易策略推薦\*?\*?",
    }

    indices = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, ai_commentary)
        if match:
            indices[key] = (match.start(), match.end())

    if not indices:
        return {}

    sorted_keys = sorted(indices.keys(), key=lambda k: indices[k][0])
    result = {}
    for i, key in enumerate(sorted_keys):
        start_idx = indices[key][1]
        if i + 1 < len(sorted_keys):
            next_key = sorted_keys[i + 1]
            end_idx = indices[next_key][0]
            content = ai_commentary[start_idx:end_idx].strip()
        else:
            content = ai_commentary[start_idx:].strip()

        content = re.sub(r"^[:：\s\-\*#]+", "", content).strip()
        content = re.sub(r"[\s#]+$", "", content).strip()
        result[key] = content

    return result


def _format_to_target_center_style(text: str) -> str:
    if not text:
        return ""

    raw_lines = text.split("\n")
    cleaned_lines = []

    for line in raw_lines:
        line_str = line.strip()
        if not line_str:
            continue

        cleaned = re.sub(r"^[\-\*\•\d+\.\s]+", "", line_str).strip()
        cleaned = _clean_ansi(cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)

    if not cleaned_lines:
        return text

    formatted_lines = []
    for idx, line in enumerate(cleaned_lines):
        prefix = " ├─ " if idx < len(cleaned_lines) - 1 else " └─ "
        formatted_lines.append(f"{prefix}{line}")

    return "\n".join(formatted_lines)


def _format_to_target_center_style_with_title(title: str, text: str) -> str:
    if not text:
        return "```ansi\n • 暫無數據\n```"

    raw_lines = text.split("\n")
    cleaned_lines = []

    for line in raw_lines:
        line_str = line.strip()
        if not line_str:
            continue

        cleaned = re.sub(r"^[\-\*\•\d+\.\s]+", "", line_str).strip()
        cleaned = _clean_ansi(cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)

    if not cleaned_lines:
        return "```ansi\n • 暫無數據\n```"

    formatted_lines = [f" {title}"]
    for line in cleaned_lines:
        formatted_lines.append(f" • {line}")

    content = "\n".join(formatted_lines)
    return f"```ansi\n{content}\n```"


def build_post_market_intelligence_embed(
    report_lines: List[str],
    hedge_analysis: Optional[Dict[str, Any]] = None,
    survival_runway: Optional[float] = None,
    sectors_data: Optional[List[Dict[str, Any]]] = None,
    ai_commentary: Optional[str] = None,
) -> List[discord.Embed]:
    """建立盤後綜合風險與 AI 策略報告 Embed (📋 盤後綜合風險與 AI 策略報告)"""
    embed_color = discord.Color.blue()
    if ai_commentary:
        if "🚨" in ai_commentary or "🆘" in ai_commentary:
            embed_color = discord.Color.red()
        elif "⚠️" in ai_commentary:
            embed_color = discord.Color.orange()

    embed = NexusEmbed(
        title="📋 報告：盤後綜合風險與 AI 策略",
        description="由 Nexus Seeker 風險引擎生成之每日風險控制結算報告。",
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    if survival_runway is not None:
        runway_text = (
            "\u001b[1;32m無限 (收益已覆蓋支出)\u001b[0m"
            if survival_runway >= 9999
            else f"\u001b[1;32m{survival_runway:,.1f} 天\u001b[0m"
        )
        embed.description = (
            "```ansi\n"
            " 🏁 財務生存跑道 (Financial Runway)\n"
            f" • 預估剩餘天數: {runway_text}\n"
            " • 計算基準: 基於現有現金儲備與 Theta 收益\n"
            "```"
        )
    else:
        embed.description = None

    positions_list = []
    debit_cost_val = "$0.00 USD"
    credit_cash_val = "$0.00 USD"
    pnl_val_str = "$0.00 USD"

    correlation_text: Optional[str] = None
    if report_lines:
        macro_index = -1
        for i, line in enumerate(report_lines):
            if _is_macro_report_marker(line):
                macro_index = i
                break
        if macro_index != -1:
            positions_list = [
                line.strip() for line in report_lines[:macro_index] if line.strip()
            ]
            correlation_index = -1
            for i in range(macro_index + 1, len(report_lines)):
                if _is_correlation_report_marker(report_lines[i]):
                    correlation_index = i
                    break
            if correlation_index != -1:
                macro_text = "\n".join(
                    line.strip()
                    for line in report_lines[macro_index:correlation_index]
                    if line.strip()
                )
                correlation_text = "\n".join(
                    line.strip()
                    for line in report_lines[correlation_index:]
                    if line.strip()
                )
            else:
                macro_text = "\n".join(
                    line.strip() for line in report_lines[macro_index:] if line.strip()
                )
        else:
            positions_list = [line.strip() for line in report_lines if line.strip()]
            macro_text = "目前無宏觀風險數據。"
        if positions_list:
            positions_text = _parse_and_format_positions_table(
                positions_list, survival_runway
            )
        else:
            positions_text = "目前無持倉部位。"
    else:
        positions_text = "目前無持倉部位。"
        macro_text = "目前無宏觀風險數據。"

    if positions_list and positions_text and positions_text != "目前無持倉部位。":
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
            positions_text = table_part.strip()

        debit_cost_clean = debit_cost_val.replace("`", "").replace("**", "").strip()
        credit_cash_clean = credit_cash_val.replace("`", "").replace("**", "").strip()
        pnl_val_clean = pnl_val_str.replace("`", "").replace("**", "").strip()
    else:
        debit_cost_clean = "$0.00 USD"
        credit_cash_clean = "$0.00 USD"
        pnl_val_clean = "$0.00 USD"

    pnl_color = (
        "\u001b[1;32m"
        if (
            "+" in pnl_val_clean
            or ("-" not in pnl_val_clean and pnl_val_clean != "$0.00 USD")
        )
        else "\u001b[1;31m"
    )
    if pnl_val_clean == "$0.00 USD":
        pnl_color = "\u001b[1;37m"

    fin_lines = [
        "```ansi",
        f" • 實質暴露 (Debit Cost)   : {debit_cost_clean}",
        f" • 收取權利金 (Credit Cash) : {credit_cash_clean}",
        f" • 未實現損益 (Unrealized)  : {pnl_color}{pnl_val_clean}\u001b[0m",
        "```",
    ]
    embed.add_field(
        name="💰 資金與實質暴露 (Financial Summary)",
        value="\n".join(fin_lines),
        inline=False,
    )

    if positions_list and positions_text and positions_text != "目前無持倉部位。":
        positions_text = positions_text.strip().strip("`").strip()
        blocks = [b.strip() for b in positions_text.split("\n\n") if b.strip()]
        transformed_blocks = []
        for block in blocks:
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            heading = lines[0]
            heading_clean = heading.replace("**", "").replace("🔹 ", "").strip()
            heading_colored = f"\u001b[1;36m{heading_clean}\u001b[0m"

            detail_lines = []
            for line in lines[1:]:
                cleaned_line = re.sub(r"^[\-\*\•\s]+", "", line).strip()
                cleaned_line = cleaned_line.replace("`", "").replace("*", "")
                detail_lines.append(cleaned_line)

            ansi_lines = [f" {heading_colored}"]
            for dl in detail_lines:
                ansi_lines.append(f" • {dl}")
            transformed_blocks.append("\n".join(ansi_lines))

        chunks = _chunk_text_blocks(transformed_blocks, max_len=1000)
        for i, chunk in enumerate(chunks):
            field_name = (
                f"📊 持倉明細 (Positions) ({i+1}/{len(chunks)})"
                if len(chunks) > 1
                else "📊 持倉明細 (Positions)"
            )
            embed.add_field(
                name=field_name, value=f"```ansi\n{chunk}\n```", inline=False
            )
    else:
        runway_info = (
            "無限 (零負擔運作)"
            if (survival_runway is not None and survival_runway >= 9999)
            else f"{survival_runway:,.1f} 天"
            if survival_runway is not None
            else "良好"
        )
        empty_lines = [
            "```ansi",
            " \u001b[1;33m💡 【帳戶處於 100% 現金防禦/觀望狀態】\u001b[0m",
            " • 🛡️ 實質暴露: $0.00 USD ｜ 無下行 Delta 曝險",
            f" • 🏁 財務生存天數: {runway_info}",
            " • 🧭 行動建議: 可使用 `/x` 執行即時量化雷達，捕捉超跌磁吸與突破標的。",
            "```",
        ]
        embed.add_field(
            name="📊 持倉明細 (Positions)",
            value="\n".join(empty_lines),
            inline=False,
        )

    # ── 🌐 【宏觀風險與資金水位報告】 ──
    macro_formatted = _format_macro_report_ansi(macro_text)
    macro_chunks = _chunk_text_blocks([macro_formatted], max_len=1000)
    for i, chunk in enumerate(macro_chunks):
        field_name = (
            f"🌐 【宏觀風險與資金水位報告】 ({i+1}/{len(macro_chunks)})"
            if len(macro_chunks) > 1
            else "🌐 【宏觀風險與資金水位報告】"
        )
        embed.add_field(name=field_name, value=f"```ansi\n{chunk}\n```", inline=False)

    # ── 🕸️ 【非系統性集中風險 (板塊連動性)】 ──
    if correlation_text:
        correlation_formatted = _format_macro_report_ansi(correlation_text)
        correlation_chunks = _chunk_text_blocks([correlation_formatted], max_len=1000)
        for i, chunk in enumerate(correlation_chunks):
            field_name = (
                f"🕸️ 【非系統性集中風險 (板塊連動性)】 ({i+1}/{len(correlation_chunks)})"
                if len(correlation_chunks) > 1
                else "🕸️ 【非系統性集中風險 (板塊連動性)】"
            )
            embed.add_field(
                name=field_name, value=f"```ansi\n{chunk}\n```", inline=False
            )

    # ── 🛡️ 對沖績效歸因 (Hedge Attribution) [Dynamic Gating] ──
    if isinstance(hedge_analysis, dict) and hedge_analysis:
        ha_net_pnl = _safe_float(hedge_analysis.get("net_pnl"), 0.0)
        ha_alpha_pnl = _safe_float(hedge_analysis.get("alpha_contribution"), 0.0)
        ha_hedge_pnl = _safe_float(hedge_analysis.get("hedge_contribution"), 0.0)
        ha_effectiveness = _safe_float(hedge_analysis.get("effectiveness"), 0.0)
        ha_hedge_ratio = _safe_float(hedge_analysis.get("hedge_ratio"), 0.0)
        ha_status = str(hedge_analysis.get("status") or "OPTIMAL")
        ha_dynamic_tau = (
            _safe_float(hedge_analysis.get("dynamic_tau"))
            if hedge_analysis.get("dynamic_tau") is not None
            else None
        )

        has_active_hedge = (
            ha_hedge_ratio > 0.0
            or ha_hedge_pnl != 0.0
            or bool(hedge_analysis.get("has_hedge"))
        )

        if has_active_hedge:
            status_desc = {
                "OPTIMAL": "OPTIMAL (對沖結構健康)",
                "OVER_HEDGED": "OVER_HEDGED (過度對沖/拖累Alpha)",
                "UNDER_HEDGED": "UNDER_HEDGED (對沖不足/需防下行)",
            }.get(ha_status, ha_status)

            ha_status_color = (
                "\033[1;32m"
                if ha_status == "OPTIMAL"
                else "\033[1;33m"
                if ha_status == "OVER_HEDGED"
                else "\033[1;31m"
            )
            ha_alpha_pnl_color = "\033[1;32m" if ha_alpha_pnl >= 0 else "\033[1;31m"
            ha_hedge_pnl_color = "\033[1;32m" if ha_hedge_pnl >= 0 else "\033[1;31m"
            ha_net_pnl_color = "\033[1;32m" if ha_net_pnl >= 0 else "\033[1;31m"
            ha_eff_pct = ha_effectiveness * 100

            hedge_lines = [
                "```ansi",
                f" Alpha 選股 PnL   : {ha_alpha_pnl_color}${ha_alpha_pnl:+,.2f}\033[0m",
                f" 對沖避險 PnL     : {ha_hedge_pnl_color}${ha_hedge_pnl:+,.2f}\033[0m",
                f" 淨損益 (Net PnL)  : {ha_net_pnl_color}${ha_net_pnl:+,.2f}\033[0m",
                " --------------------------------------------------",
                f" 對沖比率 (Ratio) : {ha_hedge_ratio:.2%} ｜ 有效性: {ha_eff_pct:.1f}%",
                f" 對沖狀態評估     : {ha_status_color}{status_desc}\033[0m",
            ]

            if ha_dynamic_tau is not None:
                hedge_lines.append(
                    f" 動態 Tau (τ)     : {ha_dynamic_tau:.4f} (模型自適應調整)"
                )

            hedge_lines.append("```")

            embed.add_field(
                name="🛡️ 對沖績效歸因 (Hedge Attribution)",
                value="\n".join(hedge_lines),
                inline=False,
            )

    if sectors_data is not None:
        if sectors_data:
            sorted_sectors = sorted(
                sectors_data,
                key=lambda item: _safe_float(item.get("pct_change")),
                reverse=True,
            )
            inflows = [
                s for s in sorted_sectors if _safe_float(s.get("pct_change")) >= 0
            ]
            outflows = [
                s for s in sorted_sectors if _safe_float(s.get("pct_change")) < 0
            ]
            outflows.sort(key=lambda item: _safe_float(item.get("pct_change")))

            sector_content_lines = []
            if inflows:
                sector_content_lines.append(
                    " \u001b[1;32m🔥 領漲板塊 (Top Inflows)\u001b[0m"
                )
                for item in inflows:
                    symbol = item.get("symbol", "N/A")
                    sec_name = item.get("name", "N/A")
                    change = _safe_float(item.get("pct_change"))
                    rel_vol = _safe_float(item.get("rel_vol"))
                    skew = _safe_float(item.get("skew"))
                    uoa_count = int(item.get("uoa_count", 0))
                    sector_content_lines.append(
                        f" • {symbol} ({sec_name})：\u001b[1;32m{change:+.2f}%\u001b[0m ｜ 量比 {rel_vol:.2f}x ｜ Skew {skew:+.1f} ｜ UOA {uoa_count}"
                    )

            if outflows:
                if inflows:
                    sector_content_lines.append("")
                sector_content_lines.append(
                    " \u001b[1;31m❄️ 領跌板塊 (Top Outflows)\u001b[0m"
                )
                for item in outflows:
                    symbol = item.get("symbol", "N/A")
                    sec_name = item.get("name", "N/A")
                    change = _safe_float(item.get("pct_change"))
                    rel_vol = _safe_float(item.get("rel_vol"))
                    skew = _safe_float(item.get("skew"))
                    uoa_count = int(item.get("uoa_count", 0))
                    sector_content_lines.append(
                        f" • {symbol} ({sec_name})：\u001b[1;31m{change:+.2f}%\u001b[0m ｜ 量比 {rel_vol:.2f}x ｜ Skew {skew:+.1f} ｜ UOA {uoa_count}"
                    )

            if not inflows and not outflows:
                sector_content_lines.append(" • 暫無波動資料")

            sector_content = "\n".join(sector_content_lines)
            sector_chunks = _chunk_text_blocks([sector_content], max_len=1000)
            for i, chunk in enumerate(sector_chunks):
                field_name = (
                    f"🔄 板塊輪動 (Sector Rotation) ({i+1}/{len(sector_chunks)})"
                    if len(sector_chunks) > 1
                    else "🔄 板塊輪動 (Sector Rotation)"
                )
                embed.add_field(
                    name=field_name, value=f"```ansi\n{chunk}\n```", inline=False
                )
        else:
            embed.add_field(
                name="🔄 板塊輪動 (Sector Rotation)",
                value="```ansi\n • 暫無行業資金輪動數據。\n```",
                inline=False,
            )

    def _add_ai_section(header: str, content: str, icon: str) -> Any:
        if not content or content == "暫無分析":
            embed.add_field(
                name=f"{icon} {header}",
                value="```ansi\n • 暫無分析\n```",
                inline=False,
            )
            return

        blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
        transformed_blocks = []

        for b_idx, block in enumerate(blocks):
            lines = [
                line_str.strip() for line_str in block.split("\n") if line_str.strip()
            ]
            formatted_lines = []
            for line in lines:
                line = line.replace("**", "")
                line = re.sub(r"^[\-\*\•\d\.]+\s*", "", line)
                if not line:
                    continue
                formatted_lines.append(f" • {line}")
            if formatted_lines:
                transformed_blocks.append("\n".join(formatted_lines))

        chunks = _chunk_text_blocks(transformed_blocks, max_len=1000)
        for i, chunk in enumerate(chunks):
            field_name = (
                f"{icon} {header} ({i+1}/{len(chunks)})"
                if len(chunks) > 1
                else f"{icon} {header}"
            )
            embed.add_field(
                name=field_name,
                value=f"```ansi\n{chunk}\n```",
                inline=False,
            )

    if ai_commentary:
        parsed = _parse_post_market_ai_commentary(ai_commentary)
        if parsed:
            if parsed.get("market"):
                _add_ai_section("AI 多空大盤交叉驗證解讀", parsed["market"], "📊")
            if parsed.get("risk"):
                _add_ai_section("AI 潛在陷阱與風險提示", parsed["risk"], "⚠️")
            if parsed.get("strategy"):
                _add_ai_section("AI 高勝率交易策略推薦", parsed["strategy"], "🛡️")
        else:
            _add_ai_section("AI 損益歸因與次日策略點評", ai_commentary, "🧠")

    embed.set_footer(text="🌌 Nexus Seeker • 盤後綜合策略簡報")
    all_embeds = split_embed_by_fields(embed)

    if len(all_embeds) > 1:
        for idx, emb in enumerate(all_embeds, start=1):
            base_title = emb.title or ""
            base_title = re.sub(r"\s*\(\d+/\d+\)$", "", base_title).rstrip()
            emb.title = f"{base_title} (第 {idx}/{len(all_embeds)} 頁)"

    return all_embeds
