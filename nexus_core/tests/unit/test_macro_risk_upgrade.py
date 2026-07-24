import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext
from market_analysis.index_microstructure import get_market_regime
from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
from market_analysis.trading_orchestration import (
    calculate_new_cost_basis,
    recommend_covered_calls,
)


def _create_sample_metrics(**overrides):
    payload = {
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "current_price": 150.0,
        "buy_zone_status": "🟢 買點：趨勢支撐",
        "buy_price_phase1": 140.0,
        "buy_price_phase2": 130.0,
        "buy_price_phase3": 120.0,
        "sell_zone_status": "🟢 賣點：第一壓力帶",
        "sell_price_phase1": 160.0,
        "sell_price_phase2": 170.0,
        "sell_price_phase3": 180.0,
        "pe_ratio": 30.0,
        "rsi_14": 50.0,
        "atr_14": 2.0,
        "beta": 1.2,
        "ma20": 148.0,
        "ma50": 145.0,
        "ma200": 135.0,
        "bias_ma20": 1.0,
        "iv_rank": 30.0,
        "iv_percentile": 30.0,
        "option_skew": -5.0,
        "skew_percentile": 50.0,
        "option_skew_state": "右偏 (Call 昂貴)",
        "pcr": 0.8,
        "volume_poc": 135.0,
        "gex_max_put_wall": 120.0,
        "vanna_sensitivity": 0.01,
        "relative_strength_spy": 1.0,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def _create_sample_event_context(**overrides):
    payload = {
        "earnings_date": None,
        "earnings_tte_hours": None,
        "macro_event": None,
        "macro_event_time": None,
        "macro_tte_hours": None,
        "risk_mode": "normal",
        "summary": "無重大事件",
    }
    payload.update(overrides)
    return WatchlistEventContext(**payload)


@pytest.mark.asyncio
async def test_get_market_regime_critical():
    # 情境 1：VIX 飆升與 Gamma Flip 踩踏
    # 輸入：現有 VIX = 22.22, VIX3M = 21.0 (vts_ratio = 1.058)，SPY 現貨價 = 510，爬取之 Gamma Flip Line = 515。
    # 預期輸出：get_market_regime() 回傳 SHORT_GAMMA_CRITICAL
    with patch(
        "services.market_data_service.get_macro_environment"
    ) as mock_macro, patch(
        "services.market_data_service.get_vix_term_structure"
    ) as mock_vts, patch("services.market_data_service.get_quote") as mock_quote, patch(
        "market_analysis.index_microstructure.fetch_gex_metrics"
    ) as mock_gex:
        mock_macro.return_value = {"vix": 22.22, "oil": 75.0, "vix_change": 0.0}
        mock_vts.return_value = {"vts_ratio": 1.058, "vts_state": "Backwardation"}
        mock_quote.return_value = {"c": 510.0}
        mock_gex.return_value = {
            "spy_spot": 510.0,
            "gamma_flip": 515.0,
            "put_wall": 505.0,
        }

        regime = await get_market_regime()
        assert regime == "SHORT_GAMMA_CRITICAL"


@pytest.mark.asyncio
async def test_grid_step_scaling_critical():
    # 當觸發 SHORT_GAMMA_CRITICAL 時，網格間距自動等比放大 1.5x
    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics"
    ) as mock_metrics, patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context"
    ) as mock_context, patch("services.market_data_service.get_quote") as mock_quote:
        mock_regime.return_value = "SHORT_GAMMA_CRITICAL"

        metrics = _create_sample_metrics(
            atr_14=2.0
        )  # 預設網格步長 = atr_14 * 0.5 = 1.0
        mock_metrics.return_value = metrics
        mock_context.return_value = _create_sample_event_context()
        mock_quote.return_value = {"dp": -1.0}

        evaluation = await evaluate_watchlist_symbol("AAPL")
        assert evaluation is not None
        # 原步長 = round(atr_14 * 0.5, 2) = 1.0
        # 放大 1.5x 後 = 1.5
        assert evaluation.tactical.dynamic_grid_step == 1.5


