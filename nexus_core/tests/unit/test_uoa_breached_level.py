"""
tests/unit/test_uoa_breached_level.py

STO 於首次偵測錨點價被標為地板／天花板，現價其後穿越履約價 → 文案須改為「失效」。
（PLTR 2026-10：$190 STO PUT 於現價 196 偵測，跌至 188.32 後仍寫「強力構築下行支撐地板」。）
"""

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from cogs.embed_builders.portfolio_embeds import _format_uoa_field
from market_analysis.sentiment.uoa_detector import _process_uoa_candidate_rows
from market_analysis.uoa_telemetry import (
    annotate_spread_structures,
    apply_breached_level,
)

BTO = "🟢 買入開倉 (BTO - Ask)"
STO = "🔴 賣出開倉 (STO - Bid)"

PUT_FLOOR = "強力構築下行支撐地板"
CALL_CEIL_SHORT = "物理鎖死上方天花板"
CALL_CEIL_LONG = "物理封頂鎖死上方天花板"


def _put_intent(claim: str = PUT_FLOOR) -> str:
    return f"🛡️ 機構在 $190.00 開倉賣出 2,000 口 PUT (OI=500)，{claim}"


def test_put_floor_rewritten_when_spot_below_strike() -> None:
    intent = _put_intent()
    new, breached = apply_breached_level(intent, "PUT", STO, 190.0, 188.32)
    assert breached is True
    assert PUT_FLOOR not in new
    assert new.endswith("現價已跌破、地板失效")
    assert len(new) <= len(intent)


def test_put_floor_unchanged_when_spot_at_or_above_strike() -> None:
    intent = _put_intent()
    assert apply_breached_level(intent, "PUT", STO, 190.0, 190.0) == (intent, False)
    assert apply_breached_level(intent, "PUT", STO, 190.0, 196.0) == (intent, False)


def test_call_ceiling_rewritten_when_spot_above_strike() -> None:
    for claim in (CALL_CEIL_SHORT, CALL_CEIL_LONG):
        intent = f"🛡️ 機構在 $200.00 賣出 CALL，{claim}"
        new, breached = apply_breached_level(intent, "CALL", STO, 200.0, 203.0)
        assert breached is True
        assert claim not in new
        assert new.endswith("現價已破、封頂失效")
        assert len(new) <= len(intent)
        assert apply_breached_level(intent, "CALL", STO, 200.0, 200.0) == (
            intent,
            False,
        )
        assert apply_breached_level(intent, "CALL", STO, 200.0, 190.0) == (
            intent,
            False,
        )


def test_bto_and_itm_atm_wordings_untouched() -> None:
    intent = _put_intent()
    assert apply_breached_level(intent, "PUT", BTO, 190.0, 180.0) == (intent, False)
    for text in (
        "賣出 1 口價內 PUT，不構成下方支撐地板",
        "賣出 1 口平價 PUT，短線承接現價附近賣壓，非遠端地板",
        "賣出 1 口價內 CALL，不構成上方天花板",
        "賣出 1 口平價 CALL，短線壓制現價附近波動，非遠端封頂",
    ):
        opt = "PUT" if "PUT" in text else "CALL"
        assert apply_breached_level(text, opt, STO, 190.0, 100.0) == (text, False)
        assert apply_breached_level(text, opt, STO, 190.0, 300.0) == (text, False)


def test_rewrite_never_longer_than_original_clause() -> None:
    for claim, repl in (
        (PUT_FLOOR, "現價已跌破、地板失效"),
        (CALL_CEIL_SHORT, "現價已破、封頂失效"),
        (CALL_CEIL_LONG, "現價已破、封頂失效"),
    ):
        assert len(repl) <= len(claim)
    assert len("賣方地板已破（貸方價差，定義風險承接）") <= len(
        "賣方看守地板（貸方價差，定義風險承接）"
    )
    assert len("賣方上限已破（貸方價差，定義風險封頂）") <= len(
        "賣方看跌上限（貸方價差，定義風險封頂）"
    )


# ── 偵測器層 ────────────────────────────────────────────────


def _put_row() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strike": 190.0,
                "option_type": "PUT",
                "volume": 2000.0,
                "openInterest": 500.0,
                "lastPrice": 3.0,
                "bid": 3.0,
                "ask": 3.4,
                "impliedVolatility": 0.6,
            }
        ]
    )


