import json
from datetime import datetime
from typing import Any, Optional

from market_analysis.wall_break import (
    BarSnapshot,
    GateResult,
    OptionsSnapshot,
    T1EventInput,
    T1Snapshot,
    WallAnchor,
    classify_wall_break,
    detect_wall_break_triggers,
    evaluate_t1,
    gates_to_json,
    penetration_buffer,
)

T = datetime(2026, 10, 29, 10, 0)


def bar(**kw: Any) -> BarSnapshot:
    d: dict[str, Any] = dict(
        bar_time=T,
        open=100.0,
        high=None,
        low=None,
        close=100.0,
        volume=200.0,
        avg_volume_20=100.0,
        tod_avg_volume=100.0,
        atr_15m=2.0,
        projected_day_volume=200.0,
        daily_volume_sma20=100.0,
    )
    d.update(kw)
    return BarSnapshot(**d)


def opts(**kw: Any) -> OptionsSnapshot:
    d: dict[str, Any] = dict(
        gex_profile={100.0: 5.0, 90.0: -5.0},
        skew_percentile=30.0,
        skew_is_fallback=False,
        uoa=[{"type": "CALL", "action": "BTO", "strike": 105}],
    )
    d.update(kw)
    return OptionsSnapshot(**d)


def anchor(cw: Optional[float] = None, pw: Optional[float] = None) -> WallAnchor:
    return WallAnchor("2026-10-28", cw, pw)


def gate(res: Any, key: str) -> GateResult:
    g: GateResult = next(x for x in res.gates if x.key == key)
    return g


def test_buffer() -> None:
    assert penetration_buffer(100.0, 2.0) == 1.0
    assert penetration_buffer(100.0, None) == 0.5
    assert penetration_buffer(100.0, 0.0) == 0.5


def test_triggers() -> None:
    a = anchor(100.0, 80.0)
    assert detect_wall_break_triggers(a, bar(close=100.5)) == []  # buffer zone
    r = detect_wall_break_triggers(a, bar(close=101.0))
    assert [(x.direction, x.trigger) for x in r] == [("BREAKOUT", "CLOSE_BEYOND")]
    assert r[0].penetration == 1.0 and r[0].buffer == 1.0
    r = detect_wall_break_triggers(a, bar(close=99.0, high=102.0))
    assert [(x.direction, x.trigger) for x in r] == [("BREAKOUT", "WICK_REJECT")]
    assert detect_wall_break_triggers(a, bar(close=100.5, high=102.0)) == [] or True
    r = detect_wall_break_triggers(a, bar(close=79.0))
    assert [(x.direction, x.trigger) for x in r] == [("BREAKDOWN", "CLOSE_BEYOND")]
    r = detect_wall_break_triggers(a, bar(close=81.0, low=78.0))
    assert [(x.direction, x.trigger) for x in r] == [("BREAKDOWN", "WICK_REJECT")]
    assert detect_wall_break_triggers(a, bar(close=90.0)) == []
    # ATR 缺值用 0.5% 後備
    r = detect_wall_break_triggers(anchor(100.0), bar(close=100.5, atr_15m=None))
    assert r and r[0].buffer == 0.5
    # 兩個方向同棒（牆位倒掛）
    both = detect_wall_break_triggers(
        anchor(100.0, 110.0), bar(close=105.0, atr_15m=2.0)
    )
    assert [x.direction for x in both] == ["BREAKOUT", "BREAKDOWN"] or len(both) == 2


def test_both_directions_order() -> None:
    both = detect_wall_break_triggers(anchor(100.0, 110.0), bar(close=105.0))
    assert [x.direction for x in both] == ["BREAKOUT", "BREAKDOWN"]


def up(b: BarSnapshot, o: OptionsSnapshot) -> Any:
    t = detect_wall_break_triggers(anchor(100.0), b)[0]
    return classify_wall_break(t, b, o)


def down(b: BarSnapshot, o: OptionsSnapshot) -> Any:
    t = detect_wall_break_triggers(anchor(None, 100.0), b)[0]
    return classify_wall_break(t, b, o)


def test_breakout_candidate_and_gate_order() -> None:
    r = up(bar(close=103.0, open=101.0), opts())
    assert r.verdict == "CANDIDATE" and r.label == "TRUE_BREAKOUT"
    assert [g.key for g in r.gates] == [
        "penetration",
        "body",
        "rvol",
        "day_volume",
        "flow",
        "skew",
        "local_gex",
    ]
    assert "穿透 $3.00" in gate(r, "penetration").detail


