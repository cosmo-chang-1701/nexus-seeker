"""Unit tests for Edge Detection & Reddit Sentiment Index using NVDA and TSLA.

Covers:
1. StockAliasMatrix alias resolution & Boolean search query generation for NVDA & TSLA.
2. Keyword matching against raw Reddit post titles.
3. LLM-based structured sentiment classification (Bullish, Bearish, Neutral).
4. Batch feed retrieval and local symbol multi-dispatching.
5. Edge Detection divergence decision tree (Retail vs Dealer Skew, Structural Divergence, IV Suppression, Price+Skew Warning).
6. ANSI Radar Embed card rendering and dynamic market intention formatting.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from market_analysis.stock_alias_matrix import StockAliasMatrix
from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed


# =====================================================================
# 1. NVDA & TSLA Alias Matrix & Query Generation Tests
# =====================================================================


@pytest.mark.asyncio
async def test_nvda_alias_and_query_generation() -> None:
    """驗證 NVDA 別名解析與 Reddit Boolean Search Query 構建。"""
    aliases: List[str] = await StockAliasMatrix.get_aliases_for_symbol("NVDA")
    assert "nvda" in aliases
    assert "nvidia" in aliases
    assert "jensen huang" in aliases
    assert "blackwell" in aliases

    query: str = StockAliasMatrix.build_reddit_query("NVDA", aliases)
    assert '"NVDA"' in query
    assert '"$NVDA"' in query
    assert '"nvidia"' in query
    assert '"jensen huang"' in query


@pytest.mark.asyncio
async def test_tsla_alias_and_query_generation() -> None:
    """驗證 TSLA 別名解析與 Reddit Boolean Search Query 構建。"""
    aliases: List[str] = await StockAliasMatrix.get_aliases_for_symbol("TSLA")
    assert "tsla" in aliases
    assert "tesla" in aliases
    assert "elon musk" in aliases
    assert "robotaxi" in aliases

    query: str = StockAliasMatrix.build_reddit_query("TSLA", aliases)
    assert '"TSLA"' in query
    assert '"$TSLA"' in query
    assert '"tesla"' in query
    assert '"elon musk"' in query


def test_reddit_post_keyword_matching_nvda_and_tsla() -> None:
    """驗證嚴格詞界與別名在真實 Reddit 標題中的匹配精準度。"""
    nvda_aliases: List[str] = ["nvda", "nvidia", "jensen huang"]
    tsla_aliases: List[str] = ["tsla", "tesla", "elon musk", "robotaxi"]

    # NVDA matching
    assert StockAliasMatrix.is_text_matching_symbol(
        "[wallstreetbets] NVDA calls 140 YOLO to the moon", "NVDA", nvda_aliases
    )
    assert StockAliasMatrix.is_text_matching_symbol(
        "[stocks] Jensen Huang keynote was insane, AI demand accelerating",
        "NVDA",
        nvda_aliases,
    )
    assert not StockAliasMatrix.is_text_matching_symbol(
        "[options] AMD and INTC earnings discussion", "NVDA", nvda_aliases
    )

    # TSLA matching
    assert StockAliasMatrix.is_text_matching_symbol(
        "[wallstreetbets] TSLA put buyers in shambles", "TSLA", tsla_aliases
    )
    assert StockAliasMatrix.is_text_matching_symbol(
        "[stocks] Elon Musk announces robotaxi event date", "TSLA", tsla_aliases
    )
    assert not StockAliasMatrix.is_text_matching_symbol(
        "[stocks] Rivian and Lucid deliveries update", "TSLA", tsla_aliases
    )


# =====================================================================
# 2. Reddit Sentiment Evaluation (LLM & Rule Engine)
# =====================================================================


@pytest.mark.asyncio
async def test_nvda_llm_sentiment_bullish_evaluation() -> None:
    """驗證 NVDA 散戶做多貼文被精準評定為 '🚀 樂觀 (Bullish)'。"""
    from services.llm_service import evaluate_reddit_sentiment

    with patch(
        "services.llm_service.client.beta.chat.completions.parse",
        new_callable=AsyncMock,
    ) as mock_parse:
        mock_parsed_obj = MagicMock()
        mock_parsed_obj.choices = [
            MagicMock(
                message=MagicMock(parsed=MagicMock(sentiment="🚀 樂觀 (Bullish)"))
            )
        ]
        mock_parse.return_value = mock_parsed_obj

        raw_nvda_posts = "[wallstreetbets] NVDA 150 calls YOLO to the moon\n[options] Betting 50k on NVDA earnings beat"
        score = await evaluate_reddit_sentiment("NVDA", raw_nvda_posts)

        assert score == "🚀 樂觀 (Bullish)"
        mock_parse.assert_called_once()


@pytest.mark.asyncio
async def test_tsla_llm_sentiment_bearish_evaluation() -> None:
    """驗證 TSLA 散戶恐慌貼文被精準評定為 '💀 恐慌 (Bearish)'。"""
    from services.llm_service import evaluate_reddit_sentiment

    with patch(
        "services.llm_service.client.beta.chat.completions.parse",
        new_callable=AsyncMock,
    ) as mock_parse:
        mock_parsed_obj = MagicMock()
        mock_parsed_obj.choices = [
            MagicMock(
                message=MagicMock(parsed=MagicMock(sentiment="💀 恐慌 (Bearish)"))
            )
        ]
        mock_parse.return_value = mock_parsed_obj

        raw_tsla_posts = "[wallstreetbets] TSLA crashing, buying 180 puts\n[stocks] Tesla margins destroyed by price war"
        score = await evaluate_reddit_sentiment("TSLA", raw_tsla_posts)

        assert score == "💀 恐慌 (Bearish)"
        mock_parse.assert_called_once()


# =====================================================================
# 3. Batch Feed Retrieval & Multi-Symbol Dispatching
# =====================================================================


@pytest.mark.asyncio
async def test_batch_feed_dispatching_for_nvda_and_tsla() -> None:
    """驗證單次 Feed 請求中，NVDA 與 TSLA 貼文能被精準分派至各自的摘要。"""
    from services.reddit_service import get_reddit_context_batch

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": [
            {
                "title": "NVDA hitting new all time highs after Blackwell shipment news",
                "subreddit": "stocks",
            },
            {
                "title": "TSLA robotaxi delay causes selloff",
                "subreddit": "wallstreetbets",
            },
            {
                "title": "General market discussion on Fed rate cuts",
                "subreddit": "stocks",
            },
        ],
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("services.reddit_service.httpx.AsyncClient", return_value=mock_client),
        patch("services.reddit_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        batch_result = await get_reddit_context_batch(["NVDA", "TSLA"])

        assert "NVDA" in batch_result
        assert "TSLA" in batch_result
        assert batch_result["NVDA"] is not None and "Blackwell" in batch_result["NVDA"]
        assert batch_result["TSLA"] is not None and "robotaxi" in batch_result["TSLA"]


# =====================================================================
# 4. Edge Detection & Divergence Decision Matrix Tests
# =====================================================================


def test_nvda_sentiment_divergence_bullish_retail_vs_dealer_skew() -> None:
    """情境 1 (NVDA): 散戶極度樂觀 (🚀) 且期權偏斜高 (Skew Percentile 84%)
    預期觸發: '情緒背離 (散戶樂觀 vs 專業避險)'，建議 '建立保護性賣權或減碼'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 125.50,
        "quote": {
            "c": 125.50,
            "dp": -0.5,
            "d": -0.6,
            "o": 126.0,
            "h": 126.5,
            "l": 124.5,
            "pc": 126.1,
        },
        "skew": 7.5,
        "skew_percentile": 84.0,
        "polymarket_odds": "68% 看多",
        "reddit_sentiment_score": "🚀 樂觀 (Bullish)",
        "pcr": {"volume_pcr": 0.8},
        "iv_data": {
            "iv_rank": 65.0,
            "current_iv": 0.55,
            "iv_percentile": 70.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    assert "📐 情緒與邊緣偵測 (Edge Detection)" in fields
    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])

    assert "情緒背離 (散戶樂觀 vs 專業避險)" in edge_text
    assert "建立保護性賣權或減碼" in edge_text
    assert "🚀 樂觀 (Bullish)" in edge_text
    assert "68% 看多" in edge_text
    assert "84.0%" in edge_text


