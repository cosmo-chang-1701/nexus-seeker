"""embed_builder — 向後相容轉接層 (Shim)。

⚠️  此檔案已不包含任何實作程式碼。
    所有實作已遷移至 cogs/embed_builders/ 子套件。

為維持所有現有 `from cogs.embed_builder import ...` 的呼叫端相容性，
此 shim 將所有公開符號從新套件重新匯出，呼叫端無需修改任何 import 語句。
"""

from cogs.embed_builders import *  # noqa: F401, F403
from cogs.embed_builders import __all__  # noqa: F401
