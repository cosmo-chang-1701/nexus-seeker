from typing import Any
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext
from market_analysis.index_microstructure import (
    get_market_regime,
    suggest_boxx_allocation_pct,
    suggest_target_allocation_pct,
    estimate_symbol_gamma_flip,
)
from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
from market_analysis.trading_orchestration import (
    calculate_new_cost_basis,
    recommend_covered_calls,
)


def _create_sample_metrics(**overrides):  # type: ignore
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
    return EnhancedWatchlistMetrics(**payload)  # type: ignore


def _create_sample_event_context(**overrides):  # type: ignore
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
    return WatchlistEventContext(**payload)  # type: ignore


@pytest.mark.asyncio
async def test_get_market_regime_critical() -> None:
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
async def test_suggest_boxx_allocation_pct_crisis_regime() -> None:
    # Regime 為 SHORT_GAMMA_CRITICAL / SYSTEMIC_LIQUIDITY_CRISIS 時，直接回傳
    # 最高防禦建議值 70，且不需再查詢 Fear & Greed 指數。
    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "SHORT_GAMMA_CRITICAL"
        result = await suggest_boxx_allocation_pct()
        assert result == 70.0
        mock_core_metrics.assert_not_called()

    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime2:
        mock_regime2.return_value = "SYSTEMIC_LIQUIDITY_CRISIS"
        result2 = await suggest_boxx_allocation_pct()
        assert result2 == 70.0


@pytest.mark.asyncio
async def test_suggest_boxx_allocation_pct_extreme_fear() -> None:
    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 20.0}
        result = await suggest_boxx_allocation_pct()
        assert result == 60.0


@pytest.mark.asyncio
async def test_suggest_boxx_allocation_pct_extreme_greed() -> None:
    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 80.0}
        result = await suggest_boxx_allocation_pct()
        assert result == 20.0


@pytest.mark.asyncio
async def test_suggest_boxx_allocation_pct_normal_baseline() -> None:
    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 48.0}
        result = await suggest_boxx_allocation_pct()
        assert result == 30.0


@pytest.mark.asyncio
async def test_suggest_target_allocation_pct_tiers() -> None:
    # target_allocation_pct 建議值方向與 boxx_allocation_pct 相反連動：市況越差，
    # 越傾向續抱防禦性核心部位（建議值越高，觸發部署門檻越高）。
    with patch("market_analysis.index_microstructure.get_market_regime") as mock_regime:
        mock_regime.return_value = "SHORT_GAMMA_CRITICAL"
        assert await suggest_target_allocation_pct() == 70.0

    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 20.0}
        assert await suggest_target_allocation_pct() == 60.0

    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 80.0}
        assert await suggest_target_allocation_pct() == 30.0

    with patch(
        "market_analysis.index_microstructure.get_market_regime"
    ) as mock_regime, patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics"
    ) as mock_core_metrics:
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 48.0}
        assert await suggest_target_allocation_pct() == 50.0


@pytest.mark.asyncio
async def test_suggest_target_and_boxx_allocation_pct_never_diverge_on_same_input() -> (
    None
):
    """target_allocation_pct 與 boxx_allocation_pct 兩套總經自動建議機制必須共用
    同一份市況分級 (regime + fear_greed 只評估一次)，確保給定完全相同的市況輸入時，
    兩者的建議值永遠落在彼此對應、不衝突的配對層級上（結構上不可能各自解讀出
    矛盾的市況判斷）。"""
    scenarios = [
        ("SYSTEMIC_LIQUIDITY_CRISIS", None, 70.0, 70.0),
        ("NORMAL", 20.0, 60.0, 60.0),
        ("NORMAL", 80.0, 30.0, 20.0),
        ("NORMAL", 48.0, 50.0, 30.0),
    ]
    for regime_value, fear_greed_value, expected_target, expected_boxx in scenarios:
        with patch(
            "market_analysis.index_microstructure.get_market_regime"
        ) as mock_regime, patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics"
        ) as mock_core_metrics:
            mock_regime.return_value = regime_value
            mock_core_metrics.return_value = {"fear_greed": fear_greed_value}
            target = await suggest_target_allocation_pct()
            boxx = await suggest_boxx_allocation_pct()
            assert target == expected_target
            assert boxx == expected_boxx


@pytest.mark.asyncio
async def test_grid_step_scaling_critical() -> None:
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


