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
@pytest.mark.parametrize(
    "symbol, expected_aliases, expected_query_terms",
    [
        (
            "NVDA",
            ["nvda", "nvidia", "jensen huang", "blackwell"],
            ['"NVDA"', '"$NVDA"', '"nvidia"', '"jensen huang"'],
        ),
        (
            "TSLA",
            ["tsla", "tesla", "elon musk", "robotaxi"],
            ['"TSLA"', '"$TSLA"', '"tesla"', '"elon musk"'],
        ),
    ],
)
async def test_alias_and_query_generation(
    symbol: str, expected_aliases: List[str], expected_query_terms: List[str]
) -> None:
    """驗證別名解析與 Reddit Boolean Search Query 構建。"""
    aliases: List[str] = await StockAliasMatrix.get_aliases_for_symbol(symbol)
    for expected_alias in expected_aliases:
        assert expected_alias in aliases

    query: str = StockAliasMatrix.build_reddit_query(symbol, aliases)
    for term in expected_query_terms:
        assert term in query


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
@pytest.mark.parametrize(
    "symbol, raw_posts, expected_sentiment",
    [
        (
            "NVDA",
            "[wallstreetbets] NVDA 150 calls YOLO to the moon\n[options] Betting 50k on NVDA earnings beat",
            "🚀 樂觀 (Bullish)",
        ),
        (
            "TSLA",
            "[wallstreetbets] TSLA crashing, buying 180 puts\n[stocks] Tesla margins destroyed by price war",
            "💀 恐慌 (Bearish)",
        ),
    ],
)
async def test_llm_sentiment_evaluation(
    symbol: str, raw_posts: str, expected_sentiment: str
) -> None:
    """驗證散戶貼文被精準評定為 LLM 回傳的結構化情緒分類。"""
    from services.llm_service import evaluate_reddit_sentiment

    with patch(
        "services.llm_service.client.beta.chat.completions.parse",
        new_callable=AsyncMock,
    ) as mock_parse:
        mock_parsed_obj = MagicMock()
        mock_parsed_obj.choices = [
            MagicMock(message=MagicMock(parsed=MagicMock(sentiment=expected_sentiment)))
        ]
        mock_parse.return_value = mock_parsed_obj

        score = await evaluate_reddit_sentiment(symbol, raw_posts)

        assert score == expected_sentiment
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


