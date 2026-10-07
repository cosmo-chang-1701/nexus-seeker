"""基本面管線離線前向統計分析工具單元測試。"""

import sqlite3
from pathlib import Path
from typing import Any

from calibration.fundamental_forward_report import (
    generate_fundamental_forward_report,
    load_fundamental_logs,
    run_fundamental_forward_report,
    write_report,
)


def test_generate_report_empty() -> None:
    empty_logs: dict[str, list[dict[str, Any]]] = {
        "fair_value_log": [],
        "revision_score_log": [],
        "fundamental_watch_candidate": [],
    }
    report = generate_fundamental_forward_report(empty_logs)
    assert "基本面管線離線前向校準報告" in report
    assert "歷史掃描交易日數 : 0 天" in report
    assert "涵蓋獨立標的總數 : 0 檔" in report
    assert "暫無公允價值歷史日誌" in report
    assert "暫無分析師修正歷史日誌" in report
    assert "暫無候選觀察名單歷史日誌" in report


def test_generate_report_with_data() -> None:
    logs: dict[str, list[dict[str, Any]]] = {
        "fair_value_log": [
            {
                "symbol": "AAPL",
                "trading_date": "2026-10-01",
                "dcf_value": 240.0,
                "comps_value": 230.0,
                "fair_value": 235.0,
                "margin_of_safety": 0.28,
                "discount_rate": 0.085,
                "equity_risk_premium": 0.045,
                "flags_json": "[]",
            },
            {
                "symbol": "NVDA",
                "trading_date": "2026-10-01",
                "dcf_value": 150.0,
                "comps_value": 140.0,
                "fair_value": 145.0,
                "margin_of_safety": -0.15,
                "discount_rate": 0.09,
                "equity_risk_premium": 0.045,
                "flags_json": "[]",
            },
        ],
        "revision_score_log": [
            {
                "symbol": "AAPL",
                "trading_date": "2026-10-01",
                "score_30d": 45.0,
                "breadth_ratio": 0.75,
                "is_pead_aligned": True,
                "detail_json": "{}",
            },
            {
                "symbol": "NVDA",
                "trading_date": "2026-10-01",
                "score_30d": -20.0,
                "breadth_ratio": 0.30,
                "is_pead_aligned": False,
                "detail_json": "{}",
            },
        ],
        "fundamental_watch_candidate": [
            {
                "trading_date": "2026-10-01",
                "symbol": "AAPL",
                "rank": 1,
                "status": "CANDIDATE",
                "reasons_json": '["深度折價"]',
                "excluded_reason": None,
            },
            {
                "trading_date": "2026-10-01",
                "symbol": "MSFT",
                "rank": 2,
                "status": "WATCH",
                "reasons_json": '["觀察中"]',
                "excluded_reason": None,
            },
            {
                "trading_date": "2026-10-01",
                "symbol": "TSLA",
                "rank": 3,
                "status": "EXCLUDED",
                "reasons_json": "[]",
                "excluded_reason": "IVR > 80",
            },
        ],
    }

    report = generate_fundamental_forward_report(logs)
    assert "歷史掃描交易日數 : 1 天" in report
    assert "涵蓋獨立標的總數 : 4 檔" in report  # AAPL, MSFT, NVDA, TSLA
    assert "公允價值估值筆數 : 2 筆" in report
    assert "深度折價標的 (MOS >= 25%) : 1 筆" in report
    assert "溢價偏高標的 (MOS < -10%) : 1 筆" in report
    assert "DCF 模型有效覆蓋率 : 100.0%" in report
    assert "平均修正動能分數 : +12.50" in report
    assert "PEAD 共振觸發頻率 : 50.0% (1/2)" in report
    assert "CANDIDATE (優先候選) : 1 筆" in report
    assert "WATCH (觀察名單)     : 1 筆" in report
    assert "EXCLUDED (風控排除)  : 1 筆" in report
    assert "AAPL (1 次)" in report


def test_write_report(tmp_path: Path) -> None:
    logs: dict[str, list[dict[str, Any]]] = {
        "fair_value_log": [],
        "revision_score_log": [],
        "fundamental_watch_candidate": [],
    }
    target = write_report(tmp_path, logs)
    assert target.exists()
    assert target.name == "results.json"
    report_md = target.parent / "report.md"
    assert report_md.exists()
    assert "基本面管線離線前向校準報告" in report_md.read_text(encoding="utf-8")


