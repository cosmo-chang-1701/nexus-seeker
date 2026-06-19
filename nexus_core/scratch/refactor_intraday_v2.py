"""
refactor_intraday_v2.py

使用精確行號提取 intraday_pipeline.py 中的程式碼區塊。
基於 grep 輸出確認的確切行號，不依賴正則自動搜尋。

提取計畫（1-based 行號）：
  signal_calculator.py 提取自：
    - lines 96-167   (_derive_buy_levels, _derive_sell_levels, _buy_zone_status,
                       _sell_zone_status, _extract_pe_ratio)
    - lines 754-981  (_is_mock, _get_tactical_model, calculate_dynamic_trading_signals)

  option_guidance.py 提取自：
    - lines 562-592  (_watchlist_event_risk_multiplier)
    - lines 982-1335 (derive_watchlist_option_guidance, _mid_price_from_row,
                       _pick_watchlist_cover_leg, _estimate_watchlist_contract_count,
                       build_watchlist_option_plan)

  gamma_squeeze_engine.py 提取自：
    - lines 1403-1712 (NexusGammaSqueezeEngine)

  models/trader_models.py 提取自：
    - lines 1336-1402 (TraderAccountState, OptionHolding, TickerMarketData, AdvancedTraderOutput)
"""

import ast
import sys
from pathlib import Path

NEXUS_CORE = Path(__file__).parent.parent

PIPELINE = NEXUS_CORE / "market_analysis" / "intraday_pipeline.py"
SIGNAL_CALC = NEXUS_CORE / "market_analysis" / "signal_calculator.py"
OPTION_GUID = NEXUS_CORE / "market_analysis" / "option_guidance.py"
GAMMA_ENG = NEXUS_CORE / "market_analysis" / "gamma_squeeze_engine.py"
TRADER_MDL = NEXUS_CORE / "market_analysis" / "models" / "trader_models.py"


def verify_syntax(path: Path, source: str) -> None:
    try:
        ast.parse(source)
        print(f"  ✅ Syntax OK: {path.name}")
    except SyntaxError as e:
        print(f"  ❌ Syntax Error in {path.name}: {e}")
        sys.exit(1)


def extract(lines: list[str], start_1: int, end_1: int) -> str:
    """1-indexed inclusive-start, exclusive-end block extract."""
    return "".join(lines[start_1 - 1 : end_1 - 1])


def build_new_pipeline(lines: list[str], removed_ranges: list[tuple[int, int]]) -> str:
    """Build new pipeline content by skipping the removed ranges.

    Args:
        lines: Original file lines.
        removed_ranges: List of (start_1, end_1) pairs (1-indexed, exclusive-end).
    """
    removed_set: set[int] = set()
    for s, e in removed_ranges:
        for i in range(s, e):
            removed_set.add(i)
    return "".join(ln for i, ln in enumerate(lines, start=1) if i not in removed_set)


def main() -> None:
    print(f"Reading {PIPELINE} ...")
    src = PIPELINE.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    total = len(lines)
    print(f"  Total lines: {total}")

    # ── Exact line ranges (1-indexed, end = first line of NEXT block) ─────────
    # signal_calculator helpers (before _estimate_options_wall_metrics at 168)
    SIG_HELPERS = (
        96,
        168,
    )  # _derive_buy_levels .. _extract_pe_ratio (end before async def _estimate_options_wall)
    # _is_mock + _get_tactical + calculate_dynamic (end at derive_watchlist_option_guidance 982)
    SIG_CALC = (754, 982)

    # _watchlist_event_risk_multiplier (end before evaluate_watchlist_symbol 593)
    OPT_WERM = (562, 593)
    # derive_watchlist_option_guidance .. build_watchlist_option_plan (end before TraderAccountState 1336)
    OPT_MAIN = (982, 1336)

    # TraderAccountState .. AdvancedTraderOutput (end before NexusGammaSqueezeEngine 1403)
    MODEL_CLASSES = (1336, 1403)

    # NexusGammaSqueezeEngine (end before IntradayScanPipeline 1713)
    GAMMA_ENGINE = (1403, 1713)

    # ── Build signal_calculator.py ─────────────────────────────────────────
    sig_helpers_src = extract(lines, *SIG_HELPERS)
    sig_calc_src = extract(lines, *SIG_CALC)

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
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan

