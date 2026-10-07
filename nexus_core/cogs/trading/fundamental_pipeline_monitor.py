"""基本面分析管線事件時鐘監控 Cog (Fundamental Pipeline Monitor)。

職責：
1. 每 5 分鐘輪詢一次全域時鐘工作註冊表 (ClockJobRegistry)。
2. 嚴格檢查 Leader 鎖定狀態與 VPS 記憶體守衛 (is_memory_safe)。
3. 使用 BoundedCache 執行執行期去重防護。
4. 預先註冊宏觀流動性 (NYSE 交易日 16:15 ET) 與總經預期差 (平日 08:30 / 10:00 ET)
   核心工作。預期差工作刻意不排除休市日：總經數據可能於休市日照常公布（如耶穌受難日的非農）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import config
import market_time
from discord.ext import commands, tasks
from market_analysis.fundamental_pipeline.event_clock import (
    ClockJob,
    ClockJobRegistry,
    weekday_at,
)
from services.bounded_cache import BoundedCache
from services.calendar_service import calendar_service
from services.liquidity_service import run_liquidity_pipeline
from services.llm_service import is_memory_safe
from services.macro_surprise_service import process_macro_surprises
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


def nyse_trading_day_at(
    hour: int, minute: int, window_minutes: int = 10
) -> Callable[[datetime], bool]:
    """NYSE 交易日（排除週末與國定休市日）指定時間視窗內觸發。

    先以 `weekday_at` 做便宜的時間視窗判斷，命中後才查詢 NYSE 行事曆。
    行事曆查詢失敗時退回平日判定（寧可多跑一次唯讀精算，也不漏跑）。
    僅適用不跨午夜的視窗（以 `dt.date()` 作為交易日）。
    """
    base = weekday_at(hour, minute, window_minutes=window_minutes)

    def _checker(dt: datetime) -> bool:
        if not base(dt):
            return False
        try:
            return market_time.is_nyse_trading_day(dt.date())
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[FundamentalPipeline] NYSE 行事曆查詢失敗，退回平日判定: {e}"
            )
            return True

    return _checker


async def _refresh_macro_calendar(now_et: datetime) -> bool:
    """預期差掃描前強制重抓當月總經日曆，取得當日剛公布的實際值。

    `CalendarService._ensure_macro_month_cached` 的一般路徑有 24 小時新鮮度，
    08:30 / 10:00 讀快取會讀不到當天的 actual。此處沿用 `/force_macro_update`
    相同的 `prefetch_monthly_macro_cache(force_fetch=True)`：未設定 TUNNEL_URL
    或 Edge 無有效回應時回傳 False 並保留既有 SQLite 快取（SWR），不拋例外。
    以 SingleFlight 合併同月份的併發刷新。
    """
    month_key = now_et.strftime("%Y-%m")
    try:
        ok = await SingleFlightManager.run(
            f"fundamental_macro_calendar_refresh:{month_key}",
            calendar_service.prefetch_monthly_macro_cache,
            reference=now_et,
            months_ahead=0,
            force_fetch=True,
        )
        if not ok:
            logger.warning(
                f"[FundamentalPipeline] 總經日曆強制刷新未成功 ({month_key})，沿用既有快取"
            )
        return bool(ok)
    except Exception:
        logger.exception(f"[FundamentalPipeline] 總經日曆強制刷新例外 ({month_key})")
        return False


async def _run_liquidity_regime_job(now_et: datetime) -> None:
    """平日 16:15 ET 執行流動性體制計算與寫入。"""
    try:
        await run_liquidity_pipeline(now_et.date())
    except Exception:
        logger.exception("[FundamentalPipeline] 執行流動性體制任務失敗")


async def _run_macro_surprise_job(now_et: datetime) -> None:
    """平日 08:30 / 10:00 ET 執行宏觀預期差計算與寫入。

    先強制刷新當月日曆（失敗時沿用既有快取，仍照常計算），再掃描入庫。
    """
    await _refresh_macro_calendar(now_et)
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
            schedule_desc="NYSE 交易日 16:15 ET (盤後維護窗口)",
            handler=_run_liquidity_regime_job,
            is_due_fn=nyse_trading_day_at(16, 15, window_minutes=15),
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
