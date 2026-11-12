"""兩級體制避險 Discord 卡片測試。"""

from typing import Any, List, Optional, Tuple

import discord

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders.regime_hedge_embeds import create_regime_hedge_embed
from market_analysis.dynamic_rollover.regime_hedge_sizing import (
    HedgePlan,
    InverseLeg,
)


def _leg(
    symbol: str,
    value: float = 10000.0,
    price: float = 40.0,
    stop_pct: float = 3.5,
    earnings: Tuple[str, ...] = (),
) -> InverseLeg:
    return InverseLeg(
        symbol=symbol,
        target_value=value,
        reference_price=price,
        shares=int(value // price),
        stop_pct=stop_pct,
        stop_trigger_price=round(price * (1 - stop_pct / 100), 2),
        earnings_reduced=bool(earnings),
        earnings_symbols=earnings,
    )


def _plan(
    tier: int = 1,
    mode: str = "TIER1_PSQ",
    legs: Tuple[InverseLeg, ...] = (),
    liquidate: Tuple[str, ...] = (),
    frozen: Optional[str] = None,
    notes: Tuple[str, ...] = (),
    funding: float = 0.0,
) -> HedgePlan:
    return HedgePlan(
        tier=tier,
        mode=mode,
        nav=100000.0,
        net_delta_before_pct=80.0,
        net_delta_after_pct=40.0,
        legs=legs,
        liquidate_symbols=liquidate,
        liquidate_value=1000.0 * len(liquidate),
        sell_for_funding_value=funding,
        boxx_target_value=20000.0,
        within_guard=True,
        minus3x_frozen_reason=frozen,
        notes=notes,
    )


def _text(embed: discord.Embed) -> str:
    parts: List[Any] = [embed.title, embed.description]
    for f in embed.fields:
        parts += [f.name, f.value]
    if embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(str(p) for p in parts if p)


def _field(embed: discord.Embed, name: str) -> str:
    for f in embed.fields:
        if f.name == name:
            return str(f.value)
    raise AssertionError(f"missing field {name}")


def test_tier1_final_has_psq_stop_market_and_no_minus3x() -> None:
    embed = create_regime_hedge_embed(
        _plan(legs=(_leg("PSQ"),)),
        market_regime="震盪",
        is_preliminary=False,
        reason="測試",
    )
    assert "第一級避險" in str(embed.title)
    orders = _field(embed, "反向 ETF 委託")
    assert "PSQ" in orders and "STOP MARKET" in orders and "−3.5%" in orders
    assert "禁用" in orders and "STOP_LIMIT" in orders
    assert "SQQQ" not in orders and "SOXS" not in orders
    assert "顧問建議" in str(embed.footer.text)


def test_preliminary_hides_shares_and_stop_prices() -> None:
    embed = create_regime_hedge_embed(
        _plan(legs=(_leg("PSQ"),)),
        market_regime="震盪",
        is_preliminary=True,
        reason="測試",
    )
    assert str(embed.title).startswith("⏳ 預警觀察｜")
    orders = _field(embed, "反向 ETF 委託")
    assert "股" not in orders and "STOP" not in orders and "觸發" not in orders
    assert "10% NAV" in orders
    assert "10:00 ET" in str(embed.footer.text)


def test_tier2_hedge_lists_both_legs_with_7pct_stop() -> None:
    legs = (_leg("SQQQ", stop_pct=7.0), _leg("SOXS", stop_pct=7.0))
    embed = create_regime_hedge_embed(
        _plan(tier=2, mode="HEDGE", legs=legs, funding=5000.0),
        market_regime="負 Gamma",
        is_preliminary=False,
        reason="測試",
    )
    orders = _field(embed, "反向 ETF 委託")
    assert "SQQQ" in orders and "SOXS" in orders
    assert orders.count("−7.0%") == 2
    assert "需減碼支應" in [f.name for f in embed.fields]
    assert "第二級避險" in str(embed.title)


def test_stop_short_mentions_40pct_cap() -> None:
    embed = create_regime_hedge_embed(
        _plan(tier=2, mode="STOP_SHORT", legs=(_leg("SQQQ", stop_pct=7.0),)),
        market_regime="負 Gamma",
        is_preliminary=False,
        reason="測試",
    )
    assert "止損轉空" in str(embed.title)
    assert "40%" in _field(embed, "等效淨 Delta")


def test_macro_freeze_field_rendered() -> None:
    embed = create_regime_hedge_embed(
        _plan(frozen="FOMC 前凍結 -3x"),
        market_regime="震盪",
        is_preliminary=False,
        reason="測試",
    )
    assert "FOMC 前凍結" in _field(embed, "🧊 總經事件凍結")
    assert "本輪不新增反向 ETF" in _field(embed, "反向 ETF 委託")


def test_earnings_isolation_label() -> None:
    embed = create_regime_hedge_embed(
        _plan(tier=2, mode="HEDGE", legs=(_leg("SOXS", earnings=("NVDA",)),)),
        market_regime="負 Gamma",
        is_preliminary=False,
        reason="測試",
    )
    assert "財報隔離 ×0.5：NVDA" in _field(embed, "反向 ETF 委託")


def test_missing_reference_price_shows_pending() -> None:
    leg = InverseLeg("SQQQ", 5000.0, 0.0, 0, 7.0, 0.0, False, ())
    embed = create_regime_hedge_embed(
        _plan(tier=2, mode="HEDGE", legs=(leg,)),
        market_regime="負 Gamma",
        is_preliminary=False,
        reason="測試",
    )
    orders = _field(embed, "反向 ETF 委託")
    assert "$0.00" not in orders and " 0 股" not in orders
    assert "股數待確認" in orders and "參考價缺失" in orders
    assert "觸發價待確認（−7.0%）" in orders


def test_colors_by_tier_and_guard_warning() -> None:
    t1 = create_regime_hedge_embed(
        _plan(), market_regime="震盪", is_preliminary=False, reason="x"
    )
    t2 = create_regime_hedge_embed(
        _plan(tier=2, mode="HEDGE"),
        market_regime="負 Gamma",
        is_preliminary=False,
        reason="x",
    )
    assert t1.color == NexusEmbed(color=discord.Color.orange()).color
    assert t2.color == NexusEmbed(color=discord.Color.red()).color
    assert "超出守門區間" not in _field(t1, "等效淨 Delta")
    bad = _plan()
    object.__setattr__(bad, "within_guard", False)
    e = create_regime_hedge_embed(
        bad, market_regime="震盪", is_preliminary=False, reason="x"
    )
    assert "⚠️ 超出守門區間" in _field(e, "等效淨 Delta")


def test_frozen_note_deduped_and_notes_overflow_marker() -> None:
    notes = ("-3x 反向 ETF 已凍結：FOMC", "A", "A") + tuple(f"n{i}" for i in range(12))
    embed = create_regime_hedge_embed(
        _plan(frozen="FOMC", notes=notes),
        market_regime="震盪",
        is_preliminary=False,
        reason="x",
    )
    body = _field(embed, "備註")
    assert "已凍結" not in body
    assert body.count("• A") == 1
    assert "另 3 則略" in body


def test_worst_case_within_discord_limits() -> None:
    legs = (
        _leg("SQQQ", stop_pct=7.0, earnings=tuple(f"SYM{i}" for i in range(15))),
        _leg("SOXS", stop_pct=7.0, earnings=tuple(f"SYM{i}" for i in range(15))),
    )
    notes = tuple(f"備註{i % 10}" + "很長的說明" * 60 for i in range(15))
    plan = _plan(
        tier=2,
        mode="STOP_SHORT",
        legs=legs,
        liquidate=tuple(f"TICK{i}" for i in range(30)),
        frozen="總經事件" * 400,
        notes=notes,
        funding=50000.0,
    )
    embed = create_regime_hedge_embed(
        plan,
        market_regime="負 Gamma" * 50,
        is_preliminary=False,
        reason="很長的原因" * 2000,
    )
    assert "等共 30 檔" in _field(embed, "清倉")
    assert len(str(embed.title)) <= 256
    assert len(str(embed.description)) <= 4096
    assert len(embed.fields) <= 25
    assert all(len(f.name or "") <= 256 for f in embed.fields)
    assert all(len(f.value or "") <= 1024 for f in embed.fields)
    assert len(embed) <= 5800
