"""
refactor_intraday.py

安全重構 market_analysis/intraday_pipeline.py：
1. 從原始檔案中精準定位各程式碼區塊（以 def/class 定義為邊界）
2. 寫入新子模組（gamma_squeeze_engine.py, signal_calculator.py, option_guidance.py）
3. 替換原始檔案中的區塊為 import 語句（向後相容 shim）
4. 驗證原始檔案語法正確性（compile 測試）
"""

import ast
import sys
import re
from pathlib import Path

NEXUS_CORE = Path(__file__).parent.parent

PIPELINE = NEXUS_CORE / "market_analysis" / "intraday_pipeline.py"
SIGNAL_CALC = NEXUS_CORE / "market_analysis" / "signal_calculator.py"
OPTION_GUID = NEXUS_CORE / "market_analysis" / "option_guidance.py"
GAMMA_ENG = NEXUS_CORE / "market_analysis" / "gamma_squeeze_engine.py"
MODELS_DIR = NEXUS_CORE / "market_analysis" / "models"


def find_block_lines(
    lines: list[str], start_pattern: str, end_patterns: list[str]
) -> tuple[int, int]:
    """找到從 start_pattern 開始、到 end_patterns 中任一個（不含）結束的程式碼區塊。

    Returns:
        (start_line_idx, end_line_idx) — 0-indexed，[start, end)
    """
    start = None
    for i, line in enumerate(lines):
        if start is None and re.match(start_pattern, line):
            start = i
        elif start is not None:
            stripped = line.strip()
            for pat in end_patterns:
                if re.match(pat, stripped) and i > start:
                    return start, i
    if start is not None:
        return start, len(lines)
    raise ValueError(f"Cannot find block starting with: {start_pattern!r}")


def read_block(lines: list[str], start: int, end: int) -> str:
    return "".join(lines[start:end]).rstrip() + "\n"


def verify_syntax(path: Path, source: str) -> None:
    try:
        ast.parse(source)
        print(f"  ✅ Syntax OK: {path.name}")
    except SyntaxError as e:
        print(f"  ❌ Syntax Error in {path.name}: {e}")
        sys.exit(1)


