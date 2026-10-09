from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import yf_api
from local_api import app
from yf_api import (
    _is_rate_limit_error,
    fetch_nearest_option_chain,
    scrape_yf_history,
    scrape_yf_options_chain,
    scrape_yf_options_expiries,
)
from yfinance.exceptions import YFRateLimitError


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


def test_scrape_yf_history_rate_limit_returns_429() -> None:
    """Ticker.history 拋 YFRateLimitError → 回 HTTP 429 + status=rate_limited，
    且不得再以 repair=False 重打一次（只會加重限流）。"""
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = YFRateLimitError()

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = scrape_yf_history("AAPL", period="1y", interval="1d")

    assert res.status_code == 429  # type: ignore[union-attr]
    assert b"rate_limited" in res.body  # type: ignore[union-attr]
    assert fake_ticker.history.call_count == 1


def test_scrape_yf_history_generic_error_returns_status_error() -> None:
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ValueError("boom")

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = scrape_yf_history("AAPL")

    assert isinstance(res, dict)
    assert res["status"] == "error"
    assert "boom" in res["message"]
    # 一般錯誤維持既有 repair=True → repair=False 兩次嘗試
    assert fake_ticker.history.call_count == 2


def test_is_rate_limit_error_ignores_epoch_timestamp() -> None:
    """含 epoch 時間戳（如 1791429600）的訊息不得被誤判為 429。"""
    assert _is_rate_limit_error(ValueError("period1=1791429600 invalid")) is False
    assert _is_rate_limit_error(ValueError("HTTP 429")) is True
    assert _is_rate_limit_error(ValueError("Error 429: slow down")) is True
    assert _is_rate_limit_error(ValueError("TOO MANY REQUESTS")) is True
    assert _is_rate_limit_error(YFRateLimitError()) is True


def test_scrape_yf_history_timestamp_error_is_not_rate_limited() -> None:
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ValueError("bad range 1791429600")
    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = scrape_yf_history("AAPL")
    assert isinstance(res, dict)
    assert res["status"] == "error"


def test_scrape_yf_history_queue_timeout_returns_503_busy() -> None:
    """排隊逾時回 503 status=busy，且不呼叫 Yahoo、不洩漏 semaphore。"""
    fake_ticker = MagicMock()
    sem = MagicMock()
    sem.acquire.return_value = False
    with (
        patch("yf_api.yf.Ticker", return_value=fake_ticker),
        patch.object(yf_api, "_HISTORY_SEMAPHORE", sem),
    ):
        res = scrape_yf_history("AAPL")
    assert res.status_code == 503  # type: ignore[union-attr]
    assert b"busy" in res.body  # type: ignore[union-attr]
    sem.acquire.assert_called_once_with(timeout=8)
    sem.release.assert_not_called()
    fake_ticker.history.assert_not_called()


@pytest.mark.asyncio
async def test_options_expiries_rate_limit_returns_429() -> None:
    fake_ticker = MagicMock()
    type(fake_ticker).options = property(
        lambda self: (_ for _ in ()).throw(YFRateLimitError())
    )
    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = await scrape_yf_options_expiries("AAPL")
    assert res.status_code == 429  # type: ignore[union-attr]
    assert b"rate_limited" in res.body  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_options_chain_rate_limit_returns_429() -> None:
    fake_ticker = MagicMock()
    fake_ticker.option_chain.side_effect = YFRateLimitError()
    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = await scrape_yf_options_chain("AAPL", expiry="2026-10-16")
    assert res.status_code == 429  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_options_chain_generic_error_stays_status_error() -> None:
    fake_ticker = MagicMock()
    fake_ticker.option_chain.side_effect = ValueError("boom")
    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = await scrape_yf_options_chain("AAPL", expiry="2026-10-16")
    assert isinstance(res, dict)
    assert res["status"] == "error"


def test_scrape_yf_history_endpoint_nan_becomes_null() -> None:
    """K 棒含 NaN 時端點回 200，NaN 轉 null（原本 JSON 序列化會 500）。"""
    idx = pd.DatetimeIndex(
        ["2026-10-07", "2026-10-08"], tz="America/New_York", name="Date"
    )
    df = pd.DataFrame({"Close": [101.5, float("nan")]}, index=idx)
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = df

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = TestClient(app).get("/api/v1/scrape/yf/history/AAPL")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["data"][0]["Close"] == 101.5
    assert body["data"][1]["Close"] is None


def test_scrape_yf_options_chain_endpoint_nan_becomes_null() -> None:
    calls = pd.DataFrame({"strike": [100.0], "volume": [float("nan")]})
    puts = pd.DataFrame({"strike": [100.0], "bid": [float("nan")]})
    fake_ticker = _make_fake_ticker(("2026-10-16",), _FakeChain(calls, puts))

    with patch("yf_api.yf.Ticker", return_value=fake_ticker):
        res = TestClient(app).get(
            "/api/v1/scrape/yf/options/AAPL/chain", params={"expiry": "2026-10-16"}
        )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["calls"][0]["volume"] is None
    assert body["data"]["puts"][0]["bid"] is None
