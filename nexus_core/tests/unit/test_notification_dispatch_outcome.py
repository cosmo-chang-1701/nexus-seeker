"""通知成效前向評估：送達紀錄、反事實路徑、20 日標註門檻與 notif-report。"""

import importlib
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import database
from database.notifications import set_user_notification_setting
from market_analysis.notification_outcome import (
    DEFAULT_HORIZON_SESSIONS,
    EXTENDED_HORIZON_SESSIONS,
    counterfactual_paths,
    exposures,
    forward_daily_returns,
)
from services import notification_dispatch_recorder as recorder
from services.notification_dispatch_recorder import (
    DispatchRecord,
    flush_dispatch_records,
    rollover_dispatch_record,
)
from services.notification_dispatcher import notify, notify_many


def _leader_bot() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot._is_leader_instance = True
    return bot


_REC = DispatchRecord(symbol="nvda", signal_kind="EXIT", scenario="SATELLITE_REBALANCE")


# ---------------------------------------------------------------------------
# 只在實際入列後記錄
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_only_after_enqueue(db_conn: Any) -> None:
    bot = _leader_bot()
    assert await notify(
        bot, 1, "defense_option_rollover", embed=MagicMock(), record=_REC
    )
    assert recorder.pending_count() == 1
    await flush_dispatch_records()
    cur = db_conn.cursor()
    cur.execute("SELECT symbol, signal_kind, channel FROM notification_dispatch_log")
    assert cur.fetchall() == [("NVDA", "EXIT", "defense_option_rollover")]


@pytest.mark.asyncio
async def test_disabled_channel_is_not_recorded(db_conn: Any) -> None:
    bot = _leader_bot()
    set_user_notification_setting(2, "defense_option_rollover", False)
    assert not await notify(
        bot, 2, "defense_option_rollover", embed=MagicMock(), record=_REC
    )
    assert recorder.pending_count() == 0


@pytest.mark.asyncio
async def test_dedup_blocked_is_not_recorded(db_conn: Any) -> None:
    bot = _leader_bot()
    await database.save_kv_cache("rollover_alert_3_x", True)
    assert not await notify(
        bot,
        3,
        "defense_option_rollover",
        embed=MagicMock(),
        dedup_key="rollover_alert_3_x",
        record=_REC,
    )
    assert recorder.pending_count() == 0


@pytest.mark.asyncio
async def test_failed_enqueue_is_not_recorded(db_conn: Any) -> None:
    bot = _leader_bot()
    bot.queue_dm = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError):
        await notify(bot, 4, "defense_option_rollover", embed=MagicMock(), record=_REC)
    assert recorder.pending_count() == 0


@pytest.mark.asyncio
async def test_non_leader_or_mock_bot_never_records(db_conn: Any) -> None:
    """單元測試的 MagicMock bot（屬性不是 True）與非 leader 實例都不記錄。"""
    mock_bot = MagicMock()
    mock_bot.queue_dm = AsyncMock()
    await notify(mock_bot, 5, "defense_option_rollover", embed=MagicMock(), record=_REC)
    follower = _leader_bot()
    follower._is_leader_instance = False
    await notify_many(
        follower, 5, "defense_option_rollover", [MagicMock()], record=_REC
    )
    assert recorder.pending_count() == 0


@pytest.mark.asyncio
async def test_duplicate_event_same_day_is_ignored(db_conn: Any) -> None:
    bot = _leader_bot()
    for _ in range(2):
        await notify(bot, 6, "defense_option_rollover", embed=MagicMock(), record=_REC)
    await flush_dispatch_records()
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notification_dispatch_log WHERE user_id = 6")
    assert cur.fetchone()[0] == 1


def test_disabled_flag_is_noop() -> None:
    bot = _leader_bot()
    with patch("config.ENABLE_NOTIFICATION_DISPATCH_LOG", False):
        recorder.record_dispatch(bot, 7, "defense_option_rollover", _REC)
    assert recorder.pending_count() == 0


