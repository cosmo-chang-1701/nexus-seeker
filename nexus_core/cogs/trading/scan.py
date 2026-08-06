"""
cogs/trading/scan.py

NRO 盤中市場掃描邏輯：_run_market_scan_logic、_should_send_alert、_update_macro_state。
MarketScanCog 持有 signal_cooldowns 與 prev_macro_state 跨輪次狀態。
"""

from typing import Any, Dict
import time as _time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from discord.ext import commands

import database
from services.trading_service import TradingService
from services.alert_filter import should_send_priority_alert
from cogs.embed_builder import (
    create_scan_embed,
    create_info_embed,
    create_rehedge_embed,
)

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


class MarketScanCog(commands.Cog):
    """NRO 盤中市場掃描邏輯與狀態持有。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.trading_service = TradingService(bot)
        self.signal_cooldowns: Dict[str, Any] = {}
        self.COOLDOWN_HOURS = 4
        self.prev_macro_state: Dict[str, float] = {}

    async def _should_send_alert(self, uid: int, symbol: str, alert_mode: int) -> bool:
        """
        根據使用者的警報模式與標的是否在持倉中，決定是否發送通知。
        0=OFF, 1=ALL, 2=PORTFOLIO_ONLY
        """
        if alert_mode == 0:
            return False
        if alert_mode == 1:
            return True
        if alert_mode == 2:
            return database.is_symbol_in_portfolio(uid, symbol)
        return True

    def _update_macro_state(self, user_results: Dict[int, list]) -> None:
        """
        從本輪掃描結果中提取宏觀環境快照 (VIX)，
        存入 prev_macro_state 供下一輪 AlertFilter 比對變動幅度。
        """
        for alerts_data in user_results.values():
            for data in alerts_data:
                vix = data.get("macro_vix")
                if vix is not None:
                    self.prev_macro_state["vix"] = vix
                    logger.debug(f"[MacroState] 快照已更新: VIX={vix:.2f}")
                    return

    async def _run_market_scan_logic(  # type: ignore
        self, is_auto: Any = True, triggered_by: Any = None
    ) -> None:
        """共用的掃描核心邏輯，協調 Service 計算與 Discord 訊息發送。"""
        try:
            if not is_auto and triggered_by:
                await triggered_by.send("🔍 **開始掃描標的...**")

            # 🚀 1. 執行 DDP 掃描 (Davis Double Play)
            all_watchlists = database.get_all_watchlist()
            symbols_all = sorted(list(set(row[1] for row in all_watchlists)))
            if symbols_all:
                ddp_results = await self.trading_service.run_ddp_scan(symbols_all)
                for report in ddp_results:
                    from cogs.embed_builder import create_ddp_embed

                    embed = create_ddp_embed(report)
                    sym = report["symbol"]

                    for uid, watch_sym, _ in all_watchlists:
                        if watch_sym == sym:
                            if not database.is_notification_enabled(
                                uid, "ddp_cheap_vol_alert"
                            ):
                                continue
                            ctx = database.get_full_user_context(uid)
                            if await self._should_send_alert(
                                uid, sym, ctx.option_alert_mode
                            ):
                                await self.bot.queue_dm(uid, embed=embed)

                    await self.trading_service.ddp_inspector.record_signal(report)

            # 🚀 2. 執行 IV 優勢掃描 (Volatility Strategist)
            uids = sorted(list(set(row[0] for row in all_watchlists)))
            for uid in uids:
                if not database.is_notification_enabled(uid, "ddp_cheap_vol_alert"):
                    continue
                user_context = database.get_full_user_context(uid)
                user_watch = [row[1] for row in all_watchlists if row[0] == uid]
                vol_results = await self.trading_service.run_iv_opportunity_scan(
                    user_watch, uid
                )
                for report in vol_results:
                    if await self._should_send_alert(
                        uid, report["symbol"], user_context.option_alert_mode
                    ):
                        from cogs.embed_builder import create_volatility_embed

                        embed = create_volatility_embed(report)
                        await self.bot.queue_dm(uid, embed=embed)

            # 🚀 3. 執行標準 NRO 掃描
            user_results = await self.trading_service.run_market_scan(
                is_auto=is_auto,
                triggered_by_id=triggered_by.id if triggered_by else None,
            )

            if not user_results:
                if not is_auto and triggered_by:
                    await triggered_by.send(
                        "📭 **本次掃描未發現符合策略的交易機會或觀察清單為空。**"
                    )
                return

            now = datetime.now(ny_tz)
            for uid, alerts_data in user_results.items():
                user_cooldowns = self.signal_cooldowns.setdefault(str(uid), {})
                valid_alerts = []

                user_context = database.get_full_user_context(uid)
                for data in alerts_data:
                    sym = data["symbol"]
                    ai_decision = data.get("ai_decision", "APPROVE")
                    alert_type = data.get("alert_type", "OPTION")
                    cooldown_key = f"{sym}_{alert_type}"

                    if ai_decision == "VETO":
                        continue

                    if is_auto:
                        last_sent_time = user_cooldowns.get(cooldown_key)
                        if last_sent_time:
                            time_diff = (now - last_sent_time).total_seconds()
                            if time_diff < (self.COOLDOWN_HOURS * 3600):
                                continue

                    if alert_type == "OPTION":
                        last_alert_state = database.get_watchlist_alert_state(uid, sym)
                        is_priority, reason = await should_send_priority_alert(
                            data, self.prev_macro_state, last_alert_state
                        )

                        if is_auto and not is_priority:
                            logger.info(f"⏭️ 標的 {sym} 未達優先通知門檻，已過濾。")
                            continue

                        for sig in data.get("ema_signals", []):
                            if sig.get("type") == "CROSSOVER":
                                database.update_watchlist_alert_state(
                                    uid,
                                    sym,
                                    direction=sig["direction"],
                                    price=data.get("price", 0.0),
                                    timestamp=int(_time.time()),
                                )
                                break

                        if reason:
                            data["alert_reason"] = reason

                        if await self._should_send_alert(
                            uid, sym, user_context.option_alert_mode
                        ):
                            valid_alerts.append(data)

                            exec_decision = data.get("execution_decision")
                            if exec_decision:
                                from formatters.execution_embeds import (
                                    build_execution_embed,
                                )

                                await self.bot.queue_dm(
                                    uid, embed=build_execution_embed(exec_decision)
                                )

                        if is_auto:
                            user_cooldowns[cooldown_key] = now
                            if user_context.enable_vtr:
                                await self.trading_service.execute_vtr_auto_entry(data)

                    elif alert_type == "PSQ":
                        if await self._should_send_alert(
                            uid, sym, user_context.option_alert_mode
                        ):
                            valid_alerts.append(data)
                        if is_auto:
                            user_cooldowns[cooldown_key] = now

                if valid_alerts:
                    title = (
                        "📡 **【盤中動態掃描】NRO 風控已介入判定：**"
                        if is_auto
                        else "⚡ **【管理員強制掃描】風險模擬結果：**"
                    )
                    await self.bot.queue_dm(
                        uid,
                        embed=create_info_embed(title="掃描通知", message=title),
                    )
                    user_capital = user_context.capital
                    for data in valid_alerts:
                        if data.get("alert_type") == "PSQ":
                            from cogs.embed_builder import create_psq_embed

                            await self.bot.queue_dm(uid, embed=create_psq_embed(data))
                        else:
                            await self.bot.queue_dm(
                                uid, embed=create_scan_embed(data, user_capital)
                            )

                            rehedge_info = data.get("rehedge_info")
                            if rehedge_info:
                                await self.bot.queue_dm(
                                    uid, embed=create_rehedge_embed(rehedge_info)
                                )

            self._update_macro_state(user_results)

        except Exception as e:
            logger.error(f"掃描邏輯執行錯誤: {e}")


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(MarketScanCog(bot))
