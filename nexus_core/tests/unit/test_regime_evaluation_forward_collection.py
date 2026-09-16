"""前向蒐集：v075 migration、DB 存取層、熱路徑記錄器、事後結果標註排程。"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from database.migrations import v075_add_regime_evaluation_log as mig_v075
from market_analysis import evaluation_recorder as rec


def _count(db_conn: Any, table: str) -> int:
    cur = db_conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")  # nosemgrep
    return int(cur.fetchone()[0])


# ---------------------------------------------------------------- migration
def test_v075_migration_contract() -> None:
    assert mig_v075.version == 75
    assert mig_v075.description
    assert "regime_evaluation_log" in mig_v075.sql
    assert "regime_evaluation_outcome" in mig_v075.sql


def test_v075_tables_and_dedup_index_exist(db_conn: Any) -> None:
    cur = db_conn.cursor()
    cur.execute("PRAGMA table_info(regime_evaluation_log)")
    cols = {row[1] for row in cur.fetchall()}
    from database.regime_evaluation_log import LOG_COLUMNS, OUTCOME_COLUMNS

    assert set(LOG_COLUMNS) <= cols
    assert "user_id" not in cols  # 閘門結果與使用者無關
    cur.execute("PRAGMA table_info(regime_evaluation_outcome)")
    assert set(OUTCOME_COLUMNS) <= {row[1] for row in cur.fetchall()}
    cur.execute("PRAGMA index_list(regime_evaluation_log)")
    assert "uq_regime_eval_dedup" in {row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------- DB 存取層
def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bar_ts": "2026-03-02T10:00:00-05:00",
        "symbol": "AAA",
        "source": "PORTFOLIO_MONITOR",
        "evaluator": "ENTRY_SHORT",
        "direction": "SHORT",
        "decision": 1,
        "spot": 100.0,
        "atr_1d": 5.0,
        "stop_price": 105.0,
        "target_price": 95.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_insert_dedups_on_symbol_evaluator_source_bar(db_conn: Any) -> None:
    from database.regime_evaluation_log import insert_regime_evaluations

    await insert_regime_evaluations([_row(), _row(decision=0)])
    await insert_regime_evaluations([_row(source="SYMBOL_VIEW")])
    assert _count(db_conn, "regime_evaluation_log") == 2


@pytest.mark.asyncio
async def test_pending_query_and_outcome_roundtrip(db_conn: Any) -> None:
    from database.regime_evaluation_log import (
        fetch_pending_evaluations,
        insert_regime_evaluations,
        save_evaluation_outcomes,
    )

    await insert_regime_evaluations([_row(), _row(symbol="BBB")])
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    pending = fetch_pending_evaluations(future)
    assert {p["symbol"] for p in pending} == {"AAA", "BBB"}
    await save_evaluation_outcomes(
        [
            {
                "evaluation_id": pending[0]["id"],
                "label_status": "NO_DATA",
                "label_version": 1,
            }
        ]
    )
    assert len(fetch_pending_evaluations(future)) == 1
    past = "2000-01-01 00:00:00"
    assert fetch_pending_evaluations(past) == []


@pytest.mark.asyncio
async def test_purge_deletes_outcomes_before_logs(db_conn: Any) -> None:
    from database.regime_evaluation_log import (
        insert_regime_evaluations,
        purge_regime_evaluation_log,
        save_evaluation_outcomes,
    )

    await insert_regime_evaluations([_row()])
    cur = db_conn.cursor()
    cur.execute(
        "UPDATE regime_evaluation_log SET evaluated_at = datetime('now', '-400 days')"
    )
    db_conn.commit()
    cur.execute("SELECT id FROM regime_evaluation_log")
    eval_id = cur.fetchone()[0]
    await save_evaluation_outcomes(
        [{"evaluation_id": eval_id, "label_status": "LABELED", "label_version": 1}]
    )
    await purge_regime_evaluation_log(retention_days=365)
    assert _count(db_conn, "regime_evaluation_log") == 0
    assert _count(db_conn, "regime_evaluation_outcome") == 0


# ---------------------------------------------------------------- 記錄器
class TestRecorder:
    def test_no_source_means_no_record(self) -> None:
        rec.record_gate_reason("ENTRY_RIGHT", "AAA", 100.0, True, "條件一✅")
        assert rec.pending_count() == 0

    def test_records_within_source_context(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_gate_reason(
                "ENTRY_RIGHT",
                "aaa",
                100.0,
                False,
                "條件一✅ | 條件二✅ | 條件三❌ | 條件四✅ | 條件五⏭️ | 條件六⏭️",
                candidate_radar={
                    "gex_profile_data": {"put_wall": 95.0, "call_wall": 110.0},
                    "iv_metrics": {"iv_rank": 42.0},
                },
            )
        assert rec.pending_count() == 1
        row = rec._BUFFER[0]
        assert row["symbol"] == "AAA"
        assert row["source"] == "PORTFOLIO_MONITOR"
        assert row["conditions_mask"] == 0b1011
        assert row["conditions_evaluated_mask"] == 0b1111
        assert row["put_wall"] == 95.0
        assert row["ivr"] == 42.0

    def test_disabled_flag_is_noop(self) -> None:
        with patch("config.ENABLE_REGIME_EVALUATION_LOG", False):
            with rec.evaluation_source("PORTFOLIO_MONITOR"):
                rec.record_gate_reason("ENTRY_RIGHT", "AAA", 100.0, True, "")
        assert rec.pending_count() == 0

    def test_buffer_is_bounded(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            for i in range(rec._BUFFER_MAXLEN + 50):
                rec.record_gate_reason("ENTRY_RIGHT", f"S{i}", 1.0, True, "")
        assert rec.pending_count() == rec._BUFFER_MAXLEN

    def test_short_evaluation_masks_and_digest_truncation(self) -> None:
        from tests.unit.short_entry_helpers import make_short_entry_evaluation

        ev = make_short_entry_evaluation(
            all_passed=False,
            conditions=(True, False, True, True, None, None),
            reason="x" * 2000,
        )
        with rec.evaluation_source("SYMBOL_VIEW"):
            rec.record_short_evaluation(ev, "aaa")
        row = rec._BUFFER[0]
        assert row["evaluator"] == "ENTRY_SHORT"
        assert row["conditions_mask"] == 0b1101
        assert row["conditions_evaluated_mask"] == 0b1111
        assert len(row["reason_digest"]) == rec._REASON_DIGEST_MAX

    def test_parse_condition_masks_without_markers(self) -> None:
        assert rec.parse_condition_masks("⛔ Regime II") == (None, None)

    @pytest.mark.asyncio
    async def test_flush_writes_once_and_drains(self, db_conn: Any) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_gate_reason("ENTRY_RIGHT", "AAA", 100.0, True, "")
            rec.record_gate_reason("ENTRY_RIGHT", "AAA", 101.0, False, "")  # 同鍵合併
            rec.record_gate_reason("ENTRY_LEFT", "AAA", 100.0, True, "")
        await rec.flush_evaluations()
        assert rec.pending_count() == 0
        assert _count(db_conn, "regime_evaluation_log") == 2

    @pytest.mark.asyncio
    async def test_classifier_records_once(self) -> None:
        from market_analysis.dynamic_rollover.regime_classifier import (
            classify_dynamic_regime,
        )

        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            regime, _reason, _data = await classify_dynamic_regime("AAA", 0.0, {})
        assert regime.value == "REGIME_II_CHAOS_STANDASIDE"
        assert rec.pending_count() == 1
        assert rec._BUFFER[0]["evaluator"] == "REGIME_CLASSIFIER"
        assert rec._BUFFER[0]["decision"] == 0


# ---------------------------------------------------------------- 結果標註排程
def _hourly(days: int, start: pd.Timestamp, price: float) -> pd.DataFrame:
    idx: list[pd.Timestamp] = []
    day = start
    while len(idx) < days * 7:
        if day.dayofweek < 5:
            idx.extend(
                day + pd.Timedelta(hours=14, minutes=30) + pd.Timedelta(hours=h)
                for h in range(7)
            )
        day += pd.Timedelta(days=1)
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": [price] * n,
            "High": [price + 0.1] * n,
            "Low": [price - 6.0] * n,
            "Close": [price] * n,
        },
        index=pd.DatetimeIndex(idx),
    )


@pytest.mark.asyncio
async def test_run_outcome_labeling_labels_and_marks_no_data(db_conn: Any) -> None:
    from database.regime_evaluation_log import insert_regime_evaluations
    from services.regime_outcome_labeler import run_outcome_labeling

    await insert_regime_evaluations([_row(), _row(symbol="EMPTY")])
    cur = db_conn.cursor()
    cur.execute("UPDATE regime_evaluation_log SET evaluated_at = '2026-03-02 15:00:00'")
    db_conn.commit()

    bars = _hourly(8, pd.Timestamp("2026-03-02", tz="UTC"), 100.0)

    async def _history(
        symbol: str, period: str = "1y", interval: str = "1d", **_: Any
    ) -> pd.DataFrame:
        return pd.DataFrame() if symbol == "EMPTY" else bars

    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        side_effect=_history,
    ) as mock_hist, patch("services.regime_outcome_labeler._FETCH_SLEEP_SECONDS", 0.0):
        summary = await run_outcome_labeling(
            now_utc=datetime(2026, 3, 20, tzinfo=timezone.utc)
        )
    assert summary.labeled == 1
    assert summary.no_data == 1
    # 紀錄已帶 ATR₁D → 每標的只抓一次 K 線，不額外抓日線
    assert mock_hist.await_count == 2
    cur.execute(
        "SELECT o.label_status, o.first_touch_k1, o.plan_outcome FROM regime_evaluation_outcome o "
        "JOIN regime_evaluation_log l ON l.id = o.evaluation_id WHERE l.symbol = 'AAA'"
    )
    status, touch_k1, plan = cur.fetchone()
    assert status == "LABELED"
    assert touch_k1 == -1  # Low 94 <= 100 − 5
    assert plan == 1  # 做空：目標 95 先於停損 105


@pytest.mark.asyncio
async def test_run_outcome_labeling_skips_recent_rows(db_conn: Any) -> None:
    from database.regime_evaluation_log import insert_regime_evaluations
    from services.regime_outcome_labeler import run_outcome_labeling

    await insert_regime_evaluations([_row()])
    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        summary = await run_outcome_labeling()
    assert summary.pending == 0
    mock_hist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leader", "memory_safe", "expected_calls"),
    [(True, True, 1), (True, False, 0), (False, True, 0)],
)
async def test_scheduler_labeler_is_leader_and_memory_gated(
    leader: bool, memory_safe: bool, expected_calls: int
) -> None:
    from types import SimpleNamespace

    from cogs.trading.scheduler import SchedulerCog

    fake_self = SimpleNamespace(bot=SimpleNamespace(_is_leader_instance=leader))
    with patch("services.llm_service.is_memory_safe", return_value=memory_safe), patch(
        "services.regime_outcome_labeler.run_outcome_labeling",
        new_callable=AsyncMock,
    ) as mock_run:
        await SchedulerCog.regime_outcome_labeler.coro(fake_self)  # type: ignore[arg-type]
    assert mock_run.await_count == expected_calls
