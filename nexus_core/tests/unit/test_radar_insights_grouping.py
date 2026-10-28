"""
tests/unit/test_radar_insights_grouping.py

批次量化雷達「即時聯動警示」欄位改為依標的分組（標頭 + 項目符號，不重複標的），
並驗證「⚡ 批次分析警示標的」按鈕的解析器可讀取新格式（含舊格式回退）。
"""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders import market_embeds
from cogs.embed_builders.market_embeds import (
    _render_grouped_insights,
    _strip_sym_prefix,
    build_radar_scan_embed,
)
from cogs.unified_terminal.batch_scan_view import (
    BatchScanWarningButton,
    _extract_warning_symbols,
)

_HDR = "\u001b[1;36m{}\u001b[0m"


def _hdr(sym: str) -> str:
    return _HDR.format(sym)


# ==========================================
# 版面與預算（純函式）
# ==========================================


def test_grouped_layout_header_once_and_blank_between_groups() -> None:
    out = _render_grouped_insights(
        {
            "AAOI": ["• 🪨 上方 $95.0 負 Gamma 泥淖", "• 📈 接近 HVN"],
            "NVDA": ["• 🧱 偵測到硬封頂"],
        }
    )
    assert out == (
        f"{_hdr('AAOI')}\n• 🪨 上方 $95.0 負 Gamma 泥淖\n• 📈 接近 HVN\n\n"
        f"{_hdr('NVDA')}\n• 🧱 偵測到硬封頂"
    )
    assert out.count(_hdr("AAOI")) == 1
    assert not out.endswith("\n")


def test_empty_groups_use_fallback_without_header() -> None:
    expected = "• ✨ 所有標的當前價格與 Max Pain 及波動邊界皆無極端異常偏離。"
    assert _render_grouped_insights({}) == expected
    assert _render_grouped_insights({"AAA": [], "BBB": ["  "]}) == expected


def test_ten_bullet_cap_and_summary_line() -> None:
    out = _render_grouped_insights({"AAA": [f"• 🔹 第{i}則" for i in range(14)]})
    bullets = [ln for ln in out.splitlines() if ln.startswith("•")]
    assert len(bullets) <= 10
    assert bullets[-1] == "• …其餘 5 則警示略"
    assert out.count(_hdr("AAA")) == 1
    assert len(out) <= 920


def test_exactly_ten_bullets_no_summary() -> None:
    out = _render_grouped_insights({"AAA": [f"• 🔹 第{i}則" for i in range(10)]})
    assert "其餘" not in out
    assert len([ln for ln in out.splitlines() if ln.startswith("•")]) == 10


def test_budget_truncation_never_orphans_header() -> None:
    long_a = "• 🔹 " + "甲" * 500
    long_b = "• 🔹 " + "乙" * 500
    out = _render_grouped_insights({"AAA": [long_a], "BBB": [long_b, long_b]})
    lines = out.splitlines()
    assert len(out) <= 920
    assert lines[-1] == "• …其餘 2 則警示略"
    # BBB 的第一則放不下 -> 標頭不得單獨出現
    assert _hdr("BBB") not in out
    assert lines[0] == _hdr("AAA")


def test_header_not_followed_by_summary_only() -> None:
    out = _render_grouped_insights(
        {"AAA": ["• 🔹 " + "甲" * 880], "BBB": ["• 🔹 短"], "CCC": ["• 🔹 短"]}
    )
    lines = out.splitlines()
    assert len(out) <= 920
    for i, ln in enumerate(lines):
        if ln.startswith("\u001b[1;36m"):
            assert lines[i + 1].startswith("• ")
    assert lines[-1] == "• …其餘 2 則警示略"


def test_first_bullet_too_long_is_truncated_under_header() -> None:
    out = _render_grouped_insights({"AAA": ["• 🔹 " + "甲" * 2000]})
    lines = out.splitlines()
    assert lines[0] == _hdr("AAA")
    assert len(lines[1]) <= 900
    assert len(out) <= 1024


def test_strip_sym_prefix_only_first_occurrence() -> None:
    assert _strip_sym_prefix("• 🔥 AAA: 文字 AAA: 次要", "AAA") == "• 🔥 文字 AAA: 次要"
    assert _strip_sym_prefix("", "AAA") == ""
    assert _strip_sym_prefix("  ", "AAA") == ""


# ==========================================
# 按鈕解析器
# ==========================================


def test_parser_new_format_with_dotted_symbol_and_ansi_header() -> None:
    value = (
        f"```ansi\n{_hdr('AAOI')}\n• 🪨 上方負 Gamma\n\n{_hdr('BRK.B')}\n• 📈 HVN\n\n"
        "NVDA\n• 🚨 Volume PCR:1.30 殺盤背離\n```"
    )
    assert _extract_warning_symbols(value) == ["AAOI", "BRK.B", "NVDA"]


def test_parser_does_not_pick_pcr_from_bullet() -> None:
    value = f"```ansi\n{_hdr('STX')}\n• 🚨 實體跌破 PutWall，PCR:1.30\n```"
    assert _extract_warning_symbols(value) == ["STX"]


