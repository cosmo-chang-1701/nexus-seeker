"""情緒指標日級規範母體 (sentiment_daily_canonical) 與相關量綱修正的測試。

涵蓋：
- 重採樣定義（盤中最後一筆、非交易日／盤前盤後排除、半日市收盤、舊 SKEW 不映射）
- 百分位與 robust Z（midrank、IQR 下限、三態）
- 規範母體優先、樣本不足退回高頻池、無前視偏差
- 收盤快照冪等、v080 回填
- 0~100 百分位契約（不做 0~1 自動放大）
- UOA／sentiment_history 交易日保留期
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from market_analysis.sentiment.canonical_history import (
    CANONICAL_MIN_SAMPLES,
    ROBUST_Z_MIN_IQR,
    compute_canonical_stats,
    resample_daily_close,
    snapshot_trading_day,
)
from market_analysis.sentiment.skew_taxonomy import (
    SKEW_DIVERGENCE_HIGH_PERCENTILE,
    SKEW_DIVERGENCE_LOW_PERCENTILE,
    SKEW_HIGH_DEFENSE_PERCENTILE,
    SKEW_INDICATOR,
    SKEW_NEUTRAL_PERCENTILE,
    SKEW_TRIPLE_CONFLUENCE_PERCENTILE,
    ensure_percentile_pct,
)
from market_time import get_last_completed_trading_date, get_session_bounds_utc

NY = ZoneInfo("America/New_York")


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> str:
    """美東時間 → sentiment_history 使用的 UTC 字串。"""
    return (
        datetime(y, mo, d, h, mi, tzinfo=NY)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


# ---------------------------------------------------------------------------
# 重採樣定義
# ---------------------------------------------------------------------------


def test_resample_picks_last_in_session_observation() -> None:
    sessions = get_session_bounds_utc(
        datetime(2026, 7, 15).date(), datetime(2026, 7, 15).date()
    )
    rows = [
        ("aapl", SKEW_INDICATOR, 4.2, _utc(2026, 7, 15, 10, 0)),
        ("AAPL", SKEW_INDICATOR, 5.8, _utc(2026, 7, 15, 15, 45)),
        # 盤後（收盤後重抓的期權鏈報價不可靠）不得覆蓋盤中值
        ("AAPL", SKEW_INDICATOR, 9.9, _utc(2026, 7, 15, 20, 0)),
        # 盤前同樣排除
        ("AAPL", "PCR", 3.0, _utc(2026, 7, 15, 8, 45)),
        ("AAPL", "PCR", 0.9, _utc(2026, 7, 15, 11, 0)),
    ]
    out = resample_daily_close(rows, sessions)
    assert ("AAPL", "2026-07-15", SKEW_INDICATOR, 5.8) in out
    assert ("AAPL", "2026-07-15", "PCR", 0.9) in out
    assert len(out) == 2


def test_resample_excludes_non_trading_days_and_legacy_skew() -> None:
    # 2026-07-18 為週六；舊 "SKEW" 為 ±5% 履約價代理值，不得映射進 SKEW_D25
    sessions = get_session_bounds_utc(
        datetime(2026, 7, 17).date(), datetime(2026, 7, 18).date()
    )
    rows = [
        ("MSFT", SKEW_INDICATOR, 3.1, _utc(2026, 7, 18, 12, 0)),
        ("MSFT", "SKEW", 7.7, _utc(2026, 7, 17, 12, 0)),
        ("MSFT", SKEW_INDICATOR, float("nan"), _utc(2026, 7, 17, 12, 0)),
    ]
    assert resample_daily_close(rows, sessions) == []


def test_resample_respects_early_close_and_winter_offset() -> None:
    # 2026-11-27 黑色星期五半日市 13:00 ET 收盤；冬令 EST (UTC-5)
    day = datetime(2026, 11, 27).date()
    sessions = get_session_bounds_utc(day, day)
    rows = [
        ("NVDA", "PCR", 0.8, _utc(2026, 11, 27, 12, 55)),
        ("NVDA", "PCR", 1.9, _utc(2026, 11, 27, 13, 30)),  # 半日市收盤後
    ]
    assert resample_daily_close(rows, sessions) == [("NVDA", "2026-11-27", "PCR", 0.8)]


# ---------------------------------------------------------------------------
# 百分位與 robust Z
# ---------------------------------------------------------------------------


def test_stats_below_min_samples_returns_none() -> None:
    stats = compute_canonical_stats(
        [1.0] * (CANONICAL_MIN_SAMPLES - 1), 5.0, SKEW_INDICATOR
    )
    assert stats.percentile is None
    assert stats.robust_z is None
    assert stats.is_canonical is False


def test_stats_flatline_uses_midrank_and_suppresses_z() -> None:
    """資料源卡住（全同值）：分位回到 50%，IQR=0 時 Z 不得爆炸成天文數字。"""
    stats = compute_canonical_stats([3.5] * 70, 3.6, SKEW_INDICATOR)
    assert stats.percentile == 100.0
    assert stats.robust_z is None
    assert stats.is_canonical is True

    flat = compute_canonical_stats([3.5] * 70, 3.5, SKEW_INDICATOR)
    assert flat.percentile == 50.0


def test_stats_iqr_below_floor_suppresses_z() -> None:
    tiny = ROBUST_Z_MIN_IQR[SKEW_INDICATOR] / 10
    values = [2.0 + (i % 4) * tiny for i in range(40)]
    stats = compute_canonical_stats(values, 2.5, SKEW_INDICATOR)
    assert stats.percentile == 100.0
    assert stats.robust_z is None
    assert stats.is_canonical is False  # 20 <= N < 60 過渡期


def test_stats_robust_z_resists_outlier() -> None:
    values = [2.0 + (i % 10) * 0.2 for i in range(64)] + [99.0]
    stats = compute_canonical_stats(values, 2.9, SKEW_INDICATOR)
    assert stats.robust_z is not None
    assert abs(stats.robust_z) < 0.5


# ---------------------------------------------------------------------------
# 規範母體優先、退回高頻池、無前視偏差
# ---------------------------------------------------------------------------


def _insert_canonical(conn: sqlite3.Connection, symbol: str, days: int) -> None:
    base = datetime(2025, 1, 1)
    conn.executemany(
        "INSERT INTO sentiment_daily_canonical (symbol, trade_date, indicator, value, source) "
        "VALUES (?, ?, ?, ?, 'EOD_CLOSE')",
        [
            (
                symbol,
                (base + timedelta(days=i)).strftime("%Y-%m-%d"),
                SKEW_INDICATOR,
                float(i),
            )
            for i in range(days)
        ],
    )
    conn.commit()


def test_percentile_prefers_canonical_population(db_conn: Any) -> None:
    from market_analysis.sentiment.history_storage import (
        get_indicator_percentile_detail,
    )

    _insert_canonical(db_conn, "CANONA", 100)
    res = get_indicator_percentile_detail("CANONA", SKEW_INDICATOR, 49.5)
    assert res.source == "CANONICAL"
    assert res.sample_size == 100
    assert res.percentile == 50.0
    assert res.is_canonical is True


def test_percentile_excludes_as_of_date_and_later(db_conn: Any) -> None:
    from market_analysis.sentiment.history_storage import (
        get_indicator_percentile_detail,
    )

    _insert_canonical(db_conn, "CANONB", 100)
    # 2025-01-01 + 50 天 = 2025-02-20；只有該日之前的 50 筆可納入母體
    res = get_indicator_percentile_detail(
        "CANONB", SKEW_INDICATOR, 1000.0, as_of_date="2025-02-20"
    )
    assert res.sample_size == 50
    assert res.percentile == 100.0


def test_percentile_falls_back_to_intraday_pool(db_conn: Any) -> None:
    from market_analysis.sentiment.history_storage import (
        get_indicator_percentile_detail,
    )

    _insert_canonical(db_conn, "CANONC", CANONICAL_MIN_SAMPLES - 1)
    db_conn.executemany(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) VALUES (?, ?, ?, ?)",
        [
            ("CANONC", SKEW_INDICATOR, float(i), f"2026-09-0{i % 5 + 1} 15:00:00")
            for i in range(30)
        ],
    )
    db_conn.commit()
    res = get_indicator_percentile_detail("CANONC", SKEW_INDICATOR, 100.0)
    assert res.source == "INTRADAY_FALLBACK"
    assert res.sample_size == 30
    assert res.percentile == 100.0
    assert res.robust_z is None
    assert res.is_canonical is False


# ---------------------------------------------------------------------------
# 收盤快照與 v080 回填
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_trading_day_is_idempotent(db_conn: Any) -> None:
    db_conn.executemany(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) VALUES (?, ?, ?, ?)",
        [
            ("SNAP", SKEW_INDICATOR, 4.0, _utc(2026, 7, 15, 10, 0)),
            ("SNAP", SKEW_INDICATOR, 5.0, _utc(2026, 7, 15, 15, 45)),
            ("SNAP", SKEW_INDICATOR, 9.0, _utc(2026, 7, 15, 17, 0)),
            ("SNAP", "PCR", 0.7, _utc(2026, 7, 15, 12, 0)),
        ],
    )
    db_conn.commit()

    assert await snapshot_trading_day("2026-07-15") == 2

    # 盤中觀測事後變動也不得覆寫已寫入的交易日
    db_conn.execute(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) VALUES (?, ?, ?, ?)",
        ("SNAP", SKEW_INDICATOR, 6.0, _utc(2026, 7, 15, 15, 55)),
    )
    db_conn.commit()
    assert await snapshot_trading_day("2026-07-15", source="SELF_HEAL") == 0

    rows = db_conn.execute(
        "SELECT indicator, value, source FROM sentiment_daily_canonical "
        "WHERE symbol = 'SNAP' ORDER BY indicator"
    ).fetchall()
    assert rows == [("PCR", 0.7, "EOD_CLOSE"), (SKEW_INDICATOR, 5.0, "EOD_CLOSE")]

    # 非交易日（週六）
    assert await snapshot_trading_day("2026-07-18") == 0


def test_v080_backfill_resamples_history() -> None:
    from database.migrations.v080_add_sentiment_daily_canonical import (
        migrate_data,
        sql,
    )

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE sentiment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol TEXT, indicator TEXT, value REAL, timestamp TIMESTAMP)"
        )
        conn.executescript(sql)
        conn.executemany(
            "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) VALUES (?, ?, ?, ?)",
            [
                ("AAPL", SKEW_INDICATOR, 4.2, _utc(2026, 7, 15, 10, 0)),
                ("AAPL", SKEW_INDICATOR, 5.8, _utc(2026, 7, 15, 15, 45)),
                ("AAPL", SKEW_INDICATOR, 9.9, _utc(2026, 7, 15, 20, 0)),
                ("AAPL", "SKEW", 1.0, _utc(2026, 7, 16, 12, 0)),
                ("NVDA", "PCR", 0.95, _utc(2026, 12, 15, 15, 55)),
                ("NVDA", "PCR", 1.20, _utc(2026, 12, 19, 12, 0)),  # 週六
            ],
        )
        migrate_data(conn)
        rows = conn.execute(
            "SELECT symbol, trade_date, indicator, value, source "
            "FROM sentiment_daily_canonical ORDER BY symbol"
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        ("AAPL", "2026-07-15", SKEW_INDICATOR, 5.8, "BACKFILL"),
        ("NVDA", "2026-12-15", "PCR", 0.95, "BACKFILL"),
    ]


# ---------------------------------------------------------------------------
# 0~100 百分位契約
# ---------------------------------------------------------------------------


def test_ensure_percentile_pct_never_rescales_low_tail() -> None:
    """0~1% 是合法的最低分位（看漲極端），不得被當成小數形式放大成 80%/100%。"""
    assert ensure_percentile_pct(0.8) == 0.8
    assert ensure_percentile_pct(1.0) == 1.0
    assert ensure_percentile_pct(97.5) == 97.5
    assert ensure_percentile_pct(None) == SKEW_NEUTRAL_PERCENTILE
    assert ensure_percentile_pct(150.0) == SKEW_NEUTRAL_PERCENTILE
    assert ensure_percentile_pct(-1.0) == SKEW_NEUTRAL_PERCENTILE
    assert ensure_percentile_pct(math.nan) == SKEW_NEUTRAL_PERCENTILE


@pytest.mark.asyncio
async def test_telemetry_price_tail_defense_uses_percent_scale() -> None:
    from services.telemetry_pricing_engine import calculate_telemetry_price

    common: dict[str, Any] = dict(
        symbol="AAPL",
        base_price=100.0,
        spot_price=100.0,
        iv=0.3,
        hist_iv=0.3,
        max_pain=0.0,
        prev_max_pain=0.0,
        base_quantity=100,
    )
    for pct in (0.8, 98.0):
        _, _, logs = await calculate_telemetry_price(**common, skew_percentile_pct=pct)
        assert any("尾端風險防禦" in line for line in logs), pct

    _, _, neutral_logs = await calculate_telemetry_price(
        **common, skew_percentile_pct=50.0
    )
    assert not any("尾端風險防禦" in line for line in neutral_logs)


def test_skew_gate_thresholds_unchanged() -> None:
    """母體改版只換百分位來源；門檻數值維持改版前的值，調整需走 calibration。"""
    assert SKEW_DIVERGENCE_HIGH_PERCENTILE == 85.0
    assert SKEW_DIVERGENCE_LOW_PERCENTILE == 15.0
    assert SKEW_HIGH_DEFENSE_PERCENTILE == 90.0
    assert SKEW_TRIPLE_CONFLUENCE_PERCENTILE == 98.0


# ---------------------------------------------------------------------------
# 交易日工具與保留期
# ---------------------------------------------------------------------------


def test_last_completed_trading_date() -> None:
    # 週一盤前 → 上週五
    assert get_last_completed_trading_date(datetime(2026, 3, 9, 8, 45)) == "2026-03-06"
    # 半日市 13:00 收盤後 → 當天
    assert (
        get_last_completed_trading_date(datetime(2026, 11, 27, 13, 5)) == "2026-11-27"
    )
    # 感恩節（休市）→ 前一個交易日
    assert (
        get_last_completed_trading_date(datetime(2026, 11, 26, 18, 0)) == "2026-11-25"
    )


@pytest.mark.asyncio
async def test_uoa_purge_uses_trading_day_retention(db_conn: Any) -> None:
    from database.uoa_history import purge_stale_uoa_history

    now = datetime.now(timezone.utc)
    rows = [
        ((now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"), "OLD"),
        ((now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "NEW"),
    ]
    db_conn.executemany(
        "INSERT INTO uoa_history (observed_at, observed_bar_ts, symbol, expiry, strike, "
        "opt_type, action) VALUES (?, ?, 'PURGE', '2026-12-18', 100.0, 'CALL', 'BUY')",
        rows,
    )
    db_conn.commit()

    assert await purge_stale_uoa_history() == 1
    left = db_conn.execute(
        "SELECT observed_bar_ts FROM uoa_history WHERE symbol = 'PURGE'"
    ).fetchall()
    assert left == [("NEW",)]


@pytest.mark.asyncio
async def test_sentiment_history_purge(db_conn: Any) -> None:
    from market_analysis.sentiment.history_storage import (
        purge_stale_sentiment_history,
    )

    now = datetime.now(timezone.utc)
    db_conn.executemany(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) VALUES (?, ?, ?, ?)",
        [
            (
                "PRG",
                "PCR",
                1.0,
                (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
            (
                "PRG",
                "PCR",
                1.1,
                (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ],
    )
    db_conn.commit()

    assert await purge_stale_sentiment_history() == 1
    left = db_conn.execute(
        "SELECT value FROM sentiment_history WHERE symbol = 'PRG'"
    ).fetchall()
    assert left == [(1.1,)]
