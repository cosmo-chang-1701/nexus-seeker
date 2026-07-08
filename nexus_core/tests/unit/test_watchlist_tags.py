import pytest
import sqlite3
import config
from database.watchlist_tags import set_watchlist_tags, get_watchlist_tags


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_nexus.db"
    monkeypatch.setattr(config, "DB_NAME", str(db_file))

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            UNIQUE(user_id, symbol, tag_name)
        );
    """)
    conn.commit()
    conn.close()

    yield

    if db_file.exists():
        db_file.unlink()


def test_multi_tenant_isolation():
    user_a = "12345"
    user_b = "67890"
    symbol = "TSLA"

    tags_a = ["CORE", "HIGH_IV"]
    tags_b = ["RISK", "TECH"]

    # User A sets tags
    assert set_watchlist_tags(user_a, symbol, tags_a) is True

    # User B sets tags on the same symbol
    assert set_watchlist_tags(user_b, symbol, tags_b) is True

    # Verify User A's tags
    fetched_a = get_watchlist_tags(user_a, symbol)
    assert fetched_a == sorted(tags_a)

    # Verify User B's tags
    fetched_b = get_watchlist_tags(user_b, symbol)
    assert fetched_b == sorted(tags_b)

    # Verify User A replacing tags doesn't affect User B
    tags_a_new = ["CORE_ONLY"]
    assert set_watchlist_tags(user_a, symbol, tags_a_new) is True

    fetched_a_new = get_watchlist_tags(user_a, symbol)
    assert fetched_a_new == sorted(tags_a_new)

    fetched_b_new = get_watchlist_tags(user_b, symbol)
    assert fetched_b_new == sorted(tags_b)


def test_set_empty_tags_clears():
    user_id = "111"
    symbol = "AAPL"

    set_watchlist_tags(user_id, symbol, ["TAG1"])
    assert len(get_watchlist_tags(user_id, symbol)) == 1

    set_watchlist_tags(user_id, symbol, [])
    assert len(get_watchlist_tags(user_id, symbol)) == 0
