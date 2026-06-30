from services.market_data_service import BoundedCache

_iv_cache = BoundedCache(max_size=500)
_IV_CACHE_TTL = 1200
