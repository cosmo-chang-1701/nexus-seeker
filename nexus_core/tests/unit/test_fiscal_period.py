"""單元測試：財報期別正規化 (fiscal_period.py)。"""

from __future__ import annotations

import pytest
from market_analysis.fundamental_pipeline.fiscal_period import (
    format_fiscal_quarter,
    normalize_fiscal_period,
    normalize_guidance_period,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-Q3", "2026-Q3"),
        ("2026Q3", "2026-Q3"),
        ("2026 q3", "2026-Q3"),
        ("Q3 2026", "2026-Q3"),
        ("Q3'26", "2026-Q3"),
        ("FY2026 Q3", "2026-Q3"),
        ("Q3 FY26", "2026-Q3"),
        ("3Q26", "2026-Q3"),
        ("3Q FY2026", "2026-Q3"),
        ("fiscal 2026 third quarter", "2026-Q3"),
        ("Third quarter of fiscal 2026", "2026-Q3"),
        ("Q4 FY2025", "2025-Q4"),
    ],
)
def test_normalize_fiscal_period_variants(raw: str, expected: str) -> None:
    """LLM 常見的季度寫法皆正規化為 YYYY-Qn，確保主鍵與字串排序一致。"""
    assert normalize_fiscal_period(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "Q3", "first quarter", "2026-Q5", "FY2026", "2026", "N/A"]
)
def test_normalize_fiscal_period_rejects_ambiguous(raw: str | None) -> None:
    """缺年份、缺季度或季度越界時回傳 None，不臆測。"""
    assert normalize_fiscal_period(raw) is None


def test_normalized_periods_sort_chronologically() -> None:
    """正規化後之字串序即時間序（跨年亦然）。"""
    raws = ["Q1 2027", "FY2026 Q4", "3Q26", "2026-Q2"]
    normalized = sorted(p for p in (normalize_fiscal_period(r) for r in raws) if p)
    assert normalized == ["2026-Q2", "2026-Q3", "2026-Q4", "2027-Q1"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FY2026", "2026-FY"),
        ("FY26", "2026-FY"),
        ("full year 2026", "2026-FY"),
        ("fiscal year 2027", "2027-FY"),
        ("2026-FY", "2026-FY"),
        ("Q1 FY2027", "2027-Q1"),
        ("2026", None),
        ("next quarter", None),
        (None, None),
    ],
)
def test_normalize_guidance_period(raw: str | None, expected: str | None) -> None:
    assert normalize_guidance_period(raw) == expected


def test_format_fiscal_quarter_bounds() -> None:
    assert format_fiscal_quarter(2026, 1) == "2026-Q1"
    assert format_fiscal_quarter(2026, 0) is None
    assert format_fiscal_quarter(2026, 5) is None
