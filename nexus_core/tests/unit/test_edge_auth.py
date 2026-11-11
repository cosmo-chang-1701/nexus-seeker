"""services/edge_auth.py：edge 驗證標頭與 401/403 響亮記錄（快速、無網路）。"""

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services import edge_auth
from services.market_data_service import _core

_TOKEN = "tok-SECRET-value-123"
_CF_ID = "cf-id-SECRET-456"
_CF_SECRET = "cf-secret-SECRET-789"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    # 預設把三個驗證設定清空，避免開發機 .env 的真實值污染測試；
    # 各測試內部的 patch(...) 仍會覆寫這些預設值
    monkeypatch.setattr("config.EDGE_API_TOKEN", "")
    monkeypatch.setattr("config.CF_ACCESS_CLIENT_ID", "")
    monkeypatch.setattr("config.CF_ACCESS_CLIENT_SECRET", "")
    edge_auth._last_rejection_log.clear()
    edge_auth._partial_cf_warned = False
    yield
    edge_auth._last_rejection_log.clear()
    edge_auth._partial_cf_warned = False


def _headers(token: str = "", cf_id: str = "", cf_secret: str = "") -> dict[str, str]:
    with (
        patch("config.EDGE_API_TOKEN", token),
        patch("config.CF_ACCESS_CLIENT_ID", cf_id),
        patch("config.CF_ACCESS_CLIENT_SECRET", cf_secret),
    ):
        return edge_auth.edge_auth_headers()


def test_no_config_yields_no_headers() -> None:
    assert _headers() == {}


def test_token_only() -> None:
    assert _headers(token=_TOKEN) == {"X-Edge-Token": _TOKEN}


def test_cf_pair_only() -> None:
    assert _headers(cf_id=_CF_ID, cf_secret=_CF_SECRET) == {
        "CF-Access-Client-Id": _CF_ID,
        "CF-Access-Client-Secret": _CF_SECRET,
    }


def test_token_and_cf_pair() -> None:
    assert _headers(_TOKEN, _CF_ID, _CF_SECRET) == {
        "X-Edge-Token": _TOKEN,
        "CF-Access-Client-Id": _CF_ID,
        "CF-Access-Client-Secret": _CF_SECRET,
    }


def test_single_cf_value_adds_neither_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.edge_auth"):
        assert _headers(cf_id=_CF_ID) == {}
        assert _headers(cf_secret=_CF_SECRET) == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert _CF_ID not in caplog.text and _CF_SECRET not in caplog.text


def test_whitespace_only_values_ignored() -> None:
    assert _headers(token="   ", cf_id=" ", cf_secret="\n") == {}
    assert _headers(token=f"  {_TOKEN}\n") == {"X-Edge-Token": _TOKEN}


def test_edge_client_kwargs_shape() -> None:
    with patch("config.EDGE_API_TOKEN", _TOKEN):
        kwargs = edge_auth.edge_client_kwargs()
    assert kwargs["headers"] == {"X-Edge-Token": _TOKEN}
    assert kwargs["event_hooks"] == {"response": [edge_auth.log_edge_auth_rejection]}


def _response(status: int, url: str = "http://edge.test/api/v1/x?k=secretq") -> Any:
    return httpx.Response(status, request=httpx.Request("GET", url))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejection_logs_error(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("config.EDGE_API_TOKEN", _TOKEN),
        caplog.at_level(logging.ERROR, logger="services.edge_auth"),
    ):
        await edge_auth.log_edge_auth_rejection(_response(status))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "/api/v1/x" in caplog.text
    assert "secretq" not in caplog.text
    assert _TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_rejection_is_throttled(caplog: pytest.LogCaptureFixture) -> None:
    clock = [1000.0]
    with (
        patch("services.edge_auth.time.monotonic", side_effect=lambda: clock[0]),
        caplog.at_level(logging.ERROR, logger="services.edge_auth"),
    ):
        await edge_auth.log_edge_auth_rejection(_response(401))
        clock[0] += 10
        await edge_auth.log_edge_auth_rejection(_response(401))
        assert len(caplog.records) == 1
        # 不同狀態碼各自節流
        await edge_auth.log_edge_auth_rejection(_response(403))
        assert len(caplog.records) == 2
        clock[0] += 601
        await edge_auth.log_edge_auth_rejection(_response(401))
        assert len(caplog.records) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429, 500])
async def test_rejection_silent_for_other_statuses(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="services.edge_auth"):
        await edge_auth.log_edge_auth_rejection(_response(status))
    assert caplog.records == []


@pytest.mark.asyncio
async def test_rejection_never_raises() -> None:
    bad = MagicMock()
    bad.status_code = 401
    bad.request = None
    await edge_auth.log_edge_auth_rejection(bad)


@pytest.mark.asyncio
async def test_get_edge_client_passes_auth_headers() -> None:
    loop = asyncio.get_running_loop()
    _core._edge_clients_by_loop.pop(loop, None)
    try:
        with (
            patch("config.EDGE_API_TOKEN", _TOKEN),
            patch("httpx.AsyncClient") as m_cls,
        ):
            _core.get_edge_client()
        kwargs = m_cls.call_args.kwargs
        assert kwargs["headers"] == {"X-Edge-Token": _TOKEN}
        assert kwargs["event_hooks"] == {
            "response": [edge_auth.log_edge_auth_rejection]
        }
    finally:
        _core._edge_clients_by_loop.pop(loop, None)
