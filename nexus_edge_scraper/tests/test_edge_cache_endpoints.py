from typing import Generator
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import database
from local_api import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    yield
    try:
        os.remove(path)
    except OSError:
        pass


def test_watchlist_sync_upserts_tracked_symbols() -> None:
    response = client.post(
        "/api/v1/watchlist/sync", json={"symbols": ["aapl", "TSLA", "aapl"]}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["synced"] == 3
    assert database.get_tracked_symbols() == ["AAPL", "TSLA"]


def test_watchlist_sync_rejects_missing_body() -> None:
    response = client.post("/api/v1/watchlist/sync", json={})
    assert response.status_code == 422


def test_get_cached_gex_miss_returns_error_status() -> None:
    response = client.get("/api/v1/cache/gex/AAPL")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"


def test_get_cached_gex_hit_returns_snapshot_with_age() -> None:
    database.save_gex_snapshot(
        "AAPL",
        spot=230.5,
        net_gex=1234.5,
        call_wall=240.0,
        put_wall=220.0,
        gex_profile={"220.0": 500.0},
    )
    response = client.get("/api/v1/cache/gex/aapl")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["spot"] == 230.5
    assert data["data"]["call_wall"] == 240.0
    assert data["data"]["gex_profile"] == {"220.0": 500.0}
    assert isinstance(data["age_seconds"], float)
    assert data["age_seconds"] >= 0.0


def test_get_cached_option_chain_miss_returns_error_status() -> None:
    response = client.get("/api/v1/cache/options/AAPL/chain")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"


def test_get_cached_option_chain_hit_returns_snapshot() -> None:
    calls = [{"strike": 230.0, "openInterest": 100}]
    puts = [{"strike": 220.0, "openInterest": 50}]
    database.save_option_chain_snapshot("AAPL", "2026-09-18", calls, puts)

    response = client.get("/api/v1/cache/options/aapl/chain")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["expiry"] == "2026-09-18"
    assert data["data"]["calls"] == calls
    assert data["data"]["puts"] == puts

    response_by_expiry = client.get(
        "/api/v1/cache/options/aapl/chain", params={"expiry": "2026-09-18"}
    )
    assert response_by_expiry.json()["status"] == "success"

    response_wrong_expiry = client.get(
        "/api/v1/cache/options/aapl/chain", params={"expiry": "1999-01-01"}
    )
    assert response_wrong_expiry.json()["status"] == "error"


def test_scrape_symbol_gex_endpoint_still_delegates_to_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即時端點應仍可正常運作，且回傳形狀與過去一致 (status/data envelope)。"""
    import local_api

    async def _fake_core(symbol: str, browser: object) -> dict:
        return {
            "spot": 100.0,
            "net_gex": 1.0,
            "call_wall": 110.0,
            "put_wall": 90.0,
            "gex_profile": {},
        }

    monkeypatch.setattr(local_api, "scrape_symbol_gex_core", _fake_core)

    class _FakeBrowser:
        async def close(self) -> None:
            pass

    class _FakeChromium:
        async def launch(self, **kwargs: object) -> "_FakeBrowser":
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeAsyncPlaywrightCtx:
        async def __aenter__(self) -> "_FakePlaywright":
            return _FakePlaywright()

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(
        local_api, "async_playwright", lambda: _FakeAsyncPlaywrightCtx()
    )

    response = client.get("/api/v1/scrape/options/AAPL/gex")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["spot"] == 100.0
