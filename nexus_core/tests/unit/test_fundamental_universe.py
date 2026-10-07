"""單元測試：標的池收集與過濾器 (fundamental_universe.py)。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.fundamental_universe import (
    _fetch_db_raw_symbols,
    filter_universe_symbols,
    get_fundamental_universe,
    is_etf_or_index,
    is_valid_equity_symbol,
)


def test_is_etf_or_index() -> None:
    """測試 ETF 與指數標的識別。"""
    # 典型指數標的
    assert is_etf_or_index("^SPX") is True
    assert is_etf_or_index("^VIX") is True
    # 典型寬基與板塊 ETF
    assert is_etf_or_index("SPY") is True
    assert is_etf_or_index("QQQ") is True
    assert is_etf_or_index("SMH") is True
    assert is_etf_or_index("XLK") is True
    assert is_etf_or_index("BOXX") is True
    assert is_etf_or_index("SOXL") is True

    # 典型個股標的與雙重股權 Class A/B 股票
    assert is_etf_or_index("AAPL") is False
    assert is_etf_or_index("MSFT") is False
    assert is_etf_or_index("NVDA") is False
    assert is_etf_or_index("TSLA") is False
    assert is_etf_or_index("SMCI") is False
    assert is_etf_or_index("BRK.B") is False
    assert is_etf_or_index("BRK-B") is False
    assert is_etf_or_index("BF.B") is False


def test_is_valid_equity_symbol() -> None:
    """測試個股代號格式驗證，確保 Class A/B 及含符號個股不被誤刪。"""
    assert is_valid_equity_symbol("AAPL") is True
    assert is_valid_equity_symbol("BRK.B") is True
    assert is_valid_equity_symbol("BRK-B") is True
    assert is_valid_equity_symbol("BF.B") is True
    assert is_valid_equity_symbol("") is False
    assert is_valid_equity_symbol("$$$") is False
    assert is_valid_equity_symbol("A" * 15) is False


def test_filter_universe_symbols_prioritizes_holdings() -> None:
    """測試持倉標的優先於自選標的，並正確剔除 ETF，且保留 BRK.B / BRK-B 等雙重股權個股。"""
    holdings = ["AAPL", "SPY", "TSLA", "BRK.B", "NVDA"]
    watchlist = ["NVDA", "MSFT", "QQQ", "AMD", "BRK-B", "GOOGL"]

    # 預期順序：
    # 1. 持倉個股: AAPL, TSLA, BRK.B, NVDA (SPY 被排除)
    # 2. 自選個股: MSFT, AMD, BRK-B, GOOGL (NVDA 已存在，QQQ 被排除)
    result = filter_universe_symbols(holdings, watchlist, max_symbols=10)
    assert result == ["AAPL", "TSLA", "BRK.B", "NVDA", "MSFT", "AMD", "BRK-B", "GOOGL"]


def test_filter_universe_symbols_caps_max_symbols() -> None:
    """測試標的池數量嚴格受限於 max_symbols。"""
    holdings = [f"H{i}" for i in range(10)]
    watchlist = [f"W{i}" for i in range(20)]

    result = filter_universe_symbols(holdings, watchlist, max_symbols=8)
    assert len(result) == 8
    assert result == [f"H{i}" for i in range(8)]


def _insert_asset(
    db_conn: Any, uid: int, sym: str, ctx: str, meta: dict[str, Any]
) -> None:
    db_conn.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, ?, ?)",
        (uid, sym, ctx, json.dumps(meta)),
    )
    db_conn.commit()


def _seed_assets(db_conn: Any) -> None:
    # 現貨持倉：多單、空單 (qty < 0)、已平倉 (qty == 0)
    _insert_asset(db_conn, 1, "AAPL", "HOLDING", {"quantity": 100, "avg_cost": 150})
    _insert_asset(db_conn, 1, "TSLA", "HOLDING", {"quantity": -50, "avg_cost": 200})
    _insert_asset(db_conn, 2, "IBM", "HOLDING", {"quantity": 0, "avg_cost": 120})
    # 期權部位：賣方 (qty < 0)、已了結 (qty == 0)、ETF 期權
    _insert_asset(
        db_conn,
        2,
        "nvda",
        "TRADE",
        {"opt_type": "put", "strike": 100, "expiry": "2099-01-15", "quantity": -2},
    )
    _insert_asset(
        db_conn,
        2,
        "INTC",
        "TRADE",
        {"opt_type": "call", "strike": 30, "expiry": "2099-01-15", "quantity": 0},
    )
    _insert_asset(
        db_conn,
        1,
        "SPY",
        "TRADE",
        {"opt_type": "put", "strike": 500, "expiry": "2099-01-15", "quantity": 1},
    )
    # 自選清單
    _insert_asset(db_conn, 1, "MSFT", "WATCH", {})
    _insert_asset(db_conn, 2, "AAPL", "WATCH", {})
    _insert_asset(db_conn, 2, "QQQ", "WATCH", {})


def test_fetch_db_raw_symbols_reads_assets_table(db_conn: Any) -> None:
    """以實際 migrated schema（assets 表 + JSON metadata）讀取持倉與自選。

    持倉以 metadata.quantity != 0 篩選（空單負數同樣納入），自選直接取 symbol。
    """
    _seed_assets(db_conn)

    holdings, watchlist = _fetch_db_raw_symbols()

    assert sorted(holdings) == ["AAPL", "NVDA", "SPY", "TSLA"]
    assert "IBM" not in holdings  # qty == 0 的現貨
    assert "INTC" not in holdings  # qty == 0 的期權
    assert sorted(watchlist) == ["AAPL", "MSFT", "QQQ"]


@pytest.mark.asyncio
async def test_get_fundamental_universe_end_to_end(db_conn: Any) -> None:
    """端到端：持倉優先、ETF 排除、跨來源去重。"""
    _seed_assets(db_conn)

    universe = await get_fundamental_universe(max_symbols=10)

    # 持倉個股在前（順序依 DB 讀取），自選僅補上未出現者
    assert set(universe[:3]) == {"AAPL", "TSLA", "NVDA"}
    assert universe[3:] == ["MSFT"]
    assert "SPY" not in universe and "QQQ" not in universe


def test_fetch_db_raw_symbols_empty_db(db_conn: Any) -> None:
    """空資料庫不應丟出例外。"""
    assert _fetch_db_raw_symbols() == ([], [])