def _detect(symbol: str, spot: float) -> dict[str, Any]:
    today = datetime.now().date()
    exp = (today + timedelta(days=7)).isoformat()
    out = _process_uoa_candidate_rows(symbol, exp, today, _put_row(), 1e9, spot, 5e8)
    assert out, "應偵測到 UOA"
    return out[0]


def test_detector_rewrites_floor_after_spot_breaks_strike() -> None:
    first = _detect("PLTRX", 196.0)
    assert PUT_FLOOR in first["intent"]
    assert first["level_breached"] is False

    later = _detect("PLTRX", 188.32)
    assert later["level_breached"] is True
    assert PUT_FLOOR not in later["intent"]
    assert "現價已跌破、地板失效" in later["intent"]
    assert later["classified_spot"] == 196.0
    assert later["intent"].endswith(later["moneyness_basis_note"])
    assert later["moneyness_basis_note"]


def test_detector_intent_unchanged_when_spot_equals_anchor() -> None:
    first = _detect("PLTRY", 196.0)
    again = _detect("PLTRY", 196.0)
    assert again["intent"] == first["intent"]
    assert again["level_breached"] is False
    assert again["moneyness_basis_note"] == ""


def test_detector_output_flows_into_bull_put_spread() -> None:
    _detect("PLTRZ", 196.0)
    short_leg = _detect("PLTRZ", 188.32)
    assert short_leg["level_breached"] is True
    long_leg: dict[str, Any] = {
        "expiry": short_leg["expiry"],
        "type": "PUT",
        "strike": 185.0,
        "volume": short_leg["volume"],
        "action": BTO,
        "ratio": 5.0,
        "intent": "🔥 在 $185 買入",
    }
    annotate_spread_structures([short_leg, long_leg])
    assert short_leg["spread_credit"] is True
    assert "賣方地板已破（貸方價差，定義風險承接）" in short_leg["intent"]
    assert "賣方看守地板" not in short_leg["intent"]
    assert PUT_FLOOR not in short_leg["intent"]
    assert short_leg["intent"].endswith(short_leg["moneyness_basis_note"])


def test_bull_put_role_note_unchanged_without_breach() -> None:
    s: dict[str, Any] = {
        "expiry": "2026-10-23",
        "type": "PUT",
        "strike": 100.0,
        "volume": 1000,
        "action": STO,
        "ratio": 5.0,
        "intent": f"🛡️ 機構在 $100 開倉賣出，{PUT_FLOOR}",
    }
    b = dict(s, strike=95.0, action=BTO, intent="🔥 在 $95 買入")
    annotate_spread_structures([s, b])
    assert "賣方看守地板（貸方價差，定義風險承接）" in s["intent"]


def test_bear_call_short_leg_with_breach_gets_breach_role_note() -> None:
    s: dict[str, Any] = {
        "expiry": "2026-10-23",
        "type": "CALL",
        "strike": 280.0,
        "volume": 1000,
        "action": STO,
        "ratio": 5.0,
        "level_breached": True,
        "intent": "🛡️ 機構在 $280 賣出，現價已破、封頂失效",
    }
    b = dict(s, strike=290.0, action=BTO, level_breached=False, intent="🔥 買入")
    annotate_spread_structures([s, b])
    assert "賣方上限已破（貸方價差，定義風險封頂）" in s["intent"]


# ── 舊版快取後備渲染 ─────────────────────────────────────────


def test_legacy_cache_fallback_has_no_hardcoded_floor_or_ceiling() -> None:
    data = [
        {
            "symbol": "PLTR",
            "expiry": "2026-10-23",
            "strike": 190.0,
            "type": "PUT",
            "volume": 2000,
            "oi": 500,
            "ratio": 4.0,
            "trade_type": "BLOCK",
        },
        {
            "symbol": "PLTR",
            "expiry": "2026-10-23",
            "strike": 200.0,
            "type": "CALL",
            "volume": 2000,
            "oi": 500,
            "ratio": 4.0,
            "trade_type": "BLOCK",
        },
    ]
    text = _format_uoa_field(data)
    assert PUT_FLOOR not in text
    assert CALL_CEIL_LONG not in text