# ---------------------------------------------------------------------------
# 動態轉倉指令映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ins", "kind", "direction", "ratio"),
    [
        ({"action": "LIQUIDATE"}, "EXIT", "LONG", 1.0),
        ({"action": "REDUCE", "sell_ratio": 0.3}, "REDUCE", "LONG", 0.3),
        ({"action": "REDUCE", "sell_ratio": 0.0}, "INFO", "LONG", 0.0),
        ({"action": "OPEN_PYRAMID"}, "ENTRY", "LONG", 1.0),
        ({"action": "OPEN_SHORT", "direction": "SHORT"}, "ENTRY", "SHORT", 1.0),
        ({"action": "BUY_PROTECTIVE_PUT"}, "EXIT", "LONG", 1.0),
        ({"action": "HOLD"}, "INFO", "LONG", 1.0),
        ({"action": "ADVISORY", "exit_tier": "SL1"}, "EXIT", "LONG", 1.0),
        ({"action": "ADVISORY", "exit_tier": "TP2"}, "INFO", "LONG", 1.0),
        ({"action": "LIQUIDATE", "instrument_type": "OPTIONS"}, "INFO", "LONG", 1.0),
    ],
)
def test_rollover_mapping(
    ins: dict[str, Any], kind: str, direction: str, ratio: float
) -> None:
    rec = rollover_dispatch_record({"symbol": "AAPL", "scenario": "S", **ins})
    assert (rec.signal_kind, rec.direction, rec.exposure_ratio) == (
        kind,
        direction,
        ratio,
    )


# ---------------------------------------------------------------------------
# 反事實路徑：無前視
# ---------------------------------------------------------------------------


def _daily(n: int = 40, start: str = "2026-03-02") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)  # tz-naive 美東日期
    closes = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame({"Close": closes}, index=idx)


def test_dispatch_before_close_uses_same_day_close() -> None:
    daily = _daily()
    # 2026-03-04 10:00 ET = 15:00 UTC → 第一個觀測是 03-04 收盤 (102)
    path = forward_daily_returns(
        daily, datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc), price=101.5
    )
    assert path is not None
    assert path.entry_ref_price == 101.5
    assert path.returns[0] == pytest.approx(102.0 / 101.5 - 1)
    assert len(path.returns) == DEFAULT_HORIZON_SESSIONS + 1


def test_dispatch_after_close_starts_next_session_without_lookahead() -> None:
    daily = _daily()
    # 2026-03-04 17:00 ET = 22:00 UTC → 從 03-05 開始；參考價為 03-04 收盤 (102)
    path = forward_daily_returns(
        daily, datetime(2026, 3, 4, 22, 0, tzinfo=timezone.utc)
    )
    assert path is not None
    assert path.entry_ref_price == 102.0
    assert path.returns[0] == pytest.approx(103.0 / 102.0 - 1)


def test_future_bars_beyond_horizon_do_not_change_path() -> None:
    """窗口之後的 K 棒被改動，結果不得改變。"""
    ts = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    base = forward_daily_returns(_daily(40), ts, price=101.0)
    tampered = _daily(40)
    tampered.iloc[-5:, 0] = 1.0
    assert forward_daily_returns(tampered, ts, price=101.0) == base


def test_insufficient_sessions_returns_none() -> None:
    daily = _daily(15)
    assert (
        forward_daily_returns(daily, datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc))
        is None
    )


def test_exposures_table() -> None:
    assert exposures("ENTRY", "LONG", 1.0) == (1.0, 0.0)
    assert exposures("ENTRY", "SHORT", 0.5) == (-0.5, 0.0)
    assert exposures("REDUCE", "LONG", 0.3) == (pytest.approx(0.7), 1.0)
    assert exposures("EXIT", "LONG", 1.0) == (0.0, 1.0)
    assert exposures("INFO", "LONG", 1.0) is None


def test_exit_in_falling_market_improves_mdd() -> None:
    paths = counterfactual_paths([-0.02] * 21, "EXIT", rf_annual=0.0)
    assert paths is not None
    assert paths.follow_mdd == 0.0
    assert paths.hold_mdd > 0.3
    assert paths.follow_total_return > paths.hold_total_return


# ---------------------------------------------------------------------------
# labeler：20 個交易日門檻
# ---------------------------------------------------------------------------


