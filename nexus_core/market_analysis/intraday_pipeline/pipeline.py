"""盤中自選標的進場顧問管道（IntradayScanPipeline）。

每 30 分鐘執行一次，只做一件事：替開啟 `/notif_settings` 的
`advisory_entry_signal` 的使用者，對其自選標的跑進場顧問（六重鐵律）並推播
通過者，同時留下 WATCHLIST_ADVISOR 前向蒐集紀錄。

過去這條管線還會推播深度心跳 (`heartbeat_symbol_deep`)、執行 Gamma Squeeze
引擎 (SPEAR)，並寫入 `uoa_{SYM}` kv 快取與 `uoa_history`；這些都已隨「盤中情報」
模組一併移除（見 docs/architecture/01_dual_watchlist_pipelines.md）。
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from market_time import is_market_open
from models.schemas import WatchlistEvaluation

from market_analysis.intraday_pipeline.evaluation import evaluate_watchlist_symbol


logger = logging.getLogger(__name__)


class IntradayScanPipeline:
    """
    盤中自選標的進場顧問管道。
    每 30 分鐘執行一次，僅對開啟進場顧問通知的使用者評估其自選標的。
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.scan_interval_seconds = 30 * 60  # 30 minutes
        # 進場顧問 radar 補抓的併發上限 (比照 portfolio_monitor.py 的 Semaphore(3))。
        self._radar_fetch_sem = asyncio.Semaphore(3)

    def start(self) -> None:
        """啟動異步監控管道"""
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("✅ IntradayScanPipeline 異步掃描管道啟動。")

    def stop(self) -> None:
        """停止異步監控管道"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            logger.info("🛑 IntradayScanPipeline 異步掃描管道停止。")

    async def evaluate_watchlist_symbol(
        self, symbol: str
    ) -> Optional[WatchlistEvaluation]:
        return await evaluate_watchlist_symbol(symbol)

    async def _resolve_candidate_radar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """取得進場確認所需的 radar dict（quote / gex_profile_data / uoa / psq_result）。

        沿用 cogs/trading/portfolio_monitor.py 的既有取得模式：先看共享快取
        (`bot._latest_radar_data_cache`，300 秒保鮮窗)，命中就零額外網路；
        未命中才經 UnifiedTerminalCog 補抓，且必須走 `self._radar_fetch_sem`。

        ⚠️ 共享快取原本由 15 分鐘自選雷達寫入，該雷達已隨「盤中情報」模組移除，
        目前沒有寫入端——fallback 補抓是常態而非例外，Semaphore 不可省。讀取端
        保留，日後若有其他迴圈寫入快取可直接受益。
        取不到 (cog 缺失／抓取失敗) 一律回 None，呼叫端視為本輪略過 (fail-safe)。
        """
        import time

        from market_analysis.intraday_pipeline.entry_advisor import (
            _RADAR_FRESH_WINDOW_SECONDS,
        )

        sym = symbol.upper()
        shared_cache = getattr(self.bot, "_latest_radar_data_cache", {}) or {}
        shared_time = float(getattr(self.bot, "_latest_radar_cache_time", 0.0) or 0.0)
        if (time.time() - shared_time) < _RADAR_FRESH_WINDOW_SECONDS:
            cached = shared_cache.get(sym)
            if isinstance(cached, dict):
                return cached

        terminal_cog = self.bot.get_cog("UnifiedTerminalCog")
        if terminal_cog is None:
            return None
        async with self._radar_fetch_sem:
            try:
                data = await terminal_cog._fetch_sym_radar_data_slow(sym)
            except Exception as e:
                logger.warning(f"[{sym}] 進場顧問 radar 補抓失敗: {e}")
                return None
        return data if isinstance(data, dict) else None

    async def _dispatch_entry_advisor_alert(
        self,
        user_id: int,
        ticker: str,
        watchlist_eval: Optional[WatchlistEvaluation],
        ctx: Any,
        now_ny: datetime,
    ) -> None:
        """自選標的進場顧問：六重鐵律通過時，獨立推播一則進場 DM（含價位建議）。

        不新增 `scenario`、不覆寫 `tactical`，只受 `/notif_settings` 的
        `advisory_entry_signal` 控制。策略分派與 `/x` 進場鐵律頁籤一致
        (見 entry_advisor.py)。

        閘門依序（便宜的先擋；**任何一道擋下都不燒去重旗標**）：
          1. 自選評估非 green（SHIELD／Backwardation／premium-harvest）→ 不進場。
             左側收租與右側追價矛盾，防禦訊號也不該被進場訊號蓋過。
          2. 通知頻道。
          3. radar → 進場確認（乾跑期間照常記錄前向蒐集）。
          4. 三個乾跑旗標：本路徑不經 portfolio_monitor 的派發迴圈，該處的乾跑
             閘門對此完全失效，必須在此自行檢查。乾跑**不寫**去重旗標。
          5. 每人每標的每 Regime 每日去重（III-B 升級為 III 是新的、更強的訊號）。

        任何一步失敗都只記錄警告，不影響同一輪其他標的的處理。
        """
        try:
            if watchlist_eval is None or watchlist_eval.tactical.alert_level != "green":
                return

            import config
            import database
            from market_analysis import evaluation_recorder
            from market_analysis.dynamic_rollover.models import (
                DynamicRegime,
                TradingStrategyMode,
            )
            from market_analysis.intraday_pipeline.entry_advisor import (
                evaluate_entry_advice,
            )

            if not database.is_notification_enabled(user_id, "advisory_entry_signal"):
                return

            strategy = (
                str(getattr(ctx, "trading_strategy", "") or "")
                or TradingStrategyMode.RIGHT_SIDE.value
            )

            radar = await self._resolve_candidate_radar(ticker)
            if not radar:
                return
            quote = radar.get("quote")
            spot = 0.0
            if isinstance(quote, dict):
                try:
                    spot = float(quote.get("c", 0.0) or 0.0)
                except (TypeError, ValueError):
                    spot = 0.0
            if spot <= 0:
                spot = float(watchlist_eval.metrics.current_price)

            # 前向蒐集：乾跑期間也照常記錄，那正是觀察期的資料來源。獨立的 source
            # 才不會與 PORTFOLIO_MONITOR / SYMBOL_VIEW 的同 bar 紀錄互相覆蓋。
            token = evaluation_recorder.set_evaluation_source("WATCHLIST_ADVISOR")
            try:
                advice = await evaluate_entry_advice(
                    strategy,
                    ticker,
                    radar,
                    spot,
                    phase1_price=float(watchlist_eval.metrics.buy_price_phase1),
                )
            finally:
                evaluation_recorder.reset_evaluation_source(token)

            if not advice.passed:
                return

            is_iii_b = (
                advice.regime == DynamicRegime.REGIME_III_B_TREND_CONTINUATION.value
            )
            dry_run_tag: Optional[str] = None
            if config.WATCHLIST_ADVISOR_DRY_RUN:
                dry_run_tag = "WatchlistAdvisor"
            elif is_iii_b and config.REGIME_III_B_DRY_RUN:
                dry_run_tag = "RegimeIIIB"
            elif advice.direction == "SHORT" and config.SHORT_ENTRY_DRY_RUN:
                dry_run_tag = "ShortEntry"
            if dry_run_tag is not None:
                logger.info(
                    f"[{dry_run_tag}][DryRun] 略過進場顧問推播 (僅記錄前向紀錄，"
                    f"不燒去重旗標): user={user_id} symbol={ticker.upper()} "
                    f"strategy={advice.strategy} regime={advice.regime}"
                )
                return

            cache_key = (
                f"advisory_entry_{user_id}_{ticker.upper()}_"
                f"{advice.regime or advice.strategy}_{now_ny.strftime('%Y%m%d')}"
            )
            if database.get_kv_cache(cache_key):
                return

            from cogs.embed_builders.portfolio_embeds import create_entry_rules_embed

            embed = create_entry_rules_embed(
                ticker.upper(),
                advice.passed,
                advice.reason.split(" | ") if advice.reason else [],
                trading_strategy=advice.strategy,
                dynamic_regime=advice.regime,
                dynamic_regime_reason=advice.regime_reason,
                structure_directive=advice.structure_directive,
                entry_price=advice.entry_price,
                stop_loss=advice.stop_loss,
                target=advice.target,
                rr_ratio=advice.rr_ratio,
            )
            from services.notification_dispatcher import notify
            from services.notification_dispatch_recorder import DispatchRecord

            await notify(
                self.bot,
                user_id,
                "advisory_entry_signal",
                embed=embed,
                dedup_key=cache_key,
                record=DispatchRecord(
                    symbol=ticker.upper(),
                    signal_kind="ENTRY",
                    scenario=str(advice.regime or advice.strategy or ""),
                    action="ENTRY_ADVICE",
                    direction="SHORT" if advice.direction == "SHORT" else "LONG",
                    price=advice.entry_price,
                ),
            )
        except Exception as e:
            logger.warning(f"[{ticker}] 進場顧問派發失敗 (uid={user_id}): {e}")

    async def _run_loop(self) -> None:
        while self.is_running:
            try:
                # 只有 leader 實例才推播，避免多實例部署重複發送 DM。
                # 這裡逐輪檢查而非在建構時檢查：cog 於 setup_hook 載入時
                # leader 尚未選舉（bot.py 於 on_ready 才決定），且 leader 身分
                # 會隨 _leader_lock_loop 在執行期間變動。
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(600)
                    continue

                if not is_market_open():
                    # 休市時，每 10 分鐘檢查一次
                    logger.info(
                        "市場已休市或尚未開盤。IntradayScanPipeline 進入待機..."
                    )
                    await asyncio.sleep(600)
                    continue

                from services.llm_service import is_memory_safe

                if not is_memory_safe():
                    logger.warning(
                        "🤖 [Intraday Pipeline] 記憶體水位過高 (RAM+Swap > 85%)，"
                        "跳過本輪進場顧問評估。"
                    )
                    await asyncio.sleep(self.scan_interval_seconds)
                    continue

                now_ny = datetime.now(ZoneInfo("America/New_York"))
                logger.info("🤖 [Intraday Pipeline] 盤中進場顧問評估觸發。")

                import database

                # 自選標的的評估結果與使用者無關，同一輪多位使用者自選同一標的時
                # 只評估一次（評估本身含報價、GEX、期權鏈等網路 I/O）。
                evaluations: Dict[str, Optional[WatchlistEvaluation]] = {}

                for uid in database.get_all_user_ids():
                    # 先查開關再評估：沒開進場顧問的使用者不付任何評估成本。
                    # 前向蒐集紀錄本來就只在通知開啟時才會產生，行為不變。
                    if not database.is_notification_enabled(
                        uid, "advisory_entry_signal"
                    ):
                        continue

                    ctx = database.get_full_user_context(uid)
                    watchlist = database.get_user_watchlist(uid)
                    for ticker, _ in watchlist:
                        try:
                            key = ticker.upper()
                            if key not in evaluations:
                                evaluations[key] = await self.evaluate_watchlist_symbol(
                                    ticker
                                )
                            await self._dispatch_entry_advisor_alert(
                                uid, ticker, evaluations[key], ctx, now_ny
                            )
                        except Exception as ticker_err:
                            logger.error(
                                f"❌ IntradayScanPipeline 處理標的 {ticker} 時發生錯誤: {ticker_err}",
                                exc_info=True,
                            )

                # 進場顧問的前向蒐集紀錄於整個週期結束後一次批次寫入。
                from market_analysis import evaluation_recorder

                await evaluation_recorder.flush_evaluations()
                from services.notification_dispatch_recorder import (
                    flush_dispatch_records,
                )

                await flush_dispatch_records()

                # 睡眠 30 分鐘
                await asyncio.sleep(self.scan_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ IntradayScanPipeline 發生錯誤: {e}", exc_info=True)
                await asyncio.sleep(60)
