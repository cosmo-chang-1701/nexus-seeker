from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from yf_api import fetch_nearest_option_chain


class _FakeChain:
    def __init__(self, calls: pd.DataFrame, puts: pd.DataFrame) -> None:
        self.calls = calls
        self.puts = puts


def _make_fake_ticker(expiries: tuple[str, ...], chain: _FakeChain) -> MagicMock:
    fake_ticker = MagicMock()
    fake_ticker.option_chain.return_value = chain
    fake_ticker.options = expiries
    return fake_ticker


@pytest.mark.asyncio
async def test_fetch_nearest_option_chain_uses_single_option_chain_call() -> None:
    """option_chain(date=None) 已內含最近到期日的完整資料，.options 只是讀取
    Ticker 內部快取，不應該再觸發額外的 yfinance 網路請求（不應呼叫
    option_chain 超過一次）。"""
    calls = pd.DataFrame([{"strike": 100.0}])
    puts = pd.DataFrame([{"strike": 90.0}])
    fake_ticker = _make_fake_ticker(
        ("2026-09-18", "2026-09-25"), _FakeChain(calls, puts)
    )

    with patch("yf_api.yf.Ticker", return_value=fake_ticker) as mock_ticker_cls:
        result = await fetch_nearest_option_chain("AAPL")

    mock_ticker_cls.assert_called_once_with("AAPL")
    fake_ticker.option_chain.assert_called_once_with()

    assert result is not None
    assert result["expiry"] == "2026-09-18"
    assert result["calls"] == [{"strike": 100.0}]
    assert result["puts"] == [{"strike": 90.0}]


@pytest.mark.asyncio
async def test_fetch_nearest_option_chain_returns_none_when_no_expiries() -> None:
    fake_ticker = _make_fake_ticker((), _FakeChain(pd.DataFrame(), pd.DataFrame()))

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        result = await fetch_nearest_option_chain("AAPL")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_nearest_option_chain_stringifies_last_trade_date() -> None:
    calls = pd.DataFrame(
        [{"strike": 100.0, "lastTradeDate": pd.Timestamp("2026-08-25", tz="UTC")}]
    )
    puts = pd.DataFrame(
        [{"strike": 90.0, "lastTradeDate": pd.Timestamp("2026-08-25", tz="UTC")}]
    )
    fake_ticker = _make_fake_ticker(("2026-09-18",), _FakeChain(calls, puts))

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        result = await fetch_nearest_option_chain("AAPL")

    assert result is not None
    assert isinstance(result["calls"][0]["lastTradeDate"], str)
    assert isinstance(result["puts"][0]["lastTradeDate"], str)
