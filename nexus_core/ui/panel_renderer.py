from __future__ import annotations

from typing import List


ZWS = "\u200b"  # 零寬空格，避免 Discord embed 欄位尾端貼齊


def visual_len(s: str) -> int:
    """估算字串的視覺寬度（中日韓寬字元以 2 計）。"""
    vlen = 0
    for c in s:
        vlen += (
            2
            if (
                ord(c) > 127 or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF
            )
            else 1
        )
    return vlen


def visual_truncate(s: str, max_vlen: int) -> str:
    """根據視覺寬度截斷字串，避免中文字元被切成一半。"""
    current_vlen = 0
    chars: list[str] = []
    for c in s:
        char_vlen = (
            2
            if (
                ord(c) > 127 or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF
            )
            else 1
        )
        if current_vlen + char_vlen > max_vlen:
            break
        chars.append(c)
        current_vlen += char_vlen
    return "".join(chars)


def wrap_visual(text: str, width: int, indent: str = "") -> list[str]:
    """依視覺寬度包行（適合放進 ANSI 面板的 └─ 條列）。"""
    paragraphs = text.replace("\r\n", "\n").split("\n")
    all_wrapped_lines: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        lines: list[str] = []
        current = ""
        for char in para:
            candidate = current + char
            if current and visual_len(candidate) > width:
                lines.append(current)
                current = indent + char
            else:
                current = candidate
        if current:
            lines.append(current)
        all_wrapped_lines.extend(lines)
    return all_wrapped_lines or [indent]


def truncate_with_boundary(text: str, max_len: int) -> str:
    """優先在換行或句點邊界截斷，避免硬切造成可讀性差。"""
    if len(text) <= max_len:
        return text

    reserved = 3
    safe_len = max(1, max_len - reserved)
    candidate = text[:safe_len]

    boundary_candidates = [
        candidate.rfind("\n\n"),
        candidate.rfind("\n"),
        candidate.rfind("。"),
    ]
    boundary = max(boundary_candidates)
    if boundary > int(max_len * 0.6):
        candidate = candidate[:boundary]

    return candidate.rstrip() + "..."


def chunk_ansi_table(
    header: str,
    divider: str,
    data_lines: List[str],
    *,
    max_len: int = 1024,
) -> List[str]:
    """將 ANSI 表格切成多個符合 Discord Embed field value 長度限制的區塊。"""
    prefix = f"```ansi\n{header}\n{divider}\n"
    suffix = "\n```"

    # 預留長度給字尾
    limit = max_len - len(suffix)

    chunks: List[str] = []
    current_lines: List[str] = []

    for line in data_lines:
        test_val = prefix + "\n".join(current_lines + [line])
        if len(test_val) <= limit:
            current_lines.append(line)
            continue

        if current_lines:
            chunks.append(prefix + "\n".join(current_lines) + suffix)
            current_lines = [line]
            continue

        # 單行超長，強行截斷（正常不應發生）
        truncated = line[: limit - len(prefix)]
        chunks.append(prefix + truncated + suffix)
        current_lines = []

    if current_lines:
        chunks.append(prefix + "\n".join(current_lines) + suffix)

    return chunks


def build_watchlist_style_panel(
    title_line: str,
    content: str,
    *,
    width: int = 45,
    empty_msg: str = "暫無資料",
) -> str:
    """建立 Watchlist 半小時戰報風格的 ANSI 面板文字內容（不含 code fence）。"""
    lines = [f" {title_line}", " ----------------------------------"]
    body = (content or "").strip()
    if not body:
        lines.append(f" └─ {empty_msg}")
        return "\n".join(lines)

    wrapped = wrap_visual(body, width=width, indent="   ")
    for line in wrapped:
        lines.append(f" └─ {line}")
    return "\n".join(lines)


def safe_codeblock_value(
    content: str,
    fallback: str,
    *,
    lang: str = "text",
    max_len: int = 1024,
    add_zws_suffix: bool = True,
) -> str:
    """產生安全的 code block 欄位值，避免截斷導致 fence 不閉合。"""
    body = (content or "").strip() or fallback

    suffix = f"\n{ZWS}" if add_zws_suffix else ""
    prefix = f"```{lang}\n"
    fence_suffix = "\n```"

    room = max(1, max_len - len(prefix) - len(fence_suffix) - len(suffix))
    body = truncate_with_boundary(body, room)

    value = prefix + body + fence_suffix + suffix
    if len(value) > max_len:
        hard_room = max(1, max_len - len(prefix) - len(fence_suffix) - len(suffix))
        value = prefix + body[:hard_room] + fence_suffix + suffix
    return value
