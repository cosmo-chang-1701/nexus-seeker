"""單元測試：基本面管線事件時鐘監控 (cogs/trading/fundamental_pipeline_monitor.py)。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import config
import pytest
from cogs.trading.fundamental_pipeline_monitor import (
    FundamentalPipelineMonitorCog,
    SecFilingSyncRunner,
    _run_macro_surprise_job,
    nyse_trading_day_at,
    register_default_fundamental_jobs,
    weekday_hourly_between,
)
from market_analysis.fundamental_pipeline.event_clock import ClockJob, ClockJobRegistry
from services.bounded_cache import BoundedCache

_MONITOR = "cogs.trading.fundamental_pipeline_monitor"

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
# SEC 申報同步排程 (sec_filing_sync_hourly)
# ---------------------------------------------------------------------------

_WED_0902 = datetime(2026, 10, 7, 9, 2, tzinfo=ny_tz)


def _fake_service(sync: AsyncMock) -> MagicMock:
    service = MagicMock()
    service.get_client = AsyncMock()
    service.sync_universe_filings = sync
    return service


def _leader_bot() -> SimpleNamespace:
    return SimpleNamespace(_is_leader_instance=True)


def test_weekday_hourly_between_window() -> None:
    """平日 07–20 點每個整點的前 10 分鐘觸發，其餘時間與週末不觸發。"""
    checker = weekday_hourly_between(7, 20, window_minutes=10)
    assert checker(datetime(2026, 10, 7, 7, 0, tzinfo=ny_tz)) is True
    assert checker(datetime(2026, 10, 7, 7, 5, tzinfo=ny_tz)) is True
    assert checker(datetime(2026, 10, 7, 7, 10, tzinfo=ny_tz)) is False
    assert checker(datetime(2026, 10, 7, 6, 55, tzinfo=ny_tz)) is False
    assert checker(datetime(2026, 10, 7, 20, 5, tzinfo=ny_tz)) is True
    assert checker(datetime(2026, 10, 7, 21, 0, tzinfo=ny_tz)) is False
    # 2026-10-10 週六
    assert checker(datetime(2026, 10, 10, 10, 0, tzinfo=ny_tz)) is False


def test_sec_filing_sync_job_registered_hourly_on_weekdays() -> None:
    """SEC 同步工作：平日 07:00–20:00 每整點到期；SEC 營業的耶穌受難日照常執行。"""
    register_default_fundamental_jobs()
    job = ClockJobRegistry.get("sec_filing_sync_hourly")
    assert job is not None
    assert job.cooldown_seconds >= 10 * 60

    due_hours = [
        h for h in range(24) if job.is_due(datetime(2026, 10, 7, h, 0, tzinfo=ny_tz))
    ]
    assert due_hours == list(range(7, 21))

    good_friday_1000 = datetime(2026, 4, 3, 10, 0, tzinfo=ny_tz)
    due_ids = [j.job_id for j in ClockJobRegistry.due_jobs(good_friday_1000)]
    assert "sec_filing_sync_hourly" in due_ids
    assert job.is_due(datetime(2026, 10, 10, 10, 0, tzinfo=ny_tz)) is False


@pytest.mark.asyncio
async def test_sec_runner_trigger_calls_sync_universe_in_background() -> None:
    """trigger 以背景任務呼叫 FilingEventService.sync_universe_filings。"""
    sync = AsyncMock(return_value={"events": 1})
    runner = SecFilingSyncRunner(_leader_bot())
    with (
        patch(f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)),
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
    ):
        assert await runner.trigger(_WED_0902) is True
        assert runner._task is not None
        await runner._task
    sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sec_runner_skips_when_not_leader() -> None:
    """非 leader 不建立服務、不執行。"""
    sync = AsyncMock(return_value={})
    runner = SecFilingSyncRunner(SimpleNamespace(_is_leader_instance=False))
    with (
        patch(
            f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)
        ) as factory,
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
    ):
        assert await runner.trigger(_WED_0902) is False
    factory.assert_not_called()
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_sec_runner_skips_when_memory_unsafe() -> None:
    """記憶體超過 85% 門檻時不執行。"""
    sync = AsyncMock(return_value={})
    runner = SecFilingSyncRunner(_leader_bot())
    with (
        patch(
            f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)
        ) as factory,
        patch(f"{_MONITOR}.is_memory_safe", return_value=False),
    ):
        assert await runner.trigger(_WED_0902) is False
    factory.assert_not_called()
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_sec_runner_missing_user_agent_logs_error_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未設定 SEC_USER_AGENT：只記一次 error，之後每輪靜默略過，不呼叫同步。"""
    monkeypatch.setattr(config, "SEC_USER_AGENT", "")
    runner = SecFilingSyncRunner(_leader_bot())
    with (
        patch(
            "services.filing_event_service.FilingEventService.sync_universe_filings",
            new_callable=AsyncMock,
        ) as sync,
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
        caplog.at_level(logging.INFO, logger=_MONITOR),
    ):
        for _ in range(3):
            assert await runner.trigger(_WED_0902) is False

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "SEC_USER_AGENT" in errors[0].getMessage()
    sync.assert_not_awaited()
    assert runner._task is None