def main() -> None:
    print(f"Reading {PIPELINE} ...")
    src = PIPELINE.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    total = len(lines)
    print(f"  Total lines: {total}")

    # ── 1. 定位各個提取區塊（line index，0-based）─────────────────────────

    # signal_calculator.py：
    #   _derive_buy_levels → build_watchlist_skew_rule_commentary の前（或 _is_mock 為起點）
    sig_start, sig_end = find_block_lines(
        lines,
        r"^def _derive_buy_levels\(",
        [r"^def _is_mock\(", r"^def _get_tactical_model\("],
    )
    # _is_mock & _get_tactical_model & calculate_dynamic_trading_signals & _helper funcs
    mock_start, mock_end = find_block_lines(
        lines,
        r"^def _is_mock\(",
        [r"^def derive_watchlist_option_guidance\("],
    )
    print(f"  sig helpers block  : lines {sig_start+1}–{sig_end}")
    print(f"  _is_mock+calc block: lines {mock_start+1}–{mock_end}")

    # option_guidance.py：derive_watchlist_option_guidance → class TraderAccountState
    opt_start, opt_end = find_block_lines(
        lines,
        r"^def derive_watchlist_option_guidance\(",
        [r"^class TraderAccountState\("],
    )
    print(f"  option_guidance block: lines {opt_start+1}–{opt_end}")

    # Models + NexusGammaSqueezeEngine → class IntradayScanPipeline
    models_start, models_end = find_block_lines(
        lines,
        r"^class TraderAccountState\(",
        [r"^class IntradayScanPipeline\("],
    )
    print(f"  models+engine block  : lines {models_start+1}–{models_end}")

    # ── 2. 提取各個 submodule 的內容 ─────────────────────────────────────

    sig_helpers_src = read_block(lines, sig_start, sig_end)
    mock_calc_src = read_block(lines, mock_start, mock_end)
    opt_src = read_block(lines, opt_start, opt_end)
    gamma_src = read_block(lines, models_start, models_end)

    # ── 3. 寫入 signal_calculator.py ─────────────────────────────────────
    SIGNAL_HEADER = '''\
"""
signal_calculator.py — 動態交易訊號計算器。

從 intraday_pipeline.py 分離的純計算層，包含：
- _derive_buy_levels / _derive_sell_levels（買賣支撐阻力推算）
- _buy_zone_status / _sell_zone_status（區間狀態判斷）
- _extract_pe_ratio（財報 PE 萃取）
- _is_mock / _get_tactical_model（測試 mock 偵測與模型解析）
- calculate_dynamic_trading_signals（動態買賣點與股數計算）
"""
import logging
from typing import Any, Mapping, Dict

from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan

logger = logging.getLogger(__name__)


'''
    sig_full = SIGNAL_HEADER + sig_helpers_src + "\n\n" + mock_calc_src
    verify_syntax(SIGNAL_CALC, sig_full)
    SIGNAL_CALC.write_text(sig_full, encoding="utf-8")
    print(f"  Written: {SIGNAL_CALC}")

    # ── 4. 寫入 option_guidance.py ────────────────────────────────────────
    OPT_HEADER = '''\
"""
option_guidance.py — 期權策略指引與可執行期權合約計畫。

從 intraday_pipeline.py 分離，包含：
- derive_watchlist_option_guidance（策略文字描述）
- _mid_price_from_row / _pick_watchlist_cover_leg（合約選擇工具）
- _estimate_watchlist_contract_count（口數估算）
- build_watchlist_option_plan（完整期權計畫建構）
"""
import logging
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEventContext,
    WatchlistLegAction,
    WatchlistOptionLeg,
    WatchlistOptionPlan,
    WatchlistOptionType,
    WatchlistPremiumType,
    WatchlistTacticalPlan,
)
from market_analysis.signal_calculator import (
    _is_mock,
    _get_tactical_model,
    calculate_dynamic_trading_signals,
)

logger = logging.getLogger(__name__)


def _watchlist_event_risk_multiplier(
    event_context: WatchlistEventContext | None,
) -> float:
    if event_context is None:
        return 1.0
    multipliers = [1.0]
    if (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 72.0
    ):
        multipliers.append(0.35)
    elif (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 168.0
    ):
        multipliers.append(0.5)

    if (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 24.0
    ):
        multipliers.append(0.5)
    elif (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 48.0
    ):
        multipliers.append(0.67)

    return min(multipliers)


'''
    opt_full = OPT_HEADER + opt_src
    verify_syntax(OPTION_GUID, opt_full)
    OPTION_GUID.write_text(opt_full, encoding="utf-8")
    print(f"  Written: {OPTION_GUID}")

    # ── 5. 寫入 gamma_squeeze_engine.py ──────────────────────────────────
    GAMMA_HEADER = '''\
"""
gamma_squeeze_engine.py — Nexus Gamma Squeeze 量化風控決策引擎。

從 intraday_pipeline.py 分離，包含：
- TraderAccountState / OptionHolding / TickerMarketData / AdvancedTraderOutput
  （向後相容 re-export，實際定義在 market_analysis.models.trader_models）
- NexusGammaSqueezeEngine（4 階段門檻、凱利倉位、Vanna 對沖）
"""
import math
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from market_time import ny_tz
from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    AdvancedTraderOutput,
)

logger = logging.getLogger(__name__)

'''
    # gamma_src contains: TraderAccountState ... class NexusGammaSqueezeEngine ... (end)
    # We need to strip the model class definitions (already in trader_models.py)
    # and keep only NexusGammaSqueezeEngine
    gamma_lines = gamma_src.splitlines(keepends=True)
    engine_start = None
    for i, ln in enumerate(gamma_lines):
        if re.match(r"^class NexusGammaSqueezeEngine", ln):
            engine_start = i
            break
    if engine_start is None:
        print("  ❌ Cannot find NexusGammaSqueezeEngine in block")
        sys.exit(1)

    engine_only = "".join(gamma_lines[engine_start:])
    gamma_full = GAMMA_HEADER + engine_only
    verify_syntax(GAMMA_ENG, gamma_full)
    GAMMA_ENG.write_text(gamma_full, encoding="utf-8")
    print(f"  Written: {GAMMA_ENG}")

    # ── 6. 修改 intraday_pipeline.py — 插入 imports，保留向後相容 shim ───
    print("\nPatching intraday_pipeline.py ...")

    NEW_IMPORTS = """\
from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    AdvancedTraderOutput,
)
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.signal_calculator import (
    _derive_buy_levels,
    _derive_sell_levels,
    _buy_zone_status,
    _sell_zone_status,
    _extract_pe_ratio,
    _is_mock,
    _get_tactical_model,
    calculate_dynamic_trading_signals,
)
from market_analysis.option_guidance import (
    _watchlist_event_risk_multiplier,
    derive_watchlist_option_guidance,
    build_watchlist_option_plan,
)
"""

    # Find the line "from services.market_data_service import BoundedCache" and insert after it
    insert_after = None
    for i, ln in enumerate(lines):
        if "from services.market_data_service import BoundedCache" in ln:
            insert_after = i
            break
    if insert_after is None:
        print("  ❌ Cannot find import anchor line")
        sys.exit(1)

    # Build new file: head + new imports + (skip extracted blocks) + remainder
    head = lines[: insert_after + 1]
    # sig_start .. mock_end  (derive_buy/sell + _is_mock + calculate_dynamic)
    # opt_start .. opt_end   (derive_watchlist_option_guidance .. TraderAccountState)
    # models_start .. models_end  (TraderAccountState .. IntradayScanPipeline)

    # Also remove _extract_pe_ratio and _buy/sell helpers (sig_start..sig_end)
    # and _watchlist_event_risk_multiplier (find it separately)
    wer_start = None
    wer_end = None
    for i, ln in enumerate(lines):
        if re.match(r"^def _watchlist_event_risk_multiplier\(", ln):
            wer_start = i
        if (
            wer_start is not None
            and i > wer_start
            and re.match(r"^(async )?def |^class ", ln)
        ):
            wer_end = i
            break
    if wer_start and not wer_end:
        wer_end = len(lines)

    print(
        f"  _watchlist_event_risk_multiplier: lines {wer_start+1 if wer_start else '?'}–{wer_end}"
    )

    # Collect "removed" ranges (sorted, non-overlapping)
    removed = sorted(
        [
            (sig_start, sig_end),  # _derive_buy_levels ... (before _is_mock)
            (
                mock_start,
                mock_end,
            ),  # _is_mock ... (before derive_watchlist_option_guidance)
            (wer_start, wer_end)
            if wer_start
            else (0, 0),  # _watchlist_event_risk_multiplier
            (opt_start, opt_end),  # derive_watchlist_option_guidance ...
            (
                models_start,
                models_end,
            ),  # TraderAccountState ... NexusGammaSqueezeEngine ...
        ],
        key=lambda x: x[0],
    )

    # Insert new imports after anchor
    new_lines: list[str] = list(head) + [NEW_IMPORTS + "\n"]

    # Add everything from anchor+1 to end, skipping removed ranges
    pos = insert_after + 1
    for rstart, rend in removed:
        if rend <= rstart:
            continue
        if pos < rstart:
            new_lines.extend(lines[pos:rstart])
        pos = max(pos, rend)
    new_lines.extend(lines[pos:])

    new_src = "".join(new_lines)
    verify_syntax(PIPELINE, new_src)
    PIPELINE.write_text(new_src, encoding="utf-8")
    print(f"  Written: {PIPELINE} ({len(new_src.splitlines())} lines)")

    print("\n✅ Refactoring complete.")


if __name__ == "__main__":
    main()
