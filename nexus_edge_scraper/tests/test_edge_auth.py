import logging
from typing import Any, Iterable, List
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from local_api import app
from local_api.auth import log_auth_mode, require_edge_token

client = TestClient(app)

TOKEN = "unit-test-secret-token-0123456789-abcdefXYZ"
SHORT = "short-secret-xyz"
HEALTH = "/api/v1/health/sys"


def _collect_calls(dependant: object) -> List[object]:
    calls: List[object] = []
    for d in getattr(dependant, "dependencies", []):
        calls.append(d.call)
        calls.extend(_collect_calls(d))
    return calls


def test_unset_is_fail_open() -> None:
    assert client.get(HEALTH).status_code == 200


def test_blank_token_behaves_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_API_TOKEN", "   ")
    assert client.get(HEALTH).status_code == 200


def test_set_token_enforced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EDGE_API_TOKEN", TOKEN)
    missing = client.get(HEALTH)
    wrong = client.get(HEALTH, headers={"X-Edge-Token": "nope-" + TOKEN[:10]})
    ok = client.get(HEALTH, headers={"X-Edge-Token": TOKEN})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    for r in (missing, wrong):
        body = r.json()
        assert set(body) == {"status", "message"}
        assert body["status"] == "unauthorized"
        assert isinstance(body["message"], str) and body["message"]
        assert TOKEN not in r.text
    assert TOKEN not in caplog.text


def test_unauthorized_sync_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_API_TOKEN", TOKEN)
    with patch("database.upsert_tracked_symbols") as mock_upsert:
        r = client.post("/api/v1/watchlist/sync", json={"symbols": ["AAPL"]})
    assert r.status_code == 401
    mock_upsert.assert_not_called()


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_docs_routes_disabled(path: str) -> None:
    assert client.get(path).status_code == 404


def _is_api_route(r: Any) -> bool:
    # 新版 FastAPI 的 _EffectiveRouteContext 不是 APIRoute 子類，但具有 dependant／dependencies
    return isinstance(r, APIRoute) or (
        hasattr(r, "dependencies") and hasattr(r, "endpoint") and hasattr(r, "methods")
    )


def _flatten_routes(routes: Iterable[Any]) -> List[Any]:
    """展開 app.routes；新版 FastAPI 以 _IncludedRouter 延遲展開 include_router。"""
    flat: List[Any] = []
    for r in routes:
        if hasattr(r, "effective_candidates"):
            for c in r.effective_candidates():
                if hasattr(c, "effective_candidates"):
                    flat.extend(_flatten_routes([c]))
                else:
                    flat.append(getattr(c, "starlette_route", None) or c)
        else:
            flat.append(r)
    return flat


def test_every_route_has_auth_dependency() -> None:
    routes = [r for r in _flatten_routes(app.routes) if _is_api_route(r)]
    assert len(routes) > 10
    for route in routes:
        calls = _collect_calls(route.dependant) if route.dependant else []
        calls.extend(d.dependency for d in route.dependencies)
        assert require_edge_token in calls, route.path


def test_log_auth_mode_unset(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log_auth_mode()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_log_auth_mode_short(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EDGE_API_TOKEN", SHORT)
    with caplog.at_level(logging.INFO):
        log_auth_mode()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert SHORT not in caplog.text


def test_log_auth_mode_strong(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EDGE_API_TOKEN", TOKEN)
    with caplog.at_level(logging.INFO):
        log_auth_mode()
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert TOKEN not in caplog.text
