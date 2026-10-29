"""舊版盤後風險結算報告 Embed：不得把非持倉行渲染成假部位，對沖欄須如實標示。"""

from typing import Any, Dict, List, Optional

import discord

from cogs.embed_builders._core import _remap_color
from cogs.embed_builders.report_embeds import create_portfolio_report_embed
from market_analysis.report_formatter import (
    format_correlation_report,
    format_position_report,
)

SPY_MISSING = (
    "⚠️ **SPY 現價資料不足**：無法換算 Beta-Delta 曝險，本次略過宏觀風險報告。\n"
)
MACRO_MARKER = "🌐 **【宏觀風險與資金水位報告】**"
HEDGE_NAME = "🛡️ 對沖績效歸因（真實部位・建倉至今累計）"


def _pos(symbol: str = "AAPL", price: Optional[float] = 1.5) -> str:
    return format_position_report(
        symbol, "2026-12-18", 150.0, "put", "", 2.0, price,
        (None if price is None else 0.25), 30, -0.5, "🟢 續抱", -1.0,
    )  # fmt: skip


def _fields(embed: discord.Embed) -> Dict[str, str]:
    return {str(f.name): str(f.value) for f in embed.fields}


def _find(fields: Dict[str, str], key: str) -> str:
    return next(v for k, v in fields.items() if key in k)


def _hedge(**kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "net_pnl": 10.0,
        "alpha_contribution": 5.0,
        "hedge_contribution": 5.0,
        "effectiveness": 0.5,
        "hedge_ratio": 0.2,
        "has_hedge": True,
    }
    base.update(kw)
    return base


def test_spy_missing_no_unknown_position() -> None:
    lines = [
        _pos(),
        SPY_MISSING,
        "📉 資料品質提示：部分標的缺報價",
        *format_correlation_report([], 1),
    ]
    fields = _fields(create_portfolio_report_embed(lines))
    for v in fields.values():
        assert "UNKNOWN" not in v
    pos_val = _find(fields, "持倉明細")
    assert pos_val.count("AAPL") == 1
    assert "SPY 現價資料不足" not in pos_val
    assert "SPY 現價資料不足" in _find(fields, "宏觀風險")
    assert "板塊相關性掃描" in _find(fields, "非系統性集中風險")


def test_normal_split_positions_macro_correlation() -> None:
    lines = [
        _pos(),
        MACRO_MARKER,
        "總 Beta-Delta: 1",
        *format_correlation_report([], 1),
    ]
    fields = _fields(create_portfolio_report_embed(lines))
    assert "AAPL" in _find(fields, "持倉明細")
    assert "Beta-Delta" in _find(fields, "宏觀風險")
    assert "板塊相關性掃描" in _find(fields, "非系統性集中風險")
    assert "UNKNOWN" not in "".join(fields.values())


def test_missing_price_caveat_and_pnl_color() -> None:
    fin = _find(
        _fields(create_portfolio_report_embed([_pos(), _pos("MSFT", price=None)])),
        "💰",
    )
    assert "缺現價，未計入上述損益" in fin
    win = _find(_fields(create_portfolio_report_embed([_pos(price=1.0)])), "💰")
    lose = _find(_fields(create_portfolio_report_embed([_pos(price=3.0)])), "💰")
    flat = _find(_fields(create_portfolio_report_embed([_pos(price=2.0)])), "💰")
    assert "\u001b[1;32m" in next(x for x in win.split("\n") if "未實現損益" in x)
    assert "\u001b[1;31m" in next(x for x in lose.split("\n") if "未實現損益" in x)
    assert "\u001b[1;37m" in next(x for x in flat.split("\n") if "未實現損益" in x)


def test_hedge_field_name_unpriced_and_split_colors() -> None:
    hedge = _hedge(
        alpha_contribution=8.0,
        hedge_contribution=-3.0,
        net_pnl=5.0,
        unpriced_count=2,
    )
    fields = _fields(create_portfolio_report_embed([_pos()], hedge))
    assert "真實部位・建倉至今累計" in "".join(fields)
    val = fields[HEDGE_NAME]
    assert "2 筆缺報價" in val
    alpha_ln = next(x for x in val.split("\n") if "Alpha" in x)
    hedge_ln = next(x for x in val.split("\n") if "對沖避險" in x)
    assert "\033[1;32m" in alpha_ln
    assert "\033[1;31m" in hedge_ln
    assert "OPTIMAL (對沖結構健康)" in val


def test_unpriced_disclosed_without_active_hedge() -> None:
    hedge = _hedge(
        alpha_contribution=0.0,
        hedge_contribution=0.0,
        hedge_ratio=0.0,
        has_hedge=False,
        unpriced_count=1,
    )
    val = _fields(create_portfolio_report_embed([_pos()], hedge))[HEDGE_NAME]
    assert "1 筆缺報價，未計入損益" in val
    assert "Alpha" not in val
    clean = _hedge(hedge_ratio=0.0, hedge_contribution=0.0, has_hedge=False)
    assert HEDGE_NAME not in _fields(create_portfolio_report_embed([_pos()], clean))


def test_uncosted_disclosed() -> None:
    hedged = _hedge(uncosted_count=2)
    val = _fields(create_portfolio_report_embed([_pos()], hedged))[HEDGE_NAME]
    assert "2 筆現貨未填成本，未計入損益" in val
    assert "Alpha" in val
    spot_only = _hedge(
        alpha_contribution=0.0,
        hedge_contribution=0.0,
        hedge_ratio=0.0,
        has_hedge=False,
        uncosted_count=1,
    )
    val2 = _fields(create_portfolio_report_embed([_pos()], spot_only))[HEDGE_NAME]
    assert "1 筆現貨未填成本，未計入損益" in val2
    assert "缺報價" not in val2


def test_color_from_macro_only() -> None:
    def color(lines: List[str], hedge: Any = None) -> Any:
        return create_portfolio_report_embed(lines, hedge).color

    assert color([_pos(), MACRO_MARKER, "⚠️ 偏高"]) == _remap_color(
        discord.Color.orange()
    )
    assert color([_pos(), MACRO_MARKER, "🚨 超限"]) == _remap_color(discord.Color.red())
    noisy = _hedge(status="🚨 ⚠️ 🆘")
    assert color([_pos(), MACRO_MARKER, "正常"], noisy) == _remap_color(
        discord.Color.blue()
    )
