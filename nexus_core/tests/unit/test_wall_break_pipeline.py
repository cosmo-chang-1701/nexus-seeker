"""單元測試：IntradayScanPipeline._dispatch_wall_break_alerts（牆突破推播掛鉤）。

monitor 以 mock 取代，只驗證推播政策：僅盤中 CANDIDATE 與 T+1 CONFIRMED 進 notify，
FAKE／UNDETERMINED 一律只記錄不推播；開關只關推播、不關資料蒐集；例外不外洩。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.intraday_pipeline import IntradayScanPipeline
from market_analysis.wall_break import (
    GateResult,
    T1EventInput,
    T1Snapshot,
    WallBreakResult,
    WallBreakTrigger,
    evaluate_t1,
)
from services.wall_break_service import WallBreakOutcome

_GATES = (GateResult("body", "K 棒實體", "PASS", "收紅 K"),)


def _result(
    verdict: str, direction: str = "BREAKOUT", kind: str = "CLOSE_BEYOND"
) -> WallBreakResult:
    trig = WallBreakTrigger(
        direction=direction,  # type: ignore[arg-type]
        trigger=kind,  # type: ignore[arg-type]
        old_wall=100.0,
        buffer=0.5,
        penetration=1.0,
        anchor_trade_date="2026-10-28",
        bar_time=datetime(2026, 10, 29, 11, 15),
        close=101.0,
    )
    return WallBreakResult(trig, _GATES, verdict)  # type: ignore[arg-type]


def _t1(
    intraday_verdict: str, oi_before: Optional[float], oi_now: Optional[float]
) -> tuple[dict[str, Any], T1EventInput, Any]:
    ev = T1EventInput(
        direction="BREAKOUT",
        old_wall=100.0,
        intraday_verdict=intraday_verdict,  # type: ignore[arg-type]
        old_wall_oi=oi_before,
        gex_expiry="2026-10-30",
    )
    snap = T1Snapshot(
        trade_date="2026-10-29",
        spot=101.0,
        call_wall=105.0,
        put_wall=85.0,
        gex_expiry="2026-10-30",
        gex_profile={100.0: 1000.0, 105.0: 3000.0},
        old_wall_oi_now=oi_now,
    )
    return {"trade_date": "2026-10-28", "id": 1}, ev, evaluate_t1(ev, snap)


def _eval() -> Any:
    return SimpleNamespace(metrics=SimpleNamespace(current_price=101.5))


def _pipe(outcome: WallBreakOutcome) -> tuple[Any, AsyncMock]:
    pipe = IntradayScanPipeline(MagicMock())
    monitor = MagicMock()
    monitor.process_symbol = AsyncMock(return_value=outcome)
    pipe._wall_break_monitor = monitor
    return pipe, monitor.process_symbol


def _last(m: AsyncMock) -> Any:
    call: Any = m.await_args
    return call.args, call.kwargs


async def _run(pipe: Any, uid: int = 7, enabled: bool = True) -> AsyncMock:
    sent = AsyncMock(return_value=True)
    with patch("services.notification_dispatcher.notify", new=sent), patch(
        "config.WALL_BREAK_ALERT_ENABLED", enabled
    ):
        await pipe._dispatch_wall_break_alerts(uid, "nvda", _eval())
    return sent


async def test_candidate_pushed_with_channel_dedup_and_record() -> None:
    pipe, _ = _pipe(WallBreakOutcome(intraday=[_result("CANDIDATE")]))
    sent = await _run(pipe)
    assert sent.await_count == 1
    args, kwargs = _last(sent)
    assert args[1] == 7
    assert args[2] == "alpha_wall_break"
    assert kwargs["dedup_key"] == "wall_break_7_NVDA_BREAKOUT_CLOSE_BEYOND_20261029"
    rec = kwargs["record"]
    assert rec.symbol == "NVDA"
    assert rec.signal_kind == "INFO"
    assert rec.scenario == "TRUE_BREAKOUT"
    assert rec.action == "WALL_BREAK_CLOSE_BEYOND"
    assert rec.direction == "LONG"
    assert rec.price == 101.0
    assert kwargs["embed"] is not None


async def test_breakdown_candidate_direction_short() -> None:
    pipe, _ = _pipe(
        WallBreakOutcome(intraday=[_result("CANDIDATE", direction="BREAKDOWN")])
    )
    sent = await _run(pipe)
    assert _last(sent)[1]["record"].direction == "SHORT"
    assert "BREAKDOWN" in _last(sent)[1]["dedup_key"]


@pytest.mark.parametrize("verdict", ["FAKE", "UNDETERMINED"])
@pytest.mark.parametrize("kind", ["CLOSE_BEYOND", "WICK_REJECT"])
async def test_non_candidate_never_pushed(verdict: str, kind: str) -> None:
    pipe, _ = _pipe(WallBreakOutcome(intraday=[_result(verdict, kind=kind)]))
    sent = await _run(pipe)
    sent.assert_not_called()


async def test_t1_confirmed_pushed() -> None:
    row, ev, res = _t1("CANDIDATE", 500.0, 400.0)
    assert res.verdict == "CONFIRMED"
    pipe, _ = _pipe(WallBreakOutcome(t1=[(row, ev, res)]))
    sent = await _run(pipe)
    assert sent.await_count == 1
    kwargs = _last(sent)[1]
    assert _last(sent)[0][2] == "alpha_wall_break"
    assert kwargs["dedup_key"] == "wall_break_t1_7_NVDA_BREAKOUT_20261028"
    rec = kwargs["record"]
    assert rec.action == "WALL_BREAK_T1_CLOSE_BEYOND"
    assert rec.scenario == "TRUE_BREAKOUT_T1"
    assert rec.signal_kind == "INFO"
    assert rec.price == 101.5


async def test_t1_confirmed_even_if_intraday_undetermined() -> None:
    row, ev, res = _t1("UNDETERMINED", 500.0, 400.0)
    assert res.verdict == "CONFIRMED"
    pipe, _ = _pipe(WallBreakOutcome(t1=[(row, ev, res)]))
    sent = await _run(pipe)
    assert sent.await_count == 1


async def test_t1_fake_and_undetermined_not_pushed() -> None:
    fake = _t1("FAKE", 500.0, 400.0)
    undet = _t1("CANDIDATE", None, None)
    assert fake[2].verdict == "FAKE"
    assert undet[2].verdict == "UNDETERMINED"
    pipe, _ = _pipe(WallBreakOutcome(t1=[fake, undet]))
    sent = await _run(pipe)
    sent.assert_not_called()


async def test_kill_switch_suppresses_notify_but_collects() -> None:
    row, ev, res = _t1("CANDIDATE", 500.0, 400.0)
    pipe, process = _pipe(
        WallBreakOutcome(intraday=[_result("CANDIDATE")], t1=[(row, ev, res)])
    )
    sent = await _run(pipe, enabled=False)
    process.assert_awaited_once()
    sent.assert_not_called()


async def test_monitor_exception_is_swallowed() -> None:
    pipe, process = _pipe(WallBreakOutcome())
    process.side_effect = RuntimeError("boom")
    sent = await _run(pipe)
    sent.assert_not_called()


async def test_notify_exception_is_swallowed_and_next_still_sent() -> None:
    pipe, _ = _pipe(
        WallBreakOutcome(
            intraday=[_result("CANDIDATE"), _result("CANDIDATE", direction="BREAKDOWN")]
        )
    )
    sent = AsyncMock(side_effect=[RuntimeError("dm failed"), True])
    with patch("services.notification_dispatcher.notify", new=sent), patch(
        "config.WALL_BREAK_ALERT_ENABLED", True
    ):
        await pipe._dispatch_wall_break_alerts(7, "NVDA", _eval())
    assert sent.await_count == 2


async def test_none_evaluation_does_not_raise() -> None:
    pipe, process = _pipe(WallBreakOutcome())
    with patch("services.notification_dispatcher.notify", new=AsyncMock()):
        await pipe._dispatch_wall_break_alerts(7, "NVDA", None)
    process.assert_awaited_once()
