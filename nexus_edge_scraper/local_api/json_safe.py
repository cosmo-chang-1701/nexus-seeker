"""local_api：NaN／Inf 安全的 JSON 回應。

Starlette 的 JSONResponse 以 allow_nan=False 序列化，yfinance DataFrame 經
`to_dict(orient="records")` 或背景快取 `json.loads` 讀回的 NaN／Inf 會讓整個
回應變成 500。此回應類別先走原生序列化（正常回應零額外成本、輸出逐位元組
相同），僅在遇到非有限浮點數時遞迴改寫為 null 後再序列化一次。
"""

import math
from typing import Any

from fastapi.responses import JSONResponse


def sanitize_non_finite(obj: Any) -> Any:
    """遞迴把 dict／list／tuple 內的 NaN、Inf、-Inf 換成 None，其餘值原樣保留。
    tuple 轉為 list（JSON 序列化結果相同）。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: sanitize_non_finite(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_non_finite(value) for value in obj]
    return obj


class NaNSafeJSONResponse(JSONResponse):
    """以 FastAPI app 的 default_response_class 套用於所有 edge 路由。"""

    def render(self, content: Any) -> bytes:
        body: bytes
        try:
            body = super().render(content)
        except ValueError:
            body = super().render(sanitize_non_finite(content))
        return body
