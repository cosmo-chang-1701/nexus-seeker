from typing import Any
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import pandas as pd
from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext
import market_analysis.index_microstructure as index_microstructure
from market_analysis.index_microstructure import (
    get_market_regime,
    fetch_core_macro_metrics,
    get_spx_capped_from_above_signal,
    invalidate_market_regime_cache,
    invalidate_core_macro_metrics_cache,
    invalidate_spx_capped_from_above_signal_cache,
    detect_uoa_sto_call_physical_cap,
    suggest_target_allocation_pct,
    estimate_symbol_gamma_flip,
)
from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
from market_analysis.trading_orchestration import (
    calculate_new_cost_basis,
    recommend_covered_calls,
    get_covered_shares,
)
from services.single_flight import SingleFlightManager


@pytest.fixture(autouse=True)
def _reset_regime_and_core_macro_caches() -> Any:
    """Phase 1 (get_market_regime/fetch_core_macro_metrics 記憶體快取) 的
    測試隔離：確保每個測試皆從乾淨的快取狀態開始，避免測試執行順序造成
    快取命中/未命中結果不確定。

    一併清 SingleFlightManager._active_tasks 是跨測試隔離（前一個測試若留下
    仍在飛行中的共享任務，會被本測試併入）。**已完成**的殘留任務不需要在此
    處理——`run()` 已顯式排除它們，見 tests/unit/test_concurrency_robustness.py
    的 test_single_flight_never_reuses_a_completed_task。"""

    def _reset() -> None:
        index_microstructure._market_regime_cache_value = None
        index_microstructure._market_regime_cache_expiry = 0.0
        index_microstructure._core_macro_metrics_cache_value = None
        index_microstructure._core_macro_metrics_cache_expiry = 0.0
        index_microstructure._spx_capped_signal_cache_value = None
        index_microstructure._spx_capped_signal_cache_expiry = 0.0
        SingleFlightManager._active_tasks.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def mock_fetch_symbol_gex_metrics() -> Any:
    """覆寫 conftest.py 同名的全域 session-scoped autouse mock：本檔案的測試
    (`test_fetch_symbol_gex_metrics_*`) 目的就是驗證 `fetch_symbol_gex_metrics`
    本身的真實邊緣快取/即時抓取降級邏輯，不能被全域 mock 取代，故在本模組層級
    以空 fixture 覆寫（pytest 依 fixture 名稱就近覆寫，本模組定義的版本優先於
    conftest.py），讓真實函式在本檔案內維持不變。"""
    yield None


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
    with (
        patch("services.market_data_service.get_vix_spot_strict") as mock_vix,
        patch("services.market_data_service.get_vix_term_structure") as mock_vts,
        patch("services.market_data_service.get_quote") as mock_quote,
        patch("market_analysis.index_microstructure.fetch_gex_metrics") as mock_gex,
    ):
        mock_vix.return_value = 22.22
        mock_vts.return_value = {
            "vts_ratio": 1.058,
            "vts_state": "Backwardation",
            "is_valid": True,
        }
        mock_quote.return_value = {"c": 510.0}
        mock_gex.return_value = {
            "spy_spot": 510.0,
            "gamma_flip": 515.0,
            "put_wall": 505.0,
        }

        regime = await get_market_regime()
        assert regime == "SHORT_GAMMA_CRITICAL"


@pytest.mark.asyncio
async def test_get_market_regime_caches_within_ttl() -> None:
    """Phase 1：TTL 內第二次呼叫應直接命中記憶體快取，不重新觸發底層運算
    (_compute_market_regime_uncached，內含 2 支未快取的邊緣爬蟲 HTTP 端點)。"""
    with patch(
        "market_analysis.index_microstructure._compute_market_regime_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = "NORMAL"

        first = await get_market_regime()
        second = await get_market_regime()

        assert first == "NORMAL"
        assert second == "NORMAL"
        mock_compute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_market_regime_refetches_after_ttl_expiry() -> None:
    """Phase 1：快取過期後應重新觸發底層運算，而非永久回傳陳舊市況判讀
    （此值直接影響 SHORT_GAMMA_CRITICAL 等交易安全機制，過期後必須重抓）。"""
    with patch(
        "market_analysis.index_microstructure._compute_market_regime_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = "NORMAL"

        await get_market_regime()
        mock_compute.assert_awaited_once()

        # 模擬 TTL 到期：直接將快取到期時間撥回過去，而非等待真實時間流逝。
        # 不需要清 SingleFlightManager._active_tasks——`run()` 已顯式排除「已完成
        # 但尚未被 done_callback 清掉」的任務，重取不再依賴回呼時序
        # （見 tests/unit/test_concurrency_robustness.py 的不變式測試）。
        index_microstructure._market_regime_cache_expiry = 0.0

        await get_market_regime()
        assert mock_compute.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_market_regime_cache_forces_refetch() -> None:
    """Phase 1：/force_macro_update 手動刷新 GEX/流動性數據後呼叫此函式，
    應使下一次 get_market_regime() 立即重新運算，而非等待 TTL 到期。"""
    with patch(
        "market_analysis.index_microstructure._compute_market_regime_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = "NORMAL"

        await get_market_regime()
        mock_compute.assert_awaited_once()

        invalidate_market_regime_cache()

        await get_market_regime()
        assert mock_compute.await_count == 2


@pytest.mark.asyncio
async def test_fetch_core_macro_metrics_caches_within_ttl() -> None:
    """Phase 1：fetch_core_macro_metrics() 沿用與 get_market_regime() 相同的
    快取機制，TTL 內第二次呼叫不應重新觸發底層邊緣爬蟲抓取。"""
    fallback = {
        "rrp": 420.5,
        "fed_balance": 7.25,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "fear_greed": 48.0,
    }
    with patch(
        "market_analysis.index_microstructure._fetch_core_macro_metrics_uncached",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = fallback

        first = await fetch_core_macro_metrics()
        second = await fetch_core_macro_metrics()

        assert first == fallback
        assert second == fallback
        mock_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_core_macro_metrics_cache_forces_refetch() -> None:
    with patch(
        "market_analysis.index_microstructure._fetch_core_macro_metrics_uncached",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = {"fear_greed": 48.0}

        await fetch_core_macro_metrics()
        mock_fetch.assert_awaited_once()

        invalidate_core_macro_metrics_cache()

        await fetch_core_macro_metrics()
        assert mock_fetch.await_count == 2


@pytest.mark.asyncio
async def test_get_spx_capped_from_above_signal_caches_within_ttl() -> None:
    """Phase A：TTL 內第二次呼叫應直接命中記憶體快取，不重新觸發底層運算
    (內含 SPY GEX Profile 抓取與 UOA 掃描兩支網路請求)。"""
    with patch(
        "market_analysis.index_microstructure._compute_spx_capped_from_above_signal_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"is_capped": True}

        first = await get_spx_capped_from_above_signal()
        second = await get_spx_capped_from_above_signal()

        assert first == {"is_capped": True}
        assert second == {"is_capped": True}
        mock_compute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_spx_capped_from_above_signal_refetches_after_ttl_expiry() -> None:
    with patch(
        "market_analysis.index_microstructure._compute_spx_capped_from_above_signal_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"is_capped": False}

        await get_spx_capped_from_above_signal()
        mock_compute.assert_awaited_once()

        index_microstructure._spx_capped_signal_cache_expiry = 0.0

        await get_spx_capped_from_above_signal()
        assert mock_compute.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_spx_capped_from_above_signal_cache_forces_refetch() -> None:
    with patch(
        "market_analysis.index_microstructure._compute_spx_capped_from_above_signal_uncached",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"is_capped": False}

        await get_spx_capped_from_above_signal()
        mock_compute.assert_awaited_once()

        invalidate_spx_capped_from_above_signal_cache()

        await get_spx_capped_from_above_signal()
        assert mock_compute.await_count == 2


@pytest.mark.asyncio
async def test_spx_capped_signal_false_when_regime_not_normal() -> None:
    """危機模式下不建議賣方策略，即使 SPY 結構上確實受制於上方，is_capped 仍應
    為 False，且不應浪費網路請求去抓取 SPY GEX/UOA 資料。"""
    with (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="SHORT_GAMMA_CRITICAL",
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
        ) as mock_gex,
    ):
        signal = await get_spx_capped_from_above_signal()

    assert signal["is_capped"] is False
    assert signal["regime"] == "SHORT_GAMMA_CRITICAL"
    mock_gex.assert_not_awaited()


@pytest.mark.asyncio
async def test_spx_capped_signal_false_when_no_negative_gamma_swamp() -> None:
    """NORMAL 市況但 SPY 上方未偵測到負 Gamma 泥淖 -> is_capped False，
    且不應為此額外觸發 UOA 掃描 (無泥淖時封頂與否已不影響結論)。"""
    with (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={"spot": 500.0, "gex_profile": {"510": 100.0}},
        ),
        patch(
            "market_analysis.index_microstructure.find_overhead_negative_gex_swamp",
            return_value=(0.0, 0.0),
        ),
        patch(
            "market_analysis.sentiment.uoa_detector.detect_uoa",
            new_callable=AsyncMock,
        ) as mock_uoa,
    ):
        signal = await get_spx_capped_from_above_signal()

    assert signal["is_capped"] is False
    assert signal["swamp_strike"] == 0.0
    mock_uoa.assert_not_awaited()


@pytest.mark.asyncio
async def test_spx_capped_signal_false_when_swamp_present_but_no_sto_cap() -> None:
    """NORMAL 市況、SPY 上方有負 Gamma 泥淖，但無 STO Call 物理封頂 -> is_capped
    仍應為 False (兩項結構訊號須同時成立)。"""
    with (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={"spot": 500.0, "gex_profile": {"510": -6_000_000.0}},
        ),
        patch(
            "market_analysis.index_microstructure.find_overhead_negative_gex_swamp",
            return_value=(510.0, -6_000_000.0),
        ),
        patch(
            "market_analysis.sentiment.uoa_detector.detect_uoa",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        signal = await get_spx_capped_from_above_signal()

    assert signal["is_capped"] is False
    assert signal["swamp_strike"] == 510.0
    assert signal["has_uoa_physical_cap"] is False


@pytest.mark.asyncio
async def test_spx_capped_signal_true_when_both_conditions_met() -> None:
    """NORMAL 市況、SPY 上方同時存在負 Gamma 泥淖與 STO Call 物理封頂 ->
    is_capped True。"""
    with (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={"spot": 500.0, "gex_profile": {"510": -6_000_000.0}},
        ),
        patch(
            "market_analysis.index_microstructure.find_overhead_negative_gex_swamp",
            return_value=(510.0, -6_000_000.0),
        ),
        patch(
            "market_analysis.sentiment.uoa_detector.detect_uoa",
            new_callable=AsyncMock,
            return_value=[
                {
                    "type": "CALL",
                    "action": "STO",
                    "strike": 515.0,
                    "ratio": 2.5,
                }
            ],
        ),
    ):
        signal = await get_spx_capped_from_above_signal()

    assert signal["is_capped"] is True
    assert signal["swamp_strike"] == 510.0
    assert signal["has_uoa_physical_cap"] is True
    assert signal["capping_strike"] == 515.0


def test_detect_uoa_sto_call_physical_cap_finds_capping_strike() -> None:
    uoa_list = [
        {"type": "CALL", "action": "STO", "strike": 105.0, "ratio": 1.5},
        {"type": "PUT", "action": "STO", "strike": 95.0, "ratio": 5.0},
    ]
    has_cap, strike = detect_uoa_sto_call_physical_cap(uoa_list, spot=100.0)
    assert has_cap is True
    assert strike == 105.0


def test_detect_uoa_sto_call_physical_cap_ignores_below_spot_or_low_ratio() -> None:
    uoa_list = [
        {"type": "CALL", "action": "STO", "strike": 95.0, "ratio": 5.0},  # 現價下方
        {"type": "CALL", "action": "STO", "strike": 105.0, "ratio": 0.5},  # ratio 過低
        {"type": "CALL", "action": "BTO", "strike": 110.0, "ratio": 5.0},  # 非 STO
    ]
    has_cap, strike = detect_uoa_sto_call_physical_cap(uoa_list, spot=100.0)
    assert has_cap is False
    assert strike == 0.0


@pytest.mark.asyncio
async def test_suggest_target_allocation_pct_tiers() -> None:
    # 市況越差，越傾向續抱防禦性核心部位（建議目標配置越高）。
    with patch("market_analysis.index_microstructure.get_market_regime") as mock_regime:
        mock_regime.return_value = "SHORT_GAMMA_CRITICAL"
        assert await suggest_target_allocation_pct() == 70.0

    with (
        patch("market_analysis.index_microstructure.get_market_regime") as mock_regime,
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics"
        ) as mock_core_metrics,
    ):
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 20.0}
        assert await suggest_target_allocation_pct() == 60.0

    with (
        patch("market_analysis.index_microstructure.get_market_regime") as mock_regime,
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics"
        ) as mock_core_metrics,
    ):
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 80.0}
        assert await suggest_target_allocation_pct() == 30.0

    with (
        patch("market_analysis.index_microstructure.get_market_regime") as mock_regime,
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics"
        ) as mock_core_metrics,
    ):
        mock_regime.return_value = "NORMAL"
        mock_core_metrics.return_value = {"fear_greed": 48.0}
        assert await suggest_target_allocation_pct() == 50.0


