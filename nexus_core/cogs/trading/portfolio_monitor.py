"""
cogs/trading/portfolio_monitor.py

真實持倉風險動態審計 (每 30 分鐘)：DITM、Gamma Fragility、動態轉倉，以及 VTR 監控。
"""

from typing import Any, Dict, List
import json
import logging
import sqlite3
from datetime import time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import config
import database
import market_time
from services.trading_service import TradingService
from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.ghost_trader import GhostTrader
from cogs.embed_builder import (
    build_vtr_stats_embed,
    create_profit_lock_alert_embed,
    create_gamma_fragility_embed,
    create_option_defense_alert_embed,
)
from cogs.embed_builders.rollover_embeds import create_dynamic_rollover_embed

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def _get_cached_symbol_metrics(sym: str) -> dict:  # type: ignore
    """從 SQLite 快取讀取標的指標，供動態轉倉盤中審計使用。"""
    res: dict[str, Any] = {
        "spot_price": 100.0,
        "ivr": 0.0,
        "max_pain": 0.0,
        "put_wall": 0.0,
        "call_wall": 0.0,
        "is_uoa_sweep": False,
        "sqz_mom": 0.0,
        "skew": 0.0,
    }
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT max_pain FROM market_cache WHERE symbol = ?", (sym,))
        row = cursor.fetchone()
        if row and row[0]:
            res["max_pain"] = float(row[0])

        cursor.execute(
            "SELECT value FROM kv_cache WHERE key = ?",
            (f"gex_profile_{sym}",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            gex_data = json.loads(row[0])
            res["spot_price"] = float(gex_data.get("spot_price", 100.0))
            res["put_wall"] = float(gex_data.get("put_wall", 0.0))
            res["call_wall"] = float(gex_data.get("call_wall", 0.0))
            res["gamma_flip"] = float(gex_data.get("gamma_flip", 0.0))

        cursor.execute("SELECT value FROM kv_cache WHERE key = ?", (f"ivr_{sym}",))
        row = cursor.fetchone()
        if row and row[0]:
            res["ivr"] = float(row[0])

        cursor.execute(
            "SELECT value FROM kv_cache WHERE key = ?",
            (f"sqz_mom_{sym}",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            res["sqz_mom"] = float(row[0])

        cursor.execute("SELECT value FROM kv_cache WHERE key = ?", (f"skew_{sym}",))
        row = cursor.fetchone()
        if row and row[0]:
            res["skew"] = float(row[0])
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return res


class PortfolioMonitorCog(commands.Cog):
    """真實持倉風險審計 + VTR 監控排程。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.trading_service = TradingService(bot)
        self.rollover_engine = DynamicRolloverEngine()
        self.monitor_real_portfolio_task.start()
        self.monitor_vtr_task.start()

    async def cog_unload(self) -> None:
        self.monitor_real_portfolio_task.cancel()
        self.monitor_vtr_task.cancel()

    # ==========================================
    # 🚀 真實持倉風險動態審計 (每 30 分鐘)
    # ==========================================
    @tasks.loop(minutes=30)
    async def monitor_real_portfolio_task(self) -> None:
        """每 30 分鐘審計真實持倉風險 (DITM & Gamma Fragility)"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        logger.info("🛡️ [NRO] 開始執行真實持倉風險審計...")
        try:
            risk_events = await self.trading_service.audit_real_portfolio_risk()

            for event in risk_events:
                uid = event["uid"]
                if event["type"] == "PROFIT_LOCK":
                    if database.is_notification_enabled(uid, "profit_lock_alert"):
                        embed = create_profit_lock_alert_embed(event)
                        await self.bot.queue_dm(uid, embed=embed)

                elif event["type"] == "GAMMA_FRAGILITY":
                    if database.is_notification_enabled(uid, "gamma_fragility_alert"):
                        embed = create_gamma_fragility_embed(event)
                        await self.bot.queue_dm(uid, embed=embed)

            # 🚀 物理死鎖解除與備兌建單指引主動推播
            try:
                from database.holdings import get_all_holdings
                from market_analysis.trading_orchestration import (
                    recommend_covered_calls,
                )
                from cogs.embed_builder import create_covered_call_unlock_embed

                all_holdings = get_all_holdings()
                user_symbols: dict[int, list[str]] = {}
                for h in all_holdings:
                    u_id = h["user_id"]
                    sym = h["symbol"].upper()
                    user_symbols.setdefault(u_id, []).append(sym)

                for u_id, syms in user_symbols.items():
                    for sym in syms:
                        res = await recommend_covered_calls(u_id, sym)
                        if res and res.get("recommendations"):
                            if database.is_notification_enabled(
                                u_id, "deadlock_recovery_alert"
                            ):
                                embed = create_covered_call_unlock_embed(res)
                                await self.bot.queue_dm(u_id, embed=embed)
            except Exception as e:
                logger.error(f"物理死鎖解除審計錯誤: {e}")

            # 🚀 動態轉倉 (輕量級邏輯: 機會成本對比、再平衡防禦)
            try:
                from database.holdings import get_all_holdings

                all_holdings = get_all_holdings()
                user_assets: Dict[int, List[Dict[str, Any]]] = {}
                CORE_ETF_SYMBOLS = {"VOO", "SPY", "QQQ", "IVV", "VTI"}

                for h in all_holdings:
                    u_id = h["user_id"]
                    sym = h["symbol"].upper()
                    metrics = _get_cached_symbol_metrics(sym)

                    is_core = sym in CORE_ETF_SYMBOLS
                    default_class = "CORE" if is_core else "SATELLITE"

                    meta_asset_class = None
                    try:
                        meta = json.loads(h.get("metadata", "{}") or "{}")
                        if meta:
                            meta_asset_class = meta.get("asset_class")
                    except Exception:
                        pass

                    final_asset_class = (
                        meta_asset_class if meta_asset_class else default_class
                    )
                    default_max_alloc = 1.0 if final_asset_class == "CORE" else 0.3

                    user_assets.setdefault(u_id, []).append(
                        {
                            "symbol": sym,
                            "asset_class": final_asset_class,
                            "current_value": h.get("quantity", 0)
                            * metrics["spot_price"],
                            "target_allocation_pct": h.get(
                                "target_allocation_pct", 0.0
                            ),
                            "max_allocation_pct": h.get(
                                "max_allocation_pct", default_max_alloc
                            ),
                            "spot_price": metrics["spot_price"],
                            "ivr": metrics["ivr"],
                            "max_pain": metrics["max_pain"],
                            "put_wall": metrics["put_wall"],
                            "call_wall": metrics["call_wall"],
                            "is_uoa_sweep": metrics["is_uoa_sweep"],
                            "gamma_flip": metrics.get("gamma_flip", 0.0),
                            "sqz_mom": metrics.get("sqz_mom", 0.0),
                            "skew": metrics.get("skew", 0.0),
                        }
                    )

                for u_id, portfolio_assets in user_assets.items():
                    total_val = sum(a["current_value"] for a in portfolio_assets)

                    rebalance_instructions = (
                        self.rollover_engine.check_satellite_rebalancing(
                            portfolio_assets, total_val
                        )
                    )

                    for ins in rebalance_instructions:
                        if not database.is_notification_enabled(
                            u_id, "option_defense_alert"
                        ):
                            continue

                        embed = create_dynamic_rollover_embed(
                            rollover_type="再平衡 (Rebalancing)",
                            sell_symbol=ins["symbol"],
                            sell_ratio=ins["sell_ratio"],
                            buy_symbol=ins["target_core"],
                            reason=ins["reason"],
                            suggested_strategy=ins.get(
                                "suggested_strategy", "Buy Shares"
                            ),
                            suggested_price="Market",
                            strike="N/A",
                            expiry="N/A",
                            direction="BTO",
                        )
                        setattr(embed, "_view", f"RolloverActionView:{ins['symbol']}")
                        await self.bot.queue_dm(u_id, embed=embed)
            except Exception as e:
                logger.error(f"動態轉倉盤中審計錯誤: {e}")

        except Exception as e:
            logger.error(f"真實持倉風險審計錯誤: {e}")

    @monitor_real_portfolio_task.before_loop
    async def before_monitor_real_portfolio_task(self) -> None:
        await self.bot.wait_until_ready()

    # ==========================================
    # 🚀 VTR 監控與風險即時預警 (每 30 分鐘)
    # ==========================================
    @tasks.loop(minutes=30)
    async def monitor_vtr_task(self) -> None:
        """每 30 分鐘檢查 VTR，並在轉倉/平倉時即時通知"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        logger.info("👻 [GhostTrader] 開始掃描 VTR 持倉與風險檢查...")
        try:
            results = await self.trading_service.monitor_vtr_and_calculate_hedging()

            for res in results:
                trade_info = res.get("trade_info", {})
                if not trade_info:
                    continue

                hedge = res.get("hedge")
                uid = res.get("uid")
                if uid is None:
                    continue

                tags = trade_info.get("tags", [])
                is_ditm = any("DITM" in str(tag) for tag in tags)

                user_capital = res.get("user_capital")
                if not user_capital or user_capital <= 0:
                    user_capital = 1.0

                exposure_pct = (
                    res.get("current_total_delta", 0.0)
                    * res.get("spy_price", 0.0)
                    / user_capital
                ) * 100

                if is_ditm:
                    exit_reason = next(
                        (
                            tag.split(":", 1)[1]
                            for tag in tags
                            if tag.startswith("exit_reason:")
                        ),
                        "N/A",
                    )
                    action_taken = (
                        "已平倉 (Closed)"
                        if trade_info.get("status") == "CLOSED"
                        else "已自動轉倉 (向上/向後轉倉)"
                    )

                    if database.is_notification_enabled(uid, "option_defense_alert"):
                        embed = create_option_defense_alert_embed(
                            is_live=False,
                            symbol=trade_info.get("symbol", "N/A"),
                            status_icon="🛡️"
                            if trade_info.get("status") == "ROLLED"
                            else "🔴",
                            action_taken=action_taken,
                            pnl=float(trade_info.get("pnl", 0.0)),
                            exposure_pct=exposure_pct,
                            exit_reason=exit_reason,
                            hedge=hedge,
                        )
                        await self.bot.queue_dm(uid, embed=embed)
                else:
                    if database.is_notification_enabled(uid, "option_defense_alert"):
                        status_icon = (
                            "🔄" if trade_info.get("status") == "ROLLED" else "🔴"
                        )
                        action_taken_str = (
                            "已自動轉倉 (Rolled)"
                            if trade_info.get("status") == "ROLLED"
                            else "已自動平倉 (Closed)"
                        )
                        await self.bot.queue_dm(
                            uid,
                            embed=create_option_defense_alert_embed(
                                is_live=False,
                                symbol=trade_info.get("symbol", "N/A"),
                                status_icon=status_icon,
                                action_taken=action_taken_str,
                                pnl=float(trade_info.get("pnl", 0.0)),
                                exposure_pct=exposure_pct,
                                regime=res.get("regime"),
                                target_delta=res.get("target_delta"),
                                hedge=hedge,
                            ),
                        )

        except Exception as e:
            logger.error(f"VTR 對沖連動任務錯誤: {e}")

    @monitor_vtr_task.before_loop
    async def before_monitor_vtr_task(self) -> None:
        await self.bot.wait_until_ready()

    # ==========================================
    # 🚀 每週 VTR 績效週報 (美東週五 17:05)
    # ==========================================
    @tasks.loop(time=time(hour=17, minute=5, tzinfo=ny_tz))
    async def weekly_vtr_report_task(self) -> None:
        """每週五收盤後：自動推送 VTR 績效週報"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        from datetime import datetime as _dt

        now = _dt.now(ny_tz)
        if now.weekday() != 4:
            return

        logger.info("📅 [Weekly Report] 偵測到週五收盤，開始產生績效週報...")

        all_watchlists = database.get_all_watchlist()
        unique_users = set(row[0] for row in all_watchlists)

        for uid in unique_users:
            try:
                if not database.is_notification_enabled(uid, "weekly_vtr_report"):
                    continue
                stats = await GhostTrader.get_vtr_performance_stats(uid)
                if stats["total_trades"] > 0:
                    user = await self.bot.fetch_user(uid)
                    embed = build_vtr_stats_embed(user.display_name, stats)
                    await self.bot.queue_dm(uid, embed=embed)
                    logger.info(f"✅ 週報已發送給用戶 {uid}")
            except Exception as e:
                logger.error(f"發送週報給 {uid} 失敗: {e}")

    @weekly_vtr_report_task.before_loop
    async def before_weekly_vtr_report_task(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(PortfolioMonitorCog(bot))