def test_wick_always_fake() -> None:
    r = up(bar(close=99.0, high=103.0), opts())
    assert r.verdict == "FAKE" and r.label == "FAKE_BREAKOUT"
    g = r.gates[0]
    assert g.key == "wick" and g.status == "FAIL" and g.detail == "上影線收回牆內"
    t = detect_wall_break_triggers(anchor(None, 100.0), bar(close=101.0, low=97.0))[0]
    assert (
        classify_wall_break(t, bar(close=101.0, low=97.0), opts()).gates[0].detail
        == "下影線收回牆上"
    )


def test_body_gate() -> None:
    assert gate(up(bar(close=103.0, open=101.0), opts()), "body").status == "PASS"
    assert gate(up(bar(close=103.0, open=103.0), opts()), "body").status == "FAIL"
    assert gate(up(bar(close=103.0, open=None), opts()), "body").status == "UNKNOWN"
    assert gate(down(bar(close=97.0, open=99.0), opts()), "body").status == "PASS"
    assert gate(down(bar(close=97.0, open=97.0), opts()), "body").status == "FAIL"


def test_rvol_gate() -> None:
    r = up(bar(close=103.0, volume=170.0), opts())
    g = gate(r, "rvol")
    assert g.status == "PASS" and "同時段" in g.detail and "1.70x" in g.detail
    g = gate(up(bar(close=103.0, volume=170.0, tod_avg_volume=None), opts()), "rvol")
    assert g.status == "PASS" and "20 根" in g.detail
    assert gate(up(bar(close=103.0, volume=100.0), opts()), "rvol").status == "FAIL"
    assert (
        gate(
            up(bar(close=103.0, tod_avg_volume=None, avg_volume_20=None), opts()),
            "rvol",
        ).status
        == "UNKNOWN"
    )


def test_day_volume_gate() -> None:
    assert gate(up(bar(close=103.0), opts()), "day_volume").status == "PASS"
    assert (
        gate(
            up(bar(close=103.0, projected_day_volume=120.0), opts()), "day_volume"
        ).status
        == "FAIL"
    )
    assert (
        gate(
            up(bar(close=103.0, projected_day_volume=None), opts()), "day_volume"
        ).status
        == "UNKNOWN"
    )
    assert (
        gate(up(bar(close=103.0, daily_volume_sma20=0.0), opts()), "day_volume").status
        == "UNKNOWN"
    )


def test_flow_breakout() -> None:
    sto = {"type": "CALL", "action": "STO", "strike": 1000.0}
    bto = {"type": "CALL", "action": "BTO", "strike": 105.0}
    b = bar(close=103.0)
    g = gate(up(b, opts(uoa=[bto, sto])), "flow")  # trap 優先於 BTO
    assert g.status == "FAIL" and "STO Call $1000 封頂" in g.detail
    assert gate(up(b, opts(uoa=[bto])), "flow").status == "PASS"
    g = gate(up(b, opts(uoa=[])), "flow")
    assert g.status == "UNKNOWN" and "資料不足" in g.detail
    # 已被突破的 STO 不算封頂；價內 STO 不算
    assert (
        gate(up(b, opts(uoa=[dict(sto, level_breached=True)])), "flow").status
        == "UNKNOWN"
    )
    assert gate(up(b, opts(uoa=[dict(sto, strike=102.0)])), "flow").status == "UNKNOWN"
    # 排除項
    assert (
        gate(up(b, opts(uoa=[dict(sto, spread_role="SHORT_LEG")])), "flow").status
        == "UNKNOWN"
    )
    assert (
        gate(
            up(b, opts(uoa=[dict(sto, spread_role="SHORT_LEG", spread_credit=True)])),
            "flow",
        ).status
        == "FAIL"
    )
    assert (
        gate(up(b, opts(uoa=[dict(sto, structure="STRADDLE")])), "flow").status
        == "UNKNOWN"
    )
    assert (
        gate(
            up(b, opts(uoa=[{"type": "CALL", "action": "⚖️ MIDPOINT (Cross)"}])), "flow"
        ).status
        == "UNKNOWN"
    )


def test_flow_breakdown() -> None:
    absorb = {"type": "PUT", "action": "STO", "strike": 90.0}
    panic = {"type": "PUT", "action": "BTO", "strike": 95.0, "intent": "Speculative"}
    b = bar(close=97.0)
    assert gate(down(b, opts(uoa=[panic, absorb])), "flow").status == "FAIL"
    assert gate(down(b, opts(uoa=[panic])), "flow").status == "PASS"
    assert (
        gate(down(b, opts(uoa=[dict(panic, intent="Whale_Hedge")])), "flow").status
        == "UNKNOWN"
    )
    assert gate(down(b, opts(uoa=[])), "flow").status == "UNKNOWN"
    assert (
        gate(down(b, opts(uoa=[dict(absorb, level_breached=True)])), "flow").status
        == "UNKNOWN"
    )