def test_nvda_structural_warning_price_up_and_extreme_skew() -> None:
    """情境 2 (NVDA): 現價上漲 (dp > 0) 且 Skew > 90%
    預期觸發: '⚠️ 警告：結構性情緒背離'，建議 '留意結構性背離：建議降槓桿、以保護性結構防禦'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 128.00,
        "quote": {
            "c": 128.00,
            "dp": 1.8,
            "d": 2.3,
            "o": 126.0,
            "h": 129.0,
            "l": 125.5,
            "pc": 125.7,
        },
        "skew": 9.2,
        "skew_percentile": 93.0,
        "polymarket_odds": "72% 看多",
        "reddit_sentiment_score": "🚀 樂觀 (Bullish)",
        "pcr": {"volume_pcr": 0.75},
        "iv_data": {
            "iv_rank": 60.0,
            "current_iv": 0.52,
            "iv_percentile": 65.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    assert "⚠️ 警告：結構性情緒背離" in edge_text
    assert "留意結構性背離：建議降槓桿、以保護性結構防禦" in edge_text


def test_tsla_sentiment_divergence_bearish_retail_vs_cheap_premium() -> None:
    """情境 3 (TSLA): 散戶極度恐慌 (💀) 但期權偏斜極低/權利金便宜 (Skew Percentile 12%)
    預期觸發: '情緒背離 (散戶恐慌 vs 權利金便宜)'，建議 '考慮賣出賣權 (Cash Secured Put)'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "TSLA",
        "price": 210.00,
        "quote": {
            "c": 210.00,
            "dp": -2.5,
            "d": -5.4,
            "o": 215.0,
            "h": 216.0,
            "l": 208.0,
            "pc": 215.4,
        },
        "skew": 1.2,
        "skew_percentile": 12.0,
        "polymarket_odds": "35% 看多",
        "reddit_sentiment_score": "💀 恐慌 (Bearish)",
        "pcr": {"volume_pcr": 0.9},
        "iv_data": {
            "iv_rank": 30.0,
            "current_iv": 0.45,
            "iv_percentile": 35.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    assert "📐 情緒與邊緣偵測 (Edge Detection)" in fields
    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])

    assert "情緒背離 (散戶恐慌 vs 權利金便宜)" in edge_text
    assert "考慮賣出賣權 (Cash Secured Put)" in edge_text
    assert "💀 恐慌 (Bearish)" in edge_text
    assert "12.0%" in edge_text


