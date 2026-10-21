from typing import Any
import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.ddp_inspector import DDPInspector
from market_analysis.volatility_inspector import VolatilityInspector


@pytest.fixture(autouse=True)
def _clear_pe_profile_cache() -> Any:
    from services.market_data_service import fundamentals

    fundamentals._pe_profile_cache.clear()
    yield
    fundamentals._pe_profile_cache.clear()


@pytest.mark.asyncio
async def test_ddp_inspector_pass() -> None:
    # Arrange
    inspector = DDPInspector()
    symbol = "NVDA"

    # Columns are report dates, newest first
    cols = pd.to_datetime(
        [
            "2026-07-26",
            "2026-04-26",
            "2026-01-25",
            "2025-10-26",
            "2025-07-27",
            "2025-04-27",
        ]
    )
    q_inc_data = {
        cols[0]: [150.0, 500.0],  # Current Q
        cols[1]: [140.0, 440.0],  # Q-1
        cols[2]: [130.0, 430.0],  # Q-2
        cols[3]: [120.0, 420.0],  # Q-3
        cols[4]: [100.0, 410.0],  # Q-4 (Year over Year)
        cols[5]: [90.0, 400.0],  # Q-5
    }
    df_q_inc = pd.DataFrame(q_inc_data, index=["Net Income", "Total Revenue"])

    # Current P/E = 15.0 <= 25th percentile of last 12 quarterly TTM P/E (all 20)
    mock_info = {"trailingPE": 15.0, "forwardPE": 10.0, "trailingEps": 10.0}
    dates = pd.date_range(end="2026-05-21", periods=156, freq="W")
    df_hist = pd.DataFrame(
        {"Close": [200.0] * 156, "Volume": [1000] * 156}, index=dates
    )

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist, patch(
        "services.market_data_service.fundamentals.get_pe_ttm_profile",
        new_callable=AsyncMock,
        return_value=(15.0, [20.0] * 12),
    ):
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.quarterly_income_stmt = df_q_inc
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        m_hist.return_value = df_hist

        # Act
        report = await inspector.inspect_symbol(symbol)

        # Assert
        assert report is not None
        assert report["is_ddp"]
        assert report["symbol"] == symbol
        assert bool(report["rev_accel"]) is True
        assert report["current_pe"] == 15.0
        assert report["pe_25th"] == 20.0
        assert report["pe_mean_3y"] == 20.0
        assert report["pe_history_quarters"] == 12
        assert report["fiscal_period"] == "2026-07-26"


@pytest.mark.asyncio
async def test_ddp_inspector_dispatches_yfinance_via_call_yf() -> None:
    """確保 `ticker.info` / `ticker.quarterly_income_stmt` /
    `ticker.quarterly_cashflow` 皆透過 market_data_service.call_yf
    (asyncio.to_thread) 分派，而非在事件迴圈中直接同步呼叫 —— 這類直接呼叫
    會在 30 分鐘心跳掃描期間凍結整個 bot 的事件迴圈（含 Discord 指令回應）。
    """
    inspector = DDPInspector()
    symbol = "NVDA"

    q_inc_data = {
        0: [150.0, 500.0],
        1: [140.0, 440.0],
        2: [130.0, 430.0],
        3: [120.0, 420.0],
        4: [100.0, 410.0],
        5: [90.0, 400.0],
    }
    df_q_inc = pd.DataFrame(q_inc_data, index=["Net Income", "Total Revenue"])
    # forwardPE 缺失，強迫觸發 quarterly_cashflow 的第二次 call_yf 分派路徑
    mock_info = {"trailingPE": 15.0, "trailingEps": 10.0}
    df_q_cash = pd.DataFrame({0: [50.0]}, index=["Operating Cash Flow"])

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.call_yf", new_callable=AsyncMock
    ) as m_call_yf:
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.quarterly_income_stmt = df_q_inc
        mock_ticker_instance.quarterly_cashflow = df_q_cash
        mock_ticker_instance.info = mock_info
        m_ticker.return_value = mock_ticker_instance

        async def _run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
            kwargs.pop("_endpoint", None)  # 計數用參數，不傳給 func
            return func(*args, **kwargs)

        m_call_yf.side_effect = _run_sync

        await inspector.inspect_symbol(symbol)

        assert m_call_yf.await_count >= 2


def _vol_df(step: float = 0.1) -> pd.DataFrame:
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    return pd.DataFrame(
        {
            "Close": [150.0 + i * step for i in range(252)],
            "High": [151.0 + i * step for i in range(252)],
            "Low": [149.0 + i * step for i in range(252)],
            "Volume": [1000 for _ in range(252)],
        },
        index=dates,
    )


