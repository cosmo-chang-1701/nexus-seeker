"""驗證 `tests/conftest.py` 的全域連網守衛。

本檔刻意不安裝檔內嚴格守衛：每個攔截都必須只靠 conftest 層就成立。
目標一律使用保留網域 `.invalid` 與文件用位址 192.0.2.0/24。
"""

import importlib
import socket
from pathlib import Path
from typing import Any

import pytest

from tests.unit.network_guard import NetworkGuard, _is_loopback, _needs_dns

_BLOCKED = "測試不得連網"
_HOST = "example.invalid"
_IP = "192.0.2.1"


def test_conftest_guard_installed(_global_network_block: NetworkGuard) -> None:
    """不安裝任何檔內守衛，`socket.connect` 也必須已被全域守衛包裝。"""
    assert getattr(socket.socket.connect, "_nexus_network_guard", False) is True
    owner = getattr(socket.socket.connect, "_nexus_network_guard_owner", None)
    assert owner is _global_network_block


def test_conftest_guard_records_attempts(_global_network_block: NetworkGuard) -> None:
    """全域守衛雖不讓測試失敗，仍須記錄每次嘗試。"""
    before = len(_global_network_block.attempts)
    with pytest.raises(socket.gaierror, match=_BLOCKED):
        socket.getaddrinfo(_HOST, 443)
    assert len(_global_network_block.attempts) > before
    assert _HOST in _global_network_block.attempts[-1]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="此平台不支援 AF_UNIX")
def test_allows_af_unix(tmp_path: Path, _global_network_block: NetworkGuard) -> None:
    """AF_UNIX 是本機通訊，`connect`／`connect_ex` 照常運作且不記錄嘗試。"""
    path = str(tmp_path / "g.sock")
    before = len(_global_network_block.attempts)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(path)
            assert client.getpeername() == path
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            assert client.connect_ex(path) == 0

    assert len(_global_network_block.attempts) == before


def test_localhost_variants_normalized() -> None:
    """localhost 名稱比對不分大小寫並容許單一尾點；其他變體仍阻擋並記錄。"""
    for host in ("LOCALHOST", "localhost.", "LocalHost."):
        assert _is_loopback(host), host
        assert not _needs_dns(host), host
    assert not _needs_dns(b"LOCALHOST")
    for host in (
        "localhost.evil.com",
        "localhost..",
        "evil-localhost",
        "localhost.invalid",
    ):
        assert not _is_loopback(host), host
        assert _needs_dns(host), host

    resolved: list[Any] = []

    def stub(host: Any, *args: Any, **kwargs: Any) -> list[Any]:
        resolved.append(host)
        return []

    # 以區域 MonkeyPatch 疊在全域守衛之上，測試結束前即依序還原。
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(socket, "getaddrinfo", stub)
        guard = NetworkGuard()
        guard.install(monkeypatch)

        socket.getaddrinfo("LOCALHOST.", 80)
        assert resolved == ["LOCALHOST."]
        assert guard.attempts == []

        with pytest.raises(socket.gaierror, match=_BLOCKED):
            socket.getaddrinfo("localhost.evil.com", 80)
        assert resolved == ["LOCALHOST."]
        assert len(guard.attempts) == 1
        assert "localhost.evil.com" in guard.attempts[0]


def test_blocks_dns_getaddrinfo() -> None:
    with pytest.raises(socket.gaierror, match=_BLOCKED):
        socket.getaddrinfo(_HOST, 443)
    with pytest.raises(socket.gaierror, match=_BLOCKED):
        socket.getaddrinfo(host=_HOST.encode(), port=443)


def test_blocks_dns_gethostbyname() -> None:
    with pytest.raises(socket.gaierror, match=_BLOCKED):
        socket.gethostbyname(_HOST)
    with pytest.raises(socket.gaierror, match=_BLOCKED):
        socket.gethostbyname_ex(_HOST)


def test_blocks_udp_sendto() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match=_BLOCKED):
            sock.sendto(b"x", (_IP, 53))
        with pytest.raises(OSError, match=_BLOCKED):
            sock.sendto(b"x", 0, (_IP, 53))


def test_allows_loopback() -> None:
    """loopback 的 DNS、TCP 與 UDP（兩種 `sendto` 簽章）照常運作。"""
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo(None, 80)
    assert socket.gethostbyname("localhost").startswith("127.")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=0.5) as client:
            assert client.getpeername() == server.getsockname()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"a", receiver.getsockname())
            sender.sendto(b"b", 0, receiver.getsockname())
        assert {receiver.recv(1), receiver.recv(1)} == {b"a", b"b"}


async def test_blocks_curl_cffi_ws_connect() -> None:
    """同步與非同步 Session 的 `ws_connect` 都要在呼叫當下被擋下。"""
    curl_requests = pytest.importorskip("curl_cffi.requests")
    url = f"wss://{_HOST}/"

    with curl_requests.Session() as session:
        with pytest.raises(OSError, match=_BLOCKED):
            session.ws_connect(url)

    async with curl_requests.AsyncSession() as async_session:
        with pytest.raises(OSError, match=_BLOCKED):
            async_session.ws_connect(url)


async def test_blocks_websockets_connect() -> None:
    client = importlib.import_module("websockets.asyncio.client")

    for url in (f"wss://{_HOST}/", f"wss://{_IP}/"):
        with pytest.raises(OSError, match=_BLOCKED):
            await client.connect(url, open_timeout=0.5)


async def test_blocks_aiohttp() -> None:
    aiohttp = importlib.import_module("aiohttp")

    timeout = aiohttp.ClientTimeout(total=0.5)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in (f"https://{_HOST}/", f"http://{_IP}/"):
            with pytest.raises(aiohttp.ClientConnectorError) as excinfo:
                async with session.get(url):
                    pass
            # IP 字面值路徑的 aiohttp 例外訊息只取 strerror，須檢查底層 OSError。
            assert _BLOCKED in str(excinfo.value.os_error)
