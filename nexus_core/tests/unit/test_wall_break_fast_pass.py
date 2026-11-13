"""單元測試：GEX 牆突破快速偵測路徑（15m K 收盤對齊，:03/:18/:33/:48 ET）。

全部 mock，不碰網路與 DB。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.intraday_pipeline import IntradayScanPipeline
from market_analysis.intraday_pipeline import pipeline as pl
from market_time import ny_tz
from services.wall_break_service import WallBreakMonitor, WallBreakOutcome


def _gex_eval() -> Any:
    return SimpleNamespace(
        symbol_gex={"call_wall": 105.0, "put_wall": 85.0, "expiry": "2026-11-06"},
        metrics=SimpleNamespace(current_price=100.0),
    )


# ------------------------------------------------------------------ detect_only


async def _run_compute(monitor: WallBreakMonitor) -> tuple[AsyncMock, AsyncMock]:
    upsert = AsyncMock()
    settle = AsyncMock()
    now = datetime(2026, 11, 4, 11, 3)
    with patch("services.wall_break_service._now_et", return_value=now), patch(
        "services.wall_break_service.upsert_wall_anchor", new=upsert
    ), patch.object(monitor, "_settle_t1", new=settle), patch(
        "services.wall_break_service.get_latest_wall_anchor_before",
        return_value=None,
    ):
        await monitor.process_symbol("NVDA", _gex_eval())
    return upsert, settle


async def test_detect_only_skips_anchor_upsert_and_t1() -> None:
    upsert, settle = await _run_compute(WallBreakMonitor(detect_only=True))
    upsert.assert_not_called()
    settle.assert_not_called()


async def test_default_monitor_still_upserts_and_settles() -> None:
    upsert, settle = await _run_compute(WallBreakMonitor())
    upsert.assert_awaited_once()
    settle.assert_awaited_once()


# ------------------------------------------------------------------ 時槽


def _secs(h: int, m: int) -> float:
    return pl._seconds_until_next_slot(
        datetime(2026, 11, 4, h, m, tzinfo=ny_tz),
        slot_minutes=pl._WALL_BREAK_FAST_SLOT_MINUTES,
    )


def test_fast_slot_computation() -> None:
    assert _secs(10, 16) == 120.0
    assert _secs(10, 48) == (11 * 60 + 3 - (10 * 60 + 48)) * 60.0
    assert _secs(10, 3) == 15 * 60.0


def test_fast_slot_minimum_sleep() -> None:
    now = datetime(2026, 11, 4, 10, 17, 30, tzinfo=ny_tz)
    got = pl._seconds_until_next_slot(
        now, slot_minutes=pl._WALL_BREAK_FAST_SLOT_MINUTES
    )
    assert got == 60.0


# ------------------------------------------------------------------ fast pass


def _pipe(rows: list[tuple[int, str, bool]]) -> tuple[Any, AsyncMock]:
    pipe = IntradayScanPipeline(MagicMock())
    pipe._wall_break_fast_monitor = MagicMock()
    pipe._wall_break_fast_monitor.begin_cycle = MagicMock()
    pipe._wall_break_monitor = MagicMock()
    dispatch = AsyncMock()
    pipe._dispatch_wall_break_alerts = dispatch  # type: ignore[method-assign]
    pipe._rows = rows  # type: ignore[attr-defined]
    return pipe, dispatch


def _patches(
    pipe: Any,
    *,
    enabled: bool = True,
    market: bool = True,
    mem: bool = True,
) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("config.WALL_BREAK_FAST_PASS_ENABLED", enabled))
    stack.enter_context(patch.object(pl, "is_market_open", return_value=market))
    stack.enter_context(patch("services.llm_service.is_memory_safe", return_value=mem))
    stack.enter_context(patch("database.get_all_watchlist", return_value=pipe._rows))
    stack.enter_context(
        patch(
            "services.notification_dispatch_recorder.flush_dispatch_records",
            new=AsyncMock(),
        )
    )
    return stack


async def test_fast_pass_dedupes_symbol_and_dispatches_per_user() -> None:
    pipe, dispatch = _pipe([(1, "NVDA", True), (2, "nvda", True)])
    ev = _gex_eval()
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), ev)
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    assert dispatch.await_count == 2
    uids = sorted(c.args[0] for c in dispatch.await_args_list)
    assert uids == [1, 2]
    for c in dispatch.await_args_list:
        assert c.kwargs["monitor"] is pipe._wall_break_fast_monitor
        assert c.args[2] is ev
    pipe._wall_break_fast_monitor.begin_cycle.assert_called_once()


async def test_fast_pass_shares_single_compute_across_users() -> None:
    pipe = IntradayScanPipeline(MagicMock())
    mon = MagicMock()
    mon.begin_cycle = MagicMock()
    mon.process_symbol = AsyncMock(return_value=WallBreakOutcome())
    pipe._wall_break_fast_monitor = mon
    pipe._rows = [(1, "NVDA", True), (2, "NVDA", True)]  # type: ignore[attr-defined]
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    # 每位使用者各呼叫一次 process_symbol；實際計算由 monitor 的每輪快取去重
    assert mon.process_symbol.await_count == 2


async def test_fast_pass_skips_missing_or_stale_eval() -> None:
    pipe, dispatch = _pipe([(1, "NVDA", True), (1, "AMD", True)])
    pipe._latest_evals["AMD"] = (
        datetime.now(ny_tz) - timedelta(seconds=pl._FAST_EVAL_MAX_AGE_SECONDS + 5),
        _gex_eval(),
    )
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    dispatch.assert_not_called()


async def test_fast_pass_prunes_unwatched_and_expired_evals() -> None:
    pipe, dispatch = _pipe([(1, "NVDA", True), (1, "AMD", True)])
    now = datetime.now(ny_tz)
    pipe._latest_evals["NVDA"] = (now, _gex_eval())
    pipe._latest_evals["AMD"] = (
        now - timedelta(seconds=pl._FAST_EVAL_MAX_AGE_SECONDS + 5),
        _gex_eval(),
    )
    pipe._latest_evals["TSLA"] = (now, _gex_eval())  # 已不在任何自選清單
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    assert set(pipe._latest_evals) == {"NVDA"}
    assert dispatch.await_count == 1
    assert dispatch.await_args_list[0].args[1] == "NVDA"


async def test_fast_pass_gates_do_not_call_monitor() -> None:
    cases: list[dict[str, Any]] = [
        {"enabled": False},
        {"market": False},
        {"mem": False},
    ]
    for kw in cases:
        pipe, dispatch = _pipe([(1, "NVDA", True)])
        pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
        with _patches(pipe, **kw):
            await pipe._run_wall_break_fast_pass()
        dispatch.assert_not_called()

    pipe, dispatch = _pipe([(1, "NVDA", True)])
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
    pipe.bot._is_leader_instance = False
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    dispatch.assert_not_called()

    pipe, dispatch = _pipe([(1, "NVDA", True)])
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
    pipe._deep_cycle_running = True
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    dispatch.assert_not_called()


async def test_fast_pass_one_symbol_failure_does_not_abort() -> None:
    pipe, dispatch = _pipe([(1, "NVDA", True), (1, "AMD", True)])
    now = datetime.now(ny_tz)
    pipe._latest_evals["NVDA"] = (now, _gex_eval())
    pipe._latest_evals["AMD"] = (now, _gex_eval())
    dispatch.side_effect = [RuntimeError("boom"), None]
    with _patches(pipe):
        await pipe._run_wall_break_fast_pass()
    assert dispatch.await_count == 2


async def test_dispatch_uses_given_monitor() -> None:
    pipe = IntradayScanPipeline(MagicMock())
    deep = MagicMock()
    deep.process_symbol = AsyncMock(return_value=WallBreakOutcome())
    fast = MagicMock()
    fast.process_symbol = AsyncMock(return_value=WallBreakOutcome())
    pipe._wall_break_monitor = deep
    await pipe._dispatch_wall_break_alerts(1, "NVDA", _gex_eval(), monitor=fast)
    fast.process_symbol.assert_awaited_once()
    deep.process_symbol.assert_not_called()


def test_fast_monitor_is_detect_only() -> None:
    pipe = IntradayScanPipeline(MagicMock())
    assert pipe._wall_break_fast_monitor._detect_only is True
    assert pipe._wall_break_monitor._detect_only is False


# ------------------------------------------------------------------ 深度心跳寫入快取


async def test_deep_loop_records_latest_eval() -> None:
    pipe = IntradayScanPipeline(MagicMock())
    pipe.is_running = True
    ev = _gex_eval()
    ev.tactical = SimpleNamespace(alert_level="green")
    pipe.evaluate_watchlist_symbol = AsyncMock(return_value=ev)  # type: ignore[method-assign]
    pipe._dispatch_entry_advisor_alert = AsyncMock()  # type: ignore[method-assign]
    pipe._dispatch_wall_break_alerts = AsyncMock()  # type: ignore[method-assign]
    pipe._load_tactical_option_positions_by_user = AsyncMock(return_value={})  # type: ignore[method-assign]
    pipe._compute_user_tactical_exposure = MagicMock(return_value=0.0)  # type: ignore[method-assign]

    seen: dict[str, Any] = {}

    async def fake_sleep(_s: float) -> None:
        seen["running_at_sleep"] = pipe._deep_cycle_running
        pipe.is_running = False

    ctx = SimpleNamespace(load_failed=False)
    with patch.object(pl, "is_market_open", return_value=True), patch(
        "services.llm_service.is_memory_safe", return_value=True
    ), patch("database.get_all_user_ids", return_value=[1]), patch(
        "database.get_full_user_context", return_value=ctx
    ), patch("database.get_user_watchlist", return_value=[("nvda", True)]), patch(
        "pandas_market_calendars.get_calendar"
    ) as cal, patch(
        "market_analysis.evaluation_recorder.flush_evaluations", new=AsyncMock()
    ), patch(
        "services.notification_dispatch_recorder.flush_dispatch_records",
        new=AsyncMock(),
    ), patch.object(pl.asyncio, "sleep", new=fake_sleep):
        import pandas as pd

        now = datetime.now(ny_tz)
        cal.return_value.schedule.return_value = pd.DataFrame(
            {
                "market_open": [pd.Timestamp(now - timedelta(hours=3))],
                "market_close": [pd.Timestamp(now + timedelta(hours=3))],
            }
        )
        await pipe._run_loop()

    assert "NVDA" in pipe._latest_evals
    assert pipe._latest_evals["NVDA"][1] is ev
    # 睡眠前已還原，快速偵測才能在睡眠期間使用快取
    assert seen["running_at_sleep"] is False
    assert pipe._deep_cycle_running is False


# ------------------------------------------------------------------ 收盤後 16:03 快速偵測


def _frozen_dt(fake_now: datetime) -> type[datetime]:
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return fake_now.astimezone(tz) if tz is not None else fake_now

    return _Fixed


def _et_dt(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ny_tz)


async def test_post_close_1603_detects_closing_bar_break(db_conn: Any) -> None:
    """16:03：快速偵測在收盤後放行，對前一場次錨點偵測 15:45 收盤棒突破（今日 trade_date）。"""
    import market_time
    from database.connection import execute_write_async
    from database.wall_break import get_wall_break_event, upsert_wall_anchor
    from tests.unit.test_wall_break_service import (
        BREAK_BAR,
        PREV,
        TODAY,
        env,
        make_15m,
        make_eval,
    )

    await execute_write_async("DELETE FROM wall_anchor")
    await execute_write_async("DELETE FROM wall_break_events")
    await upsert_wall_anchor("NVDA", PREV, 100.0, 90.0, "2026-10-30")

    pipe = IntradayScanPipeline(MagicMock())
    monitor = WallBreakMonitor(detect_only=True)
    pipe._wall_break_fast_monitor = monitor
    pipe._rows = [(1, "NVDA", True)]  # type: ignore[attr-defined]
    outcomes: list[WallBreakOutcome] = []

    async def _dispatch(uid: int, sym: str, ev: Any, monitor: Any = None) -> None:
        outcomes.append(await monitor.process_symbol(sym, ev))

    pipe._dispatch_wall_break_alerts = _dispatch  # type: ignore[method-assign,assignment]
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), make_eval())
    df = make_15m(last_bar="15:45", overrides={"15:45": BREAK_BAR})
    now = _et_dt(2026, 10, 29, 16, 3)
    try:
        with env(now=f"{TODAY} 16:03", intraday=df, prev_session=None), _patches(
            pipe, market=False
        ), patch("market_time.datetime", _frozen_dt(now)), patch.object(
            pl, "is_market_open", wraps=market_time.is_market_open
        ):
            await pipe._run_wall_break_fast_pass()
        assert len(outcomes) == 1
        assert len(outcomes[0].intraday) == 1
        assert outcomes[0].intraday[0].verdict == "CANDIDATE"
        assert get_wall_break_event("NVDA", TODAY, "BREAKOUT", "CLOSE_BEYOND")
    finally:
        await execute_write_async("DELETE FROM wall_anchor")
        await execute_write_async("DELETE FROM wall_break_events")


async def test_post_close_1618_fast_pass_skipped() -> None:
    """16:18（窗外）、週末、國定假日：快速偵測不跑。"""
    import market_time

    for when in (
        _et_dt(2026, 11, 11, 16, 18),
        _et_dt(2026, 11, 14, 16, 3),
        _et_dt(2026, 11, 26, 16, 3),
    ):
        pipe, dispatch = _pipe([(1, "NVDA", True)])
        pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
        with _patches(pipe, market=False), patch(
            "market_time.datetime", _frozen_dt(when)
        ), patch.object(pl, "is_market_open", wraps=market_time.is_market_open):
            await pipe._run_wall_break_fast_pass()
        dispatch.assert_not_called()

    # 對照組：同日 16:03 在窗內，會派發
    pipe, dispatch = _pipe([(1, "NVDA", True)])
    pipe._latest_evals["NVDA"] = (datetime.now(ny_tz), _gex_eval())
    with _patches(pipe, market=False), patch(
        "market_time.datetime", _frozen_dt(_et_dt(2026, 11, 11, 16, 3))
    ), patch.object(pl, "is_market_open", wraps=market_time.is_market_open):
        await pipe._run_wall_break_fast_pass()
    assert dispatch.await_count == 1


async def test_post_close_deep_loop_does_not_run() -> None:
    """收盤後深度心跳 (_run_loop) 仍進入待機，不評估任何標的。"""
    pipe = IntradayScanPipeline(MagicMock())
    pipe.is_running = True
    pipe.evaluate_watchlist_symbol = AsyncMock()  # type: ignore[method-assign]

    async def fake_sleep(_s: float) -> None:
        pipe.is_running = False

    with patch("market_time.datetime", _frozen_dt(_et_dt(2026, 11, 11, 16, 3))), patch(
        "services.llm_service.is_memory_safe", return_value=True
    ), patch.object(pl.asyncio, "sleep", new=fake_sleep):
        await pipe._run_loop()
    pipe.evaluate_watchlist_symbol.assert_not_called()
