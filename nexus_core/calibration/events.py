"""事件偵測器：輸出 DataFrame[symbol, event_type, side, signal_ts, entry_price,
date, atr_1d, rsi, vix_prev, …]。

進場一律為**次一根 K 棒開盤** (signal_ts 為產生訊號那根的時間戳)；VIX 取進場
前最後一個已收盤交易日。GEX 條件全部以純價格代理，報告會揭露代理的薄弱：
* Put Wall  → 前 10 個交易日最低點
* Call Wall → 前 10 個交易日最高點
* Gamma Flip → 前一日 SMA20
* 次級負 GEX 節點 → 前 60 個交易日最低點
* 1h RSI 代理分類器實際使用的 15m RSI；ATR₁₅ₘ 以 ATR₁ₕ / 2 近似
"""

import zlib
from typing import Optional

import numpy as np
import pandas as pd

from .features import et_dates

EVENT_COLUMNS = [
    "symbol",
    "event_type",
    "side",
    "signal_ts",
    "entry_price",
    "date",
    "atr_1d",
    "rsi",
    "vix_prev",
    "atr_15m_equiv",
    "next_node_space_atr",
    "room_atr",
    "bar_interval",
]

# Regime 代理的「寬鬆」RSI 邊界：偵測時保留較寬範圍，門檻掃描器再逐格過濾。
REGIME_V_RSI_LOOSE_MAX = 55.0
REGIME_III_RSI_LOOSE_MIN = 45.0
REGIME_V_RSI_DEFAULT = 45.0
REGIME_III_RSI_DEFAULT = 55.0
REGIME_I_RSI_MAX = 30.0
VOLUME_SURGE_MULT = 1.5


def scanner_signal(
    price: float, rsi: float, hv_rank: float, sma20: float, macd_hist: float
) -> Optional[str]:
    """精確複製 `market_analysis/strategy/indicators.py::_determine_strategy_signal`
    的分支 (ivr=0.0，與 analyze_symbol 的呼叫方式一致，IVR 賣方硬鎖不觸發)。
    `tests/unit/test_calibration_events.py` 會對原函式做全格點比對，防止漂移。"""
    if rsi < 35 and hv_rank >= 30:
        return "STO_PUT"
    if rsi > 65 and hv_rank >= 30:
        return "STO_CALL"
    if price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
        return "BTO_CALL" if hv_rank < 50 else "STO_PUT"
    if price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
        return "BTO_PUT" if hv_rank < 50 else "STO_CALL"
    return None


def _vix_prev(
    dates: np.ndarray, vix_daily: Optional[pd.Series], strictly_before: bool
) -> np.ndarray:
    """VIX 收盤查表：strictly_before=True 取嚴格早於該日的最後一筆。"""
    if vix_daily is None or vix_daily.empty:
        return np.full(len(dates), np.nan)
    vix = vix_daily.copy()
    vix.index = pd.Index(et_dates(vix.index))
    vix = vix[~vix.index.duplicated(keep="last")].sort_index()
    keys = np.array(list(vix.index))
    values = vix.to_numpy(dtype="float64")
    pos = np.searchsorted(keys, dates, side="left" if strictly_before else "right") - 1
    out = np.where(pos >= 0, values[np.clip(pos, 0, len(values) - 1)], np.nan)
    return out


def _frame(rows: dict[str, object], n: int) -> pd.DataFrame:
    if n == 0:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.DataFrame(rows)[EVENT_COLUMNS]


def detect_scanner_events(
    symbol: str, daily_feat: pd.DataFrame, vix_daily: Optional[pd.Series]
) -> pd.DataFrame:
    """日線掃描器 BTO_PUT / BTO_CALL 事件 (只取訊號連續區段的第一天)。"""
    f = daily_feat
    signals = [
        scanner_signal(p, r, h, s, m)
        if not any(pd.isna(v) for v in (p, r, h, s, m))
        else None
        for p, r, h, s, m in zip(
            f["close"], f["rsi"], f["hv_rank"], f["sma20"], f["macd_hist"]
        )
    ]
    sig = pd.Series(signals, index=f.index)
    run_start = sig != sig.shift(1)
    next_open = f["open"].shift(-1)
    mask = (
        sig.isin(["BTO_PUT", "BTO_CALL"])
        & run_start
        & next_open.notna()
        & f["atr14"].notna()
    )
    sel = f[mask]
    n = len(sel)
    dates = np.asarray(sel["date"])
    return _frame(
        {
            "symbol": [symbol] * n,
            "event_type": ["SCANNER_" + s for s in sig[mask]],
            "side": ["SHORT" if s == "BTO_PUT" else "LONG" for s in sig[mask]],
            "signal_ts": sel.index,
            "entry_price": next_open[mask].to_numpy(),
            "date": [str(d) for d in dates],
            "atr_1d": sel["atr14"].to_numpy(),
            "rsi": sel["rsi"].to_numpy(),
            # 訊號日收盤即進場前最後一個已收盤交易日
            "vix_prev": _vix_prev(dates, vix_daily, strictly_before=False),
            "atr_15m_equiv": np.full(n, np.nan),
            "next_node_space_atr": np.full(n, np.nan),
            "room_atr": np.full(n, np.nan),
            "bar_interval": ["1d"] * n,
        },
        n,
    )