def test_boxx_stress_test_math() -> None:
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


def test_new_cost_basis_math() -> None:
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
async def test_recommend_covered_calls_filtering() -> Any:
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
async def test_is_covered_call_unlock_allowed_logic() -> Any:
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


def test_safety_payout_threshold_logic() -> Any:
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with patch("database.get_kv_cache") as mock_kv:
        # Case 1a: Normal (小數格式 0.05)
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 0.05,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # Case 1b: Normal (百分比格式 5.0%)
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 5.0,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # Case 2a: RRP increase > 20% (小數格式 0.25)
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 0.25,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 2b: RRP increase > 20% (百分比格式 25.0%)
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 25.0,
            "macro_rrp_spike": False,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 3: RRP Spike
        mock_kv.side_effect = lambda key: {
            "macro_rrp_change_30d": 5.0,
            "macro_rrp_spike": True,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0


@pytest.mark.asyncio
async def test_get_macro_overview_data_logic() -> Any:
    from cogs.unified_terminal import get_macro_overview_data

    with patch("cogs.unified_terminal.utils.is_memory_safe") as mock_safe, patch(
        "database.get_kv_cache"
    ) as mock_kv, patch("services.market_data_service.get_quote") as mock_quote:
        # We simulate get_quote throwing an Exception so it falls back to mock_kv
        mock_quote.side_effect = Exception("Mocked error")
        # Case 1: memory (RAM + swap) normal
        mock_safe.return_value = True
        mock_kv.side_effect = lambda key: {
            "macro_spx": 5150.0,
            "macro_vix": 18.0,
            "macro_us10y": 4.25,
            "macro_gamma_flip_line": 5180.0,
        }.get(key)

        data = await get_macro_overview_data(1)
        assert data["is_degraded"] is False
        assert data["served_stale_cache"] is False
        assert data["spx"] == 5150.0
        assert data["short_gamma_critical"] is False

        # Case 2: memory (RAM + swap) high, prior cache entry exists for this user
        # -> served from the LRU cache fallback without recomputation
        mock_safe.return_value = False
        data_degraded = await get_macro_overview_data(1)
        assert data_degraded["is_degraded"] is True
        assert data_degraded["served_stale_cache"] is True

        # Case 3: memory (RAM + swap) high, but NO prior cache entry for this user
        # (cold cache) -> full computation still runs; served_stale_cache must be
        # False so the embed layer doesn't falsely claim it skipped computation.
        data_cold_degraded = await get_macro_overview_data(2)
        assert data_cold_degraded["is_degraded"] is True
        assert data_cold_degraded["served_stale_cache"] is False
        assert data_cold_degraded["spx"] == 5150.0


def _get_field_value(embed: Any, field_name: str) -> str:
    for field in embed.fields:
        if field.name == field_name:
            return str(field.value)
    raise AssertionError(f"Field {field_name!r} not found in embed")


def test_market_macro_overview_degradation_warning_wording() -> None:
    """降級警告文案應依實際是否命中 LRU 快取回退區分，避免冷快取時誤稱已簡化運算"""
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    base_macro_data: dict[str, Any] = {
        "spx": 5150.0,
        "vix": 18.0,
        "us10y": 4.25,
        "gamma_flip_line": 5180.0,
        "wti": 75.0,
        "rrp": 420.5,
        "fed_balance": 7.25,
        "cpi_nfp_calendar": "近期無重大數據",
        "fear_greed": 48.0,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "rrp_change_30d": 5.0,
        "short_gamma_critical": False,
        "recession_warning": False,
        "payout_threshold": 13000.0,
        "fedwatch_probability": None,
        "fedwatch_is_fallback": True,
        "fedwatch_details": {},
        "escape_win_status": "NEUTRAL",
        "escape_window_direction": "NONE",
        "escape_window_shift_days": 0,
        "escape_window_tier": "NONE",
        "is_degraded": True,
        "gex_is_fallback": True,
    }

    # 命中 LRU 快取回退：確實跳過了重新運算，維持原有措辭
    cache_hit_data = {**base_macro_data, "served_stale_cache": True}
    embed_cache_hit = build_market_macro_overview_embed(cache_hit_data)
    warning_cache_hit = _get_field_value(embed_cache_hit, "⚠️ 系統降級警告")
    assert "已自動啟用 LRU 降級保護機制，簡化部分動態計算" in warning_cache_hit

    # 冷快取（無先前快取可回退）：本次仍執行完整運算，文案不得宣稱已簡化計算
    cold_cache_data = {**base_macro_data, "served_stale_cache": False}
    embed_cold = build_market_macro_overview_embed(cold_cache_data)
    warning_cold = _get_field_value(embed_cold, "⚠️ 系統降級警告")
    assert "簡化部分動態計算" not in warning_cold
    assert "尚無可用 LRU 快取可供降級回退" in warning_cold
    assert "本次仍執行完整動態運算" in warning_cold


def test_fixed_income_hedging_whitelist() -> None:
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


def test_putwall_crisis_textual_martial_law() -> None:
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


def test_fedwatch_market_overview_embed_formatting() -> None:
    """測試 FedWatch 在 /market 總經 Embed 中的 ANSI 面板呈現與逃頂窗口聯動"""
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    # Case 1: 鷹派加息 (加息 >= 50%)，帶有詳細期貨拆解
    macro_data_hawkish: dict[str, Any] = {
        "spx": 5200.0,
        "vix": 16.5,
        "us10y": 4.25,
        "gamma_flip_line": 5150.0,
        "wti": 75.0,
        "rrp": 420.5,
        "fed_balance": 7.25,
        "cpi_nfp_calendar": "08/20 FOMC",
        "fear_greed": 55.0,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "rrp_change_30d": 5.0,
        "short_gamma_critical": False,
        "recession_warning": False,
        "payout_threshold": 13000.0,
        "fedwatch_probability": 0.7953,
        "fedwatch_is_fallback": False,
        "fedwatch_details": {
            "meeting_date": "09/16",
            "prob_maintain": 40.4,
            "prob_hike": 59.1,
            "prob_cut": 1.4,
            "decision": "hike",
        },
        "escape_win_status": "🟢 正常窗口 (正Gamma護航中)",
    }
    embed_hawkish = build_market_macro_overview_embed(macro_data_hawkish)
    # Check fields
    fields_dict: dict[str, str] = {
        str(field.name): str(field.value) for field in embed_hawkish.fields
    }
    assert "📈 流動性與總經指標 (Liquidity & Macro)" in fields_dict
    assert (
        "FOMC 利率定價 (FedWatch)"
        in fields_dict["📈 流動性與總經指標 (Liquidity & Macro)"]
    )
    assert (
        "(09/16) 鷹派加息 (加息 59.1% / 維持 40.4% / 降息 1.4%)"
        in fields_dict["📈 流動性與總經指標 (Liquidity & Macro)"]
    )
    assert "利率逃頂窗口" in fields_dict["🛡️ 聯動風控引擎狀態 (Risk Engine Status)"]
    assert (
        "🟢 正常窗口 (正Gamma護航中)"
        in fields_dict["🛡️ 聯動風控引擎狀態 (Risk Engine Status)"]
    )

    # 驗證面板內部無重複標題與多餘虛線
    for f_val in fields_dict.values():
        assert " 📊 大盤與核心指標 (Market & Core Indices)" not in f_val
        assert " 🛡️ 聯動風控引擎狀態 (Risk Engine Status)" not in f_val
        assert " 📈 流動性與總經指標 (Liquidity & Macro)" not in f_val
        assert " 📅 總經公布日程" not in f_val

    # Case 2: 降息預期確立 (降息 >= 50%)
    macro_data_dovish: dict[str, Any] = {
        **macro_data_hawkish,
        "fedwatch_probability": 0.25,
        "fedwatch_is_fallback": False,
        "fedwatch_details": {
            "meeting_date": "09/16",
            "prob_maintain": 25.0,
            "prob_hike": 0.0,
            "prob_cut": 75.0,
            "decision": "cut",
        },
        "escape_win_status": "🟢 後推 5 天 (流動性擴張)",
    }
    embed_dovish = build_market_macro_overview_embed(macro_data_dovish)
    fields_dovish: dict[str, str] = {
        str(field.name): str(field.value) for field in embed_dovish.fields
    }
    assert (
        "(09/16) 降息確立 (降息 75.0% / 維持 25.0%)"
        in fields_dovish["📈 流動性與總經指標 (Liquidity & Macro)"]
    )
    assert (
        "後推 5 天 (流動性擴張)"
        in fields_dovish["🛡️ 聯動風控引擎狀態 (Risk Engine Status)"]
    )

    # Case 3: 維持利率 (維持 >= 50%)
    macro_data_maintain: dict[str, Any] = {
        **macro_data_hawkish,
        "fedwatch_probability": 0.50,
        "fedwatch_is_fallback": False,
        "fedwatch_details": {
            "meeting_date": "09/16",
            "prob_maintain": 75.0,
            "prob_hike": 0.0,
            "prob_cut": 25.0,
            "decision": "maintain",
        },
        "escape_win_status": "🟢 正常窗口 (均衡定價)",
    }
    embed_maintain = build_market_macro_overview_embed(macro_data_maintain)
    fields_maintain: dict[str, str] = {
        str(field.name): str(field.value) for field in embed_maintain.fields
    }
    assert (
        "(09/16) 維持利率 (維持 75.0% / 降息 25.0%)"
        in fields_maintain["📈 流動性與總經指標 (Liquidity & Macro)"]
    )


def test_calendar_service_fedwatch_lookup() -> None:
    """測試 calendar_service.get_latest_fedwatch_probability 與 get_latest_fedwatch_info"""
    from services.calendar_service import calendar_service

    # Case 1: kv_cache 命中
    with patch("database.cache.get_kv_cache") as mock_kv:
        mock_kv.side_effect = lambda k: (
            0.65
            if k == "macro_fedwatch_probability"
            else (
                '{"meeting_date": "09/16", "prob_maintain": 65.0, "prob_hike": 0.0, "prob_cut": 35.0}'
                if k == "macro_fedwatch_details"
                else (0 if k == "macro_fedwatch_is_fallback" else None)
            )
        )
        prob, is_fallback = calendar_service.get_latest_fedwatch_probability()
        assert prob == 0.65
        assert is_fallback is False

        p, is_fb, details = calendar_service.get_latest_fedwatch_info()
        assert p == 0.65
        assert is_fb is False
        assert details.get("meeting_date") == "09/16"
        assert details.get("prob_maintain") == 65.0

    # Case 2: kv_cache miss, fallback to SQLite
    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "sqlite3.connect"
    ) as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"fedwatch_probability": 0.85}
        mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cursor
        prob, is_fallback = calendar_service.get_latest_fedwatch_probability()
        assert prob == 0.85
        assert is_fallback is True

    # Case 3: kv_cache 包含污染的 1.0 (100.0% 升息) 數據 -> 自動觸發防禦並轉為 fallback
    with patch("database.cache.get_kv_cache") as mock_kv:
        mock_kv.side_effect = (
            lambda k: 1.0
            if k == "macro_fedwatch_probability"
            else (0 if k == "macro_fedwatch_is_fallback" else None)
        )
        prob, is_fallback = calendar_service.get_latest_fedwatch_probability()
        assert is_fallback is True
        p, is_fb, details = calendar_service.get_latest_fedwatch_info()
        assert is_fb is True
        assert details.get("prob_hike") == 0.0


