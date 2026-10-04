"""多時間框架 PowerSqueeze 矩陣（W／3D／D／65m／15m／5m）。

規格見 `docs/strategies/10_multi_timeframe_squeeze_entry.md` §2.1。

資料來源只有三次抓取：日線（W／3D／D 由此重採樣）、15m、5m（65m 由 5m 重採樣）。
所有判定**只用已收盤 K 棒**——盤中最後一根仍在跳動，擠壓與動能會隨 tick 抖動；
盤中那一根另算一份「未確認預覽」只供顯示，不參與任何規則。

`get_history_df` 回傳 tz-naive US/Eastern 索引（見 AGENTS.md §3），本模組全程沿用。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd

from market_analysis.psq_engine import PSQResult, analyze_psq

logger = logging.getLogger(__name__)

TIMEFRAMES: Tuple[str, ...] = ("W", "3D", "D", "65m", "15m", "5m")
HIGHER_TIMEFRAMES: Tuple[str, ...] = ("W", "3D", "D")
INTRADAY_TRIGGER_TIMEFRAMES: Tuple[str, ...] = ("15m", "5m")

# Green Dot 回看根數：解除後 N 根內仍視為有效觸發（只看解除那一根太嚴，盤中
# 30 分鐘才評估一次，很容易錯過）。
GREEN_DOT_LOOKBACK: Dict[str, int] = {
    "W": 2,
    "3D": 3,
    "D": 3,
    "65m": 3,
    "15m": 3,
    "5m": 3,
}

# 3D K 棒的固定錨點：以此日起算的 NYSE 交易日序號 // 3 分組，讓分組邊界每天
# 不變（若以資料視窗起點分組，每多一天整串 3D K 棒都會位移）。與 TradingView
# 的錨點不保證一致，需人工比對（見規格書 §5）。
_THREE_DAY_ANCHOR = date(2020, 1, 2)

_DAILY_PERIOD = "2y"  # W 需 ≥ 40 根已收盤週線（PSQ length × 2）
_INTRADAY_15M_PERIOD = "5d"  # 與 atr_utils.fetch_atr_15m 同 key，可共乘快取
_INTRADAY_5M_PERIOD = "1mo"  # 65m 需 ≥ 40 根 ≈ 7 個交易日的 5m


@dataclass(frozen=True)
class TimeframeState:
    """單一時間框架、最後一根已收盤 K 棒的擠壓狀態。"""

    timeframe: str
    squeeze_level: str  # High／Mid／Normal／Release
    is_squeezing: bool
    momentum_value: float
    momentum_color: str  # LightBlue／DarkBlue／Red／Golden／Neutral
    green_dot: bool
    green_dot_bars_ago: Optional[int]
    turbo: bool
    squeeze_range_low: Optional[float]
    sma_20: float
    last_close: float
    bar_ts: str
    # 盤中未收盤那一根的預覽（僅顯示用；無未收盤 K 棒時為 None）
    preview_squeeze_level: Optional[str] = None
    preview_momentum_color: Optional[str] = None


PsqMatrix = Dict[str, TimeframeState]


# ---------------------------------------------------------------------------
# 重採樣與已收盤截斷（純函式，可單元測試）
# ---------------------------------------------------------------------------
_OHLCV_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}


def _agg(df: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    cols = {c: f for c, f in _OHLCV_AGG.items() if c in df.columns}
    if "Volume" in df.columns:
        cols["Volume"] = "sum"
    out = df.groupby(key, sort=True).agg(cols)
    return out.dropna(subset=["Close"])


def split_daily_confirmed(
    df_daily: pd.DataFrame, today: date
) -> Tuple[pd.DataFrame, bool]:
    """回傳 (已收盤日線, 是否有今日未確認 K 棒)。

    今日那一根一律視為未確認：盤中仍在成型；收盤後 `get_history_df` 的日線
    快取（6 小時）也可能還停在盤中快照。代價是收盤後到隔日之間 D 判定落後一根，
    這是刻意的保守取捨。
    """
    if df_daily.empty:
        return df_daily, False
    is_today = [ts.date() == today for ts in df_daily.index]
    keep = [not t for t in is_today]
    return df_daily.loc[keep], any(is_today)


def resample_weekly(df_daily: pd.DataFrame, today: date) -> Tuple[pd.DataFrame, bool]:
    """日線 → 週線（週五為界）。回傳 (已收盤週線, 最後一週是否未完成)。

    `df_daily` 必須是含今日在內的完整日線；本週尚未結束（週五 >= 今日）的那一根
    視為未確認。
    """
    if df_daily.empty:
        return df_daily, False
    week_end = pd.Series(
        [
            (ts + pd.offsets.Week(weekday=4)).normalize()
            if ts.weekday() != 4
            else ts.normalize()
            for ts in df_daily.index
        ],
        index=df_daily.index,
    )
    weekly = _agg(df_daily, week_end)
    if weekly.empty:
        return weekly, False
    last_end = weekly.index[-1].date()
    live = last_end >= today
    return (weekly.iloc[:-1] if live else weekly), live


def three_day_group_keys(
    index: pd.Index, session_ordinal: Dict[date, int]
) -> pd.Series:
    """依固定錨點的交易日序號 // 3 分組；找不到序號的日期回傳 -1（會被丟棄）。"""
    return pd.Series(
        [
            session_ordinal[ts.date()] // 3 if ts.date() in session_ordinal else -1
            for ts in index
        ],
        index=index,
    )


