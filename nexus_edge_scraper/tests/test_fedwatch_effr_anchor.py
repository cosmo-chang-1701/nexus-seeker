"""FedWatch ZQ 備援路徑：r1 預設錨改用 NY Fed EFFR（不再用 ^IRX 推算）。"""

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
from fastapi.testclient import TestClient

from local_api import app
from local_api.macro import _fetch_effr

client = TestClient(app)

_ZQ_SOURCE = "CME 30-Day Fed Funds Futures (ZQ)"


def _df(price: float) -> "pd.DataFrame":
    return pd.DataFrame({"Close": [price]}, index=[pd.Timestamp.now()])


def _ticker_factory(prices: dict[str, float]) -> Any:
    def mock_ticker(sym: str) -> MagicMock:
        t = MagicMock()
        if sym in prices:
            t.history.return_value = _df(prices[sym])
        else:
            t.history.return_value = pd.DataFrame()
        return t

    return mock_ticker


def _get_fedwatch(
    prices: dict[str, float],
    effr: tuple[float, float, float] | None,
    today: date = date(2026, 10, 16),
) -> dict[str, Any]:
    with (
        patch("yfinance.Ticker", side_effect=_ticker_factory(prices)),
        patch("requests.get", side_effect=Exception("Mock Atlanta Fed failure")),
        patch("local_api.macro._fetch_effr", return_value=effr),
        patch("local_api.macro._fedwatch_today", return_value=today),
    ):
        response = client.get("/api/v1/scrape/macro/fedwatch")
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


def test_missing_adjacent_contracts_anchor_on_effr() -> None:
    # 10/28 會議走 (b) 需 ZQX26；缺報價時 r1 退回 EFFR 3.88，而非 IRX 推算的 4.125。
    data = _get_fedwatch({"ZQV26.CBT": 96.115}, (3.88, 3.75, 4.00))
    assert data["source"] == _ZQ_SOURCE
    assert data["current_target"] == "3.75%-4.00%"
    assert data["effr"] == 3.88
    # r2 = (31*3.885 - 28*3.88)/3 ≈ 3.9317 → 加息約 20.7%，非降息 100%
    assert data["prob_cut"] == 0.0
    assert data["prob_hike"] == 20.7
    assert data["decision"] == "maintain"
    assert "anchor_gap_bp" not in data


def test_no_anchor_at_all_skips_zq_result() -> None:
    data = _get_fedwatch({"ZQV26.CBT": 96.115}, None)
    assert data["source"] == "fallback"


def test_anchor_gap_reported_when_contract_r1_deviates_from_effr() -> None:
    # 合約反推 r1 ≈ 3.8786，EFFR 刻意給 3.50 → 差 > 10bp
    data = _get_fedwatch({"ZQV26.CBT": 96.115, "ZQX26.CBT": 96.055}, (3.50, 3.25, 3.50))
    assert data["source"] == _ZQ_SOURCE
    assert data["prob_hike"] == 26.6  # 機率仍由合約決定
    assert data["anchor_gap_bp"] == round((3.8786 - 3.50) * 100.0, 1)


def _mock_response(payload: Any) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_effr_parses_nyfed_payload() -> None:
    payload = {
        "refRates": [
            {
                "effectiveDate": "2026-10-15",
                "type": "EFFR",
                "percentRate": 3.88,
                "targetRateFrom": 3.75,
                "targetRateTo": 4.00,
            }
        ]
    }
    with patch("httpx.get", return_value=_mock_response(payload)):
        assert _fetch_effr() == (3.88, 3.75, 4.00)


def test_fetch_effr_malformed_returns_none() -> None:
    payloads: list[dict[str, Any]] = [
        {},
        {"refRates": []},
        {"refRates": [{"percentRate": 3.88}]},
        {"refRates": [{"percentRate": "x", "targetRateFrom": 3.75, "targetRateTo": 4}]},
        {
            "refRates": [
                {
                    "percentRate": float("nan"),
                    "targetRateFrom": 3.75,
                    "targetRateTo": 4.0,
                }
            ]
        },
    ]
    for payload in payloads:
        with patch("httpx.get", return_value=_mock_response(payload)):
            assert _fetch_effr() is None


def test_fetch_effr_network_error_returns_none() -> None:
    with patch("httpx.get", side_effect=httpx.ConnectError("boom")):
        assert _fetch_effr() is None
