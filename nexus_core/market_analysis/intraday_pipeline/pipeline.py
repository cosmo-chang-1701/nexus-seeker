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
from market_analysis.signal_calculator import calculate_dynamic_trading_signals
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

    async def _build_watchlist_heartbeat_embed(
        self,
        evaluation: WatchlistEvaluation,
        user_context: Any,
        notif_settings: dict | None = None,
    ) -> Any:
        import database
        from cogs.embed_builder import create_watchlist_signal_embed
        from ui.formatter import generate_ansi_watchlist_report

        report_body = generate_ansi_watchlist_report(
            evaluation.metrics,
            evaluation.tactical,
        )
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
            user_holdings = {
                str(row.get("symbol", "")).upper(): row
                for row in database.get_user_holdings(user_id)
            }
            holding_row = user_holdings.get(evaluation.metrics.symbol.upper())
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

        # 計算動態買賣點現貨及對齊的期權操盤建議
        signals = calculate_dynamic_trading_signals(
            evaluation.metrics,
            evaluation.tactical,
            has_position=has_position,
            holding_quantity=holding_quantity,
            holding_avg_cost=holding_avg_cost,
            capital=user_capital,
            risk_limit=user_risk_limit,
            has_upcoming_earnings=has_upcoming_earnings,
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
        from services import market_data_service
        from market_analysis.sentiment_engine import SentimentEngine

        hb_symbol = evaluation.metrics.symbol
        is_hedging = hb_symbol.upper() in ["BOXX", "BIL"]
        hb_uoa_fetched_ok = False
        try:
            if is_hedging:
                from services import market_data_service

                hb_quote = await market_data_service.get_quote(hb_symbol)
                hb_iv_metrics = None
                hb_pcr_data = None
                hb_uoa_list = []
                hb_max_pain = None
            else:
                (
                    hb_quote,
                    hb_iv_metrics,
                    hb_pcr_data,
                    hb_uoa_list,
                    hb_max_pain,
                ) = await asyncio.gather(
                    market_data_service.get_quote(hb_symbol),
                    SentimentEngine.fetch_and_calculate_iv_metrics(hb_symbol),
                    SentimentEngine.calculate_pcr(hb_symbol),
                    SentimentEngine.detect_uoa(hb_symbol),
                    SentimentEngine.get_unified_max_pain(hb_symbol),
                )
                hb_uoa_fetched_ok = True
        except Exception as sup_err:
            logger.warning(f"[{hb_symbol}] 心跳補充數據取得失敗: {sup_err}")
            hb_quote, hb_iv_metrics, hb_pcr_data, hb_uoa_list, hb_max_pain = (
                None,
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
            report_body=report_body,
            option_guidance=option_guidance,
            event_risk_summary=(
                evaluation.event_context.summary
                if evaluation.event_context is not None
                else "未偵測到近期重大事件"
            ),
            skew_state=(
                f"{evaluation.metrics.option_skew:+.2f}% ｜ "
                f"{evaluation.metrics.option_skew_state}"
            ),
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
            quote=hb_quote if isinstance(hb_quote, dict) else None,
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

    async def _run_loop(self) -> None:
        while self.is_running:
            try:
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

                # 2. 獲取所有使用者資訊，執行量化分析
                import database

                user_ids = database.get_all_user_ids()

                for uid in user_ids:
                    ctx = database.get_full_user_context(uid)
                    if not ctx.enable_analyst_agent:
                        continue

                    # 3. 取得帳戶狀態、持倉期權、Greeks 等
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

                    # 讀取期權持倉
                    holdings = await self._fetch_user_options_holdings(uid)
                    portfolio_greeks = await self._fetch_portfolio_greeks(uid)

                    # 掃描 watchlist 中的標的
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
                                hb_enabled = database.is_notification_enabled(
                                    uid, "heartbeat_watchlist"
                                )
                                notif_settings = (
                                    database.get_user_notification_settings(uid)
                                )

                                if hb_enabled:
                                    embed = await self._build_watchlist_heartbeat_embed(
                                        watchlist_eval, ctx, notif_settings
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
                            market_data = await self._fetch_ticker_market_data(ticker)
                            if not market_data:
                                continue

                            # 執行核心量化引擎
                            _ = self.engine.analyze_ticker(
                                data=market_data,
                                account_state=account_state,
                                options_holdings=holdings,
                                portfolio_greeks=portfolio_greeks,
                                market_phase=phase,
                                current_time=now_ny,
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

    async def _fetch_ticker_market_data(
        self, ticker: str
    ) -> Optional[TickerMarketData]:
        """獲取標的即時數據並拼裝為 TickerMarketData"""
        try:
            from services.calendar_service import calendar_service
            from services.market_data_service import get_quote

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

            # 從已快取的 watchlist metrics 取得真實 IV Rank 與 Skew
            real_iv_rank = 50.0
            real_option_skew = 0.0
            cached_entry = _WATCHLIST_METRICS_CACHE.get(ticker.upper())
            if cached_entry is not None:
                cached_m, _ = cached_entry
                if cached_m.iv_rank is not None:
                    real_iv_rank = float(cached_m.iv_rank)
                if cached_m.option_skew is not None:
                    real_option_skew = float(cached_m.option_skew) / 100.0

            return TickerMarketData(
                ticker=ticker,
                spot_price=price,
                market_cap_billion=250.5,  # 安全降級預設值，不影響核心路由邏輯
                avg_option_volume=65000,  # 安全降級預設值
                days_until_earnings=days_earnings,
                tomorrow_expiring_otm_calls_premium=1200000.0,  # 安全降級預設值
                iv_rank=real_iv_rank,
                option_skew=real_option_skew,
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {ticker}: {e}")
            return None