def test_parser_legacy_fallback() -> None:
    value = "```ansi\n• 🚀 AAPL: 價格接近下緣\n• ⚠️ TSLA: 籌碼面異常\n```"
    assert _extract_warning_symbols(value) == ["AAPL", "TSLA"]


def test_parser_empty_fallback_text_yields_nothing() -> None:
    value = (
        "```ansi\n• ✨ 所有標的當前價格與 Max Pain 及波動邊界皆無極端異常偏離。\n```"
    )
    assert _extract_warning_symbols(value) == []


# ==========================================
# 原始碼掃描：不得再於 append 中帶入 {sym}:
# ==========================================


def test_no_insight_append_embeds_symbol() -> None:
    src = Path(market_embeds.__file__).read_text(encoding="utf-8")
    offenders: list[int] = []
    for m in re.finditer(r"insights\.append\(", src):
        depth = 0
        end = m.end() - 1
        for i in range(end, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if "{sym}:" in src[m.start() : end]:
            offenders.append(src.count("\n", 0, m.start()) + 1)
    assert offenders == []


# ==========================================
# 端到端：builder -> 欄位 -> 按鈕
# ==========================================


def _row(
    sym: str,
    price: float,
    max_pain: float,
    *,
    put_wall: float = 0.0,
    vol_pcr: float = 0.9,
    hvn: float = 0.0,
    swamp: tuple[float, float] | None = None,
) -> dict[str, Any]:
    return {
        "symbol": sym,
        "quote": {"c": price, "dp": 0.5},
        "iv_metrics": {
            "iv_rank": 40.0,
            "expected_move_weekly": 5.0,
            "expected_move_lower": price * 0.95,
            "expected_move_upper": price * 1.05,
        },
        "skew": 0.0,
        "skew_percentile": 50.0,
        "volume_pcr": vol_pcr,
        "max_pain": {"max_pain": max_pain},
        "uoa": [],
        "gex_metrics": {"put_wall": put_wall, "call_wall": 0.0, "net_gex": -3e6},
        "gex_profile_data": {
            "put_wall": put_wall,
            "call_wall": 0.0,
            "net_gex": -3e6,
        },
        "overhead_neg_gex_swamp": swamp,
        "vp_data": {"hvn": hvn, "lvn": 0.0},
        "psq_result": {"momentum": -1.0, "signal_direction": "🔴"},
    }


def _insights_field_value(embeds: list[Any]) -> str:
    for e in embeds:
        for f in e.fields:
            if f.name and "即時聯動警示" in f.name:
                return str(f.value)
    raise AssertionError("找不到即時聯動警示欄位")


def _build(rows: list[dict[str, Any]]) -> list[Any]:
    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
    ):
        return build_radar_scan_embed(rows, "ALL", 12345)


@pytest.mark.asyncio
async def test_end_to_end_grouping_and_button_parse() -> None:
    rows = [
        _row("AAOI", 77.5, 100.0, hvn=77.63, swamp=(95.0, -5_000_000.0)),
        _row("STX", 843.1, 860.0, put_wall=850.0, vol_pcr=1.81),
        _row("BRK.B", 400.0, 500.0, hvn=400.5),
    ]
    value = _insights_field_value(_build(rows))

    assert len(value) <= 1024
    assert value.startswith("```ansi")
    assert value.endswith("```")
    body_lines = value.splitlines()
    for sym in ("AAOI", "STX", "BRK.B"):
        assert not any(ln.startswith("•") and f"{sym}:" in ln for ln in body_lines), sym
    assert _hdr("AAOI") in value
    assert "股價距離 Max Pain 有" in value
    # 標頭順序 = 列順序
    plain = [ln for ln in re.sub(r"\x1b\[[0-9;]*m", "", value).splitlines()]
    headers = [ln for ln in plain if re.fullmatch(r"[A-Za-z0-9.\-]{1,10}", ln)]
    assert headers == ["AAOI", "STX", "BRK.B"]
    assert sum(1 for ln in plain if ln.startswith("•")) >= 4

    cog = MagicMock()
    cog._run_warning_symbols_hub = AsyncMock()
    btn = BatchScanWarningButton(cog, MagicMock())
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    field = MagicMock()
    field.name = "💡 即時聯動警示 (Real-time Insights)"
    field.value = value
    embed = MagicMock()
    embed.fields = [field]
    interaction.message = MagicMock()
    interaction.message.embeds = [embed]
    view = MagicMock()
    view.children = [btn]
    btn._view = view

    await btn.callback(interaction)

    cog._run_warning_symbols_hub.assert_awaited_once()
    assert cog._run_warning_symbols_hub.call_args.args[1] == [
        "AAOI",
        "STX",
        "BRK.B",
    ]


def test_same_symbol_in_two_rows_merges_into_one_group() -> None:
    rows = [
        _row("AAOI", 77.5, 100.0, hvn=77.63),
        _row("ZZZ", 50.0, 50.0),
        _row("AAOI", 77.5, 100.0, hvn=77.63),
    ]
    value = _insights_field_value(_build(rows))
    assert value.count(_hdr("AAOI")) == 1
