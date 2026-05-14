from typing import List, Dict, Any, Union
from models.quant import MacroRiskMetrics

# Discord/Telegram 排版優化常數
ZWS = "\u200b"  # 零寬空格
EMPTY_LINE = f"{ZWS}\n"


def format_macro_risk_report(
    metrics: Union[MacroRiskMetrics, Dict[str, Any]], spy_price: float
) -> List[str]:
    """
    將宏觀風險指標格式化為文字報告。
    """
    lines = ["🌐 **【宏觀風險與資金水位報告】**", EMPTY_LINE]

    # 支援 Pydantic 模型與傳統 Dict
    if isinstance(metrics, MacroRiskMetrics):
        exposure_pct = metrics.exposure_pct
        net_exposure_dollars = metrics.net_exposure_dollars
        total_beta_delta = metrics.total_beta_delta
        total_gamma = metrics.total_gamma
        gamma_threshold = metrics.gamma_threshold
        theta_yield = metrics.theta_yield
        total_theta = metrics.total_theta
        portfolio_heat = metrics.portfolio_heat
        total_margin_used = metrics.total_margin_used
        total_vega = metrics.total_vega
        total_vanna = metrics.total_vanna
    else:
        exposure_pct = metrics["exposure_pct"]
        net_exposure_dollars = metrics["net_exposure_dollars"]
        total_beta_delta = metrics["total_beta_delta"]
        total_gamma = metrics["total_gamma"]
        gamma_threshold = metrics["gamma_threshold"]
        theta_yield = metrics["theta_yield"]
        total_theta = metrics["total_theta"]
        portfolio_heat = metrics["portfolio_heat"]
        total_margin_used = metrics["total_margin_used"]
        total_vega = metrics.get("total_vega", 0.0)
        total_vanna = metrics.get("total_vanna", 0.0)

    DELTA_THRESHOLD_PCT = 15.0

    if exposure_pct > DELTA_THRESHOLD_PCT:
        delta_status = (
            f"🚨 **多頭曝險過高** (`{exposure_pct:.1f}%` > {DELTA_THRESHOLD_PCT}%)"
        )
        # Actually, let's just use the metrics we have
        advice = "   🛡️ **對沖指令:** 系統多頭部位過重，建議執行 Beta 對沖。"
    elif exposure_pct < -DELTA_THRESHOLD_PCT:
        delta_status = (
            f"🚨 **空頭曝險過高** (`{abs(exposure_pct):.1f}%` > {DELTA_THRESHOLD_PCT}%)"
        )
        advice = "   🛡️ **對沖指令:** 系統空頭部位過重，建議執行 Beta 對沖。"
    else:
        delta_status = f"✅ **風險中性** (`{abs(exposure_pct):.1f}%` 內)"
        advice = "   👉 目前系統性曝險在安全範圍，無需執行對沖。"

    lines.append(
        f"🔹 **淨 SPY Delta 曝險:** `${net_exposure_dollars:,.0f}` (等效 `{total_beta_delta:+.1f}` 股)\n"
    )
    lines.append(f" └─ {delta_status}\n{advice}\n")
    lines.append(EMPTY_LINE)

    # Gamma
    total_gamma = metrics["total_gamma"]
    gamma_threshold = metrics["gamma_threshold"]
    if total_gamma < -gamma_threshold:
        gamma_status = "🚨 **脆性警告 (Negative Gamma)**"
        g_msg = "   👉 下行加速度風險極大，建議買入 OTM Put 注入正 Gamma。"
    elif total_gamma > gamma_threshold:
        gamma_status = "🛡️ **反脆弱 (Positive Gamma)**"
        g_msg = "   👉 波動越劇烈對帳戶越有利 (買方優勢)。"
    else:
        gamma_status = "✅ **Gamma 中性**"
        g_msg = "   👉 非線性風險受控，帳戶淨值曲線變動平滑。"

    lines.append(f"🔹 **組合淨 Gamma:** `{total_gamma:+.2f}`\n")
    lines.append(f" └─ {gamma_status}\n{g_msg}\n")
    lines.append(EMPTY_LINE)

    # Theta
    theta_yield = metrics["theta_yield"]
    total_theta = metrics["total_theta"]
    theta_status = "✅ 現金流健康"
    if theta_yield < 0.05:
        theta_status = "⚠️ **收益率過低** (資金利用率不足)"
    elif theta_yield > 0.30:
        theta_status = "🔥 **過度收租** (暗示承擔了極高的尾部風險)"

    lines.append(
        f"🔹 **每日預期 Theta:** `${total_theta:+.2f}` (`{theta_yield:.3f}%`)\n"
    )
    lines.append(f" └─ {theta_status}\n")
    lines.append(EMPTY_LINE)

    # Heat
    portfolio_heat = metrics["portfolio_heat"]
    total_margin_used = metrics["total_margin_used"]
    heat_status = "✅ 水位正常"
    if portfolio_heat > 50.0:
        heat_status = "🆘 **強烈警告** (Heat > 50%，極易觸發保證金追繳)"
    elif portfolio_heat > 30.0:
        heat_status = "⚠️ **水位警戒** (已達常規滿水位，停止新進場部位)"

    lines.append(
        f"🔹 **資金熱度 (Heat):** `${total_margin_used:,.2f}` (`{portfolio_heat:.1f}%`)\n"
    )
    lines.append(f" └─ {heat_status}\n")
    lines.append(EMPTY_LINE)

    # Vega & Vanna
    total_vega = metrics.get("total_vega", 0.0)
    total_vanna = metrics.get("total_vanna", 0.0)
    lines.append(f"🔹 **組合淨 Vega:** `${total_vega:+.2f}`\n")
    lines.append(" └─ 指向 IV 升高時的盈虧變動。\n")
    lines.append(f"🔹 **組合淨 Vanna:** `${total_vanna:+.2f}`\n")
    lines.append(" └─ 指向 IV 升高對 Delta 曝險的二次影響 (Hidden Delta)。\n")

    return lines


