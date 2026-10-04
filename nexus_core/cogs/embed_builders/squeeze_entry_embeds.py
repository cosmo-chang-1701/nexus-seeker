"""多時間框架擠壓進場 Embed（`/x` 進場檢核頁籤與進場顧問推播共用）。

規格見 `docs/strategies/10_multi_timeframe_squeeze_entry.md`。
"""

from datetime import datetime, timezone
from typing import Any, List, Optional

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import _add_ansi_field_safely

_TF_ORDER = ("W", "3D", "D", "65m", "15m", "5m")

_LEVEL_TEXT = {
    "High": "\u001b[1;31m● 高強度擠壓\u001b[0m",
    "Mid": "\u001b[1;33m● 中強度擠壓\u001b[0m",
    "Normal": "\u001b[1;35m● 一般擠壓\u001b[0m",
    "Release": "\u001b[1;32m○ 未擠壓\u001b[0m",
}
_MOM_TEXT = {
    "LightBlue": "\u001b[1;36m淺藍↑\u001b[0m",
    "DarkBlue": "\u001b[0;34m深藍↓\u001b[0m",
    "Red": "\u001b[1;31m紅↓\u001b[0m",
    "Golden": "\u001b[1;33m金↑\u001b[0m",
    "Neutral": "中性",
}
_STATUS_TEXT = {
    "ENTRY": "\u001b[1;32m✅ 建議建倉\u001b[0m",
    "PENDING_BREAKOUT": "\u001b[1;33m🧱 待突破壓力區\u001b[0m",
    "WATCH": "\u001b[1;34m👀 觀察中\u001b[0m",
    "NONE": "⏸️ 尚無訊號",
    "VETOED": "\u001b[1;31m⛔ 否決\u001b[0m",
    "NO_DATA": "\u001b[1;31m⛔ 資料不足\u001b[0m",
}
_TIER_TEXT = {1: "T1（1%）", 2: "T2（1.5%）", 3: "T3（2.5%）"}


def _matrix_lines(matrix: Any) -> List[str]:
    lines = ["```ansi"]
    for tf in _TF_ORDER:
        st = matrix.get(tf) if matrix else None
        if st is None:
            lines.append(f" {tf:<4}│ 資料不足")
            continue
        flags: List[str] = []
        if st.green_dot:
            ago = st.green_dot_bars_ago
            flags.append(
                "\u001b[1;32mGreen Dot\u001b[0m"
                + (f"（{ago} 根前）" if ago else "（本根）")
            )
        if st.turbo:
            flags.append("\u001b[1;36mTurbo\u001b[0m")
        level = _LEVEL_TEXT.get(st.squeeze_level, st.squeeze_level)
        mom = _MOM_TEXT.get(st.momentum_color, st.momentum_color)
        line = f" {tf:<4}│ {level} │ 動能 {mom}"
        if flags:
            line += " │ " + "、".join(flags)
        lines.append(line)
        if st.preview_squeeze_level is not None:
            p_level = _LEVEL_TEXT.get(
                st.preview_squeeze_level, st.preview_squeeze_level
            )
            p_mom = _MOM_TEXT.get(
                st.preview_momentum_color or "", st.preview_momentum_color or "N/A"
            )
            lines.append(f"     └ 盤中未收盤預覽: {p_level} │ {p_mom}")
    lines.append("```")
    return lines


def create_squeeze_entry_embed(
    symbol: str,
    evaluation: Any,
    *,
    passed: bool,
    reason: str,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    trading_strategy: Optional[str] = None,
    dynamic_regime: Optional[str] = None,
) -> discord.Embed:
    """`evaluation` 為 `squeeze_entry.SqueezeEvaluation`（duck typing 讀取）。"""
    result = getattr(evaluation, "result", None)
    matrix = getattr(evaluation, "matrix", None) or {}
    resistance = getattr(evaluation, "resistance", None)

    embed = NexusEmbed(
        title=f"🗜️ {symbol} 多時間框架擠壓進場檢核",
        color=discord.Color.green() if passed else discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )

    _add_ansi_field_safely(
        embed, "📊 擠壓矩陣（只用已收盤 K 棒判定）", _matrix_lines(matrix)
    )

    status = getattr(result, "status", None) or "NONE"
    tier = getattr(result, "tier", None)
    triggers = getattr(result, "triggers", None) or []
    verdict = [
        "```ansi",
        f" ├─ 判定: {_STATUS_TEXT.get(status, status)}",
        f" ├─ 等級: {_TIER_TEXT.get(tier, '未達等級') if tier else '未達等級'}",
        f" ├─ 觸發: {'、'.join(triggers) if triggers else '無'}",
        f" ├─ 擠壓中時間框架數: {getattr(result, 'squeeze_count', 0)}",
    ]
    if trading_strategy == "DYNAMIC" and dynamic_regime:
        verdict.append(
            f" ├─ 目前 Regime: {dynamic_regime}（僅供參考，多頭建倉改由擠壓規則判定）"
        )
    verdict.append(f" └─ 說明: {reason.split(' | ')[0] if reason else 'N/A'}")
    verdict.append("```")
    _add_ansi_field_safely(embed, "🎯 進場判定", verdict)

    res_lines = ["```ansi"]
    broken = getattr(resistance, "broken", None)
    overhead = getattr(resistance, "overhead", None)
    if broken is not None:
        res_lines.append(
            f" ├─ \u001b[1;32m已突破\u001b[0m ${broken.bottom:,.2f}–${broken.top:,.2f}"
            f"（觸及 {broken.touches} 次）"
        )
    if overhead is not None:
        approaching = bool(getattr(resistance, "is_approaching", False))
        tag = "\u001b[1;33m衝擊中\u001b[0m" if approaching else "上方"
        res_lines.append(
            f" ├─ {tag} ${overhead.bottom:,.2f}–${overhead.top:,.2f}"
            f"（觸及 {overhead.touches} 次）"
        )
    if broken is None and overhead is None:
        res_lines.append(" ├─ 近 60 日上方無擺盪高點壓力（或資料不足）")
    atr = getattr(resistance, "atr_1d", None)
    res_lines.append(f" └─ ATR₁D: {f'${atr:,.2f}' if atr else 'N/A'}")
    res_lines.append("```")
    _add_ansi_field_safely(embed, "🧱 壓力區（近 60 日擺盪高點群聚）", res_lines)

    size_pct = getattr(result, "size_pct", None)
    if any(v is not None for v in (entry_price, stop_loss, size_pct)):

        def _px(v: Optional[float]) -> str:
            return f"${v:,.2f}" if v is not None else "N/A"

        _add_ansi_field_safely(
            embed,
            "💰 價位與部位建議",
            [
                "```ansi",
                f" ├─ 進場價: {_px(entry_price)}",
                f" ├─ 參考停損: {_px(stop_loss)}（D 擠壓區間低點與 20SMA 較低者 − 0.5×ATR₁D）",
                f" └─ 建議部位: {f'總資產 {size_pct:g}%' if size_pct else 'N/A'}",
                "```",
            ],
        )

    embed.set_footer(
        text="等級門檻與部位比例為初版參數（前向觀察中），僅供參考；"
        "3D／W 重採樣錨點可能與看盤軟體略有差異。"
    )
    return embed