@pytest.mark.asyncio
async def test_calendar_service_fedwatch_sanity_rejection() -> None:
    """測試 calendar_service.update_fedwatch_probability 遇到 100% 升息等污染數據時觸發防禦阻斷"""
    from services.calendar_service import calendar_service
    import config

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "probability": 1.0,
            "prob_hike": 100.0,
            "prob_maintain": 0.0,
            "prob_cut": 0.0,
            "meeting_date": "03/16",
        },
    }

    with patch.object(config, "TUNNEL_URL", "http://mock-tunnel"), patch(
        "httpx.AsyncClient.get", return_value=mock_resp
    ), patch("database.cache.save_kv_cache") as mock_save:
        await calendar_service.update_fedwatch_probability()
        # 應將 macro_fedwatch_is_fallback 寫入 1，且不應將 1.0 寫入 macro_fedwatch_probability
        saved_keys = [call.args[0] for call in mock_save.call_args_list]
        assert "macro_fedwatch_is_fallback" in saved_keys
        assert "macro_fedwatch_probability" not in saved_keys


def test_evaluate_escape_window_regime_matrix() -> None:
    """測試多因子逃頂窗口矩陣評估邏輯 (四因子)"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    # 1. 鷹派利率 + 負 Gamma -> 前移收縮警戒
    t_score, e_score, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.99,
        cpi_dev=0.0,
        wti=75.0,
        vts_ratio=0.88,
        is_negative_gamma=True,
    )
    assert direction == "前移"
    assert shift >= 5
    assert "收縮警戒" in tier
    assert "⚠️ 前移" in status

    # 2. 鷹派利率 + 正 Gamma 護航 + 通膨穩定 -> 正常窗口
    t_score, e_score, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.99,
        cpi_dev=-0.05,
        wti=72.0,
        vts_ratio=0.85,
        is_negative_gamma=False,
    )
    assert direction == "維持"
    assert shift == 0
    assert "中性平衡" in tier
    assert "🟢 正常窗口 (正Gamma護航中)" == status

    # 3. 寬鬆降息 + 正價差 -> 後推擴張
    t_score, e_score, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.25,
        cpi_dev=-0.1,
        wti=70.0,
        vts_ratio=0.82,
        is_negative_gamma=False,
    )
    assert direction == "後推"
    assert shift == 5
    assert "寬鬆擴張" in tier
    assert "🟢 後推 5 天" in status


def test_evaluate_escape_window_regime_none_prob_safe() -> None:
    """測試逃頂窗口在 prob 為 None 或非數值時具備防禦性中性回退，不拋出 TypeError"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    # Case 1: prob is None
    t_score, e_score, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=None,
        cpi_dev=0.0,
        wti=75.0,
        vts_ratio=0.88,
        is_negative_gamma=False,
    )
    assert direction == "維持"
    assert shift == 0
    assert "中性平衡" in tier
    assert "🟢 正常窗口 (均衡定價)" == status

    # Case 2: prob is malformed
    t_score2, e_score2, dir2, shift2, tier2, status2 = evaluate_escape_window_regime(
        prob="invalid_prob",  # type: ignore[arg-type]
        cpi_dev=0.0,
        wti=75.0,
        vts_ratio=0.88,
        is_negative_gamma=False,
    )
    assert dir2 == "維持"
    assert shift2 == 0


