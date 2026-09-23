"""收盤後 EM 快照 (D-03 週預期波幅校準) 的資料層、排程與讀取端點。"""

import os
import tempfile
from datetime import datetime
from typing import Any, Generator
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import database
import scheduler
from local_api import app

client = TestClient(app)
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    monkeypatch.setattr(scheduler, "_last_em_snapshot_date", None)
    yield
    try:
        os.remove(path)
    except OSError:
        pass


def _row(symbol: str, expiry: str, dte: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "expiry": expiry,
        "dte": dte,
        "spot": 100.0,
        "strike": 100.0,
        "call_mid": 2.0,
        "put_mid": 1.8,
    }


# ---------------------------------------------------------------------------
# 資料層
# ---------------------------------------------------------------------------


def test_em_snapshot_roundtrip_and_first_write_wins() -> None:
    written = database.save_em_snapshot(
        "2026-09-22", [_row("aapl", "2026-09-25", 3), _row("AAPL", "2026-10-02", 10)]
    )
    assert written == 2
    assert database.has_em_snapshot("2026-09-22")
    assert not database.has_em_snapshot("2026-09-23")

    # 同一 (symbol, trade_date, expiry) 重寫不覆蓋
    dup = _row("AAPL", "2026-09-25", 3)
    dup["call_mid"] = 99.0
    assert database.save_em_snapshot("2026-09-22", [dup]) == 0

    rows = database.get_em_history("AAPL")
    assert [(r["expiry"], r["dte"], r["call_mid"]) for r in rows] == [
        ("2026-09-25", 3, 2.0),
        ("2026-10-02", 10, 2.0),
    ]
    assert database.list_history_symbols() == ["AAPL"]


def test_em_history_range_and_prune() -> None:
    database.save_em_snapshot("2026-09-21", [_row("MSFT", "2026-09-25", 4)])
    database.save_em_snapshot("2026-09-22", [_row("MSFT", "2026-09-25", 3)])
    assert [
        r["trade_date"] for r in database.get_em_history("MSFT", since="2026-09-22")
    ] == ["2026-09-22"]
    assert [
        r["trade_date"] for r in database.get_em_history("MSFT", until="2026-09-22")
    ] == ["2026-09-21"]

    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE em_snapshot_history SET captured_at = '2000-01-01 00:00:00' "
            "WHERE trade_date = '2026-09-21'"
        )
        conn.commit()
    finally:
        conn.close()
    assert database.prune_em_history(retention_days=30) == 1
    assert [r["trade_date"] for r in database.get_em_history("MSFT")] == ["2026-09-22"]


# ---------------------------------------------------------------------------
# 排程
# ---------------------------------------------------------------------------


def test_compute_atm_straddle_uses_common_strike_nearest_spot() -> None:
    calls = [
        {"strike": 95.0, "bid": 6.0, "ask": 6.4},
        {"strike": 100.0, "bid": 2.0, "ask": 2.2},
        {"strike": 105.0, "bid": 0.5, "ask": 0.7},
    ]
    puts = [
        {"strike": 100.0, "bid": 0.0, "ask": 0.0, "lastPrice": 1.9},
        {"strike": 105.0, "bid": 5.0, "ask": 5.4},
    ]
    # 100 與 105 都有 Call/Put 報價 (100 的 Put 以 lastPrice 代替)，取最接近現價的 100
    assert scheduler.compute_atm_straddle(calls, puts, 101.0) == (100.0, 2.1, 1.9)
    assert scheduler.compute_atm_straddle(calls, [], 101.0) is None
    assert scheduler.compute_atm_straddle(calls, puts, 0.0) is None


def test_em_snapshot_window() -> None:
    assert scheduler._is_em_snapshot_window(datetime(2026, 9, 22, 16, 30, tzinfo=NY))
    assert scheduler._is_em_snapshot_window(datetime(2026, 9, 22, 20, 0, tzinfo=NY))
    # 收盤前、夜間、週末都不在時間窗內
    assert not scheduler._is_em_snapshot_window(datetime(2026, 9, 22, 16, 5, tzinfo=NY))
    assert not scheduler._is_em_snapshot_window(datetime(2026, 9, 22, 23, 0, tzinfo=NY))
    assert not scheduler._is_em_snapshot_window(datetime(2026, 9, 26, 17, 0, tzinfo=NY))