@pytest.mark.asyncio
async def test_grid_step_scaling_critical() -> None:
    # 當觸發 SHORT_GAMMA_CRITICAL 時，網格間距自動等比放大 1.5x
    with (
        patch("market_analysis.index_microstructure.get_market_regime") as mock_regime,
        patch(
            "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics"
        ) as mock_metrics,
        patch(
            "market_analysis.intraday_pipeline.build_watchlist_event_context"
        ) as mock_context,
        patch("services.market_data_service.get_quote") as mock_quote,
    ):
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
@pytest.mark.slow
async def test_recommend_covered_calls_filtering() -> Any:
    # 測試 Covered Call 篩選邏輯：
    # DTE 必須在 30-50 天內，Strike > New Cost Basis，且年化收益率 >= 10.0% 或單次收租權利金大於現貨的 1%
    with (
        # 衰退閘門改為未知時 fail-closed；本測試聚焦合約篩選，明確放行。
        patch(
            "market_analysis.trading_orchestration.is_covered_call_unlock_allowed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "market_analysis.trading_orchestration.get_user_holdings"
        ) as mock_holdings,
        patch(
            "market_analysis.trading_orchestration.get_user_active_orders"
        ) as mock_orders,
        patch(
            "market_analysis.trading_orchestration.get_covered_shares"
        ) as mock_covered,
        patch("market_analysis.trading_orchestration.get_quote") as mock_quote,
        patch(
            "market_analysis.trading_orchestration.SentimentEngine.get_last_stored_iv"
        ) as mock_iv,
        patch(
            "market_analysis.trading_orchestration.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as mock_expiries,
        patch(
            "market_analysis.trading_orchestration.get_option_chain",
            new_callable=AsyncMock,
        ) as mock_chain,
    ):
        mock_holdings.return_value = [
            {"symbol": "AAPL", "quantity": 100.0, "avg_cost": 150.0}
        ]
        mock_orders.return_value = []
        mock_covered.return_value = (0.0, [])
        mock_quote.return_value = {"c": 148.0}
        mock_iv.return_value = 0.30

        # Mock Option Chain Expirations:
        # 1. 2026-07-20 (DTE 約 39 天，合乎 30-50 區間)
        # 2. 2026-06-15 (DTE 約 4 天，被過濾)
        mock_expiries.return_value = ["2026-06-15", "2026-07-20"]

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
        mock_chain.return_value = chain_mock

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

            # 已整併至集中快取路徑 (market_data_service)，且不裁減履約價範圍
            # (成本基礎附近的合約可能落在現價 ±10% 之外)
            mock_expiries.assert_awaited_once_with("AAPL")
            mock_chain.assert_awaited_once_with("AAPL", "2026-07-20", prune_pct=None)


def test_get_covered_shares_sums_existing_short_calls() -> None:
    # 測試 get_covered_shares 正確加總既有 Short Call 鎖定的股數，且忽略其他標的/多單/賣權
    portfolio_rows = [
        (
            1,
            "AAPL",
            "call",
            160.0,
            "2026-07-20",
            5.0,
            -1,
            150.0,
            0.0,
            0.0,
            0.0,
            "HEDGE",
        ),
        (
            2,
            "AAPL",
            "call",
            165.0,
            "2026-08-17",
            3.0,
            -2,
            150.0,
            0.0,
            0.0,
            0.0,
            "HEDGE",
        ),
        (3, "AAPL", "put", 140.0, "2026-07-20", 2.0, -1, 150.0, 0.0, 0.0, 0.0, "HEDGE"),
        (
            4,
            "AAPL",
            "call",
            200.0,
            "2026-07-20",
            4.0,
            1,
            150.0,
            0.0,
            0.0,
            0.0,
            "SPECULATIVE",
        ),
        (
            5,
            "MSFT",
            "call",
            400.0,
            "2026-07-20",
            6.0,
            -1,
            300.0,
            0.0,
            0.0,
            0.0,
            "HEDGE",
        ),
    ]
    with patch("database.portfolio.get_user_portfolio", return_value=portfolio_rows):
        covered_shares, existing_calls = get_covered_shares(1, "aapl")

    # 僅計入 AAPL 的 2 筆 Short Call (100 + 200 = 300 股)，忽略 Put、多單與其他標的
    assert covered_shares == 300.0
    assert len(existing_calls) == 2
    assert {c["strike"] for c in existing_calls} == {160.0, 165.0}


@pytest.mark.asyncio
@pytest.mark.slow
async def test_recommend_covered_calls_fully_covered_returns_none() -> Any:
    # 測試現股已全數被既有 Short Call 覆蓋時，應直接跳過建議 (回傳 None)
    with (
        # 衰退閘門改為未知時 fail-closed；本測試聚焦合約篩選，明確放行。
        patch(
            "market_analysis.trading_orchestration.is_covered_call_unlock_allowed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "market_analysis.trading_orchestration.get_user_holdings"
        ) as mock_holdings,
        patch(
            "market_analysis.trading_orchestration.get_covered_shares"
        ) as mock_covered,
    ):
        mock_holdings.return_value = [
            {"symbol": "AAPL", "quantity": 100.0, "avg_cost": 150.0}
        ]
        mock_covered.return_value = (
            100.0,
            [
                {
                    "strike": 160.0,
                    "expiry": "2026-07-20",
                    "quantity": -1,
                    "shares_covered": 100.0,
                }
            ],
        )

        res = await recommend_covered_calls(1, "AAPL")
        assert res is None


@pytest.mark.asyncio
@pytest.mark.slow
async def test_recommend_covered_calls_partial_coverage_caps_contracts() -> Any:
    # 測試部分覆蓋時，推薦口數應被裁切至尚未覆蓋股數上限 (uncovered_shares // 100)
    with (
        # 衰退閘門改為未知時 fail-closed；本測試聚焦合約篩選，明確放行。
        patch(
            "market_analysis.trading_orchestration.is_covered_call_unlock_allowed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "market_analysis.trading_orchestration.get_user_holdings"
        ) as mock_holdings,
        patch(
            "market_analysis.trading_orchestration.get_user_active_orders"
        ) as mock_orders,
        patch(
            "market_analysis.trading_orchestration.get_covered_shares"
        ) as mock_covered,
        patch("market_analysis.trading_orchestration.get_quote") as mock_quote,
        patch(
            "market_analysis.trading_orchestration.SentimentEngine.get_last_stored_iv"
        ) as mock_iv,
        patch(
            "market_analysis.trading_orchestration.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as mock_expiries,
        patch(
            "market_analysis.trading_orchestration.get_option_chain",
            new_callable=AsyncMock,
        ) as mock_chain,
    ):
        # 現股 300 股，既有 2 口 Short Call 已鎖定 200 股 -> 僅剩 100 股可用 (max_new_contracts = 1)
        mock_holdings.return_value = [
            {"symbol": "AAPL", "quantity": 300.0, "avg_cost": 150.0}
        ]
        mock_orders.return_value = []
        mock_covered.return_value = (
            200.0,
            [
                {
                    "strike": 160.0,
                    "expiry": "2026-07-20",
                    "quantity": -2,
                    "shares_covered": 200.0,
                }
            ],
        )
        mock_quote.return_value = {"c": 148.0}
        mock_iv.return_value = 0.30

        mock_expiries.return_value = ["2026-07-20"]

        # 兩筆合約皆符合 Strike > New Cost Basis / Delta < 0.15 / 年化收益率 >= 10% 門檻，
        # 但 max_new_contracts = 1，應僅保留履約價最低的一筆
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
                    "strike": 172.0,
                    "impliedVolatility": 0.30,
                    "lastPrice": 1.60,
                    "bid": 1.55,
                    "ask": 1.65,
                    "contractSymbol": "AAPL260720C00172000",
                },
            ]
        )

        chain_mock = MagicMock()
        chain_mock.calls = mock_calls
        mock_chain.return_value = chain_mock

        with patch("market_analysis.trading_orchestration.datetime") as mock_dt:
            mock_dt.now.return_value = pd.Timestamp("2026-06-11 12:00:00")
            mock_dt.strptime = lambda val, fmt: pd.Timestamp(val)

            res = await recommend_covered_calls(1, "AAPL")
            assert res is not None
            assert res["covered_shares"] == 200.0
            assert res["uncovered_shares"] == 100.0
            assert res["max_new_contracts"] == 1

            recs = res["recommendations"]
            assert len(recs) == 1
            assert recs[0]["strike"] == 170.0


@pytest.mark.asyncio
async def test_is_covered_call_unlock_allowed_logic() -> Any:
    from market_analysis.trading_orchestration import is_covered_call_unlock_allowed

    with (
        patch("database.get_kv_cache") as mock_kv,
        patch("services.market_data_service.get_quote") as mock_quote,
        # VIX 一律走嚴格即時抓取 (不再讀 macro_vix KV 舊值)
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=18.0,
        ) as mock_vix,
    ):
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
        mock_vix.return_value = 22.0
        assert await is_covered_call_unlock_allowed() is False


