"""Live integration test script for 標的分析中心: {symbol}
Tests data fetching from core & edge and validates embed presentation logic for TSLA, NVDA, SPCX, QQQ.
"""

import asyncio
import logging
import os
import sys
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from cogs.embed_builders import (  # noqa: E402
    create_media_sentiment_embed,
    create_tactical_hedge_embed,
    create_tactical_symbol_embed,
)
from cogs.unified_terminal.symbol_deep_dive import (  # noqa: E402
    SymbolDeepDiveMixin,
)
from database import init_db  # noqa: E402
import discord  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("live_test_symbol_analysis")


class DummyBot:
    def __init__(self) -> None:
        self.user = MagicMock()
        self.user.id = 123456789
        self.polymarket_service = None
        self._latest_radar_data_cache: dict[str, Any] = {}
        self._latest_radar_cache_time = 0.0


class DummySymbolDeepDive(SymbolDeepDiveMixin):
    def __init__(self, bot: Any) -> None:
        self.bot = bot


def validate_embed(embed: discord.Embed, symbol: str) -> List[str]:
    """Validate Discord Embed constraints and presentation integrity."""
    issues: List[str] = []

    if not embed.title:
        issues.append("Embed title is empty")
    elif len(embed.title) > 256:
        issues.append(f"Embed title exceeds 256 chars ({len(embed.title)})")

    if embed.description and len(embed.description) > 4096:
        issues.append(
            f"Embed description exceeds 4096 chars ({len(embed.description)})"
        )

    if len(embed.fields) > 25:
        issues.append(f"Embed fields count exceeds 25 ({len(embed.fields)})")

    total_chars = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total_chars += len(embed.footer.text)
        if len(embed.footer.text) > 2048:
            issues.append(
                f"Embed footer text exceeds 2048 chars ({len(embed.footer.text)})"
            )

    for i, field in enumerate(embed.fields):
        name_str = str(field.name or "")
        val_str = str(field.value or "")
        name_len = len(name_str)
        val_len = len(val_str)
        total_chars += name_len + val_len

        if name_len > 256:
            issues.append(
                f"Field {i} ('{name_str[:20]}...') name exceeds 256 chars ({name_len})"
            )
        if val_len > 1024:
            issues.append(
                f"Field {i} ('{name_str[:20]}...') value exceeds 1024 chars ({val_len})"
            )

        if "```ansi" in val_str:
            fence_count = val_str.count("```")
            if fence_count % 2 != 0:
                issues.append(
                    f"Field {i} ('{name_str[:20]}...') has unclosed ``` block"
                )

    if total_chars > 6000:
        issues.append(f"Embed total characters exceed 6000 ({total_chars})")

    return issues


