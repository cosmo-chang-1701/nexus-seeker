"""v080 — 新增 sentiment_daily_canonical 日級規範母體，並從 sentiment_history 回填。

為什麼：Skew 百分位原本以 `sentiment_history` 最近 500 列高頻觀測為母體，只涵蓋
兩三週，而且相鄰樣本高度自相關。本表每個交易日只留一筆，作為 252 交易日的
百分位母體。設計說明見 `market_analysis/sentiment/canonical_history.py`。

回填在 Python 內完成：SQLite 的 `date()` 不支援 `'America/New_York'` 這類 IANA
時區字串（會回傳 NULL），而且回填必須依 NYSE 行事曆排除非交易日並以實際收盤
時刻（含半日市）為界。重採樣規則與線上排程共用 `resample_daily_close()`。
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

version = 80
description = (
    "新增 sentiment_daily_canonical 日級規範母體 (每標的 × 交易日 × 指標一筆)，"
    "並以 sentiment_history 的盤中最後一筆觀測回填"
)

sql = """
CREATE TABLE IF NOT EXISTS sentiment_daily_canonical (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,                 -- 美東交易日 'YYYY-MM-DD'
    indicator TEXT NOT NULL,                  -- 'SKEW_D25' | 'PCR'
    value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'EOD_CLOSE', -- 'EOD_CLOSE' | 'SELF_HEAL' | 'BACKFILL'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_canonical_record UNIQUE (symbol, trade_date, indicator)
);

-- 百分位查詢 (symbol, indicator, trade_date < ? ORDER BY trade_date DESC) 的覆蓋索引
CREATE INDEX IF NOT EXISTS idx_canonical_lookup
    ON sentiment_daily_canonical (symbol, indicator, trade_date DESC, value);

-- 保留期清理 (trade_date < ?)
CREATE INDEX IF NOT EXISTS idx_canonical_date
    ON sentiment_daily_canonical (trade_date);

-- sentiment_history 的時間範圍查詢：收盤快照讀取單一交易時段、03:00 ET 保留期清理。
CREATE INDEX IF NOT EXISTS idx_sentiment_history_timestamp
    ON sentiment_history (timestamp);
"""

_BATCH_SIZE = 5000


def migrate_data(conn: sqlite3.Connection) -> None:
    """以 sentiment_history 回填。失敗只記錄、不中斷 migration。

    回填是「錦上添花」：少了它，表會由每日收盤排程逐日累積，百分位在累積滿
    20 個交易日前會退回既有的高頻池（行為與改版前相同）。因此這裡的任何失敗
    都不應讓 v080 被回滾、進而擋住後續 migration。

    不自行 commit：由 `database/core.py::run_migrations()` 在標記版本後統一提交，
    所以 INSERT 放在最後一次性執行，失敗時不會留下半套資料。
    """
    try:
        from market_analysis.sentiment.canonical_history import (
            CANONICAL_INDICATORS,
            build_insert_params,
            resample_daily_close,
        )
        from market_time import get_session_bounds_utc

        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sentiment_history'"
        )
        if not cursor.fetchone():
            return

        placeholders = ",".join("?" for _ in CANONICAL_INDICATORS)
        cursor.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM sentiment_history "  # nosemgrep
            f"WHERE indicator IN ({placeholders})",
            CANONICAL_INDICATORS,
        )
        lo, hi = cursor.fetchone() or (None, None)
        if not lo or not hi:
            return

        # 時間戳為 UTC；美東日期最多早一天，起點往前多抓一天避免邊界遺漏。
        from datetime import datetime, timedelta

        start = datetime.fromisoformat(str(lo)).date() - timedelta(days=1)
        end = datetime.fromisoformat(str(hi)).date()
        sessions = get_session_bounds_utc(start, end)
        if not sessions:
            return

        # 逐批讀取以控制 1GB VPS 的記憶體峰值；resample 只保留每日最後一筆，
        # 跨批次的「最晚」判斷靠逐批合併：後一批的同鍵觀測必然較晚寫入。
        merged: dict[tuple[str, str, str], float] = {}
        cursor.execute(
            f"SELECT symbol, indicator, value, timestamp FROM sentiment_history "  # nosemgrep
            f"WHERE indicator IN ({placeholders}) ORDER BY id ASC",
            CANONICAL_INDICATORS,
        )
        while True:
            batch = cursor.fetchmany(_BATCH_SIZE)
            if not batch:
                break
            for sym, day, ind, val in resample_daily_close(batch, sessions):
                merged[(sym, day, ind)] = val

        if not merged:
            return

        records = [(k[0], k[1], k[2], v) for k, v in sorted(merged.items())]
        cursor.executemany(
            "INSERT OR IGNORE INTO sentiment_daily_canonical "
            "(symbol, trade_date, indicator, value, source) VALUES (?, ?, ?, ?, ?)",
            build_insert_params(records, "BACKFILL"),
        )
        logger.info(
            f"✅ [v080] sentiment_daily_canonical 回填 {len(records)} 筆 "
            f"({len({r[1] for r in records})} 個交易日)。"
        )
    except Exception as e:
        logger.warning(
            f"[v080] sentiment_daily_canonical 回填失敗，改由每日排程累積: {e}"
        )
