"""盤中量化掃描與對沖背景處理管道（IntradayScanPipeline）。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from market_time import ny_tz, is_market_open
from models.schemas import WatchlistEvaluation

from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
)
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.signal_calculator import (
    calculate_dynamic_trading_signals,
    compute_deployed_tactical_value,
)
from market_analysis.option_guidance import (
    derive_watchlist_option_guidance,
    build_watchlist_option_plan,
)

from market_analysis.intraday_pipeline.metrics import _WATCHLIST_METRICS_CACHE
from market_analysis.intraday_pipeline.evaluation import evaluate_watchlist_symbol


logger = logging.getLogger(__name__)


class IntradayScanPipeline:
    """
    盤中量化掃描與對沖背景處理管道。
    每 30 分鐘執行一次，驅動 Squeeze 決策引擎並發送通知。
    """

    def __init__(self, bot: Any, engine: NexusGammaSqueezeEngine):
        self.bot = bot
        self.engine = engine
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.scan_interval_seconds = 30 * 60  # 30 minutes
        self._cached_spy_spot: float = 500.0

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

    async def _dispatch_gamma_squeeze_alert(
        self,
        user_id: int,
        ticker: str,
        output: Any,
        now_ny: datetime,
    ) -> None:
        """把 NexusGammaSqueezeEngine 的輸出實際推播出去。

        過去 `analyze_ticker()` 的回傳值被 `_ =` 直接丟棄，整段引擎等於白跑。

        派發策略刻意保守：
          - **只在 SPEAR 時推播**。SHIELD / WAIT 是「不要動」的結論，每 30 分鐘
            對每檔標的重述一次只會製造噪音；SPEAR 才是需要使用者採取行動的訊號。
          - 走既有的 `alpha_market_signals` 通知通道（🎯 Alpha 策略與情報）。
          - 每位使用者、每檔標的、每日至多一則，沿用其他警報相同的 kv_cache
            去重鍵模式，避免同一輪行情在盤中被重複觸發 13 次。

        任何一步失敗都只記錄警告，不影響同一輪其他標的的處理。
        """
        try:
            if getattr(output, "sddm_route", None) != "SPEAR":
                return

            import database

            if not database.is_notification_enabled(user_id, "alpha_market_signals"):
                return

            cache_key = (
                f"gamma_squeeze_alert_{user_id}_{ticker.upper()}_"
                f"{now_ny.strftime('%Y%m%d')}"
            )
            if database.get_kv_cache(cache_key):
                return

            from cogs.embed_builders.alert_embeds import (
                create_gamma_squeeze_alert_embed,
            )

            embed = create_gamma_squeeze_alert_embed(output)
            await self.bot.queue_dm(user_id, embed=embed)
            await database.save_kv_cache(cache_key, True)
        except Exception as e:
            logger.warning(
                f"[{ticker}] Gamma Squeeze SPEAR 警報派發失敗 (uid={user_id}): {e}"
            )

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

                logger.info(
                    f"🤖 [Intraday Pipeline] 開盤心跳監測觸發。當前時段: {phase}"
                )

                # 預先抓取 SPY 現價供跨資產對沖價格比率計算
                try:
                    from services.market_data_service import get_quote

                    spy_q = await get_quote("SPY")
                    if spy_q and spy_q.get("c"):
                        self._cached_spy_spot = float(spy_q["c"])
                except Exception:
                    pass

                # 2. 獲取所有使用者資訊，執行量化分析
                import database

                user_ids = database.get_all_user_ids()
                # 全站 TRADE 部位每輪只讀一次（非阻塞），供各使用者的戰術曝險統計取用。
                trades_by_user = await self._load_tactical_option_positions_by_user()

                for uid in user_ids:
                    ctx = database.get_full_user_context(uid)
                    # `enable_analyst_agent` 只管 NexusGammaSqueezeEngine 那段，
                    # 不再連帶擋掉 watchlist 心跳。該欄位 DB 預設為 0 且沒有任何
                    # 指令可開啟，過去整個迴圈掛在它底下，等於這則心跳從未送出，
                    # 也與 AGENTS.md「Analyst Agent 不是 watchlist 心跳的前提」矛盾。
                    # 心跳現在只由 /notif_settings 的 heartbeat_symbol_deep 通道控制。
                    engine_enabled = bool(getattr(ctx, "enable_analyst_agent", False))

                    # 3. 取得帳戶狀態、持倉期權、Greeks 等（僅量化引擎需要，
                    # 含 VIX 報價與持倉查詢成本，關閉時不應付出）
                    account_state = None
                    holdings: List[OptionHolding] = []
                    portfolio_greeks: Dict[str, float] = {}
                    if engine_enabled:
                        account_state = TraderAccountState(
                            capital=ctx.total_capital
                            if hasattr(ctx, "total_capital")
                            else 100000.0,
                            cash_reserve=ctx.cash_reserve
                            if hasattr(ctx, "cash_reserve")
                            else 20000.0,
                            monthly_burn_rate=ctx.monthly_burn_rate
                            if hasattr(ctx, "monthly_burn_rate")
                            else 5000.0,
                            current_vix=await self._fetch_current_vix(),
                        )
                        holdings = await self._fetch_user_options_holdings(uid)
                        portfolio_greeks = await self._fetch_portfolio_greeks(uid)

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
                                # 這條深度心跳有自己的通知通道，與 15 分鐘批次
                                # 雷達 (cogs/trading/heartbeat.py 的
                                # heartbeat_watchlist) 分開控制。
                                hb_enabled = database.is_notification_enabled(
                                    uid, "heartbeat_symbol_deep"
                                )
                                notif_settings = (
                                    database.get_user_notification_settings(uid)
                                )

                                if hb_enabled:
                                    embed = await self._build_watchlist_heartbeat_embed(
                                        watchlist_eval,
                                        ctx,
                                        notif_settings,
                                        deployed_tactical_value=user_tactical_exposure,
                                    )
                                    if embed is not None:
                                        await self.bot.queue_dm(
                                            uid,
                                            embed=embed,
                                        )
                                else:
                                    logger.info(
                                        f"使用者 {uid} 已關閉所有心跳模組訂閱，略過心跳推送。"
                                    )
                            if not engine_enabled or account_state is None:
                                continue

                            market_data = await self._fetch_ticker_market_data(ticker)
                            if not market_data:
                                continue

                            # 執行核心量化引擎 (注入標的特定動態 Beta 與 SPY 現價)
                            ticker_greeks = dict(portfolio_greeks)
                            cached_entry = _WATCHLIST_METRICS_CACHE.get(ticker.upper())
                            if cached_entry is not None:
                                cached_m, _ = cached_entry
                                if getattr(cached_m, "beta", None) is not None:
                                    ticker_greeks["beta"] = float(cached_m.beta)
                            ticker_greeks["spy_price"] = self._cached_spy_spot

                            engine_output = self.engine.analyze_ticker(
                                data=market_data,
                                account_state=account_state,
                                options_holdings=holdings,
                                portfolio_greeks=ticker_greeks,
                                market_phase=phase,
                                current_time=now_ny,
                            )
                            await self._dispatch_gamma_squeeze_alert(
                                uid, ticker, engine_output, now_ny
                            )

                        except Exception as ticker_err:
                            logger.error(
                                f"❌ IntradayScanPipeline 處理標的 {ticker} 時發生錯誤: {ticker_err}",
                                exc_info=True,
                            )

                # 4. 睡眠 30 分鐘
                await asyncio.sleep(self.scan_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ IntradayScanPipeline 發生錯誤: {e}", exc_info=True)
                await asyncio.sleep(60)

    # 模擬/輔助獲取資料方法
    async def _fetch_current_vix(self) -> float:
        """獲取 VIX 即時數據，預設為 18.0"""
        try:
            from services.market_data_service import get_quote

            quote = await get_quote("^VIX")
            if quote and quote.get("c", 0) > 0:
                return float(quote["c"])
        except Exception:
            pass
        return 18.0

    async def _fetch_user_options_holdings(self, user_id: int) -> List[OptionHolding]:
        """從資料庫獲取使用者期權持倉"""
        holdings = []
        try:
            from database.holdings import get_user_holdings

            db_holdings = get_user_holdings(user_id)
            for h in db_holdings:
                # 僅處理期權合約
                if "opt_type" in h and h.get("opt_type"):
                    # 估計 theta (一般期權服務會提供，這裡給予預設值或從 holdings 讀取)
                    holdings.append(
                        OptionHolding(
                            symbol=h.get("symbol", ""),
                            quantity=float(h.get("quantity", 1.0)),
                            theta=float(h.get("theta", -0.05)),
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch option holdings for user {user_id}: {e}")

        return holdings

    async def _fetch_portfolio_greeks(self, user_id: int) -> Dict[str, float]:
        """獲取使用者投資組合 Greeks"""
        greeks = {"vanna": 0.0, "beta": 1.0}
        try:
            import database

            user_ctx = database.get_full_user_context(user_id)
            greeks["vanna"] = float(getattr(user_ctx, "total_vanna", 0.0))
        except Exception:
            pass

        # Mock 預設值
        if greeks["vanna"] == 0.0:
            greeks["vanna"] = 1.25
        return greeks

    async def _fetch_market_cap_billion(self, ticker: str) -> float:
        """公司市值（十億美元），供引擎 Gate 1 流動性門檻使用。

        Finnhub `company_profile2` 的 `marketCapitalization` 單位是**百萬美元**。
        取不到時回傳 0.0 —— Gate 1 會因此判定不通過（fail-closed）。這是刻意的：
        這條路徑會產出「建議分批建立 OTM Call」的進攻訊號，資料缺失時寧可不發，
        也不能像過去那樣用一個剛好高於門檻的假值讓閘門自動放行。
        """
        try:
            from services.market_data_service import get_company_profile

            profile = await get_company_profile(ticker)
            market_cap_musd = float((profile or {}).get("marketCapitalization") or 0.0)
            return market_cap_musd / 1000.0 if market_cap_musd > 0.0 else 0.0
        except Exception as e:
            logger.warning(f"[{ticker}] 取得市值失敗，Gate 1 將判定不通過: {e}")
            return 0.0

    async def _fetch_projected_option_volume(self, ticker: str) -> int:
        """全鏈當日期權成交量，取消跨時段線性外推。

        已取消「當日成交量 ÷ 交易時段已過比例」的線性外推機制，徹底避免開盤時段因
        成交量 U 型分佈導致的 3~5 倍虛假放大。Gate 1 已全面改用微觀結構指標
        「15m 即時成交量比 (RVOL_15m = Volume_15m / SMA20)」進行放量驗證。

        取不到時回傳 0（Gate 1 fail-closed）。
        """
        try:
            from market_analysis.sentiment_engine import SentimentEngine

            pcr_data = await SentimentEngine.calculate_pcr(ticker)
            if not isinstance(pcr_data, dict):
                return 0
            total_volume = float(pcr_data.get("put_vol") or 0.0) + float(
                pcr_data.get("call_vol") or 0.0
            )
            if total_volume <= 0.0:
                return 0
            return int(total_volume)
        except Exception as e:
            logger.warning(f"[{ticker}] 取得期權成交量失敗，Gate 1 將判定不通過: {e}")
            return 0

    async def _fetch_front_expiry_otm_call_premium(
        self, ticker: str, spot_price: float
    ) -> float:
        """統計 DTE >= 7 且 Vol/OI >= 0.8x 之價外 Call 主力成交權利金 (Σ volume × lastPrice × 100)，供 Gate 3。

        強制過濾 DTE 0~4 之末日輪流動性雜訊（做市商結算日尾盤對倒與 Gamma 軋平），
        僅採樣 DTE >= 7 且單筆/累積成交佔該合約 OI >= 0.8x 的跨週期主力訂單流。
        取不到或無符合主力單時回傳 0.0（Gate 3 fail-closed）。
        """
        try:
            from services import market_data_service
            import market_time
            import pandas as pd

            expiries = await market_data_service.get_all_option_expiries(ticker)
            if not expiries:
                return 0.0

            today = datetime.now(market_time.ny_tz).date()
            qualifying_expiries = []
            for exp_str in expiries:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    dte = (exp_date - today).days
                    # 強制過濾 DTE 0~4 之合約，僅採樣 DTE >= 7
                    if dte >= 7:
                        qualifying_expiries.append(exp_str)
                except ValueError:
                    continue

            if not qualifying_expiries:
                return 0.0

            total_institutional_premium = 0.0
            # 採樣前 3 個符合 DTE >= 7 的到期日
            for exp in qualifying_expiries[:3]:
                chain = await market_data_service.get_option_chain(ticker, exp)
                if chain is None or chain.calls.empty:
                    continue

                calls = chain.calls
                otm = calls[calls["strike"] > spot_price]
                if otm.empty:
                    continue

                vols = otm["volume"].fillna(0.0).astype(float)
                ois = (
                    otm["openInterest"].fillna(0.0).astype(float)
                    if "openInterest" in otm.columns
                    else pd.Series(0.0, index=otm.index)
                )

                # 篩選成交量佔該合約 OI >= 0.8x 的跨週期主力訂單流
                has_oi = (ois > 0).any()
                if has_oi:
                    mask = (ois > 0) & (vols >= 0.8 * ois) & (vols > 0)
                    target_otm = otm[mask]
                else:
                    target_otm = otm.iloc[0:0]

                if not target_otm.empty:
                    t_vols = target_otm["volume"].fillna(0.0).astype(float)
                    t_prices = target_otm["lastPrice"].fillna(0.0).astype(float)
                    total_institutional_premium += float(
                        (t_vols * t_prices * 100.0).sum()
                    )

            return total_institutional_premium
        except Exception as e:
            logger.warning(
                f"[{ticker}] 取得 DTE>=7 主力 OTM Call 權利金失敗，Gate 3 將判定不通過: {e}"
            )
            return 0.0

    async def _fetch_ticker_market_data(
        self, ticker: str
    ) -> Optional[TickerMarketData]:
        """獲取標的即時數據並拼裝為 TickerMarketData。

        包含即時微觀結構、15m RVOL、即時 IV 與 GEX Profile。
        任一核心項取得失敗一律安全降級並促使相應 Gate fail-closed。
        """
        try:
            from services.calendar_service import calendar_service
            from services.market_data_service import get_quote
            from market_analysis.price_volume_alert import get_confirmed_15m_bar
            from market_analysis.index_microstructure import fetch_symbol_gex_metrics
            from market_analysis.sentiment_engine import SentimentEngine

            quote = await get_quote(ticker)
            price_raw = quote.get("c", 0) if quote else 0
            if not quote or float(price_raw) <= 0:
                return None

            price = float(price_raw)

            # 獲取財報日期
            days_earnings = 30
            try:
                earnings_info = await calendar_service.get_symbol_earnings(ticker)
                if earnings_info is not None:
                    dt_earn = datetime.strptime(earnings_info.date, "%Y-%m-%d").date()
                    days_earnings = max(0, (dt_earn - datetime.now(ny_tz).date()).days)
            except Exception:
                pass

            # 接入即時波動率強制刷新數據源（Realtime_IV）
            real_iv_rank = 0.0
            realtime_iv = None
            try:
                iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(
                    ticker, force_refresh=True
                )
                if iv_metrics:
                    if iv_metrics.iv_rank is not None:
                        real_iv_rank = float(iv_metrics.iv_rank)
                    if iv_metrics.current_iv is not None:
                        realtime_iv = float(iv_metrics.current_iv)
            except Exception as e:
                logger.debug(f"[{ticker}] 抓取即時 IV 失敗: {e}")

            # Skew 取得
            real_option_skew = 0.0
            cached_entry = _WATCHLIST_METRICS_CACHE.get(ticker.upper())
            if cached_entry is not None:
                cached_m, _ = cached_entry
                if real_iv_rank == 0.0 and cached_m.iv_rank is not None:
                    real_iv_rank = float(cached_m.iv_rank)
                if cached_m.option_skew is not None:
                    real_option_skew = float(cached_m.option_skew) / 100.0

            # 微觀結構 15m K 棒 (RVOL_15m)
            rvol_15m = None
            try:
                bar_15m = await get_confirmed_15m_bar(ticker)
                if bar_15m and bar_15m.avg_volume > 0:
                    rvol_15m = round(bar_15m.volume / bar_15m.avg_volume, 4)
            except Exception as e:
                logger.debug(f"[{ticker}] 抓取 15m K 棒失敗: {e}")

            # GEX 結構與 Call Wall
            call_wall = None
            net_gex = None
            gex_profile = None
            try:
                gex_data = await fetch_symbol_gex_metrics(ticker)
                if gex_data:
                    call_wall = gex_data.get("call_wall")
                    net_gex = gex_data.get("net_gex")
                    gex_profile = gex_data.get("gex_profile")
            except Exception as e:
                logger.debug(f"[{ticker}] 抓取 GEX 數據失敗: {e}")

            # 全鏈 STO 物理封頂
            physical_cap_strikes = None
            try:
                (
                    _,
                    physical_cap_strikes,
                ) = await SentimentEngine.detect_uoa_with_physical_caps(ticker)
            except Exception as e:
                logger.debug(f"[{ticker}] 抓取 STO 物理封頂失敗: {e}")

            (
                market_cap_billion,
                avg_option_volume,
                otm_call_premium,
            ) = await asyncio.gather(
                self._fetch_market_cap_billion(ticker),
                self._fetch_projected_option_volume(ticker),
                self._fetch_front_expiry_otm_call_premium(ticker, price),
            )

            return TickerMarketData(
                ticker=ticker,
                spot_price=price,
                market_cap_billion=market_cap_billion,
                avg_option_volume=avg_option_volume,
                days_until_earnings=days_earnings,
                tomorrow_expiring_otm_calls_premium=otm_call_premium,
                iv_rank=real_iv_rank,
                option_skew=real_option_skew,
                rvol_15m=rvol_15m,
                realtime_iv=realtime_iv,
                call_wall=call_wall,
                net_gex=net_gex,
                gex_profile=gex_profile,
                physical_cap_strikes=physical_cap_strikes,
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {ticker}: {e}")
            return None