@pytest.mark.asyncio
async def test_record_em_snapshot_once_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    database.upsert_tracked_symbols(["AAPL"], [])
    calls: list[str] = []

    async def fake_close(symbol: str) -> float:
        return 101.0

    async def fake_expiries(symbol: str) -> list[str]:
        # 當天到期 (DTE=0) 與超過 14 天的到期日都不記錄
        return ["2026-09-22", "2026-09-25", "2026-10-02", "2026-10-30"]

    async def fake_chain(symbol: str, expiry: str) -> dict[str, Any]:
        calls.append(expiry)
        return {
            "calls": [{"strike": 100.0, "bid": 2.0, "ask": 2.2}],
            "puts": [{"strike": 100.0, "bid": 1.8, "ask": 2.0}],
        }

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "fetch_last_close", fake_close)
    monkeypatch.setattr(scheduler, "fetch_option_expiries", fake_expiries)
    monkeypatch.setattr(scheduler, "fetch_option_chain_dict", fake_chain)
    monkeypatch.setattr(scheduler.asyncio, "sleep", no_sleep)

    now = datetime(2026, 9, 22, 17, 0, tzinfo=NY)
    assert await scheduler.record_em_snapshot_once(now) == 2
    assert calls == ["2026-09-25", "2026-10-02"]
    assert [(r["dte"], r["strike"]) for r in database.get_em_history("AAPL")] == [
        (3, 100.0),
        (10, 100.0),
    ]

    # 同一天再次觸發 (下一輪) 不重複抓取；服務重啟後以 DB 為準
    assert await scheduler.record_em_snapshot_once(now) == 0
    monkeypatch.setattr(scheduler, "_last_em_snapshot_date", None)
    assert await scheduler.record_em_snapshot_once(now) == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_record_em_snapshot_retries_when_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.upsert_tracked_symbols(["AAPL"], [])

    async def failing_close(symbol: str) -> float:
        raise RuntimeError("blocked")

    monkeypatch.setattr(scheduler, "fetch_last_close", failing_close)
    now = datetime(2026, 9, 22, 17, 0, tzinfo=NY)
    assert await scheduler.record_em_snapshot_once(now) == 0
    # 沒寫入任何資料 → 不設當日旗標，下一輪會重試
    assert scheduler._last_em_snapshot_date is None


# ---------------------------------------------------------------------------
# 讀取端點
# ---------------------------------------------------------------------------


def test_history_symbols_endpoint() -> None:
    database.save_em_snapshot("2026-09-22", [_row("TSLA", "2026-09-25", 3)])
    resp = client.get("/api/v1/cache/history/symbols")
    assert resp.json() == {"status": "success", "data": ["TSLA"]}


def test_em_history_endpoint_paginates_by_trade_date() -> None:
    database.save_em_snapshot(
        "2026-09-21", [_row("NVDA", "2026-09-25", 4), _row("NVDA", "2026-10-02", 11)]
    )
    database.save_em_snapshot(
        "2026-09-22", [_row("NVDA", "2026-09-25", 3), _row("NVDA", "2026-10-02", 10)]
    )
    # limit=3 會切在 2026-09-22 中間：該日整日延到下一頁
    first = client.get("/api/v1/cache/em/history/NVDA", params={"limit": 3}).json()
    assert [r["trade_date"] for r in first["data"]] == ["2026-09-21", "2026-09-21"]
    assert first["next_since"] == "2026-09-22"
    second = client.get(
        "/api/v1/cache/em/history/NVDA", params={"since": first["next_since"]}
    ).json()
    assert [r["trade_date"] for r in second["data"]] == ["2026-09-22", "2026-09-22"]
    assert second["next_since"] is None