def test_safety_payout_threshold_logic() -> Any:
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with (
        patch("database.get_kv_cache") as mock_kv,
        # 不受共用 DB 的總經日曆影響（4 天內有 FOMC/CPI/PCE 會回 16,500）
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
    ):
        # Case 1: Normal (+5.0%)
        mock_kv.side_effect = lambda key: {"macro_rrp_change_30d": 5.0}.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # Case 2: RRP increase > 20% (+25.0%)
        mock_kv.side_effect = lambda key: {"macro_rrp_change_30d": 25.0}.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 3: 剛好 20.0% 不觸發（嚴格大於）
        mock_kv.side_effect = lambda key: {"macro_rrp_change_30d": 20.0}.get(key)
        assert get_safety_payout_threshold() == 13000.0


@pytest.mark.parametrize("small_pct", [0.25, 0.5, 0.99, 1.0])
def test_safety_payout_threshold_reads_rrp_change_as_percent_only(
    small_pct: float,
) -> None:
    """edge 的 rrp_change_30d 一律是百分比：+0.5% 不得被當成比例 0.5（+50%）
    而誤觸 $18,000（已刪除「小數比例相容」分支）。"""
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with (
        patch(
            "database.get_kv_cache",
            side_effect=lambda key: {
                "macro_rrp": 300.0,
                "macro_rrp_change_30d": small_pct,
            }.get(key),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
    ):
        assert get_safety_payout_threshold() == 13000.0


def test_safety_payout_threshold_event_week() -> None:
    """4 天內有 FOMC／CPI／PCE 時回 $16,500；RRP 壓力優先於事件週。"""
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    events = [{"event": "FOMC Rate Decision"}]
    with (
        patch(
            "database.get_kv_cache",
            side_effect=lambda key: {"macro_rrp_change_30d": 5.0}.get(key),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=events),
    ):
        assert get_safety_payout_threshold() == 16500.0
    with (
        patch(
            "database.get_kv_cache",
            side_effect=lambda key: {
                "macro_rrp": 300.0,
                "macro_rrp_change_30d": 25.0,
            }.get(key),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=events),
    ):
        assert get_safety_payout_threshold() == 18000.0


@pytest.mark.asyncio
@pytest.mark.slow
async def test_get_macro_overview_data_logic() -> Any:
    from cogs.unified_terminal import get_macro_overview_data

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe") as mock_safe,
        patch("database.get_kv_cache") as mock_kv,
        patch("services.market_data_service.get_quote") as mock_quote,
    ):
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


@pytest.mark.asyncio
async def test_get_macro_overview_data_short_gamma_critical_spy_basis() -> Any:
    """P1: 驗證 build_macro_terminal_embed_context (get_macro_overview_data)
    使用 SPY 現貨價比對 SPY Gamma Flip，消除 SPX 10x basis 扭曲導致的 split-brain。"""
    from cogs.unified_terminal.utils import (
        build_macro_terminal_embed_context,
        get_macro_overview_data,
    )

    # 驗證別名一致性
    assert build_macro_terminal_embed_context is get_macro_overview_data

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("database.get_kv_cache") as mock_kv,
        patch("services.market_data_service.get_quote") as mock_quote,
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
    ):
        mock_quote.side_effect = Exception("Fallback to kv")

        # 情境 A: SPY < spy_gamma_flip, VIX > 20, VTS >= 1.0 (Backwardation)
        # SPX 雖然看似在 5180 以上 (5190)，但 SPY 510 < SPY Flip 515，應觸發 short_gamma_critical
        mock_kv.side_effect = lambda key: {
            "macro_spx": 5190.0,
            "macro_spy_spot": 510.0,
            "macro_spy_gamma_flip": 515.0,
            "macro_gamma_flip_line": 5150.0,
            "macro_vix": 22.0,
            "macro_us10y": 4.25,
            "macro_vts_ratio": 1.05,
        }.get(key)

        data = await get_macro_overview_data(999)
        assert data["spy_spot"] == 510.0
        assert data["spy_gamma_flip"] == 515.0
        assert data["short_gamma_critical"] is True

        # 情境 B: SPY >= spy_gamma_flip, 即使 SPX 數值失真 (5000)，也不得誤觸發
        mock_kv.side_effect = lambda key: {
            "macro_spx": 5000.0,
            "macro_spy_spot": 520.0,
            "macro_spy_gamma_flip": 515.0,
            "macro_gamma_flip_line": 5150.0,
            "macro_vix": 22.0,
            "macro_us10y": 4.25,
            "macro_vts_ratio": 1.05,
        }.get(key)

        data2 = await get_macro_overview_data(1000)
        assert data2["spy_spot"] == 520.0
        assert data2["spy_gamma_flip"] == 515.0
        assert data2["short_gamma_critical"] is False


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

    # Case 4: ZQ 期貨階梯算式飽和 (ladder_saturated)——降息 100%/維持 0% 聚合值不變，
    # 但必須附帶 1碼/2碼+ 拆解，避免使用者誤讀成對降息幅度也 100% 確定。
    # 對應使用者回報的「(09/16) 降息確立 (降息 100.0% / 維持 0.0%)」bug。
    macro_data_ladder_saturated: dict[str, Any] = {
        **macro_data_hawkish,
        "fedwatch_probability": 0.05,
        "fedwatch_is_fallback": False,
        "fedwatch_details": {
            "meeting_date": "09/16",
            "prob_maintain": 0.0,
            "prob_hike": 0.0,
            "prob_cut": 100.0,
            "prob_cut_25": 60.0,
            "prob_cut_50": 40.0,
            "ladder_saturated": True,
            "decision": "cut",
        },
        "escape_win_status": "🟢 後推 5 天 (流動性擴張)",
    }
    embed_ladder_saturated = build_market_macro_overview_embed(
        macro_data_ladder_saturated
    )
    fields_ladder_saturated: dict[str, str] = {
        str(field.name): str(field.value) for field in embed_ladder_saturated.fields
    }
    assert (
        "(09/16) 降息確立 (降息 100.0% (1碼 60.0% / 2碼+ 40.0%) / 維持 0.0%)"
        in fields_ladder_saturated["📈 流動性與總經指標 (Liquidity & Macro)"]
    )
    # 舊版失真格式「降息 100.0% / 維持 0.0%」不應再單獨出現(需帶有拆解說明)
    assert (
        "降息 100.0% / 維持 0.0%"
        not in fields_ladder_saturated["📈 流動性與總經指標 (Liquidity & Macro)"]
    )


def test_calendar_service_fedwatch_lookup() -> None:
    """測試 calendar_service.get_latest_fedwatch_probability 與 get_latest_fedwatch_info"""
    from services.calendar_service import calendar_service

    # Case 1: kv_cache 命中
    with patch("database.cache.get_kv_cache") as mock_kv, patch(
        "database.cache.get_fedwatch_probability_fresh", return_value=(0.65, False)
    ):
        mock_kv.side_effect = lambda k: (
            0.65
            if k == "macro_fedwatch_probability"
            else (
                '{"meeting_date": "09/16", "prob_maintain": 65.0, "prob_hike": 0.0, "prob_cut": 35.0}'
                if k == "macro_fedwatch_details"
                else (0 if k == "macro_fedwatch_is_fallback" else None)
            )
        )
        prob, is_fallback, _stale = calendar_service.get_latest_fedwatch_probability()
        assert prob == 0.65
        assert is_fallback is False

        p, is_fb, details, _stale = calendar_service.get_latest_fedwatch_info()
        assert p == 0.65
        assert is_fb is False
        assert details.get("meeting_date") == "09/16"
        assert details.get("prob_maintain") == 65.0

    # Case 2: kv_cache miss, fallback to SQLite
    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch(
            "database.cache.get_fedwatch_probability_fresh", return_value=(None, False)
        ),
        patch("sqlite3.connect") as mock_conn,
    ):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"fedwatch_probability": 0.85}
        mock_conn.return_value.cursor.return_value = mock_cursor
        prob, is_fallback, _stale = calendar_service.get_latest_fedwatch_probability()
        assert prob == 0.85
        assert is_fallback is True

    # Case 3: kv_cache 包含污染的 1.0 (100.0% 升息) 數據 -> 自動觸發防禦並轉為 fallback
    with patch("database.cache.get_kv_cache") as mock_kv, patch(
        "database.cache.get_fedwatch_probability_fresh", return_value=(1.0, False)
    ):
        mock_kv.side_effect = lambda k: (
            1.0
            if k == "macro_fedwatch_probability"
            else (0 if k == "macro_fedwatch_is_fallback" else None)
        )
        prob, is_fallback, _stale = calendar_service.get_latest_fedwatch_probability()
        assert is_fallback is True
        p, is_fb, details, _stale = calendar_service.get_latest_fedwatch_info()
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

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient.get", return_value=mock_resp),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_fedwatch_probability()
        # 應將 macro_fedwatch_is_fallback 寫入 1，且不應將 1.0 寫入 macro_fedwatch_probability
        saved_keys = [call.args[0] for call in mock_save.call_args_list]
        assert "macro_fedwatch_is_fallback" in saved_keys
        assert "macro_fedwatch_probability" not in saved_keys


@pytest.mark.asyncio
async def test_calendar_service_fedwatch_cut_saturation_without_explanation_rejected() -> (
    None
):
    """測試 prob_cut 飽和至 100% 但缺乏 ladder 拆解說明（既非 Atlanta Fed 多桶來源，
    也沒有 ladder_saturated 旗標）時，比照既有的 prob_hike 對稱防禦邏輯，視為疑似
    異常/污染數據並觸發防禦阻斷。對應使用者回報的「(09/16) 降息確立 (降息 100.0% /
    維持 0.0%)」bug 修正前的資料形狀（見 macro.py 修正前的單一步階 clamp）。"""
    from services.calendar_service import calendar_service
    import config

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "probability": 0.05,
            "prob_hike": 0.0,
            "prob_maintain": 0.0,
            "prob_cut": 100.0,
            "meeting_date": "09/16",
            "source": "unexplained-source",
        },
    }

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient.get", return_value=mock_resp),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_fedwatch_probability()
        saved_keys = [call.args[0] for call in mock_save.call_args_list]
        assert "macro_fedwatch_is_fallback" in saved_keys
        assert "macro_fedwatch_probability" not in saved_keys


@pytest.mark.asyncio
async def test_calendar_service_fedwatch_cut_saturation_with_ladder_breakdown_accepted() -> (
    None
):
    """測試 prob_cut 飽和至 100% 但附帶修正後 ZQ 階梯算式的 ladder_saturated +
    prob_cut_25/prob_cut_50 拆解說明時，視為合理的高信心定價，正常寫入快取而不會
    被防禦閘門誤擋——確保修正 bug 用的新 ladder 拆解欄位不會被閘門自己吃掉。"""
    from services.calendar_service import calendar_service
    import config

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "probability": 0.05,
            "prob_hike": 0.0,
            "prob_maintain": 0.0,
            "prob_cut": 100.0,
            "prob_cut_25": 60.0,
            "prob_cut_50": 40.0,
            "ladder_saturated": True,
            "meeting_date": "09/16",
            "source": "CME 30-Day Fed Funds Futures (ZQ)",
        },
    }

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient.get", return_value=mock_resp),
        patch("database.cache.save_kv_cache") as mock_save,
        patch("database.connection.execute_write_async", new_callable=AsyncMock),
    ):
        await calendar_service.update_fedwatch_probability()
        saved_keys = [call.args[0] for call in mock_save.call_args_list]
        assert "macro_fedwatch_probability" in saved_keys
        fallback_calls = [
            call.args[1]
            for call in mock_save.call_args_list
            if call.args[0] == "macro_fedwatch_is_fallback"
        ]
        assert fallback_calls[-1] == 0


