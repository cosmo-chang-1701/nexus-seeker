"""盤後綜合風險 Embed：欄位須如實呈現，不得把失敗顯示成空手、不得捏造持倉。"""

from typing import Any, Dict, List, Optional

import discord

from cogs.embed_builders.order_embeds.post_market_intelligence import (
    build_post_market_intelligence_embed,
)
from cogs.embed_builders._core import _remap_color
from market_analysis.report_formatter import (
    format_correlation_report,
    format_position_report,
)

SPY_MISSING = (
    "⚠️ **SPY 現價資料不足**：無法換算 Beta-Delta 曝險，本次略過宏觀風險報告。\n"
)
MACRO_MARKER = "🌐 **【宏觀風險與資金水位報告】**"


def _pos(symbol: str = "AAPL", price: Optional[float] = 1.5) -> str:
    return format_position_report(
        symbol, "2026-12-18", 150.0, "put", "", 2.0, price,
        (None if price is None else 0.25), 30, -0.5, "🟢 續抱", -1.0,
    )  # fmt: skip


def _build(lines: List[str], **kw: Any) -> List[discord.Embed]:
    return build_post_market_intelligence_embed(report_lines=lines, **kw)


def _fields(embeds: List[discord.Embed]) -> Dict[str, str]:
    return {str(f.name): str(f.value) for e in embeds for f in e.fields}


def _find(fields: Dict[str, str], key: str) -> str:
    return next(v for k, v in fields.items() if key in k)


def test_report_failed_not_cash_defense() -> None:
    embeds = _build([], report_failed=True, ai_commentary="x")
    fields = _fields(embeds)
    assert "持倉結算失敗，數值未產生" in _find(fields, "💰")
    assert "以下不代表帳戶為空手" in _find(fields, "持倉明細")
    assert "持倉結算失敗，宏觀風險未產生" in _find(fields, "宏觀風險")
    for v in fields.values():
        assert "現金防禦" not in v
    assert "$0.00" not in _find(fields, "💰")
    assert embeds[0].color == _remap_color(discord.Color.orange())


def test_empty_lines_without_failure_keeps_cash_card() -> None:
    fields = _fields(_build([]))
    assert "現金防禦" in _find(fields, "持倉明細")


def test_spy_missing_no_unknown_position() -> None:
    lines = [_pos(), SPY_MISSING, *format_correlation_report([], 1)]
    embeds = _build(lines)
    fields = _fields(embeds)
    for v in fields.values():
        assert "UNKNOWN" not in v
    pos_val = _find(fields, "持倉明細")
    assert pos_val.count("AAPL") == 1
    assert "SPY 現價資料不足" in _find(fields, "宏觀風險")
    assert "SPY 現價資料不足" not in pos_val
    assert "板塊相關性掃描" in _find(fields, "非系統性集中風險")
    assert embeds[0].color == _remap_color(discord.Color.orange())


def test_macro_block_still_split() -> None:
    lines = [
        _pos(),
        MACRO_MARKER,
        "總 Beta-Delta: 1",
        *format_correlation_report([], 1),
    ]
    fields = _fields(_build(lines))
    assert "Beta-Delta" in _find(fields, "宏觀風險")
    assert "UNKNOWN" not in "".join(fields.values())


def test_zero_pnl_is_white() -> None:
    fields = _fields(_build([_pos(price=2.0)]))
    fin = _find(fields, "💰")
    pnl_line = next(ln for ln in fin.split("\n") if "未實現損益" in ln)
    assert "\u001b[1;37m" in pnl_line


def test_nonzero_pnl_colors() -> None:
    # 賣方 (quantity<0)：現價低於成本為獲利
    win = _find(_fields(_build([_pos(price=1.0)])), "💰")
    lose = _find(_fields(_build([_pos(price=3.0)])), "💰")
    assert "\u001b[1;32m" in next(ln for ln in win.split("\n") if "未實現損益" in ln)
    assert "\u001b[1;31m" in next(ln for ln in lose.split("\n") if "未實現損益" in ln)


def test_missing_price_caveat_in_financial_field() -> None:
    fin = _find(_fields(_build([_pos(), _pos("MSFT", price=None)])), "💰")
    assert "缺現價，未計入上述損益" in fin
    assert "**" not in fin


def test_color_red_when_macro_has_alarm() -> None:
    lines = [_pos(), MACRO_MARKER, "🚨 超限"]
    assert _build(lines)[0].color == _remap_color(discord.Color.red())


def test_color_blue_ignores_ai_header() -> None:
    lines = [_pos(), MACRO_MARKER, "正常"]
    ai = "## ⚠️ 潛在陷阱與風險提示\n- 🚨 注意"
    assert _build(lines, ai_commentary=ai)[0].color == _remap_color(
        discord.Color.blue()
    )


def test_uoa_none_renders_dashes() -> None:
    sectors = [
        {"symbol": "XLK", "name": "科技", "pct_change": 1.0, "uoa_count": None},
        {"symbol": "XLE", "name": "能源", "pct_change": -1.0, "uoa_count": None},
        {"symbol": "XLF", "name": "金融", "pct_change": None, "uoa_count": None},
    ]
    val = _find(_fields(_build([], sectors_data=sectors)), "板塊輪動")
    assert val.count("UOA --") == 3


def test_hedge_name_and_unpriced_line() -> None:
    hedge = {
        "net_pnl": 10.0,
        "alpha_contribution": 5.0,
        "hedge_contribution": 5.0,
        "hedge_ratio": 0.2,
        "has_hedge": True,
        "unpriced_count": 2,
    }
    fields = _fields(_build([], hedge_analysis=hedge))
    val = fields["🛡️ 對沖績效歸因（真實部位・建倉至今累計）"]
    assert "2 筆缺報價，未計入損益" in val
    hedge.pop("unpriced_count")
    val2 = _find(_fields(_build([], hedge_analysis=hedge)), "對沖績效歸因")
    assert "缺報價" not in val2
    assert "未填成本" not in val2


def test_hedge_uncosted_line() -> None:
    hedge = {
        "net_pnl": 10.0,
        "alpha_contribution": 5.0,
        "hedge_contribution": 5.0,
        "hedge_ratio": 0.2,
        "has_hedge": True,
        "uncosted_count": 3,
    }
    val = _find(_fields(_build([], hedge_analysis=hedge)), "對沖績效歸因")
    assert "3 筆現貨未填成本，未計入損益" in val
    assert "缺報價" not in val
