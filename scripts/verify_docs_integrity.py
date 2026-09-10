#!/usr/bin/env python3
"""Nexus Seeker Documentation Integrity & Quality Verification Harness.

This verification script executes an automated end-to-end quality audit against the
documentation repository under `docs/` according to PROJECT.md and ORIGINAL_REQUEST.md.

Verification Batteries:
1. [CLEANUP] Obsolete documentation cleanup (STRATEGY.md, architecture.md, etc. must not exist).
2. [STRUCTURE] Directory taxonomy and file count (all 29 specifications + docs/README.md).
3. [SECTIONS] 6-part specification structure (Headers, LaTeX math, Mermaid diagrams,
   named constants table, and valid repository source code paths).
4. [LANGUAGE] 100% Traditional Chinese purity (zero tolerance for Simplified Chinese).
5. [SEPARATION] Separation from root README.md (no Docker commands, .env tables, slash command lists).
6. [LINKS] Internal markdown link and anchor integrity (no broken relative links or dead anchors).
7. [INDEX] Master index coverage (docs/README.md references all 29 specification documents).

Exit Code:
- 0: All checks passed.
- 1: One or more checks failed.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.parse

# ---------------------------------------------------------------------------
# Constants & Specification Registry
# ---------------------------------------------------------------------------

OBSOLETE_DOC_FILES: list[str] = [
    "docs/STRATEGY.md",
    "docs/architecture.md",
    "docs/development_guide.md",
    "docs/dynamic_rollover.md",
    "docs/quant_strategy.md",
]

EXPECTED_SPECIFICATIONS: dict[str, list[str]] = {
    "docs/strategies": [
        "01_regime_routing_matrix.md",
        "02_right_side_momentum_ironclad.md",
        "03_left_side_mean_reversion_ironclad.md",
        "04_dynamic_rollover_state_machine.md",
        "05_dual_track_anti_washout_stop_loss.md",
    ],
    "docs/microstructure": [
        "01_gex_topology_and_walls.md",
        "02_wall_physical_constraints.md",
        "03_gamma_flip_estimation.md",
        "04_uoa_notional_and_paced_ratio.md",
        "05_volume_profile_and_dp_poc.md",
        "06_gamma_squeeze_engine_and_spear.md",
    ],
    "docs/valuation_pricing": [
        "01_tdp_valuation_model.md",
        "02_expected_move_and_max_pain.md",
        "03_skew_pcr_divergence_confluence.md",
        "04_ivr_regime_and_seller_lockout.md",
    ],
    "docs/risk_portfolio": [
        "01_beta_weighted_greeks.md",
        "02_vix_battle_ladder_and_kelly.md",
        "03_aroc_capital_efficiency.md",
        "04_ditm_convexity_profit_lock.md",
        "05_financial_runway_and_liquidity.md",
        "06_brinson_performance_attribution.md",
    ],
    "docs/macro_sentiment": [
        "01_macro_escape_top_matrix.md",
        "02_sec_filing_moat_scanner.md",
        "03_wti_crude_oil_monitor.md",
        "04_polymarket_vwbp_sentiment_radar.md",
    ],
    "docs/architecture": [
        "01_dual_watchlist_pipelines.md",
        "02_pre_market_cache_aside.md",
        "03_dual_service_and_proxy.md",
        "04_engineering_standards.md",
    ],
}

# Required section definitions for 29 specification documents
REQUIRED_SECTIONS: list[tuple[int, str, re.Pattern[str]]] = [
    (
        1,
        "核心哲學與適用市場環境",
        re.compile(r"^##\s+1[\.、\s]+核心哲學與適用市場環境", re.MULTILINE),
    ),
    (
        2,
        "數學模型與量化推導",
        re.compile(r"^##\s+2[\.、\s]+數學模型與量化推導", re.MULTILINE),
    ),
    (
        3,
        "決策邏輯與狀態機 / 流程圖",
        re.compile(
            r"^##\s+3[\.、\s]+決策邏輯與狀態機\s*(?:/|與)?\s*流程圖", re.MULTILINE
        ),
    ),
    (
        4,
        "關鍵具名常數與物理約束",
        re.compile(r"^##\s+4[\.、\s]+關鍵具名常數與物理約束", re.MULTILINE),
    ),
    (
        5,
        "邊界條件、風控熔斷與例外處理",
        re.compile(
            r"^##\s+5[\.、\s]+邊界條件[、，\s]*風控熔斷[與和\s]*例外處理", re.MULTILINE
        ),
    ),
    (
        6,
        "核心程式碼檔案路徑關聯",
        re.compile(r"^##\s+6[\.、\s]+核心程式碼檔案路徑關聯", re.MULTILINE),
    ),
]

# Curated mapping of unambiguous Simplified Chinese characters to Traditional Chinese.
# Every character in this mapping is strictly Simplified Chinese and never valid Traditional Chinese.
SIMPLIFIED_TO_TRADITIONAL: dict[str, str] = {
    "这": "這",
    "个": "個",
    "点": "點",
    "线": "線",
    "为": "為",
    "时": "時",
    "机": "機",
    "选": "選",
    "标": "標",
    "仓": "倉",
    "门": "門",
    "关": "關",
    "开": "開",
    "发": "發",
    "经": "經",
    "动": "動",
    "统": "統",
    "计": "計",
    "数": "數",
    "与": "與",
    "业": "業",
    "务": "務",
    "进": "進",
    "侧": "側",
    "调": "調",
    "归": "歸",
    "态": "態",
    "阶": "階",
    "阵": "陣",
    "损": "損",
    "跃": "躍",
    "极": "極",
    "规": "規",
    "则": "則",
    "设": "設",
    "连": "連",
    "获": "獲",
    "参": "參",
    "执": "執",
    "响": "響",
    "应": "應",
    "链": "鏈",
    "络": "絡",
    "错": "錯",
    "误": "誤",
    "报": "報",
    "结": "結",
    "构": "構",
    "权": "權",
    "账": "帳",
    "户": "戶",
    "预": "預",
    "胜": "勝",
    "压": "壓",
    "缩": "縮",
    "释": "釋",
    "资": "資",
    "杠": "槓",
    "杆": "桿",
    "维": "維",
    "护": "護",
    "频": "頻",
    "认": "認",
    "逻": "邏",
    "辑": "輯",
    "测": "測",
    "试": "試",
    "验": "驗",
    "证": "證",
    "击": "擊",
    "拟": "擬",
    "滤": "濾",
    "显": "顯",
    "软": "軟",
    "库": "庫",
    "储": "儲",
    "处": "處",
    "网": "網",
    "编": "編",
    "译": "譯",
    "录": "錄",
    "导": "導",
    "详": "詳",
    "细": "細",
    "图": "圖",
    "单": "單",
    "双": "雙",
    "类": "類",
    "总": "總",
    "区": "區",
    "体": "體",
    "实": "實",
    "现": "現",
    "场": "場",
    "头": "頭",
    "买": "買",
    "卖": "賣",
    "价": "價",
    "变": "變",
    "准": "準",
    "确": "確",
    "义": "義",
    "负": "負",
    "险": "險",
    "风": "風",
    "币": "幣",
    "盘": "盤",
    "国": "國",
    "际": "際",
    "对": "對",
    "节": "節",
    "宽": "寬",
    "长": "長",
    "评": "評",
    "论": "論",
    "询": "詢",
    "问": "問",
    "题": "題",
    "间": "間",
    "离": "離",
    "启": "啟",
    "闭": "閉",
    "锁": "鎖",
    "钥": "鑰",
    "环": "環",
    "约": "約",
    "级": "級",
    "别": "別",
    "层": "層",
    "签": "籤",
    "页": "頁",
    "码": "碼",
    "号": "號",
    "记": "記",
    "载": "載",
    "传": "傳",
    "输": "輸",
    "达": "達",
    "优": "優",
    "势": "勢",
    "趋": "趨",
    "两": "兩",
    "并": "並",
    "广": "廣",
    "无": "無",
    "专": "專",
    "写": "寫",
    "围": "圍",
    "补": "補",
    "偿": "償",
    "随": "隨",
    "稳": "穩",
    "项": "項",
    "监": "監",
    "视": "視",
    "画": "畫",
    "转": "轉",
    "换": "換",
    "轮": "輪",
    "暂": "暫",
    "缓": "緩",
    "队": "隊",
    "仅": "僅",
    "独": "獨",
    "联": "聯",
    "协": "協",
    "产": "產",
    "异": "異",
    "断": "斷",
    "涨": "漲",
    "质": "質",
    "亏": "虧",
    "润": "潤",
    "净": "淨",
    "债": "債",
    "撑": "撐",
    "顶": "頂",
    "筹": "籌",
    "货": "貨",
    "隐": "隱",
    "庄": "莊",
    "订": "訂",
    "种": "種",
    "触": "觸",
    "诱": "誘",
    "荡": "盪",
    "鲸": "鯨",
    "迟": "遲",
    "竞": "競",
    "条": "條",
    "滚": "滾",
    "从": "從",
    "还": "還",
    "过": "過",
    "让": "讓",
    "给": "給",
    "没": "沒",
    "们": "們",
    "样": "樣",
    "么": "麼",
    "虽": "雖",
    "带": "帶",
    "声": "聲",
    "听": "聽",
    "说": "說",
    "话": "話",
    "请": "請",
    "谢": "謝",
    "帮": "幫",
    "见": "見",
    "观": "觀",
    "览": "覽",
    "觉": "覺",
    "识": "識",
    "忆": "憶",
    "丢": "丟",
}

# Regex patterns for separation from root README.md
ROOT_README_SEPARATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Docker compose runtime command",
        re.compile(
            r"(?:docker\s+compose\s+(?:up|run|build|exec|down)|docker-compose\s+(?:up|run|build|exec|down))",
            re.IGNORECASE,
        ),
    ),
    (
        "Quickstart repository clone or environment copy",
        re.compile(
            r"(?:git\s+clone\s+https?://[^\s]+/nexus-seeker(?:\.git)?|cp\s+\.env\.example\s+\.env)"
        ),
    ),
    (
        ".env configuration table header or keys",
        re.compile(
            r"(?:\|\s*變數名稱\s*\|\s*必填\s*\|\s*說明\s*\||\|\s*`?(?:DISCORD_TOKEN|FINNHUB_API_KEY|CF_TUNNEL_TOKEN|LLM_API_BASE|DISCORD_ADMIN_USER_ID)`?\s*\|)"
        ),
    ),
    (
        "Getting-started slash command list header",
        re.compile(r"^###\s+常用指令與操作", re.MULTILINE),
    ),
]

# Patterns for extracting source code paths in Section 6
CODE_PATH_PATTERN: re.Pattern[str] = re.compile(
    r"(?:nexus_core|nexus_edge_scraper|scripts)/[a-zA-Z0-9_/]+\.py|`([a-zA-Z0-9_./\-]+\.py)`"
)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CheckFailure:
    """Represents an individual verification failure."""

    category: str
    target: str
    message: str
    line_num: int | None = None
    col_num: int | None = None
    snippet: str | None = None

    def format(self) -> str:
        loc: str = self.target
        if self.line_num is not None:
            loc += f":{self.line_num}"
            if self.col_num is not None:
                loc += f":{self.col_num}"
        base: str = f"[{self.category}] {loc} -> {self.message}"
        if self.snippet:
            base += f"\n    Context: {self.snippet.strip()}"
        return base


@dataclasses.dataclass
class BatteryResult:
    """Result of an individual check battery."""

    category: str
    description: str
    total_checks: int = 0
    passed_checks: int = 0
    failures: list[CheckFailure] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0 and self.passed_checks == self.total_checks


# ---------------------------------------------------------------------------
# Helper Utilities
# ---------------------------------------------------------------------------


def slugify_heading(heading_text: str) -> list[str]:
    """Generates candidate anchor slugs for a markdown heading (GitHub compatible)."""
    text: str = heading_text.strip()
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"[*_`]", "", text)
    clean_text: str = text.lower()

    # Candidate 1: Standard GitHub slug (punctuation stripped, spaces to hyphens)
    slug1: str = re.sub(r"[^\w\s\-\u4e00-\u9fff]", "", clean_text)
    slug1 = re.sub(r"\s+", "-", slug1)

    # Candidate 2: Slashes and colons converted to hyphens
    slug2: str = re.sub(r"[/:、，]", "-", clean_text)
    slug2 = re.sub(r"[^\w\s\-\u4e00-\u9fff]", "", slug2)
    slug2 = re.sub(r"\s+", "-", slug2)
    slug2 = re.sub(r"-+", "-", slug2).strip("-")

    # Candidate 3: Minimal punctuation replacement
    slug3: str = re.sub(r"\s+", "-", clean_text)

    slugs: set[str] = {slug1, slug2, slug3, clean_text}
    return [s for s in slugs if s]


def strip_code_fences(content: str) -> str:
    """Replaces fenced code blocks with blank lines to preserve line numbering."""
    lines: list[str] = content.splitlines(keepends=True)
    in_fence: bool = False
    cleaned_lines: list[str] = []

    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            cleaned_lines.append("\n")
        elif in_fence:
            cleaned_lines.append("\n")
        else:
            cleaned_lines.append(line)

    return "".join(cleaned_lines)


# ---------------------------------------------------------------------------
# Verification Batteries
# ---------------------------------------------------------------------------


class DocsVerifier:
    """Coordinates and runs all documentation integrity checks."""

    def __init__(self, repo_root: Path, verbose: bool = False) -> None:
        self.repo_root: Path = repo_root.resolve()
        self.docs_dir: Path = self.repo_root / "docs"
        self.verbose: bool = verbose

    def run_all(self) -> list[BatteryResult]:
        """Executes all 7 verification batteries and returns their results."""
        results: list[BatteryResult] = [
            self.battery_cleanup(),
            self.battery_structure_and_count(),
            self.battery_six_part_specifications(),
            self.battery_language_purity(),
            self.battery_readme_separation(),
            self.battery_link_integrity(),
            self.battery_master_readme_coverage(),
        ]
        return results

    # 1. Cleanup Battery
    def battery_cleanup(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="CLEANUP",
            description="Obsolete legacy documentation removal",
        )
        for rel_path in OBSOLETE_DOC_FILES:
            result.total_checks += 1
            file_path: Path = self.repo_root / rel_path
            if file_path.exists():
                result.failures.append(
                    CheckFailure(
                        category="CLEANUP",
                        target=rel_path,
                        message=f"Obsolete legacy file still exists on disk: {rel_path}",
                    )
                )
            else:
                result.passed_checks += 1
        return result

    # 2. Structure & Count Battery
    def battery_structure_and_count(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="STRUCTURE",
            description="Directory taxonomy and 29 specification documents + README existence",
        )

        # Check master README
        result.total_checks += 1
        readme_path: Path = self.docs_dir / "README.md"
        if not readme_path.exists():
            result.failures.append(
                CheckFailure(
                    category="STRUCTURE",
                    target="docs/README.md",
                    message="Master documentation index docs/README.md is missing.",
                )
            )
        elif readme_path.stat().st_size == 0:
            result.failures.append(
                CheckFailure(
                    category="STRUCTURE",
                    target="docs/README.md",
                    message="Master documentation index docs/README.md is empty (0 bytes).",
                )
            )
        else:
            result.passed_checks += 1

        # Check all 29 specification documents
        expected_total_specs: int = 0
        for category_dir, spec_files in EXPECTED_SPECIFICATIONS.items():
            dir_path: Path = self.repo_root / category_dir
            for spec_file in spec_files:
                expected_total_specs += 1
                result.total_checks += 1
                spec_path: Path = dir_path / spec_file
                rel_display: str = f"{category_dir}/{spec_file}"

                if not spec_path.exists():
                    result.failures.append(
                        CheckFailure(
                            category="STRUCTURE",
                            target=rel_display,
                            message=f"Required specification document missing: {rel_display}",
                        )
                    )
                elif spec_path.stat().st_size == 0:
                    result.failures.append(
                        CheckFailure(
                            category="STRUCTURE",
                            target=rel_display,
                            message=f"Specification document is empty (0 bytes): {rel_display}",
                        )
                    )
                else:
                    result.passed_checks += 1

        return result

    # 3. 6-Part Specification Structure Battery
    def battery_six_part_specifications(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="SECTIONS",
            description="6-part specification structure (Math LaTeX, Mermaid, Constants Table, Source Paths)",
        )

        for category_dir, spec_files in EXPECTED_SPECIFICATIONS.items():
            dir_path: Path = self.repo_root / category_dir
            for spec_file in spec_files:
                spec_path: Path = dir_path / spec_file
                rel_display: str = f"{category_dir}/{spec_file}"
                result.total_checks += 1

                if not spec_path.exists() or spec_path.stat().st_size == 0:
                    result.failures.append(
                        CheckFailure(
                            category="SECTIONS",
                            target=rel_display,
                            message="Cannot verify 6-part structure: file is missing or empty.",
                        )
                    )
                    continue

                content: str = spec_path.read_text(encoding="utf-8")
                doc_errors: list[str] = self._verify_single_spec_structure(
                    rel_display, content
                )

                if doc_errors:
                    for err in doc_errors:
                        result.failures.append(
                            CheckFailure(
                                category="SECTIONS",
                                target=rel_display,
                                message=err,
                            )
                        )
                else:
                    result.passed_checks += 1

        return result

    def _verify_single_spec_structure(
        self, rel_display: str, content: str
    ) -> list[str]:
        errors: list[str] = []
        section_positions: dict[int, int] = {}

        # 1. Check presence of all 6 section headers
        for sec_num, sec_name, pattern in REQUIRED_SECTIONS:
            match = pattern.search(content)
            if not match:
                errors.append(
                    f"Missing required section header '## {sec_num}. {sec_name}'"
                )
            else:
                section_positions[sec_num] = match.start()

        if len(section_positions) < 6:
            # Cannot proceed with slice checks if headers are missing
            return errors

        # Verify sequential ordering
        for i in range(1, 6):
            if section_positions[i] >= section_positions[i + 1]:
                errors.append(
                    f"Section {i} appears after or at same position as Section {i + 1}"
                )

        # Extract section slices
        def get_section_text(sec_num: int) -> str:
            start: int = section_positions[sec_num]
            end: int = section_positions[sec_num + 1] if sec_num < 6 else len(content)
            return content[start:end]

        # 2. Section 2 must contain LaTeX math: $$...$$ or inline $...$
        sec2_text: str = get_section_text(2)
        has_latex_block: bool = bool(re.search(r"\$\$[\s\S]+?\$\$", sec2_text))
        has_latex_inline: bool = bool(
            re.search(r"(?<!\$)\$(?!\$)[^\n$]+(?<!\$)\$(?!\$)", sec2_text)
        )
        if not (has_latex_block or has_latex_inline):
            errors.append(
                "Section 2 (數學模型與量化推導) must contain LaTeX math equations ($$...$$ or $...$)"
            )

        # 3. Section 3 must contain Mermaid diagram
        sec3_text: str = get_section_text(3)
        has_mermaid: bool = bool(re.search(r"```mermaid\s*\n[\s\S]+?\n```", sec3_text))
        if not has_mermaid:
            errors.append(
                "Section 3 (決策邏輯與狀態機 / 流程圖) must contain at least one ```mermaid diagram block"
            )

        # 4. Section 4 must contain markdown table of named constants
        sec4_text: str = get_section_text(4)
        if not self._has_markdown_table(sec4_text):
            errors.append(
                "Section 4 (關鍵具名常數與物理約束) must contain a markdown table of named constants"
            )

        # 5. Section 6 must reference valid source code paths
        sec6_text: str = get_section_text(6)
        candidates: list[str] = []
        for m in CODE_PATH_PATTERN.finditer(sec6_text):
            val: str = m.group(1) if m.group(1) else m.group(0)
            val = val.strip("`'\" \t")
            candidates.append(val)

        if not candidates:
            errors.append(
                "Section 6 (核心程式碼檔案路徑關聯) must reference repository source code files (.py)"
            )
        else:
            # Check if at least one referenced .py file actually exists in repo
            valid_existing_paths: list[str] = []
            for cand in candidates:
                cand_path: Path = self.repo_root / cand
                if cand_path.is_file():
                    valid_existing_paths.append(cand)
                else:
                    # Also try search under nexus_core or nexus_edge_scraper
                    sub1: Path = self.repo_root / "nexus_core" / cand
                    sub2: Path = self.repo_root / "nexus_edge_scraper" / cand
                    if sub1.is_file() or sub2.is_file():
                        valid_existing_paths.append(cand)

            if not valid_existing_paths:
                errors.append(
                    f"Section 6 source code paths could not be verified in repository: {candidates[:3]}"
                )

        return errors

    def _has_markdown_table(self, text: str) -> bool:
        """Detects whether text contains a valid markdown table."""
        lines: list[str] = [line.strip() for line in text.splitlines() if line.strip()]
        for i in range(len(lines) - 2):
            if lines[i].startswith("|") and lines[i].endswith("|"):
                if re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", lines[i + 1]):
                    if lines[i + 2].startswith("|") and lines[i + 2].endswith("|"):
                        return True
        return False

    # 4. Language Purity Battery (Traditional Chinese)
    def battery_language_purity(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="LANGUAGE",
            description="100% Traditional Chinese language purity (rejects Simplified Chinese)",
        )

        all_md_files: list[Path] = self._get_all_docs_md_files()
        for md_path in all_md_files:
            result.total_checks += 1
            rel_display: str = str(md_path.relative_to(self.repo_root))
            file_has_error: bool = False

            try:
                content: str = md_path.read_text(encoding="utf-8")
            except Exception as e:
                result.failures.append(
                    CheckFailure(
                        category="LANGUAGE",
                        target=rel_display,
                        message=f"Failed to read file for language purity check: {e}",
                    )
                )
                continue

            lines: list[str] = content.splitlines()
            for line_idx, line in enumerate(lines, start=1):
                # Scan for simplified characters
                for col_idx, ch in enumerate(line, start=1):
                    if ch in SIMPLIFIED_TO_TRADITIONAL:
                        trad_suggest: str = SIMPLIFIED_TO_TRADITIONAL[ch]
                        result.failures.append(
                            CheckFailure(
                                category="LANGUAGE",
                                target=rel_display,
                                message=f"Found Simplified Chinese character '{ch}' (suggested Traditional: '{trad_suggest}')",
                                line_num=line_idx,
                                col_num=col_idx,
                                snippet=line,
                            )
                        )
                        file_has_error = True
                        # To prevent flooding console, limit to max 5 errors per file
                        if len(result.failures) >= 200:
                            break
                if len(result.failures) >= 200:
                    break

            if not file_has_error:
                result.passed_checks += 1

        return result

    # 5. Separation from root README Battery
    def battery_readme_separation(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="SEPARATION",
            description="Separation from root README.md (no Docker commands, .env tables, slash command lists)",
        )

        all_md_files: list[Path] = self._get_all_docs_md_files()
        for md_path in all_md_files:
            result.total_checks += 1
            rel_display: str = str(md_path.relative_to(self.repo_root))
            content: str = md_path.read_text(encoding="utf-8")
            file_has_error: bool = False

            # Test each forbidden pattern
            for pattern_desc, pattern in ROOT_README_SEPARATION_PATTERNS:
                match = pattern.search(content)
                if match:
                    # Calculate line number
                    line_num: int = content[: match.start()].count("\n") + 1
                    matched_snippet: str = match.group(0).splitlines()[0]
                    result.failures.append(
                        CheckFailure(
                            category="SEPARATION",
                            target=rel_display,
                            message=f"Forbidden root README content detected ({pattern_desc})",
                            line_num=line_num,
                            snippet=matched_snippet,
                        )
                    )
                    file_has_error = True

            if not file_has_error:
                result.passed_checks += 1

        return result

    # 6. Link Integrity Battery
    def battery_link_integrity(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="LINKS",
            description="Markdown relative links and anchor target integrity",
        )

        all_md_files: list[Path] = self._get_all_docs_md_files()
        # Pre-cache anchors for all markdown files
        file_anchors: dict[Path, set[str]] = {}
        for md_path in all_md_files:
            file_anchors[md_path.resolve()] = self._extract_file_anchors(md_path)

        for md_path in all_md_files:
            result.total_checks += 1
            rel_display: str = str(md_path.relative_to(self.repo_root))
            content: str = md_path.read_text(encoding="utf-8")
            clean_content: str = strip_code_fences(content)

            link_pattern: re.Pattern[str] = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
            lines: list[str] = clean_content.splitlines()
            file_has_error: bool = False

            for line_idx, line in enumerate(lines, start=1):
                for match in link_pattern.finditer(line):
                    raw_target: str = match.group(2).strip()

                    # Skip external URLs or non-path targets
                    if re.match(
                        r"^(?:https?|mailto|ftp|javascript):", raw_target, re.IGNORECASE
                    ):
                        continue

                    # Parse target and anchor
                    parsed = urllib.parse.urlparse(raw_target)
                    path_part: str = parsed.path
                    anchor_part: str = parsed.fragment

                    # Target file resolution
                    target_file: Path
                    if not path_part:
                        # Same file anchor
                        target_file = md_path.resolve()
                    else:
                        target_file = (md_path.parent / path_part).resolve()

                    # 1. Verify target file existence
                    if not target_file.exists():
                        result.failures.append(
                            CheckFailure(
                                category="LINKS",
                                target=rel_display,
                                message=f"Dead link to non-existent file '{raw_target}'",
                                line_num=line_idx,
                                snippet=match.group(0),
                            )
                        )
                        file_has_error = True
                        continue

                    # 2. Verify anchor if present
                    if anchor_part:
                        decoded_anchor: str = (
                            urllib.parse.unquote(anchor_part).lower().strip("#")
                        )
                        target_known_anchors: set[str] = file_anchors.get(
                            target_file, set()
                        )

                        # Check if anchor is present
                        if not target_known_anchors:
                            # If target wasn't pre-cached (e.g. outside docs/), extract now
                            if (
                                target_file.suffix.lower() == ".md"
                                and target_file.is_file()
                            ):
                                target_known_anchors = self._extract_file_anchors(
                                    target_file
                                )
                                file_anchors[target_file] = target_known_anchors

                        if (
                            target_known_anchors
                            and decoded_anchor not in target_known_anchors
                        ):
                            # Fuzzy slug check: replace underscores/dashes
                            fuzzy_match: bool = False
                            norm_anchor: str = re.sub(r"[-_]+", "-", decoded_anchor)
                            for known in target_known_anchors:
                                if re.sub(r"[-_]+", "-", known) == norm_anchor:
                                    fuzzy_match = True
                                    break

                            if not fuzzy_match:
                                result.failures.append(
                                    CheckFailure(
                                        category="LINKS",
                                        target=rel_display,
                                        message=f"Dead anchor link '#{anchor_part}' in '{raw_target}'",
                                        line_num=line_idx,
                                        snippet=match.group(0),
                                    )
                                )
                                file_has_error = True

            if not file_has_error:
                result.passed_checks += 1

        return result

    def _extract_file_anchors(self, md_path: Path) -> set[str]:
        """Extracts all valid anchor slugs from headings and HTML tags in a markdown file."""
        anchors: set[str] = set()
        if not md_path.exists() or md_path.stat().st_size == 0:
            return anchors

        try:
            content: str = md_path.read_text(encoding="utf-8")
        except Exception:
            return anchors

        # Extract markdown headings
        for line in content.splitlines():
            line_strip: str = line.strip()
            if line_strip.startswith("#"):
                slug_candidates: list[str] = slugify_heading(line_strip)
                for slug in slug_candidates:
                    anchors.add(slug.lower())

            # Extract HTML explicit anchors <a name="..." id="...">
            for html_match in re.finditer(
                r"""<a\s+(?:[^>]*?\s+)?(?:name|id)=["']([^"']+)["']""",
                line,
                re.IGNORECASE,
            ):
                anchors.add(html_match.group(1).lower())
            for id_match in re.finditer(
                r"""id=["']([^"']+)["']""", line, re.IGNORECASE
            ):
                anchors.add(id_match.group(1).lower())

        return anchors

    # 7. Master README Index Coverage Battery
    def battery_master_readme_coverage(self) -> BatteryResult:
        result: BatteryResult = BatteryResult(
            category="INDEX",
            description="Master README (docs/README.md) specification index coverage",
        )

        readme_path: Path = self.docs_dir / "README.md"
        result.total_checks += 1

        if not readme_path.exists() or readme_path.stat().st_size == 0:
            result.failures.append(
                CheckFailure(
                    category="INDEX",
                    target="docs/README.md",
                    message="docs/README.md is missing or empty; cannot verify index coverage.",
                )
            )
            return result

        readme_content: str = readme_path.read_text(encoding="utf-8")
        unreferenced_specs: list[str] = []

        for category_dir, spec_files in EXPECTED_SPECIFICATIONS.items():
            for spec_file in spec_files:
                # Check if the specification filename or relative path is mentioned in docs/README.md
                if spec_file not in readme_content:
                    unreferenced_specs.append(f"{category_dir}/{spec_file}")

        if unreferenced_specs:
            for unref in unreferenced_specs:
                result.failures.append(
                    CheckFailure(
                        category="INDEX",
                        target="docs/README.md",
                        message=f"Specification document is not referenced in docs/README.md: {unref}",
                    )
                )
        else:
            result.passed_checks += 1

        return result

    def _get_all_docs_md_files(self) -> list[Path]:
        """Retrieves all existing markdown files under docs/."""
        if not self.docs_dir.exists():
            return []
        md_files: list[Path] = []
        for root, _, files in os.walk(self.docs_dir):
            for f in files:
                if f.endswith(".md"):
                    md_files.append(Path(root) / f)
        return sorted(md_files)


# ---------------------------------------------------------------------------
# CLI Reporter & Presentation
# ---------------------------------------------------------------------------


class Color:
    GREEN: str = "\033[92m"
    RED: str = "\033[91m"
    YELLOW: str = "\033[93m"
    BLUE: str = "\033[94m"
    CYAN: str = "\033[96m"
    BOLD: str = "\033[1m"
    RESET: str = "\033[0m"


def print_report(
    results: list[BatteryResult], duration_sec: float, verbose: bool = False
) -> int:
    """Prints a structured, formatted verification summary to console and returns exit code."""
    is_tty: bool = sys.stdout.isatty()

    def c(color: str, text: str) -> str:
        return f"{color}{text}{Color.RESET}" if is_tty else text

    total_checks: int = sum(r.total_checks for r in results)
    passed_checks: int = sum(r.passed_checks for r in results)
    total_failures: int = sum(len(r.failures) for r in results)

    print("\n" + "=" * 80)
    print(
        c(
            Color.BOLD + Color.CYAN,
            "  🌌 NEXUS SEEKER DOCUMENTATION INTEGRITY & VERIFICATION HARNESS",
        )
    )
    print("=" * 80 + "\n")

    for r in results:
        status_tag: str = (
            c(Color.GREEN + Color.BOLD, "[PASS]")
            if r.passed
            else c(Color.RED + Color.BOLD, "[FAIL]")
        )
        counts: str = f"({r.passed_checks}/{r.total_checks})"
        print(
            f"{status_tag} {c(Color.BOLD, r.category.ljust(12))} {counts.rjust(9)} : {r.description}"
        )

        if not r.passed:
            displayed_failures: list[CheckFailure] = (
                r.failures if verbose else r.failures[:5]
            )
            for failure in displayed_failures:
                print(c(Color.RED, f"    ❌ {failure.format()}"))
            if len(r.failures) > len(displayed_failures):
                print(
                    c(
                        Color.YELLOW,
                        f"    ... and {len(r.failures) - len(displayed_failures)} more failures (use --verbose to view all)",
                    )
                )
        elif verbose:
            print(c(Color.GREEN, "    All battery checks passed."))

    print("\n" + "-" * 80)
    summary_status: str
    exit_code: int
    if total_failures == 0 and total_checks > 0:
        summary_status = c(
            Color.GREEN + Color.BOLD, "SUCCESS (ALL 100% SPECIFICATIONS PASSED)"
        )
        exit_code = 0
    else:
        summary_status = c(
            Color.RED + Color.BOLD, f"FAILURE ({total_failures} VIOLATIONS FOUND)"
        )
        exit_code = 1

    print(f"  Summary:     {summary_status}")
    print(f"  Total Batteries: {len(results)}")
    print(
        f"  Total Checks:    {total_checks} evaluated ({passed_checks} passed, {total_failures} violations)"
    )
    print(f"  Duration:        {duration_sec:.2f}s")
    print("-" * 80 + "\n")

    return exit_code


# ---------------------------------------------------------------------------
# Standalone Self-Test Mode
# ---------------------------------------------------------------------------


def run_self_tests() -> int:
    """Runs isolated in-memory/temp-dir self-tests proving test harness correctness."""
    print("Executing DocsVerifier Self-Tests in isolated environment...")
    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_root: Path = Path(temp_dir_str)
        temp_docs: Path = temp_root / "docs"
        temp_docs.mkdir(parents=True)

        # 1. Create a dummy python source file in temp repo
        dummy_py: Path = temp_root / "nexus_core" / "test_module.py"
        dummy_py.parent.mkdir(parents=True)
        dummy_py.write_text("# Test module\n", encoding="utf-8")

        # 2. Populate all 29 specification documents + README with compliant content
        compliant_content_template: str = """# Specification Test Document

