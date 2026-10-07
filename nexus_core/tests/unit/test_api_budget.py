"""api_budget：計數、互動／背景分流、跨 1 小時視窗輸出單行摘要並歸零。"""

import logging
from unittest.mock import patch

import pytest

from services.market_data_service import api_budget


@pytest.fixture(autouse=True)
def _reset() -> None:
    api_budget.reset_for_tests()


def test_record_call_counts_and_splits_interactive() -> None:
    api_budget.record_call("finnhub", "quote", interactive=False)
    api_budget.record_call("finnhub", "quote", interactive=False)
    api_budget.record_call("finnhub", "quote", interactive=True)
    api_budget.record_call("yahoo", "edge_history", interactive=False)
    api_budget.record_rate_limited("finnhub", "quote")
    assert api_budget.snapshot() == {
        "finnhub/quote/background": 2,
        "finnhub/quote/interactive": 1,
        "yahoo/edge_history/background": 1,
        "finnhub/quote/429": 1,
    }


def test_window_rollover_logs_once_and_resets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = api_budget._window_started_at
    with patch(
        "services.market_data_service.api_budget.time.time", return_value=base + 10
    ):
        api_budget.record_call("finnhub", "quote", interactive=False)
        api_budget.record_rate_limited("finnhub", "quote")
        api_budget.record_call("yahoo", "edge_history", interactive=True)
    with caplog.at_level(logging.INFO, logger=api_budget.logger.name):
        with patch(
            "services.market_data_service.api_budget.time.time",
            return_value=base + 3601,
        ):
            api_budget.record_call("finnhub", "profile", interactive=False)
            api_budget.record_call("finnhub", "profile", interactive=False)
    msgs = [r.getMessage() for r in caplog.records if "API 配額" in r.getMessage()]
    assert len(msgs) == 1
    assert "finnhub=1（背景 1／互動 0，429=1）" in msgs[0]
    assert "yahoo=1（背景 0／互動 1，429=0）" in msgs[0]
    assert "finnhub/quote=1" in msgs[0]
    # 歸零後只剩視窗翻轉後的 2 次呼叫
    assert api_budget.snapshot() == {"finnhub/profile/background": 2}
