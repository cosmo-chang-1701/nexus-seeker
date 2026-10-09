"""HTTP 請求經限流閘門的共用包裝（httpx）。

流程：取得 `rate_gate` 具名閘門的 slot → 計入 `api_budget` → 送出請求 → 依回應更新冷卻：

- HTTP 429：解析 Retry-After，`trip()` 啟動全域冷卻並計入 429 統計。
- SEC 另將「403 且回應內文含 `Request Rate Threshold`」視為限流；其餘 403
  （例如 User-Agent 未申報）不是限流，不啟動冷卻。
- 2xx：`mark_ok()` 重置指數退避。

回應原樣回傳，呼叫端既有的 `status_code`／`raise_for_status` 邏輯不需要改。冷卻中、排隊逾時
或佇列滿載時拋 `RateGateError` 子類（`httpx` 例外則原樣外拋，slot 一律於離開時釋放）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from services import api_budget
from services.rate_gate import (
    _is_interactive_request,
    clock,
    get_gate,
    parse_retry_after,
)

# 非 429 但實質為限流的 403：閘門名稱 → 回應內文須包含的標記
_RATE_LIMIT_403_MARKERS: dict[str, str] = {"sec": "Request Rate Threshold"}


def _budget_source(gate_name: str) -> Any:
    """api_budget 的 Source 與閘門名稱一一對應（型別上以 Any 通過 Literal 檢查）。"""
    return gate_name


async def _is_rate_limited(gate_name: str, resp: httpx.Response) -> bool:
    status = resp.status_code
    if status == 429:
        return True
    marker = _RATE_LIMIT_403_MARKERS.get(gate_name)
    if status == 403 and marker is not None:
        try:
            await resp.aread()  # 串流回應需先讀完（限流頁面很小）
            return marker in resp.text
        except Exception:  # noqa: BLE001
            return False
    return False


async def _settle(
    gate_name: str, endpoint: str, resp: httpx.Response, started_at: float
) -> None:
    """依回應更新冷卻與統計；絕不外拋（不影響呼叫端對回應的處理）。"""
    gate = get_gate(gate_name)
    try:
        if await _is_rate_limited(gate_name, resp):
            api_budget.record_rate_limited(_budget_source(gate_name), endpoint)
            gate.trip(parse_retry_after(getattr(resp, "headers", None)))
            return
        status = resp.status_code
        if isinstance(status, int) and 200 <= status < 300:
            gate.mark_ok(started_at)
    except Exception:  # noqa: BLE001
        return


async def gated_request(
    gate_name: str,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    endpoint: str,
    **kwargs: Any,
) -> httpx.Response:
    """經 `gate_name` 閘門送出一次 HTTP 請求，回傳原始 `httpx.Response`。"""
    gate = get_gate(gate_name)
    async with gate.slot():
        api_budget.record_call(
            _budget_source(gate_name),
            endpoint,
            interactive=_is_interactive_request.get(),
        )
        started_at = clock()
        resp: httpx.Response = await getattr(client, method.lower())(url, **kwargs)
        await _settle(gate_name, endpoint, resp, started_at)
        return resp


@asynccontextmanager
async def gated_stream(
    gate_name: str,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    endpoint: str,
    **kwargs: Any,
) -> AsyncIterator[httpx.Response]:
    """`gated_request` 的串流版本：slot 於整個 `async with` 區塊（含讀取本文）期間持有。"""
    gate = get_gate(gate_name)
    async with gate.slot():
        api_budget.record_call(
            _budget_source(gate_name),
            endpoint,
            interactive=_is_interactive_request.get(),
        )
        started_at = clock()
        async with client.stream(method, url, **kwargs) as response:
            await _settle(gate_name, endpoint, response, started_at)
            yield response
