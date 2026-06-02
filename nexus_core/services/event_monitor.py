import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, List

from cogs.embed_builder import create_proactive_event_alert_embed
from database import get_full_user_context
from services.calendar_service import calendar_service
from database.user_settings import get_all_user_ids
import market_time

from services.market_data_service import BoundedCache, get_quote

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


def _extract_quote_price(quote: dict[str, Any], fallback: float = 500.0) -> float:
    for key in ("c", "current_price", "price"):
        value = quote.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return fallback


def _build_portfolio_risk_snapshot(
    user_context: Any,
    *,
    spy_price: float,
) -> dict[str, Any]:
    capital = max(float(getattr(user_context, "capital", 100000.0)), 1.0)
    risk_limit = max(float(getattr(user_context, "risk_limit", 15.0)), 1.0)
    total_delta = float(getattr(user_context, "total_weighted_delta", 0.0))
    total_theta = float(getattr(user_context, "total_theta", 0.0))
    total_gamma = float(getattr(user_context, "total_gamma", 0.0))
    total_vanna = float(getattr(user_context, "total_vanna", 0.0))

    heat_pct = abs(total_delta) * max(spy_price, 0.0) / capital * 100.0
    heat_ratio = heat_pct / risk_limit

    if total_theta > 0.05:
        theta_state = "賣方偏重"
    elif total_theta < -0.05:
        theta_state = "買方保護偏重"
    else:
        theta_state = "Theta 中性"

    if total_gamma <= -20.0:
        gamma_state = "Gamma 脆弱"
    elif total_gamma < 0.0:
        gamma_state = "短 Gamma"
    else:
        gamma_state = "Gamma 穩定"

    abs_vanna = abs(total_vanna)
    if abs_vanna >= 10.0:
        vanna_state = "Vanna 敏感高"
    elif abs_vanna >= 3.0:
        vanna_state = "Vanna 敏感中"
    else:
        vanna_state = "Vanna 敏感低"

    if heat_ratio >= 1.0 or total_gamma <= -20.0:
        tier = "high"
    elif heat_ratio >= 0.7 or total_theta > 0.05 or abs_vanna >= 3.0:
        tier = "medium"
    else:
        tier = "low"

    summary = (
        f"Heat `{heat_pct:.1f}% / {risk_limit:.1f}%` ｜ "
        f"{theta_state} ｜ {gamma_state} ｜ {vanna_state}"
    )
    return {
        "tier": tier,
        "heat_pct": round(heat_pct, 2),
        "risk_limit": risk_limit,
        "theta_state": theta_state,
        "gamma_state": gamma_state,
        "vanna_state": vanna_state,
        "summary": summary,
    }


def _build_event_nro_instruction(event: Any, risk_snapshot: dict[str, Any]) -> str:
    tte_hours = float(getattr(event, "tte_hours", 999.0) or 999.0)
    tier = str(risk_snapshot["tier"])
    theta_state = str(risk_snapshot["theta_state"])
    gamma_state = str(risk_snapshot["gamma_state"])
    vanna_state = str(risk_snapshot["vanna_state"])

    imminent = tte_hours <= 8.0
    near_term = tte_hours <= 24.0
    seller_bias = theta_state == "賣方偏重"
    gamma_fragile = gamma_state == "Gamma 脆弱"
    short_gamma = gamma_state in {"Gamma 脆弱", "短 Gamma"}
    vanna_sensitive = vanna_state in {"Vanna 敏感高", "Vanna 敏感中"}

    # Extract targeted subject for NRO personalization
    if getattr(event, "type", "") == "ECONOMIC":
        subject = getattr(event, "event", "經濟數據")
    else:
        subject = getattr(event, "symbol", "該標的")

    if getattr(event, "type", "") == "ECONOMIC":
        if imminent and (tier == "high" or gamma_fragile):
            return (
                f"【{subject}】巨集事件已逼近，先降 Beta-Weighted Delta、回補短 Gamma，"
                "避免新增賣方與高槓桿方向押注。"
            )
        if imminent and seller_bias:
            return (
                f"【{subject}】數據前 8 小時內先縮減賣方曝險，保留定義風險結構，"
                "必要時用保護性 Put / Debit Spread 緩衝。"
            )
        if near_term and vanna_sensitive:
            return (
                f"【{subject}】維持 Calendar Guard：提高 Vanna 權重、縮小方向押注，"
                "優先保留可快速調整的部位。"
            )
        return (
            f"【{subject}】事件前先控管方向曝險與倉位槓桿，避免新增裸賣方，"
            "待數據落地後再恢復常態部署。"
        )

    if imminent and seller_bias:
        return (
            f"【{subject}】財報臨近且組合偏賣方；優先回補短 Vega / Theta 收租倉，"
            "避免承受 IV Crush 與跳空雙重風險。"
        )
    if imminent and short_gamma:
        return (
            f"【{subject}】財報前短 Gamma 風險偏高；先降倉並改用定義風險結構，"
            "避免跳空放大損益。"
        )
    if near_term and tier == "high":
        return (
            f"【{subject}】啟動 Earnings Guard：降低總曝險與集中度，"
            "若保留方向判斷優先保護性 Put 或 Debit Spread。"
        )
    return (
        f"【{subject}】財報窗口已開啟；控制口數、避免堆疊裸賣方，"
        "若要保留方向觀點優先使用定義風險結構。"
    )


