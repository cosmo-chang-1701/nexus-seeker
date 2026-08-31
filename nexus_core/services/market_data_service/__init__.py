"""Facade for market_data_service — 集中式 Finnhub/yfinance API client wrapper
(Async Optimized)。

所有對 Finnhub REST API / yfinance 的呼叫統一經過此套件，確保：
1. API Key 集中管理
2. Rate limiting（免費方案 60 calls/min, 使用 aiolimiter 控制）
3. 錯誤處理與 fallback
4. 回傳格式與既有程式碼相容（pandas DataFrame）

公開 import path 維持 `services.market_data_service`（不論是
`from services.market_data_service import get_quote` 或
`from services import market_data_service; market_data_service.get_quote(...)`
兩種風格皆繼續運作）。實作依領域拆分為子模組：

- `_core`  ：互動請求標記、ticker 清洗、Finnhub/yfinance rate limiting、
             Edge HTTP 連線池、`_execute_api_call`（單一權威來源，尤其是
             `global _rate_limit_until` 冷卻旗標 —— 需要直接讀寫該旗標的
             測試必須 patch `services.market_data_service._core._rate_limit_until`）
- `caches` ：所有 BoundedCache 實例、TTL 常數、`clear_*_cache()`
- `quote`  ：即時報價、標的驗證
- `history`：歷史 K 線、SMA/EMA
- `options`：期權到期日與期權鏈
- `fundamentals`：Financials/Profile/ETF/行事曆/新聞/總經指標
"""

import yfinance as yf  # noqa: F401 (re-exported: 供 `patch("...market_data_service.yf.Ticker")`)

from ._core import (  # noqa: F401,E402
    _client,
    _edge_clients_by_loop,
    _EdgeClientContext,
    _execute_api_call,
    _finnhub_controls_by_loop,
    _get_client,
    _get_finnhub_controls,
    _get_yfinance_controls,
    _is_interactive_request,
    _rate_limit_until,
    _sanitize_ticker,
    _to_yfinance_symbol,
    _yfinance_controls_by_loop,
    call_yf,
    get_edge_client,
    interactive,
    is_finnhub_rate_limited,
    mark_interactive_request,
)
from .caches import (  # noqa: F401,E402
    BoundedCache,
    _EDGE_SNAPSHOT_MAX_AGE_SECONDS,
    _EMA_CACHE_TTL,
    _ETF_CACHE_TTL,
    _FINNHUB_QUOTE_STALE_THRESHOLD_SECONDS,
    _HISTORY_CACHE_SIZE,
    _HISTORY_CACHE_TTL,
    _OPTION_CHAIN_CACHE_SIZE,
    _OPTION_CHAIN_CACHE_TTL,
    _OPTION_CHAIN_FINGERPRINT_SIZE,
    _OPTION_EXPIRIES_CACHE_TTL,
    _PROFILE_CACHE_TTL,
    _QUOTE_CACHE_TTL,
    _SCALAR_CACHE_SIZE,
    _SMA_CACHE_TTL,
    _ema_cache,
    _etf_cache,
    _history_cache,
    _option_chain_cache,
    _option_chain_fingerprint_cache,
    _option_expiries_cache,
    _profile_cache,
    _quote_cache,
    _sma_cache,
    clear_etf_cache,
    clear_ema_cache,
    clear_history_cache,
    clear_options_cache,
    clear_profile_cache,
    clear_quote_cache,
    clear_sma_cache,
    run_garbage_collection,
)
from .quote import (  # noqa: F401,E402
    _direct_yf_history,
    _fetch_history_via_edge,
    _is_finnhub_quote_stale,
    _safe_yf_history,
    batch_get_quotes,
    get_quote,
    get_yfinance_quote,
    validate_symbol,
)
from .history import (  # noqa: F401,E402
    get_ema,
    get_history_df,
    get_sma,
    get_spy_history_df,
    get_stock_splits,
)
from .options import (  # noqa: F401,E402
    OptionChainData,
    _compute_option_chain_fingerprint,
    _fetch_option_chain_raw,
    _retry_once,
    get_all_option_expiries,
    get_option_chain,
)
from .fundamentals import (  # noqa: F401,E402
    check_and_reconcile_max_pain_anomaly,
    get_basic_financials,
    get_company_news,
    get_company_profile,
    get_dividend_yield,
    get_earnings_calendar,
    get_macro_environment,
    get_vix_term_structure,
    get_vix_zscores,
    is_etf,
)

# 明確保留 `_core` / `caches` / `quote` / `history` / `options` / `fundamentals`
# 子模組本身可經由 `services.market_data_service.<submodule>.<name>` 存取
# （例如需要直接操作 `global _rate_limit_until` 的測試）。上方的 `from .X import
# Y` 已經會讓 Python 自動把子模組註冊為本套件的屬性，這裡不需要額外處理。