def _vol_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.cash_reserve = 10000.0
    ctx.monthly_expense = 3000.0
    return ctx


def _iv_metrics(**kw: Any) -> Any:
    from models.quant import IVMetrics

    base: dict[str, Any] = {
        "symbol": "AAPL",
        "current_iv": 0.80,
        "iv_rank": 90.0,
        "iv_source": "LIVE_IV",
        "hv_20": 0.30,
        "iv_history_count": 80,
        "reference_spot_price": 175.0,
    }
    base.update(kw)
    return IVMetrics(**base)


@pytest.mark.asyncio
async def test_volatility_inspector_inspect_symbol() -> None:
    """IVR 極高且財報 12 小時內未公布 -> 高風險事件；停損沿用 LVN 迴避邏輯。"""
    inspector = VolatilityInspector()
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 12.0
    mock_earnings.is_imminent = True
    mock_earnings.is_released = False

    with (
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
        patch(
            "market_analysis.sentiment.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as m_iv,
        patch(
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new_callable=AsyncMock,
        ) as m_earnings,
        patch(
            "market_analysis.volatility_inspector.evaluate_ema_trend",
            new_callable=AsyncMock,
        ) as m_ema,
        patch("market_analysis.volatility_inspector.analyze_psq") as m_psq,
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
        ) as m_gex,
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as m_exp,
        patch(
            "market_analysis.volume_profile.calculate_volume_profile_from_df"
        ) as m_vp,
        patch("pandas_ta.atr") as m_atr,
    ):
        m_hist.return_value = _vol_df()
        m_iv.return_value = _iv_metrics()
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}
        m_psq.return_value = MagicMock(signal_direction="Long")
        m_gex.return_value = {"put_wall": 170.0}
        m_exp.return_value = ["2099-12-31"]
        m_vp.return_value = {"hvn": 165.0, "lvn": 167.5}
        mock_atr_series = MagicMock()
        mock_atr_series.empty = False
        mock_atr_series.iloc = [-1, 2.0]
        mock_atr_series.iloc[-1] = 2.0
        m_atr.return_value = mock_atr_series

        report = await inspector.inspect_symbol("AAPL", _vol_ctx())

        assert report is not None
        assert report["symbol"] == "AAPL"
        assert report["price"] == 175.0
        for key in (
            "iv",
            "iv_p",
            "hv",
            "status",
            "days_to_earnings",
            "stop_loss",
            "daily_theta",
            "iv_source",
            "iv_history_count",
        ):
            assert key in report
        assert report["iv_rank"] == 90.0 and report["iv_p"] == 90.0
        assert report["iv_current"] == 80.0
        assert report["is_high_risk_vol"] is True
        assert report["status"] == "高風險事件"
        assert report["strategy"] == "風險中性 (Risk Neutral / Defensive)"
        assert report["days_to_earnings"] == pytest.approx(0.5)
        # base_stop_loss = 170 - 1.5*2 = 167；LVN 167.5 迴避 -> 165 - 1.5*2 = 162
        assert report["stop_loss"] == pytest.approx(162.0)


@pytest.mark.asyncio
async def test_volatility_inspector_ddp_opportunity() -> None:
    """低 IVR + IV < HV20 + 動能 -> 波動率極低機會，單邊 Call。"""
    inspector = VolatilityInspector()
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 500.0
    mock_earnings.is_imminent = False
    mock_earnings.is_released = False

    with (
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
        patch(
            "market_analysis.sentiment.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as m_iv,
        patch(
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new_callable=AsyncMock,
        ) as m_earnings,
        patch(
            "market_analysis.volatility_inspector.evaluate_ema_trend",
            new_callable=AsyncMock,
        ) as m_ema,
        patch("market_analysis.volatility_inspector.analyze_psq") as m_psq,
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
        ) as m_gex,
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as m_exp,
        patch(
            "market_analysis.volume_profile.calculate_volume_profile_from_df"
        ) as m_vp,
        patch("pandas_ta.atr") as m_atr,
    ):
        m_hist.return_value = _vol_df(0.05)
        m_iv.return_value = _iv_metrics(
            current_iv=0.20, iv_rank=15.0, hv_20=0.40, reference_spot_price=200.0
        )
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}
        m_psq.return_value = MagicMock(signal_direction="Long")
        m_gex.return_value = {"put_wall": 0.0}
        m_exp.return_value = ["2099-12-31"]
        m_vp.return_value = {"hvn": 0.0, "lvn": 0.0}
        mock_atr_series = MagicMock()
        mock_atr_series.empty = False
        mock_atr_series.iloc[-1] = 0.0
        m_atr.return_value = mock_atr_series

        report = await inspector.inspect_symbol("TSLA", _vol_ctx())

        assert report is not None
        assert report["symbol"] == "TSLA"
        assert report["status"] == "波動率極低"
        assert report["is_opportunity"] is True
        assert report["is_high_risk_vol"] is False
        assert report["strategy"] == "單邊 Call (BTO)"