def test_core_indicators_tab_only_retains_calculated_summary() -> None:
    """驗證『核心指標』頁籤僅在 ANSI 區塊保留計算後的 Polymarket 與 Reddit 結果，不展開文章列表與超連結。"""
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
        "polymarket_summary": "🟢 68.0% 巨鯨看多 (1檔加權 · 池量 $1.20M)",
        "polymarket_odds": "[Will Nvidia hit $150 in September?](https://polymarket.com/event/nvda-150) (Yes: 68.0% · 池量 $1.20M)",
        "reddit_sentiment_score": "🚀 樂觀 (Bullish)",
        "reddit_posts": [
            {
                "title": "NVIDIA customers notified about AI-related price hikes above 15%",
                "subreddit": "wallstreetbets",
                "url": "https://www.reddit.com/r/wallstreetbets/comments/1vvrh14/nvidia_customers/",
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

    # 1. 驗證 ANSI 區塊內保留計算結果
    assert "巨鯨/散戶意圖映射 (Market Intention)" in edge_text
    assert (
        "Polymarket: \x1b[1;34m🟢 68.0% 巨鯨看多 (1檔加權 · 池量 $1.20M)\x1b[0m"
        in edge_text
    )
    assert "Reddit: \x1b[1;32m🚀 樂觀 (Bullish)\x1b[0m" in edge_text

    # 2. 驗證超連結列表已自『核心指標』頁籤移除
    assert "**🐋 Polymarket 預測勝率：**" not in edge_text
    assert "**📰 Reddit 熱門討論" not in edge_text
    assert "https://www.reddit.com" not in edge_text


def test_media_sentiment_tab_renders_hyperlinks() -> None:
    """驗證『輿情社群』頁籤完整渲染共振雷達、Polymarket 預測事件、Reddit 熱門文章與結構化新聞超連結。"""
    from cogs.embed_builders import create_media_sentiment_embed

    posts = [
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
    ]
    news_items = [
        {
            "source": "Bloomberg",
            "headline": "NVIDIA AI revenue soars according to quarterly reports",
            "url": "https://finance.yahoo.com/news/nvidia-ai-revenue",
            "time_tag": "25分鐘前",
        }
    ]
    poly_odds = "[Will Nvidia hit $150 in September?](https://polymarket.com/event/nvda-150) (Yes: 68.0%)"

    embed = create_media_sentiment_embed(
        "NVDA",
        news_items=news_items,
        polymarket_odds=poly_odds,
        reddit_posts=posts,
        reddit_sentiment_score="🚀 樂觀 (Bullish)",
        skew_val=7.82,
        skew_percentile=28.5,
        pcr_val=0.65,
    )
    fields = {f.name: str(f.value) for f in embed.fields}

    # 1. 驗證頂部 ANSI 輿情與期權共振雷達
    assert "📊 輿情與期權共振雷達" in fields
    radar_text = fields["📊 輿情與期權共振雷達"]
    assert "巨鯨定價 (Polymarket)" in radar_text
    assert "散戶風向 (Reddit)" in radar_text
    assert "🚀 樂觀 (Bullish)" in radar_text
    assert "期權微觀結構 (Greeks & Skew)" in radar_text
    assert "Skew 值: " in radar_text
    assert "輿情籌碼共振 (Resonance Check)" in radar_text

    # 2. 驗證 Polymarket 預測事件超連結
    assert "🐋 Polymarket 預測事件" in fields
    poly_text = fields["🐋 Polymarket 預測事件"]
    assert (
        "[Will Nvidia hit $150 in September?](https://polymarket.com/event/nvda-150)"
        in poly_text
    )
    assert "(Yes: 68.0%)" in poly_text

    # 3. 驗證 Reddit 前三名熱門討論超連結 (純文章清單，無情緒指標重複)
    assert "🔥 Reddit 社群熱門討論" in fields
    reddit_field = fields["🔥 Reddit 社群熱門討論"]
    assert "**情緒指標：**" not in reddit_field
    assert (
        "`[r/wallstreetbets]` [NVIDIA customers notified about AI-related price hikes above 15%](https://www.reddit.com/r/wallstreetbets/comments/1vvrh14/nvidia_customers/)"
        in reddit_field
    )
    assert (
        "`[r/options]` [SPX one month premium at zero into NVDA, Jackson Hole, jobs](https://www.reddit.com/r/options/comments/1vvpvj8/spx_one_month_premium/)"
        in reddit_field
    )
    assert (
        "`[r/stocks]` [NVDA raising prices 15% on certain chips per Bloomberg](https://www.reddit.com/r/stocks/comments/1vvodz9/nvda_raising_prices/)"
        in reddit_field
    )
    assert "Fourth post" not in reddit_field

    # 4. 驗證新聞結構化超連結與來源時間戳
    assert "📰 即時市場新聞與權威報導" in fields
    news_field = fields["📰 即時市場新聞與權威報導"]
    assert "`[Bloomberg]`" in news_field
    assert (
        "[NVIDIA AI revenue soars according to quarterly reports](https://finance.yahoo.com/news/nvidia-ai-revenue)"
        in news_field
    )
    assert "25分鐘前" in news_field


@pytest.mark.asyncio
async def test_polymarket_volume_weighted_bullish_probability() -> None:
    """驗證 Polymarket 方案一：成交量加權看多勝率 (VWBP) 計算與方向性標準化。"""
    from cogs.unified_terminal.utils import calculate_polymarket_weighted_odds

    markets = [
        {
            "question": "Will Nvidia hit $150 by September?",
            "tokens": [{"outcome": "Yes", "price": 0.70}],
            "volumeNum": 1_000_000.0,
        },
        {
            "question": "Will Nvidia Blackwell shipments beat targets?",
            "tokens": [{"outcome": "Yes", "price": 0.80}],
            "volumeNum": 500_000.0,
        },
        {
            # 看跌事件: Yes 20% 相當於看多 80% (1.0 - 0.20)
            "question": "Will Nvidia drop below $100 in 2026?",
            "tokens": [{"outcome": "Yes", "price": 0.20}],
            "volumeNum": 500_000.0,
        },
    ]

    # Weighted Bullish = (0.70 * 1M + 0.80 * 0.5M + 0.80 * 0.5M) / 2.0M = 1.5M / 2.0M = 75.0%
    summary = await calculate_polymarket_weighted_odds("NVDA", markets)
    assert "🟢 75.0% 巨鯨看多" in summary
    assert "3檔加權" in summary
    assert "$2.00M" in summary


@pytest.mark.asyncio
async def test_polymarket_volume_weighted_bearish_probability() -> None:
    """驗證 Polymarket 看空行情下的成交量加權勝率與 Emoji 判定。"""
    from cogs.unified_terminal.utils import calculate_polymarket_weighted_odds

    markets = [
        {
            # 看跌事件: Yes 80% 相當於看多 20%
            "question": "Will Tesla drop below $150 in October?",
            "tokens": [{"outcome": "Yes", "price": 0.80}],
            "volumeNum": 1_000_000.0,
        },
        {
            "question": "Will Tesla hit $300 this year?",
            "tokens": [{"outcome": "Yes", "price": 0.20}],
            "volumeNum": 1_000_000.0,
        },
    ]

    # Weighted Bullish = (0.20 * 1M + 0.20 * 1M) / 2.0M = 20.0%
    summary = await calculate_polymarket_weighted_odds("TSLA", markets)
    assert "🔴 20.0% 巨鯨偏空" in summary
    assert "2檔加權" in summary
    assert "$2.00M" in summary
