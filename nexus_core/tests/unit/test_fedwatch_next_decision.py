"""FedWatch 定價寫入範圍與備援明細的回歸測試。

- `update_fedwatch_probability()` 只把定價寫入「下一次利率決議」事件列；過去以
  `LIKE '%FOMC%'` 比對，會連 FOMC 會議紀要（Minutes）、記者會與之後所有會議
  一併寫入，/calendar 會讀到會議紀要列上的過期定價。
- `get_latest_fedwatch_info()` 的備援明細三桶加總為 100%，且決策方向與數字一致。
- FedWatch 資料源說明依實際來源產生，不再一律宣稱取自 Atlanta Fed。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.calendar_cache import replace_macro_month_events
from database.connection import execute_write

# 遠期月份，避免與其他測試共用的記憶體資料庫互相干擾
_MONTH_1 = "2032-10"
_MONTH_2 = "2032-12"
_DECISION = "聯準會利率決策"


def _month_events() -> dict[str, list[dict[str, Any]]]:
    base = {"impact": "high", "country": "US"}
    return {
        _MONTH_1: [
            {**base, "event": "FOMC Minutes", "time": "2032-10-07T18:00:00Z"},
            {**base, "event": _DECISION, "time": "2032-10-27T18:00:00Z"},
            {**base, "event": "Fed Press Conference", "time": "2032-10-27T18:30:00Z"},
            {
                **base,
                "event": "FOMC 利率決策記者會 (鮑爾記者會)",
                "time": "2032-10-27T18:30:00Z",
            },
        ],
        _MONTH_2: [
            {**base, "event": _DECISION, "time": "2032-12-08T19:00:00Z"},
        ],
    }


@pytest.fixture
def _calendar(db_conn: Any) -> Iterator[None]:
    # 其他測試殘留的未來利率決議列會搶走「下一次決議」，先清除
    execute_write(
        "DELETE FROM economic_calendar_events WHERE event_time >= date('now')", ()
    )
    for month, events in _month_events().items():
        replace_macro_month_events(month, events)
    # 模擬舊版寫入殘留：會議紀要與 12 月會議帶著舊定價
    execute_write(
        "UPDATE economic_calendar_events SET fedwatch_probability = 0.42 "
        "WHERE month_key IN (?, ?)",
        (_MONTH_1, _MONTH_2),
    )
    yield
    for month in _month_events():
        replace_macro_month_events(month, [])


def _probabilities(db_conn: sqlite3.Connection) -> dict[tuple[str, str], Any]:
    cur = db_conn.execute(
        "SELECT event, event_time, fedwatch_probability FROM economic_calendar_events "
        "WHERE month_key IN (?, ?)",
        (_MONTH_1, _MONTH_2),
    )
    return {(str(r[0]), str(r[1])): r[2] for r in cur.fetchall()}


@pytest.mark.asyncio
async def test_fedwatch_is_written_only_to_next_rate_decision(
    db_conn: Any, _calendar: None
) -> None:
    from services.calendar_service import calendar_service
    import config

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "probability": 0.6385,
            "prob_maintain": 72.3,
            "prob_hike": 27.7,
            "prob_cut": 0.0,
            "meeting_date": "10/27",
            "source": "CME 30-Day Fed Funds Futures (ZQ)",
        },
    }

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("httpx.AsyncClient.get", return_value=mock_resp),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
    ):
        assert await calendar_service.update_fedwatch_probability() is True

    probs = _probabilities(db_conn)
    assert probs[(_DECISION, "2032-10-27T18:00:00Z")] == pytest.approx(0.6385)
    # 會議紀要、記者會與之後的會議皆不帶定價（舊值一併清除）
    assert probs[("FOMC Minutes", "2032-10-07T18:00:00Z")] is None
    assert probs[("Fed Press Conference", "2032-10-27T18:30:00Z")] is None
    assert probs[("FOMC 利率決策記者會 (鮑爾記者會)", "2032-10-27T18:30:00Z")] is None
    assert probs[(_DECISION, "2032-12-08T19:00:00Z")] is None


@pytest.mark.parametrize(
    ("prob", "hike", "maintain", "cut", "decision"),
    [
        (0.85, 70.0, 30.0, 0.0, "hike"),
        (0.72, 44.0, 56.0, 0.0, "maintain"),
        (0.50, 0.0, 100.0, 0.0, "maintain"),
        (0.30, 0.0, 60.0, 40.0, "maintain"),
        (0.10, 0.0, 20.0, 80.0, "cut"),
    ],
)
def test_fallback_details_are_internally_consistent(
    prob: float, hike: float, maintain: float, cut: float, decision: str
) -> None:
    from services.calendar_service import calendar_service

    with (
        patch.object(
            calendar_service,
            "get_latest_fedwatch_probability",
            return_value=(prob, True),
        ),
        patch("database.cache.get_kv_cache", return_value=None),
    ):
        p, is_fallback, details = calendar_service.get_latest_fedwatch_info()

    assert p == prob
    assert is_fallback is True
    assert details["prob_hike"] == pytest.approx(hike)
    assert details["prob_maintain"] == pytest.approx(maintain)
    assert details["prob_cut"] == pytest.approx(cut)
    assert details["decision"] == decision
    assert details["current_target"] == ""
    assert details["source"] == "fallback"


@pytest.mark.parametrize(
    ("source", "expected", "unexpected"),
    [
        ("Atlanta Fed Market Probability Tracker (MPT)", "Atlanta Fed", "ZQ"),
        ("CME 30-Day Fed Funds Futures (ZQ)", "(ZQ) 反推", "Atlanta Fed"),
        ("fallback", "備援估算", "Atlanta Fed"),
        (None, "備援估算", "Atlanta Fed"),
    ],
)
def test_fedwatch_source_note_reflects_actual_source(
    source: Any, expected: str, unexpected: str
) -> None:
    from cogs.embed_builders._embed_helpers import fedwatch_source_note

    note = fedwatch_source_note(source)
    assert expected in note
    assert unexpected not in note


def test_market_overview_embed_uses_actual_fedwatch_source() -> None:
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    embed = build_market_macro_overview_embed(
        {
            "fedwatch_probability": 0.6385,
            "fedwatch_is_fallback": False,
            "fedwatch_details": {
                "meeting_date": "10/28",
                "prob_maintain": 72.3,
                "prob_hike": 27.7,
                "prob_cut": 0.0,
                "decision": "maintain",
                "source": "CME 30-Day Fed Funds Futures (ZQ)",
            },
            "rrp": 0.0,
        }
    )
    text = "\n".join(str(f.value) for f in embed.fields)
    assert "(ZQ) 反推" in text
    assert "Atlanta Fed" not in text
    # RRP 餘額為 0 是真實狀態，不是抓取失敗
    assert "$0.0B" in text
