"""market_data_service 記憶體快取設定 (BoundedCache 實例 / TTL 常數 / clear_*)。"""

from typing import Any
import functools
import gc
import logging
import math
from datetime import date, datetime, timedelta

from market_time import ny_tz, nyse_calendar
from services.bounded_cache import BoundedCache  # noqa: F401 (re-exported via __init__)

logger = logging.getLogger(__name__)


# 限制快取大小以節省記憶體 (1GB RAM VPS 優化)
# 依實際儲存內容分層設定上限：純量/小型 dict 值 (SMA/EMA/quote/profile/etf/
# option_expiries，約數百 bytes) 與整份 DataFrame (_history_cache 約 12KB/entry、
# _option_chain_cache 雙邊 DataFrame 流動性大的標的可達 80-150KB/entry) 的單筆
# 記憶體成本差距達兩個數量級，共用同一個上限等於放任最重的兩個 cache 佔用不成
# 比例的記憶體，因此拆成三層常數分別套用。
_SCALAR_CACHE_SIZE = 500  # sma/ema/quote/profile/etf/option_expiries
_HISTORY_CACHE_SIZE = 250  # _history_cache（DataFrame，~12KB/entry）
_OPTION_CHAIN_CACHE_SIZE = 150  # _option_chain_cache（雙邊 DataFrame，~20-150KB/entry）
_OPTION_CHAIN_FINGERPRINT_SIZE = 150  # 純觀測用，跟 _option_chain_cache 的 key 空間對齊

# BoundedCache 的實作已集中到 services/bounded_cache.py（過去這裡與
# polymarket_service.py 各自維護一份完全相同的 class，容量上限已分歧且無文件
# 說明理由，見上方 import）。保留模組層級名稱 BoundedCache，維持既有
# `from services.market_data_service import BoundedCache` 呼叫端不需變動。

# ---------------------------------------------------------------------------
# SMA 記憶體快取設定
# ---------------------------------------------------------------------------
_sma_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_SMA_CACHE_TTL = 3600  # 1 小時 (1GB VPS 優化)


# ---------------------------------------------------------------------------
# 即時報價與基本面資料快取設定
# ---------------------------------------------------------------------------
_quote_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_QUOTE_CACHE_TTL = 15  # 15 秒，避免在同一次掃描中心跳訊號重複對相同標的進行即時報價呼叫

# 5 分鐘：判斷 Finnhub `/quote` 回傳的報價時間戳 (`t`) 是否「陳舊」的門檻。部分
# Finnhub 免費方案對美股沒有真正的即時報價權限，`/quote` 只會回傳上一交易日收盤價
# （`c > 0` 但 `t` 停在上個交易日甚至上週五），而既有邏輯只檢查 `c > 0` 就直接採用，
# 導致盤中仍長期顯示過期價格且不會被偵測到。此門檻僅在盤中生效（見
# `get_quote._is_finnhub_quote_stale`），盤外/週末時 `t` 停留在最後成交時間是正常
# 現象，不應誤判為過期。5 分鐘遠寬於 Finnhub 正常即時/輕微延遲報價的秒級誤差，
# 只用來攔截「完全沒有更新」這種結構性問題，而非追求分鐘級的新鮮度。
_FINNHUB_QUOTE_STALE_THRESHOLD_SECONDS = 300

_profile_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_PROFILE_CACHE_TTL = 86400  # 24 小時，公司 Profile 通常是靜態的

_etf_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_ETF_CACHE_TTL = 86400  # 24 小時，ETF 屬性通常是靜態的

# ---------------------------------------------------------------------------
# 標的代號有效性快取 (24 小時有效快取 / 10 分鐘無效快取，大幅加速 /set_watch 與輸入驗證)
# ---------------------------------------------------------------------------
_VALID_SYMBOL_CACHE_SIZE = 1000
_VALID_SYMBOL_CACHE_TTL = 86400
_INVALID_SYMBOL_CACHE_TTL = 600

_valid_symbol_cache: Any = BoundedCache(max_size=_VALID_SYMBOL_CACHE_SIZE)

# ---------------------------------------------------------------------------
# 歷史 K 線數據快取設定 (6 小時，避開盤中大量重複 API 查詢)
# ---------------------------------------------------------------------------
_history_cache: Any = BoundedCache(max_size=_HISTORY_CACHE_SIZE)
_HISTORY_CACHE_TTL = (
    21600  # 6 小時（僅作盤中「指標用日線」的上限；其餘依 history_cache_expiry）
)

