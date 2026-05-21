import pytest
from unittest.mock import patch
from services.polymarket_service import PolymarketService


class MockBot:
    def __init__(self):
        self.queued_dms = []

    async def queue_dm(self, user_id, embed):
        self.queued_dms.append((user_id, embed))


@pytest.fixture
def poly_service():
    bot = MockBot()
    return PolymarketService(bot)


def test_is_relevant_market_whitelist(poly_service):
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


def test_is_relevant_market_blacklist(poly_service):
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


def test_is_relevant_market_symbol_detection(poly_service):
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


def test_is_relevant_market_mixed(poly_service):
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
async def test_push_notification_uses_embed_builder(poly_service):
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
