"""D3：牆位寫入不得刷新 Max Pain 的 updated_at／reference_spot_price。

``get_market_cache(symbol)`` 取 ``updated_at`` 最新的一列；雷達慢速路徑過去經
``save_market_cache`` 寫牆位，會讓牆位追蹤列成為「最新列」並持續被刷新，Max Pain 的
6 小時絕對 TTL 與 2% 偏離基準因而失效。``save_wall_levels`` 只動牆位欄位。
"""

import sqlite3
import tempfile
from typing import Iterator

import pytest

import config
from database.core import run_migrations
from database.market_cache import (
    WALL_TRACKING_EXPIRY,
    get_market_cache,
    save_market_cache,
    save_wall_levels,
)


@pytest.fixture
def temp_db() -> Iterator[None]:
    tf = tempfile.NamedTemporaryFile(suffix=".db")
    orig_db = config.DB_NAME
    try:
        config.DB_NAME = tf.name
        run_migrations()
        yield
    finally:
        config.DB_NAME = orig_db
        tf.close()


def _backdate(symbol: str, expiry: str, updated_at: str) -> None:
    conn = sqlite3.connect(config.DB_NAME)
    try:
        conn.execute(
            "UPDATE market_cache SET updated_at = ? WHERE symbol = ? AND expiry = ?",
            (updated_at, symbol, expiry),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_wall_write_does_not_become_latest_row(temp_db: None) -> None:
    await save_market_cache(
        "AAA", 100.0, 95.0, 105.0, reference_spot_price=101.0, expiry="2026-10-23"
    )
    _backdate("AAA", "2026-10-23", "2026-10-22 01:00:00")

    assert await save_wall_levels("AAA", call_wall=150.0, put_wall=90.0)

    latest = get_market_cache("AAA")
    assert latest is not None
    assert latest["expiry"] == "2026-10-23"
    assert latest["updated_at"] == "2026-10-22 01:00:00"
    assert latest["reference_spot_price"] == 101.0

    walls = get_market_cache("AAA", WALL_TRACKING_EXPIRY)
    assert walls is not None
    assert walls["call_wall"] == 150.0
    assert walls["put_wall"] == 90.0
    assert walls["updated_at"].startswith("1970-")


@pytest.mark.asyncio
async def test_repeated_wall_writes_never_touch_updated_at(temp_db: None) -> None:
    await save_market_cache("BBB", 50.0, 48.0, 52.0, reference_spot_price=50.0)
    _backdate("BBB", WALL_TRACKING_EXPIRY, "2026-10-22 01:00:00")

    for cw in (60.0, 60.0, 66.0):
        await save_wall_levels("BBB", call_wall=cw)

    row = get_market_cache("BBB", WALL_TRACKING_EXPIRY)
    assert row is not None
    assert row["updated_at"] == "2026-10-22 01:00:00"
    assert row["reference_spot_price"] == 50.0
    assert row["max_pain"] == 50.0
    assert row["call_wall"] == 66.0
    assert row["previous_call_wall"] == 60.0


@pytest.mark.asyncio
async def test_put_wall_migration_and_none_preserves_existing(temp_db: None) -> None:
    await save_wall_levels("CCC", call_wall=120.0, put_wall=100.0)
    await save_wall_levels("CCC", call_wall=None, put_wall=95.0)
    await save_wall_levels("CCC", call_wall=0.0, put_wall=95.0)

    row = get_market_cache("CCC", WALL_TRACKING_EXPIRY)
    assert row is not None
    assert row["call_wall"] == 120.0
    assert row["previous_call_wall"] is None
    assert row["put_wall"] == 95.0
    assert row["previous_put_wall"] == 100.0


@pytest.mark.asyncio
async def test_no_valid_wall_is_a_noop(temp_db: None) -> None:
    assert await save_wall_levels("DDD", call_wall=None, put_wall=0.0) is False
    assert get_market_cache("DDD") is None
