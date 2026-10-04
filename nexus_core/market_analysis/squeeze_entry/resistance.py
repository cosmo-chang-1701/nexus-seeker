"""自動偵測橫向壓力區（擺盪高點群聚）。

規格見 `docs/strategies/10_multi_timeframe_squeeze_entry.md` §2.3。

* 擺盪高點：近 `_LOOKBACK_SESSIONS` 個已收盤交易日中，High 為左右各
  `_PIVOT_WING` 根內最高者。
* 群聚：依價位由低到高，與群組錨點相距 ≤ `_CLUSTER_ATR_MULT × ATR_1D` 者併為同一區，
  壓力區 = [群組最低, 群組最高]。
* 只用已收盤日線；ATR 由同一份日線就地計算，不另外抓取。
"""

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

_LOOKBACK_SESSIONS = 60
_PIVOT_WING = 2
_CLUSTER_ATR_MULT = 0.5
# 現價距壓力區下緣在此 ATR 倍數以內（或已進入區內）即視為「衝擊壓力中」。
APPROACH_ATR_MULT = 0.5
# 突破判定的回看根數：最近 N 根已收盤日線內，收盤由區上緣以下站上區上緣。
_BREAKOUT_LOOKBACK = 3


@dataclass(frozen=True)
class ResistanceZone:
    bottom: float
    top: float
    touches: int


@dataclass(frozen=True)
class ResistanceContext:
    """壓力區判定結果。所有價位皆為已收盤資料推得。"""

    atr_1d: float
    overhead: Optional[ResistanceZone]  # 現價上方（或正在區內）最近的壓力區
    is_approaching: bool  # 現價已進入 overhead 下緣 0.5×ATR 以內，待突破
    broken: Optional[ResistanceZone]  # 最近 N 根內收盤站上的壓力區（突破觸發）


def find_swing_highs(df_daily: pd.DataFrame) -> List[float]:
    highs = df_daily["High"].to_numpy(dtype=float)
    out: List[float] = []
    w = _PIVOT_WING
    for i in range(w, len(highs) - w):
        window = highs[i - w : i + w + 1]
        if highs[i] >= window.max():
            out.append(float(highs[i]))
    # 視窗最右側 _PIVOT_WING 根無法確認為擺盪高點，但若它就是區間最高價，
    # 仍代表一道尚未被超越的壓力，納入。
    if len(highs) > 0:
        tail_max = float(highs[-w:].max()) if len(highs) >= w else float(highs.max())
        if tail_max >= float(highs.max()) and tail_max not in out:
            out.append(tail_max)
    return out


def cluster_zones(pivots: List[float], atr_1d: float) -> List[ResistanceZone]:
    if not pivots or atr_1d <= 0:
        return []
    tol = _CLUSTER_ATR_MULT * atr_1d
    zones: List[ResistanceZone] = []
    group: List[float] = []
    for p in sorted(pivots):
        if group and p - group[0] > tol:
            zones.append(ResistanceZone(min(group), max(group), len(group)))
            group = []
        group.append(p)
    if group:
        zones.append(ResistanceZone(min(group), max(group), len(group)))
    return zones


def detect_resistance(
    df_daily_confirmed: pd.DataFrame,
    spot: float,
    atr_1d: float,
    last_65m_close: Optional[float] = None,
) -> ResistanceContext:
    """`df_daily_confirmed` 只能含已收盤日線（呼叫端負責截掉今日）。

    突破觸發有兩條路徑：近 3 根已收盤日線收盤站上區上緣，或（日線尚未確認時）
    最後一根已收盤 65m 收盤站上前一日收盤上方的壓力區上緣。兩者都要求現價仍在
    區上緣之上，避免已跌回區內的假突破被當成觸發。
    """
    empty = ResistanceContext(atr_1d, None, False, None)
    if (
        df_daily_confirmed is None
        or df_daily_confirmed.empty
        or spot <= 0
        or atr_1d <= 0
        or len(df_daily_confirmed) < _LOOKBACK_SESSIONS // 2
    ):
        return empty

    window = df_daily_confirmed.iloc[-_LOOKBACK_SESSIONS:]
    # 偵測現價上方壓力：用整段視窗
    zones = cluster_zones(find_swing_highs(window), atr_1d)
    overhead_candidates = [z for z in zones if z.top >= spot]
    overhead = min(overhead_candidates, key=lambda z: z.bottom, default=None)
    is_approaching = bool(
        overhead is not None and spot >= overhead.bottom - APPROACH_ATR_MULT * atr_1d
    )

    # 突破偵測：壓力區只用突破前的 K 棒推得，避免突破 K 棒本身成為新的擺盪高點。
    broken: Optional[ResistanceZone] = None
    if len(window) > _BREAKOUT_LOOKBACK + 1:
        pre = window.iloc[:-_BREAKOUT_LOOKBACK]
        ref_close = float(pre["Close"].iloc[-1])
        last_close = float(window["Close"].iloc[-1])
        pre_zones = cluster_zones(find_swing_highs(pre), atr_1d)
        crossed = [z for z in pre_zones if ref_close <= z.top < last_close]
        if crossed and spot > max(crossed, key=lambda z: z.top).top:
            broken = max(crossed, key=lambda z: z.top)

    if broken is None and last_65m_close is not None and last_65m_close > 0:
        d_close = float(window["Close"].iloc[-1])
        crossed_65 = [z for z in zones if d_close <= z.top < last_65m_close]
        if crossed_65 and spot > max(crossed_65, key=lambda z: z.top).top:
            broken = max(crossed_65, key=lambda z: z.top)

    return ResistanceContext(atr_1d, overhead, is_approaching, broken)
