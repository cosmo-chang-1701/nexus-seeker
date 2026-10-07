"""單元測試：8-K Exhibit 99.1 定位、HTML 清洗與引文溯源 (press_release.py)。

fixture 取自 SEC EDGAR 實際申報 AAPL 0000320193-26-000005（2026-01-29 FY2026 Q1 財報）：
- `0000320193-26-000005-index-headers.html`：申報目錄 SGML 表頭（完整）。
- `aapl_ex991_..._head.htm`：Exhibit 99.1 新聞稿開頭片段。
- `aapl_8k_cover_..._head.htm`：8-K 主文件（封面頁 + iXBRL 表頭）開頭片段。
"""

from __future__ import annotations

from pathlib import Path

from market_analysis.fundamental_pipeline.models import GuidanceExtraction, ToneMetric
from market_analysis.fundamental_pipeline.press_release import (
    PRESS_RELEASE_CHAR_CAP,
    FilingDocumentEntry,
    evaluate_tone_grounding,
    html_to_plain_text,
    is_quote_grounded,
    parse_index_headers_documents,
    select_press_release_document,
    truncate_for_llm,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def _read(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_parse_index_headers_lists_exhibit_99_1() -> None:
    """實際 AAPL 申報目錄可解析出 8-K 主文件與 EX-99.1 新聞稿。"""
    docs = parse_index_headers_documents(
        _read("0000320193-26-000005-index-headers.html")
    )
    by_type = {d.doc_type: d for d in docs}
    assert by_type["8-K"].filename == "aapl-20260129.htm"
    assert by_type["EX-99.1"].filename == "a8-kex991q1202612272025.htm"
    assert by_type["EX-99.1"].sequence == 2

    chosen = select_press_release_document(docs)
    assert chosen is not None
    assert chosen.filename == "a8-kex991q1202612272025.htm"


def _doc(doc_type: str, seq: int, filename: str) -> FilingDocumentEntry:
    return FilingDocumentEntry(doc_type=doc_type, sequence=seq, filename=filename)


def test_select_press_release_priority_and_rejection() -> None:
    main = _doc("8-K", 1, "main.htm")
    # EX-99.1 優先於 EX-99.2
    assert select_press_release_document(
        [main, _doc("EX-99.2", 3, "cfo.htm"), _doc("EX-99.1", 2, "pr.htm")]
    ) == _doc("EX-99.1", 2, "pr.htm")
    # 次選 EX-99.01 / EX-99
    assert select_press_release_document([main, _doc("EX-99", 2, "pr.htm")]) == _doc(
        "EX-99", 2, "pr.htm"
    )
    # 只有其他 EX-99.x 時取序號最小者
    assert select_press_release_document(
        [main, _doc("EX-99.3", 4, "c.htm"), _doc("EX-99.2", 3, "b.htm")]
    ) == _doc("EX-99.2", 3, "b.htm")
    # 非文字檔不採用；沒有 EX-99 時回傳 None（不得退回 8-K 封面）
    assert select_press_release_document([main, _doc("EX-99.1", 2, "pr.pdf")]) is None
    assert select_press_release_document([main]) is None


def test_html_to_plain_text_press_release() -> None:
    """新聞稿去除 SGML 外殼、樣式與標籤後保留可讀正文。"""
    text = html_to_plain_text(_read("aapl_ex991_0000320193-26-000005_head.htm"))
    assert "Apple reports first quarter results" in text
    assert "fiscal 2026 first quarter ended December 27, 2025" in text
    assert "<" not in text
    assert "font-family" not in text
    assert "&#8212;" not in text  # HTML 實體已解碼
    assert "<TYPE>" not in text and "a8-kex991q1202612272025.htm" not in text


def test_html_to_plain_text_strips_ixbrl_header_from_8k_cover() -> None:
    """8-K 主文件開頭為 iXBRL 隱藏表頭，清洗後只剩封面文字（不含任何指引內容）。"""
    raw = _read("aapl_8k_cover_0000320193-26-000005_head.htm")
    assert "<ix:header>" in raw
    # 修正前：直接截斷原始 HTML 的前 15000 字元幾乎全是表頭與樣式
    assert "xbrli:context" in raw[:PRESS_RELEASE_CHAR_CAP]
    text = html_to_plain_text(raw)
    assert "xbrli" not in text
    assert "0000320193" not in text  # 表頭內的 CIK 識別碼已移除
    assert "FORM 8-K" in text


def test_truncate_after_cleaning() -> None:
    long_text = "a" * (PRESS_RELEASE_CHAR_CAP + 500)
    assert len(truncate_for_llm(long_text)) == PRESS_RELEASE_CHAR_CAP


_SOURCE = (
    "“Today, Apple is proud to report a remarkable, record-breaking quarter,” "
    "said Tim Cook.\nWe expect   supply constraints on memory to persist into the "
    "March quarter."
)


def test_is_quote_grounded_tolerates_quotes_case_and_whitespace() -> None:
    assert is_quote_grounded(
        '"today, Apple is proud to report a remarkable, record-breaking quarter"',
        _SOURCE,
    )
    assert is_quote_grounded(
        "We expect supply constraints on memory ... into the March quarter", _SOURCE
    )
    assert not is_quote_grounded("Demand for iPhone collapsed in China", _SOURCE)
    assert not is_quote_grounded("", _SOURCE)
    assert not is_quote_grounded("需求強勁", _SOURCE)  # 翻譯過的引文不算原文依據


def _extraction(
    backlog: tuple[int, str], supply: tuple[int, str]
) -> GuidanceExtraction:
    return GuidanceExtraction(
        symbol="AAPL",
        fiscal_period="2026-Q1",
        backlog_tone=ToneMetric(score=backlog[0], quote_snippet=backlog[1]),
        pricing_power_tone=ToneMetric(score=0, quote_snippet=""),
        supply_chain_tone=ToneMetric(score=supply[0], quote_snippet=supply[1]),
        defensive_posture_tone=ToneMetric(score=0, quote_snippet=""),
        reasoning_traditional_chinese="測試",
    )


def test_evaluate_tone_grounding() -> None:
    """非零分維度必須有原文引文；0 分且無引文視為無訊號，可接受。"""
    ok = _extraction(
        (2, "Apple is proud to report a remarkable, record-breaking quarter"),
        (-1, "supply constraints on memory to persist"),
    )
    report = evaluate_tone_grounding(ok, _SOURCE)
    assert report.is_acceptable
    assert report.grounded == ("backlog_tone", "supply_chain_tone")
    assert set(report.neutral_without_evidence) == {
        "pricing_power_tone",
        "defensive_posture_tone",
    }

    fabricated = _extraction((2, "Backlog hit an all-time record"), (0, ""))
    bad = evaluate_tone_grounding(fabricated, _SOURCE)
    assert not bad.is_acceptable
    assert bad.ungrounded_directional == ("backlog_tone",)