@pytest.mark.asyncio
async def test_calendar_service_cpi_deviation_happy_path() -> None:
    """測試 calendar_service.update_cpi_deviation 成功取得最新 CPI YoY 實際值/預測值時的寫入行為"""
    from services.calendar_service import calendar_service

    with (
        patch.object(
            calendar_service, "prefetch_monthly_macro_cache", new_callable=AsyncMock
        ),
        patch.object(
            calendar_service, "_ensure_macro_month_cached", new_callable=AsyncMock
        ),
        patch(
            "database.calendar_cache.get_latest_released_economic_event",
            return_value={"actual_value": 3.2, "consensus_value": "3.1"},
        ),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_cpi_deviation()

    saved = {call.args[0]: call.args[1] for call in mock_save.call_args_list}
    assert saved["macro_cpi_actual"] == 3.2
    assert saved["macro_cpi_expected"] == 3.1
    assert saved["macro_cpi_is_fallback"] == 0


@pytest.mark.asyncio
async def test_calendar_service_cpi_deviation_sanity_rejection() -> None:
    """測試 CPI YoY 數值超出合理區間 (-2% ~ 15%) 時觸發防禦阻斷"""
    from services.calendar_service import calendar_service

    with (
        patch.object(
            calendar_service, "prefetch_monthly_macro_cache", new_callable=AsyncMock
        ),
        patch.object(
            calendar_service, "_ensure_macro_month_cached", new_callable=AsyncMock
        ),
        patch(
            "database.calendar_cache.get_latest_released_economic_event",
            return_value={"actual_value": 99.0, "consensus_value": "3.1"},
        ),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_cpi_deviation()

    saved = {call.args[0]: call.args[1] for call in mock_save.call_args_list}
    assert saved.get("macro_cpi_is_fallback") == 1
    assert "macro_cpi_actual" not in saved
    assert "macro_cpi_expected" not in saved


@pytest.mark.asyncio
async def test_calendar_service_cpi_deviation_no_data_fallback() -> None:
    """測試行事曆快取中尚無已公布 CPI YoY 資料時的 fallback 行為"""
    from services.calendar_service import calendar_service

    with (
        patch.object(
            calendar_service, "prefetch_monthly_macro_cache", new_callable=AsyncMock
        ),
        patch.object(
            calendar_service, "_ensure_macro_month_cached", new_callable=AsyncMock
        ),
        patch(
            "database.calendar_cache.get_latest_released_economic_event",
            return_value=None,
        ),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_cpi_deviation()

    saved = {call.args[0]: call.args[1] for call in mock_save.call_args_list}
    assert saved.get("macro_cpi_is_fallback") == 1
    assert "macro_cpi_actual" not in saved
    assert "macro_cpi_expected" not in saved


@pytest.mark.asyncio
async def test_calendar_service_cpi_deviation_ensures_previous_month_cached() -> None:
    """回歸測試：月初到當月 CPI 公布前這段期間（或全新資料庫冷啟動），
    最新一期已公布 CPI YoY 只會落在「上個月」的行事曆快取裡。
    update_cpi_deviation() 必須主動確保上個月也已快取，而不是被動依賴
    /calendar 等其他功能過去是否曾經順帶快取過該月份——否則會誤判為
    「尚無最新公布數據」即使 TradingView 上其實有資料。"""
    from datetime import date, timedelta
    from services.calendar_service import calendar_service

    today = date.today()
    expected_previous_month_key = (
        date(today.year, today.month, 1) - timedelta(days=1)
    ).strftime("%Y-%m")

    with (
        patch.object(
            calendar_service, "prefetch_monthly_macro_cache", new_callable=AsyncMock
        ),
        patch.object(
            calendar_service,
            "_ensure_macro_month_cached",
            new_callable=AsyncMock,
        ) as mock_ensure_cached,
        patch(
            "database.calendar_cache.get_latest_released_economic_event",
            return_value={"actual_value": 3.4, "consensus_value": "3.4"},
        ),
        patch("database.cache.save_kv_cache") as mock_save,
    ):
        await calendar_service.update_cpi_deviation()

    mock_ensure_cached.assert_awaited_once_with(expected_previous_month_key)
    saved = {call.args[0]: call.args[1] for call in mock_save.call_args_list}
    assert saved["macro_cpi_actual"] == 3.4
    assert saved["macro_cpi_is_fallback"] == 0


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


def test_evaluate_escape_window_regime_unknown_gamma_is_not_scored() -> None:
    """Gamma Flip 未知 (None，例如大盤 GEX 快取過期) 時不計入收縮也不計入寬鬆。
    回歸：2026-10 GEX 快取停在 24 天前，舊 Flip 被判成負 Gamma，把窗口誤推為前移。"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    common: dict[str, Any] = {
        "prob": 0.6385,
        "cpi_dev": 0.0,
        "wti": 92.57,
        "vts_ratio": 0.888,
    }
    t_unknown, e_unknown, direction, shift, *_ = evaluate_escape_window_regime(
        **common, is_negative_gamma=None
    )
    assert (t_unknown, e_unknown) == (1, 1)
    assert direction == "維持"
    assert shift == 0

    t_neg, e_neg, direction_neg, *_ = evaluate_escape_window_regime(
        **common, is_negative_gamma=True
    )
    assert (t_neg, e_neg) == (2, 1)
    assert direction_neg == "前移"

    t_pos, e_pos, *_ = evaluate_escape_window_regime(**common, is_negative_gamma=False)
    assert (t_pos, e_pos) == (1, 2)


def _overview_patches(
    kv: dict[str, Any], ages: dict[str, float], core: dict[str, Any]
) -> list[Any]:
    return [
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch("database.get_kv_cache", side_effect=lambda key: kv.get(key)),
        patch(
            "database.cache.get_kv_cache_many",
            side_effect=lambda keys: {
                k: (kv.get(k), ages[k]) for k in keys if k in ages
            },
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            side_effect=Exception("offline"),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value=core,
        ),
    ]


_STALE_OVERVIEW_KV: dict[str, Any] = {
    "macro_spx": 7666.45,
    "macro_spy_spot": 763.99,
    "macro_spy_gamma_flip": 769.98,
    "macro_gamma_flip_line": 7699.8,
    "macro_vix": 16.38,
    "macro_us10y": 5.24,
    "macro_wti": 92.57,
    "macro_vts_ratio": 0.888,
    "macro_rrp": 0.2,
    "macro_fed_balance": 6.76,
    "macro_fear_greed": 65.0,
    "macro_uer": 4.1,
    "macro_sahm_rule": -0.03,
    "macro_gex_is_fallback": 1,
}


@pytest.mark.asyncio
async def test_macro_overview_expired_caches_are_not_used_as_live() -> None:
    """GEX 快取逾 3 天：Flip 仍顯示但不納入判定；核心指標逾 3 天：觸發即時重抓。"""
    from contextlib import ExitStack

    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
    from cogs.unified_terminal.utils import get_macro_overview_data

    day = 86400.0
    ages = {
        "macro_spy_gamma_flip": 24 * day,
        "macro_vts_ratio": 73 * day,
        "macro_rrp": 48 * day,
        "macro_fed_balance": 48 * day,
        "macro_fear_greed": 48 * day,
        "macro_uer": 48 * day,
        "macro_sahm_rule": 48 * day,
    }
    live_core = {
        "rrp": 0.3,
        "rrp_change_30d": 10.4,
        "fed_balance": 6.74,
        "uer": 4.1,
        "sahm_rule": -0.07,
        "fear_greed": 28.0,
    }
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(p)
            for p in _overview_patches(_STALE_OVERVIEW_KV, ages, live_core)
        ]
        data = await get_macro_overview_data(4242)
    mocks[-1].assert_awaited_once()

    assert data["gex_is_expired"] is True
    assert data["spy_gamma_flip"] == 769.98  # 仍顯示
    assert data["short_gamma_critical"] is False
    # 舊 Flip (769.98 > SPY 763.99) 不得再把窗口判成「前移」
    assert data["escape_window_direction"] == "維持"
    assert data["fear_greed"] == 28.0
    assert data["rrp_change_30d"] == 10.4
    assert data["core_is_expired"] is False

    embed = build_market_macro_overview_embed(data)
    text = "\n".join(str(f.value) for f in embed.fields)
    assert "[24.0 天前快取・不納入判定]" in text
    assert "未知 (GEX 快取過期" in text
    assert "即時抓取失敗" not in text


@pytest.mark.asyncio
async def test_macro_overview_discloses_expired_core_when_refetch_fails() -> None:
    from contextlib import ExitStack

    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
    from cogs.unified_terminal.utils import get_macro_overview_data

    ages = {
        k: 48 * 86400.0
        for k in (
            "macro_rrp",
            "macro_fed_balance",
            "macro_fear_greed",
            "macro_uer",
            "macro_sahm_rule",
        )
    }
    with ExitStack() as stack:
        for p in _overview_patches(
            _STALE_OVERVIEW_KV, ages, {"rrp": None, "_is_fallback": True}
        ):
            stack.enter_context(p)
        data = await get_macro_overview_data(4243)

    assert data["fear_greed"] == 65.0  # 沿用快取
    assert data["core_is_expired"] is True
    embed = build_market_macro_overview_embed(data)
    text = "\n".join(str(f.value) for f in embed.fields)
    assert "失業率為 48.0 天前快取（即時抓取失敗）" in text


@pytest.mark.asyncio
async def test_macro_overview_fresh_caches_skip_core_refetch() -> None:
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    ages = {
        k: 600.0
        for k in (
            "macro_spy_gamma_flip",
            "macro_vts_ratio",
            "macro_rrp",
            "macro_fed_balance",
            "macro_fear_greed",
            "macro_uer",
            "macro_sahm_rule",
        )
    }
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(p)
            for p in _overview_patches(_STALE_OVERVIEW_KV, ages, {})
        ]
        data = await get_macro_overview_data(4244)
    mocks[-1].assert_not_awaited()
    assert data["gex_is_expired"] is False
    assert data["core_is_expired"] is False
    # 新鮮的 Flip 769.98 > SPY 763.99 → 負 Gamma 計入收縮 (WTI>85 + 負 Gamma = 2)
    assert data["escape_window_direction"] == "前移"


@pytest.mark.asyncio
async def test_macro_overview_flip_line_uses_actual_spx_spy_ratio() -> None:
    """SPX 尺度翻轉線改用實際 SPX/SPY 比值換算（約 10.03），不再固定 ×10。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    ages = {"macro_spy_gamma_flip": 600.0}
    with ExitStack() as stack:
        for p in _overview_patches(_STALE_OVERVIEW_KV, ages, {}):
            stack.enter_context(p)
        data = await get_macro_overview_data(4245)

    expected = round(769.98 * (7666.45 / 763.99), 2)
    assert data["gamma_flip_line"] == pytest.approx(expected)
    assert data["gamma_flip_line"] > 7699.8  # 舊 ×10 換算會低估約 27 點


def test_evaluate_macro_top_escape_score_matrix() -> None:
    """測試宏觀逃頂綜合評分 (獨立於 evaluate_escape_window_regime 的四因子矩陣，
    額外疊加 Fear & Greed 與可選的衛星持倉亢奮廣度)"""
    from market_analysis.index_microstructure import evaluate_macro_top_escape_score

    # 1. 全數未觸發 (4 因子模式，未傳入 satellite_euphoria_ratio) -> NORMAL
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=0.85,
        fear_greed=48.0,
        prob=0.50,
        is_negative_gamma=False,
    )
    assert score == 0
    assert tier == "NORMAL"
    assert "常態" in tier_title
    assert len(factors) == 4  # 未傳入第 5 因子時只評 4 項

    # 2. 恰好 1 項觸發 (僅極度貪婪) -> WATCH
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=0.85,
        fear_greed=80.0,
        prob=0.50,
        is_negative_gamma=False,
    )
    assert score == 1
    assert tier == "WATCH"
    assert "前哨觀察" in tier_title

    # 3. 恰好 2 項觸發 (VTS 逆價差 + 極度貪婪) -> ELEVATED
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=1.05,
        fear_greed=80.0,
        prob=0.50,
        is_negative_gamma=False,
    )
    assert score == 2
    assert tier == "ELEVATED"
    assert "逃頂警戒" in tier_title

    # 4. 3 項觸發 (VTS + 貪婪 + 鷹派) -> CRITICAL
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=1.05,
        fear_greed=80.0,
        prob=0.75,
        is_negative_gamma=False,
    )
    assert score == 3
    assert tier == "CRITICAL"
    assert "逃頂確認" in tier_title

    # 5. 全數 4 項觸發 (未含第 5 因子) 仍為 CRITICAL (門檻為 >= 3 分)
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=1.05,
        fear_greed=80.0,
        prob=0.75,
        is_negative_gamma=True,
    )
    assert score == 4
    assert tier == "CRITICAL"

    # 6. 帶入第 5 因子 (衛星持倉亢奮廣度) 且觸發 -> 因子明細應多一筆，且可單獨
    # 把 WATCH (1 項既有觸發) 推升至 ELEVATED (2 項觸發)
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=0.85,
        fear_greed=80.0,
        prob=0.50,
        is_negative_gamma=False,
        satellite_euphoria_ratio=0.6,
    )
    assert score == 2
    assert tier == "ELEVATED"
    assert len(factors) == 5  # 傳入第 5 因子時應評 5 項

    # 7. 第 5 因子未觸發 (廣度不足 0.5) 時不加分，因子明細仍多一筆
    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=0.85,
        fear_greed=48.0,
        prob=0.50,
        is_negative_gamma=False,
        satellite_euphoria_ratio=0.2,
    )
    assert score == 0
    assert tier == "NORMAL"
    assert len(factors) == 5


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


