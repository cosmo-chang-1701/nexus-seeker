"""Alpaca 歷史小時線（補足 Yahoo 1h 只有最近 730 天的缺口）。

Yahoo 的 1h K 線只能回溯約 730 天。本模組以 Alpaca 歷史 bars API（``feed=sip``，
全市場成交量）抓 30 分鐘線，只保留常規交易時段 09:30–16:00（美東），再兩兩合併成
與 Yahoo 1h 相同的格式：每天 7 根、以 09:30／10:30／…／15:30 為開始時間（半日市為
4 根，最後一根只含 12:30–13:00）。

資料慣例（以 2023-10 → 2024-06 與既有 Yahoo 快取的重疊期間實測決定，見
``compare_with_cache`` 與 ``python -m calibration alpaca-seam``）：

- **價格**用 ``adjustment=all``（分割 + 股息調整），與 Yahoo ``auto_adjust`` 一致。
- **成交量**自行做分割調整：原始（``raw``）成交量 × 分割倍數，倍數 = raw 收盤 ÷
  僅分割調整（``split``）的收盤。不直接採用 Alpaca 調整後的成交量，因為它並不可靠：
  TSLA 2022-08 分割（3:1）之前的量被多乘了 3 倍（價格則正確）。
- **不與 Yahoo 1h 接合**：Yahoo 的盤中成交量**沒有做分割調整**（NVDA 2024-06 10:1
  分割前的量是實際成交股數，只有調整後的 1/10），部分標的的股息調整也不一致（FCX
  價格固定偏差約 0.38%）。在同一個 20 根 K 棒的量比 (``vol_ratio``) 視窗內混用兩種
  慣例會失真，因此多資產回測的 1h 線整段改由 Alpaca 提供（``replace_range``）。
- **IEX 不可用**：IEX 只佔全市場成交量約 2.5%，會讓量比條件失真。

金鑰只從環境變數 ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET`` 讀取，不寫入任何檔案。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx
import numpy as np
import pandas as pd

from calibration.data_store import DataStore

logger = logging.getLogger(__name__)

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_NY_TZ = "America/New_York"
_RTH_OPEN_MIN = 9 * 60 + 30
_RTH_CLOSE_MIN = 16 * 60
_PAGE_LIMIT = 10000
_MAX_PAGES = 200


def _credentials() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret or "your_alpaca" in key:
        raise RuntimeError(
            "缺少 ALPACA_API_KEY / ALPACA_API_SECRET 環境變數（請以 docker compose run -e 傳入）"
        )
    return key, secret


async def _fetch_bars(
    client: httpx.AsyncClient,
    symbols: list[str],
    start: str,
    end: str,
    adjustment: str,
    headers: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    params: dict[str, Any] = {
        "symbols": ",".join(symbols),
        "timeframe": "30Min",
        "start": start,
        "end": end,
        "feed": "sip",
        "adjustment": adjustment,
        "limit": _PAGE_LIMIT,
        "sort": "asc",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for _ in range(_MAX_PAGES):
        for attempt in range(4):
            resp = await client.get(ALPACA_BARS_URL, params=params, headers=headers)
            if resp.status_code != 429:
                break
            await asyncio.sleep(2.0 * (attempt + 1))
        resp.raise_for_status()
        payload = resp.json()
        for sym, rows in (payload.get("bars") or {}).items():
            out.setdefault(str(sym), []).extend(rows or [])
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return out


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df.set_index("t").sort_index()


def aggregate_rth_hourly(bars_30m: pd.DataFrame) -> pd.DataFrame:
    """30 分鐘線（UTC index，欄位 o/h/l/c/v）→ Yahoo 格式的常規時段 1h 線。

    分桶以 09:30 為起點每 60 分鐘一桶；回傳 index 為各桶開始時間（UTC）。
    """
    if bars_30m.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    et = bars_30m.index.tz_convert(_NY_TZ)
    minutes = np.asarray(et.hour * 60 + et.minute)
    in_rth = (minutes >= _RTH_OPEN_MIN) & (minutes < _RTH_CLOSE_MIN)
    df = bars_30m[in_rth]
    et = et[in_rth]
    bucket_min = _RTH_OPEN_MIN + ((minutes[in_rth] - _RTH_OPEN_MIN) // 60) * 60
    starts = pd.DatetimeIndex(et.normalize()) + pd.to_timedelta(bucket_min, unit="m")
    keys = starts.tz_convert("UTC")
    grouped = df.groupby(keys)
    out = pd.DataFrame(
        {
            "Open": grouped["o"].first(),
            "High": grouped["h"].max(),
            "Low": grouped["l"].min(),
            "Close": grouped["c"].last(),
            "Volume": grouped["v"].sum(),
        }
    )
    out.index.name = "Date"
    return out


def build_hourly(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """價格取 ``all``；成交量 = ``raw`` 成交量 × (``raw`` 收盤 / ``split`` 收盤)。"""
    adj_all = frames["all"]
    if adj_all.empty:
        return aggregate_rth_hourly(adj_all)
    raw = frames["raw"].reindex(adj_all.index)
    split = frames["split"].reindex(adj_all.index)
    factor = (raw["c"] / split["c"]).replace([np.inf, -np.inf], np.nan)
    # 分割倍數是整數或簡單分數；四捨五入到 4 位去掉浮點雜訊
    factor = factor.round(4).fillna(1.0)
    volume = raw["v"].astype("float64") * factor
    merged = adj_all.copy()
    merged["v"] = volume.fillna(adj_all["v"].astype("float64"))
    return aggregate_rth_hourly(merged)


async def iter_hourly(
    symbols: list[str], start: str, end: str
) -> AsyncIterator[tuple[str, pd.DataFrame]]:
    """逐檔抓取 [start, end] 期間的常規時段 1h 線並逐檔產出（呼叫端可逐檔寫入，
    中途中斷不會遺失已完成的標的）。start/end 為 ISO 日期或 RFC3339。"""
    key, secret = _credentials()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start_ts = pd.Timestamp(start, tz="UTC").isoformat().replace("+00:00", "Z")
    end_ts = pd.Timestamp(end, tz="UTC").isoformat().replace("+00:00", "Z")
    async with httpx.AsyncClient(timeout=60.0) as client:
        for sym in symbols:
            frames: dict[str, pd.DataFrame] = {}
            for adj in ("all", "raw", "split"):
                rows = await _fetch_bars(client, [sym], start_ts, end_ts, adj, headers)
                frames[adj] = _frame(rows.get(sym, []))
            hourly = build_hourly(frames)
            logger.info(f"[alpaca] {sym}: {len(hourly)} 根 1h K 棒")
            yield sym, hourly


def _et_dates(index: pd.Index) -> pd.Index:
    return pd.Index(pd.DatetimeIndex(index).tz_convert(_NY_TZ).date)


def replace_range(
    store: DataStore, symbol: str, hourly: pd.DataFrame, start: str, end: str
) -> int:
    """以 Alpaca 資料**整段取代**快取中 [start, end]（美東日期）區間的 1h K 棒。

    區間外的既有 K 棒保留；區間內一律改為 Alpaca，避免同一段期間混用兩種調整慣例。
    回傳寫入後的總列數。
    """
    lo = pd.Timestamp(start).date()
    hi = pd.Timestamp(end).date()
    existing = store.load("1h", symbol)
    if existing is not None and not existing.empty:
        days = np.asarray(_et_dates(existing.index))
        keep = np.array([not (lo <= d <= hi) for d in days], dtype=bool)
        existing = existing[keep]
    return store.overwrite("1h", symbol, existing, hourly)


# 資料品質判定（alpaca-seam）：以**同一根 1h K 棒**的成交量比值為準。
# 不可拿「小時量加總」對「日線成交量」：兩個來源的小時線都只含常規時段，日線含盤前
# 盤後，Yahoo 自己的小時加總對自己的日線也只有 0.80–0.94，會讓任何小時資料都不及格。
SEAM_MAX_MEDIAN_DEVIATION = 0.10  # 逐根量比中位數偏離 1 的上限
SEAM_MAX_VOL_RATIO_FLIP = 0.05  # vol_ratio 門檻判定翻轉比例上限
SEAM_VOL_RATIO_GATES = (1.15, 1.25)  # 進場條件使用的量比門檻


def _vol_ratio(volume: pd.Series) -> pd.Series:
    """與 features.hourly_features 相同：當根量 ÷ 前 20 根均量。"""
    v = volume.astype("float64")
    return v / v.rolling(20).mean().shift(1)


def compare_with_cache(
    store: DataStore, symbol: str, hourly: pd.DataFrame
) -> Optional[dict[str, Any]]:
    """資料品質比對：同一批時點上，Alpaca 與既有快取（Yahoo）的收盤價差、逐根量比，
    以及各自計算的 vol_ratio 在進場門檻上的判定翻轉比例。"""
    existing = store.load("1h", symbol)
    if existing is None or existing.empty or hourly.empty:
        return None
    joined = pd.concat(
        [
            existing[["Close", "Volume"]].add_prefix("y_"),
            hourly[["Close", "Volume"]].add_prefix("a_"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    if joined.empty:
        return None
    px = (
        joined["a_Close"].astype("float64") / joined["y_Close"].astype("float64")
    ) - 1.0
    vol = joined["a_Volume"].astype("float64") / joined["y_Volume"].astype("float64")
    vol = vol.replace([np.inf, -np.inf], np.nan).dropna()
    ratios = pd.concat(
        [_vol_ratio(hourly["Volume"]), _vol_ratio(existing["Volume"])],
        axis=1,
        keys=["a", "y"],
        join="inner",
    ).dropna()
    flips = {
        f"vol_ratio_flip_{gate}": float(
            ((ratios["a"] >= gate) != (ratios["y"] >= gate)).mean()
        )
        if not ratios.empty
        else None
        for gate in SEAM_VOL_RATIO_GATES
    }
    median = float(vol.median()) if not vol.empty else None
    flip_values = [v for v in flips.values() if v is not None]
    passed = (
        median is not None
        and abs(median - 1.0) <= SEAM_MAX_MEDIAN_DEVIATION
        and all(v <= SEAM_MAX_VOL_RATIO_FLIP for v in flip_values)
    )
    return {
        "symbol": symbol,
        "bars": int(len(joined)),
        "first": str(joined.index[0]),
        "last": str(joined.index[-1]),
        "close_diff_median_pct": float(px.abs().median() * 100.0),
        "close_diff_p99_pct": float(px.abs().quantile(0.99) * 100.0),
        "volume_ratio_median": median,
        "volume_ratio_p10": float(vol.quantile(0.10)) if not vol.empty else None,
        "volume_ratio_p90": float(vol.quantile(0.90)) if not vol.empty else None,
        **flips,
        "passed": bool(passed),
    }
