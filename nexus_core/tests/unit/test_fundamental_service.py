"""Unit tests for services.fundamental_service — verifies form_type/sections
are correctly threaded through (not silently dropped) across the success,
heartbeat-cache-hit, and exception-fallback-to-cache code paths, including
graceful degradation when reading legacy cache rows written before this
migration (missing the form_type/sections keys entirely)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_httpx_client(get_return_value: MagicMock) -> AsyncMock:
    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=get_return_value)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)
    return mock_client_instance


@pytest.mark.asyncio
async def test_get_fundamental_context_success_includes_form_type_and_sections() -> (
    None
):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "status": "success",
        "data": {
            "text": "SEC 財報段落",
            "source_url": "https://sec.gov/doc",
            "accession_number": "0001-22",
            "form_type": "10-Q",
            "sections": {"quarterly_financials": "Revenue $1B"},
        },
    }
    mock_client_instance = _mock_httpx_client(mock_response)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock) as mock_save_kv,
        patch(
            "services.fundamental_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.fundamental_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.fundamental_service import get_fundamental_context

        result = await get_fundamental_context("TSLA")

    assert result is not None
    assert result["form_type"] == "10-Q"
    assert result["sections"] == {"quarterly_financials": "Revenue $1B"}
    mock_save_kv.assert_called_once()
    cached_payload = mock_save_kv.call_args[0][1]
    assert cached_payload["form_type"] == "10-Q"
    assert cached_payload["sections"] == {"quarterly_financials": "Revenue $1B"}


@pytest.mark.asyncio
async def test_get_fundamental_context_heartbeat_cache_hit_includes_form_type_and_sections() -> (
    None
):
    cached_data = {
        "text": "SEC 財報段落",
        "source_url": "https://sec.gov/doc",
        "accession_number": "0001-22",
        "form_type": "8-K",
        "sections": {"key_events": "[Item 5.02] CFO resigned"},
    }

    mock_meta_response = MagicMock()
    mock_meta_response.status_code = 200
    mock_meta_response.json.return_value = {
        "status": "success",
        "data": {"accession_number": "0001-22"},
    }
    mock_client_instance = _mock_httpx_client(mock_meta_response)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch("database.cache.get_kv_cache", return_value=cached_data),
        patch(
            "services.fundamental_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.fundamental_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.fundamental_service import get_fundamental_context

        result = await get_fundamental_context("TSLA")

    assert result is not None
    assert result["form_type"] == "8-K"
    assert result["sections"] == {"key_events": "[Item 5.02] CFO resigned"}
    # Only the lightweight heartbeat call should have happened, not the full fetch
    mock_client_instance.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_fundamental_context_legacy_cache_missing_keys_defaults_gracefully() -> (
    None
):
    """Pre-migration cache rows have no form_type/sections keys at all —
    both the heartbeat-hit and exception-fallback paths must degrade to
    ""/{} rather than raising KeyError."""
    legacy_cached_data = {
        "text": "SEC 財報段落",
        "source_url": "https://sec.gov/doc",
        "accession_number": "0001-22",
    }

    # --- heartbeat-hit path ---
    mock_meta_response = MagicMock()
    mock_meta_response.status_code = 200
    mock_meta_response.json.return_value = {
        "status": "success",
        "data": {"accession_number": "0001-22"},
    }
    mock_client_instance = _mock_httpx_client(mock_meta_response)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch("database.cache.get_kv_cache", return_value=legacy_cached_data),
        patch(
            "services.fundamental_service.httpx.AsyncClient",
            return_value=mock_client_instance,
        ),
        patch("services.fundamental_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.fundamental_service import get_fundamental_context

        result = await get_fundamental_context("TSLA")

    assert result is not None
    assert result["form_type"] == ""
    assert result["sections"] == {}

    # --- exception-fallback path ---
    failing_client_instance = AsyncMock()
    failing_client_instance.get = AsyncMock(side_effect=ConnectionError("boom"))
    failing_client_instance.__aenter__ = AsyncMock(return_value=failing_client_instance)
    failing_client_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch("database.cache.get_kv_cache", return_value=legacy_cached_data),
        patch(
            "services.fundamental_service.httpx.AsyncClient",
            return_value=failing_client_instance,
        ),
        patch("services.fundamental_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.fundamental_service import get_fundamental_context

        result = await get_fundamental_context("TSLA")

    assert result is not None
    assert result["form_type"] == ""
    assert result["sections"] == {}


@pytest.mark.asyncio
async def test_get_fundamental_context_exception_fallback_includes_form_type_and_sections() -> (
    None
):
    cached_data = {
        "text": "SEC 財報段落",
        "source_url": "https://sec.gov/doc",
        "accession_number": "0001-22",
        "form_type": "10-K",
        "sections": {"forward_guidance": "guidance cut"},
    }

    failing_client_instance = AsyncMock()
    failing_client_instance.get = AsyncMock(side_effect=ConnectionError("boom"))
    failing_client_instance.__aenter__ = AsyncMock(return_value=failing_client_instance)
    failing_client_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "database.user_settings.any_user_local_tunnel_enabled",
            return_value=True,
        ),
        patch("database.cache.get_kv_cache", return_value=cached_data),
        patch(
            "services.fundamental_service.httpx.AsyncClient",
            return_value=failing_client_instance,
        ),
        patch("services.fundamental_service.config") as mock_config,
    ):
        mock_config.TUNNEL_URL = "http://localhost:8000"

        from services.fundamental_service import get_fundamental_context

        result = await get_fundamental_context("TSLA")

    assert result is not None
    assert result["form_type"] == "10-K"
    assert result["sections"] == {"forward_guidance": "guidance cut"}
