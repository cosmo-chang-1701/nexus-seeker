"""outcome_labeling.py — 事後走勢標註的單一來源 (方向中性)。

同時被兩條路徑使用：
* `services/regime_outcome_labeler.py`：離峰排程回填 production 評估紀錄的事後走勢。
* `calibration/`：離線事件研究工具標註歷史事件。

兩者共用同一份標註定義，才能把「離線回測」與「前向蒐集」的結果放在同一張表上
比較；各寫一份必然在邊界條件 (同根 K 雙觸、時間窗切點) 上漂移。

輸出刻意**方向中性**：只記錄價格往哪走、先觸及哪條 ATR 帶。方向由分析端套用
(`directional_touch`)，因此同一列可做反事實分析——例如 Regime II 若做空會如何。

時間語意：
* 只使用 index **嚴格晚於** `entry_ts` 的 K 棒 (bar 的 index 是開盤時間，
  entry_ts 所在的那根含有評估前的價格，不得納入，否則即前視)。
* Session 以美東日期切分；+N 日 = 進場日之後第 N 個有資料的交易日收盤。
"""

import math
from datetime import datetime, timedelta
from typing import NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

LABEL_VERSION = 1
DEFAULT_K_GRID: tuple[float, ...] = (1.0, 1.5, 2.0)
DEFAULT_MAX_SESSIONS = 5

# first_touch 編碼
TOUCH_UP = 1
TOUCH_DOWN = -1
TOUCH_NONE = 0
TOUCH_BOTH_SAME_BAR = 2  # 同一根 K 棒同時觸及上下兩帶，無法判定先後

_NY_TZ = ZoneInfo("America/New_York")


class BarrierTouch(NamedTuple):
    k: float
    touch: int  # TOUCH_* 編碼
    bars_to_touch: Optional[int]


class ForwardPathLabel(NamedTuple):
    entry_ref_price: float
    atr_1d_ref: float
    fwd_ret_1h: Optional[float]
    fwd_ret_eod: Optional[float]
    fwd_ret_1d: Optional[float]
    fwd_ret_3d: Optional[float]
    fwd_ret_5d: Optional[float]
    max_up_atr_5d: Optional[float]
    max_down_atr_5d: Optional[float]
    touches: tuple[BarrierTouch, ...]


