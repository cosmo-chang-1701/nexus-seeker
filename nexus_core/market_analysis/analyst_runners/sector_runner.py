"""Sector rotation, deep research, and market-open liquidity runners."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

import database
from config import get_vix_tier
import pandas as pd

from market_analysis.psq_engine import analyze_psq
from market_analysis.sentiment_engine import SentimentEngine
from services.market_data_service import (
    get_history_df,
    get_macro_environment,
    get_quote,
)
from services.llm_service import generate_analyst_report
from services.news_service import fetch_recent_news
from services.reddit_service import get_reddit_context

logger = logging.getLogger(__name__)

SECTORS = {
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
}

_GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def _get_tw_time_str() -> str:
    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return now_tw.strftime("[%H:%M UTC+8]")


async def _fetch_poly_events(bot) -> list[dict]:
    """Fetch up to 5 relevant Polymarket events."""
    events: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_GAMMA_API_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": 20},
            )
            if resp.status_code == 200:
                for m in resp.json():
                    if hasattr(bot, "polymarket_service"):
                        if not bot.polymarket_service._is_relevant_market(m):
                            continue
                    events.append(
                        {
                            "question": m.get("question"),
                            "outcome": m.get("outcomes"),
                            "price": m.get("outcomePrices"),
                        }
                    )
                    if len(events) >= 5:
                        break
    except Exception as e:
        logger.error(f"Error fetching Polymarket data: {e}")
    return events


async def gather_sector_rotation_data(bot) -> dict:
    """Collect sector performance, skew, UOA, Polymarket events, and SPY max pain."""
    macro = await get_macro_environment()
    vix = macro.get("vix", 18.0)
    vix_tier = get_vix_tier(vix)
    spy_quote = await get_quote("SPY")

    sector_results: list[dict] = []
    for symbol, name in SECTORS.items():
        try:
            df = await get_history_df(symbol, period="1mo")
            if df.empty:
                continue

            pct_change = (
                (df["Close"].iloc[-1] - df["Close"].iloc[-2])
                / df["Close"].iloc[-2]
                * 100
            )
            vol_current = df["Volume"].iloc[-1]
            vol_avg = df["Volume"].tail(20).mean()
            rel_vol = vol_current / vol_avg if vol_avg > 0 else 1.0

            try:
                skew_data = await SentimentEngine.calculate_skew(symbol)
            except Exception:
                skew_data = {"skew": 0, "state": "N/A"}

            try:
                uoa = await SentimentEngine.detect_uoa(symbol)
            except Exception:
                uoa = []

            sector_results.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "pct_change": round(pct_change, 2),
                    "rel_vol": round(rel_vol, 2),
                    "skew": skew_data.get("skew", 0),
                    "skew_state": skew_data.get("state", "N/A"),
                    "uoa_count": len(uoa),
                }
            )
        except Exception as e:
            logger.error(f"Error gathering data for {symbol}: {e}")

    poly_events = await _fetch_poly_events(bot)

    try:
        spy_max_pain = await SentimentEngine.calculate_max_pain("SPY")
    except Exception:
        spy_max_pain = {"error": "N/A"}

    return {
        "vix": vix,
        "vix_tier_name": vix_tier.get("name", "Unknown"),
        "spy_price": spy_quote.get("c", 0),
        "sectors": sector_results,
        "poly_events": poly_events,
        "spy_max_pain": spy_max_pain,
    }


async def run_sector_flow_report(bot):
    """Build and return a sector-flow report Embed using LLM analysis."""
    time_str = _get_tw_time_str()
    try:
        raw_data = await gather_sector_rotation_data(bot)
        report_type = f"{time_str} 收盤資金流向與板塊輪動報告"
        report = await generate_analyst_report(report_type, raw_data)

        from cogs.embed_builder import create_sector_flow_report_embed

        return create_sector_flow_report_embed(report_type, report, raw_data)
    except Exception as e:
        logger.error(f"run_sector_flow_report error: {e}")
        from cogs.embed_builder import create_ai_analysis_embed

        return create_ai_analysis_embed(
            f"**{time_str} 收盤資金流向報告**\n--------------------------------------------------\n系統分析發生錯誤: {e}",
            title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告",
        )


async def run_deep_research() -> str:
    """Sector-level deep research covering tech, semis, and financials."""
    time_str = _get_tw_time_str()
    try:
        sectors = {
            "Semiconductors": "SMH",
            "Technology": "XLK",
            "Financials": "XLF",
        }

        hist_tasks = [get_history_df(sym, period="3mo") for sym in sectors.values()]
        news_tasks = [fetch_recent_news(sym) for sym in sectors.values()]
        reddit_tasks = (
            [get_reddit_context(sym) for sym in sectors.values()]
            if database.any_user_local_tunnel_enabled()
            else []
        )

        hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
        news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
        reddit_results = (
            await asyncio.gather(*reddit_tasks, return_exceptions=True)
            if reddit_tasks
            else ["本地 Tunnel 已關閉，略過 Reddit 情緒。"] * len(sectors)
        )

        research_data: dict = {}
        for i, (name, sym) in enumerate(sectors.items()):
            df = hist_results[i]
            if isinstance(df, pd.DataFrame) and not df.empty:
                pct_change = (
                    (df["Close"].iloc[-1] - df["Close"].iloc[0])
                    / df["Close"].iloc[0]
                    * 100
                )
            else:
                pct_change = 0.0
            research_data[name] = {
                "symbol": sym,
                "quarterly_performance_pct": round(pct_change, 2),
                "news": news_results[i]
                if not isinstance(news_results[i], Exception)
                else "無法獲取",
                "reddit_sentiment": (
                    reddit_results[i]
                    if not isinstance(reddit_results[i], Exception)
                    else "無法獲取"
                ),
            }

        raw_data = {
            "sector_analysis": research_data,
            "capex_and_dso_status": "No cyclic oversupply detected based on price momentum proxy.",
        }
        report_type = f"{time_str} 深度研究與特定板塊分析"
        return await generate_analyst_report(report_type, raw_data)

    except Exception as e:
        logger.error(f"run_deep_research error: {e}")
        return (
            f"**{time_str} 深度研究與特定板塊分析**\n"
            "--------------------------------------------------\n"
            f"系統分析發生錯誤: {e}"
        )


async def run_market_open_liquidity() -> str:
    """Evaluate SPY/QQQ/IWM liquidity via PSQ score at market open."""
    time_str = _get_tw_time_str()
    try:
        symbols = ["SPY", "QQQ", "IWM"]
        liquidity_data: dict = {}
        for sym in symbols:
            df = await get_history_df(sym, period="1mo")
            if not df.empty:
                psq_result = analyze_psq(df, vix_spot=18.0)
                liquidity_data[sym] = {
                    "psq_score": psq_result.momentum_value if psq_result else 0.0,
                    "label": psq_result.squeeze_level if psq_result else "NEUTRAL",
                    "last_price": float(df["Close"].iloc[-1]),
                    "volume": int(df["Volume"].iloc[-1]),
                }

        raw_data = {
            "monitored_indices": liquidity_data,
            "liquidity_filter_active": True,
        }
        report_type = f"{time_str} 開盤與流動性執行監控"
        return await generate_analyst_report(report_type, raw_data)

    except Exception as e:
        logger.error(f"run_market_open_liquidity error: {e}")
        return (
            f"**{time_str} 開盤與流動性執行監控**\n"
            "--------------------------------------------------\n"
            f"系統分析發生錯誤: {e}"
        )