def _hourly_event_frame(
    symbol: str,
    event_type: str,
    side: str,
    h: pd.DataFrame,
    mask: pd.Series,
    vix_daily: Optional[pd.Series],
) -> pd.DataFrame:
    sel = h[mask]
    n = len(sel)
    dates = np.asarray(sel["date"])
    atr = sel["atr14_prev"].to_numpy()
    close = sel["close"].to_numpy()
    return _frame(
        {
            "symbol": [symbol] * n,
            "event_type": [event_type] * n,
            "side": [side] * n,
            "signal_ts": sel.index,
            "entry_price": sel["next_open"].to_numpy(),
            "date": [str(d) for d in dates],
            "atr_1d": atr,
            "rsi": sel["rsi"].to_numpy(),
            "vix_prev": _vix_prev(dates, vix_daily, strictly_before=True),
            "atr_15m_equiv": sel["atr_1h"].to_numpy() / 2.0,
            "next_node_space_atr": (close - sel["low60_prev"].to_numpy()) / atr,
            "room_atr": (sel["high60_prev"].to_numpy() - close) / atr,
            "bar_interval": ["1h"] * n,
        },
        n,
    )


def detect_regime_proxy_events(
    symbol: str, hourly_feat: pd.DataFrame, vix_daily: Optional[pd.Series]
) -> pd.DataFrame:
    """Regime V / III / I 的純價格代理事件 (RSI 以寬鬆邊界保留，供門檻掃描)。"""
    h = hourly_feat
    valid = h["next_open"].notna() & h["atr14_prev"].notna() & (h["atr14_prev"] > 0)
    below_vwap = h["close"] < h["session_vwap"]
    above_vwap = h["close"] > h["session_vwap"]
    surge = h["vol_ratio"] >= VOLUME_SURGE_MULT
    bearish = h["close"] < h["open"]
    bullish = h["close"] > h["open"]

    regime_v = (
        valid
        & below_vwap
        & bearish
        & surge
        & (h["rsi"] < REGIME_V_RSI_LOOSE_MAX)
        & (h["close"] < h["low10_prev"])  # 跌破 Put Wall 代理
        & (h["close"] < h["sma20_prev"])  # 跌破 Gamma Flip 代理
        & ((h["close"] - h["low60_prev"]) >= 2.0 * h["atr14_prev"])  # 公式 C 代理
    )
    room_req = np.maximum(1.5 * h["atr14_prev"], 0.035 * h["close"])
    regime_iii = (
        valid
        & above_vwap
        & bullish
        & surge
        & (h["rsi"] > REGIME_III_RSI_LOOSE_MIN)
        & (h["close"] > h["high10_prev"])
        & (h["close"] > h["sma20_prev"])
        & ((h["high60_prev"] - h["close"]) >= room_req)
    )
    dist_low10 = (h["close"] - h["low10_prev"]) / h["close"]
    regime_i = (
        valid
        & (h["rsi"] <= REGIME_I_RSI_MAX)
        & (h["close"] <= h["session_vwap"] - 1.5 * (h["atr_1h"] / 2.0))
        & (dist_low10 >= -0.01)
        & (dist_low10 <= 0.015)
    )
    frames = [
        _hourly_event_frame(
            symbol, "REGIME_V_PROXY", "SHORT", h, regime_v.fillna(False), vix_daily
        ),
        _hourly_event_frame(
            symbol, "REGIME_III_PROXY", "LONG", h, regime_iii.fillna(False), vix_daily
        ),
        _hourly_event_frame(
            symbol, "REGIME_I_PROXY", "LONG", h, regime_i.fillna(False), vix_daily
        ),
    ]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def first_per_session(events: pd.DataFrame) -> pd.DataFrame:
    """同一標的、同一事件類型、同一交易日只保留第一筆 (去除日內連續觸發的自相關)。"""
    if events.empty:
        return events
    ordered = events.sort_values("signal_ts")
    return ordered.drop_duplicates(
        subset=["symbol", "event_type", "date"], keep="first"
    )


def apply_default_thresholds(events: pd.DataFrame) -> pd.DataFrame:
    """把寬鬆 RSI 邊界收斂回現行常數 (Regime V < 45、Regime III > 55) 並去重。"""
    if events.empty:
        return events
    keep = ~(
        (
            (events["event_type"] == "REGIME_V_PROXY")
            & ~(events["rsi"] < REGIME_V_RSI_DEFAULT)
        )
        | (
            (events["event_type"] == "REGIME_III_PROXY")
            & ~(events["rsi"] > REGIME_III_RSI_DEFAULT)
        )
    )
    return first_per_session(events[keep])


def random_control_events(
    symbol: str,
    hourly_feat: pd.DataFrame,
    matched: pd.DataFrame,
    vix_daily: Optional[pd.Series],
    seed: int,
) -> pd.DataFrame:
    """隨機對照組：每個方向抽與真實事件同數量的 K 棒 (排除最後 7 個交易日，確保可標註)。"""
    if matched.empty or hourly_feat.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    rng = np.random.default_rng(seed + zlib.crc32(symbol.encode("utf-8")))
    h = hourly_feat
    dates = sorted(set(h["date"]))
    cutoff = dates[-7] if len(dates) > 7 else None
    eligible = h["next_open"].notna() & h["atr14_prev"].notna() & (h["atr14_prev"] > 0)
    if cutoff is not None:
        eligible &= h["date"] < cutoff
    pool = np.flatnonzero(eligible.to_numpy())
    frames = []
    for side in ("LONG", "SHORT"):
        count = int((matched["side"] == side).sum())
        if count == 0 or len(pool) == 0:
            continue
        picks = rng.choice(pool, size=min(count, len(pool)), replace=False)
        mask = pd.Series(False, index=h.index)
        mask.iloc[np.sort(picks)] = True
        frames.append(
            _hourly_event_frame(symbol, "RANDOM_CONTROL", side, h, mask, vix_daily)
        )
    if not frames:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.concat(frames, ignore_index=True)
