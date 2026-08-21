"""
cogs/trading/heartbeat.py

Watchlist 30 分鐘心跳推送邏輯 (_dispatch_watchlist_heartbeat)。
此模組提供一個獨立函式，由 SchedulerCog 呼叫。
"""

from typing import Any
import asyncio
import logging
from datetime import datetime

import database
import market_time

logger = logging.getLogger(__name__)


async def dispatch_watchlist_heartbeat(
    bot: Any,
    all_watchlists: list[tuple[int, str, int]] | None = None,
) -> None:
    """每個 30 分鐘節點推送 watchlist 批次掃描量化雷達。"""
    from cogs.embed_builder import build_radar_scan_embed

    if all_watchlists is None:
        all_watchlists = database.get_all_watchlist()
    if not all_watchlists:
        return

    user_symbols: dict[int, list[str]] = {}
    all_symbols: set[str] = set()
    for uid, sym, _ in all_watchlists:
        user_symbols.setdefault(uid, [])
        if sym not in user_symbols[uid]:
            user_symbols[uid].append(sym)
        all_symbols.add(sym)

    # Best-effort 同步全體去重後的自選標的清單給 nexus_edge_scraper，
    # 讓 edge 的背景排程知道該輪詢哪些標的的 GEX / Option Chain。
    # edge 目前部署不穩定，失敗只記錄 warning，不影響心跳繼續執行。
    try:
        from services import edge_cache_client

        await edge_cache_client.sync_watchlist_symbols(list(all_symbols))
    except Exception as sync_err:
        logger.warning(
            f"同步 watchlist 標的清單至 edge 失敗（不影響心跳繼續執行): {sync_err}"
        )

    terminal_cog = bot.get_cog("UnifiedTerminalCog")
    if not terminal_cog:
        logger.error(
            "UnifiedTerminalCog not found, cannot dispatch watchlist heartbeat."
        )
        return

    # --- Pass 1: 依每位使用者的通知開關與 option_alert_mode 篩選出實際要推播的標的 ---
    user_deliverable: dict[int, list[str]] = {}
    symbols_to_fetch: set[str] = set()
    for uid, symbols in user_symbols.items():
        try:
            if not database.is_notification_enabled(uid, "heartbeat_watchlist"):
                logger.info(f"使用者 {uid} 已關閉自選心跳訂閱，略過心跳推送。")
                continue

            user_context = database.get_full_user_context(uid)
            option_alert_mode = int(getattr(user_context, "option_alert_mode", 1))

            deliverable_symbols = []
            for sym in symbols:
                has_position = database.is_symbol_in_portfolio(uid, sym)
                if option_alert_mode == 2 and not has_position:
                    continue
                deliverable_symbols.append(sym)

            if not deliverable_symbols:
                continue

            user_deliverable[uid] = deliverable_symbols
            symbols_to_fetch.update(deliverable_symbols)
        except Exception as user_err:
            logger.error(
                f"❌ 用戶 {uid} 心跳前置篩選失敗: {user_err}",
                exc_info=True,
            )
            continue

    # --- Pass 2: 每個「去重後」的標的只實際打一次雷達數據，多位使用者共用同一標的的
    # 抓取結果不重複打 Finnhub/yfinance/Edge API，避免拖長整輪心跳佔用全域限流額度、
    # 進而卡住盤中互動指令（如 /x symbol:NVDA）。---
    import random

    radar_data_cache: dict[str, Any] = {}
    for idx, s in enumerate(sorted(symbols_to_fetch)):
        if idx > 0:
            await asyncio.sleep(random.uniform(1.5, 2.0))
        try:
            radar_data_cache[s] = await terminal_cog._fetch_sym_radar_data_slow(s)
        except Exception as ex:
            logger.error(f"Error fetching radar data for {s}: {ex}")
            radar_data_cache[s] = ex

    # --- Pass 3: 依每位使用者各自的標的清單，從共用快取組裝並推播個人化 embed ---
    for uid, deliverable_symbols in user_deliverable.items():
        try:
            scan_results = [radar_data_cache.get(s) for s in deliverable_symbols]
            valid_results = [r for r in scan_results if isinstance(r, dict)]

            if valid_results:
                embeds = build_radar_scan_embed(valid_results, "WATCHLIST", uid)
                if not isinstance(embeds, list):
                    embeds = [embeds]
                for embed in embeds:
                    await bot.queue_dm(uid, embed=embed)

                # --- Scenario Alert Logic ---
                from market_analysis.scenario_classifier import (
                    classify_market_scenario,
                    MarketScenario,
                )
                from cogs.embed_builders.alert_embeds import create_scenario_alert_embed

                today_str = datetime.now(market_time.ny_tz).strftime("%Y-%m-%d")

                for res in valid_results:
                    symbol = res.get("symbol")
                    if not symbol:
                        continue

                    iv_rank = res.get("iv_metrics", {}).get("iv_rank", 0.0)
                    vp_data = res.get("vp_data", {})
                    hvn = vp_data.get("hvn", 0.0)
                    lvn = vp_data.get("lvn", 0.0)

                    gex_data = res.get("gex_profile_data") or {}
                    call_wall = gex_data.get("call_wall", 0.0)
                    put_wall = gex_data.get("put_wall", 0.0)
                    gamma_flip = gex_data.get("gamma_flip", 0.0)

                    psq_data = res.get("psq_result") or {}
                    is_squeezing = psq_data.get("is_squeezing", False)

                    quote_data = res.get("quote") or {}
                    price = quote_data.get("c", 0.0)
                    high = quote_data.get("h", 0.0)
                    low = quote_data.get("l", 0.0)

                    vol_data = res.get("vol_data") or {}
                    current_volume = vol_data.get("current_volume", 0.0)
                    avg_volume_20 = vol_data.get("avg_volume_20", 0.0)

                    skew_percentile = float(res.get("skew_percentile", 50.0))
                    uoa_data = res.get("uoa", [])
                    is_uoa_aligned = False
                    for uoa_item in uoa_data:
                        if isinstance(uoa_item, dict):
                            action_str = uoa_item.get("action", "")
                            opt_type = str(uoa_item.get("type", "")).upper()
                            if ("BTO" in action_str and opt_type == "CALL") or (
                                "STO" in action_str and opt_type == "PUT"
                            ):
                                is_uoa_aligned = True
                                break

                    scenario = classify_market_scenario(
                        price=price,
                        high=high,
                        low=low,
                        current_volume=current_volume,
                        avg_volume_20=avg_volume_20,
                        put_wall=put_wall,
                        call_wall=call_wall,
                        gamma_flip=gamma_flip,
                        is_squeezing=is_squeezing,
                        uoa_skew=res.get("skew", 0.0),
                        ivr=iv_rank,
                        hvn=hvn,
                        lvn=lvn,
                        skew_percentile=skew_percentile,
                        is_uoa_aligned=is_uoa_aligned,
                        volume_pcr=res.get("volume_pcr"),
                        put_wall_gex=res.get("put_wall_gex"),
                    )
                    if scenario:
                        # 二階段確認：PENDING 狀態需經 15 分鐘實體 K 線確認
                        if scenario == MarketScenario.STRUCTURAL_BREAKDOWN_PENDING:
                            from market_analysis.gamma_cliff_confirmation import (
                                is_gamma_cliff_confirmed,
                            )

                            # 注意：此處刻意不含 ATR 緩衝，是自選股 watchlist 進出場
                            # 信號的粗粒度變體，涵蓋未持有標的。持倉專用、含 ATR 緩衝
                            # + SQZ 動能疊加的更嚴謹版本見
                            # market_analysis/dynamic_rollover.py 的
                            # _compute_structural_breakdown_signals，兩者刻意不同、
                            # 不應合併（詳見 gamma_cliff_confirmation.is_below_gamma_defense_line docstring）。
                            gamma_cliff_level = min(
                                put_wall if put_wall > 0 else float("inf"),
                                gamma_flip if gamma_flip > 0 else float("inf"),
                            )
                            if gamma_cliff_level < float("inf"):
                                is_confirmed = await is_gamma_cliff_confirmed(
                                    symbol, gamma_cliff_level
                                )
                                if is_confirmed:
                                    scenario = MarketScenario.STRUCTURAL_BREAKDOWN
                                else:
                                    scenario = MarketScenario.FAKE_SUPPORT_TRAP
                            else:
                                scenario = MarketScenario.FAKE_SUPPORT_TRAP

                        cache_key = (
                            f"scenario_alert_{uid}_{symbol}_{today_str}_{scenario.name}"
                        )
                        if not database.get_kv_cache(cache_key):
                            alert_embed = create_scenario_alert_embed(
                                symbol=symbol,
                                scenario=scenario,
                                price=price,
                                put_wall=put_wall,
                                call_wall=call_wall,
                                gamma_flip=gamma_flip,
                                ivr=iv_rank,
                                hvn=hvn,
                                lvn=lvn,
                                skew_percentile=skew_percentile,
                            )
                            await bot.queue_dm(uid, embed=alert_embed)
                            await database.save_kv_cache(cache_key, True)
                # ----------------------------
        except Exception as user_err:
            logger.error(
                f"❌ 用戶 {uid} 心跳推送處理失敗: {user_err}",
                exc_info=True,
            )
            continue