# --- 依 interval × 交易時段決定到期時間（見 history_cache_expiry）---
# intraday K 棒秒數：到期對齊「下一根 bar 收盤」而非固定 6 小時。
_INTRADAY_BAR_SECONDS: dict[str, int] = {
    "1m": 60,
    "2m": 120,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
    "90m": 5400,
}
# 被當「現值」使用的短期日線（VIX 期限結構、原油、跳空、2d 現價 fallback），
# 盤中最後一根日線 bar 會隨成交更新，不能再讀 6 小時前的資料。
_LIVE_DAILY_PERIODS: frozenset[str] = frozenset({"1d", "2d", "5d"})
_LIVE_DAILY_TTL_SECONDS = 900  # 對齊 15 分鐘盤中巡邏
_BAR_SETTLE_GRACE_SECONDS = 60  # Yahoo 於 bar 收盤後需要時間定案，多等 60 秒再重抓
_POST_CLOSE_SETTLE_SECONDS = 1800  # 收盤後 30 分內日線／最後一根 bar 仍可能被修正
_POST_CLOSE_SHORT_TTL_SECONDS = 300
# 除權息調整於開盤前生效；08:45 盤前預熱需拿到新資料，故次交易日開盤前 60 分（08:30 ET）刷新
_PRE_OPEN_REFRESH_OFFSET_MINUTES = 60


@functools.lru_cache(maxsize=32)
def _session_window(day: date) -> tuple[datetime, datetime] | None:
    """NYSE 該日 (open, close)（ET aware）；非交易日回 None。半日市由行事曆給提前收盤時間。"""
    schedule = nyse_calendar.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (
        row["market_open"].tz_convert(ny_tz).to_pydatetime(),
        row["market_close"].tz_convert(ny_tz).to_pydatetime(),
    )


def _next_trading_day_on_or_after(day: date) -> date:
    """自 day 起（含）往後找第一個交易日，最多掃 14 天（涵蓋最長連假）；找不到回 day+14。"""
    for offset in range(15):
        candidate = day + timedelta(days=offset)
        if _session_window(candidate) is not None:
            return candidate
    return day + timedelta(days=14)


def history_cache_expiry(interval: str, period: str, now_ts: float) -> float:
    """歷史 K 線快取到期時間（epoch 秒）。純函式：時間由 now_ts 注入，不讀系統時鐘。

    1. 盤中：intraday 對齊下一根 bar 收盤（不超過收盤）＋60 秒定案寬限；短期日線
       （period 1d/2d/5d）15 分鐘；其餘指標用日線維持 6 小時。
    2. 收盤後 30 分內：5 分鐘（全 interval）。
    3. 其餘盤外：到次一個「開盤前 60 分（08:30 ET）」刷新點；開盤前 60 分～開盤之間
       到開盤後 60 秒；一律至少 now+60 秒。
    """
    now_et = datetime.fromtimestamp(now_ts, ny_tz)
    min_expiry = now_ts + 60
    today_window = _session_window(now_et.date())
    pre_open_offset = timedelta(minutes=_PRE_OPEN_REFRESH_OFFSET_MINUTES)

    if today_window is not None:
        open_dt, close_dt = today_window
        if open_dt <= now_et <= close_dt:
            bar = _INTRADAY_BAR_SECONDS.get(interval)
            if bar is not None:
                elapsed = (now_et - open_dt).total_seconds()
                k = math.floor(elapsed / bar) + 1
                boundary = min(open_dt + timedelta(seconds=k * bar), close_dt)
                return boundary.timestamp() + _BAR_SETTLE_GRACE_SECONDS
            if interval in ("1d", "5d", "1wk", "1mo", "3mo"):
                if period in _LIVE_DAILY_PERIODS:
                    return now_ts + _LIVE_DAILY_TTL_SECONDS
                return now_ts + _HISTORY_CACHE_TTL
            # 未知 interval：保守處理
            return now_ts + _LIVE_DAILY_TTL_SECONDS
        if (
            close_dt
            < now_et
            <= close_dt + timedelta(seconds=_POST_CLOSE_SETTLE_SECONDS)
        ):
            return now_ts + _POST_CLOSE_SHORT_TTL_SECONDS
        if now_et < open_dt - pre_open_offset:
            return max((open_dt - pre_open_offset).timestamp(), min_expiry)
        if now_et < open_dt:
            return max(open_dt.timestamp() + _BAR_SETTLE_GRACE_SECONDS, min_expiry)

    # 非交易日，或今日收盤 30 分後：下一交易日開盤前刷新點
    next_day = _next_trading_day_on_or_after(now_et.date() + timedelta(days=1))
    next_window = _session_window(next_day)
    if next_window is None:  # 理論上不會發生（掃描上限 14 天）；保守 6 小時
        return now_ts + _HISTORY_CACHE_TTL
    return max((next_window[0] - pre_open_offset).timestamp(), min_expiry)


