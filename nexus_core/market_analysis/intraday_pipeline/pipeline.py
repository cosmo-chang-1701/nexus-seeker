"""盤中量化掃描與對沖背景處理管道（IntradayScanPipeline）。"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from market_time import ny_tz, is_market_open
from models.schemas import WatchlistEvaluation

from market_analysis.signal_calculator import (
    calculate_dynamic_trading_signals,
    compute_deployed_tactical_value,
)
from market_analysis.option_guidance import (
    derive_watchlist_option_guidance,
    build_watchlist_option_plan,
)

from market_analysis.intraday_pipeline.evaluation import evaluate_watchlist_symbol


logger = logging.getLogger(__name__)

# 深度心跳對齊時間點 (每小時 :10/:40 ET)。
# 為什麼：避開 :00/:15/:30/:45 巡邏與 :02/:17/:32/:47 價量監測的尖峰，
# 且 :10/:40 時 edge 期權快照（約 30 分鐘一換）與 15m/30m K 棒皆已定案。
_PIPELINE_SLOT_MINUTES: tuple[int, ...] = (10, 40)
# 最短睡眠秒數：避免剛跑完仍落在同一個時間點分鐘內而重複執行。
_MIN_SLOT_SLEEP_SECONDS = 60.0


# 相鄰兩輪掃描開始時間的最小間隔（20 分鐘，即 30 分鐘週期的 2/3）。
# 為什麼：開盤後或重啟後第一輪不在 :10/:40 時槽上，若結束後直接取「下一個時槽」，
# 可能 1 分鐘內又跑一輪，使非 green 標的收到兩則相同的深度心跳 DM。
_MIN_SCAN_GAP_SECONDS = 20 * 60.0


def _seconds_until_next_slot(
    now_et: datetime,
    slot_minutes: tuple[int, ...] = _PIPELINE_SLOT_MINUTES,
    last_scan_start: Optional[datetime] = None,
    min_gap_seconds: float = _MIN_SCAN_GAP_SECONDS,
) -> float:
    """回傳距離下一個 :10/:40 時間點的秒數（最少 60 秒）。

    now_et 必須為 ET 時間。恰在時間點上時視為已過，取下一個時間點。
    若提供 last_scan_start（上一輪掃描開始時間，須為 aware datetime），
    則時槽另須不早於 last_scan_start + min_gap_seconds，避免背靠背掃描。
    """
    earliest = now_et
    if last_scan_start is not None:
        earliest = max(now_et, last_scan_start + timedelta(seconds=min_gap_seconds))
    base = earliest.replace(minute=0, second=0, microsecond=0)
    candidates = [base + timedelta(minutes=m) for m in slot_minutes]
    candidates.append(base + timedelta(hours=1, minutes=min(slot_minutes)))
    for slot in candidates:
        # 對 now_et 嚴格大於（恰在時槽上視為已過）；對間隔下限則允許等於。
        if slot > now_et and slot >= earliest:
            return max((slot - now_et).total_seconds(), _MIN_SLOT_SLEEP_SECONDS)
    return _MIN_SLOT_SLEEP_SECONDS  # pragma: no cover


class IntradayScanPipeline:
    """
    盤中量化掃描與對沖背景處理管道。
    每 30 分鐘執行一次，產出自選標的深度心跳與進場顧問通知。
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        # 上一輪掃描開始時間，供 _seconds_until_next_slot 計算最小間隔。
        self._last_scan_started: Optional[datetime] = None
        # 進場顧問 radar 補抓的併發上限 (比照 portfolio_monitor.py 的 Semaphore(3))。
        self._radar_fetch_sem = asyncio.Semaphore(3)
        # GEX 牆突破／跌破：同一輪內同一標的只計算一次，多位使用者共用結果。
        from services.wall_break_service import WallBreakMonitor

        self._wall_break_monitor = WallBreakMonitor()

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

    async def _load_tactical_option_positions_by_user(self) -> Dict[int, list]:
        """一次讀出全站未到期的 TRADE 部位並依 user_id 分組。

        `get_all_trade_positions()` 是全表掃描，過去在使用者迴圈內逐人呼叫等於
        U 次全表掃描；且它是同步 sqlite3，直接在 async 迴圈裡呼叫會阻塞 event
        loop。改為每輪只讀一次並丟到執行緒，符合本 repo 既有的
        O(U×S) → O(S) 去重慣例。

        讀取失敗回傳空 dict：曝險會低估、閘門變寬鬆，但不會中斷整輪掃描。
        """
        import database

        def _load() -> Dict[int, list]:
            grouped: Dict[int, list] = {}
            today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
            for pos in database.get_all_trade_positions():
                expiry = str(pos.get("expiry") or "")
                # get_all_trade_positions() 的 docstring 提醒呼叫端需自行確保過期
                # 合約已歸檔（本管線沒有先呼叫 get_all_portfolio()），故在此濾除。
                if expiry and expiry < today_str:
                    continue
                grouped.setdefault(int(pos.get("user_id", 0)), []).append(pos)
            return grouped

        try:
            return await asyncio.to_thread(_load)
        except Exception as e:
            logger.warning(f"讀取全站期權部位以計算戰術曝險失敗: {e}")
            return {}

    def _compute_user_tactical_exposure(
        self,
        user_id: int,
        *,
        spot_holdings: Any = None,
        option_positions: Any = None,
    ) -> float:
        """算出該使用者目前已部署的戰術（衛星）曝險，供資金退守的組合層限額使用。

        期權部位刻意用 `get_all_trade_positions()` 而非 `get_user_portfolio()`：
        後者開頭會呼叫 `archive_expired_portfolio_records()`，那是一個全表歸檔
        **寫入**，不該由這條唯讀的曝險統計路徑、更不該在每檔標的的迴圈裡觸發。

        `get_all_trade_positions()` 的 docstring 提醒呼叫端需自行確保過期合約已
        歸檔（本管線沒有先呼叫 `get_all_portfolio()`），因此這裡直接依 `expiry`
        濾掉已到期的合約。就算漏掉一兩筆，效果也只是高估曝險、使限額更保守，
        而且這些列不會被拿去打任何報價請求。

        任何查詢失敗一律回傳目前已累加的值（最差就是 0.0）：那只會讓閘門變寬鬆
        而非誤擋，且 rationale 會照實顯示已部署金額，數字不對看得出來。
        """
        import database

        try:
            rows = (
                spot_holdings
                if spot_holdings is not None
                else database.get_user_holdings(user_id)
            )
        except Exception as e:
            logger.warning(f"讀取現貨持倉以計算戰術曝險失敗 (uid={user_id}): {e}")
            rows = []

        if option_positions is None:
            option_positions = []
            try:
                today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
                for pos in database.get_all_trade_positions():
                    if int(pos.get("user_id", 0)) != int(user_id):
                        continue
                    expiry = str(pos.get("expiry") or "")
                    if expiry and expiry < today_str:
                        continue
                    option_positions.append(pos)
            except Exception as e:
                logger.warning(f"讀取期權部位以計算戰術曝險失敗 (uid={user_id}): {e}")

        return compute_deployed_tactical_value(
            spot_holdings=rows,
            option_positions=option_positions,
        )

    async def _build_watchlist_heartbeat_embed(
        self,
        evaluation: WatchlistEvaluation,
        user_context: Any,
        notif_settings: dict | None = None,
        deployed_tactical_value: float | None = None,
    ) -> Any:
        import database
        from cogs.embed_builder import create_watchlist_signal_embed

        # 註：此處原本會呼叫 ui.formatter.generate_ansi_watchlist_report() 產生完整
        # ANSI 報表並以 report_body 傳入 embed，但 create_watchlist_signal_embed()
        # 從未讀取該參數，等於白算一份報表。已一併移除。
        user_id = int(getattr(user_context, "user_id", 0))
        has_position = (
            database.is_symbol_in_portfolio(user_id, evaluation.metrics.symbol)
            if user_id
            else False
        )
        holding_row = None
        symbol_tags = []
        if user_id:
            from database.watchlist_tags import get_watchlist_tags

            symbol_tags = get_watchlist_tags(str(user_id), evaluation.metrics.symbol)
            holdings_rows = database.get_user_holdings(user_id)
            user_holdings = {
                str(row.get("symbol", "")).upper(): row for row in holdings_rows
            }
            holding_row = user_holdings.get(evaluation.metrics.symbol.upper())

            # 資金退守閘門的組合層限額需要「目前已部署的戰術曝險」。呼叫端
            # (_run_loop) 每位使用者只算一次並傳進來；未提供時就地補算，讓其他
            # 呼叫端與測試不必知道這個參數。
            deployed_tactical_value = (
                deployed_tactical_value
                if deployed_tactical_value is not None
                else self._compute_user_tactical_exposure(
                    user_id, spot_holdings=holdings_rows
                )
            )
        if deployed_tactical_value is None:
            deployed_tactical_value = 0.0

        holding_quantity = None
        holding_avg_cost = None
        holding_pnl_pct = None
        if holding_row is not None and float(holding_row.get("quantity", 0.0)) > 0.0:
            holding_quantity = float(holding_row["quantity"])
            holding_avg_cost = float(holding_row.get("avg_cost", 0.0))
            if holding_avg_cost > 0.0:
                current_px = evaluation.metrics.current_price
                holding_pnl_pct = (current_px - holding_avg_cost) / holding_avg_cost

        base_capital = float(
            getattr(
                user_context,
                "capital",
                getattr(user_context, "total_capital", 100000.0),
            )
        )
        user_capital = base_capital
        if user_id:
            try:
                from services.trading_service import get_adjusted_user_capital

                user_capital = await get_adjusted_user_capital(user_id, base_capital)
            except Exception:
                user_capital = base_capital
        user_risk_limit = float(getattr(user_context, "risk_limit", 15.0))

        has_upcoming_earnings = False
        if evaluation.event_context is not None:
            earnings_tte = getattr(evaluation.event_context, "earnings_tte_hours", None)
            if earnings_tte is not None and earnings_tte <= 7 * 24:
                has_upcoming_earnings = True

        # 計算動態買賣點現貨及對齊的期權操盤建議 (scale_atr_to_15m=True 修復 Top 4 量綱錯配)
        signals = calculate_dynamic_trading_signals(
            evaluation.metrics,
            evaluation.tactical,
            has_position=has_position,
            holding_quantity=holding_quantity,
            holding_avg_cost=holding_avg_cost,
            capital=user_capital,
            risk_limit=user_risk_limit,
            has_upcoming_earnings=has_upcoming_earnings,
            deployed_tactical_value=deployed_tactical_value,
            scale_atr_to_15m=True,
        )

        option_guidance = derive_watchlist_option_guidance(
            evaluation.metrics,
            evaluation.tactical,
            event_context=evaluation.event_context,
            has_position=has_position,
            suitable_buy_price=signals.get("suitable_buy_price"),
            suitable_sell_price=signals.get("suitable_sell_price"),
        )

        option_plan = await build_watchlist_option_plan(
            evaluation.metrics,
            evaluation.tactical,
            capital=user_capital,
            risk_limit=user_risk_limit,
            event_context=evaluation.event_context,
            has_position=has_position,
        )
        # 延遲匯入：測試以 patch("market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary")
        # 掛在套件層屬性上，模組層級 import 會凍結綁定而失效。
        from market_analysis.intraday_pipeline import (
            build_watchlist_skew_rule_commentary,
        )

        skew_commentary = build_watchlist_skew_rule_commentary(
            evaluation.metrics, evaluation.tactical
        )

        # 取得 embed 所需的補充數據（均已快取，額外開銷極低）
        from market_analysis.sentiment_engine import SentimentEngine

        hb_symbol = evaluation.metrics.symbol
        is_hedging = hb_symbol.upper() in ["BOXX", "BIL"]
        hb_uoa_fetched_ok = False
        # 註：此處原本另外抓一次 get_quote() 傳入 embed 的 quote 參數，但該參數
        # 從未被讀取（現價一律取自 evaluation.metrics.current_price），純屬多餘的
        # 網路呼叫，已一併移除。
        try:
            if is_hedging:
                hb_iv_metrics = None
                hb_pcr_data = None
                hb_uoa_list: list[dict[str, Any]] = []
                hb_max_pain = None
            else:
                results = await asyncio.gather(
                    SentimentEngine.fetch_and_calculate_iv_metrics(hb_symbol),
                    SentimentEngine.calculate_pcr(hb_symbol),
                    SentimentEngine.detect_uoa(hb_symbol),
                    SentimentEngine.get_unified_max_pain(hb_symbol),
                    return_exceptions=True,
                )
                r_iv, r_pcr, r_uoa, r_mp = results

                if isinstance(r_iv, BaseException):
                    logger.warning(f"[{hb_symbol}] 心跳 IV 指標取得失敗: {r_iv}")
                    hb_iv_metrics = None
                else:
                    hb_iv_metrics = r_iv

                if isinstance(r_pcr, BaseException):
                    logger.warning(f"[{hb_symbol}] 心跳 PCR 數據取得失敗: {r_pcr}")
                    hb_pcr_data = None
                else:
                    hb_pcr_data = r_pcr

                if isinstance(r_uoa, BaseException):
                    logger.warning(f"[{hb_symbol}] 心跳 UOA 數據取得失敗: {r_uoa}")
                    hb_uoa_list = []
                    hb_uoa_fetched_ok = False
                else:
                    hb_uoa_list = r_uoa if isinstance(r_uoa, list) else []
                    hb_uoa_fetched_ok = True

                if isinstance(r_mp, BaseException):
                    logger.warning(f"[{hb_symbol}] 心跳 Max Pain 取得失敗: {r_mp}")
                    hb_max_pain = None
                else:
                    hb_max_pain = r_mp
        except Exception as sup_err:
            logger.warning(f"[{hb_symbol}] 心跳補充數據取得非預期失敗: {sup_err}")
            hb_iv_metrics, hb_pcr_data, hb_uoa_list, hb_max_pain = (
                None,
                None,
                [],
                None,
            )

        if hb_uoa_fetched_ok:
            # 心跳已花代價算好 UOA (併發抓多個到期日期權鏈)，寫回 /x 終端
            # 共用的 kv_cache，避免 /x 對同一標的重複觸發昂貴的自癒偵測。
            # 快取寫入失敗僅記錄警告，不影響本次心跳 embed 的正常組裝。
            try:
                from database.cache import save_kv_cache

                await save_kv_cache(f"uoa_{hb_symbol.upper()}", hb_uoa_list)
            except Exception as cache_err:
                logger.warning(f"[{hb_symbol}] UOA 快取寫回失敗: {cache_err}")

            # 同一份資料另存一份**可回看的歷史**：上面的 kv_cache 是
            # ON CONFLICT DO UPDATE 的 upsert，每輪覆蓋前一輪，結構上無法回答
            # 「最近 5 個交易日有沒有出現過機構買盤」。右側條件四的 Regime III-B
            # 時間窗 (見 opportunity_cost.py::_confirm_entry_condition4_uoa_dte)
            # 需要歷史，故另寫 uoa_history。兩者共用同一份 hb_uoa_list，
            # 不產生任何額外的期權鏈抓取成本。
            try:
                from database.uoa_history import save_uoa_observations
                from market_analysis.evaluation_recorder import current_bar_ts

                await save_uoa_observations(hb_symbol, current_bar_ts(), hb_uoa_list)
            except Exception as hist_err:
                logger.warning(f"[{hb_symbol}] UOA 歷史寫入失敗: {hist_err}")

        embed = create_watchlist_signal_embed(
            symbol=hb_symbol,
            option_guidance=option_guidance,
            event_risk_summary=(
                evaluation.event_context.summary
                if evaluation.event_context is not None
                else "未偵測到近期重大事件"
            ),
            # 只傳型態字串：數值與分位由 embed builder 用它既有的
            # skew_val_str / skew_per_str 安全格式化（option_skew 是
            # Optional[float]，在此處直接 f"{...:+.2f}" 會在缺資料時拋
            # TypeError，被逐檔 except 吞掉後整封心跳都會消失）。
            skew_state=evaluation.metrics.option_skew_state,
            alert_level=evaluation.tactical.alert_level,
            option_plan=option_plan,
            skew_commentary=skew_commentary,
            has_position=has_position,
            holding_quantity=holding_quantity,
            holding_avg_cost=holding_avg_cost,
            holding_pnl_pct=holding_pnl_pct,
            suitable_buy_price=signals.get("suitable_buy_price"),
            suitable_buy_shares=signals.get("suitable_buy_shares"),
            suitable_sell_price=signals.get("suitable_sell_price"),
            suitable_sell_shares=signals.get("suitable_sell_shares"),
            buy_rationale=signals.get("buy_rationale"),
            sell_rationale=signals.get("sell_rationale"),
            toggles=notif_settings,
            metrics=evaluation.metrics,
            iv_metrics=hb_iv_metrics,
            max_pain_data=hb_max_pain if isinstance(hb_max_pain, dict) else None,
            pcr_data=hb_pcr_data if isinstance(hb_pcr_data, dict) else None,
            uoa_list=hb_uoa_list if isinstance(hb_uoa_list, list) else None,
            symbol_gex=evaluation.symbol_gex,
            symbol_tags=symbol_tags,
        )

        if embed is not None:
            setattr(embed, "_view", f"WatchlistHeartbeatView:{hb_symbol}")
        return embed

    async def _resolve_candidate_radar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """取得進場確認所需的 radar dict（quote / gex_profile_data / uoa / psq_result）。

        沿用 cogs/trading/portfolio_monitor.py 的既有取得模式：先看共享快取
        (`bot._latest_radar_data_cache`，300 秒保鮮窗)，命中就零額外網路；
        未命中才經 UnifiedTerminalCog 補抓，且必須走 `self._radar_fetch_sem`。

        ⚠️ 共享快取原本由 15 分鐘自選雷達寫入，該雷達推播已移除，
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
        """自選標的進場顧問：進場條件成立時（右側為多時間框架擠壓，左側／做空為六重鐵律），
        獨立推播一則進場 DM（含價位建議）。

        與 30 分鐘心跳本體**完全獨立**：不新增 `scenario`、不覆寫 `tactical`、
        不受其他訊號約束，只受 `/notif_settings` 的
        `advisory_entry_signal` 控制。策略分派與 `/x` 進場鐵律頁籤一致
        (見 entry_advisor.py)。

        閘門依序（便宜的先擋；**任何一道擋下都不燒去重旗標**）：
          1. 心跳非 green（SHIELD／Backwardation／premium-harvest）→ 不進場。
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
            from market_analysis.dynamic_rollover.models import TradingStrategyMode
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
                # 擠壓規則只需要 K 線；左側／做空鐵律才依賴雷達的 GEX 資料。
                if strategy in (
                    TradingStrategyMode.LEFT_SIDE.value,
                    TradingStrategyMode.SHORT_SIDE.value,
                ):
                    return
                radar = {}
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

            is_squeeze = advice.tier is not None
            dry_run_tag: Optional[str] = None
            if config.WATCHLIST_ADVISOR_DRY_RUN:
                dry_run_tag = "WatchlistAdvisor"
            elif is_squeeze and config.SQUEEZE_ENTRY_DRY_RUN:
                dry_run_tag = "SqueezeEntry"
            elif advice.direction == "SHORT" and config.SHORT_ENTRY_DRY_RUN:
                dry_run_tag = "ShortEntry"
            if dry_run_tag is not None:
                logger.info(
                    f"[{dry_run_tag}][DryRun] 略過進場顧問推播 (僅記錄前向紀錄，"
                    f"不燒去重旗標): user={user_id} symbol={ticker.upper()} "
                    f"strategy={advice.strategy} regime={advice.regime}"
                )
                return

            # 擠壓路徑以等級入鍵：同日 T1 升級為 T3 是新的、更強的訊號。
            signal_key = (
                f"SQZ_T{advice.tier}"
                if is_squeeze
                else (advice.regime or advice.strategy)
            )
            cache_key = (
                f"advisory_entry_{user_id}_{ticker.upper()}_"
                f"{signal_key}_{now_ny.strftime('%Y%m%d')}"
            )
            if database.get_kv_cache(cache_key):
                return

            from cogs.embed_builders.portfolio_embeds import create_entry_rules_embed
            from cogs.embed_builders.squeeze_entry_embeds import (
                create_squeeze_entry_embed,
            )

            if advice.squeeze is not None:
                embed = create_squeeze_entry_embed(
                    ticker.upper(),
                    advice.squeeze,
                    passed=advice.passed,
                    reason=advice.reason,
                    entry_price=advice.entry_price,
                    stop_loss=advice.stop_loss,
                    trading_strategy=advice.strategy,
                    dynamic_regime=advice.regime,
                )
            else:
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

    async def _dispatch_wall_break_alerts(
        self,
        user_id: int,
        ticker: str,
        watchlist_eval: Optional[WatchlistEvaluation],
    ) -> None:
        """GEX 牆突破／跌破推播（通道 alpha_wall_break）。

        與心跳、進場顧問完全獨立。資料蒐集（錨點、事件、T+1 結算）一律執行，
        即使推播開關關閉——事件表是前向蒐集紀錄。只推真突破：盤中 CANDIDATE 與
        T+1 CONFIRMED；FAKE／UNDETERMINED 只記錄不推播。任何失敗只記錄警告。
        """
        try:
            import config

            outcome = await self._wall_break_monitor.process_symbol(
                ticker, watchlist_eval
            )
            if not config.WALL_BREAK_ALERT_ENABLED:
                return
            if not outcome.intraday and not outcome.t1:
                return

            from cogs.embed_builders.wall_break_embeds import (
                create_wall_break_embed,
                create_wall_break_t1_embed,
            )
            from services.notification_dispatch_recorder import DispatchRecord
            from services.notification_dispatcher import notify

            sym = ticker.upper()
            for result in outcome.intraday:
                if result.verdict != "CANDIDATE":
                    continue
                try:
                    trig = result.trigger
                    await notify(
                        self.bot,
                        user_id,
                        "alpha_wall_break",
                        embed=create_wall_break_embed(sym, result),
                        dedup_key=(
                            f"wall_break_{user_id}_{sym}_{trig.direction}_"
                            f"{trig.trigger}_{trig.bar_time.strftime('%Y%m%d')}"
                        ),
                        record=DispatchRecord(
                            symbol=sym,
                            signal_kind="INFO",
                            scenario=result.label,
                            action=f"WALL_BREAK_{trig.trigger}",
                            direction=(
                                "LONG" if trig.direction == "BREAKOUT" else "SHORT"
                            ),
                            price=trig.close,
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[{sym}] 牆突破盤中推播失敗 (uid={user_id}): {e}")

            for row, ev, t1 in outcome.t1:
                if t1.verdict != "CONFIRMED":
                    continue
                try:
                    event_date = str(row["trade_date"])
                    await notify(
                        self.bot,
                        user_id,
                        "alpha_wall_break",
                        embed=create_wall_break_t1_embed(sym, event_date, ev, t1),
                        dedup_key=(
                            f"wall_break_t1_{user_id}_{sym}_{ev.direction}_"
                            f"{event_date.replace('-', '')}"
                        ),
                        record=DispatchRecord(
                            sym,
                            "INFO",
                            scenario=f"{t1.label}_T1",
                            action="WALL_BREAK_T1_CLOSE_BEYOND",
                            direction="LONG" if ev.direction == "BREAKOUT" else "SHORT",
                            price=(
                                watchlist_eval.metrics.current_price
                                if watchlist_eval is not None
                                else None
                            ),
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[{sym}] 牆突破 T+1 推播失敗 (uid={user_id}): {e}")
        except Exception as e:
            logger.warning(f"[{ticker}] 牆突破派發失敗 (uid={user_id}): {e}")

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

                # 1. 取得當下美東時間與交易時段 phase
                now_ny = datetime.now(ZoneInfo("America/New_York"))

                # 檢查美股是否開盤
                market_active = is_market_open()

                # 計算當前 Phase
                phase = "Closed"
                if market_active:
                    # 獲取今日開收盤時間
                    import pandas_market_calendars as mcal
                    from datetime import timedelta

                    nyse_calendar = mcal.get_calendar("NYSE")
                    schedule = nyse_calendar.schedule(
                        start_date=now_ny.date(), end_date=now_ny.date()
                    )

                    if not schedule.empty:
                        row = schedule.iloc[0]
                        market_open = (
                            row["market_open"].tz_convert(ny_tz).to_pydatetime()
                        )
                        market_close = (
                            row["market_close"].tz_convert(ny_tz).to_pydatetime()
                        )

                        phase_a_end = market_open + timedelta(hours=1)
                        phase_c_start = market_close - timedelta(hours=1)

                        if market_open <= now_ny < phase_a_end:
                            phase = "Phase A"
                        elif phase_a_end <= now_ny < phase_c_start:
                            phase = "Phase B"
                        elif phase_c_start <= now_ny <= market_close:
                            phase = "Phase C"

                if phase == "Closed":
                    # 休市時，每 10 分鐘檢查一次
                    logger.info(
                        "市場已休市或尚未開盤。IntradayScanPipeline 進入待機..."
                    )
                    await asyncio.sleep(600)
                    continue

                # AGENTS.md §4：盤中背景迴圈一律受 85% RAM 閘門約束。本管線每輪
                # 對每位使用者的每檔自選標的抓報價、期權鏈、GEX 並組裝 embed，
                # 記憶體吃緊時整輪略過，下一輪 (下一個 :10/:40) 再試。
                from services.llm_service import is_memory_safe

                if not is_memory_safe():
                    logger.warning(
                        "🤖 [Intraday Pipeline] 記憶體水位過高 (RAM+Swap > 85%)，"
                        "跳過本輪掃描。"
                    )
                    await asyncio.sleep(
                        _seconds_until_next_slot(
                            datetime.now(ny_tz), last_scan_start=self._last_scan_started
                        )
                    )
                    continue

                self._last_scan_started = datetime.now(ny_tz)
                self._wall_break_monitor.begin_cycle()
                logger.info(
                    f"🤖 [Intraday Pipeline] 開盤心跳監測觸發。當前時段: {phase}"
                )

                # 2. 獲取所有使用者資訊，執行量化分析
                import database

                user_ids = database.get_all_user_ids()
                # 全站 TRADE 部位每輪只讀一次（非阻塞），供各使用者的戰術曝險統計取用。
                trades_by_user = await self._load_tactical_option_positions_by_user()

                for uid in user_ids:
                    ctx = database.get_full_user_context(uid)
                    if getattr(ctx, "load_failed", False) is True:
                        logger.error(
                            f"[Intraday Pipeline] 使用者 {uid} 帳戶資料讀取失敗，本輪略過心跳推播"
                        )
                        continue
                    # 掃描 watchlist 中的標的
                    # 戰術曝險每位使用者每輪只算一次（含一次全站 TRADE 讀取），
                    # 不在每檔標的的迴圈裡重複查詢。
                    user_tactical_exposure = self._compute_user_tactical_exposure(
                        uid, option_positions=trades_by_user.get(uid, [])
                    )

                    watchlist = database.get_user_watchlist(uid)
                    for ticker, _ in watchlist:
                        try:
                            watchlist_eval = await self.evaluate_watchlist_symbol(
                                ticker
                            )
                            if (
                                watchlist_eval is not None
                                and watchlist_eval.tactical.alert_level != "green"
                            ):
                                # 這條深度心跳有自己的通知通道 (heartbeat_symbol_deep)；
                                # 原本並存的 15 分鐘批次雷達推播已移除。
                                # 一次讀取整份設定（走每使用者快取），不再對同一個
                                # 開關各讀一次 is_notification_enabled 與完整 dict
                                notif_settings = (
                                    database.get_user_notification_settings(uid)
                                )
                                hb_enabled = notif_settings.get(
                                    "heartbeat_symbol_deep", True
                                )

                                if hb_enabled:
                                    embed = await self._build_watchlist_heartbeat_embed(
                                        watchlist_eval,
                                        ctx,
                                        notif_settings,
                                        deployed_tactical_value=user_tactical_exposure,
                                    )
                                    if embed is not None:
                                        from services.notification_dispatcher import (
                                            notify,
                                        )

                                        await notify(
                                            self.bot,
                                            uid,
                                            "heartbeat_symbol_deep",
                                            embed=embed,
                                        )
                                else:
                                    logger.info(
                                        f"使用者 {uid} 已關閉所有心跳模組訂閱，略過心跳推送。"
                                    )
                            # 進場顧問獨立於心跳本體：心跳是否推播（通道關閉、綠燈）
                            # 都不影響這裡，每檔標的每輪一律評估一次。
                            await self._dispatch_entry_advisor_alert(
                                uid, ticker, watchlist_eval, ctx, now_ny
                            )
                            await self._dispatch_wall_break_alerts(
                                uid, ticker, watchlist_eval
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

                # 4. 睡眠至下一個 :10/:40 時間點
                await asyncio.sleep(
                    _seconds_until_next_slot(
                        datetime.now(ny_tz), last_scan_start=self._last_scan_started
                    )
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ IntradayScanPipeline 發生錯誤: {e}", exc_info=True)
                await asyncio.sleep(60)
