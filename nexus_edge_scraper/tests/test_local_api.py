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


def test_scrape_sec_fundamental():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html><body><ix:header>Header</ix:header><us-gaap:Revenues>100</us-gaap:Revenues><div>Clean text</div></body></html>"
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={
            "filings": {
                "recent": {
                    "form": ["10-Q", "10-K"],
                    "accessionNumber": ["0001-22", "0002-22"],
                    "primaryDocument": ["doc1.htm", "doc2.htm"],
                }
            }
        }
    )

    with patch("local_api._get_sec_cik", new_callable=AsyncMock) as mock_cik, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_cik.return_value = "0001318605"
        mock_get.return_value = mock_response

        response = client.get("/api/v1/scrape/fundamental/TSLA")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        # The clean logic should remove ix:header and us-gaap and strip to text
        assert "Clean text" in data["data"]["text"]
