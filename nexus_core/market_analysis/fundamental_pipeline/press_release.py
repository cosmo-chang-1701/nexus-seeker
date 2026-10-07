"""8-K 財報新聞稿（Exhibit 99.1）定位、純文字清洗與語意引文溯源工具。

背景：8-K Item 2.02 的主文件（primaryDocument）只是封面頁與 iXBRL 表頭，真正的
財報新聞稿與管理層前瞻指引在附件 Exhibit 99.1。附件清單取自申報目錄的
`{accession}-index-headers.html`（完整 SGML 表頭，HTML 跳脫後列出每份 <DOCUMENT> 的
TYPE / SEQUENCE / FILENAME），約 6KB。

本模組為純函式（無 I/O），由 services.earnings_surprise_service 呼叫。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from market_analysis.fundamental_pipeline.models import GuidanceExtraction

PRESS_RELEASE_CHAR_CAP: int = 15_000  # 送入 LLM 之清洗後純文字上限（字元）
INDEX_HEADERS_BYTE_CAP: int = 262_144  # index-headers.html 下載上限（實測約 6KB）

_TEXT_EXTENSIONS: tuple[str, ...] = (".htm", ".html", ".txt")
_DOCUMENT_BLOCK_RE = re.compile(r"<DOCUMENT>(.*?)(?=<DOCUMENT>|\Z)", re.S | re.I)
_SGML_FIELD_RE = {
    "type": re.compile(r"<TYPE>\s*([^\s<]+)", re.I),
    "sequence": re.compile(r"<SEQUENCE>\s*([^\s<]+)", re.I),
    "filename": re.compile(r"<FILENAME>\s*([^\s<]+)", re.I),
    "description": re.compile(r"<DESCRIPTION>\s*([^\n<]*)", re.I),
}


@dataclass(frozen=True)
class FilingDocumentEntry:
    """申報目錄中的單一文件條目。"""

    doc_type: str
    sequence: int | None
    filename: str
    description: str = ""


def parse_index_headers_documents(index_headers_text: str) -> list[FilingDocumentEntry]:
    """解析 `{accession}-index-headers.html`，列出申報內每份文件之類型與檔名。"""
    text = html.unescape(index_headers_text or "")
    entries: list[FilingDocumentEntry] = []
    for block_match in _DOCUMENT_BLOCK_RE.finditer(text):
        block = block_match.group(1)
        type_m = _SGML_FIELD_RE["type"].search(block)
        file_m = _SGML_FIELD_RE["filename"].search(block)
        if not type_m or not file_m:
            continue
        seq_m = _SGML_FIELD_RE["sequence"].search(block)
        desc_m = _SGML_FIELD_RE["description"].search(block)
        sequence: int | None = None
        if seq_m:
            try:
                sequence = int(seq_m.group(1))
            except ValueError:
                sequence = None
        entries.append(
            FilingDocumentEntry(
                doc_type=type_m.group(1).strip().upper(),
                sequence=sequence,
                filename=file_m.group(1).strip(),
                description=desc_m.group(1).strip() if desc_m else "",
            )
        )
    return entries


def select_press_release_document(
    documents: list[FilingDocumentEntry],
) -> FilingDocumentEntry | None:
    """自申報文件清單選出財報新聞稿附件。

    優先序：`EX-99.1` → `EX-99.01` / `EX-99` → 其他 `EX-99.*` 中序號最小者；
    僅接受 .htm / .html / .txt 文字檔。找不到時回傳 None（不退回 8-K 封面主文件）。
    """
    text_docs = [d for d in documents if d.filename.lower().endswith(_TEXT_EXTENSIONS)]
    for wanted in (("EX-99.1",), ("EX-99.01", "EX-99")):
        for d in text_docs:
            if d.doc_type in wanted:
                return d
    ex99 = [d for d in text_docs if d.doc_type.startswith("EX-99")]
    if not ex99:
        return None
    return min(ex99, key=lambda d: d.sequence if d.sequence is not None else 10_000)


def html_to_plain_text(raw: str) -> str:
    """去除 HTML / iXBRL 標記，回傳可供 LLM 閱讀之純文字。

    依序移除：EDGAR SGML 外殼（<DOCUMENT>…<TEXT>）、`<ix:header>` 隱藏 XBRL 表頭、
    <head> / <style> / <script> / <title>、HTML 註解；區塊級標籤轉換行後剝除全部標籤，
    再解碼 HTML 實體並壓縮空白。
    """
    if not raw:
        return ""
    text = re.sub(r"(?is)^\s*<DOCUMENT>.*?<TEXT>", " ", raw, count=1)
    text = re.sub(r"(?is)</TEXT>\s*</DOCUMENT>\s*$", " ", text)
    text = re.sub(r"(?is)<ix:header\b.*?</ix:header\s*>", " ", text)
    text = re.sub(r"(?is)<(head|style|script|title)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(
        r"(?is)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|section|ul|ol)\s*>", "\n", text
    )
    text = re.sub(r"(?is)</t[dh]\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip()


def truncate_for_llm(text: str, char_cap: int = PRESS_RELEASE_CHAR_CAP) -> str:
    """清洗後純文字之長度截斷（先清洗、後截斷）。"""
    return text[:char_cap]


# ============================================================================
# 語意引文溯源 (Quote Grounding)
# ============================================================================

_QUOTE_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)
_ELLIPSIS_SPLIT_RE = re.compile(r"\.{3,}|…")
_MIN_SEGMENT_CHARS = 8


def _normalize_for_match(text: str) -> str:
    lowered = text.translate(_QUOTE_TRANSLATION).lower()
    return re.sub(r"\s+", " ", lowered).strip()


def is_quote_grounded(quote: str, source_text: str) -> bool:
    """檢查 LLM 引文是否逐字出現於原文（忽略大小寫、空白與彎引號差異）。

    引文以刪節號 (`...` / `…`) 分段，各段（去除引號後至少 8 字元者）都必須出現在原文。
    """
    if not quote or not quote.strip():
        return False
    normalized_source = _normalize_for_match(source_text)
    segments = [
        _normalize_for_match(seg).strip(" \"'")
        for seg in _ELLIPSIS_SPLIT_RE.split(quote)
    ]
    meaningful = [seg for seg in segments if len(seg) >= _MIN_SEGMENT_CHARS]
    if not meaningful:
        return False
    return all(seg in normalized_source for seg in meaningful)


@dataclass(frozen=True)
class ToneGroundingReport:
    """四維態度評分之原文溯源結果。"""

    grounded: tuple[str, ...]  # 引文可在原文找到之維度
    ungrounded_directional: tuple[str, ...]  # 非零分但引文無法溯源（編造風險）
    neutral_without_evidence: tuple[str, ...]  # 0 分且無可溯源引文（無訊號）

    @property
    def is_acceptable(self) -> bool:
        """所有非零分維度皆有原文依據時才可入庫。"""
        return not self.ungrounded_directional


def evaluate_tone_grounding(
    extraction: GuidanceExtraction, source_text: str
) -> ToneGroundingReport:
    """逐維度檢查態度評分是否有原文引文支撐。"""
    grounded: list[str] = []
    ungrounded: list[str] = []
    neutral: list[str] = []
    metrics = {
        "backlog_tone": extraction.backlog_tone,
        "pricing_power_tone": extraction.pricing_power_tone,
        "supply_chain_tone": extraction.supply_chain_tone,
        "defensive_posture_tone": extraction.defensive_posture_tone,
    }
    for name, metric in metrics.items():
        if is_quote_grounded(metric.quote_snippet, source_text):
            grounded.append(name)
        elif metric.score != 0:
            ungrounded.append(name)
        else:
            neutral.append(name)
    return ToneGroundingReport(
        grounded=tuple(grounded),
        ungrounded_directional=tuple(ungrounded),
        neutral_without_evidence=tuple(neutral),
    )
