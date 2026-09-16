"""特徵計算 (純函式，無 I/O)。

前視防護是本模組的核心責任：
* 日線特徵一律 `shift(1)` 後才對應到交易日 D，代表「D 開盤前已知」的值。
* 日內 Session VWAP 以當日累積計算，只含當根與之前的 K 棒。
* 量比的分母是「前 20 根」均量 (shift(1))，不含當根。
"""

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_ta as ta

NY_TZ = ZoneInfo("America/New_York")
HV_RANK_WINDOW = 252  # 掃描器以 1 年日線的 HV20 最小/最大值計算位階


def et_dates(index: pd.Index) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize(NY_TZ)
    return np.asarray(idx.tz_convert(NY_TZ).date)


def daily_features(df: pd.DataFrame) -> pd.DataFrame:
    """日線特徵 (未 shift，對應「當日收盤時」已知的值)。"""
    out = pd.DataFrame(index=df.index)
    close = df["Close"].astype("float64")
    out["open"] = df["Open"].astype("float64")
    out["close"] = close
    out["rsi"] = ta.rsi(close, length=14)
    out["sma20"] = ta.sma(close, length=20)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    out["macd_hist"] = (
        macd["MACDh_12_26_9"]
        if macd is not None and "MACDh_12_26_9" in macd
        else np.nan
    )
    log_ret = np.log(close / close.shift(1))
    hv20 = log_ret.rolling(20).std() * np.sqrt(252)
    hv_min = hv20.rolling(HV_RANK_WINDOW, min_periods=60).min()
    hv_max = hv20.rolling(HV_RANK_WINDOW, min_periods=60).max()
    span = (hv_max - hv_min).replace(0.0, np.nan)
    out["hv_rank"] = ((hv20 - hv_min) / span * 100.0).fillna(0.0)
    out["atr14"] = ta.atr(
        df["High"].astype("float64"), df["Low"].astype("float64"), close, length=14
    )
    out["low10"] = df["Low"].astype("float64").rolling(10).min()
    out["high10"] = df["High"].astype("float64").rolling(10).max()
    out["low60"] = df["Low"].astype("float64").rolling(60).min()
    out["high60"] = df["High"].astype("float64").rolling(60).max()
    out["date"] = et_dates(df.index)
    return out


def prior_session_lookup(daily_feat: pd.DataFrame) -> pd.DataFrame:
    """以 ET 日期為鍵、值為「前一交易日收盤時」已知的日線特徵 (shift(1))。"""
    cols = ["sma20", "atr14", "low10", "high10", "low60", "high60"]
    shifted = daily_feat[cols].shift(1)
    shifted["date"] = daily_feat["date"].values
    return shifted.set_index("date")


def hourly_features(df_1h: pd.DataFrame, daily_feat: pd.DataFrame) -> pd.DataFrame:
    close = df_1h["Close"].astype("float64")
    high = df_1h["High"].astype("float64")
    low = df_1h["Low"].astype("float64")
    volume = df_1h["Volume"].astype("float64")
    out = pd.DataFrame(index=df_1h.index)
    out["open"] = df_1h["Open"].astype("float64")
    out["close"] = close
    out["rsi"] = ta.rsi(close, length=14)
    out["atr_1h"] = ta.atr(high, low, close, length=14)
    out["vol_ratio"] = volume / volume.rolling(20).mean().shift(1)
    out["date"] = et_dates(df_1h.index)
    typical = (high + low + close) / 3.0
    pv = (typical * volume).groupby(out["date"]).cumsum()
    vv = volume.groupby(out["date"]).cumsum().replace(0.0, np.nan)
    out["session_vwap"] = pv / vv
    prior = prior_session_lookup(daily_feat)
    joined = prior.reindex(out["date"].values)
    for col in prior.columns:
        out[f"{col}_prev"] = joined[col].values
    # 次一根開盤 (進場價)；最後一根無次一根。
    out["next_open"] = out["open"].shift(-1)
    return out
