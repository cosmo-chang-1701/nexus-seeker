"""基本面分析管線事件時鐘監控 Cog (Fundamental Pipeline Monitor)。

職責：
1. 每 5 分鐘輪詢一次全域時鐘工作註冊表 (ClockJobRegistry)。
2. 嚴格檢查 Leader 鎖定狀態與 VPS 記憶體守衛 (is_memory_safe)。
3. 使用 BoundedCache 執行執行期去重防護。
4. 預先註冊宏觀流動性 (16:15 ET) 與總經預期差 (08:30 / 10:00 ET) 核心工作。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import config
from discord.ext import commands, tasks
from market_analysis.fundamental_pipeline.event_clock import (
    ClockJob,
    ClockJobRegistry,
    weekday_at,
)
from services.bounded_cache import BoundedCache
from services.liquidity_service import run_liquidity_pipeline
from services.llm_service import is_memory_safe
from services.macro_surprise_service import process_macro_surprises

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


async def _run_liquidity_regime_job(now_et: datetime) -> None:
    """平日 16:15 ET 執行流動性體制計算與寫入。"""
    try:
        await run_liquidity_pipeline(now_et.date())
    except Exception:
        logger.exception("[FundamentalPipeline] 執行流動性體制任務失敗")


async def _run_macro_surprise_job(now_et: datetime) -> None:
    """平日 08:30 / 10:00 ET 執行宏觀預期差計算與寫入。"""
    try:
        await process_macro_surprises()
    except Exception:
        logger.exception("[FundamentalPipeline] 執行總經預期差任務失敗")


def register_default_fundamental_jobs() -> None:
    """註冊 PR1 預設之基礎事件時鐘任務。"""
    ClockJobRegistry.register(
        ClockJob(
            job_id="macro_surprise_0830",
            name="08:30 宏觀預期差標準化掃描",
            schedule_desc="平日 08:30 ET (CPI / NFP / 零售銷售)",
            handler=_run_macro_surprise_job,
            is_due_fn=weekday_at(8, 30, window_minutes=15),
            priority=10,
            description="捕捉 08:30 ET 發布之總經數據實際值並計算 Z-Score",
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="macro_surprise_1000",
            name="10:00 宏觀預期差標準化掃描",
            schedule_desc="平日 10:00 ET (ISM PMI / 工廠訂單)",
            handler=_run_macro_surprise_job,
            is_due_fn=weekday_at(10, 0, window_minutes=15),
            priority=20,
            description="捕捉 10:00 ET 發布之總經數據實際值並計算 Z-Score",
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="liquidity_regime_1615",
            name="16:15 央行淨流動性與體制精算",
            schedule_desc="平日 16:15 ET (盤後維護窗口)",
            handler=_run_liquidity_regime_job,
            is_due_fn=weekday_at(16, 15, window_minutes=15),
            priority=30,
            description="抓取 FRED H.4.1/NFCI 計算淨流動性季增率與體制狀態",
        )
    )


class FundamentalPipelineMonitorCog(commands.Cog):
    """基本面分析管線事件時鐘監控 Cog。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._dedup_cache = BoundedCache(max_size=300)
        register_default_fundamental_jobs()
        self.fundamental_clock_task.start()

    async def cog_unload(self) -> None:
        self.fundamental_clock_task.cancel()

    @tasks.loop(minutes=5)
    async def fundamental_clock_task(self) -> None:
        """每 5 分鐘輪詢一次時鐘工作註冊表。"""
        # 1. 功能開關守衛
        if not getattr(config, "ENABLE_FUNDAMENTAL_PIPELINE_LOG", True):
            return

        # 2. Leader 角色守衛
        if not getattr(self.bot, "_is_leader_instance", False):
            return

        # 3. 記憶體守衛 (1GB VPS 85% RAM 門檻)
        if not is_memory_safe():
            logger.warning(
                "[FundamentalPipelineMonitor] VPS 記憶體使用率超標，跳過本輪時鐘掃描"
            )
            return

        now_et = datetime.now(ny_tz)
        due_jobs = ClockJobRegistry.due_jobs(now_et)
        if not due_jobs:
            return

        for job in due_jobs:
            now_ts = now_et.timestamp()
            last_run = self._dedup_cache.get(job.job_id)
            if last_run is not None and isinstance(last_run, (int, float)):
                cooldown = getattr(job, "cooldown_seconds", 1800.0)
                if (now_ts - last_run) < cooldown:
                    continue

            self._dedup_cache[job.job_id] = now_ts
            logger.info(
                f"[FundamentalPipelineMonitor] 觸發排程任務: {job.name} ({job.job_id})"
            )

            try:
                await job.execute(now_et)
            except Exception:
                logger.exception(
                    f"[FundamentalPipelineMonitor] 任務 {job.job_id} 執行例外"
                )

    @fundamental_clock_task.before_loop
    async def before_fundamental_clock_task(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(FundamentalPipelineMonitorCog(bot))
