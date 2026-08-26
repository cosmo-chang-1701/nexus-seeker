from services.market_data_service import BoundedCache

_iv_cache = BoundedCache(max_size=500)
# 15 分鐘：對齊 yfinance 期權資料本身約 15 分鐘的延遲，以及 dynamic_market_scanner
# 15 分鐘心跳節奏（同 services/market_data_service.py 的 _OPTION_CHAIN_CACHE_TTL）。
_IV_CACHE_TTL = 900
