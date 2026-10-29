from datetime import datetime
from typing import Any

import pytest
from cogs.embed_builders.wall_break_embeds import (
    create_wall_break_embed,
    create_wall_break_t1_embed,
)
from market_analysis.wall_break import (
    GateResult,
    T1EventInput,
    T1Result,
    WallBreakResult,
    WallBreakTrigger,
)

GATES = (
    GateResult(
        "penetration", "穿透幅度", "PASS", "穿透 $3.20 ≥ 緩衝 $2.45（0.5×ATR15m）"
    ),
    GateResult("skew", "偏斜百分位", "UNKNOWN", "Skew 百分位資料不足"),
    GateResult("flow", "選擇權流向", "FAIL", "x"),
)


def trig(direction: Any, wall: float) -> WallBreakTrigger:
    return WallBreakTrigger(
        direction,
        "CLOSE_BEYOND",
        wall,
        1.0,
        3.2,
        "2026-10-28",
        datetime(2026, 10, 29, 10, 15),
        413.2,
    )


def texts(e: Any) -> str:
    return "\n".join(
        [e.title or "", (e.description or "")]
        + [f.name + f.value for f in e.fields]
        + [(e.footer.text or "")]
    )


def test_intraday_breakout() -> None:
    e = create_wall_break_embed(
        "NVDA", WallBreakResult(trig("BREAKOUT", 410.0), GATES, "CANDIDATE")
    )
    assert e.title == "🧱 NVDA 突破前日 CallWall $410.00｜突破候選（待 T+1 確認）"
    assert (
        "✅ 穿透幅度" in (e.description or "")
        and "❔ 偏斜百分位" in (e.description or "")
        and "❌ 選擇權流向" in (e.description or "")
    )
    assert "10-29 10:15" in (e.description or "") and "2026-10-28" in (
        e.description or ""
    )
    v = str(e.fields[0].value)
    assert v.startswith("（候選，待 T+1 確認；次日未收到確認推播即視為未成立）")
    assert "$405.90–$407.54" in v and "CallWall $410.00" in v
    assert (
        "門檻未校準（PRE_CALIBRATION）｜錨點＝2026-10-28 收盤牆｜UOA 方向為日累積推斷"
        in (e.footer.text or "")
    )
    assert len(texts(e)) < 2000


def test_intraday_breakdown() -> None:
    e = create_wall_break_embed(
        "NVDA", WallBreakResult(trig("BREAKDOWN", 380.0), GATES, "CANDIDATE")
    )
    assert e.title == "🧱 NVDA 跌破前日 PutWall $380.00｜跌破候選（待 T+1 確認）"
    assert "嚴禁左側接刀" in str(e.fields[0].value)


def test_t1_embeds() -> None:
    ev = T1EventInput("BREAKOUT", 410.0, "CANDIDATE", 100.0, None)
    e = create_wall_break_t1_embed(
        "NVDA", "2026-10-29", ev, T1Result(GATES, "CONFIRMED", "BREAKOUT")
    )
    assert e.title == "🧱 NVDA T+1 定案｜確認突破（CallWall $410.00）"
    assert "$405.90–$407.54" in str(e.fields[0].value)
    assert "候選" not in str(e.fields[0].value)
    assert (e.footer.text or "").endswith(
        "門檻未校準（PRE_CALIBRATION）｜事件日 2026-10-29"
    )
    ev2 = T1EventInput("BREAKDOWN", 380.0, "CANDIDATE", 100.0, None)
    e2 = create_wall_break_t1_embed(
        "NVDA", "2026-10-29", ev2, T1Result(GATES, "CONFIRMED", "BREAKDOWN")
    )
    assert e2.title == "🧱 NVDA T+1 定案｜確認跌破（PutWall $380.00）"


@pytest.mark.parametrize("v", ["FAKE", "UNDETERMINED"])
def test_non_pushable_raises(v: Any) -> None:
    with pytest.raises(ValueError):
        create_wall_break_embed(
            "NVDA", WallBreakResult(trig("BREAKOUT", 410.0), GATES, v)
        )
    ev = T1EventInput("BREAKOUT", 410.0, "CANDIDATE", None, None)
    with pytest.raises(ValueError):
        create_wall_break_t1_embed(
            "NVDA", "2026-10-29", ev, T1Result(GATES, v, "BREAKOUT")
        )


def test_naming_rule() -> None:
    ev = T1EventInput("BREAKDOWN", 380.0, "CANDIDATE", None, None)
    for e in (
        create_wall_break_embed(
            "X", WallBreakResult(trig("BREAKOUT", 410.0), GATES, "CANDIDATE")
        ),
        create_wall_break_embed(
            "X", WallBreakResult(trig("BREAKDOWN", 380.0), GATES, "CANDIDATE")
        ),
        create_wall_break_t1_embed(
            "X", "2026-10-29", ev, T1Result(GATES, "CONFIRMED", "BREAKDOWN")
        ),
    ):
        assert "真突破" not in texts(e) and "真跌破" not in texts(e)