def format_correlation_report(
    high_corr_pairs: List[tuple], symbol_count: int
) -> List[str]:
    """
    格式化相關性報告。
    """
    lines = ["🕸️ **【非系統性集中風險 (板塊連動性)】**", EMPTY_LINE]
    lines.append(
        f"🔹 **板塊相關性掃描:** 目標 `{symbol_count}` 檔 (60 日 Pearson 係數)\n"
    )

    if high_corr_pairs:
        lines.append("   🚨 **高度正相關警告:** 發現板塊重疊曝險！\n")
        for sym1, sym2, rho in high_corr_pairs:
            lines.append(f"      ⚠️ `{sym1}` & `{sym2}` (ρ = {rho:.2f})\n")
        lines.append(
            "   👉 **經理人建議:** 若發生整體利空，將引發 Gamma 同步擴張，建議適度降載。\n"
        )
    else:
        lines.append(
            "   ✅ **分散性良好:** 未發現 ρ > 0.75 的重疊曝險，非系統性風險受控。\n"
        )
    lines.append(EMPTY_LINE)
    return lines


def format_position_report(
    symbol: str,
    expiry: str,
    strike: float,
    opt_type: str,
    cc_tag: str,
    entry_price: float,
    current_price: float,
    pnl_pct: float,
    dte: int,
    spx_weighted_delta: float,
    status: str,
) -> str:
    """
    格式化單一持倉報告。
    """
    pnl_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"
    return (
        f"🔹 **{symbol}** ｜ `{expiry}` ｜ `${strike}` **{opt_type.upper()}**{cc_tag}\n"
        f"├─ 💰 成本: `${entry_price:.2f}` ｜ 📈 現價: `${current_price:.2f}`\n"
        f"├─ {pnl_icon} 損益: **{pnl_pct*100:+.2f}%**\n"
        f"├─ ⏳ DTE: `{dte}` 天 ｜ 秤⚖️ SPY Δ: `{spx_weighted_delta:+.2f}`\n"
        f"└─ 🎯 動作: {status}\n"
    )
