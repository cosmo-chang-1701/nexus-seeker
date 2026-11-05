"""services/edge_auth.py

core → edge 請求的驗證標頭集中處（葉節點模組，僅依賴 stdlib／httpx／config）。

- `X-Edge-Token`：值取自 `config.EDGE_API_TOKEN`，須與 edge 端相同。
- `CF-Access-Client-Id` / `CF-Access-Client-Secret`：Cloudflare Access 服務權杖，
  兩者皆有值才會帶出。

三個設定皆為空時不帶任何額外標頭，行為與導入前完全一致。
任何日誌皆不得包含權杖值。
"""

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

import config

logger = logging.getLogger(__name__)

EDGE_TOKEN_HEADER = "X-Edge-Token"
CF_ACCESS_ID_HEADER = "CF-Access-Client-Id"
CF_ACCESS_SECRET_HEADER = "CF-Access-Client-Secret"

_REJECTION_LOG_INTERVAL_SECONDS = 600.0

# 各狀態碼上次記錄 ERROR 的 monotonic 時間（節流用）
_last_rejection_log: dict[int, float] = {}
_partial_cf_warned: bool = False


def _cfg(name: str) -> str:
    value = getattr(config, name, "")
    return value.strip() if isinstance(value, str) else ""


def edge_auth_headers() -> dict[str, str]:
    """組出對 edge 請求應帶的驗證標頭（呼叫時才讀取設定）。"""
    global _partial_cf_warned
    headers: dict[str, str] = {}

    token = _cfg("EDGE_API_TOKEN")
    if token:
        headers[EDGE_TOKEN_HEADER] = token

    cf_id = _cfg("CF_ACCESS_CLIENT_ID")
    cf_secret = _cfg("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers[CF_ACCESS_ID_HEADER] = cf_id
        headers[CF_ACCESS_SECRET_HEADER] = cf_secret
    elif cf_id or cf_secret:
        if not _partial_cf_warned:
            _partial_cf_warned = True
            logger.warning(
                "CF_ACCESS_CLIENT_ID 與 CF_ACCESS_CLIENT_SECRET 只設定了其中一個，"
                "兩者皆需設定才會帶 Cloudflare Access 標頭，目前一律不帶。"
            )
    return headers


async def log_edge_auth_rejection(response: httpx.Response) -> None:
    """httpx response event hook：edge 回 401/403 時以 ERROR 記錄（每狀態碼 600 秒一次）。"""
    try:
        status = response.status_code
        if status not in (401, 403):
            return
        now = time.monotonic()
        last = _last_rejection_log.get(status)
        if last is not None and now - last < _REJECTION_LOG_INTERVAL_SECONDS:
            return
        _last_rejection_log[status] = now
        path = urlsplit(str(response.request.url)).path or "/"
        logger.error(
            "edge 拒絕請求 (HTTP %s, 路徑 %s)：core 的 EDGE_API_TOKEN／"
            "CF_ACCESS_CLIENT_* 與 edge／Cloudflare Access 不符，此請求將退回備援路徑。",
            status,
            path,
        )
    except Exception:
        return


def edge_client_kwargs() -> dict[str, Any]:
    """供 `httpx.AsyncClient(**edge_client_kwargs())` 使用。"""
    return {
        "headers": edge_auth_headers(),
        "event_hooks": {"response": [log_edge_auth_rejection]},
    }
