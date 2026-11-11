"""無 user_settings 列的使用者仍須取得真實的組合 Greeks，且被排程列舉。"""

import dataclasses
import json
import sqlite3

import pytest

import database
from market_analysis.hedging import get_portfolio_exposure_status

UID = 4242


def _seed_assets(conn: sqlite3.Connection, uid: int = UID) -> None:
    rows = [
        (
            "HOLDING",
            "AAPL",
            {"weighted_delta": 120.0, "theta": 0.0, "gamma": 0.5, "vanna": 1.0},
        ),
        (
            "TRADE",
            "TSLA",
            {"weighted_delta": -30.0, "theta": 4.5, "gamma": -0.25, "vanna": -0.5},
        ),
        ("WATCH", "MSFT", {"weighted_delta": 999.0}),
    ]
    cur = conn.cursor()
    for ctx_type, symbol, meta in rows:
        cur.execute(
            "INSERT INTO assets (user_id, symbol, context_type, metadata) "
            "VALUES (?, ?, ?, ?)",
            (uid, symbol, ctx_type, json.dumps(meta)),
        )
    conn.commit()


def _assert_greeks(ctx: database.UserContext) -> None:
    assert ctx.total_weighted_delta == pytest.approx(90.0)
    assert ctx.total_theta == pytest.approx(4.5)
    assert ctx.total_gamma == pytest.approx(0.25)
    assert ctx.total_vanna == pytest.approx(0.5)


def test_no_settings_row_keeps_greeks_and_defaults(db_conn: sqlite3.Connection) -> None:
    _seed_assets(db_conn)
    ctx = database.get_full_user_context(UID)
    _assert_greeks(ctx)
    assert ctx.load_failed is False  # 無設定列是合法讀取，不是讀取失敗
    assert ctx.risk_limit == 15.0
    base = database.UserContext(UID, ctx.capital, 15.0, 0.0, 0.0, 0.0, 0.0)
    for f in dataclasses.fields(ctx):
        if f.name.startswith("total_"):
            continue
        assert getattr(ctx, f.name) == getattr(base, f.name), f.name


def test_with_settings_row_same_greeks(db_conn: sqlite3.Connection) -> None:
    _seed_assets(db_conn)
    db_conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (UID,))
    db_conn.commit()
    _assert_greeks(database.get_full_user_context(UID))


def test_no_settings_ctx_triggers_hedge_directive(db_conn: sqlite3.Connection) -> None:
    _seed_assets(db_conn)
    ctx = database.get_full_user_context(UID)
    status = get_portfolio_exposure_status(ctx, 500.0)
    assert status["directive"] != "HOLD"


def test_get_all_user_ids_includes_assets_only_user(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_assets(db_conn, uid=777)
    assert 777 in database.get_all_user_ids()


@pytest.mark.parametrize("with_settings", [False, True])
def test_greeks_ignore_other_users_and_other_context_types(
    db_conn: sqlite3.Connection, with_settings: bool
) -> None:
    """他人的 TRADE/HOLDING 與本人非 TRADE/HOLDING 的列不得混入加總（兩條路徑皆然）。"""
    _seed_assets(db_conn)  # 本人：HOLDING + TRADE + 一筆 WATCH（delta 999）
    other = {"weighted_delta": 5000.0, "theta": 50.0, "gamma": 7.0, "vanna": 9.0}
    cur = db_conn.cursor()
    for ctx_type in ("TRADE", "HOLDING"):
        cur.execute(
            "INSERT INTO assets (user_id, symbol, context_type, metadata) "
            "VALUES (?, ?, ?, ?)",
            (UID + 1, "NVDA", ctx_type, json.dumps(other)),
        )
    cur.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) "
        "VALUES (?, ?, ?, ?)",
        (UID, "AMD", "WATCH", json.dumps(other)),
    )
    if with_settings:
        cur.execute("INSERT INTO user_settings (user_id) VALUES (?)", (UID,))
    db_conn.commit()
    _assert_greeks(database.get_full_user_context(UID))
