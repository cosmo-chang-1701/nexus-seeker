"""測試用網路守衛：攔截、記錄並阻擋對外連線嘗試。

呼叫端常以 `except Exception` 吞掉連線錯誤，只 raise 不足以讓測試失敗，
因此守衛同時記錄每次嘗試，由 fixture 在 teardown 呼叫 `assert_clean()`。
curl_cffi（yfinance 走這條路）在 C 層建立 socket，Python 的 `socket.connect`
攔不到，需另外包裝其 Session。
"""

import importlib
import ipaddress
import socket
import traceback
from typing import Any

import pytest

_BLOCK_MESSAGE = "測試不得連網"
_STACK_LIMIT = 12


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def _is_outbound(sock: socket.socket, address: Any) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return False
    return not _is_loopback(str(address[0]))


class NetworkGuard:
    """記錄並阻擋對外連線；loopback 與 AF_UNIX 不受影響。"""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_socket(monkeypatch)
        self._install_curl_cffi(monkeypatch)

    def reset(self) -> None:
        self.attempts.clear()

    def assert_clean(self) -> None:
        assert not self.attempts, "\n\n".join(self.attempts)

    def _block(self, target: str) -> OSError:
        # 去掉最後兩層（本方法與攔截函式），保留呼叫端堆疊。
        stack = "".join(traceback.format_stack(limit=_STACK_LIMIT + 2)[:-2])
        self.attempts.append(f"對外連線嘗試：{target}\n{stack}")
        return OSError(_BLOCK_MESSAGE)

    def _install_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def guarded_connect(sock: socket.socket, address: Any) -> None:
            if _is_outbound(sock, address):
                raise self._block(f"socket.connect {address}")
            original_connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
            if _is_outbound(sock, address):
                raise self._block(f"socket.connect_ex {address}")
            return original_connect_ex(sock, address)

        setattr(guarded_connect, "_nexus_network_guard", True)
        setattr(guarded_connect_ex, "_nexus_network_guard", True)
        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    def _install_curl_cffi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 動態匯入：pre-commit 的 mypy 環境未安裝 curl_cffi，靜態匯入會解析失敗。
        try:
            curl_requests = importlib.import_module("curl_cffi.requests")
        except ImportError:
            return

        def guarded_request(
            session: Any, method: str, url: str, *args: Any, **kwargs: Any
        ) -> Any:
            raise self._block(f"curl_cffi {method} {url}")

        async def guarded_async_request(
            session: Any, method: str, url: str, *args: Any, **kwargs: Any
        ) -> Any:
            raise self._block(f"curl_cffi async {method} {url}")

        monkeypatch.setattr(curl_requests.Session, "request", guarded_request)
        monkeypatch.setattr(
            curl_requests.AsyncSession, "request", guarded_async_request
        )