def resample_three_day(
    df_daily: pd.DataFrame, session_ordinal: Dict[date, int], today: date
) -> Tuple[pd.DataFrame, bool]:
    """日線 → 3D 線。回傳 (已收盤 3D 線, 最後一組是否未完成)。

    最後一組若尚未湊滿 3 個交易日、或含今日，視為未確認。
    """
    if df_daily.empty:
        return df_daily, False
    keys = three_day_group_keys(df_daily.index, session_ordinal)
    valid = keys >= 0
    df = df_daily.loc[valid]
    keys = keys.loc[valid]
    if df.empty:
        return df, False
    grouped = _agg(df, keys)
    counts = keys.value_counts()
    last_key = int(keys.iloc[-1])
    last_dates = [ts.date() for ts in df.index[keys == last_key]]
    live = int(counts.get(last_key, 0)) < 3 or today in last_dates
    out = grouped.iloc[:-1] if live else grouped
    # 以每組最後一個交易日當作索引，方便顯示
    last_ts = df.groupby(keys).apply(lambda g: g.index[-1])
    out.index = pd.DatetimeIndex([last_ts[k] for k in out.index])
    return out, live


def resample_65m(df_5m: pd.DataFrame) -> pd.DataFrame:
    """5m → 65m：每個交易日自 09:30 起每 65 分鐘一根（一天恰好 6 根）。

    只取常規交易時段 09:30–16:00；索引為每根的起始時間。
    """
    if df_5m.empty:
        return df_5m
    idx = df_5m.index
    minutes = pd.Series(
        [(ts.hour * 60 + ts.minute) - (9 * 60 + 30) for ts in idx], index=idx
    )
    rth = (minutes >= 0) & (minutes < 390)
    df = df_5m.loc[rth]
    minutes = minutes.loc[rth]
    if df.empty:
        return df
    starts = pd.Series(
        [
            ts.normalize()
            + timedelta(hours=9, minutes=30)
            + timedelta(minutes=65 * (m // 65))
            for ts, m in zip(df.index, minutes)
        ],
        index=df.index,
    )
    out = _agg(df, starts)
    out.index = pd.DatetimeIndex(out.index)
    return out


def split_intraday_confirmed(
    df: pd.DataFrame, bar_minutes: int, now_ny: datetime
) -> Tuple[pd.DataFrame, bool]:
    """回傳 (已收盤日內 K 棒, 最後一根是否未收盤)；索引為 K 棒起始時間。"""
    if df.empty:
        return df, False
    closed = df.index + pd.Timedelta(minutes=bar_minutes) <= pd.Timestamp(now_ny)
    return df.loc[closed], bool((~closed).any())


# ---------------------------------------------------------------------------
# PSQ 矩陣
# ---------------------------------------------------------------------------
def _to_state(
    tf: str, res: PSQResult, df: pd.DataFrame, preview: Optional[PSQResult]
) -> TimeframeState:
    return TimeframeState(
        timeframe=tf,
        squeeze_level=res.squeeze_level,
        is_squeezing=res.is_squeezing,
        momentum_value=res.momentum_value,
        momentum_color=res.momentum_color,
        green_dot=res.green_dot,
        green_dot_bars_ago=res.green_dot_bars_ago,
        turbo=res.turbo,
        squeeze_range_low=res.squeeze_range_low,
        sma_20=res.sma_20,
        last_close=float(df["Close"].iloc[-1]),
        bar_ts=str(df.index[-1]),
        preview_squeeze_level=preview.squeeze_level if preview else None,
        preview_momentum_color=preview.momentum_color if preview else None,
    )


def compute_state(
    tf: str, confirmed: pd.DataFrame, full: Optional[pd.DataFrame] = None
) -> Optional[TimeframeState]:
    """對已收盤 K 棒計算 PSQ；`full`（含未收盤那一根）只用來產生預覽。"""
    if confirmed is None or confirmed.empty:
        return None
    res = analyze_psq(confirmed, green_dot_lookback=GREEN_DOT_LOOKBACK.get(tf, 1))
    if res is None:
        return None
    preview = None
    if full is not None and len(full) > len(confirmed):
        preview = analyze_psq(full)
    return _to_state(tf, res, confirmed, preview)


def build_matrix(
    df_daily: Optional[pd.DataFrame],
    df_15m: Optional[pd.DataFrame],
    df_5m: Optional[pd.DataFrame],
    now_ny: datetime,
    session_ordinal: Dict[date, int],
) -> PsqMatrix:
    """由三份原始 K 線組裝六欄 PSQ 矩陣；資料不足的時間框架直接缺席。"""
    matrix: PsqMatrix = {}
    today = now_ny.date()

    if df_daily is not None and not df_daily.empty:
        d_conf, _ = split_daily_confirmed(df_daily, today)
        st = compute_state("D", d_conf, df_daily)
        if st:
            matrix["D"] = st

        w_conf, w_live = resample_weekly(df_daily, today)
        if w_live:
            w_full, _ = resample_weekly(df_daily, today + timedelta(days=7))
        else:
            w_full = None
        st = compute_state("W", w_conf, w_full)
        if st:
            matrix["W"] = st

        t_conf, t_live = resample_three_day(df_daily, session_ordinal, today)
        st = compute_state("3D", t_conf)
        if st:
            matrix["3D"] = st

    if df_5m is not None and not df_5m.empty:
        m65 = resample_65m(df_5m)
        m65_conf, _ = split_intraday_confirmed(m65, 65, now_ny)
        st = compute_state("65m", m65_conf, m65)
        if st:
            matrix["65m"] = st
        m5_conf, _ = split_intraday_confirmed(df_5m, 5, now_ny)
        st = compute_state("5m", m5_conf, df_5m)
        if st:
            matrix["5m"] = st

    if df_15m is not None and not df_15m.empty:
        m15_conf, _ = split_intraday_confirmed(df_15m, 15, now_ny)
        st = compute_state("15m", m15_conf, df_15m)
        if st:
            matrix["15m"] = st

    return matrix


def _session_ordinals(start: date, end: date) -> Dict[date, int]:
    """固定錨點起算的 NYSE 交易日序號。"""
    from market_time import nyse_calendar

    days = nyse_calendar.valid_days(start_date=_THREE_DAY_ANCHOR, end_date=end)
    out: Dict[date, int] = {}
    for i, ts in enumerate(days):
        d = ts.date()
        if d >= start:
            out[d] = i
    return out


async def fetch_psq_matrix(
    symbol: str, now_ny: Optional[datetime] = None
) -> Tuple[PsqMatrix, Optional[pd.DataFrame]]:
    """抓取並組裝 PSQ 矩陣；回傳 (矩陣, 原始日線) 供壓力區偵測重用同一份日線。

    記憶體超過 85% 時回傳空矩陣（呼叫端視為資料不足、fail-closed）。
    """
    import asyncio

    import market_time
    from services.llm_service import is_memory_safe
    from services.market_data_service import get_history_df

    if not is_memory_safe():
        logger.warning(f"[{symbol}] 記憶體過載，略過多時間框架擠壓計算")
        return {}, None

    if now_ny is None:
        now_ny = datetime.now(market_time.ny_tz).replace(tzinfo=None)

    df_daily, df_15m, df_5m = await asyncio.gather(
        get_history_df(symbol, period=_DAILY_PERIOD, interval="1d"),
        get_history_df(
            symbol, period=_INTRADAY_15M_PERIOD, interval="15m", force_refresh=True
        ),
        get_history_df(
            symbol, period=_INTRADAY_5M_PERIOD, interval="5m", force_refresh=True
        ),
    )

    ordinals: Dict[date, int] = {}
    if df_daily is not None and not df_daily.empty:
        try:
            ordinals = await asyncio.to_thread(
                _session_ordinals, df_daily.index[0].date(), now_ny.date()
            )
        except Exception as e:
            logger.warning(f"[{symbol}] NYSE 交易日序號計算失敗，略過 3D: {e}")

    matrix = await asyncio.to_thread(
        build_matrix, df_daily, df_15m, df_5m, now_ny, ordinals
    )
    return matrix, df_daily
