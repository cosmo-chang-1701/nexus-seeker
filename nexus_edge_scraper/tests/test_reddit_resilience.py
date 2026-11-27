"""測試 Reddit RSS 多層降級、局部關鍵字過濾與結構化錯誤碼。"""

from typing import Any
from unittest.mock import patch
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

import local_api
from local_api import app
from local_api.reddit import (
    _DEFAULT_REDDIT_USER_AGENT,
    _TOTAL_FETCH_BUDGET_SEC,
    _entry_matches_query,
    _fetch_reddit_rss,
    _parse_atom_entries,
)

client = TestClient(app)


def _atom_feed(entries: list[tuple[str, str, str, str]]) -> str:
    """建立測試用 Reddit Atom XML 字串（title, subreddit, url, published）。"""
    items: list[str] = []
    for title, sub, url, published in entries:
        items.append(
            f"""
        <entry>
            <title>{title}</title>
            <category term="{sub}" label="r/{sub}"/>
            <link href="{url}"/>
            <published>{published}</published>
        </entry>"""
        )
    return (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(items)
        + "</feed>"
    )


async def _noop_sleep(_delay: float) -> None:
    return None


def test_default_user_agent_has_no_placeholder_or_mozilla() -> None:
    assert "CHANGE_ME" not in _DEFAULT_REDDIT_USER_AGENT
    assert "Mozilla" not in _DEFAULT_REDDIT_USER_AGENT
    assert "CHANGE_ME" not in local_api.REDDIT_USER_AGENT
    assert "Mozilla" not in local_api.REDDIT_USER_AGENT


def test_parse_atom_entries_parses_and_handles_edge_cases() -> None:
    xml_text = _atom_feed(
        [
            (
                "NVDA breaks out",
                "wallstreetbets",
                "https://reddit.com/r/wallstreetbets/1",
                "2026-10-01T12:00:00+00:00",
            ),
            (
                "TSLA delivery report",
                "stocks",
                "https://reddit.com/r/stocks/2",
                "2026-10-01T11:00:00+00:00",
            ),
        ]
    )
    parsed = _parse_atom_entries(xml_text, limit=1)
    assert len(parsed) == 1
    assert parsed[0] == {
        "title": "NVDA breaks out",
        "subreddit": "wallstreetbets",
        "published": "2026-10-01T12:00:00+00:00",
        "url": "https://reddit.com/r/wallstreetbets/1",
    }
    assert _parse_atom_entries(xml_text, limit=0) == []

    with pytest.raises(ET.ParseError):
        _parse_atom_entries("<html><body>unclosed", limit=5)

    with pytest.raises(ET.ParseError):
        _parse_atom_entries("<html><body>unclosed", limit=0)

    with pytest.raises(ET.ParseError):
        _parse_atom_entries("<html><body>Not Atom XML</body></html>", limit=5)


def test_entry_matches_query_word_boundaries() -> None:
    assert _entry_matches_query("NVDA to the moon", "NVDA", "", "") is True
    assert _entry_matches_query("$nvda calls printing", "NVDA", "", "") is True
    assert _entry_matches_query("NVDAA is unrelated", "NVDA", "", "") is False
    assert (
        _entry_matches_query(
            "Jensen Huang announces Blackwell",
            "NVDA",
            "",
            '"NVDA" OR "$NVDA" OR "NVIDIA" OR "Jensen Huang"',
        )
        is True
    )
    assert (
        _entry_matches_query("Nvidia surges on AI demand", "NVDA", "Nvidia", "") is True
    )


def test_scrape_reddit_multireddit_404_falls_back_to_global_search() -> None:
    local_api._reddit_cache.clear()
    called_urls: list[str] = []
    global_xml = _atom_feed(
        [
            (
                "NVDA Blackwell demand insane",
                "stocks",
                "https://www.reddit.com/r/stocks/comments/1/nvda/",
                "2026-10-01T12:00:00+00:00",
            )
        ]
    )

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        called_urls.append(url)
        req = httpx.Request("GET", url)
        if "r/wallstreetbets+stocks+options/search.rss" in url:
            return httpx.Response(404, request=req)
        if url.startswith("https://www.reddit.com/search.rss"):
            return httpx.Response(200, text=global_xml, request=req)
        raise AssertionError(f"非預期呼叫 URL: {url}")

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
    ):
        resp = client.get("/api/v1/scrape/reddit/NVDA")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "[stocks] NVDA Blackwell demand insane" in body["data"]
    assert len(body["posts"]) == 1
    assert len(called_urls) == 2


