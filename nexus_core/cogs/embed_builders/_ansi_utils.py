"""ANSI 視覺工具與 Discord Embed 內容安全工具。

本模組收錄所有與字元寬度、ANSI 渲染、文字截斷相關的純工具函式。
無 Discord Embed 物件依賴，可獨立測試。
"""

import re

from typing import List, Any

from ui import panel_renderer


# ============================================================================
# Visual-width and character utilities
# ============================================================================


def _visual_len(s: str) -> int:
    """計算字串的視覺寬度，中文字元與中文標點視為雙倍寬度。"""
    return sum(
        2
        if (ord(c) > 127 or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF)
        else 1
        for c in s
    )


def _pad_string(s: str, width: int, align: str = "left") -> str:
    """根據視覺寬度對字串進行填充。"""
    vlen = _visual_len(s)
    pad_len = max(0, width - vlen)
    if align == "right":
        return " " * pad_len + s
    elif align == "center":
        left_pad = pad_len // 2
        right_pad = pad_len - left_pad
        return " " * left_pad + s + " " * right_pad
    else:
        return s + " " * pad_len


def _visual_truncate(s: str, max_vlen: int) -> str:
    """根據視覺寬度截斷字串，避免中文字元被切成一半。"""
    return panel_renderer.visual_truncate(s, max_vlen)


def _wrap_visual(text: str, width: int, indent: str = "") -> list[str]:
    return panel_renderer.wrap_visual(text, width, indent)


def _clean_ansi(text: str) -> str:
    if not text:
        return ""
    # Remove real ANSI escape sequences
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", text)
    # Remove raw string ANSI residuals (e.g. [0;31m, [0m)
    text = re.sub(r"\[\d+;?\d*m", "", text)
    return text


def _chunk_text_blocks(blocks: List[str], max_len: int = 1024) -> List[str]:
    chunks = []
    current_chunk: List[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block)
        if current_chunk and current_len + block_len + 2 > max_len:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [block]
            current_len = block_len
        else:
            current_chunk.append(block)
            current_len += block_len + (2 if current_len > 0 else 0)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def _truncate_with_boundary(text: str, max_len: int) -> str:
    """優先在換行或句點邊界截斷，避免硬切造成可讀性差。"""
    return panel_renderer.truncate_with_boundary(text, max_len)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_macro_report_marker(line: str) -> bool:
    """較穩健地辨識宏觀風險段落起始行。"""
    if not line:
        return False
    normalized = line.strip()
    if not normalized.startswith("🌐"):
        return False
    return ("宏觀風險" in normalized) or ("資金水位報告" in normalized)
