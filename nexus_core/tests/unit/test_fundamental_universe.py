"""單元測試：標的池收集與過濾器 (fundamental_universe.py)。"""

from __future__ import annotations

from services.fundamental_universe import (
    filter_universe_symbols,
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