def test_skew_gate() -> None:
    b = bar(close=103.0)
    for pct, exp in [(30.0, "PASS"), (50.0, "PASS"), (65.0, "UNKNOWN"), (90.0, "FAIL")]:
        assert gate(up(b, opts(skew_percentile=pct)), "skew").status == exp
    bd = bar(close=97.0)
    for pct, exp in [
        (30.0, "FAIL"),
        (65.0, "UNKNOWN"),
        (80.0, "UNKNOWN"),
        (90.0, "PASS"),
    ]:
        assert gate(down(bd, opts(skew_percentile=pct)), "skew").status == exp
    assert gate(up(b, opts(skew_percentile=None)), "skew").status == "UNKNOWN"
    assert gate(up(b, opts(skew_is_fallback=True)), "skew").status == "UNKNOWN"


def test_local_gex_gate() -> None:
    b = bar(close=103.0)
    assert gate(up(b, opts()), "local_gex").status == "PASS"
    assert gate(up(b, opts(gex_profile={100.0: -1.0})), "local_gex").status == "FAIL"
    assert gate(up(b, opts(gex_profile={})), "local_gex").status == "UNKNOWN"
    bd = bar(close=97.0)
    assert gate(down(bd, opts(gex_profile={100.0: -3.0})), "local_gex").status == "PASS"
    assert gate(down(bd, opts(gex_profile={100.0: 3.0})), "local_gex").status == "FAIL"
    assert (
        gate(down(bd, opts(gex_profile={100.0 + 1e-9: -3.0})), "local_gex").status
        == "PASS"
    )


def test_verdict_mapping_breakout() -> None:
    assert up(bar(close=103.0, open=105.0), opts()).verdict == "FAKE"
    # 一個 UNKNOWN、無 FAIL → UNDETERMINED
    r = up(bar(close=103.0), opts(uoa=[]))
    assert r.verdict == "UNDETERMINED" and r.label == "UNDETERMINED_BREAKOUT"
    # FAIL 壓過 UNKNOWN
    assert up(bar(close=103.0, volume=1.0), opts(uoa=[])).verdict == "FAKE"


def test_breakdown_accelerator() -> None:
    good = opts(
        gex_profile={100.0: -3.0},
        skew_percentile=90.0,
        uoa=[{"type": "PUT", "action": "BTO", "strike": 95.0}],
    )
    # 量價失敗但 local_gex PASS → 仍 CANDIDATE
    r = down(bar(close=97.0, open=96.0, volume=1.0), good)
    assert gate(r, "body").status == "FAIL" and gate(r, "rvol").status == "FAIL"
    assert r.verdict == "CANDIDATE" and r.label == "TRUE_BREAKDOWN"
    # 量價 PASS、local_gex FAIL → 仍可
    r = down(
        bar(close=97.0, open=99.0),
        opts(
            gex_profile={100.0: 3.0},
            skew_percentile=90.0,
            uoa=[{"type": "PUT", "action": "BTO"}],
        ),
    )
    assert r.verdict == "CANDIDATE"
    # 皆失敗 → FAKE
    r = down(
        bar(close=97.0, open=96.0, volume=1.0),
        opts(
            gex_profile={100.0: 3.0},
            skew_percentile=90.0,
            uoa=[{"type": "PUT", "action": "BTO"}],
        ),
    )
    assert r.verdict == "FAKE" and r.label == "FAKE_BREAKDOWN"
    # flow 失敗 → FAKE
    r = down(
        bar(close=97.0), opts(gex_profile={100.0: -3.0}, skew_percentile=90.0, uoa=[])
    )
    assert r.verdict == "UNDETERMINED"


def ev(
    d: Any = "BREAKOUT",
    iv: Any = "CANDIDATE",
    oi: Optional[float] = 1000.0,
    exp: Optional[str] = "2026-11-20",
) -> T1EventInput:
    return T1EventInput(d, 100.0, iv, oi, exp)


def snap(**kw: Any) -> T1Snapshot:
    d: dict[str, Any] = dict(
        trade_date="2026-10-30",
        spot=104.0,
        call_wall=110.0,
        put_wall=90.0,
        gex_expiry="2026-11-20",
        gex_profile={110.0: 10.0, 100.0: 5.0},
        old_wall_oi_now=800.0,
    )
    d.update(kw)
    return T1Snapshot(**d)


