"""單元測試：services/wall_break_service.py（牆突破管線服務）。

所有網路函式皆以 mock 取代；DB 使用測試用 shared in-memory SQLite（經 store 模組）。
時間不依賴真實時鐘：同時凍結服務自己的 `_now_et` 與
`market_analysis.price_volume_alert.datetime`（trim_to_confirmed_15m_bars 自行讀時鐘）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import market_time
from database.connection import execute_write_async
from database.wall_break import (
    get_latest_wall_anchor_before,
    get_wall_break_event,
    insert_wall_break_event,
    upsert_wall_anchor,
)
from services.wall_break_service import (
    WallBreakMonitor,
    parse_gex_profile,
)

TODAY = "2026-10-29"
PREV = "2026-10-28"


@pytest.fixture(autouse=True)
async def _clean_tables(db_conn: Any) -> Any:
    await execute_write_async("DELETE FROM wall_anchor")
    await execute_write_async("DELETE FROM wall_break_events")
    yield
    await execute_write_async("DELETE FROM wall_anchor")
    await execute_write_async("DELETE FROM wall_break_events")


def _et(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=market_time.ny_tz)


def _slots(day: str, last: str) -> list[pd.Timestamp]:
    out: list[pd.Timestamp] = []
    t = pd.Timestamp(f"{day} 09:30")
    end = pd.Timestamp(f"{day} {last}")
    while t <= end:
        out.append(t)
        t += timedelta(minutes=15)
    return out


def make_15m(
    last_bar: str = "11:15",
    overrides: Optional[dict[str, dict[str, float]]] = None,
) -> pd.DataFrame:
    """前三個交易日整天 + 今日至 last_bar；tz-naive ET 索引。基準 K 棒：量 1000、價 99、振幅 1。"""
    rows: list[dict[str, float]] = []
    idx: list[pd.Timestamp] = []
    for day in ("2026-10-26", "2026-10-27", PREV):
        for ts in _slots(day, "15:45"):
            idx.append(ts)
            rows.append(
                {
                    "Open": 99.0,
                    "High": 99.5,
                    "Low": 98.5,
                    "Close": 99.0,
                    "Volume": 1000.0,
                }
            )
    for ts in _slots(TODAY, last_bar):
        idx.append(ts)
        rows.append(
            {"Open": 99.0, "High": 99.5, "Low": 98.5, "Close": 99.0, "Volume": 1000.0}
        )
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    for key, vals in (overrides or {}).items():
        for col, v in vals.items():
            df.loc[pd.Timestamp(f"{TODAY} {key}"), col] = v
    return df


BREAK_BAR = {"Open": 99.9, "High": 101.2, "Low": 99.8, "Close": 101.0, "Volume": 5000.0}
WICK_BAR = {"Open": 99.9, "High": 101.2, "Low": 99.0, "Close": 99.5, "Volume": 5000.0}
DOWN_BAR = {"Open": 90.2, "High": 90.3, "Low": 88.5, "Close": 89.0, "Volume": 5000.0}


def make_daily() -> pd.DataFrame:
    idx = pd.date_range(end=PREV, periods=30, freq="D")
    return pd.DataFrame({"Volume": [1000.0] * 30}, index=idx)


def make_eval(
    gex: Optional[dict[str, Any]] = None,
    price: float = 101.0,
    skew: Optional[float] = 40.0,
) -> Any:
    if gex is None:
        gex = {
            "call_wall": 105.0,
            "put_wall": 85.0,
            "net_gex": 1.0,
            "gex_profile": {"100": 5000.0, "105": 9000.0},
            "expiry": "2026-10-30",
        }
    return SimpleNamespace(
        metrics=SimpleNamespace(
            symbol="NVDA",
            current_price=price,
            skew_percentile=skew,
            skew_is_fallback=False,
        ),
        symbol_gex=gex,
    )


BTO_CALL = [{"type": "CALL", "action": "BTO", "strike": 105.0}]


def make_chain(call_oi: float = 111.0, put_oi: float = 222.0) -> Any:
    return SimpleNamespace(
        calls=pd.DataFrame({"strike": [100.0, 105.0], "openInterest": [call_oi, 5.0]}),
        puts=pd.DataFrame({"strike": [90.0, 85.0], "openInterest": [put_oi, 5.0]}),
        underlying=100.0,
    )


@contextmanager
def env(
    now: str = f"{TODAY} 11:30",
    intraday: Optional[pd.DataFrame] = None,
    quote: Any = None,
    uoa: Any = BTO_CALL,
    chain: Any = None,
    prev_session: str = PREV,
) -> Iterator[SimpleNamespace]:
    now_dt = _et(now)
    intraday_df = intraday if intraday is not None else make_15m()
    daily_df = make_daily()
    calls: dict[str, int] = {"15m": 0, "1d": 0}

    async def _hist(
        sym: str, period: str = "1y", interval: str = "1d", **kw: Any
    ) -> Any:
        if interval == "15m":
            calls["15m"] += 1
            return intraday_df.copy()
        calls["1d"] += 1
        return daily_df.copy()

    ns = SimpleNamespace(
        calls=calls,
        quote=AsyncMock(
            return_value=quote
            if quote is not None
            else {"h": 200.0, "l": 1.0, "c": 100.0}
        ),
        uoa=AsyncMock(return_value=list(uoa)),
        chain=AsyncMock(return_value=chain if chain is not None else make_chain()),
        expiries=AsyncMock(return_value=["2026-10-28", "2026-11-06", "2026-11-13"]),
    )
    with patch("services.wall_break_service._now_et", return_value=now_dt), patch(
        "market_analysis.price_volume_alert.datetime"
    ) as dt, patch(
        "market_time.get_last_completed_trading_date", return_value=prev_session
    ), patch("services.market_data_service.get_history_df", new=_hist), patch(
        "services.market_data_service.get_quote", new=ns.quote
    ), patch("services.market_data_service.get_option_chain", new=ns.chain), patch(
        "services.market_data_service.get_all_option_expiries", new=ns.expiries
    ), patch("services.wall_break_service.SentimentEngine.detect_uoa", new=ns.uoa):
        dt.now.return_value = now_dt
        yield ns


async def _anchor(call: Optional[float] = 100.0, put: Optional[float] = 90.0) -> None:
    await upsert_wall_anchor("NVDA", PREV, call, put, "2026-10-30")


def _event(**over: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "symbol": "NVDA",
        "trade_date": PREV,
        "direction": "BREAKOUT",
        "trigger_kind": "CLOSE_BEYOND",
        "bar_time": f"{PREV} 11:00",
        "anchor_trade_date": "2026-10-27",
        "old_wall": 100.0,
        "close": 101.0,
        "verdict": "CANDIDATE",
        "label": "TRUE_BREAKOUT",
        "gates_json": "[]",
        "old_wall_oi": 500.0,
        "oi_expiry": "2026-10-30",
        "gex_expiry": "2026-10-30",
        "old_wall_gex": 1000.0,
    }
    ev.update(over)
    return ev


# ------------------------------------------------------------------ parse_gex_profile


def test_parse_gex_profile_variants() -> None:
    assert parse_gex_profile({"100": 5, 105.5: "7"}) == {100.0: 5.0, 105.5: 7.0}
    assert parse_gex_profile('{"100": 5}') == {100.0: 5.0}
    assert parse_gex_profile({"x": 1, "100": "bad", "101": 2}) == {101.0: 2.0}
    assert parse_gex_profile("not json") == {}
    assert parse_gex_profile(None) == {}
    assert parse_gex_profile([1, 2]) == {}


# ------------------------------------------------------------------ 錨點


async def test_anchor_upsert_then_next_day_read() -> None:
    with env(now=f"{PREV} 15:50", prev_session="2026-10-27") as e:
        await WallBreakMonitor().process_symbol("nvda", make_eval())
    row = get_latest_wall_anchor_before("NVDA", TODAY)
    assert row is not None
    assert row["trade_date"] == PREV
    assert row["call_wall"] == 105.0
    assert row["put_wall"] == 85.0
    assert e.calls["15m"] == 0


async def test_no_anchor_means_no_fetch() -> None:
    with env() as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.intraday == []
    e.quote.assert_not_called()
    assert e.calls["15m"] == 0


async def test_anchor_older_than_previous_session_skips_detection() -> None:
    await upsert_wall_anchor("NVDA", "2026-10-26", 100.0, 90.0, None)
    with env(intraday=make_15m(overrides={"11:15": BREAK_BAR})) as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.intraday == []
    e.quote.assert_not_called()
    assert e.calls["15m"] == 0


async def test_boxx_skipped() -> None:
    with env() as e:
        out = await WallBreakMonitor().process_symbol("BOXX", make_eval())
    assert out.intraday == [] and out.t1 == []
    assert get_latest_wall_anchor_before("BOXX", "2026-12-31") is None
    assert e.calls["15m"] == 0


async def test_stale_gex_skips_anchor_and_t1() -> None:
    await insert_wall_break_event(_event())
    stale = {
        "call_wall": 105.0,
        "put_wall": 85.0,
        "gex_profile": {},
        "_is_stale_cache": True,
    }
    with env(now=f"{TODAY} 11:30"):
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval(gex=stale))
    assert out.t1 == []
    assert get_latest_wall_anchor_before("NVDA", "2026-12-31") is None
    row = get_wall_break_event("NVDA", PREV, "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None and row["t1_verdict"] is None


async def test_edge_fallback_zero_walls_treated_as_not_fresh() -> None:
    zero = {"call_wall": 0.0, "put_wall": 0.0, "gex_profile": {}}
    with env():
        await WallBreakMonitor().process_symbol("NVDA", make_eval(gex=zero))
    assert get_latest_wall_anchor_before("NVDA", "2026-12-31") is None


# ------------------------------------------------------------------ 偵測


async def test_precheck_skips_bar_fetch() -> None:
    await _anchor()
    with env(quote={"h": 99.0, "l": 95.0, "c": 97.0}) as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.intraday == []
    e.quote.assert_awaited()
    assert e.calls["15m"] == 0


async def test_close_beyond_breakout_inserted_once() -> None:
    await _anchor()
    df = make_15m(overrides={"11:15": BREAK_BAR})
    mon = WallBreakMonitor()
    with env(intraday=df) as e:
        mon.begin_cycle()
        out = await mon.process_symbol("NVDA", make_eval())
        assert len(out.intraday) == 1
        res = out.intraday[0]
        assert res.verdict == "CANDIDATE"
        assert res.trigger.trigger == "CLOSE_BEYOND"
        row = get_wall_break_event("NVDA", TODAY, "BREAKOUT", "CLOSE_BEYOND")
        assert row is not None
        assert row["bar_time"] == f"{TODAY} 11:15"
        assert row["old_wall_oi"] == 111.0  # calls
        assert row["oi_expiry"] == "2026-10-30"
        assert row["old_wall_gex"] == 5000.0
        assert row["anchor_trade_date"] == PREV
        # 第二輪：不重複寫入、不重複回報
        mon.begin_cycle()
        out2 = await mon.process_symbol("NVDA", make_eval())
        assert out2.intraday == []
        assert e.uoa.await_count == 1


async def test_oi_expiry_falls_back_to_first_future_expiry() -> None:
    await _anchor()
    gex = {
        "call_wall": 105.0,
        "put_wall": 85.0,
        "gex_profile": {"100": 5000.0},
    }
    with env(intraday=make_15m(overrides={"11:15": BREAK_BAR})) as e:
        await WallBreakMonitor().process_symbol("NVDA", make_eval(gex=gex))
    row = get_wall_break_event("NVDA", TODAY, "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None
    assert row["oi_expiry"] == "2026-11-06"  # 跳過已過期的 10-28
    e.expiries.assert_awaited()


async def test_breakdown_uses_puts_for_oi() -> None:
    await _anchor()
    df = make_15m(overrides={"11:15": DOWN_BAR})
    with env(intraday=df):
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert [r.trigger.direction for r in out.intraday] == ["BREAKDOWN"]
    row = get_wall_break_event("NVDA", TODAY, "BREAKDOWN", "CLOSE_BEYOND")
    assert row is not None and row["old_wall_oi"] == 222.0


async def test_wick_reject_stored_as_fake() -> None:
    await _anchor()
    df = make_15m(overrides={"11:15": WICK_BAR})
    with env(intraday=df):
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert len(out.intraday) == 1
    assert out.intraday[0].trigger.trigger == "WICK_REJECT"
    assert out.intraday[0].verdict == "FAKE"
    row = get_wall_break_event("NVDA", TODAY, "BREAKOUT", "WICK_REJECT")
    assert row is not None and row["verdict"] == "FAKE"
    assert row["old_wall_oi"] is None  # 僅 CLOSE_BEYOND 才取 OI


async def test_uoa_fetched_once_for_two_triggers() -> None:
    await _anchor()
    df = make_15m(overrides={"11:00": WICK_BAR, "11:15": BREAK_BAR})
    with env(intraday=df) as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert {r.trigger.trigger for r in out.intraday} == {"WICK_REJECT", "CLOSE_BEYOND"}
    assert e.uoa.await_count == 1


async def test_unknown_event_is_still_recorded_and_cached_per_cycle() -> None:
    await _anchor()
    df = make_15m(overrides={"11:15": BREAK_BAR})
    mon = WallBreakMonitor()
    with env(intraday=df, uoa=[]) as e:
        mon.begin_cycle()
        first = await mon.process_symbol("NVDA", make_eval())
        second = await mon.process_symbol("nvda", make_eval())
    assert second is first
    assert len(first.intraday) == 1
    assert first.intraday[0].verdict == "UNDETERMINED"
    row = get_wall_break_event("NVDA", TODAY, "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None and row["verdict"] == "UNDETERMINED"
    assert e.calls["15m"] == 1


async def test_malformed_gex_profile_does_not_crash() -> None:
    await _anchor()
    gex = {"call_wall": 105.0, "put_wall": 85.0, "gex_profile": "}{bad"}
    with env(intraday=make_15m(overrides={"11:15": BREAK_BAR})):
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval(gex=gex))
    assert len(out.intraday) == 1
    row = get_wall_break_event("NVDA", TODAY, "BREAKOUT", "CLOSE_BEYOND")
    assert row is not None and row["old_wall_gex"] is None


async def test_evaluation_none_not_cached_then_valid_user_computes() -> None:
    await _anchor()
    df = make_15m(overrides={"11:15": BREAK_BAR})
    mon = WallBreakMonitor()
    with env(intraday=df) as e:
        mon.begin_cycle()
        empty = await mon.process_symbol("NVDA", None)
        assert empty.intraday == []
        assert e.calls["15m"] == 0
        out = await mon.process_symbol("NVDA", make_eval())
    assert len(out.intraday) == 1
    assert e.calls["15m"] == 1


async def test_no_symbol_gex_returns_immediately() -> None:
    ev = make_eval()
    ev.symbol_gex = None
    with env() as e:
        out = await WallBreakMonitor().process_symbol("NVDA", ev)
    assert out.intraday == [] and out.t1 == []
    e.quote.assert_not_called()


# ------------------------------------------------------------------ T+1


async def test_t1_settles_next_day_with_oi() -> None:
    await insert_wall_break_event(_event())
    with env(chain=make_chain(call_oi=400.0)) as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert len(out.t1) == 1
    row, ev, res = out.t1[0]
    assert res.verdict == "CONFIRMED"
    assert ev.old_wall == 100.0
    saved = get_wall_break_event("NVDA", PREV, "BREAKOUT", "CLOSE_BEYOND")
    assert saved is not None
    assert saved["t1_verdict"] == "CONFIRMED"
    assert saved["t1_trade_date"] == TODAY
    e.chain.assert_awaited()


async def test_t1_expired_expiry_means_oi_unknown() -> None:
    await insert_wall_break_event(_event(oi_expiry=PREV))
    with env() as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert len(out.t1) == 1
    assert out.t1[0][2].verdict == "UNDETERMINED"
    e.chain.assert_not_called()


async def test_t1_before_ten_stays_pending_then_settles_later() -> None:
    await insert_wall_break_event(_event())
    mon = WallBreakMonitor()
    with env(now=f"{TODAY} 09:40", chain=make_chain(call_oi=400.0)):
        mon.begin_cycle()
        out = await mon.process_symbol("NVDA", make_eval())
    assert out.t1 == []
    pending = get_wall_break_event("NVDA", PREV, "BREAKOUT", "CLOSE_BEYOND")
    assert pending is not None and pending["t1_verdict"] is None
    with env(now=f"{TODAY} 10:10", chain=make_chain(call_oi=400.0)):
        mon.begin_cycle()
        out2 = await mon.process_symbol("NVDA", make_eval())
    assert len(out2.t1) == 1


async def test_t1_event_older_than_previous_session_expires() -> None:
    await insert_wall_break_event(_event(trade_date="2026-10-27"))
    with env(now=f"{TODAY} 10:10") as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.t1 == []
    saved = get_wall_break_event("NVDA", "2026-10-27", "BREAKOUT", "CLOSE_BEYOND")
    assert saved is not None
    assert saved["t1_verdict"] == "EXPIRED"
    assert saved["t1_trade_date"] == TODAY
    e.chain.assert_not_called()


async def test_t1_settles_across_holiday() -> None:
    await insert_wall_break_event(
        _event(trade_date="2026-11-25", oi_expiry="2026-12-04")
    )
    with env(now="2026-11-27 10:10", chain=make_chain(call_oi=400.0)) as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert len(out.t1) == 1
    assert out.t1[0][2].verdict in {"CONFIRMED", "FAKE", "UNDETERMINED"}
    saved = get_wall_break_event("NVDA", "2026-11-25", "BREAKOUT", "CLOSE_BEYOND")
    assert saved is not None
    assert saved["t1_trade_date"] == "2026-11-27"
    e.chain.assert_awaited()


async def test_t1_non_session_day_leaves_pending() -> None:
    await insert_wall_break_event(_event(trade_date="2026-10-30"))
    with env(now="2026-10-31 10:10") as e:
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.t1 == []
    saved = get_wall_break_event("NVDA", "2026-10-30", "BREAKOUT", "CLOSE_BEYOND")
    assert saved is not None and saved["t1_verdict"] is None
    e.chain.assert_not_called()


async def test_t1_calendar_error_leaves_pending() -> None:
    await insert_wall_break_event(_event(trade_date="2026-10-27"))
    with env(now=f"{TODAY} 10:10") as e, patch(
        "market_time.count_nyse_sessions_after", side_effect=RuntimeError("boom")
    ):
        out = await WallBreakMonitor().process_symbol("NVDA", make_eval())
    assert out.t1 == []
    saved = get_wall_break_event("NVDA", "2026-10-27", "BREAKOUT", "CLOSE_BEYOND")
    assert saved is not None and saved["t1_verdict"] is None
    e.chain.assert_not_called()


# ------------------------------------------------------------------ 多使用者


async def test_symbol_evaluated_once_for_n_users() -> None:
    from market_analysis.intraday_pipeline import IntradayScanPipeline

    await _anchor()
    df = make_15m(overrides={"11:15": BREAK_BAR})
    pipe = IntradayScanPipeline(MagicMock())
    pipe._wall_break_monitor.begin_cycle()
    sent = AsyncMock(return_value=True)
    ev = make_eval()
    with env(intraday=df) as e, patch(
        "services.notification_dispatcher.notify", new=sent
    ), patch("config.WALL_BREAK_ALERT_ENABLED", True):
        for uid in (1, 2, 3):
            await pipe._dispatch_wall_break_alerts(uid, "NVDA", ev)
    assert e.calls["15m"] == 1
    assert e.uoa.await_count <= 1
    assert sent.await_count == 3
    assert {c.args[1] for c in sent.await_args_list} == {1, 2, 3}
