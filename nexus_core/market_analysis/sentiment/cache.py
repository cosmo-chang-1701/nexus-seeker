from services.market_data_service import BoundedCache

_iv_cache = BoundedCache(max_size=500)
# 30 分鐘：上游 edge 期權快照約 30 分鐘才換一次（見 services/market_data_service/
# caches.py 的 _EDGE_SNAPSHOT_MAX_AGE_SECONDS），更頻繁重算只會讀到同一份快照、
# 白白消耗運算。這是真正的資料新鮮度 TTL（IV/報價本身會隨時間變化），與
# services/market_data_service.py 的 _OPTION_CHAIN_CACHE_TTL 語意不同——後者
# 已改為 60 秒的純請求去重快取，新鮮度改由 edge 快照的 age_seconds 把關，
# 兩者不再對齊，也不需要對齊。
_IV_CACHE_TTL = 1800