def test_scrape_reddit_both_search_fail_falls_back_to_feed_local_filter() -> None:
    local_api._reddit_cache.clear()
    called_urls: list[str] = []
    feed_xml = _atom_feed(
        [
            (
                "NVDAA typo ticker should not match",
                "wallstreetbets",
                "https://reddit.com/r/wallstreetbets/1",
                "2026-10-01T13:00:00+00:00",
            ),
            (
                "$NVDA guidance beat expectations",
                "stocks",
                "https://reddit.com/r/stocks/2",
                "2026-10-01T12:00:00+00:00",
            ),
            (
                "AAPL iPhone sales update",
                "options",
                "https://reddit.com/r/options/3",
                "2026-10-01T11:00:00+00:00",
            ),
        ]
    )

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        called_urls.append(url)
        req = httpx.Request("GET", url)
        if "search.rss" in url:
            return httpx.Response(404 if "r/" in url else 403, request=req)
        if "r/wallstreetbets+stocks+options/new.rss" in url:
            return httpx.Response(200, text=feed_xml, request=req)
        raise AssertionError(f"非預期呼叫 URL: {url}")

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
    ):
        resp = client.get("/api/v1/scrape/reddit/NVDA")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert len(body["posts"]) == 1
    assert body["posts"][0]["title"] == "$NVDA guidance beat expectations"
    assert "NVDAA" not in body["data"]
    assert "AAPL" not in body["data"]
    assert len(called_urls) == 3

    # 後續另一個沒有出現過的標的應直接重用剛快取的 feed，且零匹配時回傳標準空訊息
    called_urls.clear()
    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
    ):
        resp_miss = client.get("/api/v1/scrape/reddit/MSFT")

    assert resp_miss.status_code == 200
    miss_body = resp_miss.json()
    assert miss_body["status"] == "success"
    assert miss_body["data"] == "過去 24 小時內無相關討論。"
    assert miss_body["posts"] == []
    assert len(called_urls) == 2  # 兩次 search.rss 失敗後直接命中快取的 feed


def test_scrape_reddit_feed_falls_back_to_per_subreddit_and_deduplicates() -> None:
    local_api._reddit_cache.clear()
    called_urls: list[str] = []

    wsb_xml = _atom_feed(
        [
            (
                "NVDA gamma squeeze",
                "wallstreetbets",
                "https://reddit.com/r/wallstreetbets/10",
                "2026-10-01T14:00:00+00:00",
            ),
            (
                "Crosspost market wrap",
                "wallstreetbets",
                "https://reddit.com/r/shared/99",
                "2026-10-01T12:00:00+00:00",
            ),
        ]
    )
    stocks_xml = _atom_feed(
        [
            (
                "Crosspost market wrap",
                "stocks",
                "https://reddit.com/r/stocks/20",
                "2026-10-01T12:00:00+00:00",
            ),
            (
                "Fed minutes takeaway",
                "stocks",
                "https://reddit.com/r/stocks/21",
                "2026-10-01T13:00:00+00:00",
            ),
        ]
    )

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        called_urls.append(url)
        req = httpx.Request("GET", url)
        if "r/wallstreetbets+stocks+options/new.rss" in url:
            raise httpx.ReadTimeout("multireddit timeout", request=req)
        if "r/wallstreetbets/new.rss" in url:
            return httpx.Response(200, text=wsb_xml, request=req)
        if "r/stocks/new.rss" in url:
            return httpx.Response(200, text=stocks_xml, request=req)
        if "r/options/new.rss" in url:
            return httpx.Response(404, request=req)
        raise AssertionError(f"非預期呼叫 URL: {url}")

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
    ):
        resp = client.get("/api/v1/scrape/reddit/feed?limit=50")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    titles = [p["title"] for p in body["data"]]
    # 至少一個版塊成功即成功，且重複標題只保留一次並依時間降冪排序
    assert titles == [
        "NVDA gamma squeeze",
        "Fed minutes takeaway",
        "Crosspost market wrap",
    ]
    assert len(called_urls) == 4


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        (429, "rate_limited"),
        (404, "upstream_404"),
        (403, "upstream_403"),
        ("timeout", "timeout"),
        (503, "upstream_error"),
        ("bad_xml", "upstream_error"),
    ],
)
def test_scrape_reddit_total_failure_returns_structured_error_code(
    outcome: int | str, expected_code: str
) -> None:
    local_api._reddit_cache.clear()

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        req = httpx.Request("GET", url)
        if outcome == "timeout":
            raise httpx.ReadTimeout("read timed out", request=req)
        if outcome == "bad_xml":
            return httpx.Response(200, text="<html>broken xml", request=req)
        assert isinstance(outcome, int)
        return httpx.Response(outcome, request=req)

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
    ):
        resp_symbol = client.get("/api/v1/scrape/reddit/NVDA")
        resp_feed = client.get("/api/v1/scrape/reddit/feed?limit=20")

    for resp in (resp_symbol, resp_feed):
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["error_code"] == expected_code
        assert body["data"].startswith("本地端執行例外:")