@pytest.mark.parametrize(
    "gex_profile",
    [
        {"90": 10.0, "95": 20.0, "100": 30.0},
        {"90": -10.0, "95": -20.0, "100": -5.0},
    ],
    ids=["all_positive", "all_negative"],
)
def test_estimate_symbol_gamma_flip_no_crossing(gex_profile: dict) -> None:
    """全數同號 GEX (無負轉正交叉點) -> 回傳 0.0"""
    assert estimate_symbol_gamma_flip(gex_profile, spot=95.0) == 0.0


def test_estimate_symbol_gamma_flip_crossing_outside_bracket_returns_zero() -> None:
    """交叉點存在但落在 spot ± 30% bracket 之外 -> 視為無法採信，回傳 0.0。"""
    gex_profile = {"50": -100.0, "100": -50.0, "200": 300.0}
    # 累積: 50 -> -100 (負) ; 100 -> -150 (仍負) ; 200 -> +150 (交叉點，但 200 遠超出 bracket)
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 0.0


def test_estimate_symbol_gamma_flip_crossing_at_bracket_boundary() -> None:
    """交叉點恰好落在 bracket 邊界 (spot*1.3) 上 -> 應視為在 bracket 內，正常回傳。"""
    gex_profile = {"50.0": -100.0, "130.0": 100.0}
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 130.0


def test_estimate_symbol_gamma_flip_picks_crossing_closest_to_spot() -> None:
    """現價附近若有多次正負交錯，應回傳離現價最近的交叉點，而非掃描到的
    第一個交叉點——否則可能回傳遠離現價、與 Net GEX Regime 矛盾的失真翻轉線
    (真實案例：SPCX 現價 $147.95，舊邏輯回傳 $170.00 的失真翻轉線，
    但現價附近 $146~$150 早已存在真正的正負交錯區間)。"""
    gex_profile = {
        "60": -500.0,
        "70": 600.0,  # 累積轉正 (+100)，第一個交叉點，但離現價 100 很遠 (距離 30)
        "90": -50.0,  # 累積轉為 +50，仍為正
        "95": -70.0,  # 累積轉負 (-20)
        "99": 30.0,  # 累積轉正 (+10)，第二個交叉點，離現價僅 1
        "110": 5.0,
    }
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 99.0


def test_estimate_symbol_gamma_flip_returns_zero_when_only_crossing_contradicts_long_gamma() -> (
    None
):
    """修正後演算法改採逐履約價符號變化偵測（非累積和），GEX(K=60)=-1000 (負)、
    GEX(K=90)=+500 (正)，於 K=90 存在真實的負轉正交叉點。
    K=90 落在 bracket [70,130] 內，且 total_gex=+100 > 0（LONG_GAMMA）要求
    候選 <= spot(100)，90 <= 100 ✓，正確回傳 90.0。
    舊測試期待 0.0 係因累積算法把交叉點定位在 K=120（累積值才轉正），
    120 > spot 被方向性過濾清空，但這是舊演算法的 Bug，非正確行為。"""
    gex_profile = {"60": -1000.0, "90": 500.0, "120": 600.0}
    # Per-strike: GEX(60)=-1000 (neg) → GEX(90)=+500 (pos) → crossing at K=90
    # total_gex = +100 > 0 (LONG_GAMMA), filter s<=100: 90 ✓ → return 90.0
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 90.0


def test_estimate_symbol_gamma_flip_returns_zero_when_only_crossing_contradicts_short_gamma() -> (
    None
):
    """對稱情境：Net GEX 為負 (SHORT_GAMMA)，但 bracket 內唯一的負轉正交叉點
    卻落在現價之下——理應要求交叉點 >= spot 才與 SHORT_GAMMA 一致，方向不符
    應剔除並回傳 0.0。"""
    gex_profile = {"50": -1000.0, "80": 1200.0, "130": -700.0}
    # 累積: 50 -> -1000 (負) ; 80 -> +200 (交叉點，但 80 < spot) ; 130 -> -500 (再度轉負)
    # 最終累積 (=net_gex) = -500 < 0 (SHORT_GAMMA)，理應要求交叉點 >= spot
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 0.0


def test_estimate_symbol_gamma_flip_no_bracket_restriction_when_spot_non_positive() -> (
    None
):
    """spot<=0 時無法定義合理 bracket，應退回不限制 bracket 的既有行為。"""
    gex_profile = {"50": -100.0, "100": -50.0, "200": 300.0}
    assert estimate_symbol_gamma_flip(gex_profile, spot=0.0) == 200.0


def test_estimate_symbol_gamma_flip_empty_profile_returns_zero() -> None:
    assert estimate_symbol_gamma_flip({}, spot=100.0) == 0.0
    assert estimate_symbol_gamma_flip(None, spot=100.0) == 0.0  # type: ignore[arg-type]


def test_estimate_symbol_gamma_flip_malformed_profile_returns_zero() -> None:
    """履約價/GEX 值非數值格式 -> fail-safe 回傳 0.0，不拋例外"""
    gex_profile = {"not_a_strike": "not_a_number"}
    assert estimate_symbol_gamma_flip(gex_profile, spot=100.0) == 0.0


# ---------------------------------------------------------------------------
# ISSUE-4.1 修復驗證：逐履約價符號變化演算法（三大場景）
# ---------------------------------------------------------------------------


def test_estimate_symbol_gamma_flip_real_market_long_gamma_returns_nonzero() -> None:
    """[ISSUE-4.1 修復] 真實市場 LONG_GAMMA 場景：低履約價 Put 主導（負 GEX），
    高履約價 Call 主導（正 GEX），中間存在真實符號翻轉點。
    修復前：舊累積算法把交叉點定位在現價以上，被方向性過濾清空 → 恆回傳 0.0；
    修復後：逐履約價偵測在 K=150 正確識別符號翻轉（-500K → +1M），回傳 150.0。"""
    # 模擬真實市場：現價 $200，下方 Put 負 GEX，上方 Call 正 GEX
    gex_profile = {
        "100": -800_000.0,  # 深價外 Put → 負 GEX
        "130": -500_000.0,  # 價外 Put → 負 GEX
        "150": 1_000_000.0,  # 接近 ATM → 正 GEX（LONG_GAMMA 翻轉點）
        "170": 1_500_000.0,  # Call 主導 → 正 GEX
        "200": 500_000.0,  # 接近現價 → 正 GEX
    }
    # Per-strike: (130,-500K)→(150,+1M): 符號翻轉。total_gex = +1.7M > 0 (LONG_GAMMA)
    # 150 ∈ bracket [140, 260], 150 <= spot(200) ✓ → 回傳 150.0
    result = estimate_symbol_gamma_flip(gex_profile, spot=200.0)
    assert result > 0.0, f"修復後應回傳非零 Gamma Flip，實際回傳: {result}"
    assert result == 150.0


def test_estimate_symbol_gamma_flip_all_positive_gex_returns_zero() -> None:
    """[ISSUE-4.1 修復] 全鏈 Net GEX 全正（無零交叉點）→ 回傳 0.0。
    場景：市場完全處於正 Gamma 自穩定區間，無做市商 Gamma 方向翻轉點，
    呼叫端應啟動 Fallback 替代方案（VWAP + 0.5×ATR₁₅ₘ），不應收到
    誤導性的非零 Flip 值。"""
    # 全部履約價 GEX 均為正值 → 無任何相鄰對存在 neg→pos 符號翻轉
    gex_profile = {
        "90": 100_000.0,
        "100": 500_000.0,
        "110": 1_000_000.0,
        "120": 800_000.0,
    }
    assert estimate_symbol_gamma_flip(gex_profile, spot=105.0) == 0.0


