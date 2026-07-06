"""Intraday execution guide dispatch logic for the Analyst Agent."""

from __future__ import annotations

import logging
from datetime import datetime

import psutil

import database
from config import get_vix_tier
from market_time import ny_tz
from market_analysis.sentiment_engine import SentimentEngine
from cogs.embed_builder import create_intraday_execution_guide_embed

logger = logging.getLogger(__name__)


async def run_intraday_guide(bot, fetch_macro_fn=None) -> None:
    """Build and queue per-user intraday execution guide DMs.

    Args:
        bot: The Discord bot instance.
        fetch_macro_fn: Optional async callable returning a macro data dict.
                        Defaults to ``macro_runner.fetch_macro_data``.
    """
    if fetch_macro_fn is None:
        from market_analysis.analyst_runners.macro_runner import fetch_macro_data

        fetch_macro_fn = fetch_macro_data

    # 0. 記憶體安全閘
    mem = psutil.virtual_memory()
    is_memory_gated = mem.percent > 85.0
    if is_memory_gated:
        logger.warning(
            "🚨 [Memory Gate] RAM usage > 85%, deferring heavy intraday analysis."
        )

    # 1. 宏觀資料與 VIX 階梯
    macro_data = await fetch_macro_fn()
    vix = macro_data.get("vix", 18.0)
    vix_tier = get_vix_tier(vix)
    vix_level_name = vix_tier.get("name", "Unknown")

    # 判斷盤中階段
    now_ny = datetime.now(ny_tz)
    hour = now_ny.hour
    if hour < 11:
        phase = "A"
        phase_name = "流動性與開盤波動 (Phase A)"
    elif 11 <= hour < 14:
        phase = "B"
        phase_name = "深度研究與板塊輪動 (Phase B)"
    else:
        phase = "C"
        phase_name = "投資組合對沖與收尾 (Phase C)"

    # Polymarket 巨鯨意向
    poly_intent = "無顯著巨鯨活動"
    if not is_memory_gated and hasattr(bot, "polymarket_service"):
        try:
            markets = bot.polymarket_service.get_active_markets(limit=3)
            if markets:
                poly_intent = f"焦點: {markets[0].get('question', '')[:30]}..."
        except Exception:
            pass

    # SPY 偏態
    skew_data = {"skew": 0.0, "state": "N/A"}
    if not is_memory_gated:
        try:
            skew_data = await SentimentEngine.calculate_skew("SPY")
        except Exception:
            pass

    user_ids = database.get_all_user_ids()
    dispatched_count = 0

    for uid in user_ids:
        if not database.is_notification_enabled(uid, "intraday_decision_scan"):
            continue
        ctx = database.get_full_user_context(uid)

        if is_memory_gated:
            embed = create_intraday_execution_guide_embed(
                phase_name=phase_name,
                vix=vix,
                memory_percent=mem.percent,
                is_memory_gated=True,
            )
            await bot.queue_dm(uid, embed=embed)
            dispatched_count += 1
            continue

        # 2. 組合希臘值與 Vanna
        from market_analysis.portfolio import refresh_portfolio_greeks
        from services.asset_manager import AssetManager
        from models.asset import ContextType, TradeMetadata, HoldingMetadata
        from market_analysis.risk_engine import calculate_vega_adjusted_delta

        await refresh_portfolio_greeks(uid)
        manager = AssetManager()
        trade_assets = manager.get_assets(uid, ContextType.TRADE)
        holding_assets = manager.get_assets(uid, ContextType.HOLDING)

        total_delta = 0.0
        total_vanna = 0.0
        for a in trade_assets:
            t_meta = TradeMetadata(**a.metadata)
            total_delta += t_meta.weighted_delta
            total_vanna += t_meta.vanna
        for a in holding_assets:
            h_meta = HoldingMetadata(**a.metadata)
            total_delta += h_meta.weighted_delta

        adj_delta = calculate_vega_adjusted_delta(total_delta, total_vanna, 0.10)
        greeks_status = f"Δ: `{total_delta:.2f}` | 隱含 Δ (Vanna): `{adj_delta:.2f}`"

        # 3. 財務健康
        from market_analysis.pro_management import calculate_financial_runway

        runway_days = calculate_financial_runway(
            ctx.cash_reserve, ctx.monthly_expense, ctx.total_theta
        )
        theta_cov = (
            (ctx.total_theta * 30 / ctx.monthly_expense * 100)
            if ctx.monthly_expense > 0
            else 0.0
        )

        # 4. 主動訊號
        hedge_suggest = "無需緊急對沖 (Hold)"
        if abs(adj_delta) > (ctx.capital / 1000) * 0.1:
            action = "BUY" if adj_delta < 0 else "SELL"
            # SPY 每股 Delta 為 1.0
            qty = max(1, int(round(abs(adj_delta))))
            hedge_suggest = (
                f"建議 {action} {qty} 單位 SPY 對沖 Delta 偏離 (`/settle_hedge`)\n"
                f"> (稽核詳情: adj_delta={adj_delta:.2f}, capital=${ctx.capital:,.0f})"
            )
        elif float(str(vix_tier.get("multiplier", 1.0))) < 0.5:
            hedge_suggest = "VIX 過高，建議啟動尾部風險防禦"

        if phase == "A":
            active_signal_content = f"**早盤流動性:** 觀察 VIX 變化與日內開盤跳空缺口。\n**對沖建議:** {hedge_suggest}"
        elif phase == "B":
            active_signal_content = f"**情緒/巨鯨:** Skew `{skew_data.get('skew', 0.0)}`, {poly_intent}\n**板塊輪動:** 關注科技與金融板塊資金流向。"
        else:
            active_signal_content = f"**尾盤收斂:** 檢視 Vanna-Adjusted Delta 是否過高。\n**強制對沖建議:** {hedge_suggest}"

        # 5. 系統快取健康
        import services.market_data_service as mds

        embed = create_intraday_execution_guide_embed(
            phase_name=phase_name,
            vix=vix,
            memory_percent=mem.percent,
            is_memory_gated=False,
            vix_level_name=vix_level_name,
            greeks_status=greeks_status,
            runway_days=runway_days,
            theta_cov=theta_cov,
            active_signal_content=active_signal_content,
            sma_cache_size=len(mds._sma_cache),
            ema_cache_size=len(mds._ema_cache),
        )
        await bot.queue_dm(uid, embed=embed)
        dispatched_count += 1

    logger.info(f"Dispatched Intra-day Execution Guide to {dispatched_count} users.")
