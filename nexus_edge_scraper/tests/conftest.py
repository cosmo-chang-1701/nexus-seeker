import pytest


@pytest.fixture(autouse=True)
def _no_edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """既有測試維持 fail-open，即使開發者環境設了 EDGE_API_TOKEN。"""
    monkeypatch.delenv("EDGE_API_TOKEN", raising=False)