# ---------------------------------------------------------------------------
# 期權到期日與期權鏈快取設定 (避開盤中重複的 yfinance 查詢)
# ---------------------------------------------------------------------------
_option_expiries_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_OPTION_EXPIRIES_CACHE_TTL = 43200  # 12 小時

_option_chain_cache: Any = BoundedCache(max_size=_OPTION_CHAIN_CACHE_SIZE)
# 60 秒：這層快取的用途改為「同一輪評估內的請求去重」，不再是「資料新鮮度保證」。
# 新鮮度改由 _fetch_option_chain_raw() 讀取 edge 背景輪詢寫入的 SQLite 快照時
# 附帶的 age_seconds 直接把關（該快照本身已由 nexus_edge_scraper/scheduler.py
# 每輪抓最新資料維持在 ~25-30 分鐘內，且該次讀取是毫秒級本地讀取，不是需要用長
# TTL 保護的昂貴資源）。舊值 900 秒（15 分鐘）會讓 get_option_chain() 在 edge
# 快照早就刷新之後，仍多疊加最多 15 分鐘才回頭去看新資料，等於在 edge 端的延遲
# 之外又白白多疊加一層。60 秒只是用來合併同一次心跳評估中，多個下游模組
# （max_pain.py、iv_metrics.py、uoa_detector.py 等）對同一 symbol+expiry 短時間
# 內重複呼叫 get_option_chain() 的情況，避免重複打 edge 快照讀取請求。
_OPTION_CHAIN_CACHE_TTL = 60  # 60 秒（僅請求去重，非新鮮度保證）

# 30 分鐘：_fetch_option_chain_raw() 用來判斷 edge 快照是否還「夠新鮮」可直接採用
# 的門檻，對齊 nexus_edge_scraper/scheduler.py 背景輪詢設計目標的整份清單輪替
# 週期（~25-30 分鐘，持倉標的因不受批次輪替影響則遠低於此）。舊值 3600 秒（1 小時）
# 比背景輪詢自己的正常週期寬鬆兩倍以上，等於這道 fallback 保護網在背景輪詢正常
# 運作時幾乎不會被觸發；調緊到 1800 秒後，只要某標的的批次輪替真的落後正常週期
# （例如背景輪詢卡住、Playwright 持續失敗），nexus_core 就會主動觸發下方的即時
# scrape 補上，而不是照單全收一份可能將近 1 小時舊的資料。
_EDGE_SNAPSHOT_MAX_AGE_SECONDS = 1800  # 30 分鐘

# --- 期權鏈「資料是否真的刷新過」觀測用指紋快取 ---
# yfinance/Yahoo 不提供 chain 層級的資料快照時間戳（各 contract 的 lastTradeDate 只反映
# 該合約最後成交時間，冷門合約可能是好幾天前，不能拿來判斷整條鏈是否已刷新）。因此無法在
# 抓取「之前」保證這次拿到的資料已經過了一次刷新週期，只能靠 _fetch_option_chain_raw()
# 讀取的 edge 快照 age_seconds 做時間上的把關。這裡額外做「抓取之後」的觀測：比對本次與
# 上次成功抓取的 OI/IV 指紋，若完全相同就記錄 warning，代表 Yahoo 端資料這一輪可能尚未
# 真正刷新（純觀測用，不重試、不阻擋主流程，只用來驗證背景輪詢節奏是否與實際資料延遲相符）。
_option_chain_fingerprint_cache: Any = BoundedCache(
    max_size=_OPTION_CHAIN_FINGERPRINT_SIZE
)

# ---------------------------------------------------------------------------
# EMA 記憶體快取設定
# ---------------------------------------------------------------------------
_ema_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_EMA_CACHE_TTL = 3600  # 1 小時 (1GB VPS 優化)


def clear_quote_cache() -> None:
    _quote_cache.clear()
    logger.info("Clarified quote cache")


def clear_profile_cache() -> None:
    _profile_cache.clear()
    logger.info("Clarified profile cache")


def clear_etf_cache() -> None:
    _etf_cache.clear()
    logger.info("Clarified ETF cache")


def clear_history_cache() -> None:
    _history_cache.clear()
    logger.info("Clarified history cache")


def clear_options_cache() -> None:
    _option_expiries_cache.clear()
    _option_chain_cache.clear()
    logger.info("Clarified options cache")


def clear_sma_cache() -> None:
    _sma_cache.clear()
    logger.info("Clarified SMA cache")


def clear_ema_cache() -> None:
    _ema_cache.clear()
    logger.info("Clarified EMA cache")


def clear_valid_symbol_cache() -> None:
    _valid_symbol_cache.clear()
    logger.info("Clarified valid symbol cache")


def run_garbage_collection() -> None:
    """手動觸發垃圾回收 (用於大規模掃描後)。"""
    gc.collect()
    logger.info("🧹 [系統優化] 已手動執行垃圾回收機制。")