def _to_utc_index(bars: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        # `services.market_data_service.get_history_df` 回傳的 index 是去掉時區的
        # **美東時間** (例如 1h K 棒 09:30)。當成 UTC 會讓日內時點偏 4–5 小時、
        # 日線日期錯一天，直接造成前視或落後。
        idx = idx.tz_localize(_NY_TZ, ambiguous="NaT", nonexistent="shift_forward")
    else:
        idx = idx.tz_convert("UTC")
    out = bars.copy()
    out.index = idx
    return out.sort_index()


def _as_utc(ts: datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def horizon_window(
    bars: pd.DataFrame, entry_ts: datetime, max_sessions: int = DEFAULT_MAX_SESSIONS
) -> pd.DataFrame:
    """entry 之後 (嚴格晚於 entry_ts)、涵蓋進場日與其後 `max_sessions` 個交易日的 K 棒。"""
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = _to_utc_index(bars)
    entry_utc = _as_utc(entry_ts)
    after = frame[frame.index > entry_utc]
    if after.empty:
        return after
    et_dates = after.index.tz_convert(_NY_TZ).date
    entry_date = entry_utc.tz_convert(_NY_TZ).date()
    sessions = sorted({d for d in et_dates if d > entry_date})[:max_sessions]
    allowed = set(sessions) | {entry_date}
    return after[[d in allowed for d in et_dates]]


def first_barrier_touch(
    highs: Sequence[float],
    lows: Sequence[float],
    upper: float,
    lower: float,
) -> tuple[int, Optional[int]]:
    """依序掃描 K 棒，回傳 (先觸及編碼, 第幾根觸及 (1-based))；皆未觸及為 (0, None)。"""
    for i, (hi, lo) in enumerate(zip(highs, lows), start=1):
        hit_up = hi >= upper
        hit_down = lo <= lower
        if hit_up and hit_down:
            return TOUCH_BOTH_SAME_BAR, i
        if hit_up:
            return TOUCH_UP, i
        if hit_down:
            return TOUCH_DOWN, i
    return TOUCH_NONE, None


def directional_touch(touch: int, direction: str) -> int:
    """把方向中性的觸及編碼轉為「對該方向」的勝負：+1 有利帶先觸及、-1 不利帶
    先觸及、0 逾時。同根 K 雙觸一律判為不利 (保守)。"""
    if touch == TOUCH_NONE:
        return 0
    if touch == TOUCH_BOTH_SAME_BAR:
        return -1
    favorable = TOUCH_DOWN if direction == "SHORT" else TOUCH_UP
    return 1 if touch == favorable else -1


def label_forward_path(
    bars: pd.DataFrame,
    entry_ts: datetime,
    entry_price: float,
    atr_1d: float,
    k_grid: Sequence[float] = DEFAULT_K_GRID,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> Optional[ForwardPathLabel]:
    """標註單一進場點之後的走勢。資料不足 (無後續 K 棒或價格無效) 回傳 None。

    `bars` 需含 High / Low / Close 欄位；index 為 K 棒開盤時間 (naive 視為美東時間，
    與 get_history_df 的慣例一致)。`entry_ts` 若為 naive 則視為 UTC (DB 的
    CURRENT_TIMESTAMP)。
    """
    if (
        bars is None
        or bars.empty
        or not math.isfinite(entry_price)
        or entry_price <= 0
        or not math.isfinite(atr_1d)
        or atr_1d <= 0
    ):
        return None

    frame = _to_utc_index(bars)
    entry_utc = _as_utc(entry_ts)
    after = frame[frame.index > entry_utc]
    if after.empty:
        return None

    et_dates = after.index.tz_convert(_NY_TZ).date
    entry_date = entry_utc.tz_convert(_NY_TZ).date()
    session_dates = sorted({d for d in et_dates if d > entry_date})

    def _ret(close: float) -> float:
        return float(close / entry_price - 1.0)

    # +1h：entry 後一小時內最後一根的收盤；資料必須延伸超過該時點才算數。
    fwd_1h: Optional[float] = None
    one_hour = entry_utc + pd.Timedelta(hours=1)
    if after.index[-1] >= one_hour:
        window = after[after.index <= one_hour]
        if not window.empty:
            fwd_1h = _ret(float(window["Close"].iloc[-1]))

    # 當日收盤：進場日最後一根；需有隔日資料證明當日已收完。
    fwd_eod: Optional[float] = None
    same_day = after[et_dates == entry_date]
    if not same_day.empty and session_dates:
        fwd_eod = _ret(float(same_day["Close"].iloc[-1]))

    def _session_close(n: int) -> Optional[float]:
        if len(session_dates) < n:
            return None
        target = session_dates[n - 1]
        day = after[et_dates == target]
        if day.empty:
            return None
        return _ret(float(day["Close"].iloc[-1]))

    fwd_1d = _session_close(1)
    fwd_3d = _session_close(3)
    fwd_5d = _session_close(5)

    horizon = horizon_window(frame, entry_ts, max_sessions)
    highs = [float(v) for v in horizon["High"].tolist()]
    lows = [float(v) for v in horizon["Low"].tolist()]

    max_up: Optional[float] = None
    max_down: Optional[float] = None
    if highs:
        max_up = max(0.0, (max(highs) - entry_price) / atr_1d)
        max_down = max(0.0, (entry_price - min(lows)) / atr_1d)

    touches: list[BarrierTouch] = []
    for k in k_grid:
        touch, n_bars = first_barrier_touch(
            highs, lows, entry_price + k * atr_1d, entry_price - k * atr_1d
        )
        touches.append(BarrierTouch(float(k), touch, n_bars))

    return ForwardPathLabel(
        entry_ref_price=float(entry_price),
        atr_1d_ref=float(atr_1d),
        fwd_ret_1h=fwd_1h,
        fwd_ret_eod=fwd_eod,
        fwd_ret_1d=fwd_1d,
        fwd_ret_3d=fwd_3d,
        fwd_ret_5d=fwd_5d,
        max_up_atr_5d=max_up,
        max_down_atr_5d=max_down,
        touches=tuple(touches),
    )


def plan_outcome(
    bars: pd.DataFrame,
    entry_ts: datetime,
    direction: str,
    stop_price: Optional[float],
    target_price: Optional[float],
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> Optional[int]:
    """評估當下自帶的停損／目標何者先觸及：+1 目標先、-1 停損先 (同根雙觸算停損)、
    0 窗口內皆未觸及、None 無價位或無資料。"""
    if (
        not stop_price
        or not target_price
        or stop_price <= 0
        or target_price <= 0
        or bars is None
        or bars.empty
    ):
        return None
    window = horizon_window(bars, entry_ts, max_sessions)
    if window.empty:
        return None
    highs = [float(v) for v in window["High"].tolist()]
    lows = [float(v) for v in window["Low"].tolist()]
    if direction == "SHORT":
        touch, _ = first_barrier_touch(highs, lows, stop_price, target_price)
        mapping = {TOUCH_UP: -1, TOUCH_DOWN: 1, TOUCH_BOTH_SAME_BAR: -1}
    else:
        touch, _ = first_barrier_touch(highs, lows, target_price, stop_price)
        mapping = {TOUCH_UP: 1, TOUCH_DOWN: -1, TOUCH_BOTH_SAME_BAR: -1}
    return mapping.get(touch, 0)


def labeling_ready_before(
    now_utc: datetime, sessions: int = DEFAULT_MAX_SESSIONS
) -> datetime:
    """保守估計「已走完 N 個交易日」的評估時間截點 (N 個交易日 ≈ N × 7/5 日曆日 + 1)。

    精確的交易日曆判定交由呼叫端 (有 pandas_market_calendars 時)；本函式只提供
    不依賴外部套件的下限，確保不會標註尚未走完窗口的紀錄。
    """
    calendar_days = math.ceil(sessions * 7 / 5) + 1
    return now_utc - timedelta(days=calendar_days)
