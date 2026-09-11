from typing import Any
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
import pandas as pd
import pytest

from market_analysis.intraday_pipeline.events import (
    _resolve_watchlist_event_mode,
    _build_watchlist_event_summary,
)
from market_analysis.option_guidance import build_watchlist_option_plan
from market_analysis.sentiment.iv_metrics import _calculate_iv_term_structure
from risk_engine.nro import WatchlistRiskController
from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext


def _sample_metrics(**overrides: Any) -> EnhancedWatchlistMetrics:
    payload = {
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "current_price": 132.0,
        "buy_zone_status": "🟢 買點：趨勢支撐 (VIX 修正)",
        "buy_price_phase1": 130.0,
        "buy_price_phase2": 124.0,
        "buy_price_phase3": 118.0,
        "sell_zone_status": "🟢 賣點：第一壓力帶",
        "sell_price_phase1": 136.0,
        "sell_price_phase2": 142.0,
        "sell_price_phase3": 148.0,
        "pe_ratio": 42.5,
        "rsi_14": 56.4,
        "atr_14": 6.0,
        "beta": 1.4,
        "ma20": 128.0,
        "ma50": 122.0,
        "ma200": 110.0,
        "bias_ma20": 999.0,
        "iv_rank": 72.0,
        "iv_percentile": 64.0,
        "option_skew": -6.4,
        "skew_percentile": 10.0,
        "option_skew_state": "右偏 (Call 昂貴)",
        "pcr": 0.95,
        "volume_poc": 126.5,
        "gex_max_put_wall": 120.0,
        "vanna_sensitivity": 0.35,
        "relative_strength_spy": 0.08,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)  # type: ignore


def test_issue_1_5_macro_event_priority_over_distant_earnings() -> None:
    """ISSUE-1.5: 6 天後的遠期財報不應掩蓋 1 小時後的即期宏觀事件 (FOMC / CPI)"""
    # 財報 144 小時 (6 天) 後，宏觀事件 1 小時後：必須是 macro-guard 而非 earnings-guard
    mode = _resolve_watchlist_event_mode(earnings_tte_hours=144.0, macro_tte_hours=1.0)
    assert mode == "macro-guard"

    # 宏觀消化冷卻期 (-1.0 小時)，財報 120 小時後：必須維持 macro-guard
    mode_cooling = _resolve_watchlist_event_mode(
        earnings_tte_hours=120.0, macro_tte_hours=-1.0
    )
    assert mode_cooling == "macro-guard"

    # 摘要中應同時包含宏觀事件與遠期財報提示
    summary = _build_watchlist_event_summary(
        symbol="NVDA",
        earnings_date="2026-09-20",
        earnings_tte_hours=144.0,
        macro_event="FOMC 利率決策",
        macro_tte_hours=1.0,
        risk_mode=mode,
    )
    assert "FOMC 利率決策" in summary
    assert "NVDA 財報將於" in summary

    # 財報在 48 小時內 (即期)，則強制 event-lock
    lock_mode = _resolve_watchlist_event_mode(
        earnings_tte_hours=48.0, macro_tte_hours=1.0
    )
    assert lock_mode == "event-lock"


@pytest.mark.asyncio
async def test_issue_1_6_earnings_jump_risk_crosses_earnings_blocks_credit() -> None:
    """ISSUE-1.6: 候選賣方合約跨越已知財報公布日應觸發 Earnings Jump Risk WAIT 計畫"""
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    # 財報在 2026-09-25 (距今約 14 天，非 72h event-lock)，但選到的合約在 2026-10-16 到期 (跨過財報)
    event_context = WatchlistEventContext(
        earnings_date="2026-09-25",
        earnings_tte_hours=336.0,
        risk_mode="earnings-guard",
        summary="NVDA 財報倒數 14 天",
    )
    chain = type(
        "Chain",
        (),
        {
            "calls": pd.DataFrame(
                [{"strike": 135.0, "bid": 2.0, "ask": 2.2, "lastPrice": 2.1}]
            ),
            "puts": pd.DataFrame(
                [{"strike": 125.0, "bid": 2.0, "ask": 2.2, "lastPrice": 2.1}]
            ),
        },
    )()

    with patch(
        "market_analysis.strategy.find_best_contract",
        new_callable=AsyncMock,
        return_value={"strike": 125.0, "expiry": "2026-10-16", "mid": 2.1},
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ):
        plan = await build_watchlist_option_plan(
            metrics,
            tactical,
            capital=100000.0,
            risk_limit=15.0,
            event_context=event_context,
        )

    assert plan is not None
    assert "跨越財報日" in plan.strategy_name
    assert plan.suggested_contracts == 0
    assert plan.crosses_earnings is True
    assert "Earnings Jump Risk" in plan.rationale


@pytest.mark.asyncio
async def test_iss_09_term_structure_filters_0dte_and_selects_5_to_20_days() -> None:
    """ISS-09: 期限結構應過濾 0-DTE/1-DTE (0~4 天)，強制在 5~20 天取樣近月"""

    class MockChain:
        def __init__(self, calls: pd.DataFrame, puts: pd.DataFrame) -> None:
            self.calls = calls
            self.puts = puts

    calls_df = pd.DataFrame([{"strike": 100.0, "impliedVolatility": 0.30}])
    puts_df = pd.DataFrame([{"strike": 100.0, "impliedVolatility": 0.30}])

    today = datetime.now().date()
    # 構造：0-DTE (+0天), 2-DTE (+2天), 10-DTE (+10天), 35-DTE (+35天)
    exp_0dte = (today + timedelta(days=0)).strftime("%Y-%m-%d")
    exp_2dte = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    exp_10dte = (today + timedelta(days=10)).strftime("%Y-%m-%d")
    exp_35dte = (today + timedelta(days=35)).strftime("%Y-%m-%d")

    mock_expiries = [exp_0dte, exp_2dte, exp_10dte, exp_35dte]
    requested_expiries: list[str] = []

    async def mock_get_chain(symbol: str, expiry: str, **kwargs: Any) -> MockChain:
        requested_expiries.append(expiry)
        return MockChain(calls_df, puts_df)

    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=mock_expiries,
    ), patch(
        "services.market_data_service.get_option_chain",
        side_effect=mock_get_chain,
    ):
        status, ratio = await _calculate_iv_term_structure("TEST_DTE", 100.0)

    # 應選中 exp_10dte (10天) 作為近月，exp_35dte (35天) 作為遠月，徹底跳過 0-DTE 與 2-DTE
    assert requested_expiries == [exp_10dte, exp_35dte]
    assert ratio is not None
    assert abs(ratio - 1.0) < 1e-3