def test_estimate_symbol_gamma_flip_all_negative_gex_returns_zero() -> None:
    """[ISSUE-4.1 修復] 全鏈 Net GEX 全負（無零交叉點）→ 回傳 0.0。
    場景：做市商全鏈均處於負 Gamma 泥淖（SHORT_GAMMA 全域），無任何正 GEX
    支撐錨點，函數無法估算翻轉線；呼叫端（opportunity_cost.py）應確認處於
    全域 Short Gamma 泥淖，拒絕進場而非誤報一個虛假的翻轉價位。"""
    # 全部履約價 GEX 均為負值 → 無任何相鄰對存在 neg→pos 符號翻轉
    gex_profile = {
        "90": -200_000.0,
        "100": -800_000.0,
        "110": -1_200_000.0,
        "120": -400_000.0,
    }
    assert estimate_symbol_gamma_flip(gex_profile, spot=105.0) == 0.0


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

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=edge_payload,
        ),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
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

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=stale_edge_payload,
        ),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await fetch_symbol_gex_metrics("AAPL")

        assert result["call_wall"] == 240.0
        assert result["put_wall"] == 220.0
        mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_symbol_gex_metrics_falls_back_when_edge_cache_older_than_tightened_threshold() -> (
    None
):
    """edge 快取新鮮度門檻已與選擇權鏈共用同一個 30 分鐘 (1800 秒) 常數
    (_EDGE_SNAPSHOT_MAX_AGE_SECONDS)，而非舊有寫死的 3600 秒。age_seconds=2400
    落在新舊門檻之間，應觸發 fallback 至即時 scrape，證明新閾值確實生效。"""
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_symbol_gex_metrics

    edge_payload_between_thresholds = {
        "data": {
            "spot": 1.0,
            "net_gex": 1.0,
            "call_wall": 1.0,
            "put_wall": 1.0,
            "gex_profile": {},
        },
        "age_seconds": 2400.0,
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

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=edge_payload_between_thresholds,
        ),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
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

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
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

    with (
        patch("database.cache.get_kv_cache", return_value=last_known_good),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
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

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await fetch_gex_metrics()

        assert result == {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}
        assert "_is_stale_cache" not in result


@pytest.mark.asyncio
async def test_fetch_gex_metrics_allow_empty_when_no_cache() -> None:
    """當 allow_empty=True 且無任何快取時，抓取失敗應回傳空 dict {}，而非使用 510/515 硬編碼預設值。"""
    import httpx
    from unittest.mock import AsyncMock
    from market_analysis.index_microstructure import fetch_gex_metrics

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await fetch_gex_metrics(allow_empty=True)
        assert result == {}


@pytest.mark.asyncio
async def test_fetch_gex_metrics_rejects_scraper_fake_fallback() -> None:
    """當 Tunnel Scraper 回傳假的靜態預設值 fallback 時，應拒絕作為即時數據，
    並回退至上次成功的快取結果。"""
    from unittest.mock import AsyncMock, MagicMock
    from market_analysis.index_microstructure import fetch_gex_metrics

    last_known_good = {
        "data": {"spy_spot": 760.0, "gamma_flip": 765.0, "put_wall": 750.0},
        "timestamp": 1234567890.0,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "spy_spot": 510.0,
            "gamma_flip": 515.0,
            "put_wall": 505.0,
            "is_fallback": True,
        },
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("database.cache.get_kv_cache", return_value=last_known_good),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await fetch_gex_metrics()
        assert result["spy_spot"] == 760.0
        assert result["gamma_flip"] == 765.0
        assert result.get("_is_stale_cache") is True


@pytest.mark.asyncio
async def test_macro_overview_data_cross_derivation_spx_spy() -> None:
    """驗證 SPX 與 SPY 現貨價同量級 cross-derivation (~7600s vs ~760s)，
    消除 SPX 7620 與 GEX Flip 5150 的歷史脫節問題。"""
    from cogs.unified_terminal.utils import get_macro_overview_data

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("database.get_kv_cache") as mock_kv,
        patch("services.market_data_service.get_quote") as mock_quote,
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
    ):
        # 模擬 SPX 抓取失敗，但 SPY 即時報價為 762.50
        def _get_quote_side_effect(symbol: str) -> Any:
            if symbol == "SPY":
                return {"c": 762.50}
            return None

        mock_quote.side_effect = _get_quote_side_effect
        mock_kv.return_value = None

        with (
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
            ) as mock_gex,
            patch(
                "market_analysis.index_microstructure.fetch_core_macro_metrics",
                new_callable=AsyncMock,
            ) as mock_core,
            patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        ):
            mock_gex.return_value = {
                "spy_spot": 762.50,
                "gamma_flip": 765.0,
                "put_wall": 755.0,
            }
            mock_core.return_value = {}

            data = await get_macro_overview_data(12345)
            # SPX 應由 SPY * 10 衍生為 7625.0，而非預設值 5150.0
            assert data["spx"] == 7625.0
            assert data["spy_spot"] == 762.50
            assert data["spy_gamma_flip"] == 765.0
            assert data["gamma_flip_line"] == 7650.0


@pytest.mark.asyncio
async def test_macro_embed_displays_fetch_failed_when_completely_no_data() -> None:
    """驗證當大盤與總經數據完全無數據時，直接顯示『獲取數據失敗』，
    不使用任何硬編碼預設值。"""
    from cogs.unified_terminal.utils import get_macro_overview_data
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("database.get_kv_cache", return_value=None),
        patch("services.market_data_service.get_quote", return_value=None),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("Failed"),
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        data = await get_macro_overview_data(123456)
        assert data["spx"] is None
        assert data["gamma_flip_line"] is None
        assert data["spy_gamma_flip"] is None

        # 驗證 build_market_macro_overview_embed 渲染顯示獲取數據失敗
        embed = build_market_macro_overview_embed(data)
        # 尋找描述或欄位中是否包含「獲取數據失敗」
        found_failure = False
        if embed.description and "獲取數據失敗" in embed.description:
            found_failure = True
        for field in embed.fields:
            if field.value and "獲取數據失敗" in field.value:
                found_failure = True
                break
        assert found_failure is True


def test_interpolate_gamma_flip_zero_audit_example() -> None:
    """稽查實例：閘門 Flip 為 $227.50，相鄰履約價內插零軸 ≈ $225.18。"""
    profile = {"222.5": 3_000_000.0, "225.0": -1_434_309.0, "227.5": 18_108_163.0}
    assert index_microstructure.estimate_symbol_gamma_flip(profile, 228.0) == 227.5
    zero = index_microstructure.interpolate_gamma_flip_zero(profile, 227.5)
    assert zero == pytest.approx(225.1835, abs=1e-3)


@pytest.mark.parametrize(
    "profile, flip",
    [
        ({"100.0": 5.0, "105.0": 10.0}, 105.0),  # 前一檔不為負
        ({"100.0": 5.0}, 100.0),  # 沒有前一檔
        ({"100.0": -5.0, "105.0": 10.0}, 110.0),  # flip 不在 profile 中
        ({}, 100.0),
        ({"100.0": -5.0, "105.0": 10.0}, 0.0),
        ({"bad": "x"}, 100.0),
    ],
)
def test_interpolate_gamma_flip_zero_invalid_returns_zero(
    profile: dict, flip: float
) -> None:
    assert index_microstructure.interpolate_gamma_flip_zero(profile, flip) == 0.0


def test_dynamic_escape_window_drivers() -> None:
    """測試逃頂窗口收縮歸因動態驅動因子 (油價、通膨、期限結構倒掛、負Gamma、高利率)。"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    # Case 1: WTI 高油價 (90.0) + 負 Gamma (True)，利率中性 (prob=0.55) -> (高油價+結構承壓)
    t_score, _, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.55,
        cpi_dev=0.0,
        wti=90.0,
        vts_ratio=0.85,
        is_negative_gamma=True,
    )
    assert direction == "前移"
    assert "收縮警戒" in tier
    assert "高油價+結構承壓" in status
    assert "高利率" not in status

    # Case 2: 鷹派利率 (prob=0.90) + 通膨升溫 (cpi_dev=0.4) -> (高利率+通膨升溫)
    t_score, _, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.90,
        cpi_dev=0.4,
        wti=75.0,
        vts_ratio=0.85,
        is_negative_gamma=False,
    )
    assert direction == "前移"
    assert "高利率+通膨升溫" in status

    # Case 3: 波動倒掛 (vts_ratio=1.1) + 負 Gamma (True)，利率溫和 (prob=0.50) -> (波動倒掛+結構承壓)
    t_score, _, direction, shift, tier, status = evaluate_escape_window_regime(
        prob=0.50,
        cpi_dev=0.0,
        wti=75.0,
        vts_ratio=1.1,
        is_negative_gamma=True,
    )
    assert direction == "前移"
    assert "波動倒掛+結構承壓" in status


def test_estimate_symbol_gamma_flip_bracket_pct() -> None:
    """測試 estimate_symbol_gamma_flip 的 bracket_pct 參數限制。"""
    from market_analysis.index_microstructure import estimate_symbol_gamma_flip

    spot = 770.0
    # 建立一個 profile (SHORT_GAMMA, total_gex < 0, 允許 flip >= spot)：
    # 850 (-10), 870 (+20) -> 遠端交叉點在 870 (距離 spot 770 為 +13%，超出 8% 但在 30% 內)
    profile_noise: dict[str, float] = {
        "700.0": -50.0,
        "850.0": -10.0,
        "870.0": 20.0,
    }
    # 預設 bracket_pct=0.30: 870 在 [770*0.7=539, 770*1.3=1001] 內 -> 回傳 870.0
    flip_wide = estimate_symbol_gamma_flip(profile_noise, spot, bracket_pct=0.30)
    assert flip_wide == 870.0

    # bracket_pct=0.08: bracket_high 為 770*1.08 = 831.6，870 超出區間 -> 無候選，回傳 0.0
    flip_narrow = estimate_symbol_gamma_flip(profile_noise, spot, bracket_pct=0.08)
    assert flip_narrow == 0.0


_OUTLIER_OVERVIEW_KV: dict[str, Any] = {
    "macro_spx": 7748.0,
    "macro_spy_spot": 774.8,
    # 修正前寫入的離群 Flip：高於現價 22.5%，超出 edge 搜尋區間（+20%）
    "macro_spy_gamma_flip": 948.9,
    "macro_gamma_flip_line": 9489.0,
    "macro_gex_is_fallback": 0,
    "macro_vix": 16.0,
    "macro_vts_ratio": 0.85,
    "macro_rrp": 1.0,
    "macro_fed_balance": 6.7,
    "macro_fear_greed": 50.0,
}
_FRESH_OVERVIEW_AGES: dict[str, float] = {
    k: 600.0
    for k in (
        "macro_spy_gamma_flip",
        "macro_vts_ratio",
        "macro_rrp",
        "macro_fed_balance",
        "macro_fear_greed",
        "macro_uer",
        "macro_sahm_rule",
    )
}


@pytest.mark.asyncio
async def test_macro_gamma_flip_sanity_gate() -> None:
    """KV 內的離群 Flip（948.9 vs SPY 774.8）不可沿用：走自癒路徑以 SPY 個股
    期權鏈重估；自癒成功時採用新值，而不是單純變成 None。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    mock_spy_gex = {
        "spot": 774.8,
        # SHORT_GAMMA (total < 0)，負轉正交叉在 777.0
        "gex_profile": {"770.0": -200.0, "777.0": 50.0},
    }
    with ExitStack() as stack:
        for p in _overview_patches(_OUTLIER_OVERVIEW_KV, _FRESH_OVERVIEW_AGES, {}):
            stack.enter_context(p)
        mock_macro = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
                return_value=mock_spy_gex,
            )
        )
        overview = await get_macro_overview_data(123456)

    mock_macro.assert_awaited_once()  # KV 離群 -> 觸發自癒
    assert overview["spy_gamma_flip"] == 777.0
    assert overview["gamma_flip_line"] == pytest.approx(7770.0)
    assert overview["gex_is_expired"] is False


