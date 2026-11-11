import asyncio
import os
import tempfile
from datetime import datetime
from typing import Any, Generator, cast
from zoneinfo import ZoneInfo

import pytest

import database
import scheduler
from local_api import cache_and_sync

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    monkeypatch.setattr(scheduler, "_last_oi_probe_ts", None)

    async def _no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", _no_sleep)
    yield
    try:
        os.remove(path)
    except OSError:
        pass


def test_oi_readiness_summarize_chain_handles_nan() -> None:
    calls: list[dict[str, Any]] = [
        {"openInterest": 10, "bid": 1.0, "volume": 5},
        {"openInterest": float("nan"), "bid": float("nan"), "volume": float("nan")},
        {"openInterest": None, "bid": "x", "volume": None},
    ]
    puts: list[dict[str, Any]] = [
        {"openInterest": 0, "bid": 0, "volume": 3.9},
        {"openInterest": "7"},
    ]
    out = scheduler.summarize_chain_oi(calls, puts)
    assert out == {"rows": 5, "oi_rows": 2, "bid_rows": 1, "volume_sum": 8}


def test_oi_readiness_save_get_prune_roundtrip() -> None:
    database.save_oi_readiness("nvda", "2026-11-13", 10, 9, 8, 100, "poll")
    database.save_oi_readiness(
        "NVDA", "2026-11-13", 10, 0, 0, 0, "probe", captured_at="2020-01-01 00:00:00"
    )
    recent = database.get_oi_readiness(7)
    assert len(recent) == 1
    assert recent[0]["symbol"] == "NVDA" and recent[0]["source"] == "poll"
    assert database.prune_oi_readiness(14) == 1
    assert database.prune_oi_readiness(14) == 0


def _chain(oi: int = 1) -> dict[str, Any]:
    return {
        "expiry": "2026-11-13",
        "calls": [{"openInterest": oi, "bid": 1.0}],
        "puts": [{"openInterest": oi, "bid": 1.0}],
    }


@pytest.mark.asyncio
async def test_oi_readiness_probe_skips_market_hours_and_weekend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.upsert_tracked_symbols(["AAPL"])
    mon_open = datetime(2026, 11, 9, 10, 0, tzinfo=NY)
    sat = datetime(2026, 11, 14, 3, 0, tzinfo=NY)
    assert await scheduler.record_oi_probe_once(sat) == 0
    # 盤中判斷用真實時鐘的 _is_us_market_hours，這裡強制為 True
    monkeypatch.setattr(scheduler, "_is_us_market_hours", lambda: True)
    assert await scheduler.record_oi_probe_once(mon_open) == 0
    assert database.get_oi_readiness(14) == []


@pytest.mark.asyncio
async def test_oi_readiness_probe_throttle_priority_cap_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.upsert_tracked_symbols(
        ["AAA", "BBB", "CCC", "DDD", "EEE", "ZZZ"], priority_symbols=["ZZZ"]
    )
    monkeypatch.setattr(scheduler, "_is_us_market_hours", lambda: False)
    seen: list[str] = []

    async def _fake_chain(symbol: str) -> dict[str, Any]:
        seen.append(symbol)
        if symbol == "AAA":
            raise RuntimeError("boom")
        return _chain()

    monkeypatch.setattr(scheduler, "fetch_nearest_option_chain", _fake_chain)
    night = datetime(2026, 11, 10, 3, 0, tzinfo=NY)
    written = await scheduler.record_oi_probe_once(night)
    assert sorted(seen) == ["AAA", "BBB", "CCC", "ZZZ"]
    assert "ZZZ" in seen and "EEE" not in seen and "DDD" not in seen
    assert written == 3
    assert {r["source"] for r in database.get_oi_readiness(14)} == {"probe"}
    # 15 分鐘內再次呼叫被節流
    assert await scheduler.record_oi_probe_once(night) == 0
    assert len(seen) == 4


class _FakeBrowser:
    pass


@pytest.mark.asyncio
async def test_oi_readiness_poll_records_and_survives_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_gex(symbol: str, browser: object) -> dict[str, Any]:
        return {"spot": 100.0, "net_gex": 1.0, "call_wall": 1.0, "put_wall": 1.0}

    async def _fake_chain(symbol: str) -> dict[str, Any]:
        return _chain()

    monkeypatch.setattr(scheduler, "scrape_symbol_gex_core", _fake_gex)
    monkeypatch.setattr(scheduler, "fetch_nearest_option_chain", _fake_chain)
    browser = cast(Any, _FakeBrowser())

    await scheduler._poll_symbol("AAPL", browser, asyncio.Semaphore(1))
    rows = database.get_oi_readiness(1)
    assert len(rows) == 1 and rows[0]["source"] == "poll" and rows[0]["oi_rows"] == 2

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "save_oi_readiness", _boom)
    await scheduler._poll_symbol("TSLA", browser, asyncio.Semaphore(1))
    assert database.get_option_chain_snapshot("TSLA") is not None
    assert len(database.get_oi_readiness(1)) == 1


@pytest.mark.asyncio
async def test_oi_readiness_endpoint_summary_and_days_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 美東 11/10：03:00 未就緒、09:45 就緒、16:15 未就緒（EST，UTC-5）
    for utc, oi in (
        ("2026-11-10 08:00:00", 0),
        ("2026-11-10 14:45:00", 90),
        ("2026-11-10 21:15:00", 0),
    ):
        database.save_oi_readiness("NVDA", "2026-11-13", 100, oi, 50, 10, "poll", utc)
    # 以 DB 時間為準的 days 查詢：用固定紀錄繞過 now()
    recs = [
        {
            "captured_at": u,
            "symbol": "NVDA",
            "expiry": "x",
            "rows": 100,
            "oi_rows": o,
            "bid_rows": 1,
            "volume_sum": 1,
            "source": "poll",
        }
        for u, o in (
            ("2026-11-10 08:00:00", 0),
            ("2026-11-10 14:45:00", 90),
            ("2026-11-10 21:15:00", 0),
        )
    ]
    summary, rows = cache_and_sync.build_oi_readiness_report(recs)
    assert len(summary) == 1 and len(rows) == 3
    s = summary[0]
    assert s["date_et"] == "2026-11-10" and s["samples"] == 3
    assert s["first_ready_et"] == "09:45"
    assert s["first_unready_after_close_et"] == "16:15"
    assert s["min_share"] == 0.0 and s["max_share"] == 0.9
    assert rows[0]["captured_at_et"] == "2026-11-10T03:00:00"

    seen_days: list[int] = []
    real = database.get_oi_readiness

    def _spy(days: int = 7) -> list[dict[str, Any]]:
        seen_days.append(days)
        return real(days)

    monkeypatch.setattr(database, "get_oi_readiness", _spy)
    out = await cache_and_sync.get_oi_readiness(days=99)
    assert out["days"] == 14 and out["threshold"] == 0.10
    out = await cache_and_sync.get_oi_readiness(days=0)
    assert out["days"] == 1
    assert seen_days == [14, 1]
