"""
test_gex_sparse_chain.py

夜間 Yahoo OI 未就緒時，edge GEX 只剩單一遠端履約價：
- is_valid_gex_profile 須與 nexus_core 的有效性規則一致
- scrape_symbol_gex_core 對稀疏剖面回 FALLBACK_GEX
- 背景輪詢不得以稀疏／fallback 結果覆蓋快照或寫入歷史
"""

import asyncio
import os
import tempfile
from typing import Any, Generator, cast

import pytest

import database
import gex_scraper
import scheduler
from tests.test_gex_scraper import _make_browser, _make_html, _row


def test_helper_nvda_single_far_strike_invalid() -> None:
    assert gex_scraper.is_valid_gex_profile({"335.0": 1774.69}, 166.19) is False


def test_helper_three_near_strikes_valid() -> None:
    profile = {"95.0": 5.0, "100.0": -3.0, "105.0": 2.0, "400.0": 9.0}
    assert gex_scraper.is_valid_gex_profile(profile, 100.0) is True
    # 數值鍵（scrape 內部產物）亦可
    assert gex_scraper.is_valid_gex_profile({95.0: 1.0, 100.0: 1.0, 105.0: 1.0}, 100.0)
    # 近端僅 2 個、其餘在 ±25% 之外 → 無效
    assert not gex_scraper.is_valid_gex_profile(
        {"95.0": 5.0, "100.0": 3.0, "200.0": 2.0, "50.0": 1.0}, 100.0
    )


def test_helper_zero_spot_requires_three_nonzero() -> None:
    assert gex_scraper.is_valid_gex_profile({"1": 1.0, "50": 1.0, "900": 1.0}, 0.0)
    assert not gex_scraper.is_valid_gex_profile({"1": 1.0, "50": 1.0, "900": 0.0}, 0.0)
    # 非數值／非有限的鍵或值被忽略
    bad: dict[str, Any] = {"x": 1.0, "50": float("nan"), "60": 1.0, "70": 1.0}
    assert not gex_scraper.is_valid_gex_profile(bad, 0.0)
    assert not gex_scraper.is_valid_gex_profile({}, 100.0)


@pytest.mark.asyncio
async def test_scrape_returns_fallback_for_sparse_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeStealth:
        async def apply_stealth_async(self, context: Any) -> None:
            return None

    monkeypatch.setattr(gex_scraper, "Stealth", _FakeStealth)
    # 只有一筆遠端 OTM 合約有 OI，其餘 OI=0 被雜訊過濾丟棄
    html = _make_html(
        spot=166.19,
        call_rows=[_row("TESTC1", 335.0, 1000, 20), _row("TESTC2", 170.0, 0, 20)],
        put_rows=[_row("TESTP1", 160.0, 0, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result == gex_scraper.FALLBACK_GEX


@pytest.fixture
def _db(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    yield
    try:
        os.remove(path)
    except OSError:
        pass


async def _run_poll(monkeypatch: pytest.MonkeyPatch, gex: dict[str, Any]) -> None:
    async def _fake_gex_core(symbol: str, browser: object) -> dict[str, Any]:
        return gex

    async def _fake_nearest_chain(symbol: str) -> None:
        return None

    async def _no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "scrape_symbol_gex_core", _fake_gex_core)
    monkeypatch.setattr(scheduler, "fetch_nearest_option_chain", _fake_nearest_chain)
    monkeypatch.setattr(scheduler.asyncio, "sleep", _no_sleep)
    await scheduler._poll_symbol("NVDA", cast(Any, object()), asyncio.Semaphore(1))


def _history_count() -> int:
    conn = database._get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM gex_snapshot_history").fetchone()
        return int(row[0])
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_poll_symbol_skips_save_for_sparse_or_fallback(
    monkeypatch: pytest.MonkeyPatch, _db: None
) -> None:
    good = {
        "spot": 100.0,
        "net_gex": 5.0,
        "call_wall": 105.0,
        "put_wall": 95.0,
        "gex_profile": {"95.0": -1.0, "100.0": 2.0, "105.0": 3.0},
        "expiry": None,
    }
    await _run_poll(monkeypatch, good)
    assert _history_count() == 1

    sparse = {
        "spot": 166.19,
        "net_gex": 1774.69,
        "call_wall": 335.0,
        "put_wall": 0.0,
        "gex_profile": {"335.0": 1774.69},
        "expiry": None,
    }
    await _run_poll(monkeypatch, sparse)
    await _run_poll(monkeypatch, dict(gex_scraper.FALLBACK_GEX))

    row = database.get_gex_snapshot("NVDA")
    assert row is not None
    assert row["call_wall"] == 105.0
    assert _history_count() == 1


@pytest.mark.asyncio
async def test_poll_symbol_saves_valid_profile(
    monkeypatch: pytest.MonkeyPatch, _db: None
) -> None:
    valid = {
        "spot": 100.0,
        "net_gex": 4.0,
        "call_wall": 110.0,
        "put_wall": 90.0,
        "gex_profile": {"90.0": -1.0, "100.0": 2.0, "110.0": 3.0},
        "expiry": "2026-11-20",
    }
    await _run_poll(monkeypatch, valid)
    row = database.get_gex_snapshot("NVDA")
    assert row is not None
    assert row["call_wall"] == 110.0
    assert _history_count() == 1
