"""單元測試：公司治理審查閘門 (governance_gate.py)。"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.governance_gate import (
    create_governance_flag,
    evaluate_governance_status,
    generate_governance_flags_from_8k,
)


def test_generate_governance_flags_from_8k_critical_and_high() -> None:
    """測試從 8-K Items 中檢測出 4.02 (CRITICAL) 與 5.02 (HIGH)。"""
    flags = generate_governance_flags_from_8k(
        symbol="SMCI",
        accession="0001375365-26-000010",
        items=["4.02", "5.02"],
        accepted_at="2026-10-05T16:30:00Z",
        text_snippet="Restatement of FY2025 financial statements and resignation of CFO.",
        review_days=30,
    )

    assert len(flags) == 2
    f_map = {f.flag_kind: f for f in flags}

    # 4.02 財報重編
    assert "ITEM_4_02_RESTATEMENT" in f_map
    assert f_map["ITEM_4_02_RESTATEMENT"].severity == "CRITICAL"
    assert "SMCI" == f_map["ITEM_4_02_RESTATEMENT"].symbol

    # 5.02 高管解職
    assert "ITEM_5_02_OFFICER_DEPARTURE" in f_map
    assert f_map["ITEM_5_02_OFFICER_DEPARTURE"].severity == "HIGH"


def test_evaluate_governance_status_clean() -> None:
    """測試無生效中旗標時回傳乾淨狀態 (is_clean=True, max_severity=None)。"""
    status = evaluate_governance_status("AAPL", active_flags=[])
    assert status.is_clean is True
    assert status.max_severity is None
    assert status.active_flags == []


def test_evaluate_governance_status_with_flags() -> None:
    """測試存在多項旗標時正確仲裁出最高等級 (CRITICAL > HIGH > REVIEW > INFO)。"""
    flags = [
        create_governance_flag(
            symbol="TEST",
            source_accession="ACC-1",
            flag_kind="FLAG_A",
            severity="HIGH",
        ),
        create_governance_flag(
            symbol="TEST",
            source_accession="ACC-2",
            flag_kind="FLAG_B",
            severity="CRITICAL",
        ),
    ]

    status = evaluate_governance_status("TEST", flags)
    assert status.is_clean is False
    assert status.max_severity == "CRITICAL"
    assert len(status.active_flags) == 2


def test_item_502_without_text_downgrades_to_review() -> None:
    """只有 item code（無內文）時，5.02 無法區分離任或新任，一律降為 REVIEW。"""
    flags = generate_governance_flags_from_8k(
        symbol="AAPL",
        accession="0001140361-26-000199",
        items=["5.02"],
        accepted_at="20260102163052",
    )
    assert len(flags) == 1
    assert flags[0].flag_kind == "ITEM_5_02_OFFICER_CHANGE"
    assert flags[0].severity == "REVIEW"


def test_item_502_appointment_or_compensation_stays_review() -> None:
    """5.02(c)(d)(e) 新任 / 選任 / 薪酬協議：即使內文含 5.02 標題的 "Departure" 字樣也不升級。"""
    snippet = (
        "Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
        "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. "
        "On October 1, 2026, the Board appointed Jane Doe as Chief Financial Officer and "
        "approved her annual base salary."
    )
    flags = generate_governance_flags_from_8k(
        symbol="TEST",
        accession="ACC-APPOINT",
        items=["5.02"],
        accepted_at="20261001163000",
        text_snippet=snippet,
    )
    assert [f.severity for f in flags] == ["REVIEW"]


def test_item_502_explicit_departure_escalates_to_high() -> None:
    """內文明確指出辭職 / 退休 / 解職 (5.02(b)) 才升為 HIGH。"""
    for snippet in (
        "John Smith notified the Company of his resignation as Chief Financial Officer.",
        "The Chief Executive Officer will retire effective December 31, 2026.",
        "The Board terminated the employment of the Chief Operating Officer.",
    ):
        flags = generate_governance_flags_from_8k(
            symbol="TEST",
            accession="ACC-DEPART",
            items=["Item 5.02"],
            accepted_at="20261001163000",
            text_snippet=snippet,
        )
        assert len(flags) == 1
        assert flags[0].severity == "HIGH"
        assert flags[0].flag_kind == "ITEM_5_02_OFFICER_DEPARTURE"


def test_review_flag_does_not_count_as_high_for_exclusion() -> None:
    """REVIEW 旗標使治理狀態非乾淨，但最高嚴重度僅為 REVIEW（下游依 HIGH 排除時不受影響）。"""
    flags = generate_governance_flags_from_8k(
        symbol="AAPL",
        accession="ACC-REVIEW",
        items=["5.02"],
        accepted_at="20261001163000",
    )
    status = evaluate_governance_status("AAPL", flags)
    assert status.is_clean is False
    assert status.max_severity == "REVIEW"


def test_expires_at_is_utc_from_eastern_acceptance() -> None:
    """審查期以權威美東受理時間起算，expires_at 以 UTC 字串儲存（與 UTC as_of 比較）。

    20261005184245 (EDT, UTC-4) = 2026-10-05 22:42:45 UTC；+30 天 = 2026-11-04 22:42:45 UTC。
    """
    flags = generate_governance_flags_from_8k(
        symbol="SMCI",
        accession="ACC-EXP",
        items=["4.02"],
        accepted_at="20261005184245",
    )
    assert flags[0].expires_at == "2026-11-04 22:42:45"

    # 正規化後的美東 ISO 字串得到相同結果
    flags_iso = generate_governance_flags_from_8k(
        symbol="SMCI",
        accession="ACC-EXP",
        items=["4.02"],
        accepted_at="2026-10-05T18:42:45-04:00",
    )
    assert flags_iso[0].expires_at == "2026-11-04 22:42:45"
