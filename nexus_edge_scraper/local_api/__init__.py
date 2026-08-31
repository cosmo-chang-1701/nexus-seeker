"""Facade for local_api — Nexus Edge Scraper 的 FastAPI 應用程式。

公開 import path 維持 `local_api`（`from local_api import app` 與
`uvicorn local_api:app` 皆繼續運作）。路由依領域拆分為子模組：

- `reddit`         ：Reddit RSS 抓取（版塊清單 / 個股關鍵字搜尋）
- `macro`           ：總經 GEX/流動性/FedWatch Playwright 抓取、個股 GEX 端點
                       （`async_playwright`、`scrape_symbol_gex_core` 皆於本模組
                       頂層 import，需要攔截 Playwright 的測試應
                       patch `local_api.macro.async_playwright` /
                       `local_api.macro.scrape_symbol_gex_core`，而非套件層級
                       的 `local_api.async_playwright`）
- `macro_calendar`   ：TradingView 總經行事曆抓取與中文化翻譯引擎
- `fundamental`       ：SEC EDGAR 財報 Metadata/清單/文本抓取
- `cache_and_sync`     ：watchlist 同步、背景排程快取讀取端點、系統健康檢查
- `yf_api`（既有獨立模組）：yfinance 歷史/期權資料代理
"""

import logging
import warnings
from contextlib import asynccontextmanager
from typing import AsyncIterator

from bs4 import XMLParsedAsHTMLWarning
from fastapi import FastAPI

import scheduler

from . import cache_and_sync, fundamental, macro, macro_calendar, reddit

# Suppress BS4 XML warning for SEC filings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(lifespan=lifespan)

app.include_router(reddit.router)
app.include_router(macro.router)
app.include_router(macro_calendar.router)
app.include_router(fundamental.router)
app.include_router(cache_and_sync.router)

try:
    from yf_api import router as yf_router

    app.include_router(yf_router)
except ImportError as e:
    logger.warning(f"Failed to import yf_api: {e}")

# 重新匯出既有呼叫端/測試直接引用的名稱，維持 `local_api.<name>` 存取面不變。
from .reddit import (  # noqa: F401,E402
    _fetch_reddit_rss,
    _reddit_cache,
    _REDDIT_CACHE_TTL,
    REDDIT_USER_AGENT,
    scrape_reddit,
    scrape_reddit_feed,
)
from .macro import (  # noqa: F401,E402
    async_playwright,
    scrape_core_macro_metrics,
    scrape_fedwatch,
    scrape_gex,
    scrape_symbol_gex,
    scrape_symbol_gex_core,
)
from .macro_calendar import scrape_macro_calendar  # noqa: F401,E402
from .fundamental import (  # noqa: F401,E402
    SEC_USER_AGENT,
    _FORM_ANCHOR_PATTERNS,
    _get_sec_cik,
    cik_cache,
    scrape_fundamental_list,
    scrape_fundamental_metadata,
    scrape_fundamental_text,
)
from .cache_and_sync import (  # noqa: F401,E402
    WatchlistSyncRequest,
    _row_age_seconds,
    get_cached_gex,
    get_cached_option_chain,
    sync_watchlist_symbols,
    sys_health,
)
