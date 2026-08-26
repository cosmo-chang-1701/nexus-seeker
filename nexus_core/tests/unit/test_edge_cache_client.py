from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import edge_cache_client


def _mock_httpx_client(response: Any) -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_noop_when_tunnel_url_unset() -> None:
    with patch("config.TUNNEL_URL", ""), patch("httpx.AsyncClient") as m_client_cls:
        await edge_cache_client.sync_watchlist_symbols(["AAPL", "TSLA"])
        m_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_noop_when_symbols_empty() -> None:
    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient") as m_client_cls,
    ):
        await edge_cache_client.sync_watchlist_symbols([])
        m_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_posts_to_edge() -> None:
    resp = _mock_response(200, {"status": "success", "data": {"synced": 2}})
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await edge_cache_client.sync_watchlist_symbols(["AAPL", "TSLA"])

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.await_args
    assert call_args.args[0] == "http://mock-tunnel/api/v1/watchlist/sync"
    assert call_args.kwargs["json"] == {
        "symbols": ["AAPL", "TSLA"],
        "priority_symbols": [],
    }


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_includes_priority_symbols() -> None:
    resp = _mock_response(200, {"status": "success", "data": {"synced": 2}})
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await edge_cache_client.sync_watchlist_symbols(
            ["AAPL", "TSLA"], priority_symbols=["NVDA"]
        )

    call_args = mock_client.post.await_args
    assert call_args.kwargs["json"] == {
        "symbols": ["AAPL", "TSLA"],
        "priority_symbols": ["NVDA"],
    }


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_syncs_when_only_priority_symbols_present() -> (
    None
):
    """實際持倉標的可能沒有被加入任何使用者的自選清單，symbols 為空時
    仍應同步 priority_symbols，而不是被空 symbols 擋下。"""
    resp = _mock_response(200, {"status": "success", "data": {"synced": 0}})
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await edge_cache_client.sync_watchlist_symbols([], priority_symbols=["NVDA"])

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.await_args
    assert call_args.kwargs["json"] == {"symbols": [], "priority_symbols": ["NVDA"]}


@pytest.mark.asyncio
async def test_sync_watchlist_symbols_swallows_exceptions() -> None:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("connection refused"))

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        # 不應拋出例外
        await edge_cache_client.sync_watchlist_symbols(["AAPL"])


@pytest.mark.asyncio
async def test_get_cached_gex_returns_none_when_tunnel_url_unset() -> None:
    with patch("config.TUNNEL_URL", ""):
        result = await edge_cache_client.get_cached_gex("AAPL")
        assert result is None


@pytest.mark.asyncio
async def test_get_cached_gex_returns_data_on_success() -> None:
    resp = _mock_response(
        200,
        {
            "status": "success",
            "data": {"spot": 100.0, "call_wall": 110.0, "put_wall": 90.0},
            "age_seconds": 42.0,
        },
    )
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await edge_cache_client.get_cached_gex("AAPL")

    assert result is not None
    assert result["data"]["call_wall"] == 110.0
    assert result["age_seconds"] == 42.0


@pytest.mark.asyncio
async def test_get_cached_gex_returns_none_on_cache_miss() -> None:
    resp = _mock_response(200, {"status": "error", "message": "not_found"})
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await edge_cache_client.get_cached_gex("AAPL")

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_gex_returns_none_on_exception() -> None:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("timeout"))

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await edge_cache_client.get_cached_gex("AAPL")

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_option_chain_returns_none_when_tunnel_url_unset() -> None:
    with patch("config.TUNNEL_URL", ""):
        result = await edge_cache_client.get_cached_option_chain("AAPL", "2026-09-18")
        assert result is None


@pytest.mark.asyncio
async def test_get_cached_option_chain_returns_data_on_success() -> None:
    resp = _mock_response(
        200,
        {
            "status": "success",
            "data": {
                "expiry": "2026-09-18",
                "calls": [{"strike": 100.0}],
                "puts": [{"strike": 90.0}],
            },
            "age_seconds": 10.0,
        },
    )
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await edge_cache_client.get_cached_option_chain("AAPL", "2026-09-18")

    assert result is not None
    assert result["data"]["expiry"] == "2026-09-18"
    mock_client.get.assert_awaited_once()
    call_args = mock_client.get.await_args
    assert call_args.args[0] == "http://mock-tunnel/api/v1/cache/options/AAPL/chain"
    assert call_args.kwargs["params"] == {"expiry": "2026-09-18"}


@pytest.mark.asyncio
async def test_get_cached_option_chain_omits_expiry_param_when_none() -> None:
    resp = _mock_response(200, {"status": "error", "message": "not_found"})
    mock_client = _mock_httpx_client(resp)

    with (
        patch("config.TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await edge_cache_client.get_cached_option_chain("AAPL")

    assert result is None
    call_args = mock_client.get.await_args
    assert call_args.kwargs["params"] is None
