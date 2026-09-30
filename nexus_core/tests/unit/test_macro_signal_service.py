"""總經訊號乾跑：FRED 解析、修正時保留首次所見值、冪等寫入、確認狀態串接、失敗隔離。"""

from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.macro_signal_log import (
    load_observations,
    load_recent_regimes,
    store_observations,
    write_daily_log,
)
from market_analysis.macro_signals import (
    STATE_GOOD,
    STATE_WORST,
    Indicator,
    IndicatorReading,
    Observation,
)
from services import macro_signal_service as svc


def _reading(ind: Indicator, flag: bool | None) -> IndicatorReading:
    return IndicatorReading(ind, 1.0, flag, date(2026, 9, 1), date(2026, 9, 2))


def test_parse_fred_csv_skips_missing_and_accepts_both_headers() -> None:
    text = "observation_date,DGS2\n2026-09-01,3.50\n2026-09-02,.\n2026-09-03,3.55\n"
    assert svc.parse_fred_csv(text) == [
        (date(2026, 9, 1), 3.5),
        (date(2026, 9, 3), 3.55),
    ]
    legacy = "DATE,ICSA\n2026-09-05,230000\n"
    assert svc.parse_fred_csv(legacy) == [(date(2026, 9, 5), 230000.0)]
    assert svc.parse_fred_csv("") == []


@pytest.mark.asyncio
async def test_fetch_attaches_available_dates(db_conn: Any) -> None:
    csv_text = "observation_date,ICSA\n2026-09-19,230000\n"
    with patch.object(svc, "_download_fred", new=AsyncMock(return_value=csv_text)):
        obs = await svc.fetch_fred_series("ICSA", date(2026, 9, 26))
    assert obs == [Observation(date(2026, 9, 19), 230000.0, date(2026, 9, 24))]


@pytest.mark.asyncio
async def test_revised_values_keep_first_seen(db_conn: Any) -> None:
    """FRED 事後修正時，保留當時首次看到的值（前向資料不受修正偏差影響）。"""
    d = date(2026, 9, 19)
    first = [Observation(d, 230000.0, d + timedelta(days=5))]
    revised = [Observation(d, 245000.0, d + timedelta(days=5))]
    assert await store_observations("ICSA", first, date(2026, 9, 24)) == 1
    assert await store_observations("ICSA", revised, date(2026, 10, 1)) == 0
    stored = load_observations("ICSA")
    assert [o.value for o in stored] == [230000.0]


@pytest.mark.asyncio
async def test_daily_log_is_idempotent(db_conn: Any) -> None:
    td = date(2026, 9, 25)
    readings = [
        _reading(Indicator.REAL_YIELD_JUMP, True),
        _reading(Indicator.FIN_STRESS, None),
    ]
    await write_daily_log(td, readings, "CAUTION", None)
    await write_daily_log(td, readings, "CAUTION", "CAUTION")
    cur = db_conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM macro_signal_log").fetchone()[0] == 2
    rows = cur.execute(
        "SELECT raw_state, confirmed_state FROM macro_regime_log"
    ).fetchall()
    assert rows == [("CAUTION", "CAUTION")]
    flag = cur.execute(
        "SELECT flag FROM macro_signal_log WHERE indicator = 'fin_stress'"
    ).fetchone()[0]
    assert flag is None


@pytest.mark.asyncio
async def test_confirmation_uses_prior_days_from_db(db_conn: Any) -> None:
    start = date(2026, 9, 1)
    for i in range(4):
        await write_daily_log(start + timedelta(days=i), [], STATE_WORST, STATE_GOOD)
    today = start + timedelta(days=4)
    assert len(load_recent_regimes(today, 4)) == 4
    worst_today = [_reading(Indicator.VIX_TERM_INVERSION, True)]
    raw, confirmed = svc.resolve_states(worst_today, today)
    assert raw == STATE_WORST
    assert confirmed == STATE_WORST
    # 今天轉好，但尚未連續 5 天 → 維持前一個確認狀態（上一列的 confirmed = GOOD）
    raw2, confirmed2 = svc.resolve_states([], today)
    assert raw2 == STATE_GOOD and confirmed2 == STATE_GOOD


