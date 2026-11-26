"""驗證 `tests/conftest.py` 的全域連網守衛。

本檔刻意不安裝檔內嚴格守衛：每個攔截都必須只靠 conftest 層就成立。
目標一律使用保留網域 `.invalid` 與文件用位址 192.0.2.0/24。
"""

import importlib
import socket

import pytest

_BLOCKED = "測試不得連網"
_HOST = "example.invalid"
_IP = "192.0.2.1"


def test_conftest_guard_installed() -> None:
    """不安裝任何檔內守衛，`socket.connect` 也必須已被包裝。"""
    assert getattr(socket.socket.connect, "_nexus_network_guard", False) is True


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

    for url in (f"wss://{_HOST}/", f"ws://{_IP}/"):
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
