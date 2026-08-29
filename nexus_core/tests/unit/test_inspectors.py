from typing import Any
import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.ddp_inspector import DDPInspector
from market_analysis.volatility_inspector import VolatilityInspector


@pytest.mark.asyncio
async def test_ddp_inspector_pass() -> None:
    # Arrange
    inspector = DDPInspector()
    symbol = "NVDA"

    # Mock Income Statement
    # Index needs to have 'Net Income' and 'Total Revenue'
    # Columns are 0 (current) to 5 (oldest)
    q_inc_data = {
        0: [150.0, 500.0],  # Current Q
        1: [140.0, 440.0],  # Q-1
        2: [130.0, 430.0],  # Q-2
        3: [120.0, 420.0],  # Q-3
        4: [100.0, 410.0],  # Q-4 (Year over Year)
        5: [90.0, 400.0],  # Q-5
    }
    df_q_inc = pd.DataFrame(q_inc_data, index=["Net Income", "Total Revenue"])

    # Mock ticker info
    # TTM EPS = 10.0
    # Current P/E = 15.0
    # Forward P/E = 10.0 (fwd_pe < curr_pe)
    mock_info = {
        "trailingPE": 15.0,
        "forwardPE": 10.0,
        "trailingEps": 10.0,
    }

    # Mock Historical DF
    # We want current P/E (15.0) to be < 25th percentile of 3Y P/E.
    # If trailingEps = 10.0, then historical prices should be higher so historical P/E > 15.0.
    # E.g., prices around 200 => P/E = 20.
    dates = pd.date_range(end="2026-05-21", periods=156, freq="W")
    df_hist = pd.DataFrame(
        {"Close": [200.0] * 156, "Volume": [1000] * 156}, index=dates
    )

    with patch("yfinance.Ticker") as m_ticker, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as m_hist:
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
            return func(*args, **kwargs)

        m_call_yf.side_effect = _run_sync

        await inspector.inspect_symbol(symbol)

        assert m_call_yf.await_count >= 2


@pytest.mark.asyncio
async def test_volatility_inspector_inspect_symbol() -> None:
    # Arrange
    inspector = VolatilityInspector()
    symbol = "AAPL"

    # Mock user context
    mock_user_ctx = MagicMock()
    mock_user_ctx.cash_reserve = 10000.0
    mock_user_ctx.monthly_expense = 3000.0

    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {
            "Close": [150.0 + i * 0.1 for i in range(252)],
            "High": [151.0 + i * 0.1 for i in range(252)],
            "Low": [149.0 + i * 0.1 for i in range(252)],
            "Volume": [1000 for _ in range(252)],
        },
        index=dates,
    )

    # Mock earnings info (within 24 hours to trigger high risk)
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 12.0

    with (
        patch("yfinance.Ticker") as m_ticker,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
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
        # Mock yfinance.Ticker
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = {
            "currentPrice": 175.0,
            "impliedVolatility": 0.80,  # High IV to trigger iv_rank > 80
        }
        m_ticker.return_value = mock_ticker_instance

        m_hist.return_value = df_hist
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}

        mock_psq_res = MagicMock()
        mock_psq_res.signal_direction = "Long"
        m_psq.return_value = mock_psq_res

        m_gex.return_value = {"put_wall": 170.0}
        m_exp.return_value = ["2099-12-31"]
        m_vp.return_value = {"hvn": 165.0, "lvn": 167.5}
        # mock atr series returning 2.0
        mock_atr_series = MagicMock()
        mock_atr_series.empty = False
        mock_atr_series.iloc = [-1, 2.0]  # last element 2.0
        mock_atr_series.iloc[-1] = 2.0
        m_atr.return_value = mock_atr_series

        # Act
        report = await inspector.inspect_symbol(symbol, mock_user_ctx)

        # Assert
        assert report is not None
        assert report["symbol"] == symbol
        assert report["price"] == 175.0

        # Assert compatibility keys are present
        assert "iv" in report
        assert "iv_p" in report
        assert "hv" in report
        assert "status" in report
        assert "days_to_earnings" in report
        assert "stop_loss" in report
        assert "daily_theta" in report

        # Check specific values
        assert report["status"] == "高風險事件"
        assert report["days_to_earnings"] == pytest.approx(0.5)
        # Expected calculation: base_stop_loss = 170 - 1.5 * 2 = 167
        # lvn = 167.5, diff = 0.5 <= 167.5 * 0.015 (2.5125) -> triggers LVN avoidance
        # defensive_wall = min(165, 170) = 165 < 167.5 -> base_stop_loss = 165 - 1.5 * 2 = 162.0
        assert report["stop_loss"] == pytest.approx(162.0)


