"""基本面管線離線前向統計分析工具單元測試。"""

import sqlite3
from pathlib import Path
from typing import Any

import json

from calibration.fundamental_forward_report import (
    build_valuation_robustness,
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


def _fv(
    symbol: str,
    date: str,
    dcf: float | None,
    comps: float | None,
    spot: float,
    mos: float,
    detail: dict[str, Any] | None = None,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trading_date": date,
        "dcf_value": dcf,
        "comps_value": comps,
        "fair_value": spot / (1 - mos),
        "margin_of_safety": mos,
        "discount_rate": 0.1,
        "equity_risk_premium": 0.045,
        "flags_json": json.dumps(flags or []),
        "method": "BLENDED",
        "spot_price": spot,
        "comps_detail_json": json.dumps(detail) if detail is not None else None,
    }


def _cand(symbol: str, date: str) -> dict[str, Any]:
    return {
        "trading_date": date,
        "symbol": symbol,
        "rank": 1,
        "status": "CANDIDATE",
        "reasons_json": "{}",
        "excluded_reason": None,
    }


def test_valuation_robustness_median_quartiles_per_method_and_candidates() -> None:
    fv = [
        # MU：DCF 1120 / Comps 5353（相差 > 3 倍），DCF 單獨 MOS = 15%（未達 25%）
        _fv(
            "MU",
            "2026-10-13",
            1120.0,
            5353.0,
            953.0,
            0.7,
            detail={
                "median_pe": 34.87,
                "forward_eps": 146.1,
                "peers": [{"symbol": "NVDA", "pe": 17.0, "trailing": False}],
            },
        ),
        # SNDK：DCF 單獨 MOS ≈ 16.5%，比值 < 3、無 comps_detail_json（舊資料）
        _fv("SNDK", "2026-10-13", 1776.0, 3416.0, 1482.0, 0.43),
        # DCF 單獨即 >= 25% 的穩健候選
        _fv("ROB", "2026-10-13", 200.0, 220.0, 100.0, 0.52),
        _fv("OVR", "2026-10-13", 50.0, 60.0, 100.0, -0.8),
    ]
    watch = [
        _cand("MU", "2026-10-13"),
        _cand("SNDK", "2026-10-13"),
        _cand("ROB", "2026-10-13"),
    ]
    res = build_valuation_robustness(fv, watch)

    # 中位數 = (0.43 + 0.52) / 2；平均會被 -0.8 拉低
    mos = res["mos"]
    assert abs(mos["median"] - 0.475) < 1e-9
    assert mos["p25"] < mos["median"] < mos["p75"]
    assert mos["mean"] < mos["median"]

    dcf = res["per_method"]["dcf"]
    assert dcf["n"] == 4
    assert dcf["n_ge_deep_value"] == 1  # 只有 ROB（50%）
    comps = res["per_method"]["comps"]
    assert comps["n"] == 4
    assert comps["n_ge_deep_value"] == 3

    cr = res["candidate_robustness"]
    assert cr["n_candidates"] == 3 and cr["n_matched"] == 3
    assert cr["dcf_only_still_deep_value"] == 1
    assert (
        cr["valuation_divergence"] == 1
    )  # 只有 MU（4.78 倍）；SNDK 1.92 倍、ROB 1.1 倍不算

    by_sym = {d["symbol"]: d for d in res["candidate_comps_detail"]}
    assert by_sym["MU"]["median_pe"] == 34.87
    assert by_sym["MU"]["peers"] == ["NVDA"]
    assert by_sym["SNDK"]["median_pe"] is None and by_sym["SNDK"]["peers"] == []


def test_valuation_robustness_handles_legacy_rows_without_spot_or_detail() -> None:
    """舊快照無 spot_price / comps_detail_json / method：現價由 FV 與 MOS 反推，不丟例外。"""
    row = {
        "symbol": "AAPL",
        "trading_date": "2026-10-01",
        "dcf_value": 240.0,
        "comps_value": 230.0,
        "fair_value": 250.0,
        "margin_of_safety": 0.2,
        "flags_json": "[]",
    }
    res = build_valuation_robustness([row], [_cand("AAPL", "2026-10-01")])
    assert res["per_method"]["dcf"]["n"] == 1
    # spot = 250 * 0.8 = 200 → DCF MOS = (240-200)/240
    assert abs(res["per_method"]["dcf"]["distribution"]["median"] - 40 / 240) < 1e-9
    assert res["candidate_comps_detail"][0]["median_pe"] is None
    assert build_valuation_robustness([], [])["mos"] is None


def test_valuation_divergence_counts_flag_or_ratio() -> None:
    """帶旗標或比值 >= 3 皆計入；比值不足且無旗標不計。"""
    fv = [
        _fv("A", "2026-10-13", 100.0, 150.0, 80.0, 0.3, flags=["VALUATION_DIVERGENCE"]),
        _fv("B", "2026-10-13", 100.0, 400.0, 80.0, 0.3),
        _fv("C", "2026-10-13", 100.0, 150.0, 80.0, 0.3),
    ]
    watch = [_cand(s, "2026-10-13") for s in ("A", "B", "C")]
    assert (
        build_valuation_robustness(fv, watch)["candidate_robustness"][
            "valuation_divergence"
        ]
        == 2
    )


def test_report_includes_new_sections_and_old_data_has_no_error() -> None:
    fv = [_fv("MU", "2026-10-13", 1120.0, 5353.0, 953.0, 0.7)]
    logs: dict[str, list[dict[str, Any]]] = {
        "fair_value_log": fv,
        "revision_score_log": [],
        "fundamental_watch_candidate": [_cand("MU", "2026-10-13")],
    }
    report = generate_fundamental_forward_report(logs)
    assert "中位數安全邊際" in report
    assert "受極端值主導，僅供參考" in report
    assert "各估值法單獨計算的 MOS" in report
    assert "DCF_ONLY 假設下仍 >= 25% : 0/1" in report
    assert "相差 3 倍以上 : 1/1" in report
    assert "MU: —" in report


def test_load_logs_pre_v095_snapshot_falls_back_without_comps_detail(
    tmp_path: Path,
) -> None:
    """快照有 method / spot_price 但無 comps_detail_json（v095 前）也能讀。"""
    db_file = tmp_path / "pre_v095.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            """
            CREATE TABLE fair_value_log (
                symbol TEXT, trading_date TEXT, dcf_value REAL, comps_value REAL,
                fair_value REAL, margin_of_safety REAL, discount_rate REAL,
                equity_risk_premium REAL, flags_json TEXT, method TEXT, spot_price REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO fair_value_log VALUES "
            "('MU','2026-10-13',1120,5353,3236,0.7,0.13,0.04,'[]','BLENDED',953)"
        )
        conn.commit()
    finally:
        conn.close()
    logs = load_fundamental_logs(snapshot_db=db_file)
    row = logs["fair_value_log"][0]
    assert row["method"] == "BLENDED" and row["spot_price"] == 953
    assert row["comps_detail_json"] is None