@pytest.mark.asyncio
async def test_macro_gamma_flip_sanity_gate_heal_fails_degrades_to_none() -> None:
    """KV 離群且自癒（大盤端點與 SPY 個股估算）皆失敗時，Flip 視為未知 (None)。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    with ExitStack() as stack:
        for p in _overview_patches(_OUTLIER_OVERVIEW_KV, _FRESH_OVERVIEW_AGES, {}):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
                return_value={"spot": 0.0, "gex_profile": {}},
            )
        )
        overview = await get_macro_overview_data(123457)

    assert overview["spy_gamma_flip"] is None
    assert overview["gamma_flip_line"] is None
    assert overview["short_gamma_critical"] is False


@pytest.mark.asyncio
async def test_macro_overview_crash_keeps_far_above_flip() -> None:
    """SPY 急跌 12%：真實 Flip 高於現價約 12%（short gamma 方向）不得被當成
    雜訊丟棄，short_gamma_critical 必須成立。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    kv = {
        **_OUTLIER_OVERVIEW_KV,
        "macro_spx": 6160.0,
        "macro_spy_spot": 616.0,  # 自 700 急跌 12%
        "macro_spy_gamma_flip": 690.0,  # 高於現價 12.0%
        "macro_gamma_flip_line": 6900.0,
        "macro_vix": 38.0,
        "macro_vts_ratio": 1.12,
    }
    with ExitStack() as stack:
        for p in _overview_patches(kv, _FRESH_OVERVIEW_AGES, {}):
            stack.enter_context(p)
        mock_macro = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={},
            )
        )
        overview = await get_macro_overview_data(123458)

    mock_macro.assert_not_awaited()  # 合法值，不需自癒
    assert overview["spy_gamma_flip"] == 690.0
    assert overview["gamma_flip_line"] is not None
    assert overview["short_gamma_critical"] is True


