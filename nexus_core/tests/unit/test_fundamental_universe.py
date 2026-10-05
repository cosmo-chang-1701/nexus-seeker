"""單元測試：標的池收集與過濾器 (fundamental_universe.py)。"""

from __future__ import annotations

from services.fundamental_universe import (
    filter_universe_symbols,
    is_etf_or_index,
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

    # 典型個股標的
    assert is_etf_or_index("AAPL") is False
    assert is_etf_or_index("MSFT") is False
    assert is_etf_or_index("NVDA") is False
    assert is_etf_or_index("TSLA") is False
    assert is_etf_or_index("SMCI") is False


def test_filter_universe_symbols_prioritizes_holdings() -> None:
    """測試持倉標的優先於自選標的，並正確剔除 ETF。"""
    holdings = ["AAPL", "SPY", "TSLA", "NVDA"]
    watchlist = ["NVDA", "MSFT", "QQQ", "AMD", "GOOGL"]

    # 預期順序：
    # 1. 持倉個股: AAPL, TSLA, NVDA (SPY 被排除)
    # 2. 自選個股: MSFT, AMD, GOOGL (NVDA 已存在，QQQ 被排除)
    result = filter_universe_symbols(holdings, watchlist, max_symbols=10)
    assert result == ["AAPL", "TSLA", "NVDA", "MSFT", "AMD", "GOOGL"]


def test_filter_universe_symbols_caps_max_symbols() -> None:
    """測試標的池數量嚴格受限於 max_symbols。"""
    holdings = [f"H{i}" for i in range(10)]
    watchlist = [f"W{i}" for i in range(20)]

    result = filter_universe_symbols(holdings, watchlist, max_symbols=8)
    assert len(result) == 8
    assert result == [f"H{i}" for i in range(8)]