def _build_event_alert_payload(
    event: Any,
    risk_snapshot: dict[str, Any],
) -> dict[str, str | float]:
    if getattr(event, "type", "") == "ECONOMIC":
        event_name = getattr(event, "event", "未知事件")
        name = f"🔴 經濟數據: {event_name}"
    else:
        name = f"📊 財報預警: {getattr(event, 'symbol', '未知標的')}"
    return {
        "name": name,
        "tte_hours": float(getattr(event, "tte_hours", 0.0) or 0.0),
        "risk_status": str(risk_snapshot["summary"]),
        "instruction": _build_event_nro_instruction(event, risk_snapshot),
    }


class EventMonitor:
    """
    Background monitor for NYSE Dynamic Scheduler.
    Detects upcoming high-impact events and pushes proactive alerts.
    """

    def __init__(self, bot):
        self.bot = bot
        # Cache to track alerted events to prevent spam
        # Key: (user_id, event_type, event_id, event_date)
        # Max 2000 entries should be enough for many users and events
        self._alerted_cache = BoundedCache(max_size=2000)

    async def check_upcoming_events(self):
        """
        Scan all users' portfolios for upcoming high-impact events.
        """
        if not market_time.is_market_open():
            # Still check even if closed, as we want proactive alerts
            pass

        user_ids = await asyncio.to_thread(get_all_user_ids)

        for uid in user_ids:
            try:
                # 1. Fetch events affecting this user
                events = await calendar_service.get_portfolio_events(uid, days=3)

                # 2. Filter for events within the next 48 hours that haven't been alerted
                critical_events = []
                for e in events:
                    if not (0 < e.tte_hours < 48.0):
                        continue

                    # Create a unique key for deduplication
                    if e.type == "ECONOMIC":
                        # For economic events, use event name and ISO time
                        event_id = getattr(e, "event", "unknown")
                        event_date = getattr(e, "time", "unknown")
                    else:
                        # For earnings, use symbol and date
                        event_id = getattr(e, "symbol", "unknown")
                        event_date = getattr(e, "date", "unknown")

                    alert_key = f"{uid}_{e.type}_{event_id}_{event_date}"

                    if alert_key not in self._alerted_cache:
                        critical_events.append(e)
                        self._alerted_cache[alert_key] = datetime.now()

                if critical_events:
                    await self._send_event_alert(uid, critical_events)

            except Exception as e:
                logger.error(f"Error checking events for user {uid}: {e}")

    async def _send_event_alert(self, user_id: int, events: List[Any]):
        """
        Send a proactive hedging alert based on upcoming events.
        """
        import database

        if not database.is_notification_enabled(user_id, "proactive_event_alert"):
            logger.info(
                f"使用者 {user_id} 已關閉 proactive_event_alert，略過經濟/財報事件警報。"
            )
            return
        user_context = await asyncio.to_thread(get_full_user_context, user_id)
        try:
            spy_quote = await get_quote("SPY")
            spy_price = _extract_quote_price(spy_quote)
        except Exception as e:
            logger.warning(f"取得 SPY 報價失敗，事件預警改用預設價格: {e}")
            spy_price = 500.0

        risk_snapshot = _build_portfolio_risk_snapshot(
            user_context,
            spy_price=spy_price,
        )
        event_payloads = [
            _build_event_alert_payload(event, risk_snapshot) for event in events
        ]
        embeds = create_proactive_event_alert_embed(event_payloads)
        for embed in embeds:
            await self.bot.queue_dm(user_id, embed=embed)


# Helper to start the monitor
async def start_event_monitor(bot):
    monitor = EventMonitor(bot)
    while True:
        await monitor.check_upcoming_events()
        # Check every 4 hours
        await asyncio.sleep(4 * 3600)