@pytest.mark.asyncio
async def test_volatility_inspector_uses_canonical_iv_not_yahoo() -> None:
    """IV 來源改為 SentimentEngine 正典指標：不再經 call_yf 讀 `.info`，
    也不再以最近到期日 ATM 期權鏈 fallback 推 IV。"""
    inspector = VolatilityInspector()
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 500.0
    mock_earnings.is_imminent = False
    mock_earnings.is_released = False

    with (
        patch("yfinance.Ticker") as m_ticker,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
        patch(
            "market_analysis.sentiment.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as m_iv,
        patch(
            "services.calendar_service.calendar_service.get_symbol_earnings",
            new_callable=AsyncMock,
        ) as m_earnings,
        patch(
            "market_analysis.volatility_inspector.evaluate_ema_trend",
            new_callable=AsyncMock,
        ) as m_ema,
        patch("market_analysis.volatility_inspector.analyze_psq") as m_psq,
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
        ) as m_gex,
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as m_exp,
        patch(
            "services.market_data_service.get_option_chain", new_callable=AsyncMock
        ) as m_chain,
        patch(
            "services.market_data_service.call_yf", new_callable=AsyncMock
        ) as m_call_yf,
    ):
        m_hist.return_value = _vol_df()
        m_iv.return_value = _iv_metrics(current_iv=0.20, iv_rank=15.0, hv_20=0.40)
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}
        m_psq.return_value = MagicMock(signal_direction="Long")
        m_gex.return_value = {"put_wall": 0.0}
        m_exp.return_value = ["2099-12-31"]

        report = await inspector.inspect_symbol("AAPL", _vol_ctx())

        assert report is not None
        m_iv.assert_awaited_once_with("AAPL")
        m_call_yf.assert_not_awaited()
        m_chain.assert_not_awaited()
        m_ticker.assert_not_called()


@pytest.mark.asyncio
async def test_ddp_inspector_yahoo_cooldown_returns_none_without_raising() -> None:
    """Yahoo 429 冷卻中 call_yf 拋 YahooRateLimitedError：fail-safe 回 None（不中斷 /x gather）。"""
    from services.market_data_service import YahooRateLimitedError

    inspector = DDPInspector()
    with patch(
        "services.market_data_service.call_yf",
        new_callable=AsyncMock,
        side_effect=YahooRateLimitedError("cooldown"),
    ):
        assert await inspector.inspect_symbol("NVDA") is None
    assert "限流" in inspector.last_fail_reason["NVDA"]


@pytest.mark.asyncio
async def test_volatility_inspector_yahoo_cooldown_returns_none() -> None:
    from services.market_data_service import YahooRateLimitedError

    df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(260)]})
    with (
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df,
        ),
        patch(
            "services.market_data_service.call_yf",
            new_callable=AsyncMock,
            side_effect=YahooRateLimitedError("cooldown"),
        ),
    ):
        assert await VolatilityInspector().inspect_symbol("NVDA", None) is None


@pytest.mark.asyncio
async def test_ddp_inspector_yahoo_gate_busy_returns_none_without_raising() -> None:
    """Yahoo 閘門排隊逾時／佇列滿載（YahooEdgeBusyError）：同樣 fail-safe 回 None。"""
    from services.market_data_service import YahooEdgeBusyError

    inspector = DDPInspector()
    with patch(
        "services.market_data_service.call_yf",
        new_callable=AsyncMock,
        side_effect=YahooEdgeBusyError("gate busy"),
    ):
        assert await inspector.inspect_symbol("NVDA") is None
    assert "排隊逾時" in inspector.last_fail_reason["NVDA"]


@pytest.mark.asyncio
async def test_volatility_inspector_yahoo_gate_busy_returns_none() -> None:
    from services.market_data_service import YahooEdgeBusyError

    df = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(260)]})
    with (
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df,
        ),
        patch(
            "services.market_data_service.call_yf",
            new_callable=AsyncMock,
            side_effect=YahooEdgeBusyError("gate busy"),
        ),
    ):
        assert await VolatilityInspector().inspect_symbol("NVDA", None) is None