## 1. 核心哲學與適用市場環境
本模組在適應性市場中發揮核心做市商微觀結構平衡作用。

## 2. 數學模型與量化推導
做市商淨曝險公式如下：
$$
\\text{NetGEX} = \\sum_{i=1}^{N} \\Gamma_i \\cdot S \\cdot 100
$$
現價約為 $S = 100.0$。

## 3. 決策邏輯與狀態機 / 流程圖
```mermaid
graph TD
    A[市場開盤] --> B{NetGEX > 0}
    B -- 是 --> C[自穩定狀態]
    B -- 否 --> D[助漲助跌泥淖]
```

## 4. 關鍵具名常數與物理約束
| 常數名稱 | 數值/門檻 | 物理/代碼約束 | 程式碼檔案路徑 |
|---|---|---|---|
| `GEX_THICKNESS_THRESHOLD` | `500,000` | 薄紙牆判定臨界值 | `nexus_core/test_module.py` |

## 5. 邊界條件、風控熔斷與例外處理
當數據缺失或除以零時啟動降級防禦。

## 6. 核心程式碼檔案路徑關聯
- `nexus_core/test_module.py`
"""

        readme_content_lines: list[str] = ["# Documentation Index\n\n## 策略導航\n"]
        for cat_dir, spec_files in EXPECTED_SPECIFICATIONS.items():
            rel_cat: str = cat_dir.replace("docs/", "")
            sub_dir: Path = temp_docs / rel_cat
            sub_dir.mkdir(parents=True, exist_ok=True)
            for spec_f in spec_files:
                spec_path: Path = sub_dir / spec_f
                spec_path.write_text(compliant_content_template, encoding="utf-8")
                readme_content_lines.append(f"- [{spec_f}]({rel_cat}/{spec_f})\n")

        readme_path: Path = temp_docs / "README.md"
        readme_path.write_text("".join(readme_content_lines), encoding="utf-8")

        # Verify that compliant docs pass all batteries
        verifier = DocsVerifier(repo_root=temp_root)
        results = verifier.run_all()
        for r in results:
            if not r.passed:
                print(
                    f"Self-test assertion failed on clean setup: {r.category} had failures: {r.failures}"
                )
                return 1

        # 3. Test Negative Scenarios:
        # 3a. Obsolete file detection
        obsolete_file: Path = temp_docs / "STRATEGY.md"
        obsolete_file.write_text("old", encoding="utf-8")
        clean_res = verifier.battery_cleanup()
        assert not clean_res.passed, "Cleanup battery failed to detect obsolete file"
        obsolete_file.unlink()

        # 3b. Simplified Chinese detection
        test_spec: Path = temp_docs / "strategies" / "01_regime_routing_matrix.md"
        original_text: str = test_spec.read_text(encoding="utf-8")
        test_spec.write_text(original_text + "\n这里包含了简体字\n", encoding="utf-8")
        lang_res = verifier.battery_language_purity()
        assert (
            not lang_res.passed
        ), "Language purity battery failed to detect Simplified Chinese"
        test_spec.write_text(original_text, encoding="utf-8")

        # 3c. Root README forbidden content detection
        test_spec.write_text(
            original_text + "\n```bash\ndocker compose up -d\n```\n", encoding="utf-8"
        )
        sep_res = verifier.battery_readme_separation()
        assert (
            not sep_res.passed
        ), "Separation battery failed to detect docker compose command"
        test_spec.write_text(original_text, encoding="utf-8")

        # 3d. Dead link detection
        test_spec.write_text(
            original_text + "\n[Broken Link](non_existent_file.md)\n", encoding="utf-8"
        )
        link_res = verifier.battery_link_integrity()
        assert (
            not link_res.passed
        ), "Link integrity battery failed to detect dead file link"
        test_spec.write_text(original_text, encoding="utf-8")

        print("✅ All DocsVerifier self-tests passed successfully!")
        return 0


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nexus Seeker Documentation Integrity & Quality Verification Harness"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root path (default: parent of scripts/)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose failure diagnostics and context snippets",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Execute internal harness self-tests against mock compliant and non-compliant docs",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_tests()

    start_time: float = time.time()
    verifier = DocsVerifier(repo_root=args.repo_root, verbose=args.verbose)
    results: list[BatteryResult] = verifier.run_all()
    duration: float = time.time() - start_time

    return print_report(results=results, duration_sec=duration, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