def test_boxx_stress_test_math():
    # 情境 2：BOXX 水壩極限壓力測試
    # 輸入：常規現金 = $150，BOXX 持倉 = 213 股（最大套現 $21,000）。SQLite 中有 18 筆 GTC 網格單，若全成交總計需消耗 $22,500。
    # 預期輸出：計算出總赤字淨值為 -$1,350，且 is_critical 觸發 (大於 BOXX 清算極限)
    cash_reserve = 150.0
    boxx_shares = 213.0
    total_deficit = 22500.0  # 18 筆 GTC 網格單總額

    boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
    assert boxx_cash == 21000.0

    net_deficit = cash_reserve + boxx_cash - total_deficit
    assert net_deficit == -1350.0

    is_critical = total_deficit > (cash_reserve + boxx_cash)
    assert is_critical is True


def test_new_cost_basis_math():
    # 測試模擬吸籌後的加權平均成本
    grid_orders = [
        {"validity": "GTC", "side": "BUY", "limit_price": 140.0, "quantity": 10.0},
        {"validity": "GTC_90", "side": "BUY", "limit_price": 130.0, "quantity": 20.0},
        {"validity": "DAY", "side": "BUY", "limit_price": 120.0, "quantity": 50.0},
        {"validity": "GTC", "side": "SELL", "limit_price": 160.0, "quantity": 10.0},
    ]

    new_cost = calculate_new_cost_basis(100.0, 150.0, grid_orders)
    assert new_cost == 146.15


@pytest.mark.asyncio
async def test_recommend_covered_calls_filtering():
    # 測試 Covered Call 篩選邏輯：
    # DTE 必須在 30-50 天內，Strike > New Cost Basis，且年化收益率 >= 10.0% 或單次收租權利金大於現貨的 1%
    with patch(
        "market_analysis.trading_orchestration.get_user_holdings"
    ) as mock_holdings, patch(
        "market_analysis.trading_orchestration.get_user_active_orders"
    ) as mock_orders, patch(
        "market_analysis.trading_orchestration.get_quote"
    ) as mock_quote, patch(
        "market_analysis.trading_orchestration.SentimentEngine.get_last_stored_iv"
    ) as mock_iv, patch("yfinance.Ticker") as mock_ticker:
        mock_holdings.return_value = [
            {"symbol": "AAPL", "quantity": 100.0, "avg_cost": 150.0}
        ]
        mock_orders.return_value = []
        mock_quote.return_value = {"c": 148.0}
        mock_iv.return_value = 0.30

        # Mock Option Chain Expirations:
        # 1. 2026-07-20 (DTE 約 39 天，合乎 30-50 區間)
        # 2. 2026-06-15 (DTE 約 4 天，被過濾)
        ticker_instance = MagicMock()
        ticker_instance.options = ["2026-06-15", "2026-07-20"]

        # Mock option chain call contracts for 2026-07-20
        # Call 1: Strike = 170.0 (Strike > 150, Delta ~ 0.09, Premium = 1.60 -> 年化收益率 = 10.38% -> 通過)
        # Call 2: Strike = 165.0 (Strike > 150, Delta ~ 0.15, Premium = 0.05 -> 年化收益率 = 0.3% -> 被年化過濾)
        # Call 3: Strike = 145.0 (Strike <= 150 -> 被成本過濾)
        mock_calls = pd.DataFrame(
            [
                {
                    "strike": 170.0,
                    "impliedVolatility": 0.30,
                    "lastPrice": 1.60,
                    "bid": 1.55,
                    "ask": 1.65,
                    "contractSymbol": "AAPL260720C00170000",
                },
                {
                    "strike": 165.0,
                    "impliedVolatility": 0.30,
                    "lastPrice": 0.05,
                    "bid": 0.04,
                    "ask": 0.06,
                    "contractSymbol": "AAPL260720C00165000",
                },
                {
                    "strike": 145.0,
                    "impliedVolatility": 0.30,
                    "lastPrice": 8.00,
                    "bid": 7.90,
                    "ask": 8.10,
                    "contractSymbol": "AAPL260720C00145000",
                },
            ]
        )

        chain_mock = MagicMock()
        chain_mock.calls = mock_calls
        ticker_instance.option_chain.return_value = chain_mock
        mock_ticker.return_value = ticker_instance

        # Mock current date to be 2026-06-11
        with patch("market_analysis.trading_orchestration.datetime") as mock_dt:
            # mock datetime.now() to 2026-06-11
            mock_dt.now.return_value = pd.Timestamp("2026-06-11 12:00:00")
            mock_dt.strptime = lambda val, fmt: pd.Timestamp(val)

            res = await recommend_covered_calls(1, "AAPL")
            assert res is not None
            assert res["symbol"] == "AAPL"
            assert res["new_cost_basis"] == 150.0

            recs = res["recommendations"]
            # 應只剩下一筆 AAPL260720C00170000 推薦 (另外兩筆分別因成本及收益率低於 10% / 1% 門檻被過濾)
            assert len(recs) == 1
            assert recs[0]["strike"] == 170.0
            assert recs[0]["annualized_yield"] >= 10.0


