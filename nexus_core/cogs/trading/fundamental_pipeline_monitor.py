"""基本面分析管線事件時鐘監控 Cog (Fundamental Pipeline Monitor)。

職責：
1. 每 5 分鐘輪詢一次全域時鐘工作註冊表 (ClockJobRegistry)。
2. 嚴格檢查 Leader 鎖定狀態與 VPS 記憶體守衛 (is_memory_safe)。
3. 使用 BoundedCache 執行執行期去重防護。
4. 預先註冊宏觀流動性 (NYSE 交易日 16:15 ET) 與總經預期差 (平日 08:30 / 10:00 ET)
   核心工作。預期差工作刻意不排除休市日：總經數據可能於休市日照常公布（如耶穌受難日的非農）。
5. 註冊 SEC 申報同步 (平日 07:00–20:00 ET 每整點)：以背景任務執行
   `FilingEventService.sync_universe_filings`，避免單輪（尤其首次回填）阻塞其他時鐘工作；
   上一輪未完成時略過本輪。SEC 依聯邦營業日運作（耶穌受難日照常受理），故採平日而非 NYSE 交易日。
6. 同步發現的 8-K Item 2.02（含首次回填）注入 `EarningsSurpriseService.process_filing_event`
   事件觸發財報預期差與指引擷取（只入庫、不推播；LLM 前由服務檢查記憶體與 API 設定）。
7. 註冊財報預期差 PENDING 重試 (NYSE 交易日 17:30 ET)：BMO 財報在 Finnhub 尚無實際值時
   先寫成 PENDING，收盤後重算近 14 個日曆日（約 10 個交易日）內仍為 PENDING 的財季。
8. PR4 產業鏈交叉驗證 (NYSE 交易日 18:00 ET)：對最近兩個已結束曆季執行 17 條鏈檢驗，
   只寫入 channel_check_log，不推播。
9. PR5 每日分析師 EPS 共識快照刷新 (NYSE 交易日 19:00 ET)：`EstimateSnapshotRunner` 以背景任務
   逐檔寫入當日快照（帶財期），供修正動能以財期配對 t-30d；上一輪未完成時略過。
10. PR5 估值與次日候選名單 (NYSE 交易日 20:00 ET)：`ValuationJobRunner` 以背景任務執行，19:00 快照
   刷新若仍在執行先等待其完成；只入庫、不推播（docs/valuation_pricing/06 未規範推播），
   供 /fa 與盤前簡報讀取。兩者皆逐檔複檢 leader 與 `is_memory_safe()`，單一標的例外隔離。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import config
import market_time
from database.fundamental_pipeline import get_pending_earnings_surprise_keys
from discord.ext import commands, tasks
from market_analysis.fundamental_pipeline.alt_data_metrics import completed_periods
from market_analysis.fundamental_pipeline.event_clock import (
    ClockJob,
    ClockJobRegistry,
    weekday_at,
)
from market_analysis.fundamental_pipeline.models import FilingEventRecord
from services.alt_data_service import alt_data_service
from services.bounded_cache import BoundedCache
from services.calendar_service import calendar_service
from services.earnings_surprise_service import EarningsSurpriseService
from services.filing_event_service import FilingEventService
from services.fundamental_clock_service import (
    EstimateSnapshotRunner,
    ValuationJobRunner,
)
from services.liquidity_service import run_liquidity_pipeline
from services.llm_service import is_memory_safe
from services.macro_surprise_service import process_macro_surprises
from services.sec_edgar_client import SecConfigError
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")

# PENDING 重試回看窗口：14 個日曆日約等於 10 個交易日；超過仍無實際值者不再重試
EARNINGS_PENDING_RETRY_LOOKBACK_DAYS: int = 14


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


def weekday_hourly_between(
    start_hour: int, end_hour: int, window_minutes: int = 10
) -> Callable[[datetime], bool]:
    """平日（週一至週五）`start_hour`–`end_hour` 點（含兩端）每個整點視窗內觸發。

    時鐘每 5 分鐘輪詢一次，視窗 10 分鐘確保每個整點至少命中一次；同一整點的
    重複命中由 ClockJob.cooldown_seconds 擋下。
    """

    def _checker(dt: datetime) -> bool:
        if dt.weekday() >= 5:
            return False
        return start_hour <= dt.hour <= end_hour and dt.minute < window_minutes

    return _checker


class SecFilingSyncRunner:
    """SEC 申報同步的背景執行器（單一實例、不重疊、設定錯誤只記一次）。

    - `trigger` 由 ClockJob 呼叫：再次確認 leader 與記憶體守衛後，以 `asyncio.Task`
      在背景執行 `sync_universe_filings` 並立即返回，不阻塞時鐘輪詢。
    - 上一輪仍在執行時略過本輪（防重疊）。
    - 缺少合規 `SEC_USER_AGENT`（`SecConfigError`）時只記一次 error，之後靜默略過。
    - 跨輪重用同一個 FilingEventService / SecEdgarClient（共用限速器與 CIK 快取），
      並與 `alt_data_service`（18:00 產業鏈檢驗的 SEC XBRL 端）共用同一個實例。
    - 8-K Item 2.02 事件交由 `EarningsSurpriseService.process_filing_event`（共用同一個
      SecEdgarClient 與限速器）；只入庫、不推播，例外由 FilingEventService 隔離成 warning。
    """

    def __init__(self, bot: Any | None = None) -> None:
        self._bot = bot
        self._service: FilingEventService | None = None
        self._earnings_service: EarningsSurpriseService | None = None
        self._task: asyncio.Task[None] | None = None
        self._config_error_logged = False

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _get_service(self) -> FilingEventService | None:
        if self._service is not None:
            return self._service
        # 與產業鏈檢驗（alt_data_service）共用同一個 SecEdgarClient：兩者可能在 18:00 同時
        # 打 SEC，各自 8 req/s 會超過 SEC 10 req/s 上限。誰先建立，另一方就沿用。
        service = FilingEventService(
            bot=self._bot,
            client=alt_data_service.sec_client,
            earnings_handler=self._handle_earnings_event,
        )
        try:
            client = await service.get_client()
        except SecConfigError as e:
            if not self._config_error_logged:
                logger.error(
                    f"[FundamentalPipeline] SEC 申報同步停用：{e} 設定 SEC_USER_AGENT 後重啟即可啟用；"
                    "在此之前每輪靜默略過，不再重複記錄。"
                )
                self._config_error_logged = True
            return None
        alt_data_service.attach_sec_client(client)
        # 財報預期差只入庫供 /fa 與估值使用，不傳 bot（docs/valuation_pricing/05 未規範推播）
        self._earnings_service = EarningsSurpriseService(sec_client=client)
        self._service = service
        return service

    async def _handle_earnings_event(self, event: FilingEventRecord) -> None:
        """8-K Item 2.02 事件 → 財報預期差與指引擷取（回填事件同樣處理，本路徑不推播）。"""
        earnings = self._earnings_service
        if earnings is None:
            return
        result = await earnings.process_filing_event(event)
        backfill_tag = "（回填）" if event.is_backfill else ""
        if result is None:
            logger.info(
                f"[FundamentalPipeline] {event.symbol} 財報事件{backfill_tag} {event.accession} "
                "未寫入預期差（財季推導失敗、無共識值或既有 PROCESSED）"
            )
        else:
            logger.info(
                f"[FundamentalPipeline] {event.symbol} 財報事件{backfill_tag} {event.accession} "
                f"已寫入 {result.fiscal_period} 預期差（{result.status}）"
            )

    async def trigger(self, now_et: datetime) -> bool:
        """啟動一輪背景同步；有啟動回傳 True，略過回傳 False。"""
        if self._bot is not None and not getattr(
            self._bot, "_is_leader_instance", False
        ):
            return False
        if self.is_running():
            logger.info(
                "[FundamentalPipeline] 上一輪 SEC 申報同步仍在執行，略過本輪 "
                f"({now_et:%Y-%m-%d %H:%M} ET)"
            )
            return False
        if not is_memory_safe():
            logger.warning(
                "[FundamentalPipeline] VPS 記憶體使用率超標，略過本輪 SEC 申報同步"
            )
            return False
        service = await self._get_service()
        if service is None:
            return False
        self._task = asyncio.create_task(
            self._run(service, now_et), name="fundamental_sec_filing_sync"
        )
        return True

    async def _run(self, service: FilingEventService, now_et: datetime) -> None:
        started = asyncio.get_running_loop().time()
        try:
            stats = await service.sync_universe_filings()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                f"[FundamentalPipeline] SEC 申報同步失敗 ({now_et:%Y-%m-%d %H:%M} ET)"
            )
            return
        elapsed = asyncio.get_running_loop().time() - started
        logger.info(
            f"[FundamentalPipeline] SEC 申報同步完成 ({now_et:%Y-%m-%d %H:%M} ET)，"
            f"耗時 {elapsed:.1f}s：{stats}"
        )

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


class EarningsPendingRetryRunner:
    """財報預期差 PENDING 重試（NYSE 交易日 17:30 ET）。

    BMO 財報 8-K 在盤前同步時，Finnhub 可能尚未更新實際 EPS，服務先寫成 PENDING。
    本執行器重算近 `EARNINGS_PENDING_RETRY_LOOKBACK_DAYS` 個日曆日內首次寫入、狀態仍為
    PENDING 的財季（`evaluate_symbol_surprise` 只呼叫 Finnhub，不呼叫 LLM）。
    leader-only 並通過 `is_memory_safe()`；單一標的例外只記 warning，不影響其他標的。
    """

    def __init__(
        self,
        bot: Any | None = None,
        service: EarningsSurpriseService | None = None,
    ) -> None:
        self._bot = bot
        self._service = service

    def _get_service(self) -> EarningsSurpriseService:
        if self._service is None:
            self._service = EarningsSurpriseService()
        return self._service

    async def run(self, now_et: datetime) -> dict[str, int]:
        stats = {"pending": 0, "processed": 0, "still_pending": 0, "failed": 0}
        if self._bot is not None and not getattr(
            self._bot, "_is_leader_instance", False
        ):
            return stats
        if not is_memory_safe():
            logger.warning(
                "[FundamentalPipeline] VPS 記憶體使用率超標，略過本輪財報預期差 PENDING 重試"
            )
            return stats

        since = now_et.astimezone(timezone.utc) - timedelta(
            days=EARNINGS_PENDING_RETRY_LOOKBACK_DAYS
        )
        try:
            keys = await asyncio.to_thread(get_pending_earnings_surprise_keys, since)
        except Exception:
            logger.exception("[FundamentalPipeline] 讀取 PENDING 財報預期差失敗")
            return stats
        stats["pending"] = len(keys)
        if not keys:
            return stats

        service = self._get_service()
        for symbol, fiscal_period in keys:
            try:
                result = await service.evaluate_symbol_surprise(symbol, fiscal_period)
            except Exception as e:
                stats["failed"] += 1
                logger.warning(
                    f"[FundamentalPipeline] {symbol} {fiscal_period} PENDING 重試失敗: {e!r}"
                )
                continue
            if result is not None and result.status == "PROCESSED":
                stats["processed"] += 1
            else:
                stats["still_pending"] += 1

        logger.info(
            f"[FundamentalPipeline] 財報預期差 PENDING 重試完成 ({now_et:%Y-%m-%d %H:%M} ET)：{stats}"
        )
        return stats


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


# 每次排程檢驗最近 N 個已結束曆季：較新一季多半仍在財報季（XBRL 尚未申報），
# 前一季通常已完整，可完成方向命中驗證。
CHANNEL_CHECK_PERIODS_PER_RUN = 2


async def _run_channel_check_job(now_et: datetime) -> None:
    """NYSE 交易日 18:00 ET 執行 17 條產業鏈交叉驗證並寫入 channel_check_log（不推播）。

    監控迴圈已做 leader 與記憶體守衛；此工作耗時較長（SEC 限速下約數十秒），
    每個期別開始前再檢查一次 `is_memory_safe()`。單條鏈的例外由
    `run_all_channel_checks` 隔離，單一期別失敗也不影響另一期別。
    """
    as_of = now_et.date()
    for period in completed_periods(as_of, CHANNEL_CHECK_PERIODS_PER_RUN):
        if not is_memory_safe():
            logger.warning(
                f"[FundamentalPipeline] 記憶體使用率超標，略過產業鏈檢驗 {period}"
            )
            return
        try:
            results = await SingleFlightManager.run(
                f"channel_check_job:{period}",
                alt_data_service.run_all_channel_checks,
                as_of_period=period,
                as_of=as_of,
                persist=True,
            )
            verdicts = [r.verdict for r in results]
            logger.info(
                f"[FundamentalPipeline] 產業鏈檢驗 {period} 完成：共 {len(verdicts)} 條，"
                f"共振確認 {verdicts.count('CONFIRM')}、背離 {verdicts.count('DIVERGE')}、"
                f"資料不足 {verdicts.count('INSUFFICIENT')}"
            )
        except Exception:
            logger.exception(f"[FundamentalPipeline] 產業鏈檢驗 {period} 執行失敗")


def register_default_fundamental_jobs(
    sec_sync_runner: SecFilingSyncRunner | None = None,
    earnings_retry_runner: EarningsPendingRetryRunner | None = None,
    snapshot_runner: EstimateSnapshotRunner | None = None,
    valuation_runner: ValuationJobRunner | None = None,
) -> None:
    """註冊預設之基礎事件時鐘任務（PR1 總經 / 流動性、PR2 SEC 申報同步、PR3 財報 PENDING 重試、
    PR4 產業鏈交叉驗證、PR5 共識快照刷新與估值候選名單）。

    `sec_sync_runner` 由 Cog 傳入（帶 bot 以便關閉乾跑後推播）；未提供時建立無 bot 的
    執行器（只入庫、不推播）。`earnings_retry_runner` / `snapshot_runner` / `valuation_runner`
    由 Cog 傳入（帶 bot 以複檢 leader）；未提供時建立無 bot 的執行器，且估值執行器會等待
    同一次註冊建立的快照執行器。
    """
    runner = sec_sync_runner if sec_sync_runner is not None else SecFilingSyncRunner()
    retry_runner = (
        earnings_retry_runner
        if earnings_retry_runner is not None
        else EarningsPendingRetryRunner()
    )
    snap_runner = (
        snapshot_runner if snapshot_runner is not None else EstimateSnapshotRunner()
    )
    val_runner = (
        valuation_runner
        if valuation_runner is not None
        else ValuationJobRunner(snapshot_runner=snap_runner)
    )
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

    ClockJobRegistry.register(
        ClockJob(
            job_id="sec_filing_sync_hourly",
            name="SEC 申報同步（Form 4 / 8-K 治理 / 13D）",
            schedule_desc="平日 07:00–20:00 ET 每整點（背景任務，不重疊）",
            handler=runner.trigger,
            is_due_fn=weekday_hourly_between(7, 20, window_minutes=10),
            priority=40,
            description="增量同步基本面標的池之 SEC 申報、內部人交易與治理旗標；8-K Item 2.02 觸發財報預期差",
            cooldown_seconds=1800.0,
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="earnings_pending_retry_1730",
            name="17:30 財報預期差 PENDING 重試",
            schedule_desc="NYSE 交易日 17:30 ET（近 14 日內仍為 PENDING 之財季）",
            handler=retry_runner.run,
            is_due_fn=nyse_trading_day_at(17, 30, window_minutes=15),
            priority=50,
            description="BMO 財報 Finnhub 實際值延遲更新時，收盤後重算預期差",
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="channel_check_1800",
            name="18:00 實體產業鏈交叉驗證",
            schedule_desc="NYSE 交易日 18:00 ET（每日一次，最近兩個已結束曆季）",
            handler=_run_channel_check_job,
            is_due_fn=nyse_trading_day_at(18, 0, window_minutes=15),
            priority=60,
            description="SEC XBRL / TSA / FRED / TWSE·TPEx 17 條產業鏈檢驗，只寫入 channel_check_log",
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="eps_estimate_snapshot_1900",
            name="19:00 分析師 EPS 共識快照刷新",
            schedule_desc="NYSE 交易日 19:00 ET（背景任務，不重疊）",
            handler=snap_runner.trigger,
            is_due_fn=nyse_trading_day_at(19, 0, window_minutes=30),
            priority=70,
            description=(
                "逐檔寫入當日分析師 EPS 共識快照（eps_estimate_snapshot，帶財期），"
                "供 20:00 修正動能以財期配對 t-30d（不推播）"
            ),
            cooldown_seconds=3600.0,
        )
    )

    ClockJobRegistry.register(
        ClockJob(
            job_id="fundamental_watch_candidate_2000",
            name="20:00 估值、修正動能與次日候選名單",
            schedule_desc="NYSE 交易日 20:00 ET（背景任務，不重疊；等待 19:00 快照刷新完成）",
            handler=val_runner.trigger,
            is_due_fn=nyse_trading_day_at(20, 0, window_minutes=30),
            priority=80,
            description=(
                "計算 DCF / Comps 安全邊際、修正動能與 PEAD，寫入 fair_value_log / "
                "revision_score_log / fundamental_watch_candidate（不推播）"
            ),
            cooldown_seconds=3600.0,
        )
    )


class FundamentalPipelineMonitorCog(commands.Cog):
    """基本面分析管線事件時鐘監控 Cog。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._dedup_cache = BoundedCache(max_size=300)
        self._sec_sync_runner = SecFilingSyncRunner(bot)
        self._earnings_retry_runner = EarningsPendingRetryRunner(bot)
        self._snapshot_runner = EstimateSnapshotRunner(bot)
        self._valuation_runner = ValuationJobRunner(
            bot, snapshot_runner=self._snapshot_runner
        )
        register_default_fundamental_jobs(
            self._sec_sync_runner,
            self._earnings_retry_runner,
            self._snapshot_runner,
            self._valuation_runner,
        )
        self.fundamental_clock_task.start()

    async def cog_unload(self) -> None:
        self.fundamental_clock_task.cancel()
        self._sec_sync_runner.cancel()
        self._snapshot_runner.cancel()
        self._valuation_runner.cancel()

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
