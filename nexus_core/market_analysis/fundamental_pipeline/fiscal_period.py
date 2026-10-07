"""財報期別字串正規化工具 (Fiscal Period Normalization)。

`earnings_surprise` 與 `guidance_extraction` 以 `(symbol, fiscal_period)` 為主鍵，並以
字串排序取得最新 / 前期記錄，因此期別一律正規化為 `YYYY-Qn`（財年 + 財季）。

LLM 自由輸出的期別（"Q3 2026"、"FY2026 Q3"、"3Q26"、"fiscal 2026 third quarter"…）
只作為參考；權威期別由 Finnhub 財報日曆 / 歷史業績（year + quarter 欄位）推導。
"""

from __future__ import annotations

import re

_CANONICAL_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_ORDINAL_QUARTERS: dict[str, int] = {
    "FIRST": 1,
    "1ST": 1,
    "SECOND": 2,
    "2ND": 2,
    "THIRD": 3,
    "3RD": 3,
    "FOURTH": 4,
    "4TH": 4,
}
# Q3 / Q3'26 / Q3FY26 等「Q 在前」寫法
_Q_PREFIX_RE = re.compile(r"(?<![A-Z0-9])Q([1-4])(?![0-9])")
# 3Q / 3Q26 / 3QFY26 等「數字在前」寫法
_Q_SUFFIX_RE = re.compile(r"(?<![A-Z0-9])([1-4])Q(?![A-Z])")
_ORDINAL_RE = re.compile(
    r"\b(FIRST|SECOND|THIRD|FOURTH|1ST|2ND|3RD|4TH)\s+(?:FISCAL\s+)?QUARTER\b"
)
_YEAR4_RE = re.compile(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])")
# 兩位數年份僅接受明確前綴（FY26、'26）或緊接季度記號（3Q26、Q3 26）之寫法
_YEAR2_PREFIXED_RE = re.compile(r"(?:FY|FISCAL\s+(?:YEAR\s+)?|')\s*'?(\d{2})(?![0-9])")
_YEAR2_AFTER_Q_RE = re.compile(r"(?:Q[1-4]|[1-4]Q)\s*'?(\d{2})(?![0-9])")
_ANNUAL_HINT_RE = re.compile(r"\b(FY|FISCAL|FULL[\s-]*YEAR|ANNUAL|YEAR)\b|FY\d")


def _extract_quarter(text: str) -> int | None:
    m = _Q_PREFIX_RE.search(text)
    if m:
        return int(m.group(1))
    m = _Q_SUFFIX_RE.search(text)
    if m:
        return int(m.group(1))
    m = _ORDINAL_RE.search(text)
    if m:
        return _ORDINAL_QUARTERS[m.group(1)]
    return None


def _extract_year(text: str) -> int | None:
    m = _YEAR4_RE.search(text)
    if m:
        return int(m.group(1))
    m = _YEAR2_PREFIXED_RE.search(text)
    if m:
        return 2000 + int(m.group(1))
    m = _YEAR2_AFTER_Q_RE.search(text)
    if m:
        return 2000 + int(m.group(1))
    return None


def format_fiscal_quarter(year: int, quarter: int) -> str | None:
    """以財年與財季組出 `YYYY-Qn`；數值不合理時回傳 None。"""
    if not (1900 <= year <= 2999) or not (1 <= quarter <= 4):
        return None
    return f"{year}-Q{quarter}"


def normalize_fiscal_period(raw: str | None) -> str | None:
    """容錯解析各種季度寫法並正規化為 `YYYY-Qn`；無法同時辨識年份與季度時回傳 None。

    支援：`2026-Q3`、`2026Q3`、`Q3 2026`、`Q3'26`、`FY2026 Q3`、`Q3 FY26`、`3Q26`、
    `3Q FY2026`、`fiscal 2026 third quarter`、`third quarter of fiscal 2026`。
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None

    canonical = _CANONICAL_QUARTER_RE.match(text)
    if canonical:
        return format_fiscal_quarter(int(canonical.group(1)), int(canonical.group(2)))

    # 2026Q3 / 2026 Q3 / 2026-Q3 之年份在前寫法
    year_first = re.search(
        r"(?<![0-9])((?:19|20)\d{2})\s*[-/ ]?\s*Q([1-4])(?![0-9])", text
    )
    if year_first:
        return format_fiscal_quarter(int(year_first.group(1)), int(year_first.group(2)))

    quarter = _extract_quarter(text)
    if quarter is None:
        return None
    year = _extract_year(text)
    if year is None:
        return None
    return format_fiscal_quarter(year, quarter)


def normalize_guidance_period(raw: str | None) -> str | None:
    """正規化指引所針對之目標期別：季度回傳 `YYYY-Qn`，全年度回傳 `YYYY-FY`。

    無法辨識年份，或既非季度亦無全年度字樣時回傳 None（視為期別不明，不可比較）。
    """
    if raw is None:
        return None
    quarter_form = normalize_fiscal_period(raw)
    if quarter_form is not None:
        return quarter_form

    text = str(raw).strip().upper()
    if not text:
        return None
    m = re.match(r"^(\d{4})-FY$", text)
    if m:
        return f"{m.group(1)}-FY"
    if _extract_quarter(text) is not None:
        return None  # 有季度記號但年份不明
    if not _ANNUAL_HINT_RE.search(text):
        return None
    year = _extract_year(text)
    if year is None or not (1900 <= year <= 2999):
        return None
    return f"{year}-FY"
