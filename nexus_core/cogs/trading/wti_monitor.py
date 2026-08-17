"""WTI 原油價格警報背景監控器。

獨立於美股交易時段，每 30 分鐘執行一次 CL=F 報價抓取與閾值評估。
支援絕對價格閾值 (上限/下限) 與百分比波動觸發。
深夜靜默保護 (00:00–06:00 ET) 防止打擾。
"""

import logging
from datetime import time, datetime
from typing import Any

from discord.ext import commands, tasks

import database
from database.wti_config import get_wti_config
import market_time
from market_analysis.wti_analysis import (
    WtiAlertType,
    analyze_wti,
)
from cogs.embed_builders.alert_embeds import create_wti_alert_embed

logger = logging.getLogger(__name__)

# 靜默時段 (ET): 00:00 – 06:00
QUIET_HOUR_START: int = 0
QUIET_HOUR_END: int = 6

# 30 分鐘輪詢時間點 (每小時 :00 和 :30)
_wti_scan_times: list[time] = [
    time(hour=h, minute=m, tzinfo=market_time.ny_tz) for h in range(24) for m in (0, 30)
]


class WtiMonitorCog(commands.Cog, name="WtiMonitorCog"):
    """WTI 原油價格警報背景排程器。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.wti_oil_monitor.start()

    async def cog_unload(self) -> None:
        self.wti_oil_monitor.cancel()

    @tasks.loop(time=_wti_scan_times)
    async def wti_oil_monitor(self) -> None:
        """每 30 分鐘 WTI 油價監控主循環。"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return

        now_et = datetime.now(market_time.ny_tz)

        # 靜默時段保護 (00:00 – 06:00 ET)
        if QUIET_HOUR_START <= now_et.hour < QUIET_HOUR_END:
            logger.debug(
                f"🛢️ [WTI Monitor] 處於靜默時段 ({now_et.hour:02d}:{now_et.minute:02d} ET)，跳過掃描"
            )
            return

        try:
            await self._evaluate_wti_alerts()
        except Exception as e:
            logger.error(f"🛢️ [WTI Monitor] 執行失敗: {e}", exc_info=True)

    @wti_oil_monitor.before_loop
    async def before_wti_monitor(self) -> None:
        await self.bot.wait_until_ready()
        logger.info(
            "🛢️ WTI 油價監控器已啟動，每 30 分鐘執行一次 (全天候，00:00-06:00 ET 靜默)。"
        )

    async def _evaluate_wti_alerts(self) -> None:
        """評估所有用戶的 WTI 閾值並觸發警報。"""
        from services.market_data_service import get_quote

        # 1. 抓取 CL=F 即時報價
        wti_quote = await get_quote("CL=F")
        if not isinstance(wti_quote, dict) or wti_quote.get("c", 0.0) <= 0.0:
            logger.warning("🛢️ [WTI Monitor] CL=F 報價抓取失敗或無效")
            return

        current_price = float(wti_quote["c"])
        logger.info(f"🛢️ [WTI Monitor] CL=F 現價: ${current_price:.2f}")

        # 2. 計算 30 分鐘波動百分比
        prev_price_raw = database.get_kv_cache("macro_wti_prev_30m")
        prev_price = (
            float(prev_price_raw) if prev_price_raw is not None else current_price
        )
        pct_change = (
            ((current_price - prev_price) / prev_price * 100.0)
            if prev_price > 0.0
            else 0.0
        )

        # 更新前次價格快取與宏觀快取
        await database.save_kv_cache("macro_wti_prev_30m", current_price)
        await database.save_kv_cache("macro_wti", current_price)

        # 3. 遍歷所有用戶，評估觸發條件
        today_str = datetime.now(market_time.ny_tz).strftime("%Y%m%d")
        uids: list[int] = database.get_all_user_ids()

        for uid in uids:
            if not database.is_notification_enabled(uid, "alpha_wti_oil"):
                continue

            config = await get_wti_config(uid)
            alerts_to_send: list[tuple[WtiAlertType, float]] = []

            # 絕對價格上限檢查
            if config.upper_price is not None and current_price >= config.upper_price:
                alerts_to_send.append((WtiAlertType.UPPER_BREACH, config.upper_price))

            # 絕對價格下限檢查
            if config.lower_price is not None and current_price <= config.lower_price:
                alerts_to_send.append((WtiAlertType.LOWER_BREACH, config.lower_price))

            # 百分比波動檢查 (30 分鐘)
            if abs(pct_change) >= config.pct_change_threshold:
                alert_type = (
                    WtiAlertType.PCT_SURGE
                    if pct_change > 0
                    else WtiAlertType.PCT_PLUNGE
                )
                alerts_to_send.append((alert_type, config.pct_change_threshold))

            if not alerts_to_send:
                continue

            # 4. 取得用戶自選清單和持倉 (用於關聯股標記)
            user_watchlist: list[str] = []
            user_holdings: list[str] = []
            try:
                user_watchlist_raw = database.get_user_watchlist(uid)
                user_watchlist = [sym for sym, _ in user_watchlist_raw]
                if hasattr(database, "get_user_holding_symbols"):
                    user_holdings = database.get_user_holding_symbols(uid)
            except Exception as e:
                logger.debug(f"讀取使用者自選/持倉失敗 (uid: {uid}): {e}")

            # 5. 對每個觸發的警報類型進行分析與發送 (含 KV Cache 每日去重)
            for alert_type, threshold in alerts_to_send:
                cache_key = f"wti_alert_{uid}_{today_str}_{alert_type.value}"
                if database.get_kv_cache(cache_key):
                    continue  # 每日每類型只觸發一次，避免震盪重複洗版

                try:
                    analysis = await analyze_wti(
                        price=current_price,
                        alert_type=alert_type,
                        threshold_value=threshold,
                        pct_change_30min=pct_change,
                        user_watchlist=user_watchlist,
                        user_holdings=user_holdings,
                    )

                    embed = create_wti_alert_embed(analysis)
                    await self.bot.queue_dm(uid, embed=embed)
                    await database.save_kv_cache(cache_key, 1)

                    logger.warning(
                        f"🛢️ [WTI Alert] 已發送 {alert_type.value} 警報給使用者 {uid} "
                        f"(現價: ${current_price:.2f}, 閾值: {threshold})"
                    )
                except Exception as e:
                    logger.error(
                        f"🛢️ [WTI Alert] 發送失敗 (uid: {uid}, type: {alert_type.value}): {e}"
                    )


async def setup(bot: Any) -> None:
    await bot.add_cog(WtiMonitorCog(bot))
