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
    for uid, sym, _ in all_watchlists:
        user_symbols.setdefault(uid, [])
        if sym not in user_symbols[uid]:
            user_symbols[uid].append(sym)

    terminal_cog = bot.get_cog("UnifiedTerminalCog")
    if not terminal_cog:
        logger.error(
            "UnifiedTerminalCog not found, cannot dispatch watchlist heartbeat."
        )
        return

    for uid, symbols in user_symbols.items():
        try:
            notif_settings = database.get_user_notification_settings(uid)
            hb_keys = [
                "hb_options_structure",
                "hb_execution_risk",
            ]
            hb_enabled = any(notif_settings.get(k, True) for k in hb_keys)

            if not hb_enabled:
                logger.info(f"使用者 {uid} 已關閉所有心跳模組訂閱，略過心跳推送。")
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

            import random

            scan_results = []
            for idx, s in enumerate(deliverable_symbols):
                if idx > 0:
                    await asyncio.sleep(random.uniform(1.5, 2.0))
                try:
                    res = await terminal_cog._fetch_sym_radar_data_slow(s)
                    scan_results.append(res)
                except Exception as ex:
                    logger.error(f"Error fetching radar data for {s}: {ex}")
                    scan_results.append(ex)

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
                    )
                    if scenario:
                        # 二階段確認：PENDING 狀態需經 15 分鐘實體 K 線確認
                        if scenario == MarketScenario.STRUCTURAL_BREAKDOWN_PENDING:
                            from market_analysis.gamma_cliff_confirmation import (
                                is_gamma_cliff_confirmed,
                            )

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