def test_safety_payout_threshold_rrp_material_balance() -> None:
    """測試 RRP 極低水位 (<$20B) 下，小基數除零引起的巨幅百分比變動不誤觸 $18,000。"""
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with (
        patch("database.get_kv_cache") as mock_kv,
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
    ):
        # Case 1: RRP = 1.0B (極低水位), 30d 變動 = +402.0% -> 不觸發 $18,000，回歸基準 $13,000
        mock_kv.side_effect = lambda key: {
            "macro_rrp": 1.0,
            "macro_rrp_change_30d": 402.0,
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # Case 2: RRP = 50.0B (實質規模 >= $20B), 30d 變動 = +25.0% -> 觸發最高戒備 $18,000
        mock_kv.side_effect = lambda key: {
            "macro_rrp": 50.0,
            "macro_rrp_change_30d": 25.0,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 3: RRP 剛好 $20B 視為實質規模
        mock_kv.side_effect = lambda key: {
            "macro_rrp": 20.0,
            "macro_rrp_change_30d": 25.0,
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # Case 4: RRP 餘額未知 -> 保守視為實質規模，不因缺值放寬紅線
        mock_kv.side_effect = lambda key: {"macro_rrp_change_30d": 25.0}.get(key)
        assert get_safety_payout_threshold() == 18000.0


def test_market_embed_escape_window_ansi_coloring() -> None:
    """測試 build_market_macro_overview_embed 的逃頂窗口 ANSI 色碼包裝。"""
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    # Case 1: 前移收縮警戒 -> 紅色 \u001b[1;31m
    macro_data_contract: dict[str, Any] = {
        "spx": 7800.0,
        "vix": 16.0,
        "us10y": 4.5,
        "gamma_flip_line": 7750.0,
        "escape_win_status": "⚠️ 前移 5 天 (高油價+結構承壓)",
        "payout_threshold": 13000.0,
    }
    embed_contract = build_market_macro_overview_embed(macro_data_contract)
    risk_field = next(
        str(f.value)
        for f in embed_contract.fields
        if f.name and "聯動風控引擎狀態" in f.name
    )
    assert "\u001b[1;31m⚠️ 前移 5 天 (高油價+結構承壓)\u001b[0m" in risk_field

    # Case 2: 後推寬鬆擴張 -> 綠色 \u001b[1;32m
    macro_data_expand: dict[str, Any] = {
        "spx": 7800.0,
        "vix": 16.0,
        "us10y": 4.5,
        "gamma_flip_line": 7750.0,
        "escape_win_status": "🟢 後推 5 天 (流動性擴張)",
        "payout_threshold": 13000.0,
    }
    embed_expand = build_market_macro_overview_embed(macro_data_expand)
    risk_field_expand = next(
        str(f.value)
        for f in embed_expand.fields
        if f.name and "聯動風控引擎狀態" in f.name
    )
    assert "\u001b[1;32m🟢 後推 5 天 (流動性擴張)\u001b[0m" in risk_field_expand


@pytest.mark.asyncio
async def test_macro_gamma_flip_outlier_raw_flip_triggers_symbol_fallback() -> None:
    """測試當 macro GEX 回傳離群雜訊（Flip = 948.9，高於現貨 22%），get_macro_overview_data
    能自動辨識 raw_flip 異常、觸發 SPY 個股期權鏈即時備援並成功自癒（回傳有效值 777.0）。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    kv = {
        **_OUTLIER_OVERVIEW_KV,
        "macro_spx": 7750.0,
        "macro_spy_spot": 775.0,
        "macro_spy_gamma_flip": None,
        "macro_gamma_flip_line": None,
    }
    # macro GEX 回傳偏離現貨超出合理區間的雜訊
    mock_outlier_macro_gex = {"spy_spot": 775.0, "gamma_flip": 948.9}
    # SPY 個股期權鏈回傳正常結構 (SHORT_GAMMA, total_gex < 0, 翻轉線在 777.0)
    mock_spy_gex = {
        "spot": 775.0,
        "gex_profile": {"770.0": -200.0, "777.0": 50.0},
    }
    with ExitStack() as stack:
        for p in _overview_patches(kv, {"macro_vts_ratio": 10.0}, {}):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value=mock_outlier_macro_gex,
            )
        )
        mock_symbol = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
                return_value=mock_spy_gex,
            )
        )
        overview = await get_macro_overview_data(123459)

    mock_symbol.assert_awaited_once()
    # 成功避開 948.9 雜訊，自癒回傳 SPY Flip 777.0
    assert overview["spy_gamma_flip"] == 777.0
    assert overview["gamma_flip_line"] is not None
    assert 7700.0 <= overview["gamma_flip_line"] <= 7800.0


@pytest.mark.asyncio
async def test_macro_overview_heal_ignores_fallback_flagged_payload() -> None:
    """自癒時大盤 GEX 帶 is_fallback 旗標（不比對 510/515 魔術數字）即改走 SPY
    個股估算；SPY 真的在 515 附近時，合法的 Flip 515 不會被誤丟。"""
    from contextlib import ExitStack

    from cogs.unified_terminal.utils import get_macro_overview_data

    kv = {
        **_OUTLIER_OVERVIEW_KV,
        "macro_spx": 5100.0,
        "macro_spy_spot": 510.0,
        "macro_spy_gamma_flip": None,
        "macro_gamma_flip_line": None,
    }
    with ExitStack() as stack:
        for p in _overview_patches(kv, {"macro_vts_ratio": 10.0}, {}):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={"spy_spot": 510.0, "gamma_flip": 515.0},
            )
        )
        mock_symbol = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
            )
        )
        overview = await get_macro_overview_data(123460)

    mock_symbol.assert_not_awaited()
    assert overview["spy_gamma_flip"] == 515.0

    with ExitStack() as stack:
        for p in _overview_patches(kv, {"macro_vts_ratio": 10.0}, {}):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={
                    "spy_spot": 510.0,
                    "gamma_flip": 515.0,
                    "is_fallback": True,
                },
            )
        )
        mock_symbol = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
                return_value={"spot": 0.0, "gex_profile": {}},
            )
        )
        overview = await get_macro_overview_data(123461)

    mock_symbol.assert_awaited_once()
    assert overview["spy_gamma_flip"] is None


def test_safety_payout_threshold_rrp_string_type_safety() -> None:
    """測試 RRP 變動率為字串型態 (如 '402.0' 或 '0') 及 spike 為字串時不會拋出 TypeError。"""
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    with (
        patch("database.get_kv_cache") as mock_kv,
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
    ):
        # 字串型態 402.0，RRP 餘額 1.0 (極低水位) -> 不誤觸 $18,000
        mock_kv.side_effect = lambda key: {
            "macro_rrp": "1.0",
            "macro_rrp_change_30d": "402.0",
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0

        # 字串型態 25.0，RRP 餘額 50.0 (實質水位) -> 觸發 $18,000
        mock_kv.side_effect = lambda key: {
            "macro_rrp": "50.0",
            "macro_rrp_change_30d": "25.0",
        }.get(key)
        assert get_safety_payout_threshold() == 18000.0

        # 無法解析的字串視為 0%，不拋例外
        mock_kv.side_effect = lambda key: {
            "macro_rrp": "abc",
            "macro_rrp_change_30d": "n/a",
        }.get(key)
        assert get_safety_payout_threshold() == 13000.0


@pytest.mark.parametrize(
    ("kwargs", "ansi"),
    [
        # 中性平衡 -> evaluate_escape_window_regime 實際輸出「🟢 正常窗口 (...)」-> 綠色
        (
            {"prob": 0.55, "cpi_dev": 0.0, "wti": 75.0, "vts_ratio": 0.95},
            "\u001b[1;32m",
        ),
        # 前移 -> 紅色
        (
            {
                "prob": 0.90,
                "cpi_dev": 0.4,
                "wti": 75.0,
                "vts_ratio": 0.85,
                "is_negative_gamma": False,
            },
            "\u001b[1;31m",
        ),
        # 後推 -> 綠色
        (
            {
                "prob": 0.30,
                "cpi_dev": -0.1,
                "wti": 70.0,
                "vts_ratio": 0.85,
                "is_negative_gamma": False,
            },
            "\u001b[1;32m",
        ),
    ],
)
def test_market_embed_escape_window_ansi_uses_real_regime_output(
    kwargs: dict[str, Any], ansi: str
) -> None:
    """面板著色以 evaluate_escape_window_regime 的實際輸出為準（該函式不會產生
    「未知／中性／資料不足」字樣，原黃色分支為死碼已刪除）。"""
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    *_, status = evaluate_escape_window_regime(**kwargs)
    assert not any(word in status for word in ("未知", "中性", "不足"))
    embed = build_market_macro_overview_embed(
        {
            "spx": 7800.0,
            "vix": 16.0,
            "us10y": 4.5,
            "gamma_flip_line": 7750.0,
            "escape_win_status": status,
            "payout_threshold": 13000.0,
        }
    )
    risk_field = next(
        str(f.value) for f in embed.fields if f.name and "聯動風控引擎狀態" in f.name
    )
    assert f"{ansi}{status}\u001b[0m" in risk_field


@pytest.mark.asyncio
async def test_fetch_gex_metrics_outlier_rejected_from_cache() -> None:
    """測試 fetch_gex_metrics 收到爬蟲回傳超出合理區間（高於現價 >20%）的異常 Flip 時拒絕寫入快取。"""
    from unittest.mock import MagicMock
    from market_analysis.index_microstructure import fetch_gex_metrics

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "spy_spot": 774.8,
            "gamma_flip": 948.9,  # 高於現價 22.5% > 20%
            "put_wall": 700.0,
        },
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    saved_keys: dict[str, Any] = {}

    async def mock_save(key: str, val: Any) -> bool:
        saved_keys[key] = val
        return True

    with (
        patch("config.TUNNEL_URL", "https://mock-tunnel.test"),
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("database.cache.save_kv_cache", side_effect=mock_save),
        patch("database.cache.get_kv_cache", return_value=None),
    ):
        res = await fetch_gex_metrics(allow_empty=True)
        assert res == {}
        # 異常值不應寫入 macro_spy_gamma_flip
        assert "macro_spy_gamma_flip" not in saved_keys
        # 且 macro_gex_is_fallback 應被標記為 1
        assert saved_keys.get("macro_gex_is_fallback") == 1


# ---------------------------------------------------------------------------
# 大盤 Gamma Flip 非對稱合理性閘門（is_macro_gamma_flip_outlier）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flip", "spot", "expected"),
    [
        (690.0, 616.0, False),  # 急跌 12%：Flip 高於現價 12%（short gamma）-> 合法
        (739.0, 616.0, False),  # +19.97%（edge 搜尋上限內）-> 合法
        (740.0, 616.0, True),  # 超過 +20% -> 離群
        (948.9, 774.8, True),  # +22.5% -> 離群
        (713.0, 775.0, False),  # -8.0% -> 合法
        (705.0, 775.0, True),  # -9.0% -> 離群（long gamma 方向維持較嚴門檻）
        (None, 775.0, False),  # 缺值不等於離群
        (0.0, 775.0, False),
        (780.0, None, False),
        (780.0, 0.0, False),
        ("abc", 775.0, False),
        (True, 775.0, False),
    ],
)
def test_is_macro_gamma_flip_outlier_asymmetric(
    flip: Any, spot: Any, expected: bool
) -> None:
    from market_analysis.index_microstructure import is_macro_gamma_flip_outlier

    assert is_macro_gamma_flip_outlier(flip, spot) is expected


def test_macro_gamma_flip_bounds_match_edge_search_range() -> None:
    """上方容許上限須與 edge find_gamma_flip() 的搜尋區間（spot × 1.2）一致。"""
    from market_analysis.index_microstructure import (
        MACRO_GEX_FLIP_MAX_ABOVE_SPOT_PCT,
        MACRO_GEX_FLIP_MAX_BELOW_SPOT_PCT,
    )

    assert MACRO_GEX_FLIP_MAX_ABOVE_SPOT_PCT == 0.20
    assert MACRO_GEX_FLIP_MAX_BELOW_SPOT_PCT == 0.08


def test_estimate_macro_spy_gamma_flip_asymmetric_bracket() -> None:
    """SPY 備援估算：SHORT_GAMMA 崩跌時 +12% 的交叉必須保留；+25% 與 -10% 剔除。"""
    from market_analysis.index_microstructure import estimate_macro_spy_gamma_flip

    spot = 616.0
    crash_profile = {"600.0": -500.0, "680.0": -100.0, "690.0": 50.0}  # total < 0
    assert estimate_macro_spy_gamma_flip(crash_profile, spot) == 690.0
    # 舊的對稱 ±8% bracket 會把同一個合法交叉丟掉
    assert estimate_symbol_gamma_flip(crash_profile, spot, bracket_pct=0.08) == 0.0

    far_profile = {"600.0": -500.0, "760.0": -100.0, "770.0": 50.0}  # +25%
    assert estimate_macro_spy_gamma_flip(far_profile, spot) == 0.0

    # LONG_GAMMA：交叉在 -10%（554.4）-> 低於下方 8% 上限，剔除
    long_profile = {"550.0": -10.0, "554.4": 30.0, "620.0": 500.0}
    assert estimate_macro_spy_gamma_flip(long_profile, spot) == 0.0


def _mock_edge_client(payload: dict[str, Any]) -> Any:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "success", "data": payload}
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_gex_metrics_accepts_crash_flip_far_above_spot() -> None:
    """SPY 急跌 12% 時 edge 回傳的 Flip（高於現價 12%）是合法 short gamma 訊號，
    必須寫入快取並清除 fallback 旗標，不得被當成雜訊丟棄。"""
    from market_analysis.index_microstructure import fetch_gex_metrics

    saved: dict[str, Any] = {}

    async def mock_save(key: str, val: Any) -> bool:
        saved[key] = val
        return True

    payload = {"spy_spot": 616.0, "gamma_flip": 690.0, "put_wall": 600.0}
    with (
        patch("config.TUNNEL_URL", "https://mock-tunnel.test"),
        patch("httpx.AsyncClient", return_value=_mock_edge_client(payload)),
        patch("database.cache.save_kv_cache", side_effect=mock_save),
        patch("database.cache.get_kv_cache", return_value=None),
    ):
        res = await fetch_gex_metrics(allow_empty=True)

    assert res["gamma_flip"] == 690.0
    assert saved["macro_spy_gamma_flip"] == 690.0
    assert saved["macro_gamma_flip_line"] == 6900.0
    assert saved["macro_gex_is_fallback"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flip_payload", [{}, {"gamma_flip": 0.0}, {"gamma_flip": None}]
)
async def test_fetch_gex_metrics_missing_or_zero_flip_is_fallback(
    flip_payload: dict[str, Any],
) -> None:
    """edge 缺 gamma_flip 或 ≤ 0（搜尋區間內無交叉）時視為備援：不寫入預設值
    515.0 或 0.0，且 macro_gex_is_fallback 標記為 1。"""
    from market_analysis.index_microstructure import fetch_gex_metrics

    saved: dict[str, Any] = {}

    async def mock_save(key: str, val: Any) -> bool:
        saved[key] = val
        return True

    payload = {"spy_spot": 700.0, "put_wall": 680.0, **flip_payload}
    with (
        patch("config.TUNNEL_URL", "https://mock-tunnel.test"),
        patch("httpx.AsyncClient", return_value=_mock_edge_client(payload)),
        patch("database.cache.save_kv_cache", side_effect=mock_save),
        patch("database.cache.get_kv_cache", return_value=None),
    ):
        res = await fetch_gex_metrics(allow_empty=True)

    assert res == {}
    assert "macro_spy_gamma_flip" not in saved
    assert "macro_gamma_flip_line" not in saved
    assert saved.get("macro_gex_is_fallback") == 1


@pytest.mark.asyncio
async def test_fetch_gex_metrics_last_known_good_rejects_cached_outlier() -> None:
    """last-known-good 快取裡的離群 Flip（修正前寫入）不得再被回傳。"""
    import httpx

    from market_analysis.index_microstructure import fetch_gex_metrics

    cached_outlier = {
        "data": {"spy_spot": 774.8, "gamma_flip": 948.9, "put_wall": 700.0},
        "timestamp": 1234567890.0,
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("database.cache.get_kv_cache", return_value=cached_outlier),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        assert await fetch_gex_metrics(allow_empty=True) == {}


@pytest.mark.asyncio
async def test_get_market_regime_treats_outlier_flip_as_unknown() -> None:
    """get_market_regime：Flip 相對即時 SPY 離群時視為未知，不可據以判成危機。"""
    with (
        patch("services.market_data_service.get_vix_spot_strict") as mock_vix,
        patch("services.market_data_service.get_vix_term_structure") as mock_vts,
        patch("services.market_data_service.get_quote") as mock_quote,
        patch("market_analysis.index_microstructure.fetch_gex_metrics") as mock_gex,
        patch(
            "market_analysis.index_microstructure.fetch_liquidity_metrics",
            new_callable=AsyncMock,
            return_value={"ted_spread": 0.2},
        ),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
    ):
        mock_vix.return_value = 30.0
        mock_vts.return_value = {"vts_ratio": 1.1, "is_valid": True}
        mock_quote.return_value = {"c": 774.8}
        mock_gex.return_value = {"spy_spot": 774.8, "gamma_flip": 948.9}
        assert await get_market_regime() == "UNKNOWN"

    invalidate_market_regime_cache()
    with (
        patch("services.market_data_service.get_vix_spot_strict") as mock_vix,
        patch("services.market_data_service.get_vix_term_structure") as mock_vts,
        patch("services.market_data_service.get_quote") as mock_quote,
        patch("market_analysis.index_microstructure.fetch_gex_metrics") as mock_gex,
        patch(
            "market_analysis.index_microstructure.fetch_liquidity_metrics",
            new_callable=AsyncMock,
            return_value={"ted_spread": 0.2},
        ),
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
    ):
        # 急跌 12%：Flip 高於現價 12% 為合法值，危機必須成立
        mock_vix.return_value = 38.0
        mock_vts.return_value = {"vts_ratio": 1.12, "is_valid": True}
        mock_quote.return_value = {"c": 616.0}
        mock_gex.return_value = {"spy_spot": 616.0, "gamma_flip": 690.0}
        assert await get_market_regime() == "SHORT_GAMMA_CRITICAL"


def test_escape_window_attribution_matches_scoring_thresholds() -> None:
    """歸因門檻與計分一致：cpi_dev 0.2（> 0.1）會計分，也必須出現在歸因。"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    t_score, _, direction, _, _, status = evaluate_escape_window_regime(
        prob=0.90, cpi_dev=0.2, wti=75.0, vts_ratio=0.85, is_negative_gamma=False
    )
    assert t_score == 2
    assert direction == "前移"
    assert "(高利率+通膨升溫)" in status


def test_escape_window_attribution_lists_all_factors_when_t_ge_3() -> None:
    """T ≥ 3 時列出全部觸發因子；CPI 與 WTI 同屬因子 2，合併為一個標籤。"""
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    t_score, _, _, shift, _, status = evaluate_escape_window_regime(
        prob=0.90, cpi_dev=0.4, wti=95.0, vts_ratio=1.1, is_negative_gamma=True
    )
    assert t_score == 4
    assert shift == 8
    assert "(高利率+通膨油價雙升+波動倒掛+結構承壓)" in status

    t_score, _, _, _, _, status = evaluate_escape_window_regime(
        prob=0.55, cpi_dev=0.0, wti=95.0, vts_ratio=1.1, is_negative_gamma=True
    )
    assert t_score == 3
    assert "(高油價+波動倒掛+結構承壓)" in status
