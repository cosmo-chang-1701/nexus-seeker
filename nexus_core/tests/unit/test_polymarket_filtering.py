from typing import Any
import pytest
from unittest.mock import AsyncMock, patch

from services.polymarket_service import PolymarketService


class MockBot:
    def __init__(self) -> None:
        self.queued_dms = []  # type: ignore

    async def queue_dm(self, user_id: Any, embed: Any) -> None:
        self.queued_dms.append((user_id, embed))


@pytest.fixture
def poly_service() -> Any:
    bot = MockBot()
    return PolymarketService(bot)


def test_is_relevant_market_whitelist(poly_service: Any) -> Any:
    # Test items in whitelist
    market_info = {
        "question": "Will the FED raise interest rates in June?",
        "description": "This market resolves to Yes if the Federal Reserve increases the target range for the federal funds rate.",
    }
    assert poly_service._is_relevant_market(market_info) is True

    market_info = {
        "question": "Bitcoin price at the end of 2024?",
        "description": "Resolution based on Coindesk BPI.",
    }
    assert poly_service._is_relevant_market(market_info) is True


def test_is_relevant_market_blacklist(poly_service: Any):  # type: ignore
    # Test items in blacklist
    market_info = {
        "question": "Who will win the NBA Finals?",
        "description": "Resolution based on official NBA results.",
    }
    assert poly_service._is_relevant_market(market_info) is False

    market_info = {
        "question": "Will 'Movie Name' win the Oscar for Best Picture?",
        "description": "Resolution based on Academy Awards.",
    }
    assert poly_service._is_relevant_market(market_info) is False


def test_is_relevant_market_symbol_detection(poly_service: Any):  # type: ignore
    # Test symbol detection
    market_info = {
        "question": "Will NVDA reach $1000 before July?",
        "description": "Resolution based on Yahoo Finance.",
    }
    assert poly_service._is_relevant_market(market_info) is True

    # Test common non-stock caps
    market_info = {
        "question": "Will the USA win the most medals?",
        "description": "Olympic medals.",
    }
    assert poly_service._is_relevant_market(market_info) is False


def test_is_relevant_market_mixed(poly_service: Any):  # type: ignore
    # Mix of keywords
    market_info = {
        "question": "NVIDIA stock vs Apple stock in 2024",
        "description": "Tech giants comparison.",
    }
    assert poly_service._is_relevant_market(market_info) is True

    market_info = {
        "question": "NBA vs NFL viewership during Election night",
        "description": "Comparing sports and politics.",
    }
    # Now that we prioritize blacklist, hitting NBA/NFL should return False
    # even though "Election" is in allow_keywords.
    assert poly_service._is_relevant_market(market_info) is False


@pytest.mark.asyncio
async def test_push_notification_uses_embed_builder(poly_service: Any):  # type: ignore
    embed = object()
    market_info = {
        "question": "Will NVDA beat earnings?",
        "event_slug": "nvda-earnings",
    }
    trade = {"side": "BUY", "price": 0.74}
    uoa_correlation = {
        "uoa": {
            "symbol": "NVDA",
            "expiry": "2026-06-19",
            "strike": 150,
            "type": "CALL",
        },
        "classification": {
            "classification": "方向性押注",
            "confidence": 0.88,
            "explanation": "同步觀察到買權放量。",
        },
    }

    with patch(
        "services.polymarket_service.create_polymarket_whale_alert_embed",
        return_value=embed,
    ) as mock_create:
        await poly_service._push_notification(
            123,
            "市場預期財報後仍有延續動能。",
            market_info,
            trade,
            65000.0,
            10000.0,
            uoa_correlation,
        )

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["intent_label"] == "強力看多"
    assert kwargs["win_rate"] == 74.0
    assert kwargs["is_high_conviction"] is True
    assert kwargs["event_slug"] == "nvda-earnings"
    assert poly_service.bot.queued_dms == [(123, embed)]


def test_is_relevant_market_unblocked_stocks(poly_service: Any) -> None:
    """驗證修復後的黑名單不再誤殺正牌美股、科技產品發布會或財報。"""
    netflix_market = {
        "question": "Will Netflix (NFLX) subscriber count grow by 5M in Q3?",
        "description": "Resolution based on Netflix quarterly earnings report.",
    }
    assert poly_service._is_relevant_market(netflix_market) is True

    apple_release_market = {
        "question": "Will Apple release M4 MacBook Pro at WWDC?",
        "description": "Official Apple hardware announcement.",
    }
    assert poly_service._is_relevant_market(apple_release_market) is True

    sony_market = {
        "question": "Sony gaming division revenue in 2026",
        "description": "PlayStation and gaming hardware financials.",
    }
    assert poly_service._is_relevant_market(sony_market) is True

    tesla_fsd_market = {
        "question": "Will Tesla release unsupervised FSD before end of year?",
        "description": "Full self driving robotaxi milestone.",
    }
    assert poly_service._is_relevant_market(tesla_fsd_market) is True


