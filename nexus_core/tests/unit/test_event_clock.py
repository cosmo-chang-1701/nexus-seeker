"""單元測試：事件時鐘與工作註冊表 (event_clock.py)。"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from market_analysis.fundamental_pipeline.event_clock import (
    ClockJob,
    ClockJobRegistry,
    daily_at,
    every_n_minutes,
    weekday_at,
)


@pytest.fixture(autouse=True)
def clean_registry() -> Generator[None, None, None]:
    """每個測試前後清理註冊表以確保測試獨立性。"""
    ClockJobRegistry.clear()
    yield
    ClockJobRegistry.clear()


@pytest.mark.asyncio
async def test_clock_job_execution() -> None:
    """測試 ClockJob 實例化與非同步執行。"""
    executed = False

    async def sample_handler(val: str) -> str:
        nonlocal executed
        executed = True
        return f"done:{val}"

    job = ClockJob(
        job_id="test_job",
        name="測試工作",
        schedule_desc="隨時",
        handler=sample_handler,
        is_due_fn=lambda dt: True,
        priority=10,
    )

    res = await job.execute("foo")
    assert executed is True
    assert res == "done:foo"


def test_clock_job_registry_operations() -> None:
    """測試註冊表的註冊、取得、註銷與依優先級排序。"""

    async def dummy() -> None:
        pass

    job1 = ClockJob(
        job_id="job_low",
        name="低優先",
        schedule_desc="",
        handler=dummy,
        is_due_fn=lambda dt: True,
        priority=50,
    )
    job2 = ClockJob(
        job_id="job_high",
        name="高優先",
        schedule_desc="",
        handler=dummy,
        is_due_fn=lambda dt: True,
        priority=10,
    )

    ClockJobRegistry.register(job1)
    ClockJobRegistry.register(job2)

    assert ClockJobRegistry.get("job_low") == job1
    assert ClockJobRegistry.get("job_high") == job2

    # 排序檢查：優先級數字小的在前
    all_jobs = ClockJobRegistry.all_jobs()
    assert len(all_jobs) == 2
    assert all_jobs[0].job_id == "job_high"
    assert all_jobs[1].job_id == "job_low"

    # 註銷
    removed = ClockJobRegistry.unregister("job_low")
    assert removed == job1
    assert ClockJobRegistry.get("job_low") is None
    assert len(ClockJobRegistry.all_jobs()) == 1


def test_schedule_condition_helpers() -> None:
    """測試排程條件判斷輔助函式。"""
    ny_tz = ZoneInfo("America/New_York")
    # 2026-10-05 (週一) 08:35:00
    mon_0835 = datetime(2026, 10, 5, 8, 35, 0, tzinfo=ny_tz)
    # 2026-10-05 (週一) 08:46:00
    mon_0846 = datetime(2026, 10, 5, 8, 46, 0, tzinfo=ny_tz)
    # 2026-10-10 (週六) 08:35:00
    sat_0835 = datetime(2026, 10, 10, 8, 35, 0, tzinfo=ny_tz)

    # daily_at 08:30 (10分鐘視窗: 08:30 ~ 08:40)
    d_checker = daily_at(8, 30, window_minutes=10)
    assert d_checker(mon_0835) is True
    assert d_checker(mon_0846) is False
    assert d_checker(sat_0835) is True

    # weekday_at 08:30 (10分鐘視窗: 08:30 ~ 08:40)
    w_checker = weekday_at(8, 30, window_minutes=10)
    assert w_checker(mon_0835) is True
    assert w_checker(mon_0846) is False
    # 週末不觸發
    assert w_checker(sat_0835) is False

    # every_n_minutes 15
    m_checker = every_n_minutes(15)
    assert m_checker(datetime(2026, 10, 5, 8, 0, 0, tzinfo=ny_tz)) is True
    assert m_checker(datetime(2026, 10, 5, 8, 15, 0, tzinfo=ny_tz)) is True
    assert m_checker(datetime(2026, 10, 5, 8, 30, 0, tzinfo=ny_tz)) is True
    assert m_checker(datetime(2026, 10, 5, 8, 7, 0, tzinfo=ny_tz)) is False


def test_clock_job_registry_due_jobs() -> None:
    """測試 due_jobs 篩選。"""

    async def dummy() -> None:
        pass

    job_due = ClockJob(
        job_id="due_job",
        name="到期任務",
        schedule_desc="",
        handler=dummy,
        is_due_fn=lambda dt: dt.hour == 16,
        priority=10,
    )
    job_not_due = ClockJob(
        job_id="not_due_job",
        name="未到期任務",
        schedule_desc="",
        handler=dummy,
        is_due_fn=lambda dt: dt.hour == 9,
        priority=20,
    )

    ClockJobRegistry.register(job_due)
    ClockJobRegistry.register(job_not_due)

    ny_tz = ZoneInfo("America/New_York")
    dt_16 = datetime(2026, 10, 5, 16, 15, 0, tzinfo=ny_tz)
    due = ClockJobRegistry.due_jobs(dt_16)
    assert len(due) == 1
    assert due[0].job_id == "due_job"
