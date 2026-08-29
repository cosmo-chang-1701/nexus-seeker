"""量化掃描、部位轉換模擬與 VTR（虛擬交易室）績效查詢指令邏輯。"""

from typing import Any
import asyncio
import logging

import discord

import database
import market_math
from services import market_data_service
from cogs.embed_builder import (
    create_error_embed,
    create_info_embed,
    create_scan_embed,
    create_transition_simulation_embed,
)

logger = logging.getLogger(__name__)


async def manual_scan_impl(interaction: discord.Interaction, symbol: str) -> Any:
    await interaction.response.defer(ephemeral=True)
    user_id, symbol = interaction.user.id, symbol.upper()

    # 🚀 驗證標的合法性
    if not await market_data_service.validate_symbol(symbol):
        return await interaction.followup.send(
            embed=create_error_embed(
                f"**無效的標的代號**: `{symbol}`。請輸入正確的美股代號。",
                title="系統錯誤",
            ),
            ephemeral=True,
        )

    # 🚀 獲取用戶現貨成本 (如果有)
    from services.asset_manager import AssetManager
    from models.asset import ContextType

    manager = AssetManager()
    assets = manager.get_assets(user_id, ContextType.HOLDING)
    stock_cost = next(
        (float(a.metadata.get("avg_cost", 0.0)) for a in assets if a.symbol == symbol),
        0.0,
    )

    try:
        spy_task = market_data_service.get_spy_history_df("1y")
        macro_task = market_data_service.get_macro_environment()
        df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
        spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
        from market_analysis.risk_engine import MacroContext

        macro_data = MacroContext(
            vix=macro_raw.get("vix", 18.0),
            oil_price=macro_raw.get("oil", 75.0),
            vix_change=macro_raw.get("vix_change", 0.0),
        )
    except Exception:
        df_spy, spy_price, macro_data = (
            None,
            670.0,
            MacroContext(vix=22.0, oil_price=85.0, vix_change=0.0),
        )

    result = await market_math.analyze_symbol(
        symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
    )
    is_option_valid = bool(result)
    if not result:
        result = {"symbol": symbol, "stock_cost": stock_cost}

    # 🚀 執行 Gap & Fill 跳空分析 (New)
    try:
        from market_analysis.gap_analysis import GapAnalyzer

        df_gap = await market_data_service.get_history_df(
            symbol, period="5d", interval="1d"
        )
        if not df_gap.empty and len(df_gap) >= 2:
            gap_metrics = GapAnalyzer.analyze_gap(df_gap)
            if gap_metrics:
                result["gap_status"] = gap_metrics
    except Exception as gap_e:
        logger.warning(f"手動掃描 Gap 分析失敗 for {symbol}: {gap_e}")

    df_hist_1d = await market_data_service.get_history_df(
        symbol, period="1y", interval="1d"
    )
    from market_analysis.psq_engine import analyze_psq
    from cogs.embed_builder import create_psq_embed

    psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
    if psq_result:
        result["psq_result"] = psq_result

    embeds_to_send = []
    if is_option_valid:
        from services import llm_service, news_service
        from market_analysis.risk_engine import optimize_position_risk
        from market_analysis.sentiment_engine import SentimentEngine

        # 使用快取 Reddit 資料
        from database.cache import get_kv_cache

        reddit_text = get_kv_cache(f"reddit_sentiment_{symbol}") or "暫無快取情緒資料。"
        news_text = await news_service.fetch_recent_news(symbol)

        # 並行獲取期權情緒指標
        skew_task = SentimentEngine.calculate_skew(symbol)
        pcr_task = SentimentEngine.calculate_pcr(symbol)
        uoa_task = SentimentEngine.detect_uoa(symbol)

        skew_data, pcr_data, uoa_list = await asyncio.gather(
            skew_task, pcr_task, uoa_task
        )
        pcr_val = pcr_data.get("pcr", 0.8)
        skew_val = skew_data.get("skew", 0.0)

        ai_verdict = await llm_service.evaluate_trade_risk(
            symbol, result.get("strategy", ""), news_text, reddit_text
        )
        result.update(
            {
                "news_text": news_text,
                "reddit_text": reddit_text,
                "ai_decision": ai_verdict.get("decision", "APPROVE"),
                "ai_reasoning": ai_verdict.get("reasoning", "無資料"),
                "vix": macro_data.vix,
                "oil": macro_data.oil_price,
                "pcr": pcr_val,
                "skew": skew_val,
                "uoa_list": uoa_list,
            }
        )

        user_context = database.get_full_user_context(user_id)
        opt_res = optimize_position_risk(
            current_delta=user_context.total_weighted_delta,
            unit_weighted_delta=result.get("weighted_delta", 0.0),
            user_capital=user_context.capital,
            spy_price=spy_price,
            stock_iv=result.get("iv", 0.15),
            strategy=result.get("strategy", ""),
            macro_data=macro_data,
            risk_limit=user_context.risk_limit,
            vix_spot=macro_data.vix,
            pcr=pcr_val,
            skew=skew_val,
        )
        safe_qty = opt_res.suggested_contracts
        hedge_spy = opt_res.suggested_hedge_spy
        projected_exposure_pct = opt_res.exposure_pct

        result.update(
            {
                "projected_exposure_pct": round(projected_exposure_pct, 2),
                "safe_qty": safe_qty,
                "hedge_spy": hedge_spy,
                "spy_price": spy_price,
                "risk_limit": user_context.risk_limit,
            }
        )
        embeds_to_send.append(create_scan_embed(result, user_context.capital))

    if psq_result:
        result["price"] = df_hist_1d["Close"].iloc[-1] if not df_hist_1d.empty else 0.0
        embeds_to_send.append(create_psq_embed(result))

    if embeds_to_send:
        await interaction.followup.send(embeds=embeds_to_send)
    else:
        await interaction.followup.send(
            embed=create_info_embed(
                title="系統資訊", message=f" 目前 `{symbol}` 查無有效訊號。"
            )
        )


