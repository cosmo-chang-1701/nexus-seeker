import pytest
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