def test_outcome_row_deferred_then_no_data() -> None:
    from services.regime_outcome_labeler import build_dispatch_outcome_row

    now = datetime(2026, 3, 20, 8, 0, tzinfo=timezone.utc)
    row = {
        "id": 1,
        "dispatched_at": "2026-03-04 15:00:00",
        "signal_kind": "EXIT",
        "direction": "LONG",
        "exposure_ratio": 1.0,
        "price": 101.0,
    }
    # 資料不足、尚未超過 90 天：延後
    assert build_dispatch_outcome_row(row, _daily(15), now) is None
    # 資料不足且已超過 90 天：NO_DATA
    late = now + timedelta(days=120)
    stale = build_dispatch_outcome_row(row, _daily(15), late)
    assert stale is not None and stale["label_status"] == "NO_DATA"
    labeled = build_dispatch_outcome_row(row, _daily(40), now)
    assert labeled is not None and labeled["label_status"] == "LABELED"
    assert (
        len(json.loads(labeled["follow_returns_json"])) == DEFAULT_HORIZON_SESSIONS + 1
    )


def test_outcome_row_uses_60_sessions_when_available() -> None:
    """能走完 60 日就直接以 60 日標註（報告以前 21 期重建 20 日視窗）。"""
    from services.regime_outcome_labeler import build_dispatch_outcome_row

    row = {
        "id": 2,
        "dispatched_at": "2026-03-04 15:00:00",
        "signal_kind": "EXIT",
        "direction": "LONG",
        "exposure_ratio": 1.0,
        "price": 101.0,
    }
    out = build_dispatch_outcome_row(
        row, _daily(80), datetime(2026, 7, 1, tzinfo=timezone.utc)
    )
    assert out is not None and out["horizon_days"] == EXTENDED_HORIZON_SESSIONS
    assert len(json.loads(out["follow_returns_json"])) == EXTENDED_HORIZON_SESSIONS + 1
    # 前 21 期與單獨以 20 日計算的路徑一致（兩個視窗可並列比較）
    short = build_dispatch_outcome_row(
        row, _daily(40), datetime(2026, 4, 10, tzinfo=timezone.utc)
    )
    assert short is not None and short["horizon_days"] == DEFAULT_HORIZON_SESSIONS
    assert json.loads(out["follow_returns_json"])[: DEFAULT_HORIZON_SESSIONS + 1] == (
        json.loads(short["follow_returns_json"])
    )


def test_extension_retry_keeps_existing_20_day_label() -> None:
    """已以 20 日標註者：60 日尚未走完時不覆寫、也不降級成 NO_DATA。"""
    from services.regime_outcome_labeler import build_dispatch_outcome_row

    row = {
        "id": 3,
        "dispatched_at": "2026-03-04 15:00:00",
        "signal_kind": "EXIT",
        "direction": "LONG",
        "exposure_ratio": 1.0,
        "price": 101.0,
        "label_status": "LABELED",
        "horizon_days": DEFAULT_HORIZON_SESSIONS,
    }
    far_future = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert build_dispatch_outcome_row(row, _daily(40), far_future) is None
    assert build_dispatch_outcome_row(row, None, far_future) is None
    extended = build_dispatch_outcome_row(row, _daily(80), far_future)
    assert extended is not None
    assert extended["horizon_days"] == EXTENDED_HORIZON_SESSIONS


@pytest.mark.asyncio
async def test_labeler_extends_previously_labeled_rows(db_conn: Any) -> None:
    from services import regime_outcome_labeler

    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO notification_dispatch_log (id, dispatched_at, trade_date, user_id, "
        "channel, symbol, scenario, action, signal_kind, direction, exposure_ratio, price) "
        "VALUES (10, '2026-03-04 15:00:00', '2026-03-04', 1, 'c', 'EXT', 'S', 'A', "
        "'EXIT', 'LONG', 1.0, 101.0)"
    )
    cur.execute(
        "INSERT INTO notification_dispatch_outcome (dispatch_id, label_status, "
        "label_version, horizon_days) VALUES (10, 'LABELED', 1, 20)"
    )
    db_conn.commit()

    with (
        patch(
            "market_time.get_trading_days_ago_utc", return_value="2026-05-20 13:30:00"
        ),
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=_daily(80)),
        ),
        patch.object(regime_outcome_labeler, "_FETCH_SLEEP_SECONDS", 0.0),
    ):
        summary = await regime_outcome_labeler.run_dispatch_outcome_labeling(
            now_utc=datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
        )
    assert summary.pending == 1 and summary.labeled == 1
    cur.execute(
        "SELECT horizon_days FROM notification_dispatch_outcome WHERE dispatch_id = 10"
    )
    assert cur.fetchone()[0] == EXTENDED_HORIZON_SESSIONS


