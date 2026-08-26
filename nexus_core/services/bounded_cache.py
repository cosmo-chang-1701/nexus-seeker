"""services/bounded_cache.py

共用的 BoundedCache 實作。過去 market_data_service.py 與 polymarket_service.py
各自定義了完全相同的 OrderedDict LRU class，兩份實作已經在預設容量上出現分歧
（500 vs 2000）且沒有任何文件說明理由，未來修 bug 也容易漏改其中一份。集中到
這個共用模組後，兩處皆改為 import 同一份實作，只保留各自呼叫端自行決定的
容量大小（本來就是建構子參數，本質上不算重複）。
"""

from collections import OrderedDict
from typing import Any


class BoundedCache(OrderedDict):
    """具備容量上限的快取 (LRU 邏輯)。"""

    def __init__(self, max_size: Any = 500):
        super().__init__()
        self.max_size = max_size

    def __getitem__(self, key: Any):  # type: ignore
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key: Any, value: Any):  # type: ignore
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)
