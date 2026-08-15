from typing import Any
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from local_api import app

client = TestClient(app)


# Mock async context manager for Playwright
class AsyncContextManagerMock:
    async def __aenter__(self) -> Any:
        mock_p = MagicMock()
        mock_browser = AsyncMock()
        # Raise exception inside the try...except block (new_context)
        mock_browser.new_context.side_effect = Exception("Mock context failure")
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)
        return mock_p

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


def test_scrape_reddit_fallback() -> None:
    # Mock playwright to fail at context creation inside try-except
    with patch("local_api.async_playwright", return_value=AsyncContextManagerMock()):
        response = client.get("/api/v1/scrape/reddit/AAPL")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["error", "success"]


def test_scrape_gex_fallback() -> None:
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


def test_scrape_fedwatch_fallback() -> None:
    # Mock requests.get to fail
    with patch("requests.get", side_effect=Exception("Mock requests failure")):
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["probability"] == 0.50
        assert data["data"]["prob_maintain"] == 50.0
        assert data["data"]["prob_cut"] == 50.0
        assert data["data"]["decision"] == "maintain"


def test_scrape_fedwatch_excel_parsing_and_buckets() -> None:
    from datetime import date, timedelta

    meeting_date = date.today() + timedelta(days=15)
    mock_rows = [
        ("date", "meeting_date", "target", "field", "val"),
        (
            "2026-08-15",
            meeting_date,
            "525bps - 550bps",
            "Prob: 500bps - 525bps",
            "15.0",
        ),
        (
            "2026-08-15",
            meeting_date,
            "525bps - 550bps",
            "Prob: 525bps - 550bps",
            "80.0",
        ),
        (
            "2026-08-15",
            meeting_date,
            "525bps - 550bps",
            "Prob: 550bps - 575bps",
            "5.0",
        ),
    ]

    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = mock_rows
    mock_wb = MagicMock()
    mock_wb.__getitem__.return_value = mock_ws

    with patch("requests.get") as mock_req, patch(
        "openpyxl.load_workbook", return_value=mock_wb
    ):
        mock_req.return_value.status_code = 200
        mock_req.return_value.iter_content.return_value = [b"mock"]
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["decision"] == "maintain"
        assert data["data"]["prob_maintain"] == 80.0
        assert data["data"]["prob_cut"] == 15.0
        assert data["data"]["prob_hike"] == 5.0
        assert data["data"]["meeting_date"] == meeting_date.strftime("%m/%d")


def test_scrape_fedwatch_stale_data_fallback() -> None:
    from datetime import date, timedelta

    # Meeting date from 100 days ago (stale)
    old_meeting_date = date.today() - timedelta(days=100)
    mock_rows = [
        ("date", "meeting_date", "target", "field", "val"),
        (
            "2026-01-01",
            old_meeting_date,
            "525bps - 550bps",
            "Prob: hike",
            "100.0",
        ),
    ]

    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = mock_rows
    mock_wb = MagicMock()
    mock_wb.__getitem__.return_value = mock_ws

    with patch("requests.get") as mock_req, patch(
        "openpyxl.load_workbook", return_value=mock_wb
    ):
        mock_req.return_value.status_code = 200
        mock_req.return_value.iter_content.return_value = [b"mock"]
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        # Stale data should trigger fallback instead of reporting 100% hike
        assert data["status"] == "success"
        assert data["data"]["probability"] == 0.50
        assert data["data"]["decision"] == "maintain"


def test_scrape_sec_fundamental() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "<html><body><ix:header>Header</ix:header><us-gaap:Revenues>100</us-gaap:Revenues><div>Clean text. Revenue grew by 10% year-over-year.</div></body></html>"
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
        # Verify sections are extracted and returned
        assert "sections" in data["data"]
        assert "quarterly_financials" in data["data"]["sections"]
        assert "Revenue grew" in data["data"]["sections"]["quarterly_financials"]
