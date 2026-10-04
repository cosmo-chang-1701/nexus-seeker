"""多時間框架擠壓進場（取代右側六重鐵律的多頭建倉與加碼判定）。

規格：`docs/strategies/10_multi_timeframe_squeeze_entry.md`。
"""

from market_analysis.squeeze_entry.evaluator import SqueezeEvaluation, evaluate_symbol
from market_analysis.squeeze_entry.resistance import (
    ResistanceContext,
    ResistanceZone,
    detect_resistance,
)
from market_analysis.squeeze_entry.rules import (
    STATUS_ENTRY,
    STATUS_NO_DATA,
    STATUS_VETOED,
    STATUS_WATCH,
    TIER_SIZE_PCT,
    SqueezeEntryResult,
    evaluate_squeeze_entry,
)
from market_analysis.squeeze_entry.timeframes import (
    TIMEFRAMES,
    PsqMatrix,
    TimeframeState,
    fetch_psq_matrix,
)

__all__ = [
    "SqueezeEvaluation",
    "evaluate_symbol",
    "ResistanceContext",
    "ResistanceZone",
    "detect_resistance",
    "STATUS_ENTRY",
    "STATUS_NO_DATA",
    "STATUS_VETOED",
    "STATUS_WATCH",
    "TIER_SIZE_PCT",
    "SqueezeEntryResult",
    "evaluate_squeeze_entry",
    "TIMEFRAMES",
    "PsqMatrix",
    "TimeframeState",
    "fetch_psq_matrix",
]