@pytest.mark.asyncio
async def test_labeler_queries_with_20_session_cutoff(db_conn: Any) -> None:
    from services import regime_outcome_labeler

    cur = db_conn.cursor()
    cur.executemany(
        "INSERT INTO notification_dispatch_log (dispatched_at, trade_date, user_id, "
        "channel, symbol, scenario, action, signal_kind, direction, exposure_ratio, price) "
        "VALUES (?, ?, 1, 'defense_option_rollover', ?, 'S', 'A', ?, 'LONG', 1.0, 101.0)",
        [
            ("2026-03-04 15:00:00", "2026-03-04", "OLD", "EXIT"),
            ("2026-03-30 15:00:00", "2026-03-30", "NEW", "EXIT"),
            ("2026-03-04 15:00:00", "2026-03-04", "INF", "INFO"),
        ],
    )
    db_conn.commit()

    with (
        patch(
            "market_time.get_trading_days_ago_utc", return_value="2026-03-10 13:30:00"
        ) as cutoff,
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=_daily(40)),
        ) as fetch,
        patch.object(regime_outcome_labeler, "_FETCH_SLEEP_SECONDS", 0.0),
    ):
        summary = await regime_outcome_labeler.run_dispatch_outcome_labeling(
            now_utc=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc)
        )
    assert [c.args[0] for c in cutoff.call_args_list] == [
        DEFAULT_HORIZON_SESSIONS + 1,
        EXTENDED_HORIZON_SESSIONS + 1,
    ]
    assert summary.pending == 1  # NEW 未走完、INFO 不計算
    assert summary.labeled == 1
    assert [c.args[0] for c in fetch.await_args_list] == ["OLD"]


# ---------------------------------------------------------------------------
# notif-report：決定性、無網路、不寫 DB、判讀
# ---------------------------------------------------------------------------


def _synthetic_rows(n: int, follow_better: bool, seed: int = 1) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        base = rng.normal(0.001, 0.02, DEFAULT_HORIZON_SESSIONS + 1)
        hold = base
        follow = base * (0.3 if follow_better else 1.0) - (
            0.0 if follow_better else 0.004
        )
        rows.append(
            {
                "channel": "defense_option_rollover",
                "scenario": "SATELLITE_REBALANCE",
                "signal_kind": "REDUCE",
                "follow_returns_json": json.dumps(follow.tolist()),
                "hold_returns_json": json.dumps(hold.tolist()),
                "follow_total_return": float(np.prod(1 + follow) - 1),
                "hold_total_return": float(np.prod(1 + hold) - 1),
                "follow_mdd": 0.05,
                "hold_mdd": 0.08,
            }
        )
    return rows


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("notif-report 不得發動網路連線")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


def test_notif_report_deterministic_and_verdicts(no_network: None) -> None:
    from calibration.notif_report import (
        VERDICT_INSUFFICIENT,
        VERDICT_SUGGEST_OFF,
        build_notif_report,
        render_markdown,
    )

    worse = _synthetic_rows(40, follow_better=False)
    a = build_notif_report(worse, n_boot=200, seed=7)
    b = build_notif_report(worse, n_boot=200, seed=7)
    assert a == b
    section = a["sections"][0]
    assert section["n_events"] == 40
    assert section["delta_sortino"] < 0
    assert section["verdict"] == VERDICT_SUGGEST_OFF

    # 只有 21 期的事件只出現在 20 日視窗
    assert {s["horizon_days"] for s in a["sections"]} == {DEFAULT_HORIZON_SESSIONS}

    few = build_notif_report(_synthetic_rows(5, follow_better=False), n_boot=50)
    assert few["sections"][0]["verdict"] == VERDICT_INSUFFICIENT
    assert "照做 vs 持有不動" in render_markdown(a)


