"""api_budget：計數、互動／背景分流、跨 1 小時視窗輸出單行摘要並歸零。"""

import logging
import threading
from unittest.mock import patch

import pytest

from services import api_budget


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
    with patch("services.api_budget.time.time", return_value=base + 10):
        api_budget.record_call("finnhub", "quote", interactive=False)
        api_budget.record_rate_limited("finnhub", "quote")
        api_budget.record_call("yahoo", "edge_history", interactive=True)
    with caplog.at_level(logging.INFO, logger=api_budget.logger.name):
        with patch(
            "services.api_budget.time.time",
            return_value=base + 3601,
        ):
            api_budget.record_call("finnhub", "profile", interactive=False)
            api_budget.record_call("finnhub", "profile", interactive=False)
    msgs = [r.getMessage() for r in caplog.records if "API 配額" in r.getMessage()]
    assert len(msgs) == 1
    assert "finnhub=1（背景 1／互動 0，429=1，平均 1/小時）" in msgs[0]
    assert "yahoo=1（背景 0／互動 1，429=0，平均 1/小時）" in msgs[0]
    assert "finnhub/quote=1" in msgs[0]
    # 歸零後只剩視窗翻轉後的 2 次呼叫
    assert api_budget.snapshot() == {"finnhub/profile/background": 2}


def _flush_msgs(caplog: pytest.LogCaptureFixture, elapsed: float) -> list[str]:
    base = api_budget._window_started_at
    with patch("services.api_budget.time.time", return_value=base + 5):
        for _ in range(10):
            api_budget.record_call("yahoo", "history", interactive=False)
    with caplog.at_level(logging.INFO, logger=api_budget.logger.name):
        with patch(
            "services.api_budget.time.time",
            return_value=base + elapsed,
        ):
            api_budget.record_call("yahoo", "history", interactive=False)
    return [r.getMessage() for r in caplog.records if "API 配額" in r.getMessage()]


def test_summary_reports_actual_elapsed_and_hourly_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 閒置 3.2 小時後才翻轉：應顯示實際經過時間、平均速率與 ET 起訖時間
    msgs = _flush_msgs(caplog, 3.2 * 3600)
    assert len(msgs) == 1
    assert "過去 3.2 小時" in msgs[0]
    assert "ET" in msgs[0]
    assert "平均 3/小時" in msgs[0]  # 10 次 / 3.2h ≈ 3


def test_summary_one_hour_window_without_et(
    caplog: pytest.LogCaptureFixture,
) -> None:
    msgs = _flush_msgs(caplog, 3601)
    assert len(msgs) == 1
    assert "過去 1.0 小時" in msgs[0]
    assert "ET" not in msgs[0]


def test_record_never_raises_even_if_logger_fails() -> None:
    base = api_budget._window_started_at
    with patch.object(api_budget.logger, "info", side_effect=RuntimeError("boom")):
        with patch(
            "services.api_budget.time.time",
            return_value=base + 4000,
        ):
            api_budget.record_call("finnhub", "quote", interactive=False)
            api_budget.record_rate_limited("finnhub", "quote")


def test_concurrent_record_calls_are_not_lost() -> None:
    def _worker() -> None:
        for _ in range(500):
            api_budget.record_call("yahoo", "history", interactive=False)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert api_budget.snapshot() == {"yahoo/history/background": 4000}
