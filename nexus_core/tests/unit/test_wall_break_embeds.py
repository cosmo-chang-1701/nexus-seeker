import re
from datetime import datetime
from typing import Any

import pytest
from cogs.embed_builders._ansi_utils import _visual_len
from cogs.embed_builders.wall_break_embeds import (
    _gate_lines,
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
    assert not e.description
    trigger, gate, directive = (str(f.value) for f in e.fields)
    assert "✅ 穿透幅度" in gate and "❔ 偏斜百分位" in gate and "❌ 選擇權流向" in gate
    assert "10-29 10:15" in trigger and "2026-10-28" in trigger
    assert "CallWall" in trigger and "$410.00" in trigger
    assert _CANDIDATE in directive
    assert "$405.90–$407.54" in directive and "CallWall $410.00" in directive
    assert (e.footer.text or "").endswith(
        "門檻未校準（PRE_CALIBRATION）｜錨點＝2026-10-28 收盤牆"
    )
    assert len(texts(e)) < 2000


def test_intraday_breakdown() -> None:
    e = create_wall_break_embed(
        "NVDA", WallBreakResult(trig("BREAKDOWN", 380.0), GATES, "CANDIDATE")
    )
    assert e.title == "🧱 NVDA 跌破前日 PutWall $380.00｜跌破候選（待 T+1 確認）"
    assert "嚴禁左側接刀" in str(e.fields[2].value)


def test_t1_embeds() -> None:
    ev = T1EventInput("BREAKOUT", 410.0, "CANDIDATE", 100.0, None)
    e = create_wall_break_t1_embed(
        "NVDA", "2026-10-29", ev, T1Result(GATES, "CONFIRMED", "BREAKOUT")
    )
    assert e.title == "🧱 NVDA T+1 定案｜確認突破（CallWall $410.00）"
    assert not e.description
    assert "$405.90–$407.54" in str(e.fields[2].value)
    assert "候選" not in str(e.fields[2].value)
    assert (e.footer.text or "").endswith(
        "門檻未校準（PRE_CALIBRATION）｜事件日 2026-10-29"
    )
    ev2 = T1EventInput("BREAKDOWN", 380.0, "CANDIDATE", 100.0, None)
    e2 = create_wall_break_t1_embed(
        "NVDA", "2026-10-29", ev2, T1Result(GATES, "CONFIRMED", "BREAKDOWN")
    )
    assert e2.title == "🧱 NVDA T+1 定案｜確認跌破（PutWall $380.00）"


_CANDIDATE = "（候選，待 T+1 確認；次日未收到確認推播即視為未成立）"
_UOA_NOTE = "※ UOA 方向為日累積推斷（非逐筆成交方向）"
_OLD_BREAKOUT = (
    "嚴禁追高現貨與在突破點賣低履約價 Covered Call。"
    "等待縮量回踩原 CallWall $410.00 確認支撐後，"
    "以總資產 1%–2.5% 輕倉配置右側現貨（比照擠壓進場 T1–T3），"
    "結構停損設於 $405.90–$407.54；"
    "或於原底牆賣 30–45 DTE、Delta 0.12–0.15 的現金擔保賣 Put（CSP）。"
)
_OLD_BREAKDOWN = (
    "嚴禁左側接刀，嚴禁在負 GEX 助跌區賣任何 CSP。"
    "保持 100% 現金／BOXX，待恐慌出清、VIX ≥ 22 且下方出現新的正 GEX 承接底牆後，"
    "才進入評估。"
)


def _squash(text: str) -> str:
    text = re.sub(r"\u001b\[[0-9;]*m", "", text)
    text = text.replace("```ansi", "").replace("```", "")
    text = text.replace("├─", "").replace("└─", "")
    return re.sub(r"\s+", "", text)


def _squash_keep(text: str) -> str:
    return re.sub(r"\u001b\[[0-9;]*m", "", text).strip()


def _build(kind: str, direction: Any, gates: Any = GATES) -> Any:
    wall = 410.0 if direction == "BREAKOUT" else 380.0
    if kind == "intraday":
        return create_wall_break_embed(
            "NVDA", WallBreakResult(trig(direction, wall), gates, "CANDIDATE")
        )
    ev = T1EventInput(direction, wall, "CANDIDATE", 100.0, None)
    return create_wall_break_t1_embed(
        "NVDA", "2026-10-29", ev, T1Result(gates, "CONFIRMED", direction)
    )


@pytest.mark.parametrize("kind", ["intraday", "t1"])
@pytest.mark.parametrize("direction", ["BREAKOUT", "BREAKDOWN"])
def test_nexus_embed_layout(kind: str, direction: str) -> None:
    long_gates = tuple(
        GateResult(key, label, "PASS", "長" * 200)
        for key, label in (
            ("penetration", "穿透幅度"),
            ("body", "K 棒實體"),
            ("rvol", "相對量能"),
            ("day_volume", "全日量推估"),
            ("flow", "選擇權流向"),
            ("skew", "偏斜百分位"),
            ("local_gex", "舊牆淨 GEX"),
        )
    )
    for gates in (GATES, long_gates):
        e = _build(kind, direction, gates)
        gate_name = "🧪 判定關卡" if kind == "intraday" else "🧪 T+1 定案檢核"
        assert [f.name for f in e.fields] == ["🎯 觸發事件", gate_name, "📋 操作指引"]
        assert not e.description
        assert "UOA" not in (e.footer.text or "")
        trigger, gate, directive = (str(f.value) for f in e.fields)
        if kind == "intraday":
            assert gate.rstrip().endswith(_UOA_NOTE)
        else:
            assert "※" not in gate
        for v in (trigger, gate, directive):
            assert v.startswith("```ansi")
            assert v.count("```") % 2 == 0
            assert "├─" in v or "└─" in v
        assert all(len(str(f.value)) <= 1024 for f in e.fields)
        lines = [ln for ln in gate.splitlines() if "：" in ln]
        details = [_squash_keep(ln).split("：", 1)[1] for ln in lines]
        if gates is long_gates:
            assert len(details) == 7
            assert all(x.endswith("…") and _visual_len(x) <= 70 for x in details)
        else:
            assert all("…" not in x for x in details)
        old = _OLD_BREAKOUT if direction == "BREAKOUT" else _OLD_BREAKDOWN
        if direction == "BREAKDOWN":
            assert "CallWall $410.00" not in directive
        expect = (_CANDIDATE if kind == "intraday" else "") + old
        assert _squash(directive) == _squash(expect)
        assert (_CANDIDATE in directive) == (kind == "intraday")


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


def test_gate_detail_ellipsis_truncation() -> None:
    def detail(text: str) -> str:
        line = _gate_lines([GateResult("g", "標籤", "PASS", text)])[0]
        return _squash_keep(line).split("標籤：", 1)[1]

    assert detail("長" * 35) == "長" * 35
    assert detail("short") == "short"
    for n in (36, 200):
        r = detail("長" * n)
        assert r.endswith("…")
        assert _visual_len(r) <= 70