@pytest.mark.asyncio
async def test_sec_runner_prevents_overlapping_runs() -> None:
    """上一輪未完成時略過本輪；完成後下一輪可再次啟動。"""
    gate = asyncio.Event()

    async def _slow_sync() -> dict[str, int]:
        await gate.wait()
        return {}

    sync = AsyncMock(side_effect=_slow_sync)
    runner = SecFilingSyncRunner(_leader_bot())
    with (
        patch(f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)),
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
    ):
        assert await runner.trigger(_WED_0902) is True
        await asyncio.sleep(0)
        assert runner.is_running() is True
        assert await runner.trigger(_WED_0902) is False
        assert sync.await_count == 1

        gate.set()
        assert runner._task is not None
        await runner._task
        assert runner.is_running() is False
        assert await runner.trigger(_WED_0902) is True
        await runner._task
    assert sync.await_count == 2


@pytest.mark.asyncio
async def test_sec_runner_logs_sync_exception_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """背景同步例外以 logger.exception 記錄，不外拋。"""
    sync = AsyncMock(side_effect=RuntimeError("boom"))
    runner = SecFilingSyncRunner(_leader_bot())
    with (
        patch(f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)),
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
        caplog.at_level(logging.ERROR, logger=_MONITOR),
    ):
        assert await runner.trigger(_WED_0902) is True
        assert runner._task is not None
        await runner._task
    assert any("SEC 申報同步失敗" in r.getMessage() for r in caplog.records)


def _bare_cog(bot: Any, runner: SecFilingSyncRunner) -> FundamentalPipelineMonitorCog:
    """不啟動 tasks.loop 的 Cog 實例，直接呼叫迴圈本體。"""
    cog = object.__new__(FundamentalPipelineMonitorCog)
    cog.bot = bot
    cog._dedup_cache = BoundedCache(max_size=10)
    cog._sec_sync_runner = runner
    return cog


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "_FixedDateTime":
        return cls(2026, 10, 7, 9, 2, tzinfo=ny_tz)


@pytest.mark.asyncio
async def test_clock_task_skips_jobs_when_not_leader_or_memory_unsafe() -> None:
    """時鐘迴圈：非 leader 或記憶體不足時不執行任何到期工作。"""
    handler = AsyncMock()
    ClockJobRegistry.register(
        ClockJob(
            job_id="probe",
            name="probe",
            schedule_desc="always",
            handler=handler,
            is_due_fn=lambda _dt: True,
        )
    )
    bot = SimpleNamespace(_is_leader_instance=False)
    cog = _bare_cog(bot, SecFilingSyncRunner(bot))
    loop_body = FundamentalPipelineMonitorCog.fundamental_clock_task.coro

    with patch.object(config, "ENABLE_FUNDAMENTAL_PIPELINE_LOG", True):
        with patch(f"{_MONITOR}.is_memory_safe", return_value=True):
            await loop_body(cog)
        handler.assert_not_awaited()

        bot._is_leader_instance = True
        with patch(f"{_MONITOR}.is_memory_safe", return_value=False):
            await loop_body(cog)
        handler.assert_not_awaited()

        with patch(f"{_MONITOR}.is_memory_safe", return_value=True):
            await loop_body(cog)
        handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_clock_task_triggers_sec_sync_without_blocking() -> None:
    """09:02 ET 平日：時鐘觸發 SEC 同步並立即返回（同步於背景執行），同一整點不重複觸發。"""
    gate = asyncio.Event()

    async def _slow_sync() -> dict[str, int]:
        await gate.wait()
        return {}

    sync = AsyncMock(side_effect=_slow_sync)
    bot = _leader_bot()
    runner = SecFilingSyncRunner(bot)
    register_default_fundamental_jobs(runner)
    cog = _bare_cog(bot, runner)
    loop_body = FundamentalPipelineMonitorCog.fundamental_clock_task.coro

    with (
        patch.object(config, "ENABLE_FUNDAMENTAL_PIPELINE_LOG", True),
        patch(f"{_MONITOR}.datetime", _FixedDateTime),
        patch(f"{_MONITOR}.FilingEventService", return_value=_fake_service(sync)),
        patch(f"{_MONITOR}.is_memory_safe", return_value=True),
    ):
        await asyncio.wait_for(loop_body(cog), timeout=2)
        assert runner.is_running() is True
        await asyncio.sleep(0)
        sync.assert_awaited_once()

        # 同一整點的下一次輪詢由 cooldown 擋下
        await loop_body(cog)
        assert sync.await_count == 1

        gate.set()
        assert runner._task is not None
        await runner._task
