from typing import Any
from unittest.mock import patch, MagicMock, AsyncMock
import httpx
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


def test_scrape_reddit_retries_on_429_then_caches_result() -> None:
    import local_api

    local_api._reddit_cache.clear()

    call_count = {"n": 0}
    xml_text = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        call_count["n"] += 1
        req = httpx.Request("GET", url)
        if call_count["n"] == 1:
            return httpx.Response(429, request=req)
        return httpx.Response(200, text=xml_text, request=req)

    async def fake_sleep(_delay: float) -> None:
        return None

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=fake_sleep),
    ):
        response = client.get("/api/v1/scrape/reddit/TSLA")
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert call_count["n"] == 2  # first call hit 429, retry succeeded

        # Second request for the same symbol/query should be served from cache
        response2 = client.get("/api/v1/scrape/reddit/TSLA")
        assert response2.status_code == 200
        assert call_count["n"] == 2  # no additional network call


def test_scrape_reddit_uses_configured_user_agent() -> None:
    import local_api

    local_api._reddit_cache.clear()

    captured_headers: dict[str, Any] = {}
    xml_text = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        captured_headers.update(headers or {})
        req = httpx.Request("GET", url)
        return httpx.Response(200, text=xml_text, request=req)

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.get("/api/v1/scrape/reddit/AAPL")
        assert response.status_code == 200
        assert captured_headers.get("User-Agent") == local_api.REDDIT_USER_AGENT
        # Must not be a generic browser UA string
        assert "Mozilla" not in captured_headers.get("User-Agent", "")