def test_notif_report_pools_daily_returns_and_splits_horizons(
    no_network: None,
) -> None:
    """60 日事件同時貢獻 20 日（前 21 期）與 60 日兩個視窗；Sortino 以合併序列計算。"""
    from calibration.notif_report import build_notif_report
    from market_analysis.downside_risk import sortino_ratio
    from market_analysis.notification_outcome import DEFAULT_RF_ANNUAL

    rng = np.random.default_rng(3)
    rows = []
    for _ in range(3):
        hold = rng.normal(0.0, 0.02, EXTENDED_HORIZON_SESSIONS + 1)
        follow = hold * 0.5
        rows.append(
            {
                "channel": "c",
                "scenario": "s",
                "signal_kind": "REDUCE",
                "follow_returns_json": json.dumps(follow.tolist()),
                "hold_returns_json": json.dumps(hold.tolist()),
            }
        )
    result = build_notif_report(rows, n_boot=20)
    by_h = {s["horizon_days"]: s for s in result["sections"]}
    assert set(by_h) == {DEFAULT_HORIZON_SESSIONS, EXTENDED_HORIZON_SESSIONS}
    assert by_h[DEFAULT_HORIZON_SESSIONS]["n_events"] == 3

    n = DEFAULT_HORIZON_SESSIONS + 1
    pooled_f = [r for row in rows for r in json.loads(row["follow_returns_json"])[:n]]
    pooled_h = [r for row in rows for r in json.loads(row["hold_returns_json"])[:n]]
    expected = sortino_ratio(pooled_f, DEFAULT_RF_ANNUAL) - sortino_ratio(
        pooled_h, DEFAULT_RF_ANNUAL
    )
    assert by_h[DEFAULT_HORIZON_SESSIONS]["delta_sortino"] == pytest.approx(expected)


def test_notif_report_cli_reads_snapshot_readonly(
    tmp_path: Path, db_conn: Any, no_network: None
) -> None:
    """CLI 讀取快照並寫出 results.json / report.md；快照以唯讀模式開啟。"""
    import sqlite3

    snap = tmp_path / "snapshot.db"
    conn = sqlite3.connect(snap)
    mig = importlib.import_module(
        "database.migrations.v083_add_notification_dispatch_log"
    )
    conn.executescript(mig.sql)
    conn.execute(
        "INSERT INTO notification_dispatch_log (id, dispatched_at, trade_date, user_id, "
        "channel, symbol, scenario, action, signal_kind) VALUES "
        "(1, '2026-03-04 15:00:00', '2026-03-04', 1, 'c', 'AAA', 's', 'a', 'EXIT')"
    )
    conn.execute(
        "INSERT INTO notification_dispatch_outcome (dispatch_id, label_status, "
        "label_version, follow_returns_json, hold_returns_json, follow_total_return, "
        "hold_total_return, follow_mdd, hold_mdd) VALUES "
        "(1, 'LABELED', 1, '[0.0, 0.0]', '[-0.01, 0.01]', 0.0, 0.0, 0.0, 0.01)"
    )
    conn.commit()
    conn.close()

    from calibration.__main__ import main

    code = main(
        [
            "notif-report",
            "--force",
            "--snapshot-db",
            str(snap),
            "--out",
            str(tmp_path / "out"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert code == 0
    outputs = list((tmp_path / "out" / "calibration").glob("notif-report_*"))
    assert len(outputs) == 1
    result = json.loads((outputs[0] / "results.json").read_text(encoding="utf-8"))
    assert result["total_labeled"] == 1
    assert (outputs[0] / "report.md").exists()


def test_v083_exports_required_attributes() -> None:
    mod = importlib.import_module(
        "database.migrations.v083_add_notification_dispatch_log"
    )
    assert mod.version == 83
    assert mod.description and "notification_dispatch_log" in mod.sql