@pytest.mark.asyncio
async def test_volatility_inspector_ddp_opportunity() -> None:
    # Arrange
    inspector = VolatilityInspector()
    symbol = "TSLA"

    # Mock user context
    mock_user_ctx = MagicMock()
    mock_user_ctx.cash_reserve = 10000.0
    mock_user_ctx.monthly_expense = 3000.0

    # Mock historical data (252 days)
    # We want HV_20 to range from something to something.
    # Let's say HV_20 min is 0.2, max is 0.6.
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {
            "Close": [100.0 + i * 0.05 for i in range(252)],
            "High": [101.0 + i * 0.05 for i in range(252)],
            "Low": [99.0 + i * 0.05 for i in range(252)],
            "Volume": [1000 for _ in range(252)],
        },
        index=dates,
    )
    # We will just patch the rolling calculation for easier exact mocking
    # Or just let pandas calculate it. For simplicity, we patch df calculation? No, it calculates in the code:
    # df["Log_Ret"] = np.log(df["Close"] / df["Close"].shift(1))
    # df["HV_20"] = df["Log_Ret"].rolling(window=20).std() * np.sqrt(252)
    # To make the test robust, we can just patch 'get_history_df' but then we have to supply a df that yields correct HV.
    # A flat or slightly growing price gives low HV.

    # Mock earnings info (far away)
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 500.0

    with (
        patch("yfinance.Ticker") as m_ticker,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
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
        # Calculate expected HV_current from mock data so we can set IV appropriately
        # Let's mock the df to just have a preset HV column instead, but the code calculates it.
        # So let's provide a df with prices, and just mock info["impliedVolatility"] to be extremely low (0.01).

        m_hist.return_value = df_hist
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}

        mock_psq_res = MagicMock()
        mock_psq_res.signal_direction = "Long"
        m_psq.return_value = mock_psq_res

        m_gex.return_value = {"put_wall": 0.0}
        m_exp.return_value = ["2099-12-31"]
        m_vp.return_value = {"hvn": 0.0, "lvn": 0.0}
        mock_atr_series = MagicMock()
        mock_atr_series.empty = False
        mock_atr_series.iloc[-1] = 0.0
        m_atr.return_value = mock_atr_series

        # Need to know exactly what the max and min HV will be to set IV.
        # But wait, if we set IV = 0.000018, it's very likely to be < hv_current and IVR < 25%.
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.info = {
            "currentPrice": 200.0,
            "impliedVolatility": 0.000018,  # Extremely low IV, less than hv_min
        }
        m_ticker.return_value = mock_ticker_instance

        # Act
        report = await inspector.inspect_symbol(symbol, mock_user_ctx)

        # Assert
        assert report is not None
        assert report["symbol"] == symbol
        assert report["status"] == "波動率極低"
        assert report["is_opportunity"] is True
        assert report["is_high_risk_vol"] is False
        assert report["strategy"] == "單邊 Call (BTO)"


@pytest.mark.asyncio
async def test_volatility_inspector_dispatches_yfinance_via_call_yf() -> None:
    """確保 `ticker.info` 透過 market_data_service.call_yf (asyncio.to_thread)
    分派，而非在事件迴圈中直接同步呼叫 —— 這類直接呼叫會在 30 分鐘心跳掃描
    期間凍結整個 bot 的事件迴圈（含 Discord 指令回應）。IV 缺失時的 ATM 期權鏈
    fallback 路徑則已整併至集中快取路徑 (market_data_service.get_all_option_
    expiries / get_option_chain)，享有 edge tunnel 降級保護，不再直接呼叫裸
    yfinance，因此不再經過 call_yf。
    """
    inspector = VolatilityInspector()
    symbol = "AAPL"

    mock_user_ctx = MagicMock()
    mock_user_ctx.cash_reserve = 10000.0
    mock_user_ctx.monthly_expense = 3000.0

    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    df_hist = pd.DataFrame(
        {
            "Close": [150.0 + i * 0.1 for i in range(252)],
            "High": [151.0 + i * 0.1 for i in range(252)],
            "Low": [149.0 + i * 0.1 for i in range(252)],
            "Volume": [1000 for _ in range(252)],
        },
        index=dates,
    )
    mock_earnings = MagicMock()
    mock_earnings.tte_hours = 500.0

    with (
        patch("yfinance.Ticker") as m_ticker,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as m_hist,
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
            "market_analysis.volume_profile.calculate_volume_profile_from_df"
        ) as m_vp,
        patch("pandas_ta.atr") as m_atr,
        patch(
            "services.market_data_service.call_yf", new_callable=AsyncMock
        ) as m_call_yf,
    ):
        mock_ticker_instance = MagicMock()
        # impliedVolatility 缺失，強迫觸發 ATM 期權鏈 fallback 路徑
        mock_ticker_instance.info = {"currentPrice": 175.0}
        m_ticker.return_value = mock_ticker_instance

        m_hist.return_value = df_hist
        m_earnings.return_value = mock_earnings
        m_ema.return_value = {"trend": "BULLISH_STRONG"}

        mock_psq_res = MagicMock()
        mock_psq_res.signal_direction = "Long"
        m_psq.return_value = mock_psq_res

        m_gex.return_value = {"put_wall": 0.0}
        m_exp.return_value = ["2099-12-31"]

        mock_chain_obj = MagicMock()
        mock_calls_df = pd.DataFrame({"strike": [175.0], "impliedVolatility": [0.35]})
        mock_chain_obj.calls = mock_calls_df
        m_chain.return_value = mock_chain_obj

        m_vp.return_value = {"hvn": 0.0, "lvn": 0.0}
        mock_atr_series = MagicMock()
        mock_atr_series.empty = False
        mock_atr_series.iloc[-1] = 0.0
        m_atr.return_value = mock_atr_series

        async def _run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        m_call_yf.side_effect = _run_sync

        await inspector.inspect_symbol(symbol, mock_user_ctx)

        # 僅 `.info` 仍經由 call_yf 分派；ATM 期權鏈 fallback 已改走
        # market_data_service 的集中快取路徑，不再經過 call_yf。
        assert m_call_yf.await_count >= 1
        m_chain.assert_awaited_once_with("AAPL", "2099-12-31", prune_pct=None)