def test_scrape_reddit_feed_returns_structured_posts() -> None:
    import local_api

    local_api._reddit_cache.clear()

    xml_text = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>NVDA to the moon</title>
            <category term="wallstreetbets" label="r/wallstreetbets"/>
            <published>2026-08-18T12:00:00+00:00</published>
        </entry>
        <entry>
            <title>Thoughts on TSLA earnings</title>
            <category term="stocks" label="r/stocks"/>
            <published>2026-08-18T11:00:00+00:00</published>
        </entry>
    </feed>"""

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        req = httpx.Request("GET", url)
        return httpx.Response(200, text=xml_text, request=req)

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.get("/api/v1/scrape/reddit/feed?limit=100")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        posts = data["data"]
        assert len(posts) == 2
        assert posts[0]["title"] == "NVDA to the moon"
        assert posts[0]["subreddit"] == "wallstreetbets"
        assert posts[1]["title"] == "Thoughts on TSLA earnings"


def test_scrape_reddit_feed_retries_on_429_then_caches_result() -> None:
    import local_api

    local_api._reddit_cache.clear()

    call_count = {"n": 0}
    xml_text = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        call_count["n"] += 1
        req = httpx.Request("GET", url)
        if call_count["n"] == 1:
            return httpx.Response(429, request=req)
        return httpx.Response(200, text=xml_text, request=req)

    async def fake_sleep(_delay: float) -> None:
        return None

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=fake_sleep),
    ):
        response = client.get("/api/v1/scrape/reddit/feed?limit=50")
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert call_count["n"] == 2

        response2 = client.get("/api/v1/scrape/reddit/feed?limit=50")
        assert response2.status_code == 200
        assert call_count["n"] == 2  # served from cache, no additional network call


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


def test_scrape_fedwatch_realtime_zq_calculation() -> None:
    import pandas as pd

    mock_df_meeting = pd.DataFrame({"Close": [96.33]}, index=[pd.Timestamp.now()])
    mock_df_prior = pd.DataFrame({"Close": [96.3675]}, index=[pd.Timestamp.now()])
    mock_df_irx = pd.DataFrame({"Close": [3.70]}, index=[pd.Timestamp.now()])

    def mock_ticker(sym: str) -> MagicMock:
        t = MagicMock()
        if sym == "^IRX":
            t.history.return_value = mock_df_irx
        elif "ZQQ" in sym:
            t.history.return_value = mock_df_prior
        else:
            t.history.return_value = mock_df_meeting
        return t

    with patch("yfinance.Ticker", side_effect=mock_ticker):
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        res_data = data["data"]
        assert res_data["source"] == "CME 30-Day Fed Funds Futures (ZQ)"
        assert res_data["prob_maintain"] == 67.9
        assert res_data["prob_hike"] == 32.1
        assert res_data["prob_cut"] == 0.0
        assert res_data["decision"] == "maintain"
        assert (
            round(
                res_data["prob_maintain"]
                + res_data["prob_hike"]
                + res_data["prob_cut"],
                1,
            )
            == 100.0
        )


def test_scrape_fedwatch_fallback() -> None:
    # Mock yfinance and requests.get to fail
    with (
        patch("yfinance.Ticker", side_effect=Exception("Mock yfinance failure")),
        patch("requests.get", side_effect=Exception("Mock requests failure")),
    ):
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["probability"] == 0.50
        assert data["data"]["prob_maintain"] == 50.0
        assert data["data"]["prob_cut"] == 50.0
        assert data["data"]["decision"] == "maintain"
        assert data["data"]["source"] == "fallback"


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

    with (
        patch("yfinance.Ticker", side_effect=Exception("Mock yfinance failure")),
        patch("requests.get") as mock_req,
        patch("openpyxl.load_workbook", return_value=mock_wb),
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
        assert data["data"]["source"] == "Atlanta Fed Market Probability Tracker (MPT)"


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

    with (
        patch("yfinance.Ticker", side_effect=Exception("Mock yfinance failure")),
        patch("requests.get") as mock_req,
        patch("openpyxl.load_workbook", return_value=mock_wb),
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


def test_scrape_fedwatch_direct_summary_and_buckets_no_double_count() -> None:
    from datetime import date, timedelta

    meeting_date = date.today() + timedelta(days=30)
    mock_rows = [
        ("date", "meeting_date", "target", "field", "val"),
        (
            "2026-08-13",
            meeting_date,
            "350bps - 375bps",
            "Rate: 25th percentile",
            "371.90",
        ),
        ("2026-08-13", meeting_date, "350bps - 375bps", "Rate: mean", "378.52"),
        ("2026-08-13", meeting_date, "350bps - 375bps", "Prob: cut", "1.36"),
        ("2026-08-13", meeting_date, "350bps - 375bps", "Prob: hike", "59.06"),
        (
            "2026-08-13",
            meeting_date,
            "350bps - 375bps",
            "Prob: 350bps - 375bps",
            "40.42",
        ),
        (
            "2026-08-13",
            meeting_date,
            "350bps - 375bps",
            "Prob: 375bps - 400bps",
            "54.67",
        ),
        (
            "2026-08-13",
            meeting_date,
            "350bps - 375bps",
            "Prob: 400bps - 425bps",
            "4.91",
        ),
    ]

    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = mock_rows
    mock_wb = MagicMock()
    mock_wb.__getitem__.return_value = mock_ws

    with (
        patch("yfinance.Ticker", side_effect=Exception("Mock yfinance failure")),
        patch("requests.get") as mock_req,
        patch("openpyxl.load_workbook", return_value=mock_wb),
    ):
        mock_req.return_value.status_code = 200
        mock_req.return_value.iter_content.return_value = [b"mock"]
        response = client.get("/api/v1/scrape/macro/fedwatch")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        res_data = data["data"]
        assert res_data["meeting_date"] == meeting_date.strftime("%m/%d")
        assert res_data["current_target"] == "3.50%-3.75%"
        assert res_data["decision"] == "hike"
        # 1.36 / 100.84 -> 1.3%, 59.06 / 100.84 -> 58.6%, 40.1% maintain
        assert res_data["prob_cut"] == 1.3
        assert res_data["prob_hike"] == 58.6
        assert res_data["prob_maintain"] == 40.1
        assert (
            round(
                res_data["prob_cut"]
                + res_data["prob_hike"]
                + res_data["prob_maintain"],
                1,
            )
            == 100.0
        )
        assert res_data["source"] == "Atlanta Fed Market Probability Tracker (MPT)"


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

    with (
        patch("local_api._get_sec_cik", new_callable=AsyncMock) as mock_cik,
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
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
