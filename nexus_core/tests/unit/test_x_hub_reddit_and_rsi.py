"""整合測試：Reddit 降級/分類/重試/呈現修復，以及 /x 週線 RSI(14) & 日線重返顯示。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pandas as pd
import pytest

from cogs.embed_builders.alert_embeds.sentiment_feeds import (
    create_media_sentiment_embed,
)
from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from cogs.trading.scheduler import SchedulerCog
from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin
from cogs.unified_terminal.symbol_view import SymbolHubView
from market_analysis.rsi_reentry import (
    REENTRY_TRIGGERED,
    RsiReentryStatus,
    WEEKLY_NEAR_OVERSOLD,
)
from services import reddit_service


def _mock_http_client(
    *, json_payload: dict[str, Any] | None = None, side_effect: Exception | None = None
) -> AsyncMock:
    client = AsyncMock()
    if side_effect is not None:
        client.get = AsyncMock(side_effect=side_effect)
    else:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = json_payload or {}
        resp.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_reddit_details_kv_cache_fallback_on_edge_429_404_timeout() -> None:
    cached_str = (
        "[wallstreetbets] NVDA calls yolo moon\n"
        "[stocks] NVDA breakout above resistance\n"
    )
    for err_payload, side_fx in [
        ({"status": "error", "error_code": "rate_limited", "data": "429"}, None),
        ({"status": "error", "error_code": "upstream_404", "data": "404"}, None),
        (None, httpx.ReadTimeout("timeout")),
    ]:
        client = _mock_http_client(json_payload=err_payload, side_effect=side_fx)
        with (
            patch("services.reddit_service.config") as mock_cfg,
            patch("services.reddit_service.httpx.AsyncClient", return_value=client),
            patch(
                "services.reddit_service.get_kv_cache_fresh", return_value=cached_str
            ) as mock_cache,
        ):
            mock_cfg.TUNNEL_URL = "http://localhost:8000"
            text, posts = await reddit_service.get_reddit_details("nvda", limit=2)

        assert text is not None and text.startswith("[快取]\n")
        assert len(posts) == 2
        assert posts[0]["subreddit"] == "wallstreetbets"
        assert posts[0]["title"] == "NVDA calls yolo moon"
        mock_cache.assert_called_once_with(
            "reddit_sentiment_NVDA",
            reddit_service.REDDIT_CACHE_FALLBACK_MAX_AGE_SECONDS,
        )


@pytest.mark.asyncio
async def test_reddit_details_preserves_specific_error_when_cache_empty() -> None:
    cases: list[tuple[dict[str, Any] | None, Exception | None, str]] = [
        (
            {"status": "error", "error_code": "rate_limited", "data": "429"},
            None,
            reddit_service.REDDIT_ERR_RATE_LIMITED,
        ),
        (
            {"status": "error", "error_code": "upstream_404", "data": "Not found"},
            None,
            reddit_service.REDDIT_ERR_UPSTREAM_HTTP,
        ),
        (
            {"status": "error", "error_code": "upstream_403", "data": "Forbidden"},
            None,
            reddit_service.REDDIT_ERR_UPSTREAM_HTTP,
        ),
        (None, httpx.ReadTimeout("timed out"), reddit_service.REDDIT_ERR_TIMEOUT),
    ]
    for err_payload, side_fx, expected_msg in cases:
        client = _mock_http_client(json_payload=err_payload, side_effect=side_fx)
        with (
            patch("services.reddit_service.config") as mock_cfg,
            patch("services.reddit_service.httpx.AsyncClient", return_value=client),
            patch("services.reddit_service.get_kv_cache_fresh", return_value=None),
        ):
            mock_cfg.TUNNEL_URL = "http://localhost:8000"
            text, posts = await reddit_service.get_reddit_details("AAPL")

        assert text == expected_msg
        assert posts == []


@pytest.mark.asyncio
async def test_daily_reddit_update_skips_none_to_preserve_prior_cache() -> None:
    bot = MagicMock()
    bot._is_leader_instance = True
    cog = SchedulerCog(bot)

    with (
        patch("config.TUNNEL_URL", "http://localhost:8000"),
        patch("database.get_all_watchlist", return_value=[(1, "NVDA"), (1, "AAPL")]),
        patch(
            "services.reddit_service.get_reddit_context_batch",
            new_callable=AsyncMock,
            return_value={"NVDA": None, "AAPL": "[stocks] AAPL rally\n"},
        ),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock) as mock_save,
    ):
        await cog.daily_reddit_update.coro(cog)

    mock_save.assert_awaited_once_with("reddit_sentiment_AAPL", "[stocks] AAPL rally\n")


def test_classify_reddit_sentiment_lexicon_negation_and_error_guards() -> None:
    # 1. Post title containing "錯誤" / "超時" must NOT false-positive as service error
    posts_with_error_words = [
        {"subreddit": "stocks", "title": "NVDA 超時未出貨傳聞被否認，股價大漲突破"}
    ]
    assert (
        reddit_service.classify_reddit_sentiment(
            "[stocks] NVDA 超時未出貨傳聞被否認，股價大漲突破", posts_with_error_words
        )
        == "🚀 樂觀 (Bullish)"
    )
    assert reddit_service.is_reddit_error_text("[stocks] 系統錯誤新聞討論") is False

    # 2. Bullish / Bearish / Negation / Cached
    assert (
        reddit_service.classify_reddit_sentiment(
            None, [{"title": "NVDA calls moon breakout"}]
        )
        == "🚀 樂觀 (Bullish)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            None, [{"title": "TSLA crash dump puts"}]
        )
        == "💀 恐慌 (Bearish)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            None, [{"title": "not bullish at all"}]
        )
        == "💀 恐慌 (Bearish)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            "[快取]\n[stocks] NVDA rally beat",
            [{"title": "NVDA rally beat"}],
        )
        == "🚀 樂觀 (Bullish) [快取]"
    )

    # 3. Specific service failure mappings
    assert (
        reddit_service.classify_reddit_sentiment(
            reddit_service.REDDIT_ERR_TUNNEL_UNSET, []
        )
        == "⚠️ 無法抓取 (未配置邊緣節點)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(reddit_service.REDDIT_ERR_TIMEOUT, [])
        == "⚠️ 抓取失敗 (邊緣節點超時)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            reddit_service.REDDIT_ERR_RATE_LIMITED, []
        )
        == "⚠️ 抓取失敗 (Reddit 上游限流)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            reddit_service.REDDIT_ERR_UPSTREAM_HTTP, []
        )
        == "⚠️ 抓取失敗 (Reddit 上游異常)"
    )
    assert (
        reddit_service.classify_reddit_sentiment(
            reddit_service.REDDIT_ERR_EDGE_GENERIC, []
        )
        == "⚠️ 抓取失敗 (邊緣節點異常)"
    )


@pytest.mark.asyncio
async def test_btn_media_retries_on_error_text_and_recomputes_score(
    mock_interaction: Any,
) -> None:
    bot = MagicMock()
    view = SymbolHubView(symbol="NVDA", user_id=1, bot=bot)
    view.base_data = {
        "symbol": "NVDA",
        "reddit_text": reddit_service.REDDIT_ERR_TIMEOUT,
        "reddit_posts": [],
        "reddit_sentiment_score": "⚠️ 抓取失敗 (邊緣節點超時)",
    }
    retried_posts = [
        {
            "subreddit": "wallstreetbets",
            "title": "NVDA calls moon after beat",
            "url": "https://reddit.com/1",
        }
    ]

    with (
        patch(
            "services.news_service.fetch_recent_news_structured",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "services.reddit_service.get_reddit_details",
            new_callable=AsyncMock,
            return_value=("[wallstreetbets] NVDA calls moon after beat", retried_posts),
        ) as mock_retry,
    ):
        await view.btn_media.callback(mock_interaction)

    mock_retry.assert_awaited_once_with("NVDA")
    assert view.base_data["reddit_posts"] == retried_posts
    assert view.base_data["reddit_sentiment_score"] == "🚀 樂觀 (Bullish)"
    _, last_kwargs = mock_interaction.edit_original_response.call_args
    radar = next(
        f.value or ""
        for f in last_kwargs["embed"].fields
        if "輿情與期權共振雷達" in (f.name or "")
    )
    assert "🚀 樂觀 (Bullish)" in radar


def test_sentiment_feeds_timeout_bullet_and_missing_resonance() -> None:
    embed = create_media_sentiment_embed(
        "NVDA",
        reddit_text=reddit_service.REDDIT_ERR_TIMEOUT,
        reddit_posts=[],
        reddit_sentiment_score="⚠️ 抓取失敗 (邊緣節點超時)",
    )
    fields = {f.name: str(f.value) for f in embed.fields}
    radar = fields["📊 輿情與期權共振雷達"]
    reddit_block = fields["🔥 Reddit 社群熱門討論"]

    assert "⚪ Reddit 輿情缺失，散戶共振未判定" in radar
    assert "\u001b[1;30m⚠️ 抓取失敗 (邊緣節點超時)" in radar
    assert reddit_block.startswith("• 本地節點連線超時。")
    assert "```" not in reddit_block


@pytest.mark.asyncio
async def test_symbol_deep_dive_attaches_rsi_reentry_from_df_fallback() -> None:
    class _Harness(SymbolDeepDiveMixin):
        def __init__(self) -> None:
            self.bot = MagicMock()

    dates = pd.bdate_range(end="2026-10-07", periods=140)
    closes = [100.0 + (i % 5) for i in range(140)]
    df_daily = pd.DataFrame({"Close": closes}, index=dates)

    raw_data: dict[str, Any] = {
        "df_spy": pd.DataFrame({"Close": [500.0]}, index=dates[-1:]),
        "macro_raw": {"vix": 16.0},
        "quote": {"c": 102.0, "h": 103.0, "l": 100.0, "pc": 101.0, "d": 1.0, "dp": 1.0},
        "skew_data": {"skew": 3.0, "skew_percentile": 50.0},
        "pcr_data": {"volume_pcr": 0.8},
        "uoa_data": [],
        "sto_physical_cap_strikes": [],
        "max_pain_data": {"max_pain": 100.0},
        "iv_metrics": {"iv_rank": 40.0, "current_iv": 0.4},
        "reddit_text": "[stocks] NVDA breakout rally",
        "reddit_posts": [{"subreddit": "stocks", "title": "NVDA breakout rally"}],
        "poly_markets": [],
        "ddp_report": {"is_ddp": False},
        "squeeze_eval": SimpleNamespace(rsi_reentry=None, matrix={}),
        "df_hist_1d": df_daily,
        "df_hist_fetched_at": datetime(2026, 10, 7, 16, 0),
        "month_max_pains": [],
        "option_expiries": [],
        "gex_profile_data": {},
        "volume_profile": {},
        "atr_15m": 1.0,
        "session_vwap": 101.0,
        "session_stats": None,
        "bar_15m": None,
        "catalysts": [],
    }

    harness = _Harness()
    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
        patch(
            "market_math.analyze_symbol",
            new_callable=AsyncMock,
            return_value={"symbol": "NVDA", "price": 102.0},
        ),
        patch(
            "cogs.unified_terminal.utils.find_matching_polymarket_odds",
            new_callable=AsyncMock,
            return_value="N/A",
        ),
        patch(
            "cogs.unified_terminal.utils.calculate_polymarket_weighted_odds",
            new_callable=AsyncMock,
            return_value="N/A",
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.risk_engine.load_available_collateral",
            new_callable=AsyncMock,
            return_value=(100000.0, None),
        ),
        patch("market_analysis.risk_engine.optimize_position_risk", return_value=None),
        patch(
            "database.user_settings.get_full_user_context",
            return_value=MagicMock(
                capital=100000.0, risk_limit=15.0, cash_reserve=0.0, load_failed=False
            ),
        ),
    ):
        res = await harness._process_symbol_hub_data("NVDA", 1, raw_data)

    assert res["reddit_text"] == "[stocks] NVDA breakout rally"
    assert res["reddit_sentiment_score"] == "🚀 樂觀 (Bullish)"
    assert isinstance(res["rsi_reentry"], RsiReentryStatus)
    assert res["rsi_reentry"].weekly_rsi is not None


def test_portfolio_embed_renders_rsi_reentry_tag() -> None:
    data: dict[str, Any] = {
        "symbol": "NVDA",
        "price": 120.0,
        "quote": {
            "c": 120.0,
            "dp": 1.0,
            "d": 1.2,
            "o": 119.0,
            "h": 121.0,
            "l": 118.5,
            "pc": 118.8,
        },
        "psq_result": {
            "is_squeezing": True,
            "momentum": 12.5,
            "squeeze_level": "Mid",
            "direction": "Bullish",
        },
        "rsi_reentry": RsiReentryStatus(
            weekly_rsi=33.2,
            weekly_rsi_live=33.8,
            weekly_state=WEEKLY_NEAR_OVERSOLD,
            weekly_is_oversold=False,
            daily_rsi=31.5,
            daily_prev_rsi=28.0,
            daily_reentry_state=REENTRY_TRIGGERED,
            daily_above_sma20=True,
            daily_reentry_bars_since_oversold=1,
        ),
    }
    embed = create_tactical_symbol_embed(data)
    sqz_field = next(
        (f.value or "" for f in embed.fields if "動能與擠壓狀態" in (f.name or "")),
        "",
    )
    assert "週線 RSI(14): 33.2 (🟡近超賣) │ 日線重返: 🟢觸發重返" in sqz_field