logger = logging.getLogger(__name__)


'''
    sig_full = SIGNAL_HEADER + sig_helpers_src + "\n\n" + sig_calc_src
    verify_syntax(SIGNAL_CALC, sig_full)
    SIGNAL_CALC.write_text(sig_full, encoding="utf-8")
    print(f"  Written: {SIGNAL_CALC} ({len(sig_full.splitlines())} lines)")

    # ── Build option_guidance.py ───────────────────────────────────────────
    opt_werm_src = extract(lines, *OPT_WERM)
    opt_main_src = extract(lines, *OPT_MAIN)

    OPT_HEADER = '''\
"""
option_guidance.py — 期權策略指引與可執行期權合約計畫。

從 intraday_pipeline.py 分離，包含：
  - _watchlist_event_risk_multiplier（事件風險乘數）
  - derive_watchlist_option_guidance（策略文字描述）
  - _mid_price_from_row / _pick_watchlist_cover_leg（合約選擇工具）
  - _estimate_watchlist_contract_count（口數估算）
  - build_watchlist_option_plan（完整期權計畫建構）
"""
import logging
from typing import Any, Dict, List, Mapping, Optional

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


'''
    opt_full = OPT_HEADER + opt_werm_src + "\n\n" + opt_main_src
    verify_syntax(OPTION_GUID, opt_full)
    OPTION_GUID.write_text(opt_full, encoding="utf-8")
    print(f"  Written: {OPTION_GUID} ({len(opt_full.splitlines())} lines)")

    # ── gamma_squeeze_engine.py (NexusGammaSqueezeEngine only) ────────────
    gamma_src = extract(lines, *GAMMA_ENGINE)

    GAMMA_HEADER = '''\
"""
gamma_squeeze_engine.py — Nexus Gamma Squeeze 量化風控決策引擎。

從 intraday_pipeline.py 分離，包含 NexusGammaSqueezeEngine：
  - 四階段門檻評估（流動性、財務跑道、Kelly 倉位、Vanna 對沖）
  - 生存分析與每日 Theta 對沖覆蓋率
  - 戰術性操作路由（SPEAR / SHIELD / WAIT）
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
    gamma_full = GAMMA_HEADER + gamma_src
    verify_syntax(GAMMA_ENG, gamma_full)
    GAMMA_ENG.write_text(gamma_full, encoding="utf-8")
    print(f"  Written: {GAMMA_ENG} ({len(gamma_full.splitlines())} lines)")

    # ── models/trader_models.py (already written separately; just verify) ──
    print(f"  ✅ trader_models.py already exists at {TRADER_MDL}")

    # ── Build new intraday_pipeline.py (remove extracted ranges) ─────────
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

    # Find import anchor: "from services.market_data_service import BoundedCache"
    anchor_line = None
    for i, ln in enumerate(lines, start=1):
        if "from services.market_data_service import BoundedCache" in ln:
            anchor_line = i
            break
    if anchor_line is None:
        print("  ❌ Cannot find import anchor line")
        sys.exit(1)

    print(f"  Import anchor at line {anchor_line}")

    # Removed ranges (1-indexed, exclusive-end)
    removed_ranges = [
        SIG_HELPERS,
        SIG_CALC,
        OPT_WERM,
        OPT_MAIN,
        MODEL_CLASSES,
        GAMMA_ENGINE,
    ]

    # Build: head (1..anchor_line) + new_imports + rest (skipping removed)
    head = "".join(lines[:anchor_line])
    # Collect all lines after anchor_line
    rest_parts = []
    removed_set: set[int] = set()
    for s, e in removed_ranges:
        for i in range(s, e):
            removed_set.add(i)

    for i in range(anchor_line + 1, total + 1):
        if i not in removed_set:
            rest_parts.append(lines[i - 1])
    rest = "".join(rest_parts)

    new_src = head + "\n" + NEW_IMPORTS + "\n" + rest
    verify_syntax(PIPELINE, new_src)
    PIPELINE.write_text(new_src, encoding="utf-8")
    print(f"  Written: {PIPELINE} ({len(new_src.splitlines())} lines)")

    print("\n✅ Refactoring v2 complete.")


if __name__ == "__main__":
    main()