def test_nvda_structural_divergence_skew_vs_volume_pcr() -> None:
    """情境 4 (NVDA): 結構性背離 (P1 優先級)
    Skew Percentile > 85% 且 Volume PCR < 0.4 (散戶/動能瘋狂買 Call，但機構重金買 Put 避險)
    預期觸發: '⚠️ 警告：結構性情緒背離'，建議 '高度背離：避免追價買權；僅允許小倉位收租並搭配保護'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 130.00,
        "quote": {
            "c": 130.00,
            "dp": -0.2,
            "d": -0.3,
            "o": 130.5,
            "h": 131.0,
            "l": 129.5,
            "pc": 130.3,
        },
        "skew": 9.0,
        "skew_percentile": 88.0,
        "polymarket_odds": "N/A",
        "reddit_sentiment_score": "⚖️ 中性",
        "pcr": {"volume_pcr": 0.25},
        "iv_data": {
            "iv_rank": 50.0,
            "current_iv": 0.50,
            "iv_percentile": 55.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    assert "⚠️ 警告：結構性情緒背離" in edge_text
    assert "高度背離：避免追價買權；僅允許小倉位收租並搭配保護" in edge_text


def test_tsla_structural_divergence_low_skew_high_pcr() -> None:
    """情境 5 (TSLA): 結構性背離 (P1 優先級)
    Skew Percentile < 15% 且 Volume PCR > 1.5 (市場期權定價極度輕視下行，但成交量出現大量 Put 殺盤)
    預期觸發: '⚠️ 警告：結構性情緒背離'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "TSLA",
        "price": 195.00,
        "quote": {
            "c": 195.00,
            "dp": -1.5,
            "d": -3.0,
            "o": 198.0,
            "h": 199.0,
            "l": 194.0,
            "pc": 198.0,
        },
        "skew": 0.8,
        "skew_percentile": 10.0,
        "polymarket_odds": "N/A",
        "reddit_sentiment_score": "⚖️ 中性",
        "pcr": {"volume_pcr": 1.85},
        "iv_data": {
            "iv_rank": 40.0,
            "current_iv": 0.48,
            "iv_percentile": 42.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    assert "⚠️ 警告：結構性情緒背離" in edge_text


def test_darkpool_and_iv_suppression_divergence() -> None:
    """情境 6: 現價暴跌 (quote.dp < -3.0) 且 IV Rank 極低 (< 15%)
    預期觸發: '情緒背離 (現價暴跌但波動率極低)'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 110.00,
        "quote": {
            "c": 110.00,
            "dp": -5.0,
            "d": -5.8,
            "o": 115.0,
            "h": 116.0,
            "l": 109.0,
            "pc": 115.8,
        },
        "skew": 3.0,
        "skew_percentile": 40.0,
        "polymarket_odds": "N/A",
        "reddit_sentiment_score": "⚖️ 中性",
        "pcr": {"volume_pcr": 0.9},
        "iv_data": {
            "iv_rank": 10.0,
            "current_iv": 0.25,
            "iv_percentile": 12.0,
            "iv_status": "Low",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    assert "情緒背離 (現價暴跌但波動率極低)" in edge_text
    assert "異常背離：現價大跌但 IV Rank 極低" in edge_text


def test_nvda_normal_synchronized_market_state() -> None:
    """情境 7 (NVDA): 正常市場狀態
    無結構性背離，Skew 正常 (55%)，Reddit 中性
    預期觸發: '同步'，建議 '保持觀察'
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 128.00,
        "quote": {
            "c": 128.00,
            "dp": 0.5,
            "d": 0.6,
            "o": 127.5,
            "h": 129.0,
            "l": 127.0,
            "pc": 127.4,
        },
        "skew": 4.5,
        "skew_percentile": 55.0,
        "polymarket_odds": "52% 看多",
        "reddit_sentiment_score": "⚖️ 中性",
        "pcr": {"volume_pcr": 0.85},
        "iv_data": {
            "iv_rank": 45.0,
            "current_iv": 0.48,
            "iv_percentile": 50.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    assert "狀態: \x1b[1;32m同步\x1b[0m" in edge_text
    assert "建議: \x1b[1;32m保持觀察\x1b[0m" in edge_text


def test_edge_failure_graceful_degradation_and_hiding() -> None:
    """情境 8: 邊緣節點異常時的降級測試
    當 Reddit 抓取失敗且 Polymarket 為 N/A 時，自動隱藏意圖映射區塊，避免雜訊
    """
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 120.00,
        "quote": {
            "c": 120.00,
            "dp": 0.0,
            "d": 0.0,
            "o": 120.0,
            "h": 121.0,
            "l": 119.0,
            "pc": 120.0,
        },
        "skew": 4.0,
        "skew_percentile": 50.0,
        "polymarket_odds": "N/A",
        "reddit_sentiment_score": "⚠️ 抓取失敗 (邊緣節點異常)",
        "pcr": {"volume_pcr": 0.8},
        "iv_data": {
            "iv_rank": 40.0,
            "current_iv": 0.45,
            "iv_percentile": 42.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])
    # 意圖映射區塊應被優雅隱藏
    assert "巨鯨/散戶意圖映射" not in edge_text
    assert "狀態: \x1b[1;32m同步\x1b[0m" in edge_text


def test_polymarket_and_reddit_top_3_hyperlinks_in_embed() -> None:
    """驗證 Polymarket 預測勝率與 Reddit 前三名熱門文章皆以超連結形式正確呈現在卡片中。"""
    mock_data: Dict[str, Any] = {
        "symbol": "NVDA",
        "price": 128.00,
        "quote": {
            "c": 128.00,
            "dp": 0.5,
            "d": 0.6,
            "o": 127.5,
            "h": 129.0,
            "l": 127.0,
            "pc": 127.4,
        },
        "skew": 4.5,
        "skew_percentile": 55.0,
        "polymarket_odds": "[Will Nvidia hit $150 in September?](https://polymarket.com/event/nvda-150) (Yes: 68.0%)",
        "reddit_sentiment_score": "🚀 樂觀 (Bullish)",
        "reddit_posts": [
            {
                "title": "NVIDIA customers notified about AI-related price hikes above 15%",
                "subreddit": "wallstreetbets",
                "url": "https://www.reddit.com/r/wallstreetbets/comments/1vvrh14/nvidia_customers/",
            },
            {
                "title": "SPX one month premium at zero into NVDA, Jackson Hole, jobs",
                "subreddit": "options",
                "url": "https://www.reddit.com/r/options/comments/1vvpvj8/spx_one_month_premium/",
            },
            {
                "title": "NVDA raising prices 15% on certain chips per Bloomberg",
                "subreddit": "stocks",
                "url": "https://www.reddit.com/r/stocks/comments/1vvodz9/nvda_raising_prices/",
            },
            {
                "title": "Fourth post that should not be in top 3",
                "subreddit": "stocks",
                "url": "https://www.reddit.com/r/stocks/comments/ignored/",
            },
        ],
        "pcr": {"volume_pcr": 0.85},
        "iv_data": {
            "iv_rank": 45.0,
            "current_iv": 0.48,
            "iv_percentile": 50.0,
            "iv_status": "Normal",
        },
    }

    embed = create_tactical_symbol_embed(mock_data)
    fields = {f.name: f.value for f in embed.fields}

    edge_text = str(fields["📐 情緒與邊緣偵測 (Edge Detection)"])

    # 1. 驗證 Polymarket 超連結
    assert "**🐋 Polymarket 預測勝率：**" in edge_text
    assert (
        "[Will Nvidia hit $150 in September?](https://polymarket.com/event/nvda-150)"
        in edge_text
    )
    assert "(Yes: 68.0%)" in edge_text

    # 2. 驗證 Reddit 前三名熱門文章超連結
    assert "**📰 Reddit 熱門討論 (Top 3)：**" in edge_text
    assert (
        "[r/wallstreetbets: NVIDIA customers notified about AI-related price hik...](https://www.reddit.com/r/wallstreetbets/comments/1vvrh14/nvidia_customers/)"
        in edge_text
    )
    assert (
        "[r/options: SPX one month premium at zero into NVDA, Jackson Hol...](https://www.reddit.com/r/options/comments/1vvpvj8/spx_one_month_premium/)"
        in edge_text
    )
    assert (
        "[r/stocks: NVDA raising prices 15% on certain chips per Bloomberg](https://www.reddit.com/r/stocks/comments/1vvodz9/nvda_raising_prices/)"
        in edge_text
    )

    # 3. 驗證第 4 篇貼文未被列出 (嚴守 Top 3)
    assert "Fourth post" not in edge_text
