"""replace_macro_month_events() 重寫整月總經事件時保留 FedWatch 欄位的回歸測試。

日曆來源不提供 `fedwatch_probability`；早期整月 DELETE + INSERT 的實作會讓
4h 排程（CPI 偏差更新內部的日曆 prefetch）、/market、Analyst Agent 等任何觸發
日曆重抓的路徑洗掉已寫入的 FedWatch 定價。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

import pytest

from database.calendar_cache import get_macro_month_status, replace_macro_month_events
from database.connection import execute_write

# 使用遠期月份，避免與其他測試共用的記憶體資料庫互相干擾
_MONTH = "2031-03"
_FOMC = "FOMC 利率決策"
_FOMC_TIME = "2031-03-18T18:00:00Z"
_CPI = "CPI 年增率"
_CPI_TIME = "2031-03-11T12:30:00Z"


@pytest.fixture(autouse=True)
def _clean_month(db_conn: Any) -> Iterator[None]:
    replace_macro_month_events(_MONTH, [])
    yield
    replace_macro_month_events(_MONTH, [])


def _events(**overrides: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"event": _FOMC, "time": _FOMC_TIME, "impact": "high", "country": "US"},
        {
            "event": _CPI,
            "time": _CPI_TIME,
            "impact": "high",
            "country": "US",
            "consensus_value": "3.0",
        },
    ]
    for item in events:
        item.update(overrides.get(item["event"], {}))
    return events


def _rows(db_conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    db_conn.row_factory = sqlite3.Row
    try:
        cur = db_conn.execute(
            "SELECT * FROM economic_calendar_events WHERE month_key = ?", (_MONTH,)
        )
        return {str(r["event"]): dict(r) for r in cur.fetchall()}
    finally:
        db_conn.row_factory = None


def _write_fedwatch(prob: float) -> None:
    execute_write(
        "UPDATE economic_calendar_events SET fedwatch_probability = ? "
        "WHERE month_key = ? AND event = ?",
        (prob, _MONTH, _FOMC),
    )


def test_refresh_preserves_existing_fedwatch_probability(db_conn: Any) -> None:
    replace_macro_month_events(_MONTH, _events())
    _write_fedwatch(0.72)

    # 模擬任何路徑觸發的日曆重抓：新資料沒有 fedwatch_probability，但更新了 CPI 實際值
    replace_macro_month_events(
        _MONTH, _events(**{_CPI: {"actual_value": 3.1, "consensus_value": "2.9"}})
    )

    rows = _rows(db_conn)
    assert rows[_FOMC]["fedwatch_probability"] == 0.72
    # 日曆欄位仍以最新抓取結果覆寫
    assert rows[_CPI]["actual_value"] == 3.1
    assert rows[_CPI]["consensus_value"] == "2.9"
    status = get_macro_month_status(_MONTH)
    assert status is not None and status["event_count"] == 2


def test_new_fedwatch_value_overrides_existing(db_conn: Any) -> None:
    replace_macro_month_events(_MONTH, _events())
    _write_fedwatch(0.72)

    replace_macro_month_events(
        _MONTH, _events(**{_FOMC: {"fedwatch_probability": 0.4}})
    )
    assert _rows(db_conn)[_FOMC]["fedwatch_probability"] == 0.4


def test_stale_events_are_removed(db_conn: Any) -> None:
    replace_macro_month_events(_MONTH, _events())
    _write_fedwatch(0.72)

    # 最新抓取結果中 CPI 已不存在（例如改期），FOMC 仍在
    replace_macro_month_events(_MONTH, _events()[:1])

    rows = _rows(db_conn)
    assert set(rows) == {_FOMC}
    assert rows[_FOMC]["fedwatch_probability"] == 0.72


def test_rescheduled_event_starts_without_fedwatch(db_conn: Any) -> None:
    """事件時間改變視為不同事件：舊列刪除，新列無 FedWatch（由下一次 FedWatch
    更新補上），不會把舊時間的定價錯接到新事件。"""
    replace_macro_month_events(_MONTH, _events())
    _write_fedwatch(0.72)

    replace_macro_month_events(
        _MONTH, _events(**{_FOMC: {"time": "2031-03-19T18:00:00Z"}})
    )
    rows = _rows(db_conn)
    assert rows[_FOMC]["event_time"] == "2031-03-19T18:00:00Z"
    assert rows[_FOMC]["fedwatch_probability"] is None


def test_empty_events_clear_month(db_conn: Any) -> None:
    replace_macro_month_events(_MONTH, _events())
    replace_macro_month_events(_MONTH, [])
    assert _rows(db_conn) == {}
    status = get_macro_month_status(_MONTH)
    assert status is not None and status["event_count"] == 0


def test_other_months_are_untouched(db_conn: Any) -> None:
    other_month = "2031-04"
    replace_macro_month_events(
        other_month,
        [
            {
                "event": _FOMC,
                "time": "2031-04-29T18:00:00Z",
                "impact": "high",
                "country": "US",
            }
        ],
    )
    replace_macro_month_events(_MONTH, _events())
    replace_macro_month_events(_MONTH, _events()[:1])

    cur = db_conn.execute(
        "SELECT COUNT(*) FROM economic_calendar_events WHERE month_key = ?",
        (other_month,),
    )
    assert cur.fetchone()[0] == 1
    replace_macro_month_events(other_month, [])