def test_scrape_reddit_stops_within_deadline_budget() -> None:
    local_api._reddit_cache.clear()
    called_urls: list[str] = []
    clock_ticks = iter([0.0, 35.0, 35.0, 35.0])

    async def fake_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        called_urls.append(url)
        req = httpx.Request("GET", url)
        raise httpx.ReadTimeout("slow upstream", request=req)

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("asyncio.sleep", new=_noop_sleep),
        patch(
            "local_api.reddit._now_monotonic",
            side_effect=lambda: next(clock_ticks, 35.0),
        ),
    ):
        resp = client.get("/api/v1/scrape/reddit/NVDA")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "timeout"
    # 主請求超時後因牆鐘預算已耗盡，不再發出後續備援網路請求
    assert len(called_urls) == 1


@pytest.mark.asyncio
async def test_429_backoff_aborts_within_deadline_budget() -> None:
    local_api._reddit_cache.clear()
    sim_now = [0.0]
    direct_calls = 0

    async def advancing_sleep(delay: float) -> None:
        sim_now[0] += max(delay, 0.0)

    async def fake_direct_get(
        self: Any, url: str, headers: Any = None
    ) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        return httpx.Response(429, request=httpx.Request("GET", url))

    with (
        patch("local_api.reddit._now_monotonic", side_effect=lambda: sim_now[0]),
        patch("asyncio.sleep", side_effect=advancing_sleep),
        patch("httpx.AsyncClient.get", new=fake_direct_get),
    ):
        async with httpx.AsyncClient() as http_client:
            resp = await _fetch_reddit_rss(
                http_client,
                "https://www.reddit.com/r/stocks/search.rss?q=NVDA",
                max_retries=3,
                deadline=12.0,
            )

    assert resp.status_code == 429
    # 第 1 次嘗試 (0.3-1.0s) + 第 1 次 429 退避 (6-8s) 後執行第 2 次嘗試，
    # 接著第 2 次 429 退避 (11-13s) 會超過 deadline=12.0，應立即中止重試。
    assert direct_calls == 2
    assert sim_now[0] < 12.0

    # 端對端測試：在持續 429（0ms RTT、無 Retry-After）情境下，scrape_reddit 總耗時必須嚴格小於 35s
    local_api._reddit_cache.clear()
    sim_now[0] = 0.0
    e2e_calls = 0

    async def fake_e2e_get(self: Any, url: str, headers: Any = None) -> httpx.Response:
        nonlocal e2e_calls
        e2e_calls += 1
        return httpx.Response(429, request=httpx.Request("GET", url))

    with (
        patch("local_api.reddit._now_monotonic", side_effect=lambda: sim_now[0]),
        patch("asyncio.sleep", side_effect=advancing_sleep),
        patch("httpx.AsyncClient.get", new=fake_e2e_get),
    ):
        api_resp = client.get("/api/v1/scrape/reddit/NVDA")

    assert api_resp.status_code == 200
    body = api_resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "rate_limited"
    assert e2e_calls >= 3
    assert sim_now[0] <= _TOTAL_FETCH_BUDGET_SEC
    assert sim_now[0] < 35.0
