"""
cogs/trading/portfolio_monitor.py

真實持倉風險動態審計 (每 30 分鐘)：DITM、Gamma Fragility、動態轉倉，以及 VTR 監控。
"""

from typing import Any, Dict, List, Optional
import json
import logging
from datetime import time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import database
import market_time
from services.trading_service import TradingService
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    CORE_DEFENSE_ETF_SYMBOLS,
)
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
                    if database.is_notification_enabled(uid, "defense_portfolio_risk"):
                        embed = create_profit_lock_alert_embed(event)
                        await self.bot.queue_dm(uid, embed=embed)

                elif event["type"] == "GAMMA_FRAGILITY":
                    if database.is_notification_enabled(uid, "defense_portfolio_risk"):
                        embed = create_gamma_fragility_embed(event)
                        await self.bot.queue_dm(uid, embed=embed)

                elif event["type"] == "MARGIN_API":
                    if database.is_notification_enabled(uid, "defense_portfolio_risk"):
                        from cogs.embed_builders.alert_embeds import (
                            create_margin_api_alert_embed,
                        )

                        embed = create_margin_api_alert_embed(event["ratio"])
                        await self.bot.queue_dm(uid, embed=embed)

            import asyncio
            from database.holdings import get_all_holdings

            all_holdings = get_all_holdings()

            # --- 提前抓取雷達數據，供後續模組共用 ---
            terminal_cog = getattr(self, "bot", None) and self.bot.get_cog(
                "UnifiedTerminalCog"
            )
            radar_cache_map: Dict[str, Any] = {}

            for h in all_holdings:
                sym = h["symbol"].upper()
                if sym not in radar_cache_map:
                    if terminal_cog:
                        try:
                            # 節流保護：若已抓取過其他標的，先休息 1.5 秒
                            if radar_cache_map:
                                await asyncio.sleep(1.5)
                            radar_cache_map[
                                sym
                            ] = await terminal_cog._fetch_sym_radar_data_slow(sym)
                        except Exception as ex:
                            logger.error(f"Failed to fetch radar data for {sym}: {ex}")
                            radar_cache_map[sym] = None
                    else:
                        radar_cache_map[sym] = None

            # 🚀 物理死鎖解除與備兌建單指引主動推播
            try:
                from market_analysis.trading_orchestration import (
                    recommend_covered_calls,
                )
                from cogs.embed_builder import create_covered_call_unlock_embed

                user_symbols: dict[int, list[str]] = {}
                for h in all_holdings:
                    u_id = h["user_id"]
                    sym = h["symbol"].upper()
                    user_symbols.setdefault(u_id, []).append(sym)

                for u_id, syms in user_symbols.items():
                    for sym in set(syms):
                        # 檢查雷達數據，若符合強勢多頭+低IV+強支撐條件則阻斷
                        r_data = radar_cache_map.get(sym)
                        if r_data:
                            ivr = float(
                                r_data.get("iv_metrics", {}).get("iv_rank", 0.0)
                                if r_data.get("iv_metrics")
                                else 0.0
                            )
                            spot = float(
                                r_data.get("quote", {}).get("c", 0.0)
                                if r_data.get("quote")
                                else 0.0
                            )
                            put_wall = float(
                                r_data.get("gex_profile_data", {}).get("put_wall", 0.0)
                                if isinstance(r_data.get("gex_profile_data"), dict)
                                else 0.0
                            )
                            sqz_mom = float(
                                r_data.get("psq_result", {}).get("momentum_value", 0.0)
                                if r_data.get("psq_result")
                                else 0.0
                            )

                            if ivr <= 5.0 and sqz_mom > 10.0 and spot > put_wall:
                                logger.info(
                                    f"[{sym}] 處於零溢價與多頭強勢巡航形態 (IVR: {ivr}%, SQZ: {sqz_mom}, Spot: {spot} > PutWall: {put_wall})，拒絕物理死鎖解除與備兌建單。"
                                )
                                continue

                        res = await recommend_covered_calls(u_id, sym)
                        if res and res.get("recommendations"):
                            if database.is_notification_enabled(
                                u_id, "defense_option_rollover"
                            ):
                                embed = create_covered_call_unlock_embed(res)
                                await self.bot.queue_dm(u_id, embed=embed)
            except Exception as e:
                logger.error(f"物理死鎖解除審計錯誤: {e}")

            # 🚀 動態轉倉 (輕量級邏輯: 機會成本對比、再平衡防禦)
            try:
                user_assets: Dict[int, List[Dict[str, Any]]] = {}

                for h in all_holdings:
                    u_id = h["user_id"]
                    sym = h["symbol"].upper()

                    r_data = radar_cache_map.get(sym)
                    if r_data:
                        try:
                            metrics = {
                                "spot_price": float(
                                    r_data.get("quote", {}).get("c", 0.0)
                                    if r_data.get("quote")
                                    else 0.0
                                ),
                                "ivr": float(
                                    r_data.get("iv_metrics", {}).get("iv_rank", 0.0)
                                    if r_data.get("iv_metrics")
                                    else 0.0
                                ),
                                "max_pain": float(
                                    r_data.get("max_pain", {}).get("max_pain") or 0.0
                                )
                                if isinstance(r_data.get("max_pain"), dict)
                                else (
                                    float(r_data.get("max_pain"))
                                    if r_data.get("max_pain")
                                    else 0.0
                                ),
                                "put_wall": float(
                                    r_data.get("gex_profile_data", {}).get(
                                        "put_wall", 0.0
                                    )
                                    or 0.0
                                )
                                if isinstance(r_data.get("gex_profile_data"), dict)
                                else 0.0,
                                "call_wall": float(
                                    r_data.get("gex_profile_data", {}).get(
                                        "call_wall", 0.0
                                    )
                                    or 0.0
                                )
                                if isinstance(r_data.get("gex_profile_data"), dict)
                                else 0.0,
                                "is_uoa_sweep": len(r_data.get("uoa", [])) > 0
                                if r_data.get("uoa")
                                else False,
                                "gamma_flip": float(
                                    r_data.get("gex_profile_data", {}).get(
                                        "gamma_flip", 0.0
                                    )
                                    or 0.0
                                )
                                if isinstance(r_data.get("gex_profile_data"), dict)
                                else 0.0,
                                "sqz_mom": float(
                                    r_data.get("psq_result", {}).get(
                                        "momentum_value", 0.0
                                    )
                                    if r_data.get("psq_result")
                                    else 0.0
                                ),
                                "skew": float(
                                    r_data.get("skew", 0.0)
                                    if r_data.get("skew")
                                    else 0.0
                                ),
                                "atr_14": float(r_data.get("atr_14", 0.0)),
                                "hvn": float(r_data.get("vp_data", {}).get("hvn", 0.0))
                                if isinstance(r_data.get("vp_data"), dict)
                                else 0.0,
                                "lvn": float(r_data.get("vp_data", {}).get("lvn", 0.0))
                                if isinstance(r_data.get("vp_data"), dict)
                                else 0.0,
                                "dte": int(r_data.get("nearest_dte"))
                                if r_data.get("nearest_dte") is not None
                                else 99,
                            }
                        except Exception as parse_ex:
                            logger.error(
                                f"Failed to parse radar data for {sym}: {parse_ex}"
                            )
                            fallback_metrics = {
                                "spot_price": 0.0,
                                "ivr": 0.0,
                                "max_pain": 0.0,
                                "put_wall": 0.0,
                                "call_wall": 0.0,
                                "is_uoa_sweep": False,
                                "gamma_flip": 0.0,
                                "sqz_mom": 0.0,
                                "skew": 0.0,
                                "atr_14": 0.0,
                                "hvn": 0.0,
                                "lvn": 0.0,
                                "dte": 99,
                            }
                            metrics = fallback_metrics
                    else:
                        fallback_metrics = {
                            "spot_price": 0.0,
                            "ivr": 0.0,
                            "max_pain": 0.0,
                            "put_wall": 0.0,
                            "call_wall": 0.0,
                            "is_uoa_sweep": False,
                            "gamma_flip": 0.0,
                            "sqz_mom": 0.0,
                            "skew": 0.0,
                            "atr_14": 0.0,
                            "hvn": 0.0,
                            "lvn": 0.0,
                            "dte": 99,
                        }
                        metrics = fallback_metrics

                    is_core = sym in CORE_DEFENSE_ETF_SYMBOLS
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

                    # target_allocation_pct 目前無 DB 欄位持久化、也無 /settings UI 可設定，
                    # 因此只在持倉本身真的帶有明確數值時才寫入該 key，讓
                    # check_satellite_rebalancing 既有的 asset.get(..., max_alloc) fallback
                    # 生效（退回「修剪至上限」而非誤判為「目標配置 0%」導致近乎全清倉）。
                    asset_entry: Dict[str, Any] = {
                        "symbol": sym,
                        "asset_class": final_asset_class,
                        "quantity": h.get("quantity", 0),
                        "current_value": h.get("quantity", 0) * metrics["spot_price"],
                        "max_allocation_pct": h.get(
                            "max_allocation_pct", default_max_alloc
                        ),
                        "spot_price": metrics["spot_price"],
                        "price_15m_close": metrics.get(
                            "price_15m_close", metrics["spot_price"]
                        ),
                        "ivr": metrics["ivr"],
                        "max_pain": metrics["max_pain"],
                        "put_wall": metrics["put_wall"],
                        "call_wall": metrics["call_wall"],
                        "is_uoa_sweep": metrics["is_uoa_sweep"],
                        "gamma_flip": metrics.get("gamma_flip", 0.0),
                        "sqz_mom": metrics.get("sqz_mom", 0.0),
                        "skew": metrics.get("skew", 0.0),
                        "atr_14": metrics.get("atr_14", 0.0),
                        "atr_15m": metrics.get("atr_15m", metrics.get("atr_14", 0.0)),
                        "hvn": metrics.get("hvn", 0.0),
                        "lvn": metrics.get("lvn", 0.0),
                        "dte": metrics.get("dte", 99),
                        "gex_profile_data": r_data.get("gex_profile_data", {})
                        if r_data
                        else {},
                        "avg_cost": h.get("avg_cost", 0.0),
                        "psq_result": r_data.get("psq_result", {}) if r_data else {},
                    }
                    if h.get("target_allocation_pct") is not None:
                        asset_entry["target_allocation_pct"] = h.get(
                            "target_allocation_pct"
                        )
                    user_assets.setdefault(u_id, []).append(asset_entry)

                for u_id, portfolio_assets in user_assets.items():
                    total_val = sum(a["current_value"] for a in portfolio_assets)

                    rebalance_instructions = (
                        await self.rollover_engine.check_satellite_rebalancing(
                            u_id, portfolio_assets, total_val
                        )
                    )

                    # 🚀 邏輯 (2): 機會成本轉倉 — 對尚未被 Scenario 3 標記的
                    # SATELLITE 持倉，比對單一預篩選高 EV 候選標的的機會成本
                    already_flagged = {ins["symbol"] for ins in rebalance_instructions}
                    candidate_symbol = self.rollover_engine._find_best_rollover_target(
                        u_id, exclude_symbols={a["symbol"] for a in portfolio_assets}
                    )
                    candidate_radar = radar_cache_map.get(candidate_symbol)
                    if (
                        candidate_symbol != "VOO"
                        and candidate_radar is None
                        and terminal_cog
                    ):
                        try:
                            if radar_cache_map:
                                await asyncio.sleep(1.5)  # 沿用既有節流保護
                            candidate_radar = (
                                await terminal_cog._fetch_sym_radar_data_slow(
                                    candidate_symbol
                                )
                            )
                            radar_cache_map[candidate_symbol] = candidate_radar
                        except Exception as ex:
                            logger.error(
                                f"Failed to fetch candidate radar data for {candidate_symbol}: {ex}"
                            )

                    rebalance_instructions += await self.rollover_engine.evaluate_opportunity_cost_for_satellites(
                        u_id,
                        portfolio_assets,
                        already_flagged,
                        candidate_symbol,
                        candidate_radar,
                    )

                    # 🚀 邏輯 (4): 槓桿與保證金防禦 — 排除已被 Scenario 2/3 標記過的
                    # 標的，避免同一標的同一輪次收到互相矛盾的清倉指令。
                    already_flagged = {ins["symbol"] for ins in rebalance_instructions}
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_margin_defense(
                            u_id,
                            portfolio_assets,
                            already_flagged_symbols=already_flagged,
                        )
                    )

                    # 情境識別碼 → 人類可讀標籤，僅供標題補充說明；顏色/危險等級判斷
                    # 由 create_dynamic_rollover_embed 依 scenario 明確對照表決定，
                    # 不再依賴此處字串是否包含特定關鍵字。
                    _SCENARIO_LABELS = {
                        "OPPORTUNITY_COST": "機會成本轉倉",
                        "SATELLITE_REBALANCE": "核心衛星再平衡",
                        "MARGIN_DEFENSE": "槓桿與保證金防禦",
                    }

                    for ins in rebalance_instructions:
                        if not database.is_notification_enabled(
                            u_id, "defense_option_rollover"
                        ):
                            continue

                        scenario = ins.get("scenario", "UNKNOWN")
                        scenario_label = _SCENARIO_LABELS.get(scenario, "動態轉倉")
                        # 若為 HOLD 狀態且無減倉動作，通常不主動洗版，但若有指示則發送安心防守卡
                        rollover_type = (
                            f"持倉防守 ({scenario_label})"
                            if ins["action"] == "HOLD"
                            else scenario_label
                        )
                        suggested_price = (
                            "N/A (維持現狀)" if ins["action"] == "HOLD" else "Market"
                        )

                        embed = create_dynamic_rollover_embed(
                            rollover_type=rollover_type,
                            sell_symbol=ins["symbol"],
                            sell_ratio=ins["sell_ratio"],
                            buy_symbol=ins["target_core"],
                            reason=ins["reason"],
                            suggested_strategy=ins.get(
                                "suggested_strategy", "Buy Shares"
                            ),
                            suggested_price=suggested_price,
                            strike="N/A",
                            expiry="N/A",
                            direction="BTO" if ins["action"] != "HOLD" else "HOLD",
                            sell_action=ins.get("sell_action", "STC"),
                            buy_action_label=ins.get("buy_action_label"),
                            scenario=scenario,
                            cash_impact=ins.get("cash_impact"),
                            trigger_condition_text=ins.get("trigger_condition_text"),
                        )
                        if ins.get("is_manual_override_required"):
                            setattr(
                                embed, "_view", f"ManualOverrideView:{ins['symbol']}"
                            )
                        else:
                            setattr(
                                embed, "_view", f"RolloverActionView:{ins['symbol']}"
                            )
                        await self.bot.queue_dm(u_id, embed=embed)
            except Exception as e:
                logger.error(f"動態轉倉盤中審計錯誤: {e}")

        except Exception as e:
            logger.error(f"真實持倉風險審計錯誤: {e}")

    @monitor_real_portfolio_task.before_loop
    async def before_monitor_real_portfolio_task(self) -> None:
        await self.bot.wait_until_ready()

    async def handle_microstructure_interrupt(
        self,
        user_id: int,
        symbol: str,
        trigger_type: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        微觀異動事件中斷器 (Interrupt Handler)：
        當盤中發生「標的價格穿透 Zero Gamma 轉為負 Gamma 領域」(ZERO_GAMMA_BREAKDOWN) 或
        「單筆 UOA 溢價暴增」(MASSIVE_WHALE_UOA) 時，即時中斷定時器，
        秒級觸發該用戶與該標的的再平衡審計並推送緊急防守卡。
        """
        if not database.is_notification_enabled(user_id, "defense_option_rollover"):
            return []

        logger.info(
            f"⚡ [Microstructure Interrupt] 觸發異動事件中斷: user={user_id}, symbol={symbol}, trigger={trigger_type}"
        )
        try:
            holdings = database.get_user_holdings(user_id)
            target_holding = next(
                (h for h in holdings if h.get("symbol", "").upper() == symbol.upper()),
                None,
            )
            if not target_holding:
                return []

            total_val = sum(float(h.get("current_value", 0.0)) for h in holdings)
            if total_val <= 0:
                total_val = 1.0

            r_data = data or {}
            metrics = r_data.get("metrics", {})
            asset_entry: Dict[str, Any] = {
                "symbol": symbol.upper(),
                "asset_class": target_holding.get("asset_class", "SATELLITE"),
                "instrument_type": target_holding.get("instrument_type", "SPOT"),
                "current_value": float(target_holding.get("current_value", 0.0)),
                "quantity": float(target_holding.get("quantity", 0.0)),
                "max_allocation_pct": float(
                    target_holding.get("max_allocation_pct", 0.3)
                ),
                "spot_price": metrics.get(
                    "spot_price", target_holding.get("spot_price", 0.0)
                ),
                "put_wall": metrics.get("put_wall", 0.0),
                "call_wall": metrics.get("call_wall", 0.0),
                "max_pain": metrics.get("max_pain", 0.0),
                "ivr": metrics.get("ivr", 0.0),
                "is_uoa_sweep": metrics.get("is_uoa_sweep", False),
                "sqz_mom": metrics.get("sqz_mom", 0.0),
                "skew": metrics.get("skew", 0.0),
                "skew_percentile": metrics.get("skew_percentile", 50.0),
                "gamma_flip": metrics.get("gamma_flip", 0.0),
                "atr_14": metrics.get("atr_14", 0.0),
                "atr_15m": metrics.get("atr_15m", 0.0),
                "price_15m_close": metrics.get("price_15m_close", 0.0),
                "hvn": metrics.get("hvn", 0.0),
                "lvn": metrics.get("lvn", 0.0),
                "dte": metrics.get("dte", 99),
                "gex_profile_data": r_data.get("gex_profile_data", {}),
            }
            if target_holding.get("target_allocation_pct") is not None:
                asset_entry["target_allocation_pct"] = float(
                    target_holding.get("target_allocation_pct")
                )

            instructions = await self.rollover_engine.check_satellite_rebalancing(
                user_id, [asset_entry], total_val
            )

            for ins in instructions:
                rollover_type = (
                    f"⚡ 微觀事件防守: {trigger_type}"
                    if ins["action"] == "HOLD"
                    else f"⚡ 微觀事件轉倉: {trigger_type}"
                )
                suggested_price = (
                    "N/A (維持現狀)" if ins["action"] == "HOLD" else "Market"
                )

                embed = create_dynamic_rollover_embed(
                    rollover_type=rollover_type,
                    sell_symbol=ins["symbol"],
                    sell_ratio=ins["sell_ratio"],
                    buy_symbol=ins["target_core"],
                    reason=ins["reason"],
                    suggested_strategy=ins.get("suggested_strategy", "Buy Shares"),
                    suggested_price=suggested_price,
                    strike="N/A",
                    expiry="N/A",
                    direction="BTO" if ins["action"] != "HOLD" else "HOLD",
                    sell_action=ins.get("sell_action", "STC"),
                    buy_action_label=ins.get("buy_action_label"),
                    scenario=ins.get("scenario", "SATELLITE_REBALANCE"),
                    cash_impact=ins.get("cash_impact"),
                    trigger_condition_text=ins.get("trigger_condition_text"),
                )
                if ins.get("is_manual_override_required"):
                    setattr(embed, "_view", f"ManualOverrideView:{ins['symbol']}")
                else:
                    setattr(embed, "_view", f"RolloverActionView:{ins['symbol']}")
                await self.bot.queue_dm(user_id, embed=embed)

            return instructions
        except Exception as e:
            logger.error(f"微觀異動事件中斷處理失敗: {e}")
            return []

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

                    if database.is_notification_enabled(uid, "defense_option_rollover"):
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
                    if database.is_notification_enabled(uid, "defense_option_rollover"):
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
                if not database.is_notification_enabled(uid, "briefing_weekly_vtr"):
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


async def setup(bot: Any) -> None:
    await bot.add_cog(PortfolioMonitorCog(bot))
