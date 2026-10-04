"""擠壓進場的非同步協調：抓 K 線 → 矩陣 → 壓力區 → 規則。

否決條件（Regime IV 宏觀鎖定、財報／總經閥、逃頂警戒）涉及其他 I/O，由呼叫端
算好再傳入；本模組只負責與 K 線相關的部分，讓進場顧問與加碼共用同一份判定。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import market_time
from market_analysis.atr_utils import compute_atr_14_from_daily_df
from market_analysis.squeeze_entry.resistance import (
    ResistanceContext,
    detect_resistance,
)
from market_analysis.squeeze_entry.rules import (
    SqueezeEntryResult,
    evaluate_squeeze_entry,
)
from market_analysis.squeeze_entry.timeframes import (
    PsqMatrix,
    fetch_psq_matrix,
    split_daily_confirmed,
)


@dataclass(frozen=True)
class SqueezeEvaluation:
    result: SqueezeEntryResult
    matrix: PsqMatrix
    resistance: Optional[ResistanceContext]


async def evaluate_symbol(
    symbol: str,
    spot: float,
    hard_vetoes: Optional[List[str]] = None,
    downgrade_reason: Optional[str] = None,
    now_ny: Optional[datetime] = None,
) -> SqueezeEvaluation:
    if now_ny is None:
        now_ny = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    matrix, df_daily = await fetch_psq_matrix(symbol, now_ny)

    resistance: Optional[ResistanceContext] = None
    if df_daily is not None and not df_daily.empty and spot > 0:
        d_conf, _ = split_daily_confirmed(df_daily, now_ny.date())
        atr_1d = compute_atr_14_from_daily_df(d_conf)
        m65 = matrix.get("65m")
        resistance = detect_resistance(
            d_conf, spot, atr_1d, m65.last_close if m65 is not None else None
        )

    result = evaluate_squeeze_entry(matrix, resistance, hard_vetoes, downgrade_reason)
    return SqueezeEvaluation(result, matrix, resistance)