async def transition_sim_impl(
    interaction: discord.Interaction,
    symbol: str,
    current_option_pnl: float,
    target_cc_strike: float,
    target_cc_premium: float,
) -> Any:
    await interaction.response.defer(ephemeral=True)
    symbol = symbol.upper()

    try:
        quote = await market_data_service.get_quote(symbol)
        current_price = quote.get("c", 0.0) if quote else 0.0

        if current_price <= 0:
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"無法獲取 `{symbol}` 即時報價。", title="系統錯誤"
                ),
                ephemeral=True,
            )

        from market_analysis.pro_management import simulate_pro_transition

        res = simulate_pro_transition(
            current_option_pnl=current_option_pnl,
            current_stock_price=current_price,
            target_cc_strike=target_cc_strike,
            target_cc_premium=target_cc_premium,
        )

        embed = create_transition_simulation_embed(
            symbol=symbol,
            current_price=current_price,
            initial_pnl=res.initial_pnl,
            additional_capital_required=res.additional_capital_required,
            adjusted_cost_basis=res.adjusted_cost_basis,
            target_cc_strike=target_cc_strike,
            target_cc_premium=target_cc_premium,
            projected_aroc=res.projected_aroc,
            capital_efficiency_gain=res.capital_efficiency_gain,
        )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Transition Simulation failed: {e}")
        await interaction.followup.send(
            embed=create_error_embed(
                "模擬執行失敗，請檢查輸入數據。", title="系統錯誤"
            ),
            ephemeral=True,
        )


async def vtr_stats_impl(interaction: discord.Interaction) -> Any:
    await interaction.response.defer(ephemeral=True)
    try:
        from market_analysis.ghost_trader import GhostTrader
        from market_analysis.attribution import AttributionEngine

        # 0. 結算目前的對沖日誌 (歸因分析)
        await AttributionEngine.finalize_vtr_attribution(interaction.user.id)

        # 1. 獲取基礎統計
        stats = await GhostTrader.get_vtr_performance_stats(interaction.user.id)

        # 2. 獲取對沖歸因報告
        attr_lines = AttributionEngine.format_attribution_report(interaction.user.id)

        # 3. 建立 Embed
        from cogs.embed_builder import build_vtr_stats_embed

        embed = build_vtr_stats_embed(interaction.user.display_name, stats, attr_lines)

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"VTR Stats failed: {e}")
        await interaction.followup.send(
            embed=create_error_embed(f"無法獲取績效數據: {e}", title="操作失敗"),
            ephemeral=True,
        )


async def vtr_list_impl(interaction: discord.Interaction) -> Any:
    await interaction.response.defer(ephemeral=True)
    from database.virtual_trading import get_all_virtual_trades

    rows = get_all_virtual_trades(interaction.user.id)
    if not rows:
        return await interaction.followup.send(
            embed=create_info_embed(
                title="查無資料", message="📭 虛擬交易室目前無任何紀錄。"
            ),
            ephemeral=True,
        )

    msg = "👻 **【虛擬交易室 (VTR) 紀錄清單】**\n"
    for row in rows[:20]:  # 限制顯示最近 20 筆
        status_emoji = "🟢" if row["status"] == "OPEN" else "⚪"
        pnl_str = f" | PnL: `{row['pnl']:+.2f}`" if row["status"] != "OPEN" else ""
        msg += f"{status_emoji} `ID:{row['id']:02d}` | **{row['symbol']}** | ${row['strike']} {row['opt_type'].upper()} | {row['status']}{pnl_str}\n"

    if len(rows) > 20:
        msg += f"\n*(僅顯示最近 20 筆，總計 {len(rows)} 筆)*"

    await interaction.followup.send(
        embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
    )
