"""基本面分析管線事件時鐘 (Event Clock & Job Registry)。

遵從開閉原則 (Open-Closed Principle)：
- 定義不可變的 ClockJob 結構與 ClockJobRegistry 單例註冊器。
- 未來 PR (PR2~PR5) 僅需向註冊器註冊新工作，無需修改主排程輪詢器。
- 提供排程條件判斷輔助函式 (daily_at, weekday_at 等)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClockJob:
    """時鐘工作定義。"""

    job_id: str
    name: str
    schedule_desc: str
    handler: Callable[..., Coroutine[Any, Any, Any]]
    is_due_fn: Callable[[datetime], bool]
    priority: int = 100
    description: str = ""
    cooldown_seconds: float = 1800.0

    def is_due(self, dt: datetime) -> bool:
        """判定給定時間是否到達觸發條件。"""
        return self.is_due_fn(dt)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """非同步執行工作本體。"""
        return await self.handler(*args, **kwargs)


class ClockJobRegistry:
    """時鐘工作全域註冊表（開閉原則）。"""

    _registry: ClassVar[dict[str, ClockJob]] = {}

    @classmethod
    def register(cls, job: ClockJob) -> None:
        """註冊新時鐘工作。若 job_id 已存在則覆蓋。"""
        cls._registry[job.job_id] = job
        logger.debug(f"[ClockJobRegistry] 已註冊工作: {job.job_id} ({job.name})")

    @classmethod
    def unregister(cls, job_id: str) -> ClockJob | None:
        """註銷工作。"""
        return cls._registry.pop(job_id, None)

    @classmethod
    def get(cls, job_id: str) -> ClockJob | None:
        """取得指定 ID 的工作。"""
        return cls._registry.get(job_id)

    @classmethod
    def all_jobs(cls) -> list[ClockJob]:
        """取得所有已註冊工作，依優先級 (priority) 升冪排序。"""
        return sorted(cls._registry.values(), key=lambda j: j.priority)

    @classmethod
    def due_jobs(cls, now_dt: datetime) -> list[ClockJob]:
        """查詢在指定時間到達觸發條件的工作清單。"""
        due: list[ClockJob] = []
        for job in cls.all_jobs():
            try:
                if job.is_due(now_dt):
                    due.append(job)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[ClockJobRegistry] 檢查工作 {job.job_id} 到期狀態失敗: {e}"
                )
        return due

    @classmethod
    def clear(cls) -> None:
        """清空註冊表（主要用於單元測試隔離）。"""
        cls._registry.clear()


# ---------------------------------------------------------------------------
# 排程條件建構輔助函式
# ---------------------------------------------------------------------------


def daily_at(
    hour: int, minute: int, window_minutes: int = 10
) -> Callable[[datetime], bool]:
    """每日指定時間視窗內觸發（支援跨午夜視窗防護）。"""

    def _checker(dt: datetime) -> bool:
        target_minutes = hour * 60 + minute
        curr_minutes = dt.hour * 60 + dt.minute
        elapsed = (curr_minutes - target_minutes) % 1440
        return 0 <= elapsed < window_minutes

    return _checker


def weekday_at(
    hour: int, minute: int, window_minutes: int = 10
) -> Callable[[datetime], bool]:
    """僅平日（週一至週五）指定時間視窗內觸發（支援跨午夜視窗防護）。"""

    def _checker(dt: datetime) -> bool:
        target_minutes = hour * 60 + minute
        curr_minutes = dt.hour * 60 + dt.minute
        elapsed = (curr_minutes - target_minutes) % 1440
        if not (0 <= elapsed < window_minutes):
            return False
        # 若發生跨午夜（當前分鐘數小於目標分鐘數），有效營業日參照前一日
        target_weekday = (
            (dt.date() - timedelta(days=1)).weekday()
            if curr_minutes < target_minutes
            else dt.weekday()
        )
        return target_weekday < 5

    return _checker


def every_n_minutes(interval: int) -> Callable[[datetime], bool]:
    """每隔 N 分鐘觸發。"""

    def _checker(dt: datetime) -> bool:
        return dt.minute % interval == 0

    return _checker
