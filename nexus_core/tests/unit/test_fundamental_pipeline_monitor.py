"""單元測試：基本面管線事件時鐘監控 (cogs/trading/fundamental_pipeline_monitor.py)。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from cogs.trading.fundamental_pipeline_monitor import (
    _run_macro_surprise_job,
    nyse_trading_day_at,
    register_default_fundamental_jobs,
)
from market_analysis.fundamental_pipeline.event_clock import ClockJobRegistry

ny_tz = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def clean_registry() -> Generator[None, None, None]:
    ClockJobRegistry.clear()
    yield
    ClockJobRegistry.clear()


def test_nyse_trading_day_at_skips_market_holidays() -> None:
    """16:15 流動性任務：NYSE 休市日（感恩節、聖誕節）與週末不觸發。"""
    checker = nyse_trading_day_at(16, 15, window_minutes=15)

    # 2026-11-25 (週三) 正常交易日
    assert checker(datetime(2026, 11, 25, 16, 20, tzinfo=ny_tz)) is True
    # 2026-11-26 (週四) 感恩節休市
    assert checker(datetime(2026, 11, 26, 16, 20, tzinfo=ny_tz)) is False
    # 2026-12-25 (週五) 聖誕節休市
    assert checker(datetime(2026, 12, 25, 16, 20, tzinfo=ny_tz)) is False
    # 2026-11-28 (週六)
    assert checker(datetime(2026, 11, 28, 16, 20, tzinfo=ny_tz)) is False
    # 交易日但不在時間視窗內
    assert checker(datetime(2026, 11, 25, 16, 40, tzinfo=ny_tz)) is False


def test_nyse_trading_day_at_falls_back_to_weekday_on_calendar_error() -> None:
    """行事曆查詢失敗時退回平日判定。"""
    checker = nyse_trading_day_at(16, 15, window_minutes=15)
    with patch(
        "cogs.trading.fundamental_pipeline_monitor.market_time.is_nyse_trading_day",
        side_effect=RuntimeError("calendar down"),
    ):
        assert checker(datetime(2026, 11, 26, 16, 20, tzinfo=ny_tz)) is True
        assert checker(datetime(2026, 11, 28, 16, 20, tzinfo=ny_tz)) is False


def test_liquidity_job_not_due_on_holiday_but_macro_jobs_keep_weekday() -> None:
    """流動性任務走 NYSE 交易日；預期差任務維持平日（休市日仍可能公布總經數據）。"""
    register_default_fundamental_jobs()
    holiday_1620 = datetime(2026, 11, 26, 16, 20, tzinfo=ny_tz)
    assert ClockJobRegistry.due_jobs(holiday_1620) == []

    good_friday_0830 = datetime(2026, 4, 3, 8, 32, tzinfo=ny_tz)
    due_ids = [j.job_id for j in ClockJobRegistry.due_jobs(good_friday_0830)]
    assert due_ids == ["macro_surprise_0830"]


@pytest.mark.asyncio
async def test_macro_surprise_job_force_refreshes_calendar_first() -> None:
    """預期差任務先以 force_fetch=True 刷新當月日曆，再計算預期差。"""
    call_order: list[str] = []

    async def _fake_prefetch(**kwargs: object) -> bool:
        call_order.append("prefetch")
        assert kwargs["force_fetch"] is True
        assert kwargs["months_ahead"] == 0
        return True

    async def _fake_process() -> list[object]:
        call_order.append("process")
        return []

    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.calendar_service.prefetch_monthly_macro_cache",
            new=AsyncMock(side_effect=_fake_prefetch),
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.process_macro_surprises",
            new=AsyncMock(side_effect=_fake_process),
        ),
    ):
        await _run_macro_surprise_job(datetime(2026, 10, 7, 8, 31, tzinfo=ny_tz))

    assert call_order == ["prefetch", "process"]


@pytest.mark.asyncio
async def test_macro_surprise_job_still_processes_when_refresh_fails() -> None:
    """Edge 不可用（刷新失敗或例外）時沿用既有快取，仍照常計算。"""
    mock_process = AsyncMock(return_value=[])
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.calendar_service.prefetch_monthly_macro_cache",
            new=AsyncMock(side_effect=RuntimeError("edge down")),
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.process_macro_surprises",
            new=mock_process,
        ),
    ):
        await _run_macro_surprise_job(datetime(2026, 10, 7, 8, 31, tzinfo=ny_tz))

    mock_process.assert_awaited_once()


# ---------------------------------------------------------------------------
# PR4 產業鏈交叉驗證排程 (channel_check_1800)
# ---------------------------------------------------------------------------


def test_channel_check_job_due_only_on_trading_day_1800_window() -> None:
    """18:00 ET 視窗（15 分鐘）且為 NYSE 交易日才觸發；休市日與視窗外不觸發。"""
    register_default_fundamental_jobs()

    def _due(dt: datetime) -> list[str]:
        return [j.job_id for j in ClockJobRegistry.due_jobs(dt)]

    assert "channel_check_1800" in _due(datetime(2026, 10, 7, 18, 0, tzinfo=ny_tz))
    assert "channel_check_1800" in _due(datetime(2026, 10, 7, 18, 14, tzinfo=ny_tz))
    assert "channel_check_1800" not in _due(datetime(2026, 10, 7, 18, 15, tzinfo=ny_tz))
    assert "channel_check_1800" not in _due(datetime(2026, 10, 7, 17, 55, tzinfo=ny_tz))
    # 感恩節休市、週六
    assert "channel_check_1800" not in _due(datetime(2026, 11, 26, 18, 5, tzinfo=ny_tz))
    assert "channel_check_1800" not in _due(datetime(2026, 10, 10, 18, 5, tzinfo=ny_tz))


@pytest.mark.asyncio
async def test_channel_check_job_runs_two_completed_periods_with_persist() -> None:
    """排程對最近兩個已結束曆季呼叫 run_all_channel_checks(persist=True)。"""
    from cogs.trading.fundamental_pipeline_monitor import _run_channel_check_job

    mock_run = AsyncMock(return_value=[])
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.alt_data_service.run_all_channel_checks",
            new=mock_run,
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.is_memory_safe",
            return_value=True,
        ),
    ):
        await _run_channel_check_job(datetime(2026, 10, 7, 18, 1, tzinfo=ny_tz))

    periods = [c.kwargs["as_of_period"] for c in mock_run.await_args_list]
    assert periods == ["2026-Q3", "2026-Q2"]
    for c in mock_run.await_args_list:
        assert c.kwargs["persist"] is True
        assert str(c.kwargs["as_of"]) == "2026-10-07"


@pytest.mark.asyncio
async def test_channel_check_job_skips_when_memory_unsafe() -> None:
    """記憶體不足時工作本體不執行任何檢驗。"""
    from cogs.trading.fundamental_pipeline_monitor import _run_channel_check_job

    mock_run = AsyncMock(return_value=[])
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.alt_data_service.run_all_channel_checks",
            new=mock_run,
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.is_memory_safe",
            return_value=False,
        ),
    ):
        await _run_channel_check_job(datetime(2026, 10, 7, 18, 1, tzinfo=ny_tz))
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_check_job_one_period_failure_does_not_stop_other() -> None:
    """單一期別例外不影響另一期別。"""
    from cogs.trading.fundamental_pipeline_monitor import _run_channel_check_job

    mock_run = AsyncMock(side_effect=[RuntimeError("SEC down"), []])
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.alt_data_service.run_all_channel_checks",
            new=mock_run,
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.is_memory_safe",
            return_value=True,
        ),
    ):
        await _run_channel_check_job(datetime(2026, 10, 7, 18, 1, tzinfo=ny_tz))
    assert mock_run.await_count == 2


def _make_monitor_cog(is_leader: bool) -> object:
    from unittest.mock import MagicMock

    from cogs.trading.fundamental_pipeline_monitor import (
        FundamentalPipelineMonitorCog,
    )

    bot = MagicMock()
    bot._is_leader_instance = is_leader
    with patch("discord.ext.tasks.Loop.start"):
        return FundamentalPipelineMonitorCog(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_leader", "memory_ok"), [(False, True), (True, False)])
async def test_clock_loop_does_not_run_channel_check_when_not_leader_or_memory_unsafe(
    is_leader: bool, memory_ok: bool
) -> None:
    """非 leader 或記憶體不足時，監控迴圈不執行任何到期工作（含產業鏈檢驗）。"""
    cog = _make_monitor_cog(is_leader)
    mock_run = AsyncMock(return_value=[])
    fixed_now = datetime(2026, 10, 7, 18, 2, tzinfo=ny_tz)
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.alt_data_service.run_all_channel_checks",
            new=mock_run,
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.is_memory_safe",
            return_value=memory_ok,
        ),
        patch("cogs.trading.fundamental_pipeline_monitor.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = fixed_now
        await cog.fundamental_clock_task.coro(cog)  # type: ignore[attr-defined]
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_clock_loop_runs_channel_check_for_leader() -> None:
    """leader 且記憶體安全時，18:00 視窗內會執行產業鏈檢驗（對照組）。"""
    cog = _make_monitor_cog(True)
    mock_run = AsyncMock(return_value=[])
    fixed_now = datetime(2026, 10, 7, 18, 2, tzinfo=ny_tz)
    with (
        patch(
            "cogs.trading.fundamental_pipeline_monitor.alt_data_service.run_all_channel_checks",
            new=mock_run,
        ),
        patch(
            "cogs.trading.fundamental_pipeline_monitor.is_memory_safe",
            return_value=True,
        ),
        patch("cogs.trading.fundamental_pipeline_monitor.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = fixed_now
        await cog.fundamental_clock_task.coro(cog)  # type: ignore[attr-defined]
    assert mock_run.await_count == 2
