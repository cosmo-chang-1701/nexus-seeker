"""單元測試：SEC 申報項目路由與時段分類器 (sec_item_router.py)。"""

from __future__ import annotations

from datetime import datetime, timezone

from market_analysis.fundamental_pipeline.sec_item_router import (
    classify_filing_session,
    extract_8k_items,
    route_filing,
)


def test_classify_filing_session_iso_utc() -> None:
    """測試 UTC ISO 時間正確映射至美東時區的四個交易時段。"""
    # 1. BMO 盤前: 美東 04:00 <= ET < 09:30 (對應夏令 UTC 08:00 - 13:30)
    # 2026-10-05 08:30:00 UTC -> 04:30:00 ET (BMO)
    assert classify_filing_session("2026-10-05T08:30:00Z") == "BMO"

    # 2. RTH 常規交易: 美東 09:30 <= ET < 16:00 (對應夏令 UTC 13:30 - 20:00)
    # 2026-10-05 14:00:00 UTC -> 10:00:00 ET (RTH)
    assert classify_filing_session("2026-10-05T14:00:00Z") == "RTH"

    # 3. AMC 盤後: 美東 16:00 <= ET < 20:00 (對應夏令 UTC 20:00 - 00:00)
    # 2026-10-05 20:45:00 UTC -> 16:45:00 ET (AMC)
    assert classify_filing_session("2026-10-05T20:45:00Z") == "AMC"

    # 4. OVERNIGHT 夜間: 美東 20:00 <= ET < 04:00
    # 2026-10-05 01:00:00 UTC -> 21:00:00 ET 前一日 (OVERNIGHT)
    assert classify_filing_session("2026-10-05T01:00:00Z") == "OVERNIGHT"


def test_classify_filing_session_compact_and_datetime() -> None:
    """測試緊湊字串 (YYYYMMDDHHMMSS) 與原生 datetime 物件。"""
    # 20261005204500 (UTC 20:45) -> ET 16:45 (AMC)
    assert classify_filing_session("20261005204500") == "AMC"

    dt_utc = datetime(2026, 10, 5, 14, 30, 0, tzinfo=timezone.utc)
    assert classify_filing_session(dt_utc) == "RTH"


def test_extract_8k_items() -> None:
    """測試從各種格式字串提取 8-K Items 代號。"""
    assert extract_8k_items("Item 2.02, Item 4.02") == ["2.02", "4.02"]
    assert extract_8k_items("2.02, 5.02, 1.01") == ["1.01", "2.02", "5.02"]
    assert extract_8k_items(["Item 2.05", "4.02"]) == ["2.05", "4.02"]
    assert extract_8k_items(None) == []
    assert extract_8k_items("") == []


def test_route_filing_form4() -> None:
    """測試 Form 4 分流。"""
    assert route_filing("4") == ["INSIDER_TRANSACTION"]
    assert route_filing("4/A") == ["INSIDER_TRANSACTION"]


def test_route_filing_schedules() -> None:
    """測試 Schedule 13D/G 分流。"""
    assert route_filing("SC 13D") == ["ACTIVIST_13D"]
    assert route_filing("SC 13D/A") == ["ACTIVIST_13D"]
    assert route_filing("SC 13G") == ["PASSIVE_13G"]


def test_route_filing_8k_items() -> None:
    """測試 8-K 依據 Items 分流至對應管線。"""
    # 2.02 財報預期差
    assert route_filing("8-K", "Item 2.02") == ["EARNINGS"]
    # 4.02 財報重編治理審查
    assert route_filing("8-K", "Item 4.02") == ["GOVERNANCE_CRITICAL"]
    # 5.02 高管異動
    assert route_filing("8-K", "Item 5.02") == ["GOVERNANCE_HIGH"]
    # 多項目複合
    routes = route_filing("8-K", "Item 2.02, Item 4.02")
    assert "EARNINGS" in routes
    assert "GOVERNANCE_CRITICAL" in routes
    # 無匹配項目但為 8-K
    assert route_filing("8-K", "Item 9.01") == ["FORM_8K"]
    # 未知表單類型
    assert route_filing("13F-HR") == ["OTHER"]
    # 大小寫與空白容錯
    assert route_filing("  form 4  ") == ["INSIDER_TRANSACTION"]
    assert route_filing("8-k", ["2.05", "1.01"]) == [
        "MATERIAL_AGREEMENT",
        "RESTRUCTURING",
    ]
