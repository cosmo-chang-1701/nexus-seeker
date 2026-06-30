from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from local_api import app

client = TestClient(app)


# Mock async context manager for Playwright
class AsyncContextManagerMock:
    async def __aenter__(self):
        mock_p = MagicMock()
        mock_browser = AsyncMock()
        # Raise exception inside the try...except block (new_context)
        mock_browser.new_context.side_effect = Exception("Mock context failure")
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)
        return mock_p

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def test_scrape_reddit_fallback():
    # Mock playwright to fail at context creation inside try-except
    with patch("local_api.async_playwright", return_value=AsyncContextManagerMock()):
        response = client.get("/api/v1/scrape/reddit/AAPL")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "例外" in data["data"] or "exception" in data["data"].lower()


def test_scrape_gex_fallback():
    # Mock playwright to fail at context creation inside try-except
    with patch("local_api.async_playwright", return_value=AsyncContextManagerMock()):
        response = client.get("/api/v1/scrape/macro/gex")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        # Verify fallback values
        assert data["data"]["spy_spot"] == 510.0
        assert data["data"]["gamma_flip"] == 515.0
        assert data["data"]["put_wall"] == 505.0


def test_scrape_fedwatch_fallback():
    # Mock requests.get to fail
    with patch("requests.get", side_effect=Exception("Mock requests failure")):
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["probability"] == 0.72
        assert data["data"]["decision"] == "maintain"
