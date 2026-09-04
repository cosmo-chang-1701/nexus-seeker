"""
cogs/trading/portfolio_monitor.py

真實持倉風險動態審計 (每 15 分鐘)：DITM、Gamma Fragility、動態轉倉，以及 VTR 監控。
"""

from typing import Any, Dict, List, Optional
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import config
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
from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
    create_covered_call_overlay_embed,
    create_covered_call_profit_lock_embed,
)

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

portfolio_scanner_times = [
    time(hour=h, minute=m, tzinfo=ny_tz) for h in range(24) for m in (5, 20, 35, 50)
]


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

    async def _build_symbol_metrics(
        self, sym: str, r_data: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """彙整單一標的的量化指標快照，供現貨與期權部位共用同一份計算結果。"""
        fallback_metrics: Dict[str, Any] = {
            "spot_price": 0.0,
            "price_15m_close": 0.0,
            "ivr": 0.0,
            "ivr_drop": 0.0,
            "max_pain": 0.0,
            "put_wall": 0.0,
            "call_wall": 0.0,
            "is_uoa_sweep": False,
            "gamma_flip": 0.0,
            "sqz_mom": 0.0,
            "skew": 0.0,
            "atr_14": 0.0,
            "atr_15m": 0.0,
            "hvn": 0.0,
            "lvn": 0.0,
            "dte": 99,
            "iv_term_structure_status": None,
        }
        if not r_data:
            return fallback_metrics
        try:
            # 追蹤 IVR 變動量 (供期權快速通道偵測 IV 崩塌)
            curr_ivr = float(
                r_data.get("iv_metrics", {}).get("iv_rank", 0.0)
                if r_data.get("iv_metrics")
                else 0.0
            )
            ivr_drop_val = 0.0
            try:
                from database.cache import get_kv_cache, save_kv_cache

                prev_ivr_val = get_kv_cache(f"prev_ivr_{sym.upper()}")
                if prev_ivr_val is not None:
                    prev_ivr = float(prev_ivr_val)
                    if prev_ivr > curr_ivr:
                        ivr_drop_val = prev_ivr - curr_ivr
                await save_kv_cache(f"prev_ivr_{sym.upper()}", curr_ivr)
            except Exception:
                pass

            atr_val = float(r_data.get("atr_14", 0.0))
            atr_15m_val = float(r_data.get("atr_15m", 0.0))
            spot_val = float(
                r_data.get("quote", {}).get("c", 0.0) if r_data.get("quote") else 0.0
            )

            raw_max_pain = r_data.get("max_pain")
            max_pain_val = (
                float(raw_max_pain.get("max_pain") or 0.0)
                if isinstance(raw_max_pain, dict)
                else (float(raw_max_pain) if raw_max_pain else 0.0)
            )
            raw_dte = r_data.get("nearest_dte")
            dte_val = int(raw_dte) if raw_dte is not None else 99

            return {
                "spot_price": spot_val,
                "price_15m_close": spot_val,
                "ivr": curr_ivr,
                "ivr_drop": ivr_drop_val,
                "max_pain": max_pain_val,
                "put_wall": float(
                    r_data.get("gex_profile_data", {}).get("put_wall", 0.0) or 0.0
                )
                if isinstance(r_data.get("gex_profile_data"), dict)
                else 0.0,
                "call_wall": float(
                    r_data.get("gex_profile_data", {}).get("call_wall", 0.0) or 0.0
                )
                if isinstance(r_data.get("gex_profile_data"), dict)
                else 0.0,
                "is_uoa_sweep": len(r_data.get("uoa", [])) > 0
                if r_data.get("uoa")
                else False,
                "gamma_flip": float(
                    r_data.get("gex_profile_data", {}).get("gamma_flip", 0.0) or 0.0
                )
                if isinstance(r_data.get("gex_profile_data"), dict)
                else 0.0,
                "sqz_mom": float(
                    r_data.get("psq_result", {}).get("momentum_value", 0.0)
                    if r_data.get("psq_result")
                    else 0.0
                ),
                "skew": float(r_data.get("skew", 0.0) if r_data.get("skew") else 0.0),
                "atr_14": atr_val,
                "atr_15m": atr_15m_val,
                "hvn": float(r_data.get("vp_data", {}).get("hvn", 0.0))
                if isinstance(r_data.get("vp_data"), dict)
                else 0.0,
                "lvn": float(r_data.get("vp_data", {}).get("lvn", 0.0))
                if isinstance(r_data.get("vp_data"), dict)
                else 0.0,
                "dte": dte_val,
                "iv_term_structure_status": (
                    r_data.get("iv_metrics", {}).get("iv_term_structure_status")
                    if isinstance(r_data.get("iv_metrics"), dict)
                    else None
                ),
            }
        except Exception as parse_ex:
            logger.error(f"Failed to parse radar data for {sym}: {parse_ex}")
            return fallback_metrics

    @staticmethod
    def _build_option_asset_entry(
        opt_sym: str,
        quantity: float,
        mid_price: float,
        bid: float,
        ask: float,
        metrics: Dict[str, Any],
        r_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """組裝單筆多頭期權持倉的 asset_entry，供動態轉倉引擎評估迴圈使用。

        選擇權合約本身恆為戰術性部位：即使標的是 CORE 防禦 ETF，也不套用
        CORE 的無上限配置假設，避免 evaluate_core_deployment /
        evaluate_covered_call_overlay 誤處理。期權部位無法從既有資料推導
        單筆成本基礎 (見 anti_washout.py 的 acquired_at 估算邏輯)，
        avg_cost/acquired_at 明確降級為 0.0/None。
        """
        return {
            "symbol": opt_sym,
            "asset_class": "SATELLITE",
            "instrument_type": "OPTIONS_CONTRACT",
            "quantity": quantity,
            "current_value": quantity * mid_price * 100.0,
            "max_allocation_pct": 0.3,
            "spot_price": metrics["spot_price"],
            "price_15m_close": metrics.get("price_15m_close", metrics["spot_price"]),
            "ivr": metrics["ivr"],
            "ivr_drop": metrics.get("ivr_drop", 0.0),
            "max_pain": metrics["max_pain"],
            "put_wall": metrics["put_wall"],
            "call_wall": metrics["call_wall"],
            "is_uoa_sweep": metrics["is_uoa_sweep"],
            "gamma_flip": metrics.get("gamma_flip", 0.0),
            "sqz_mom": metrics.get("sqz_mom", 0.0),
            "skew": metrics.get("skew", 0.0),
            "atr_14": metrics.get("atr_14", 0.0),
            "atr_15m": metrics.get("atr_15m", 0.0),
            "hvn": metrics.get("hvn", 0.0),
            "lvn": metrics.get("lvn", 0.0),
            "dte": metrics.get("dte", 99),
            "iv_term_structure_status": metrics.get("iv_term_structure_status"),
            "gex_profile_data": r_data.get("gex_profile_data", {}) if r_data else {},
            "avg_cost": 0.0,
            "psq_result": r_data.get("psq_result", {}) if r_data else {},
            "acquired_at": None,
            "bid": bid,
            "ask": ask,
            "boxx_allocation_pct": None,
        }

    # ==========================================
    # 🚀 真實持倉風險動態審計 (每 15 分鐘，於 :05、:20、:35、:50 執行，
    # 固定落後 SchedulerCog.dynamic_market_scanner 5 分鐘以消費其共用雷達快取)
    # ==========================================
    @tasks.loop(time=portfolio_scanner_times)
    async def monitor_real_portfolio_task(self) -> None:
        """每 15 分鐘審計真實持倉風險 (DITM & Gamma Fragility)"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        from services.llm_service import is_memory_safe

        if not is_memory_safe():
            logger.warning("🛡️ [NRO] 記憶體水位過高 (RAM+Swap > 85%)，跳過本輪審計。")
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

            # 🚀 動態轉倉引擎：真實期權持倉併入評估迴圈 (Feature Flag，預設關閉)。
            # 僅納入多頭買方部位 (quantity > 0) 至 Scenario 2/3/4/5 的 SATELLITE
            # 評估迴圈；空頭 (STO) 部位風險輪廓相反 (時間價值衰減對我方有利)，
            # 套用該迴圈的結構性破位邏輯會產生方向錯誤的清倉指令。開立新備兌
            # 買權已由既有 evaluate_covered_call_overlay / recommend_covered_calls
            # 覆蓋；既有空頭 CALL 部位的提前 BTC 回補了結則由下方獨立的
            # evaluate_covered_call_profit_lock 處理 (見 short_call_trades)。
            long_option_trades: List[Dict[str, Any]] = []
            short_call_trades: List[Dict[str, Any]] = []
            if config.ENABLE_OPTIONS_ROLLOVER_INGESTION:
                from database.portfolio import get_all_trade_positions

                all_trades_raw = get_all_trade_positions()
                long_option_trades = [
                    t for t in all_trades_raw if float(t.get("quantity") or 0) > 0
                ]
                short_call_trades = [
                    t
                    for t in all_trades_raw
                    if float(t.get("quantity") or 0) < 0
                    and str(t.get("opt_type", "")).lower() == "call"
                ]
                skipped_short_count = (
                    len(all_trades_raw)
                    - len(long_option_trades)
                    - len(short_call_trades)
                )
                if skipped_short_count:
                    logger.debug(
                        f"[OptionsRollover] 略過 {skipped_short_count} 筆非多頭/"
                        "非空頭 CALL 期權部位 (不在動態轉倉引擎評估範圍內)"
                    )

            # --- 提前抓取雷達數據，供後續模組共用 ---
            terminal_cog = getattr(self, "bot", None) and self.bot.get_cog(
                "UnifiedTerminalCog"
            )
            radar_cache_map: Dict[str, Any] = {}
            import time

            shared_cache = getattr(self.bot, "_latest_radar_data_cache", {}) or {}
            shared_time = float(
                getattr(self.bot, "_latest_radar_cache_time", 0.0) or 0.0
            )
            is_shared_fresh = (time.time() - shared_time) < 300.0

            all_symbols_needed = {h["symbol"].upper() for h in all_holdings} | {
                str(t["symbol"]).upper() for t in long_option_trades
            }
            symbols_to_query: set[str] = set()
            for sym in all_symbols_needed:
                if (
                    is_shared_fresh
                    and sym in shared_cache
                    and isinstance(shared_cache[sym], dict)
                ):
                    radar_cache_map[sym] = shared_cache[sym]
                elif sym not in radar_cache_map:
                    symbols_to_query.add(sym)

            if symbols_to_query and terminal_cog:
                sem = asyncio.Semaphore(3)

                async def _fetch_one_holding_radar(s: str) -> tuple[str, Any]:
                    async with sem:
                        try:
                            data = await terminal_cog._fetch_sym_radar_data_slow(s)
                            return s, data
                        except Exception as ex:
                            logger.error(f"Failed to fetch radar data for {s}: {ex}")
                            return s, None

                fetch_res = await asyncio.gather(
                    *[_fetch_one_holding_radar(s) for s in sorted(symbols_to_query)]
                )
                for s, data in fetch_res:
                    radar_cache_map[s] = data
            elif symbols_to_query:
                for s in symbols_to_query:
                    radar_cache_map[s] = None

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

                cc_unlock_today_str = datetime.now(market_time.ny_tz).strftime("%Y%m%d")

                for u_id, syms in user_symbols.items():
                    for sym in set(syms):
                        cc_unlock_cache_key = (
                            f"cc_unlock_{u_id}_{sym}_{cc_unlock_today_str}"
                        )
                        if database.get_kv_cache(cc_unlock_cache_key):
                            continue
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
                                await database.save_kv_cache(cc_unlock_cache_key, 1)
            except Exception as e:
                logger.error(f"物理死鎖解除審計錯誤: {e}")

            # 🚀 動態轉倉 (輕量級邏輯: 機會成本對比、再平衡防禦)
            try:
                user_assets: Dict[int, List[Dict[str, Any]]] = {}

                # 現貨與期權部位共用同一份標的層級量化指標快照 (避免重複計算，
                # 也讓 prev_ivr_{symbol} kv_cache 每個標的每輪只讀寫一次，而非
                # 每筆持倉都重複讀寫)。
                symbol_metrics_map: Dict[str, Dict[str, Any]] = {}
                for sym in all_symbols_needed:
                    symbol_metrics_map[sym] = await self._build_symbol_metrics(
                        sym, radar_cache_map.get(sym)
                    )

                for h in all_holdings:
                    u_id = h["user_id"]
                    sym = h["symbol"].upper()
                    metrics = symbol_metrics_map[sym]

                    is_core = sym in CORE_DEFENSE_ETF_SYMBOLS
                    default_class = "CORE" if is_core else "SATELLITE"

                    # asset_class/max_allocation_pct/target_allocation_pct 現由
                    # /edit_holding 持久化於 assets.metadata（database/holdings.py
                    # 的 get_user_holdings() 已展平為頂層欄位），僅在使用者未曾
                    # 手動設定時才退回此處的預設值。target_allocation_pct 僅在
                    # 使用者真的設定過時才寫入該 key，讓 check_satellite_rebalancing
                    # 既有的 asset.get(..., max_alloc) fallback 生效（退回「修剪至
                    # 上限」而非誤判為「目標配置 0%」導致近乎全清倉）。
                    final_asset_class = h.get("asset_class") or default_class
                    default_max_alloc = 1.0 if final_asset_class == "CORE" else 0.3

                    asset_entry: Dict[str, Any] = {
                        "symbol": sym,
                        "asset_class": final_asset_class,
                        "quantity": h.get("quantity", 0),
                        "current_value": h.get("quantity", 0) * metrics["spot_price"],
                        "max_allocation_pct": h.get("max_allocation_pct")
                        if h.get("max_allocation_pct") is not None
                        else default_max_alloc,
                        "spot_price": metrics["spot_price"],
                        "price_15m_close": metrics.get(
                            "price_15m_close", metrics["spot_price"]
                        ),
                        "ivr": metrics["ivr"],
                        "ivr_drop": metrics.get("ivr_drop", 0.0),
                        "max_pain": metrics["max_pain"],
                        "put_wall": metrics["put_wall"],
                        "call_wall": metrics["call_wall"],
                        "is_uoa_sweep": metrics["is_uoa_sweep"],
                        "gamma_flip": metrics.get("gamma_flip", 0.0),
                        "sqz_mom": metrics.get("sqz_mom", 0.0),
                        "skew": metrics.get("skew", 0.0),
                        "atr_14": metrics.get("atr_14", 0.0),
                        "atr_15m": metrics.get("atr_15m", 0.0),
                        "hvn": metrics.get("hvn", 0.0),
                        "lvn": metrics.get("lvn", 0.0),
                        "dte": metrics.get("dte", 99),
                        "iv_term_structure_status": metrics.get(
                            "iv_term_structure_status"
                        ),
                        "gex_profile_data": radar_cache_map[sym].get(
                            "gex_profile_data", {}
                        )
                        if radar_cache_map.get(sym)
                        else {},
                        "avg_cost": h.get("avg_cost", 0.0),
                        "psq_result": radar_cache_map[sym].get("psq_result", {})
                        if radar_cache_map.get(sym)
                        else {},
                        "acquired_at": h.get("acquired_at"),
                        # 核心資金部署引擎 (Scenario 5) 的 BOXX 防禦閾值：None 時
                        # evaluate_core_deployment() 會自動改用
                        # suggest_boxx_allocation_pct() 的總經自動建議值。
                        "boxx_allocation_pct": h.get("boxx_allocation_pct"),
                    }
                    if h.get("target_allocation_pct") is not None:
                        asset_entry["target_allocation_pct"] = h.get(
                            "target_allocation_pct"
                        )
                    user_assets.setdefault(u_id, []).append(asset_entry)

                # 🚀 期權部位併入動態轉倉評估迴圈 (Feature Flag)。此機制與
                # audit_real_portfolio_risk() 的 PROFIT_LOCK/GAMMA_FRAGILITY
                # 判斷完全獨立 (一個判斷「該獲利了結」，一個判斷「該轉倉/清倉
                # 防禦」)，兩者刻意保持獨立，未來重構不應合併耦合。
                # Covered Call 權利金衰減停利 (evaluate_covered_call_profit_lock)
                # 所需的即時報價，併入下方同一批 Semaphore(3) 併發抓取，避免對
                # 同一批合約 (若剛好與 long_option_trades 重疊) 重複發送請求。
                user_short_calls: Dict[int, List[Dict[str, Any]]] = {}
                if long_option_trades or short_call_trades:
                    quote_sem = asyncio.Semaphore(3)
                    unique_contracts: Dict[
                        tuple[str, Any, Any, Any], Dict[str, Any]
                    ] = {}
                    for t in long_option_trades:
                        contract_key = (
                            str(t["symbol"]).upper(),
                            t.get("expiry"),
                            t.get("strike"),
                            t.get("opt_type"),
                        )
                        unique_contracts.setdefault(contract_key, t)
                    for t in short_call_trades:
                        contract_key = (
                            str(t["symbol"]).upper(),
                            t.get("expiry"),
                            t.get("strike"),
                            t.get("opt_type"),
                        )
                        unique_contracts.setdefault(contract_key, t)

                    from market_analysis.portfolio import get_option_chain_mid_iv

                    async def _fetch_one_contract_quote(
                        key: tuple[str, Any, Any, Any],
                    ) -> tuple[tuple[str, Any, Any, Any], float, float, float]:
                        sym_k, expiry_k, strike_k, opt_type_k = key
                        async with quote_sem:
                            try:
                                mid, _iv, bid, ask = await get_option_chain_mid_iv(
                                    sym_k, expiry_k, strike_k, opt_type_k
                                )
                                return key, mid, bid, ask
                            except Exception as ex:
                                logger.warning(
                                    f"[OptionsRollover] 抓取 {sym_k} {expiry_k} "
                                    f"{strike_k}{opt_type_k} 報價失敗: {ex}"
                                )
                                return key, 0.0, 0.0, 0.0

                    quote_results = await asyncio.gather(
                        *[_fetch_one_contract_quote(key) for key in unique_contracts]
                    )
                    quote_map = {
                        key: (mid, bid, ask) for key, mid, bid, ask in quote_results
                    }

                    for t in long_option_trades:
                        opt_u_id = t["user_id"]
                        opt_sym = str(t["symbol"]).upper()
                        contract_key = (
                            opt_sym,
                            t.get("expiry"),
                            t.get("strike"),
                            t.get("opt_type"),
                        )
                        mid_price, bid, ask = quote_map.get(
                            contract_key, (0.0, 0.0, 0.0)
                        )
                        if mid_price <= 0:
                            logger.warning(
                                f"[OptionsRollover] {opt_sym} {t.get('expiry')} "
                                f"{t.get('strike')}{t.get('opt_type')} 無有效報價 "
                                "(合約可能已下市/到期)，跳過本輪評估。"
                            )
                            continue

                        opt_metrics = symbol_metrics_map.get(opt_sym)
                        if opt_metrics is None:
                            continue
                        opt_quantity = float(t.get("quantity") or 0.0)
                        opt_r_data = radar_cache_map.get(opt_sym)

                        option_asset_entry = self._build_option_asset_entry(
                            opt_sym,
                            opt_quantity,
                            mid_price,
                            bid,
                            ask,
                            opt_metrics,
                            opt_r_data,
                        )
                        user_assets.setdefault(opt_u_id, []).append(option_asset_entry)

                    for t in short_call_trades:
                        sc_u_id = t["user_id"]
                        sc_sym = str(t["symbol"]).upper()
                        contract_key = (
                            sc_sym,
                            t.get("expiry"),
                            t.get("strike"),
                            t.get("opt_type"),
                        )
                        mid_price, _bid, _ask = quote_map.get(
                            contract_key, (0.0, 0.0, 0.0)
                        )
                        # 報價缺失 (mid_price<=0) 時仍併入清單——
                        # evaluate_covered_call_profit_lock 對 DTE<=1 的
                        # 結算保護分支不需要報價，只有一般衰減判定分支才會
                        # fail-safe 跳過缺報價的部位。
                        user_short_calls.setdefault(sc_u_id, []).append(
                            {
                                "symbol": sc_sym,
                                "expiry": t.get("expiry"),
                                "strike": t.get("strike"),
                                "quantity": t.get("quantity"),
                                "entry_price": t.get("entry_price"),
                                "current_premium": mid_price,
                            }
                        )

                # 聯集 user_assets 與 user_short_calls 的使用者集合：Covered
                # Call 賣方通常會同時持有對應現貨 (已在 user_assets 中)，但為
                # 避免僅持有空頭 CALL、無對應現貨紀錄的邊界情況被靜默忽略，
                # 仍以聯集為準，portfolio_assets 缺席時安全退回空列表。
                all_user_ids = set(user_assets.keys()) | set(user_short_calls.keys())
                for u_id in all_user_ids:
                    portfolio_assets = user_assets.get(u_id, [])
                    total_val = sum(a["current_value"] for a in portfolio_assets)

                    rebalance_instructions = (
                        await self.rollover_engine.check_satellite_rebalancing(
                            u_id, portfolio_assets, total_val
                        )
                    )

                    # 🚀 邏輯 (2): 機會成本轉倉 — 對尚未被 Scenario 3 標記的
                    # SATELLITE 持倉，比對單一預篩選高 EV 候選標的的機會成本
                    # 注意：僅排除 Scenario 3 有實際賣出/減碼動作的標的；HOLD
                    # (安心防守卡，無實際動作) 不構成矛盾指令，不應阻擋本情境評估。
                    # 去重鍵採 (symbol, instrument_type) 複合鍵：同一標的可能
                    # 同時存在現貨與期權部位，純 symbol 去重會讓其中一種工具
                    # 類型的清倉指令意外壓制另一種工具類型的獨立評估。
                    already_flagged = {
                        (ins["symbol"], ins.get("instrument_type", "SPOT"))
                        for ins in rebalance_instructions
                        if ins.get("action") != "HOLD"
                    }
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

                    (
                        opportunity_cost_instructions,
                        candidate_entry_confirmation,
                    ) = await self.rollover_engine.evaluate_opportunity_cost_for_satellites(
                        u_id,
                        portfolio_assets,
                        already_flagged,
                        candidate_symbol,
                        candidate_radar,
                    )
                    rebalance_instructions += opportunity_cost_instructions

                    # 🚀 邏輯 (5): 核心資金部署 — 對超過使用者明確設定
                    # target_allocation_pct 的 CORE 持倉，將超額部位部署至
                    # 邏輯 (2) 已找到並確認突破的候選標的，重用同一份
                    # candidate_symbol / candidate_radar，不重複掃描 watchlist；
                    # 同時沿用邏輯 (2) 已算好的 _confirm_entry_signal 六重鐵律
                    # 確認結果 (candidate_entry_confirmation)，避免對同一候選
                    # 標的在同一輪次內重複驗證 (內含未快取的 get_market_regime()
                    # 呼叫)。
                    already_flagged = {
                        (ins["symbol"], ins.get("instrument_type", "SPOT"))
                        for ins in rebalance_instructions
                        if ins.get("action") != "HOLD"
                    }
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_core_deployment(
                            u_id,
                            portfolio_assets,
                            already_flagged,
                            total_val,
                            candidate_symbol,
                            candidate_radar,
                            precomputed_entry_confirmation=candidate_entry_confirmation,
                        )
                    )

                    # 🚀 邏輯 (5) 延伸: Covered Call Overlay — 與上方超額配置部署
                    # 分支互相獨立，不要求 target_allocation_pct opt-in，只要求
                    # CORE 持倉股數達 1 口門檻。輸出恆為 action="HOLD"（不賣出
                    # 任何標的持股），故不需要、也不應該把它的輸出併入
                    # already_flagged_symbols 用於排除後續分支 —— 沿用當前的
                    # already_flagged 集合即可（其僅用於避免重複評估已被標記
                    # 賣出/減碼的標的）。
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_covered_call_overlay(
                            u_id,
                            portfolio_assets,
                            already_flagged,
                        )
                    )

                    # 🚀 邏輯 (4): 槓桿與保證金防禦 — 排除已被 Scenario 2/3/5 標記過的
                    # 標的，避免同一標的同一輪次收到互相矛盾的清倉指令。
                    # 同樣僅排除有實際賣出/減碼動作者；Scenario 3 的 HOLD 安心防守卡
                    # 不應在大盤觸發系統性保證金風控紅線時，silently 蓋掉更高等級的
                    # 強制平倉防禦警報。
                    already_flagged = {
                        (ins["symbol"], ins.get("instrument_type", "SPOT"))
                        for ins in rebalance_instructions
                        if ins.get("action") != "HOLD"
                    }
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_margin_defense(
                            u_id,
                            portfolio_assets,
                            already_flagged_symbols=already_flagged,
                        )
                    )

                    # 🚀 邏輯 (6): 宏觀逃頂前瞻防禦 — 排在六大情境的最後一位
                    # (3→2→5→4→6)。本情境是信心度最低、最具推測性的觸發
                    # (機率性組合評分 vs. 其餘情境已確認的價格/保證金破位)，
                    # 必須確保不會搶在更確定的訊號之前對同一標的下指令；沿用
                    # 累積的 already_flagged 集合，保證 Scenario 2-5 對任一
                    # 標的永遠享有優先權。嚴格 opt-in
                    # (user_settings.enable_macro_top_escape_defense)，未開啟
                    # 的使用者這裡恆為 no-op。
                    already_flagged = {
                        (ins["symbol"], ins.get("instrument_type", "SPOT"))
                        for ins in rebalance_instructions
                        if ins.get("action") != "HOLD"
                    }
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_macro_top_escape_defense(
                            u_id,
                            portfolio_assets,
                            already_flagged_symbols=already_flagged,
                        )
                    )

                    # 🚀 Covered Call 權利金衰減停利 — 與 Scenario 2/3/4/5/6 完全
                    # 獨立，只處理既有空頭 CALL 部位是否該提前 BTC 回補了結，
                    # 不涉及任何轉倉/開倉決策，因此不參與 already_flagged_symbols
                    # 排除邏輯，也不影響/受影響於上述任一情境。
                    rebalance_instructions += (
                        await self.rollover_engine.evaluate_covered_call_profit_lock(
                            u_id,
                            user_short_calls.get(u_id, []),
                        )
                    )

                    # 情境識別碼 → 人類可讀標籤，僅供標題補充說明；顏色/危險等級判斷
                    # 由 create_dynamic_rollover_embed 依 scenario 明確對照表決定，
                    # 不再依賴此處字串是否包含特定關鍵字。
                    _SCENARIO_LABELS = {
                        "OPPORTUNITY_COST": "機會成本轉倉",
                        "SATELLITE_REBALANCE": "核心衛星再平衡",
                        "MARGIN_DEFENSE": "槓桿與保證金防禦",
                        "CORE_DEPLOYMENT": "核心資金部署",
                        "MACRO_TOP_ESCAPE_DEFENSE": "宏觀逃頂前瞻防禦",
                        "COVERED_CALL_PROFIT_LOCK": "Covered Call 權利金衰減停利",
                    }

                    today_str = datetime.now(ny_tz).strftime("%Y%m%d")
                    for ins in rebalance_instructions:
                        scenario = ins.get("scenario", "UNKNOWN")
                        # 保證金強制平倉警報 (MARGIN_DEFENSE) 為帳戶生存等級警訊，
                        # 獨立於例行轉倉建議 (Scenario 2/3) 的開關之外，避免使用者
                        # 在 `mute_intraday` 等預設情境下靜音例行雜訊時，連帶誤將
                        # 系統性保證金風控紅線警報一併關閉。
                        notif_key = (
                            "defense_margin_call"
                            if scenario == "MARGIN_DEFENSE"
                            else "defense_option_rollover"
                        )
                        if not database.is_notification_enabled(u_id, notif_key):
                            continue

                        action = ins.get("action", "UNKNOWN")
                        # 冷卻去重：每位使用者、每個標的、每個情境、每種動作，
                        # 每日最多發送一則警報，避免同一觸發條件在盤中每 30 分鐘
                        # 反覆重推導致警報疲勞（比照 WTI / 價量突破警報既有模式）。
                        # 動作 (action) 亦納入 key：若條件當日從 HOLD 升級為
                        # LIQUIDATE 等實際動作，仍視為新警報正常發送。
                        instrument_type = ins.get("instrument_type", "SPOT")
                        dedup_key = (
                            f"rollover_alert_{u_id}_{ins['symbol']}_"
                            f"{instrument_type}_{scenario}_{action}_{today_str}"
                        )
                        if scenario == "COVERED_CALL_PROFIT_LOCK":
                            # 同一標的可能同時存在多筆不同履約價/到期日的 Covered
                            # Call，通用 dedup_key 僅以 (symbol, action) 區分會讓
                            # 其中一筆的警報意外壓制另一筆；額外納入 strike/expiry
                            # 與衰減門檻分級 (action 已隱含 LIQUIDATE=全額/
                            # REDUCE=局部)，允許當日從局部門檻推進至全額門檻時
                            # 仍能重新提醒一次。
                            dedup_key += f"_{ins.get('strike')}_{ins.get('expiry')}"
                        if database.get_kv_cache(dedup_key):
                            continue

                        scenario_label = _SCENARIO_LABELS.get(scenario, "動態轉倉")
                        # 若為 HOLD 狀態且無減倉動作，通常不主動洗版，但若有指示則發送安心防守卡
                        rollover_type = (
                            f"持倉防守 ({scenario_label})"
                            if ins["action"] == "HOLD"
                            else scenario_label
                        )
                        # 優先採用各情境引擎實際計算出的目標資產參考限價
                        # (取代過去恆為 "Market" 的佔位字串)；僅在引擎未提供
                        # 有效數值時（例如 Scenario 2/4 尚未接上定價邏輯）才
                        # 退回 "Market" 泛用字串。
                        limit_price_val = ins.get("limit_price")
                        if ins["action"] == "HOLD":
                            suggested_price = "N/A (維持現狀)"
                        elif limit_price_val:
                            suggested_price = f"${float(limit_price_val):.2f} (限價)"
                        else:
                            suggested_price = "Market"

                        if ins.get("is_covered_call_overlay"):
                            # Covered Call Overlay：不賣出任何標的持股、沒有第二個
                            # 轉倉標的，套用 create_dynamic_rollover_embed 的
                            # sell/buy 轉倉框架會產生誤導文案 (該函式的 is_hold
                            # 判斷只要 sell_ratio==0 就恆為真，會顯示「安全續抱、
                            # 無需任何手動操作」——但這裡恰恰需要使用者主動掛單
                            # 賣出買權)，改用專屬 embed。既有兩個互動 View
                            # (RolloverActionView 試算買入股數、ManualOverrideView)
                            # 語意皆不適用於「賣出備兌買權」，故不附加互動按鈕。
                            embed = create_covered_call_overlay_embed(
                                symbol=ins["symbol"],
                                reason=ins["reason"],
                                strike=ins.get("strike") or "N/A",
                                expiry=ins.get("expiry") or "N/A",
                                cash_impact=ins.get("cash_impact"),
                                trigger_condition_text=ins.get(
                                    "trigger_condition_text"
                                ),
                                is_manual_override_required=bool(
                                    ins.get("is_manual_override_required")
                                ),
                            )
                        elif ins.get("is_covered_call_profit_lock"):
                            # Covered Call 權利金衰減停利：純 BTC 平倉了結，沒有
                            # 第二個轉倉標的，理由同上不套用通用轉倉框架。
                            embed = create_covered_call_profit_lock_embed(
                                symbol=ins["symbol"],
                                reason=ins["reason"],
                                entry_premium=float(ins.get("entry_premium") or 0.0),
                                current_premium=float(
                                    ins.get("current_premium") or 0.0
                                ),
                                decay_pct=float(ins.get("decay_pct") or 0.0),
                                btc_ratio=ins["sell_ratio"],
                                dte=int(ins.get("dte") or 0),
                                strike=ins.get("strike") or "N/A",
                                expiry=ins.get("expiry") or "N/A",
                                cash_impact=ins.get("cash_impact"),
                            )
                        else:
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
                                strike=ins.get("strike") or "N/A",
                                expiry=ins.get("expiry") or "N/A",
                                direction=ins.get("direction")
                                or ("BTO" if ins["action"] != "HOLD" else "HOLD"),
                                sell_action=ins.get("sell_action", "STC"),
                                buy_action_label=ins.get("buy_action_label"),
                                scenario=scenario,
                                cash_impact=ins.get("cash_impact"),
                                trigger_condition_text=ins.get(
                                    "trigger_condition_text"
                                ),
                                extreme_stop_loss=ins.get("extreme_stop_loss"),
                                is_extreme_tick_breach=bool(
                                    ins.get("is_extreme_tick_breach")
                                ),
                                extreme_breach_detail_block=ins.get(
                                    "extreme_breach_detail_block"
                                ),
                            )
                            if ins.get("is_manual_override_required"):
                                setattr(
                                    embed,
                                    "_view",
                                    f"ManualOverrideView:{ins['symbol']}",
                                )
                            else:
                                setattr(
                                    embed,
                                    "_view",
                                    f"RolloverActionView:{ins['symbol']}",
                                )
                        # 期權相關轉倉指令是「首次真正被生產環境觸發」的分支，
                        # 行為分佈尚未經過實際流量驗證；dry-run 期間僅記錄
                        # log 與審計軌跡，不實際推播 DM，待觀察 1-2 週分佈合理
                        # 後再關閉 OPTIONS_ROLLOVER_DRY_RUN。
                        if (
                            instrument_type == "OPTIONS"
                            and config.OPTIONS_ROLLOVER_DRY_RUN
                        ):
                            logger.info(
                                f"[OptionsRollover][DryRun] 略過推播 (僅記錄審計軌跡): "
                                f"user={u_id} symbol={ins['symbol']} scenario={scenario} "
                                f"action={action}"
                            )
                        else:
                            await self.bot.queue_dm(u_id, embed=embed)
                        await database.save_kv_cache(dedup_key, 1)
                        # 審計軌跡：記錄本次實際推送給使用者的轉倉建議本身
                        # (系統僅提供建議、不代為執行券商下單，故無法追蹤實際
                        # 成交結果，此處記錄的是「推送了什麼建議」而非「後續
                        # 執行結果」)，供事後回顧與問責。
                        await database.log_rollover_instruction(
                            user_id=u_id,
                            symbol=ins["symbol"],
                            scenario=scenario,
                            action=ins["action"],
                            sell_ratio=ins["sell_ratio"],
                            target_core=ins.get("target_core"),
                            suggested_price=suggested_price,
                            cash_impact=ins.get("cash_impact"),
                        )
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