async def run_live_test_for_symbol(
    deep_dive: DummySymbolDeepDive, symbol: str
) -> Dict[str, Any]:
    logger.info("============================================================")
    logger.info(f"Starting test for symbol: {symbol}")
    logger.info("============================================================")

    start_time = time.time()
    res_summary: Dict[str, Any] = {
        "symbol": symbol,
        "success": False,
        "duration_seconds": 0.0,
        "raw_data_keys": [],
        "quote": None,
        "skew": None,
        "pcr": None,
        "iv_metrics": None,
        "max_pain": None,
        "month_max_pains_count": 0,
        "gex_profile_present": False,
        "volume_profile_present": False,
        "uoa_count": 0,
        "catalysts_count": 0,
        "kelly_sizing_present": False,
        "reddit_score": None,
        "embed_issues": [],
        "media_embed_issues": [],
        "hedge_embed_issues": [],
        "embed_fields": [],
        "errors": [],
    }

    try:
        # 1. Fetch raw data
        raw_data = await deep_dive._fetch_single_symbol_data_raw(symbol)
        fetch_duration = time.time() - start_time
        res_summary["raw_data_keys"] = list(raw_data.keys())
        logger.info(f"[{symbol}] Raw data fetched in {fetch_duration:.2f}s")

        # 2. Extract key metrics for inspection
        quote = raw_data.get("quote") or {}
        res_summary["quote"] = {
            "c": quote.get("c"),
            "dp": quote.get("dp"),
            "h": quote.get("h"),
            "l": quote.get("l"),
        }
        res_summary["skew"] = raw_data.get("skew_data")
        res_summary["pcr"] = raw_data.get("pcr_data")
        res_summary["iv_metrics"] = str(raw_data.get("iv_metrics"))
        res_summary["max_pain"] = raw_data.get("max_pain_data")
        res_summary["month_max_pains_count"] = len(
            raw_data.get("month_max_pains") or []
        )
        res_summary["gex_profile_present"] = bool(
            raw_data.get("gex_profile_data", {}).get("gex_profile")
        )
        res_summary["volume_profile_present"] = bool(raw_data.get("volume_profile"))
        uoa_list = raw_data.get("uoa_data") or []
        res_summary["uoa_count"] = len(uoa_list)
        res_summary["catalysts_count"] = len(raw_data.get("catalysts") or [])

        # 3. Process data through unified _process_symbol_hub_data
        result = await deep_dive._process_symbol_hub_data(symbol, 123456789, raw_data)
        res_summary["kelly_sizing_present"] = bool(result.get("kelly_sizing"))
        res_summary["reddit_score"] = result.get("reddit_sentiment_score")

        # 4. Generate & Validate Tactical Symbol Embed (Home / Main Hub)
        main_embed = create_tactical_symbol_embed(result)
        issues = validate_embed(main_embed, symbol)
        res_summary["embed_issues"] = issues
        res_summary["embed_title"] = main_embed.title
        res_summary["embed_fields"] = [f.name for f in main_embed.fields]

        # 5. Generate & Validate Media Sentiment Embed (Media Tab)
        from services import news_service

        news_items = await news_service.fetch_recent_news_structured(symbol)
        media_embed = create_media_sentiment_embed(
            symbol,
            news_items=news_items,
            reddit_text=result.get("reddit_text"),
            polymarket_odds=result.get("polymarket_odds"),
            polymarket_summary=result.get("polymarket_summary"),
            reddit_posts=result.get("reddit_posts"),
            reddit_sentiment_score=result.get("reddit_sentiment_score"),
            skew_val=result.get("skew"),
            skew_percentile=result.get("skew_percentile"),
            pcr_val=result.get("pcr", {}).get("volume_pcr")
            if isinstance(result.get("pcr"), dict)
            else None,
        )
        media_issues = validate_embed(media_embed, symbol)
        res_summary["media_embed_issues"] = media_issues

        # 6. Generate & Validate Tactical Hedge Embed (Hedge Tab)
        ivr = float(result.get("iv_rank") or 50.0)
        rec_strategy = (
            "Bull Put Spread (賣出認沽價差策略)"
            if ivr > 50.0
            else "Bear Debits / Put Protection (買入保護性認沽)"
        )
        hedge_embed = create_tactical_hedge_embed(symbol, ivr, rec_strategy)
        hedge_issues = validate_embed(hedge_embed, symbol)
        res_summary["hedge_embed_issues"] = hedge_issues

        res_summary["success"] = (
            len(issues) == 0 and len(media_issues) == 0 and len(hedge_issues) == 0
        )

        logger.info(f"[{symbol}] Main Embed generated with title: {main_embed.title}")
        for f in main_embed.fields:
            field_val_str = str(f.value or "")
            logger.info(f"  Field: {f.name} (length: {len(field_val_str)} chars)")
            print(f"\n--- [{symbol}] FIELD: {f.name} ---\n{field_val_str}")

        if issues:
            logger.error(f"[{symbol}] Main Embed validation issues: {issues}")
        if media_issues:
            logger.error(f"[{symbol}] Media Embed validation issues: {media_issues}")
        if hedge_issues:
            logger.error(f"[{symbol}] Hedge Embed validation issues: {hedge_issues}")

    except Exception as e:
        logger.exception(f"[{symbol}] Exception during test: {e}")
        res_summary["errors"].append(str(e))

    res_summary["duration_seconds"] = time.time() - start_time
    return res_summary


async def main() -> None:
    init_db()

    logger.info(f"TUNNEL_URL configured as: {getattr(config, 'TUNNEL_URL', '')}")
    logger.info(
        f"FINNHUB_API_KEY configured: {bool(getattr(config, 'FINNHUB_API_KEY', ''))}"
    )

    bot = DummyBot()
    deep_dive = DummySymbolDeepDive(bot)

    symbols = ["TSLA", "NVDA", "SPCX", "QQQ"]
    all_results: dict[str, Any] = {}

    for sym in symbols:
        res = await run_live_test_for_symbol(deep_dive, sym)
        all_results[sym] = res

    print("\n" + "=" * 80)
    print("TEST SUMMARY RESULT:")
    print("=" * 80)
    for sym, res in all_results.items():
        status = "✅ PASS" if res["success"] else "❌ FAIL"
        print(
            f"Symbol: {sym:<6} | Status: {status} | Duration: {res['duration_seconds']:.2f}s | Title: {res.get('embed_title')}"
        )
        if res.get("embed_issues"):
            print(f"  Issues: {res['embed_issues']}")
        if res.get("errors"):
            print(f"  Errors: {res['errors']}")
        print(f"  Quote: {res.get('quote')}")
        print(f"  Skew: {res.get('skew')}")
        print(f"  PCR: {res.get('pcr')}")
        print(
            f"  Max Pain: {res.get('max_pain')} (Month MPs: {res.get('month_max_pains_count')})"
        )
        print(
            f"  GEX Profile: {res.get('gex_profile_present')} | VP: {res.get('volume_profile_present')} | UOA Count: {res.get('uoa_count')}"
        )
        print(f"  Reddit: {res.get('reddit_score')}")
        print("-" * 80)


if __name__ == "__main__":
    asyncio.run(main())
