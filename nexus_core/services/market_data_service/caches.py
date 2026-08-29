"""market_data_service 記憶體快取設定 (BoundedCache 實例 / TTL 常數 / clear_*)。"""

from typing import Any
import gc
import logging

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

_profile_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_PROFILE_CACHE_TTL = 86400  # 24 小時，公司 Profile 通常是靜態的

_etf_cache: Any = BoundedCache(max_size=_SCALAR_CACHE_SIZE)
_ETF_CACHE_TTL = 86400  # 24 小時，ETF 屬性通常是靜態的

# ---------------------------------------------------------------------------
# 歷史 K 線數據快取設定 (6 小時，避開盤中大量重複 API 查詢)
# ---------------------------------------------------------------------------
_history_cache: Any = BoundedCache(max_size=_HISTORY_CACHE_SIZE)
_HISTORY_CACHE_TTL = 21600  # 6 小時

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


def run_garbage_collection() -> None:
    """手動觸發垃圾回收 (用於大規模掃描後)。"""
    gc.collect()
    logger.info("🧹 [系統優化] 已手動執行垃圾回收機制。")
