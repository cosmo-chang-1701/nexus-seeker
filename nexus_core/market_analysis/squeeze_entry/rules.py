"""多時間框架擠壓進場規則（純函式，不做任何 I/O）。

規格見 `docs/strategies/10_multi_timeframe_squeeze_entry.md` §2.2／§2.4。

等級與建議部位（占總資產 %）：

| 等級 | 條件（皆需 D 動能 > 0 且 W 動能非紅） | 部位 |
| T1 | W／3D／D 任一擠壓中，且 15m/5m 出現 Green Dot 或 Turbo（或壓力區突破） | 1% |
| T2 | ≥ 3 個時間框架擠壓中（含 D），且 D 動能上升（淺藍） | 1.5% |
| T3 | D Green Dot，或 3D Turbo 且 ≥ 3 個時間框架擠壓中 | 2.5% |

壓力區：現價在最近壓力區下緣 0.5×ATR 以內且未突破 → **只標註、不擋**（照常建議與
推播，理由與 `resistance_warning` 附上「正在衝擊壓力區」）。日線事件研究顯示被擋下
的訊號報酬不比放行的差，且自動偵測的價位不一定是使用者認定的那條線，由使用者
看圖決定比替他擋掉合適（使用者 2026-10-04 決定）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from market_analysis.squeeze_entry.resistance import ResistanceContext
from market_analysis.squeeze_entry.timeframes import (
    HIGHER_TIMEFRAMES,
    INTRADAY_TRIGGER_TIMEFRAMES,
    PsqMatrix,
)

TIER_SIZE_PCT: Dict[int, float] = {1: 1.0, 2: 1.5, 3: 2.5}
TIER_LABEL: Dict[int, str] = {1: "T1", 2: "T2", 3: "T3"}
_MIN_SQUEEZE_FOR_WATCH = 2
_MIN_SQUEEZE_MULTI = 3
_STOP_ATR_BUFFER = 0.5

# 狀態值（供推播、前向記錄與面板共用）
STATUS_ENTRY = "ENTRY"  # 達 T1 以上且無否決 → 可推播
STATUS_WATCH = "WATCH"  # 多時間框架擠壓中，尚無觸發
STATUS_NONE = "NONE"
STATUS_VETOED = "VETOED"
STATUS_NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class SqueezeEntryResult:
    status: str
    tier: Optional[int]  # 1／2／3；未達等級為 None
    size_pct: Optional[float]
    stop: Optional[float]
    reason: str
    triggers: List[str] = field(default_factory=list)
    squeeze_count: int = 0
    resistance_note: Optional[str] = None
    # 正在衝擊尚未突破的壓力區時的警示文字（只標註、不影響判定）；否則為 None。
    resistance_warning: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == STATUS_ENTRY


def _reference_stop(matrix: PsqMatrix, atr_1d: float) -> Optional[float]:
    """參考停損 = min(D 擠壓區間低點, D 20SMA) − 0.5×ATR_1D。

    刻意不用 15m/5m 的擠壓區間：日內區間對 B&H 部位過窄，會被日內雜訊洗掉。
    """
    d = matrix.get("D")
    if d is None or atr_1d <= 0:
        return None
    anchors = [v for v in (d.squeeze_range_low, d.sma_20) if v is not None and v > 0]
    if not anchors:
        return None
    stop = min(anchors) - _STOP_ATR_BUFFER * atr_1d
    return round(stop, 2) if stop > 0 else None


def _grade(matrix: PsqMatrix, breakout: bool) -> Tuple[Optional[int], List[str], int]:
    d = matrix["D"]
    squeezing = [tf for tf, st in matrix.items() if st.is_squeezing]
    count = len(squeezing)
    higher_sq = any(matrix[tf].is_squeezing for tf in HIGHER_TIMEFRAMES if tf in matrix)
    triggers: List[str] = []

    three_d = matrix.get("3D")
    if d.green_dot:
        triggers.append("D Green Dot")
    if three_d is not None and three_d.turbo and count >= _MIN_SQUEEZE_MULTI:
        triggers.append("3D Turbo")
    if triggers:
        return 3, triggers, count

    if (
        count >= _MIN_SQUEEZE_MULTI
        and d.is_squeezing
        and d.momentum_color == "LightBlue"
    ):
        return 2, [f"{count} 個時間框架擠壓"], count

    intraday = [
        f"{tf} {'Green Dot' if matrix[tf].green_dot else 'Turbo'}"
        for tf in INTRADAY_TRIGGER_TIMEFRAMES
        if tf in matrix and (matrix[tf].green_dot or matrix[tf].turbo)
    ]
    if breakout:
        intraday.append("壓力區突破")
    if higher_sq and intraday:
        return 1, intraday, count

    return None, [], count


def evaluate_squeeze_entry(
    matrix: PsqMatrix,
    resistance: Optional[ResistanceContext],
    hard_vetoes: Optional[List[str]] = None,
    downgrade_reason: Optional[str] = None,
) -> SqueezeEntryResult:
    """依多時間框架擠壓矩陣判定進場等級。

    Args:
        hard_vetoes: 任何一項成立即不給建議（Regime IV 宏觀鎖定、財報／總經閥）。
        downgrade_reason: 非 None 時等級降一級（逃頂警戒 tier != NORMAL）。
    """
    if "D" not in matrix:
        return SqueezeEntryResult(
            STATUS_NO_DATA, None, None, None, "⛔ 缺少 D 擠壓資料，不給建議"
        )

    # W 缺席只會是「上市未滿 40 週」：D 存在代表日線抓取成功，週線由同一份
    # 日線重採樣而來。新上市股略過 W 條件（使用者 2026-10-04 決定），並在理由中
    # 揭露，而不是永遠不給建議。
    d, w = matrix["D"], matrix.get("W")
    w_note = "（上市未滿 40 週，略過週線條件）" if w is None else ""
    atr = resistance.atr_1d if resistance is not None else 0.0
    stop = _reference_stop(matrix, atr)

    w_red = w is not None and w.momentum_color == "Red"
    if d.momentum_value <= 0 or w_red:
        why = []
        if d.momentum_value <= 0:
            why.append("D 動能 ≤ 0")
        if w_red:
            why.append("W 動能轉弱（紅）")
        count = sum(1 for st in matrix.values() if st.is_squeezing)
        return SqueezeEntryResult(
            STATUS_NONE,
            None,
            None,
            stop,
            "⏸️ 多頭前提不成立：" + "、".join(why),
            squeeze_count=count,
        )

    breakout = resistance is not None and resistance.broken is not None
    tier, triggers, count = _grade(matrix, breakout)

    note: Optional[str] = None
    if resistance is not None and resistance.broken is not None:
        note = (
            f"已突破壓力區 ${resistance.broken.bottom:.2f}–${resistance.broken.top:.2f}"
        )
    elif resistance is not None and resistance.overhead is not None:
        z = resistance.overhead
        note = f"上方壓力區 ${z.bottom:.2f}–${z.top:.2f}（觸及 {z.touches} 次）"

    if tier is None:
        status = STATUS_WATCH if count >= _MIN_SQUEEZE_FOR_WATCH else STATUS_NONE
        reason = (
            f"👀 {count} 個時間框架擠壓中，等待觸發"
            if status == STATUS_WATCH
            else "⏸️ 尚未形成擠壓進場條件"
        )
        return SqueezeEntryResult(status, None, None, stop, reason, [], count, note)

    if hard_vetoes:
        return SqueezeEntryResult(
            STATUS_VETOED,
            tier,
            None,
            stop,
            "⛔ " + "；".join(hard_vetoes),
            triggers,
            count,
            note,
        )

    downgrade_text = w_note
    if downgrade_reason:
        tier -= 1
        downgrade_text = f"（{downgrade_reason}，降一級）{w_note}"
        if tier < 1:
            return SqueezeEntryResult(
                STATUS_WATCH,
                None,
                None,
                stop,
                f"👀 擠壓條件成立但{downgrade_reason}，降級為觀察",
                triggers,
                count,
                note,
            )

    # 衝擊中的壓力區一定尚未突破（overhead 定義為 top >= 現價）。即使剛突破
    # 一個較低的壓力區，又頂到上方下一個壓力區時同樣要標註。只標註、不擋。
    warning: Optional[str] = None
    if (
        resistance is not None
        and resistance.is_approaching
        and resistance.overhead is not None
    ):
        warning = (
            f"⚠️ 正在衝擊壓力區 ${resistance.overhead.bottom:.2f}–"
            f"${resistance.overhead.top:.2f}，尚未突破"
        )

    reason = f"✅ {TIER_LABEL[tier]}：{'、'.join(triggers)}{downgrade_text}"
    if warning:
        reason = f"{reason} | {warning}"
    return SqueezeEntryResult(
        STATUS_ENTRY,
        tier,
        TIER_SIZE_PCT[tier],
        stop,
        reason,
        triggers,
        count,
        note,
        warning,
    )
