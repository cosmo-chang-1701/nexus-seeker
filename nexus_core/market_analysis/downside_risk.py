"""下行風險指標的單一權威定義：Sortino、最大回撤 (MDD)、歷史模擬 VaR / CVaR。

本模組是刻意只依賴 numpy 的葉模組（比照 `room_threshold.py` / `sentiment/skew_taxonomy.py`），
離線回測（`calibration/backtest_engine_2025.py`）與日後的即時投組下行風險監控共用同一份
定義，避免兩處各自實作而數字對不起來。

評估優先序（見 `docs/risk_portfolio/07_downside_risk_sortino_var_cvar.md`）：
Sortino 為主，MDD 與 VaR / CVaR 為輔。Sortino 只懲罰「低於 MAR 的報酬」，不懲罰上行
波動——對 Buy & Hold 投組而言，砍掉上行的操作不會讓 Sortino 變好，只會讓分子變小；
Sharpe 對上下行一視同仁，會把「砍獲利部位」誤判為風險改善，因此 `sharpe_ratio()`
只保留作為回測描述欄位，**不得作為任何決策或判讀依據**。

慣例：
- `returns` 為**單期簡單報酬**（例如日報酬 0.01 = +1%）。
- VaR / CVaR / MDD 一律回傳**正值的損失比例**（0.05 = 虧損 5%）。
- 年化以 `periods` 期為一年（日報酬 = 252）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

import numpy as np

TRADING_DAYS_PER_YEAR = 252

# 歷史模擬 VaR / CVaR 的最低樣本數。95% 信賴水準下 60 筆樣本的左尾只有 3 筆，
# 再少就只是在描述單一事件，不再是分佈的估計。
MIN_VAR_SAMPLES = 60

DEFAULT_VAR_CONFIDENCE = 0.95


class DrawdownResult(NamedTuple):
    max_drawdown: float
    peak_index: int
    trough_index: int


class VarCvarResult(NamedTuple):
    var: float
    cvar: float


def _as_array(values: Sequence[float] | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    finite: np.ndarray = arr[np.isfinite(arr)]
    return finite


def downside_deviation(
    returns: Sequence[float] | np.ndarray, mar_per_period: float = 0.0
) -> float:
    """單期下行差：sqrt( (1/N) * Σ min(0, r_t − MAR)² )。

    分母是**全部樣本數 N**，不是負報酬樣本數。只對負報酬取均值會讓「虧損次數少但
    每次都大」與「虧損次數多但每次都小」得到相同的下行差，等於丟掉了虧損頻率。
    """
    arr = _as_array(returns)
    if arr.size == 0:
        return 0.0
    shortfall = np.minimum(0.0, arr - mar_per_period)
    return float(np.sqrt(np.mean(shortfall**2)))


def annualized_downside_deviation(
    returns: Sequence[float] | np.ndarray,
    mar_annual: float = 0.0,
    periods: int = TRADING_DAYS_PER_YEAR,
) -> float:
    return downside_deviation(returns, mar_annual / periods) * float(np.sqrt(periods))


def annualized_return(
    returns: Sequence[float] | np.ndarray, periods: int = TRADING_DAYS_PER_YEAR
) -> float:
    """以複利累積後年化的幾何報酬。"""
    arr = _as_array(returns)
    if arr.size == 0:
        return 0.0
    growth = float(np.prod(1.0 + arr))
    if growth <= 0.0:
        return -1.0
    return float(growth ** (periods / arr.size) - 1.0)


def sortino_ratio(
    returns: Sequence[float] | np.ndarray,
    mar_annual: float = 0.0,
    periods: int = TRADING_DAYS_PER_YEAR,
    annual_return: float | None = None,
) -> float:
    """年化 Sortino = (年化報酬 − MAR) / 年化下行差（下行差同以 MAR 為界）。

    `annual_return` 未提供時以 `returns` 的幾何年化報酬計算；回測呼叫端傳入以期初
    本金精算的 CAGR，使分子與報告中的 CAGR 一致。下行差為 0（整段期間沒有任何一期
    低於 MAR）時回傳 0.0，而不是無限大——後者會讓任何排序或比較失去意義。
    """
    dd = annualized_downside_deviation(returns, mar_annual, periods)
    if dd <= 0.0:
        return 0.0
    r = annualized_return(returns, periods) if annual_return is None else annual_return
    return (r - mar_annual) / dd


def sharpe_ratio(
    returns: Sequence[float] | np.ndarray,
    rf_annual: float = 0.0,
    periods: int = TRADING_DAYS_PER_YEAR,
    annual_return: float | None = None,
) -> float:
    """年化 Sharpe。**僅供回測報告描述，不作任何判讀或決策依據**（理由見模組 docstring）。"""
    arr = _as_array(returns)
    if arr.size < 2:
        return 0.0
    vol = float(np.std(arr) * np.sqrt(periods))
    if vol <= 0.0:
        return 0.0
    r = annualized_return(arr, periods) if annual_return is None else annual_return
    return (r - rf_annual) / vol


def max_drawdown(nav: Sequence[float] | np.ndarray) -> DrawdownResult:
    """淨值序列的最大回撤（正值），以及對應的高點與低點索引。"""
    arr = np.asarray(nav, dtype=float)
    if arr.size == 0:
        return DrawdownResult(0.0, 0, 0)
    peaks = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - arr) / peaks, 0.0)
    trough = int(np.argmax(dd))
    peak = int(np.argmax(arr[: trough + 1])) if trough > 0 else 0
    return DrawdownResult(float(dd[trough]), peak, trough)


def current_drawdown(nav: Sequence[float] | np.ndarray) -> float:
    """最後一點相對於歷史高點的回撤（正值）。"""
    arr = np.asarray(nav, dtype=float)
    if arr.size == 0:
        return 0.0
    peak = float(np.max(arr))
    if peak <= 0.0:
        return 0.0
    return max(0.0, (peak - float(arr[-1])) / peak)


def nav_from_returns(
    returns: Sequence[float] | np.ndarray, start: float = 1.0
) -> np.ndarray:
    """由單期報酬累積出淨值序列（首點為 `start`）。"""
    arr = _as_array(returns)
    return np.concatenate(([start], start * np.cumprod(1.0 + arr)))


def historical_var_cvar(
    returns: Sequence[float] | np.ndarray,
    confidence: float = DEFAULT_VAR_CONFIDENCE,
    min_samples: int = MIN_VAR_SAMPLES,
) -> VarCvarResult | None:
    """歷史模擬法單期 VaR 與 CVaR（Expected Shortfall），皆為正值損失比例。

    VaR = −q_{1−c}(r)；CVaR = −mean(r | r ≤ q_{1−c})。樣本不足 `min_samples` 回 None，
    呼叫端必須把 None 當成「無法評估」而不是「零風險」。VaR 以 0 為下限：左尾分位數
    仍為正報酬時代表樣本期間沒有虧損，不應回報負的「損失」。
    """
    arr = _as_array(returns)
    if arr.size < max(1, min_samples):
        return None
    q = float(np.quantile(arr, 1.0 - confidence))
    tail = arr[arr <= q]
    cvar = -float(np.mean(tail)) if tail.size > 0 else -q
    return VarCvarResult(max(0.0, -q), max(0.0, cvar))
