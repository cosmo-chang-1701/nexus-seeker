"""單元測試：SEC 申報項目路由與時段分類器 (sec_item_router.py)。"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from market_analysis.fundamental_pipeline.sec_item_router import (
    SEC_JSON_ACCEPTANCE_MAX_SKEW,
    classify_filing_session,
    extract_8k_items,
    parse_sec_acceptance_datetime,
    parse_sec_header_acceptance,
    route_filing,
)

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "sec"


def _load_samples() -> list[dict[str, str]]:
    raw = json.loads((_FIXTURE_DIR / "submissions_acceptance_samples.json").read_text())
    samples: list[dict[str, str]] = raw["samples"]
    return samples


def test_compact_sgml_timestamp_is_eastern_wall_clock() -> None:
    """SGML 表頭緊湊格式為美東牆上時間（非 UTC）。

    實測：AAPL Form 4 0001140361-26-038674 之 <ACCEPTANCE-DATETIME>20261005184245，
    EDGAR index 頁 Accepted 欄為 2026-10-05 18:42:45 → 美東 18:42:45 (AMC)。
    """
    dt = parse_sec_acceptance_datetime("20261005184245")
    assert dt.tzinfo is not None
    assert dt.replace(tzinfo=None) == datetime(2026, 10, 5, 18, 42, 45)
    assert dt.utcoffset() == timedelta(hours=-4)
    assert classify_filing_session("20261005184245") == "AMC"
    # 冬令：AAPL 2026-01-29 財報 8-K 16:30:33 ET (EST, UTC-5)
    winter = parse_sec_acceptance_datetime("20260129163033")
    assert winter.utcoffset() == timedelta(hours=-5)
    assert classify_filing_session("20260129163033") == "AMC"
    # 盤前：AAPL 10-Q 06:01:02 ET、AMZN 8-A12B 08:30:49 ET
    assert classify_filing_session("20260731060102") == "BMO"
    assert classify_filing_session("20260924083049") == "BMO"


def test_naive_index_page_format_is_eastern_wall_clock() -> None:
    """EDGAR index 頁 Accepted 欄（無時區 'YYYY-MM-DD HH:MM:SS'）同為美東牆上時間。"""
    assert classify_filing_session("2026-10-05 18:42:45") == "AMC"
    assert classify_filing_session("2026-07-31 06:01:02") == "BMO"
    # 無時區 datetime 亦視為美東牆上時間
    assert classify_filing_session(datetime(2026, 10, 5, 10, 0, 0)) == "RTH"


def test_explicit_offsets_are_honoured() -> None:
    """帶明確時區的字串 / datetime 依其時區換算（含正規化後儲存的美東 ISO）。"""
    assert classify_filing_session("2026-10-05T18:42:45-04:00") == "AMC"
    assert classify_filing_session("2026-10-05T14:00:00Z") == "RTH"  # 10:00 ET
    dt_utc = datetime(2026, 10, 5, 14, 30, 0, tzinfo=timezone.utc)
    assert classify_filing_session(dt_utc) == "RTH"
    # 時段邊界
    assert classify_filing_session("2026-10-05T04:00:00-04:00") == "BMO"
    assert classify_filing_session("2026-10-05T09:30:00-04:00") == "RTH"
    assert classify_filing_session("2026-10-05T16:00:00-04:00") == "AMC"
    assert classify_filing_session("2026-10-05T20:00:00-04:00") == "OVERNIGHT"
    assert classify_filing_session("2026-10-05T03:59:59-04:00") == "OVERNIGHT"


@pytest.mark.parametrize(
    "filename,expected_et",
    [
        ("0001140361-26-038674.hdr.sgml", datetime(2026, 10, 5, 18, 42, 45)),
        ("0000320193-26-000005.hdr.sgml", datetime(2026, 1, 29, 16, 30, 33)),
        ("0000320193-26-000020.hdr.sgml", datetime(2026, 7, 31, 6, 1, 2)),
        ("0001104659-26-110227.hdr.sgml", datetime(2026, 9, 24, 8, 30, 49)),
        ("0001193125-26-380280.hdr.sgml", datetime(2026, 9, 2, 16, 30, 24)),
    ],
)
def test_parse_sec_header_acceptance_real_fixtures(
    filename: str, expected_et: datetime
) -> None:
    """真實 .hdr.sgml 表頭可擷取權威美東受理時間。"""
    text = (_FIXTURE_DIR / filename).read_text()
    dt = parse_sec_header_acceptance(text)
    assert dt.replace(tzinfo=None) == expected_et
    assert dt.tzinfo is not None


def test_parse_sec_header_acceptance_missing_tag_raises() -> None:
    with pytest.raises(ValueError):
        parse_sec_header_acceptance("<SEC-HEADER>\n<TYPE>4\n")


def test_submissions_json_acceptance_is_only_an_upper_bound() -> None:
    """真實樣本：submissions JSON 的 acceptanceDateTime 不是可靠的 UTC。

    - AAPL（全部）與 AMZN 舊筆：JSON = 真實 UTC + 美東偏移（夏令 +4h、冬令 +5h）。
    - MSFT、AMZN 最新筆：JSON = 真實 UTC。
    因此 JSON 值只能當作真實時間的上界，偏差介於 0 與 SEC_JSON_ACCEPTANCE_MAX_SKEW 之間。
    """
    skews: dict[str, timedelta] = {}
    for sample in _load_samples():
        true_et = parse_sec_acceptance_datetime(sample["sgmlAcceptanceDateTimeET"])
        json_dt = parse_sec_acceptance_datetime(sample["acceptanceDateTime"])
        skew = json_dt - true_et
        assert timedelta(0) <= skew <= SEC_JSON_ACCEPTANCE_MAX_SKEW
        skews[sample["accessionNumber"]] = skew

    # 偏差型樣本（AAPL 夏令 / 冬令、AMZN 舊筆）
    assert skews["0001140361-26-038674"] == timedelta(hours=4)
    assert skews["0000320193-26-000020"] == timedelta(hours=4)
    assert skews["0000320193-26-000005"] == timedelta(hours=5)
    assert skews["0001104659-26-110227"] == timedelta(hours=4)
    # 正確型樣本（MSFT 夏令 / 冬令、AMZN 最新筆）
    assert skews["0001193125-26-380280"] == timedelta(0)
    assert skews["0001193125-26-027207"] == timedelta(0)
    assert skews["0001595602-26-000009"] == timedelta(0)


def test_json_acceptance_would_misclassify_sessions() -> None:
    """把 JSON 值當 UTC 會把盤前申報誤判為盤中——這是改讀 SGML 表頭的原因。"""
    by_acc = {s["accessionNumber"]: s for s in _load_samples()}
    aapl_10q = by_acc["0000320193-26-000020"]
    assert classify_filing_session(aapl_10q["acceptanceDateTime"]) == "RTH"
    assert classify_filing_session(aapl_10q["sgmlAcceptanceDateTimeET"]) == "BMO"

    amzn_8a = by_acc["0001104659-26-110227"]
    assert classify_filing_session(amzn_8a["acceptanceDateTime"]) == "RTH"
    assert classify_filing_session(amzn_8a["sgmlAcceptanceDateTimeET"]) == "BMO"

    aapl_form4 = by_acc["0001140361-26-038674"]
    assert classify_filing_session(aapl_form4["acceptanceDateTime"]) == "OVERNIGHT"
    assert classify_filing_session(aapl_form4["sgmlAcceptanceDateTimeET"]) == "AMC"


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
    # 5.02 高管異動（路由鍵；嚴重度由 governance_gate 判定）
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
