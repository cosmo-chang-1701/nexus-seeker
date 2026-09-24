"""notification_outcome.py — 可行動通知「照做 vs 持有不動」的反事實淨值路徑（純函式）。

由 03:30 ET 的 `services/regime_outcome_labeler.run_dispatch_outcome_labeling()` 與離線
報告 `calibration/notif_report.py` 共用，確保兩處對「照做」的定義一致。

反事實建構（每則通知獨立、以 1 單位名目部位計）：

| signal_kind | 持有不動 (hold) 曝險 | 照做 (follow) 曝險 |
| :--- | :--- | :--- |
| ENTRY  | 0（留在現金） | +ratio（做多）或 −ratio（做空） |
| REDUCE | 1（維持持倉） | 1 − ratio |
| EXIT   | 1（維持持倉） | 0（出場至現金） |

每日組合報酬 = 曝險 × 標的日報酬 + (1 − |曝險|) × 現金日利率。REDUCE / EXIT 的
`direction == "SHORT"` 代表被調整的是空頭部位，曝險取負號。

無前視：
* 價格序列只取「送達之後」的日線：送達時點若在美東 16:00 之前，當日收盤是第一個觀測；
  之後（或非交易日）則從下一個交易日開始。
* 參考價優先用送達當下的 `price`；缺值時用第一個觀測日**之前**最後一根收盤。
* `get_history_df` 的日線 index 是去時區的**美東**日期（見 AGENTS.md 對
  `outcome_labeling.py` 的警告），因此直接以美東日期比較，不做 UTC 轉換。
"""

from __future__ import annotations

from datetime import datetime, time
from typing import NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from market_analysis.downside_risk import max_drawdown, nav_from_returns

OUTCOME_LABEL_VERSION = 1
DEFAULT_HORIZON_SESSIONS = 20
# 延伸視窗：停損、逃頂、基本面出場這類防護訊號的價值常在更長期間才顯現（例如 2025 年
# NVDA 在 SL1 之後三個月才跌到 −37%），20 日視窗會系統性低估它們。labeler 先以 20 日
# 標註，滿 60 個交易日後以 60 日路徑覆寫同一列；報告兩個視窗並列。
EXTENDED_HORIZON_SESSIONS = 60
REPORT_HORIZONS: tuple[int, ...] = (DEFAULT_HORIZON_SESSIONS, EXTENDED_HORIZON_SESSIONS)
# 與 calibration/backtest_engine_2025.py 相同的 MAR / 無風險利率基準
DEFAULT_RF_ANNUAL = 0.045

_NY_TZ = ZoneInfo("America/New_York")
_MARKET_CLOSE = time(16, 0)


class ForwardDailyPath(NamedTuple):
    entry_ref_price: float
    returns: list[float]  # 第 0 期為參考價 → 第一個觀測日收盤，其後為逐日收盤報酬


class CounterfactualPaths(NamedTuple):
    follow: list[float]
    hold: list[float]
    follow_total_return: float
    hold_total_return: float
    follow_mdd: float
    hold_mdd: float


def _et_dates(daily: pd.DataFrame) -> list:
    idx = pd.DatetimeIndex(daily.index)
    if idx.tz is not None:
        idx = idx.tz_convert(_NY_TZ)
    return [ts.date() for ts in idx]


def forward_daily_returns(
    daily: Optional[pd.DataFrame],
    dispatched_at_utc: datetime,
    price: Optional[float] = None,
    horizon: int = DEFAULT_HORIZON_SESSIONS,
) -> Optional[ForwardDailyPath]:
    """送達後 `horizon` 個交易日的日報酬；資料不足（尚未走完或缺資料）回傳 None。

    回傳 `horizon + 1` 期：第 0 期是參考價到第一個觀測日收盤（送達當日若在收盤前即為
    當日），其後 `horizon` 期為逐日收盤報酬。
    """
    if daily is None or daily.empty or "Close" not in daily:
        return None
    moment = dispatched_at_utc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    local = moment.astimezone(_NY_TZ)
    dispatch_date = local.date()
    before_close = local.time() < _MARKET_CLOSE

    closes = [float(c) for c in daily["Close"].tolist()]
    dates = _et_dates(daily)
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    dates = [dates[i] for i in order]
    closes = [closes[i] for i in order]

    first = next(
        (
            i
            for i, d in enumerate(dates)
            if d > dispatch_date or (d == dispatch_date and before_close)
        ),
        None,
    )
    if first is None:
        return None
    window = closes[first : first + horizon + 1]
    if len(window) < horizon + 1 or any(c <= 0 for c in window):
        return None

    ref = float(price) if price is not None and price > 0 else None
    if ref is None:
        if first == 0 or closes[first - 1] <= 0:
            return None
        ref = closes[first - 1]

    returns = [window[0] / ref - 1.0]
    returns += [window[k] / window[k - 1] - 1.0 for k in range(1, len(window))]
    return ForwardDailyPath(entry_ref_price=ref, returns=returns)


def exposures(
    signal_kind: str, direction: str, exposure_ratio: float
) -> Optional[tuple[float, float]]:
    """(照做曝險, 持有曝險)；INFO 或未知種類回傳 None。"""
    ratio = max(0.0, min(1.0, float(exposure_ratio)))
    sign = -1.0 if str(direction).upper() == "SHORT" else 1.0
    kind = str(signal_kind).upper()
    if kind == "ENTRY":
        return sign * ratio, 0.0
    if kind == "REDUCE":
        return sign * (1.0 - ratio), sign
    if kind == "EXIT":
        return 0.0, sign
    return None


def _portfolio_returns(
    returns: Sequence[float], exposure: float, rf_daily: float
) -> list[float]:
    cash = 1.0 - abs(exposure)
    return [exposure * r + cash * rf_daily for r in returns]


def counterfactual_paths(
    returns: Sequence[float],
    signal_kind: str,
    direction: str = "LONG",
    exposure_ratio: float = 1.0,
    rf_annual: float = DEFAULT_RF_ANNUAL,
) -> Optional[CounterfactualPaths]:
    pair = exposures(signal_kind, direction, exposure_ratio)
    if pair is None or not returns:
        return None
    follow_e, hold_e = pair
    rf_daily = rf_annual / 252.0
    follow = _portfolio_returns(returns, follow_e, rf_daily)
    hold = _portfolio_returns(returns, hold_e, rf_daily)
    follow_nav = nav_from_returns(follow)
    hold_nav = nav_from_returns(hold)
    return CounterfactualPaths(
        follow=follow,
        hold=hold,
        follow_total_return=float(follow_nav[-1] - 1.0),
        hold_total_return=float(hold_nav[-1] - 1.0),
        follow_mdd=max_drawdown(follow_nav).max_drawdown,
        hold_mdd=max_drawdown(hold_nav).max_drawdown,
    )