def test_is_relevant_market_expanded_whitelist(poly_service: Any) -> None:
    """驗證擴充後的白名單支援宏觀總經利率、聯準會決策與美股龍頭。"""
    test_cases = [
        {"question": "Will FOMC announce a 50bps rate cut in September?"},
        {"question": "Core PCE Inflation rate below 2.5% in August 2026?"},
        {"question": "Will Jerome Powell resign as Fed Chair before 2027?"},
        {"question": "Palantir (PLTR) total AIP customer count above 1000?"},
        {"question": "Broadcom (AVGO) Q3 AI revenue above $15B?"},
        {"question": "MicroStrategy (MSTR) Bitcoin holdings exceed 500k BTC?"},
        {"question": "Super Micro Computer (SMCI) gross margin expansion?"},
        {"question": "Eli Lilly (LLY) Mounjaro global sales reach $10B?"},
    ]
    for case in test_cases:
        assert (
            poly_service._is_relevant_market(case) is True
        ), f"Failed on: {case['question']}"


@pytest.mark.asyncio
async def test_format_gamma_market(poly_service: Any) -> None:
    """驗證 Gamma API 市場資料格式化結構。"""
    raw_gamma_market = {
        "id": "12345",
        "question": "Will NVIDIA beat quarterly earnings?",
        "description": "Non-GAAP EPS resolution.",
        "endDate": "2026-08-26T21:00:00Z",
        "clobTokenIds": '["tok_yes_1", "tok_no_2"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.95", "0.05"]',
        "volumeNum": 54200.0,
        "slug": "nvda-beat-earnings-2026",
        "closed": False,
        "active": True,
    }
    raw_event = {"slug": "nvda-earnings-event", "title": "NVDA Earnings"}

    formatted = poly_service._format_gamma_market(raw_gamma_market, raw_event)
    assert formatted["question"] == "Will NVIDIA beat quarterly earnings?"
    assert formatted["event_slug"] == "nvda-earnings-event"
    assert formatted["volumeNum"] == 54200.0
    assert len(formatted["tokens"]) == 2
    assert formatted["tokens"][0]["outcome"] == "Yes"
    assert formatted["tokens"][0]["price"] == "0.95"


@pytest.mark.asyncio
async def test_search_markets_and_get_symbol_markets(poly_service: Any) -> None:
    """驗證 search_markets 與 get_symbol_markets 在線檢索與排序。"""
    mock_search_response = {
        "events": [
            {
                "title": "NVIDIA Stock Target",
                "slug": "nvda-target-event",
                "markets": [
                    {
                        "question": "Will NVIDIA (NVDA) close above $180?",
                        "description": "Yahoo Finance close.",
                        "outcomes": ["Yes", "No"],
                        "outcomePrices": [0.98, 0.02],
                        "volumeNum": 120000.0,
                        "slug": "nvda-180",
                        "closed": False,
                        "active": True,
                    }
                ],
            }
        ]
    }

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: mock_search_response

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        # 1. 測試 search_markets
        res = await poly_service.search_markets("NVDA", limit=5)
        assert len(res) >= 1
        assert "NVIDIA" in res[0]["question"] or "NVDA" in res[0]["question"]

        # 2. 測試 get_symbol_markets (多別名自動補齊)
        sym_res = await poly_service.get_symbol_markets("NVDA", limit=3)
        assert len(sym_res) >= 1
        assert sym_res[0]["slug"] == "nvda-180"


@pytest.mark.asyncio
async def test_get_symbol_markets_dispatches_search_terms_concurrently(
    poly_service: Any,
) -> None:
    """驗證 get_symbol_markets 對多個別名搜尋詞是透過 asyncio.gather 併發派發，
    而非逐一序列 await —— 序列版本會在請求尾端疊加多次網路往返延遲。"""
    call_order: list[str] = []

    async def _fake_search_markets(
        term: str, limit: int = 5, active_only: bool = True
    ) -> list:
        call_order.append(f"start:{term}")
        # 若是序列 await，第二個詞要等第一個詞完全 return 才會 start；
        # 併發版本則應該在第一個詞 return 前就看到後續詞的 start。
        await __import__("asyncio").sleep(0)
        call_order.append(f"end:{term}")
        return []

    with patch.object(
        poly_service, "search_markets", side_effect=_fake_search_markets
    ), patch(
        "market_analysis.stock_alias_matrix.StockAliasMatrix.get_aliases_for_symbol",
        new_callable=AsyncMock,
        return_value=["NVIDIA"],
    ):
        await poly_service.get_symbol_markets("NVDA", limit=3)

    # 併發派發：所有 start 事件應在任一 end 事件之前發生
    starts = [i for i, ev in enumerate(call_order) if ev.startswith("start:")]
    ends = [i for i, ev in enumerate(call_order) if ev.startswith("end:")]
    assert max(starts) < min(
        ends
    ), f"Expected concurrent dispatch (all starts before any end), got: {call_order}"


@pytest.mark.asyncio
async def test_get_symbol_markets_isolates_per_term_failure(poly_service: Any) -> None:
    """驗證單一搜尋詞失敗不會拖垮整個 get_symbol_markets 呼叫。"""

    async def _fake_search_markets(
        term: str, limit: int = 5, active_only: bool = True
    ) -> list:
        if term == "NVDA":
            raise RuntimeError("simulated network failure")
        return [
            {
                "question": "Will NVIDIA beat earnings?",
                "description": "",
                "slug": "nvda-earnings",
                "volumeNum": 1000.0,
            }
        ]

    with patch.object(
        poly_service, "search_markets", side_effect=_fake_search_markets
    ), patch(
        "market_analysis.stock_alias_matrix.StockAliasMatrix.get_aliases_for_symbol",
        new_callable=AsyncMock,
        return_value=["NVIDIA"],
    ):
        result = await poly_service.get_symbol_markets("NVDA", limit=3)

    assert len(result) == 1
    assert result[0]["slug"] == "nvda-earnings"
