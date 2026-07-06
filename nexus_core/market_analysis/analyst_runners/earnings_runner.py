"""Pre-market earnings & valuation scan logic for the Analyst Agent."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import database
from database.watchlist import get_all_watchlist
from market_time import ny_tz
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
from services import market_data_service
from services.llm_service import generate_analyst_report
from services.news_service import fetch_recent_news
from services.reddit_service import get_reddit_context
from cogs.embed_builder import create_earnings_report_embed, create_ai_analysis_embed

logger = logging.getLogger(__name__)


def _get_tw_time_str() -> str:
    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return now_tw.strftime("[%H:%M UTC+8]")


async def run_premarket_earnings():
    """Scan upcoming earnings events, enrich with quant metrics, and return an Embed."""
    time_str = _get_tw_time_str()
    try:
        from services.calendar_service import calendar_service

        # 獲取所有觀察名單與持倉標的
        watchlist = get_all_watchlist()
        portfolio = database.get_all_portfolio()
        symbols = list(
            set([row[1] for row in watchlist] + [row[2] for row in portfolio])
        )

        earnings_map = await calendar_service.get_symbol_earnings_batch(symbols)

        today = datetime.now(ny_tz).date()
        valid_earnings = []
        for sym, info in earnings_map.items():
            if info is not None:
                try:
                    e_date = datetime.strptime(info.date, "%Y-%m-%d").date()
                    days_left = (e_date - today).days
                    if 0 <= days_left <= 14:
                        valid_earnings.append(
                            {
                                "symbol": sym,
                                "date": info.date,
                                "days_left": days_left,
                            }
                        )
                except Exception as ve:
                    logger.warning(f"Error parsing earnings date for {sym}: {ve}")

        valid_earnings.sort(key=lambda x: x["days_left"])
        top_earnings = valid_earnings[:10]

        # 緊迫度分級：2 天內財報 → 深度掃描；其餘 → 輕量掃描
        deep_scan_symbols = [
            item["symbol"] for item in top_earnings if item["days_left"] <= 2
        ]
        light_scan_symbols = [
            item["symbol"] for item in top_earnings if item["days_left"] > 2
        ]

        sem = asyncio.Semaphore(3)

        async def deep_scan_symbol(sym):
            async with sem:
                return await asyncio.gather(
                    evaluate_watchlist_symbol(sym),
                    SentimentEngine.calculate_pcr(sym),
                    market_data_service.get_company_profile(sym),
                    return_exceptions=True,
                )

        async def light_scan_symbol(sym):
            async with sem:
                return await market_data_service.get_company_profile(sym)

        deep_results_list = await asyncio.gather(
            *[deep_scan_symbol(sym) for sym in deep_scan_symbols],
            return_exceptions=True,
        )
        deep_results_map: dict = {}
        for sym, res in zip(deep_scan_symbols, deep_results_list):
            if isinstance(res, Exception) or not isinstance(res, (list, tuple)):
                deep_results_map[sym] = (None, {"pcr": 0.0, "state": "ERROR"}, {})
            else:
                deep_results_map[sym] = (
                    res[0] if not isinstance(res[0], Exception) else None,
                    res[1]
                    if not isinstance(res[1], Exception)
                    else {"pcr": 0.0, "state": "ERROR"},
                    res[2] if not isinstance(res[2], Exception) else {},
                )

        light_results_list = await asyncio.gather(
            *[light_scan_symbol(sym) for sym in light_scan_symbols],
            return_exceptions=True,
        )
        light_results_map: dict = {
            sym: ({} if isinstance(res, Exception) else res)
            for sym, res in zip(light_scan_symbols, light_results_list)
        }

        earnings_data: dict = {}
        for item in top_earnings:
            sym = item["symbol"]
            metrics_payload = {
                "date": item["date"],
                "days_left": item["days_left"],
                "current_price": 0.0,
                "rsi_14": 50.0,
                "pe_ratio": None,
                "bias_ma20": 0.0,
                "iv_rank": 0.0,
                "option_skew": 0.0,
                "pcr": 0.0,
                "sector": "Unknown",
            }

            if sym in deep_results_map:
                eval_res, pcr_res, prof_res = deep_results_map[sym]
                metrics_payload.update(
                    {
                        "pcr": pcr_res.get("pcr", 0.0),
                        "sector": prof_res.get("finnhubIndustry", "Unknown"),
                    }
                )
                if eval_res is not None and eval_res.metrics is not None:
                    m = eval_res.metrics
                    metrics_payload.update(
                        {
                            "current_price": m.current_price,
                            "rsi_14": m.rsi_14,
                            "pe_ratio": m.pe_ratio,
                            "bias_ma20": getattr(
                                m,
                                "bias_ma20",
                                (m.current_price / m.ma20 - 1.0) if m.ma20 else 0.0,
                            ),
                            "iv_rank": m.iv_rank,
                            "option_skew": m.option_skew,
                        }
                    )
            elif sym in light_results_map:
                metrics_payload["sector"] = light_results_map[sym].get(
                    "finnhubIndustry", "Unknown"
                )

            earnings_data[sym] = [metrics_payload]

        # 情緒掃描：取前 2 個最緊迫標的的新聞 + Reddit
        upcoming_symbols = [item["symbol"] for item in valid_earnings[:2]]
        sentiment_data: dict = {}
        if upcoming_symbols:
            news_results = await asyncio.gather(
                *[fetch_recent_news(sym) for sym in upcoming_symbols],
                return_exceptions=True,
            )
            reddit_results: list
            if database.any_user_local_tunnel_enabled():
                reddit_results = await asyncio.gather(
                    *[get_reddit_context(sym) for sym in upcoming_symbols],
                    return_exceptions=True,
                )
            else:
                reddit_results = ["本地 Tunnel 已關閉，略過 Reddit 情緒。"] * len(
                    upcoming_symbols
                )

            for i, sym in enumerate(upcoming_symbols):
                sentiment_data[sym] = {
                    "news": (
                        news_results[i]
                        if not isinstance(news_results[i], Exception)
                        else "無法獲取"
                    ),
                    "reddit_sentiment": (
                        reddit_results[i]
                        if not isinstance(reddit_results[i], Exception)
                        else "無法獲取"
                    ),
                }

        raw_data = {
            "analyzed_symbols": len(symbols),
            "upcoming_earnings": earnings_data,
            "earnings_sentiment_scan": sentiment_data,
            "note": "IV and VRP are evaluated dynamically based on recent price action. Proximity sorted (max 10).",
        }

        report_type = f"{time_str} 盤前財報與估值調整"
        report_content = await generate_analyst_report(report_type, raw_data)
        return create_earnings_report_embed(report_type, report_content, raw_data)

    except Exception as e:
        logger.error(f"run_premarket_earnings error: {e}")
        return create_ai_analysis_embed(
            f"**{time_str} 盤前財報與估值調整**\n--------------------------------------------------\n系統分析發生錯誤: {e}",
            title="📊 Nexus Seeker 盤前財報與估值調整",
        )
