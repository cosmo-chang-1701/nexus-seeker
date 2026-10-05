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