# ---------------------------------------------------------------------------
# estimate_symbol_gamma_flip：個股 Gamma Flip 輕量客戶端估算 (累積 GEX 零交叉點)
# ---------------------------------------------------------------------------


def test_estimate_symbol_gamma_flip_finds_zero_crossing() -> None:
    """累積 GEX 由負轉正的履約價視為 Gamma Flip 估計值"""
    gex_profile = {"90": -50.0, "95": 80.0, "100": 20.0}
    # 累積: 90 -> -50 (負) ; 95 -> +30 (轉正，交叉點) ; 100 -> +50
    assert estimate_symbol_gamma_flip(gex_profile, spot=97.0) == 95.0


def test_estimate_symbol_gamma_flip_all_positive_no_crossing() -> None:
    """全數為正 GEX (無負轉正交叉點) -> 回傳 0.0"""
    gex_profile = {"90": 10.0, "95": 20.0, "100": 30.0}
    assert estimate_symbol_gamma_flip(gex_profile, spot=95.0) == 0.0


def test_estimate_symbol_gamma_flip_all_negative_no_crossing() -> None:
    """全數為負 GEX (無交叉點) -> 回傳 0.0"""
    gex_profile = {"90": -10.0, "95": -20.0, "100": -5.0}
    assert estimate_symbol_gamma_flip(gex_profile, spot=95.0) == 0.0