def test_load_fundamental_logs_snapshot(tmp_path: Path) -> None:
    db_file = tmp_path / "snapshot_test.db"
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE fair_value_log (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                dcf_value REAL,
                comps_value REAL,
                fair_value REAL NOT NULL,
                margin_of_safety REAL,
                discount_rate REAL NOT NULL,
                equity_risk_premium REAL NOT NULL,
                flags_json TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE revision_score_log (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                score_30d REAL NOT NULL,
                breadth_ratio REAL NOT NULL,
                is_pead_aligned INTEGER NOT NULL,
                detail_json TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE fundamental_watch_candidate (
                trading_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                rank INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                excluded_reason TEXT
            )
            """
        )
        cur.execute(
            """
            INSERT INTO fair_value_log VALUES ('AAPL', '2026-10-01', 250.0, 240.0, 245.0, 0.20, 0.08, 0.045, '[]')
            """
        )
        conn.commit()
    finally:
        conn.close()

    logs = load_fundamental_logs(snapshot_db=db_file)
    assert len(logs["fair_value_log"]) == 1
    assert logs["fair_value_log"][0]["symbol"] == "AAPL"
    assert logs["fair_value_log"][0]["fair_value"] == 245.0
    assert len(logs["revision_score_log"]) == 0
    assert len(logs["fundamental_watch_candidate"]) == 0

    # Test run_fundamental_forward_report with snapshot_db and out_dir
    report_text = run_fundamental_forward_report(snapshot_db=db_file, out_dir=tmp_path)
    assert "AAPL" in report_text
    assert (tmp_path / "calibration").exists()


def test_report_excludes_null_and_method_none_rows() -> None:
    """method = NONE 或 NULL 公允價值 / 動能不得以 0 計入分佈與平均。"""
    logs: dict[str, list[dict[str, Any]]] = {
        "fair_value_log": [
            {
                "symbol": "MSFT",
                "trading_date": "2026-10-06",
                "dcf_value": 600.0,
                "comps_value": 560.0,
                "fair_value": 580.0,
                "margin_of_safety": 0.30,
                "method": "BLENDED",
            },
            {
                "symbol": "SPY",
                "trading_date": "2026-10-06",
                "dcf_value": None,
                "comps_value": None,
                "fair_value": None,
                "margin_of_safety": None,
                "method": "NONE",
            },
            {  # 舊版 0.0 哨兵（無 method 欄位）同樣排除
                "symbol": "QQQ",
                "trading_date": "2026-10-06",
                "dcf_value": None,
                "comps_value": None,
                "fair_value": 0.0,
                "margin_of_safety": 0.0,
                "method": None,
            },
        ],
        "revision_score_log": [
            {
                "symbol": "MSFT",
                "trading_date": "2026-10-06",
                "score_30d": 40.0,
                "is_pead_aligned": False,
            },
            {
                "symbol": "SPY",
                "trading_date": "2026-10-06",
                "score_30d": None,
                "is_pead_aligned": False,
            },
        ],
        "fundamental_watch_candidate": [],
    }
    report = generate_fundamental_forward_report(logs)
    assert "有效估值筆數 : 1 筆（排除 method = NONE 或 NULL 共 2 筆）" in report
    assert "平均安全邊際 (Mean MOS) : +30.00%" in report
    assert "深度折價標的 (MOS >= 25%) : 1 筆 (100.0%)" in report
    assert "有效動能筆數 : 1 筆（排除無可配對財期 1 筆）" in report
    assert "平均修正動能分數 : +40.00" in report


def test_load_reports_missing_tables_explicitly(tmp_path: Path) -> None:
    """缺表不默默略過：記錄到 load_errors 並在報告開頭明示。"""
    db_file = tmp_path / "empty.db"
    sqlite3.connect(str(db_file)).close()
    logs = load_fundamental_logs(snapshot_db=db_file)
    missing = {e["table"] for e in logs["load_errors"] if e["kind"] == "MISSING_TABLE"}
    assert missing == {
        "fair_value_log",
        "revision_score_log",
        "fundamental_watch_candidate",
    }
    report = generate_fundamental_forward_report(logs)
    assert "缺少資料表 fair_value_log" in report
    assert "3 項資料讀取問題" in report
