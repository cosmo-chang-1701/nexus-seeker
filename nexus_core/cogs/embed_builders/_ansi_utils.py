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


def _format_macro_report_ansi(macro_text: str) -> str:
    """
    將宏觀風險與資金水位報告文字格式化為 Target Center 2.0 樹狀 ANSI 排版。
    - 過濾重複的標題行
    - 移除多餘的 ` • ` 前綴
    - 每一主項目 (🔹 或 🕸️) 與前一項目之間插入一行空行
    - 次級項目採用 ├─ 與 └─ 樹狀結構
    - 針對狀態 (✅, ⚠️, 🚨, 🆘, 🛡️) 進行 ANSI 顏色標註
    """
    if (
        not macro_text
        or not macro_text.strip()
        or macro_text.strip() == "目前無宏觀風險數據。"
    ):
        return " • 目前無宏觀風險數據。"

    raw_lines = [line.strip() for line in macro_text.split("\n") if line.strip()]
    grouped_items: List[dict[str, Any]] = []
    current_item: dict[str, Any] | None = None

    for line in raw_lines:
        clean = re.sub(r"^[\-\*\•\s]+", "", line).strip()
        clean = clean.replace("`", "").replace("*", "")
        if not clean:
            continue
        if "【宏觀風險與資金水位報告】" in clean:
            continue

        is_primary = clean.startswith("🔹") or clean.startswith("🕸️")
        if is_primary:
            if current_item:
                grouped_items.append(current_item)
            icon = "🕸️" if clean.startswith("🕸️") else "🔹"
            heading_content = re.sub(r"^[🔹🕸️\s]+", "", clean).strip()
            current_item = {"icon": icon, "heading": heading_content, "sublines": []}
        else:
            sub = re.sub(r"^(?:├─|└─|[\-\*\•\s])+", "", clean).strip()
            if sub:
                if current_item is not None:
                    current_item["sublines"].append(sub)
                else:
                    current_item = {
                        "icon": "🔹",
                        "heading": "宏觀指標",
                        "sublines": [sub],
                    }

    if current_item:
        grouped_items.append(current_item)

    if not grouped_items:
        return " • 目前無宏觀風險數據。"

    formatted_blocks: List[str] = []
    for item in grouped_items:
        icon = item["icon"]
        heading = item["heading"]
        sublines: List[str] = item["sublines"]

        block_lines: List[str] = []
        # 主項目標題高亮
        if ":" in heading or "：" in heading:
            parts = re.split(r"[:：]", heading, 1)
            metric_name = parts[0].strip()
            rest = f": {parts[1].strip()}" if len(parts) > 1 else ""
            block_lines.append(f" {icon} \u001b[1;36m{metric_name}\u001b[0m{rest}")
        else:
            block_lines.append(f" {icon} \u001b[1;36m{heading}\u001b[0m")

        for sub in sublines:
            prefix = "   • "
            # 狀態著色
            if (
                "🚨" in sub
                or "🆘" in sub
                or "多頭曝險過高" in sub
                or "空頭曝險過高" in sub
                or "脆性警告" in sub
                or "過度收租" in sub
                or "高度正相關" in sub
            ):
                colored_sub = f"\u001b[1;31m{sub}\u001b[0m"
            elif "⚠️" in sub or "收益率過低" in sub or "水位警戒" in sub:
                colored_sub = f"\u001b[1;33m{sub}\u001b[0m"
            elif (
                "✅" in sub
                or "🛡️" in sub
                or "風險中性" in sub
                or "反脆弱" in sub
                or "健康" in sub
                or "正常" in sub
                or "分散性良好" in sub
            ):
                colored_sub = f"\u001b[1;32m{sub}\u001b[0m"
            else:
                colored_sub = sub
            block_lines.append(f"{prefix}{colored_sub}")

        formatted_blocks.append("\n".join(block_lines))

    return "\n\n".join(formatted_blocks)
