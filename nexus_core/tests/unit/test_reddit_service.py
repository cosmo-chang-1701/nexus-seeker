"""Unit tests for ``services.reddit_service`` — Tunnel toggle defense-in-depth.

Covers three defensive gates:
1. Caller passes ``enable_tunnel=False`` → immediate ``None``, no HTTP.
2. Caller uses default (``True``) but DB reports no user has tunnel enabled → ``None``, no HTTP.
3. DB query itself fails → conservative skip, ``None``, no HTTP.
4. Happy-path: tunnel enabled, HTTP call proceeds.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_tunnel_disabled_by_caller_skips_http_call() -> None:
    """Gate 1: When the caller explicitly passes enable_tunnel=False,
    the function must return None immediately without any HTTP call."""

    with patch("services.reddit_service.httpx.AsyncClient") as mock_client_cls:
        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("NVDA", enable_tunnel=False)

        assert result is None, "Should return None when tunnel is explicitly disabled"
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_tunnel_disabled_by_db_global_check_skips_http_call() -> None:
    """Gate 2: When enable_tunnel defaults to True but the DB reports
    all users have tunnel disabled, no HTTP call should be made."""

    with (
        patch("services.reddit_service.httpx.AsyncClient") as mock_client_cls,
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=False,
        ),
    ):
        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("AAPL")

        assert result is None, "Should return None when DB global toggle is off"
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_tunnel_db_query_failure_conservative_skip() -> None:
    """Gate 2 fallback: If the DB query itself raises an exception,
    we conservatively skip (no HTTP call) rather than risk a 530 error."""

    with (
        patch("services.reddit_service.httpx.AsyncClient") as mock_client_cls,
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            side_effect=Exception("DB connection lost"),
        ),
    ):
        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("TSLA")

        assert result is None, "Should return None when DB query fails"
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_tunnel_enabled_makes_http_call() -> None:
    """Happy path: When tunnel is globally enabled and TUNNEL_URL is set,
    the function should actually make an HTTP call to the edge scraper."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": "Reddit sentiment: Bullish on MSFT",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch(
            "services.reddit_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.reddit_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("MSFT", enable_tunnel=True)

        assert result == "Reddit sentiment: Bullish on MSFT"
        mock_client_instance.get.assert_called_once()
        # Verify the URL contains the symbol
        call_url = mock_client_instance.get.call_args[0][0]
        assert "MSFT" in call_url


@pytest.mark.asyncio
async def test_tunnel_disabled_returns_none_not_string() -> None:
    """Regression: Verify the return type is None (not a string message)
    when the tunnel is disabled, matching the updated Optional[str] contract."""

    with patch("services.reddit_service.httpx.AsyncClient"):
        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("AMD", enable_tunnel=False)
        assert result is None
        assert not isinstance(result, str)


@pytest.mark.asyncio
async def test_custom_query_passed_to_edge_scraper() -> None:
    """驗證 Reddit 服務正確傳入 StockAliasMatrix 構建之 custom_query 參數至邊緣端點。"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": "Reddit post for NVDA",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch(
            "services.reddit_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.reddit_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.reddit_service import get_reddit_context

        result = await get_reddit_context("NVDA", enable_tunnel=True)

        assert result == "Reddit post for NVDA"
        call_url = mock_client_instance.get.call_args[0][0]
        assert "custom_query=" in call_url
        assert "NVDA" in call_url


@pytest.mark.asyncio
async def test_batch_skips_http_when_tunnel_globally_disabled() -> None:
    """get_reddit_context_batch should skip the HTTP call entirely and
    return None for every symbol when no user has the tunnel enabled."""

    with (
        patch("services.reddit_service.httpx.AsyncClient") as mock_client_cls,
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=False,
        ),
    ):
        from services.reddit_service import get_reddit_context_batch

        result = await get_reddit_context_batch(["NVDA", "TSLA"])

        assert result == {"NVDA": None, "TSLA": None}
        mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_batch_makes_single_feed_request_and_matches_locally() -> None:
    """Happy path: exactly ONE HTTP call fetches the raw feed, and matching
    against multiple symbols happens locally against that single payload."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "data": [
            {"title": "NVDA earnings beat expectations", "subreddit": "stocks"},
            {
                "title": "Why I'm bullish on Tesla (TSLA) this quarter",
                "subreddit": "wallstreetbets",
            },
            {"title": "Unrelated post about the weather", "subreddit": "stocks"},
        ],
    }
    mock_response.raise_for_status = MagicMock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch(
            "services.reddit_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.reddit_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.reddit_service import get_reddit_context_batch

        result = await get_reddit_context_batch(["NVDA", "TSLA", "ZZZZ_NOMATCH"])

        # Exactly one HTTP request regardless of how many symbols were requested
        mock_client_instance.get.assert_called_once()
        call_url = mock_client_instance.get.call_args[0][0]
        assert "/api/v1/scrape/reddit/feed" in call_url

        assert result["NVDA"] is not None and "NVDA" in result["NVDA"]
        assert result["TSLA"] is not None and "Tesla" in result["TSLA"]
        assert result["ZZZZ_NOMATCH"] == "過去 24 小時內無相關討論。"


@pytest.mark.asyncio
async def test_batch_returns_none_for_all_symbols_on_empty_input() -> None:
    from services.reddit_service import get_reddit_context_batch

    result = await get_reddit_context_batch([])
    assert result == {}