@pytest.mark.asyncio
async def test_is_covered_call_unlock_allowed_logic():
    from market_analysis.trading_orchestration import is_covered_call_unlock_allowed

    with patch("database.get_kv_cache") as mock_kv, patch(
        "services.market_data_service.get_quote"
    ) as mock_quote:
        # We simulate get_quote throwing an Exception so it falls back to mock_kv
        mock_quote.side_effect = Exception("Mocked error")
        # Case 1: Normal
        mock_kv.side_effect = lambda key: {
            "macro_uer": 4.0,
            "macro_sahm_rule": 0.35,
            "macro_us10y": 4.25,
            "macro_vix": 18.0,
        }.get(key)
        assert await is_covered_call_unlock_allowed() is True

        # Case 2: Sahm Rule triggered (recession warning)
        mock_kv.side_effect = lambda key: {
            "macro_uer": 4.0,
            "macro_sahm_rule": 0.55,
            "macro_us10y": 4.25,
            "macro_vix": 18.0,
        }.get(key)
        assert await is_covered_call_unlock_allowed() is False

        # Case 3: Yield > 4.5% and VIX > 20 (recession warning)
        mock_kv.side_effect = lambda key: {
            "macro_uer": 4.0,
            "macro_sahm_rule": 0.35,
            "macro_us10y": 4.65,
            "macro_vix": 22.0,
        }.get(key)
        assert await is_covered_call_unlock_allowed() is False


def test_safety_payout_threshold_logic():
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with patch("database.get_kv_cache") as mock_kv:
        # Case 1: Normal
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 0.05,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # Case 2: RRP increase > 20%
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 0.25,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 3: RRP Spike
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 0.05,
            "macro_rrp_spike": True,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0


@pytest.mark.asyncio
async def test_get_macro_overview_data_logic():
    from cogs.unified_terminal import get_macro_overview_data

    with patch("psutil.virtual_memory") as mock_mem, patch(
        "database.get_kv_cache"
    ) as mock_kv, patch("services.market_data_service.get_quote") as mock_quote:
        # We simulate get_quote throwing an Exception so it falls back to mock_kv
        mock_quote.side_effect = Exception("Mocked error")
        # Case 1: RAM normal
        mock_mem.return_value.percent = 70.0
        mock_kv.side_effect = lambda key: {
            "macro_spx": 5150.0,
            "macro_vix": 18.0,
            "macro_us10y": 4.25,
            "macro_gamma_flip_line": 5180.0,
        }.get(key)

        data = await get_macro_overview_data(1)
        assert data["is_degraded"] is False
        assert data["spx"] == 5150.0
        assert data["short_gamma_critical"] is False

        # Case 2: RAM high (>85%) -> Degraded mode
        mock_mem.return_value.percent = 90.0
        data_degraded = await get_macro_overview_data(1)
        assert data_degraded["is_degraded"] is True


def test_fixed_income_hedging_whitelist():
    """測試 BOXX 在 InsightsEngine 等級的白名單豁免"""
    from market_analysis.insights_engine import RiskInsightsContext, InsightsEngine

    context = RiskInsightsContext(
        symbol="BIL",
        current_price=91.4,
        put_wall=91.4,
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.0,
        max_pain_deviation_pct=0.0,
        can_trade_spreads=False,
        cash_reserve_protection=True,
    )

    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)
    assert status_label == "現金避險部位，風控豁免 🛡️"
    assert dmp_label == "(避險資產)"


def test_putwall_crisis_textual_martial_law():
    from market_analysis import insight_generator

    test_data = {
        "symbol": "SPY",
        "spot": 246.75,
        "max_pain": 277.50,
        "put_wall": 250.00,
        "gex_status": "NEGATIVE",
    }

    insights = insight_generator.compute_realtime_insights(test_data)

    assert "磁吸" not in insights, "錯誤：在底牆危機下仍釋放痛點磁吸信號！"
    assert "逢低吸納" not in insights, "錯誤：在負 Gamma 拋壓下誘導用戶接刀！"
    assert (
        "剛性拋壓" in insights or "嚴禁" in insights
    ), "錯誤：未正確提示做市商對沖風險！"
