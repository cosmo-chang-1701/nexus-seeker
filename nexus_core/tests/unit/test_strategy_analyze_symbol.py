"""针對 strategy.analyze_symbol() 併發重構（Fix B）的回歸測試。

這次重構把原本的序列 await 瀑布拆成 Phase 1 / Phase 2 / Phase 4 三段 asyncio.gather，
但函式中散布多個提前 return 短路點。這裡驗證兩件事：
1. 併發派發確實發生（而非退化回序列 await）。
2. 早退路徑（indicators=None、best_contract=None）的最終回傳值仍然正確，
   即使代表 Phase 4 的其他協程會被併發執行後才捨棄（刻意接受的 tradeoff）。
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis import strategy

BASE_INDICATORS = {
    "price": 105.0,
    "rsi": 55.0,
    "sma20": 100.0,
    "macd_hist": 0.5,
    "hv_current": 0.3,
    "hv_rank": 40.0,
}


def _make_df(rows: int = 60) -> pd.DataFrame:
    dates = pd.date_range(end="2026-08-19", periods=rows, freq="D")
    closes = [100.0 + i * 0.1 for i in range(rows)]
    return pd.DataFrame({"Close": closes, "Volume": [1000] * rows}, index=dates)


@pytest.mark.asyncio
async def test_analyze_symbol_phase1_dispatches_concurrently() -> None:
    """驗證 quote / is_etf / history(symbol) / history(SPY) 透過 gather 併發派發。"""
    call_order: list[str] = []

    async def _quote_effect(*_a: Any, **_k: Any) -> dict:
        call_order.append("start:quote")
        await asyncio.sleep(0)
        call_order.append("end:quote")
        return {"c": 100.0}

    async def _is_etf_effect(*_a: Any, **_k: Any) -> bool:
        call_order.append("start:is_etf")
        await asyncio.sleep(0)
        call_order.append("end:is_etf")
        return False

    async def _history_effect(symbol: str, *_a: Any, **_k: Any) -> pd.DataFrame:
        call_order.append(f"start:history_{symbol}")
        await asyncio.sleep(0)
        call_order.append(f"end:history_{symbol}")
        return _make_df()

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        side_effect=_quote_effect,
    ), patch(
        "services.market_data_service.is_etf",
        new_callable=AsyncMock,
        side_effect=_is_etf_effect,
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        side_effect=_history_effect,
    ), patch(
        "services.market_data_service.get_dividend_yield",
        new_callable=AsyncMock,
        return_value=0.01,
    ), patch(
        # 讓函式在 indicators 判定後立即提前 return，不需要再往下 mock 更多依賴
        "market_analysis.strategy._calculate_technical_indicators",
        return_value=None,
    ):
        result = await strategy.analyze_symbol("NVDA")

    assert result is None
    starts = [i for i, ev in enumerate(call_order) if ev.startswith("start:")]
    ends = [i for i, ev in enumerate(call_order) if ev.startswith("end:")]
    assert len(starts) == 4
    assert max(starts) < min(
        ends
    ), f"Expected concurrent Phase 1 dispatch, got: {call_order}"


@pytest.mark.asyncio
async def test_analyze_symbol_reuses_provided_df_spy() -> None:
    """若呼叫端已提供 df_spy，不應再重新對 SPY 發送 get_history_df 請求。"""
    history_calls: list[str] = []

    async def _history_effect(symbol: str, *_a: Any, **_k: Any) -> pd.DataFrame:
        history_calls.append(symbol)
        return _make_df()

    df_spy = _make_df()

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "services.market_data_service.is_etf",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        side_effect=_history_effect,
    ), patch(
        "services.market_data_service.get_dividend_yield",
        new_callable=AsyncMock,
        return_value=0.01,
    ), patch(
        "market_analysis.strategy._calculate_technical_indicators", return_value=None
    ):
        await strategy.analyze_symbol("NVDA", df_spy=df_spy)

    assert "SPY" not in history_calls
    assert history_calls == ["NVDA"]


@pytest.mark.asyncio
async def test_analyze_symbol_phase4_dispatches_concurrently_and_respects_best_contract_none() -> (
    None
):
    """驗證 Phase 4 (opt_chain+best_contract / ema_eval / mmm / term_structure) 併發派發，
    且即使 best_contract 為 None 導致提前 return，其餘三項仍會被併發呼叫（刻意接受的 tradeoff）。
    """
    call_order: list[str] = []

    async def _opt_chain_effect(*_a: Any, **_k: Any) -> tuple:
        call_order.append("start:opt_chain")
        await asyncio.sleep(0)
        call_order.append("end:opt_chain")
        return None, None  # best_contract=None -> 觸發提前 return

    async def _ema_effect(*_a: Any, **_k: Any) -> dict:
        call_order.append("start:ema_eval")
        await asyncio.sleep(0)
        call_order.append("end:ema_eval")
        return {
            "trend": "NEUTRAL",
            "ema_8": 1.0,
            "ema_21": 1.0,
            "distance_from_21": 0.0,
        }

    async def _mmm_effect(*_a: Any, **_k: Any) -> tuple:
        call_order.append("start:mmm")
        await asyncio.sleep(0)
        call_order.append("end:mmm")
        return 0.0, 0.0, 0.0, -1

    async def _ts_effect(*_a: Any, **_k: Any) -> tuple:
        call_order.append("start:term_structure")
        await asyncio.sleep(0)
        call_order.append("end:term_structure")
        return 1.0, "平滑 (Flat)"

    today = datetime.now().date()
    future_expiry = (today + timedelta(days=40)).strftime("%Y-%m-%d")

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "services.market_data_service.is_etf",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_make_df(),
    ), patch(
        "services.market_data_service.get_dividend_yield",
        new_callable=AsyncMock,
        return_value=0.01,
    ), patch(
        "market_analysis.strategy._calculate_technical_indicators",
        return_value=BASE_INDICATORS,
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[future_expiry],
    ), patch(
        "market_analysis.strategy._fetch_opt_chain_and_best_contract",
        new_callable=AsyncMock,
        side_effect=_opt_chain_effect,
    ), patch(
        "market_analysis.strategy.evaluate_ema_trend",
        new_callable=AsyncMock,
        side_effect=_ema_effect,
    ), patch(
        "market_analysis.strategy._calculate_mmm",
        new_callable=AsyncMock,
        side_effect=_mmm_effect,
    ), patch(
        "market_analysis.strategy._calculate_term_structure",
        new_callable=AsyncMock,
        side_effect=_ts_effect,
    ):
        result = await strategy.analyze_symbol("NVDA", vix_spot=15.0)

    # best_contract is None -> 整個函式應回傳 None
    assert result is None

    # 但四項 Phase 4 協程仍應全數被呼叫（併發派發後才依序捨棄），而非因為
    # best_contract=None 就跳過 ema_eval/mmm/term_structure 的呼叫。
    started = {ev.split(":", 1)[1] for ev in call_order if ev.startswith("start:")}
    assert started == {"opt_chain", "ema_eval", "mmm", "term_structure"}

    starts = [i for i, ev in enumerate(call_order) if ev.startswith("start:")]
    ends = [i for i, ev in enumerate(call_order) if ev.startswith("end:")]
    assert max(starts) < min(
        ends
    ), f"Expected concurrent Phase 4 dispatch, got: {call_order}"