def test_estimate_symbol_gamma_flip_empty_profile_returns_zero() -> None:
    assert estimate_symbol_gamma_flip({}, spot=100.0) == 0.0
    assert estimate_symbol_gamma_flip(None, spot=100.0) == 0.0  # type: ignore[arg-type]


def test_estimate_symbol_gamma_flip_malformed_profile_returns_zero() -> None:
    """履約價/GEX 值非數值格式 -> fail-safe 回傳 0.0，不拋例外"""
    gex_profile = {"not_a_strike": "not_a_number"}
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 0.0


@pytest.mark.asyncio
async def test_fetch_symbol_gex_metrics_prefers_fresh_edge_cache() -> None:
    """edge 背景排程快取命中且夠新鮮時，應直接採用，完全不觸發即時
    Playwright scrape（不呼叫 httpx 打向 /api/v1/scrape/options/.../gex）。"""
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_symbol_gex_metrics

    edge_payload = {
        "data": {
            "spot": 230.0,
            "net_gex": 500.0,
            "call_wall": 240.0,
            "put_wall": 220.0,
            "gex_profile": {"220.0": 100.0},
        },
        "age_seconds": 120.0,
    }

    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ), patch(
        "services.edge_cache_client.get_cached_gex",
        new_callable=AsyncMock,
        return_value=edge_payload,
    ), patch("httpx.AsyncClient") as mock_client_cls:
        result = await fetch_symbol_gex_metrics("AAPL")

        assert result["call_wall"] == 240.0
        assert result["put_wall"] == 220.0
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_symbol_gex_metrics_falls_back_when_edge_cache_stale() -> None:
    """edge 快取過舊 (超過 3600 秒新鮮度門檻) 時，應完全 fallback 回既有的
    即時 scrape 路徑，行為與 edge 未部署時完全一致。"""
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_symbol_gex_metrics

    stale_edge_payload = {
        "data": {
            "spot": 1.0,
            "net_gex": 1.0,
            "call_wall": 1.0,
            "put_wall": 1.0,
            "gex_profile": {},
        },
        "age_seconds": 9999.0,
    }
    live_scrape_response = {
        "status": "success",
        "data": {
            "spot": 230.0,
            "net_gex": 500.0,
            "call_wall": 240.0,
            "put_wall": 220.0,
            "gex_profile": {"220.0": 100.0},
        },
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = live_scrape_response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ), patch(
        "services.edge_cache_client.get_cached_gex",
        new_callable=AsyncMock,
        return_value=stale_edge_payload,
    ), patch("config.TUNNEL_URL", "http://mock-tunnel"), patch(
        "httpx.AsyncClient", return_value=mock_client
    ):
        result = await fetch_symbol_gex_metrics("AAPL")

        assert result["call_wall"] == 240.0
        assert result["put_wall"] == 220.0
        mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_symbol_gex_metrics_falls_back_when_edge_unreachable() -> None:
    """edge 連不上/離線 (get_cached_gex 回傳 None) 時，應完全 fallback 回
    既有的即時 scrape 路徑，watchlist 心跳不受影響。"""
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_symbol_gex_metrics

    live_scrape_response = {
        "status": "success",
        "data": {
            "spot": 230.0,
            "net_gex": 500.0,
            "call_wall": 240.0,
            "put_wall": 220.0,
            "gex_profile": {"220.0": 100.0},
        },
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = live_scrape_response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ), patch(
        "services.edge_cache_client.get_cached_gex",
        new_callable=AsyncMock,
        return_value=None,
    ), patch("config.TUNNEL_URL", "http://mock-tunnel"), patch(
        "httpx.AsyncClient", return_value=mock_client
    ):
        result = await fetch_symbol_gex_metrics("AAPL")

        assert result["call_wall"] == 240.0
        mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_gex_metrics_uses_last_known_good_cache_when_unreachable() -> None:
    """巨集 GEX (SPY) 抓取逾時/失敗時，應優先回傳最近一次成功抓取的快取值，
    而非寫死的舊常數 (gamma_flip=515.0)，避免與現價脫節造成負 Gamma 誤判。"""
    import httpx
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_gex_metrics

    last_known_good = {
        "data": {"spy_spot": 700.0, "gamma_flip": 690.0, "put_wall": 650.0},
        "timestamp": 1234567890.0,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("database.cache.get_kv_cache", return_value=last_known_good), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ), patch("config.TUNNEL_URL", "http://mock-tunnel"), patch(
        "httpx.AsyncClient", return_value=mock_client
    ):
        result = await fetch_gex_metrics()

        assert result["gamma_flip"] == 690.0
        assert result["spy_spot"] == 700.0
        assert result["_is_stale_cache"] is True


@pytest.mark.asyncio
async def test_fetch_gex_metrics_falls_back_to_static_constant_without_cache() -> None:
    """從未成功抓取過 (無任何歷史快取) 時，仍應安全回退至既有寫死常數，
    維持向後相容行為。"""
    import httpx
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_gex_metrics

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.cache.save_kv_cache", new_callable=AsyncMock
    ), patch("config.TUNNEL_URL", "http://mock-tunnel"), patch(
        "httpx.AsyncClient", return_value=mock_client
    ):
        result = await fetch_gex_metrics()

        assert result == {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}
        assert "_is_stale_cache" not in result
