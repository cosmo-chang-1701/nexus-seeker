from typing import Generator
import os
import tempfile

import pytest

import database


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """每個測試使用獨立的暫存 SQLite 檔案，避免互相污染或污染真實快取檔。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    yield
    try:
        os.remove(path)
    except OSError:
        pass


def test_tracked_symbols_upsert_and_get() -> None:
    database.upsert_tracked_symbols(["aapl", "TSLA", "aapl"])
    symbols = database.get_tracked_symbols()
    assert symbols == ["AAPL", "TSLA"]


def test_tracked_symbols_upsert_empty_is_noop() -> None:
    database.upsert_tracked_symbols([])
    assert database.get_tracked_symbols() == []


def test_prune_stale_symbols_removes_old_rows() -> None:
    database.upsert_tracked_symbols(["AAPL"])
    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE tracked_symbols SET last_synced_at = datetime('now', '-72 hours') WHERE symbol = 'AAPL'"
        )
        conn.commit()
    finally:
        conn.close()

    removed = database.prune_stale_symbols(older_than_hours=48)
    assert removed == 1
    assert database.get_tracked_symbols() == []


def test_gex_snapshot_roundtrip() -> None:
    assert database.get_gex_snapshot("AAPL") is None

    database.save_gex_snapshot(
        "aapl",
        spot=230.5,
        net_gex=1234.5,
        call_wall=240.0,
        put_wall=220.0,
        gex_profile={"220.0": 500.0, "240.0": -300.0},
    )
    row = database.get_gex_snapshot("AAPL")
    assert row is not None
    assert row["symbol"] == "AAPL"
    assert row["spot"] == 230.5
    assert row["call_wall"] == 240.0
    assert row["gex_profile"] == {"220.0": 500.0, "240.0": -300.0}
    assert row["updated_at"] is not None


def test_gex_snapshot_upsert_overwrites() -> None:
    database.save_gex_snapshot("AAPL", 100.0, 1.0, 110.0, 90.0, {})
    database.save_gex_snapshot("AAPL", 200.0, 2.0, 210.0, 190.0, {})
    row = database.get_gex_snapshot("AAPL")
    assert row is not None
    assert row["spot"] == 200.0
    assert row["put_wall"] == 190.0


def test_option_chain_snapshot_roundtrip() -> None:
    assert database.get_option_chain_snapshot("AAPL") is None

    calls = [{"strike": 230.0, "openInterest": 100}]
    puts = [{"strike": 220.0, "openInterest": 50}]
    database.save_option_chain_snapshot("aapl", "2026-09-18", calls, puts)

    row = database.get_option_chain_snapshot("AAPL")
    assert row is not None
    assert row["expiry"] == "2026-09-18"
    assert row["calls"] == calls
    assert row["puts"] == puts

    row_by_expiry = database.get_option_chain_snapshot("AAPL", "2026-09-18")
    assert row_by_expiry is not None
    assert row_by_expiry["expiry"] == "2026-09-18"

    assert database.get_option_chain_snapshot("AAPL", "1999-01-01") is None


def test_option_chain_snapshot_latest_when_multiple_expiries() -> None:
    # CURRENT_TIMESTAMP 為秒級精度，測試中手動錯開 updated_at 以確保排序決定性，
    # 避免同一秒內兩次寫入造成排序不穩定 (實際生產環境同一標的的兩筆快照
    # 通常相隔數小時甚至數天，不會有這個問題)。
    database.save_option_chain_snapshot("AAPL", "2026-09-18", [], [])
    conn = database._get_connection()
    try:
        conn.execute(
            "UPDATE option_chain_snapshot SET updated_at = datetime('now', '-1 hour') "
            "WHERE symbol = 'AAPL' AND expiry = '2026-09-18'"
        )
        conn.commit()
    finally:
        conn.close()
    database.save_option_chain_snapshot("AAPL", "2026-09-25", [{"strike": 1.0}], [])

    row = database.get_option_chain_snapshot("AAPL")
    assert row is not None
    assert row["expiry"] == "2026-09-25"