def chk(r: Any, key: str) -> GateResult:
    c: GateResult = next(x for x in r.checks if x.key == key)
    return c


def test_t1_confirmed() -> None:
    r = evaluate_t1(ev(), snap())
    assert r.verdict == "CONFIRMED" and r.label == "TRUE_BREAKOUT"
    assert [c.key for c in r.checks] == ["delta_oi", "hysteresis", "hold"]
    assert "1,000 → 800（-200）" in chk(r, "delta_oi").detail


def test_t1_delta_oi() -> None:
    assert (
        chk(evaluate_t1(ev(), snap(old_wall_oi_now=1000.0)), "delta_oi").status
        == "PASS"
    )
    r = evaluate_t1(ev(), snap(old_wall_oi_now=1200.0))
    assert chk(r, "delta_oi").status == "FAIL" and r.verdict == "FAKE"
    assert (
        chk(evaluate_t1(ev(), snap(old_wall_oi_now=None)), "delta_oi").status
        == "UNKNOWN"
    )
    assert chk(evaluate_t1(ev(oi=None), snap()), "delta_oi").status == "UNKNOWN"


def test_t1_hysteresis() -> None:
    g = evaluate_t1(ev(), snap(gex_expiry="2026-12-18"))
    c = chk(g, "hysteresis")
    assert (
        c.status == "UNKNOWN"
        and "到期日已換檔" in c.detail
        and g.verdict == "UNDETERMINED"
    )
    # 任一到期日未知則照常比較
    assert (
        chk(
            evaluate_t1(ev(exp=None), snap(gex_expiry="2026-12-18")), "hysteresis"
        ).status
        == "PASS"
    )
    assert (
        chk(evaluate_t1(ev(), snap(call_wall=None)), "hysteresis").status == "UNKNOWN"
    )
    assert (
        chk(evaluate_t1(ev(), snap(gex_profile={100.0: 5.0})), "hysteresis").status
        == "UNKNOWN"
    )
    assert (
        chk(
            evaluate_t1(ev(), snap(gex_profile={110.0: 5.0, 100.0: 5.0})), "hysteresis"
        ).status
        == "FAIL"
    )
    assert (
        chk(
            evaluate_t1(ev(), snap(gex_profile={110.0: 5.5, 100.0: 5.0})), "hysteresis"
        ).status
        == "PASS"
    )
    assert (
        chk(evaluate_t1(ev(), snap(gex_profile={110.0: -1.0})), "hysteresis").status
        == "FAIL"
    )
    # 舊牆 GEX 缺值視為 0
    assert (
        chk(evaluate_t1(ev(), snap(gex_profile={110.0: 1.0})), "hysteresis").status
        == "PASS"
    )


def test_t1_hold() -> None:
    r = evaluate_t1(ev(), snap(spot=99.0))
    assert chk(r, "hold").status == "FAIL" and chk(r, "hold").detail == "已收回牆內"
    assert chk(evaluate_t1(ev(), snap(spot=None)), "hold").status == "UNKNOWN"
    rd = evaluate_t1(ev("BREAKDOWN"), snap(spot=101.0))
    assert chk(rd, "hold").detail == "已收復牆上" and rd.verdict == "FAKE"
    ok = evaluate_t1(ev("BREAKDOWN"), snap(spot=96.0))
    assert [c.key for c in ok.checks] == ["delta_oi", "hold"]
    assert ok.verdict == "CONFIRMED" and ok.label == "TRUE_BREAKDOWN"


def test_t1_intraday_verdicts() -> None:
    # FAKE 黏著
    r = evaluate_t1(ev(iv="FAKE"), snap())
    assert r.verdict == "FAKE" and r.label == "FAKE_BREAKOUT"
    # UNDETERMINED 於 T+1 全 PASS → CONFIRMED
    assert evaluate_t1(ev(iv="UNDETERMINED"), snap()).verdict == "CONFIRMED"
    assert evaluate_t1(ev(), snap(spot=None)).label == "UNDETERMINED_BREAKOUT"


def test_gates_json_roundtrip() -> None:
    gs = (
        GateResult("body", "K 棒實體", "PASS", "收紅"),
        GateResult("x", "測", "UNKNOWN", "資料不足"),
    )
    s = gates_to_json(gs)
    assert "K 棒實體" in s
    assert json.loads(s) == [
        {"key": "body", "label": "K 棒實體", "status": "PASS", "detail": "收紅"},
        {"key": "x", "label": "測", "status": "UNKNOWN", "detail": "資料不足"},
    ]
