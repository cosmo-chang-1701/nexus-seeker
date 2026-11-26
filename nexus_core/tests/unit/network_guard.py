"""測試用網路守衛：攔截、記錄並阻擋對外連線嘗試。

分兩層使用：`tests/conftest.py` 對全部測試安裝「寬鬆」守衛（只阻擋、不失敗）；
個別測試檔可再以 autouse fixture 安裝「嚴格」守衛並在 teardown 呼叫 `assert_clean()`。
呼叫端常以 `except Exception` 吞掉連線錯誤，只 raise 不足以讓測試失敗，
因此守衛同時記錄每次嘗試供嚴格模式斷言。

攔截範圍：TCP／UDP socket、DNS 查詢，以及 curl_cffi（yfinance 走這條路）。
curl_cffi 在 C 層建立 socket，Python 層的 socket 攔不到，需另外包裝其 Session。
"""

import importlib
import ipaddress
import socket
import traceback
from typing import Any

import pytest

_BLOCK_MESSAGE = "測試不得連網"
_STACK_LIMIT = 12


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    ip = _parse_ip(host)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def _needs_dns(host: Any) -> bool:
    """主機名需要實際查詢 DNS 時回傳 True；None、空字串、IP 字面值與 localhost 放行。"""
    if host is None:
        return False
    text = host.decode(errors="replace") if isinstance(host, bytes) else str(host)
    if text in ("", "localhost"):
        return False
    return _parse_ip(text) is None


def _is_outbound(sock: socket.socket, address: Any) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return False
    return not _is_loopback(str(address[0]))


class NetworkGuard:
    """記錄並阻擋對外連線與 DNS 查詢；loopback 與 AF_UNIX 不受影響。"""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_socket(monkeypatch)
        self._install_dns(monkeypatch)
        self._install_curl_cffi(monkeypatch)

    def reset(self) -> None:
        self.attempts.clear()

    def assert_clean(self) -> None:
        assert not self.attempts, "\n\n".join(self.attempts)

    def _record(self, target: str) -> None:
        # 去掉最後三層（本方法、`_block`／`_block_dns` 與攔截函式），保留呼叫端堆疊。
        stack = "".join(traceback.format_stack(limit=_STACK_LIMIT + 3)[:-3])
        self.attempts.append(f"對外連線嘗試：{target}\n{stack}")

    def _block(self, target: str) -> OSError:
        self._record(target)
        return OSError(_BLOCK_MESSAGE)

    def _block_dns(self, target: str) -> socket.gaierror:
        # 維持 DNS 失敗的例外型別，呼叫端的降級路徑才會與真實離線一致。
        self._record(target)
        return socket.gaierror(socket.EAI_NONAME, _BLOCK_MESSAGE)

    def _install_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_sendto = socket.socket.sendto

        def guarded_connect(sock: socket.socket, address: Any) -> None:
            if _is_outbound(sock, address):
                raise self._block(f"socket.connect {address}")
            original_connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
            if _is_outbound(sock, address):
                raise self._block(f"socket.connect_ex {address}")
            return original_connect_ex(sock, address)

        def guarded_sendto(sock: socket.socket, data: Any, *args: Any) -> int:
            # 兩種簽章：sendto(data, address) 與 sendto(data, flags, address)。
            if args and _is_outbound(sock, args[-1]):
                raise self._block(f"socket.sendto {args[-1]}")
            return original_sendto(sock, data, *args)

        setattr(guarded_connect, "_nexus_network_guard", True)
        setattr(guarded_connect_ex, "_nexus_network_guard", True)
        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)

    def _install_dns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def wrap(name: str) -> None:
            original: Any = getattr(socket, name)

            def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
                if _needs_dns(host):
                    raise self._block_dns(f"socket.{name} {host!r}")
                return original(host, *args, **kwargs)

            monkeypatch.setattr(socket, name, guarded)

        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            wrap(name)

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

        def guarded_ws_connect(
            session: Any, url: str, *args: Any, **kwargs: Any
        ) -> Any:
            # `AsyncSession.ws_connect` 本身是同步函式（回傳 context manager），
            # 兩個類別共用同步攔截即可在呼叫當下擋下。
            raise self._block(f"curl_cffi ws_connect {url}")

        monkeypatch.setattr(curl_requests.Session, "request", guarded_request)
        monkeypatch.setattr(
            curl_requests.AsyncSession, "request", guarded_async_request
        )
        for session_class in (curl_requests.Session, curl_requests.AsyncSession):
            if hasattr(session_class, "ws_connect"):
                monkeypatch.setattr(session_class, "ws_connect", guarded_ws_connect)
