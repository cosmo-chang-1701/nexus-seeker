"""Edge API 共享金鑰驗證。

環境變數 `EDGE_API_TOKEN` 有設定時，所有路由都必須帶 `X-Edge-Token` 標頭且內容相符；
未設定（或空白）時為 fail-open，方便 core 與 edge 分開部署時的滾動升級。
"""

import logging
import os
import secrets
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

EDGE_TOKEN_HEADER = "X-Edge-Token"
EDGE_TOKEN_ENV = "EDGE_API_TOKEN"
MIN_TOKEN_LENGTH = 32


class EdgeUnauthorized(Exception):
    """X-Edge-Token 缺少或不符。"""


def configured_token() -> str:
    """每次呼叫都重新讀取環境變數（測試可 monkeypatch.setenv）。"""
    return os.environ.get(EDGE_TOKEN_ENV, "").strip()


async def require_edge_token(
    x_edge_token: Optional[str] = Header(default=None, alias=EDGE_TOKEN_HEADER),
) -> None:
    expected = configured_token()
    if not expected:
        return
    received = x_edge_token or ""
    if not secrets.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
        raise EdgeUnauthorized()


async def edge_unauthorized_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "status": "unauthorized",
            "message": f"未授權：缺少或不正確的 {EDGE_TOKEN_HEADER} 標頭。",
        },
    )


def register_auth_handlers(app: FastAPI) -> None:
    app.add_exception_handler(EdgeUnauthorized, edge_unauthorized_handler)


def log_auth_mode() -> None:
    """啟動時記錄驗證模式（不輸出 token 值）。"""
    token = configured_token()
    if not token:
        logger.warning(
            "⚠️ 未設定 %s：Edge API 目前不做任何身分驗證，"
            "任何知道公開網址的人都能呼叫。",
            EDGE_TOKEN_ENV,
        )
        return
    if len(token) < MIN_TOKEN_LENGTH:
        logger.warning(
            "⚠️ %s 長度不足 %d 字元，強度偏弱（仍會強制驗證）。",
            EDGE_TOKEN_ENV,
            MIN_TOKEN_LENGTH,
        )
    logger.info("🔒 Edge API 已啟用 %s 標頭驗證。", EDGE_TOKEN_HEADER)