def test_fred_readings_respect_availability() -> None:
    """觀測的可用日在今天之後時，不得進入計算。"""
    today = date(2026, 10, 9)
    obs = {
        "SAHMREALTIME": [
            Observation(date(2026, 8, 1), 0.3, date(2026, 9, 10)),
            Observation(date(2026, 9, 1), 0.7, date(2026, 10, 12)),
        ]
    }
    readings = {r.indicator: r for r in svc.fred_readings(obs, today)}
    assert readings[Indicator.SAHM_RULE].flag is False
    assert readings[Indicator.REAL_YIELD_JUMP].flag is None  # 無資料


@pytest.mark.asyncio
async def test_job_skipped_when_not_leader(db_conn: Any) -> None:
    bot = MagicMock()
    bot._is_leader_instance = False
    with patch.object(svc, "refresh_fred_observations", new=AsyncMock()) as refresh:
        assert await svc.run_macro_signal_job(bot, date(2026, 9, 25)) is None
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_job_writes_log_without_notifications(db_conn: Any) -> None:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    market = [_reading(Indicator.TECH_RELATIVE_WEAK, True)]
    with (
        patch.object(svc, "refresh_fred_observations", new=AsyncMock(return_value={})),
        patch.object(svc, "_market_readings", new=AsyncMock(return_value=market)),
        patch("services.llm_service.is_memory_safe", return_value=True),
    ):
        await svc.run_macro_signal_job(bot, date(2026, 9, 25))
    cur = db_conn.cursor()
    raw = cur.execute("SELECT raw_state FROM macro_regime_log").fetchone()[0]
    assert raw == "CAUTION"
    # 9 個指標全部記錄（FRED 7 個 + 市場 1 個此處 mock 為 1 個）
    assert cur.execute("SELECT COUNT(*) FROM macro_signal_log").fetchone()[0] == 8
    bot.queue_dm.assert_not_called()


@pytest.mark.asyncio
async def test_job_failure_is_contained(db_conn: Any) -> None:
    bot = MagicMock()
    bot._is_leader_instance = True
    with (
        patch.object(
            svc,
            "refresh_fred_observations",
            new=AsyncMock(side_effect=RuntimeError("down")),
        ),
        patch("services.llm_service.is_memory_safe", return_value=True),
    ):
        assert await svc.run_macro_signal_job(bot, date(2026, 9, 25)) is None


@pytest.mark.asyncio
async def test_single_series_failure_does_not_block_others(db_conn: Any) -> None:
    async def fake_fetch(series_id: str, today: date) -> list[Observation]:
        if series_id == "ICSA":
            raise RuntimeError("timeout")
        return [Observation(date(2026, 9, 1), 1.0, date(2026, 9, 2))]

    with patch.object(svc, "fetch_fred_series", new=fake_fetch):
        inserted = await svc.refresh_fred_observations(date(2026, 9, 25))
    assert "ICSA" not in inserted
    assert inserted["DGS2"] == 1


@pytest.mark.asyncio
async def test_job_disabled_by_config(db_conn: Any) -> None:
    bot = MagicMock()
    bot._is_leader_instance = True
    with (
        patch("config.ENABLE_MACRO_SIGNAL_LOG", False),
        patch.object(svc, "refresh_fred_observations", new=AsyncMock()) as refresh,
    ):
        assert await svc.run_macro_signal_job(bot, date(2026, 9, 25)) is None
    refresh.assert_not_awaited()


def test_v084_migration_contract_and_tables(db_conn: Any) -> None:
    """缺 version / description / sql 任一者的遷移會被無聲跳過。"""
    from database.core import get_migrations
    from database.migrations import v084_add_macro_signal_log as m

    assert m.version == 84 and m.description and m.sql
    assert any(x["version"] == 84 for x in get_migrations())
    tables = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "macro_series_observation",
        "macro_signal_log",
        "macro_regime_log",
    } <= tables
