"""單元測試：SEC EDGAR 非同步客戶端 (sec_edgar_client.py)。"""

from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from services.sec_edgar_client import (
    SecConfigError,
    SecEdgarClient,
)


def test_sec_edgar_client_missing_user_agent() -> None:
    """驗證當缺少 User-Agent 或不包含 @ 聯絡信箱時拋出 SecConfigError。"""
    with pytest.raises(SecConfigError):
        SecEdgarClient(user_agent="")

    with pytest.raises(SecConfigError):
        SecEdgarClient(user_agent="InvalidUserAgentWithoutEmail")


def test_sec_edgar_client_valid_user_agent() -> None:
    """驗證合法 User-Agent 可正常初始化。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    assert client._headers["User-Agent"] == "NexusSeeker test@sample.com"
    assert client._limiter.max_rate == 8.0


@pytest.mark.asyncio
async def test_get_cik_cache_hit() -> None:
    """測試常見種子代碼可直接從快取中命中 CIK。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    cik_tsla = await client.get_cik("TSLA")
    assert cik_tsla == "0001318605"
    assert len(cik_tsla) == 10

    cik_aapl = await client.get_cik("aapl")
    assert cik_aapl == "0000320193"


@pytest.mark.asyncio
async def test_get_cik_network_fallback() -> None:
    """測試快取未命中時向 SEC company_tickers.json 發送網路請求並解析快取。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    mock_data = {
        "0": {"cik_str": 99999, "ticker": "MOCK", "title": "Mock Inc."},
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        cik = await client.get_cik("MOCK")
        assert cik == "0000099999"
        # 第二次呼叫應命中快取，不重複發送網路請求
        cik_again = await client.get_cik("MOCK")
        assert cik_again == "0000099999"
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_document_text_truncation() -> None:
    """測試串流拉取時遇到 byte_cap 硬截斷防護。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")

    # 模擬 10 個 chunk，每個 100 bytes，總長 1000 bytes
    chunk = b"X" * 100

    async def mock_aiter_bytes() -> Any:
        for _ in range(10):
            yield chunk

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_bytes = mock_aiter_bytes

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_resp
    mock_cm.__aexit__.return_value = None

    with patch("httpx.AsyncClient.stream", return_value=mock_cm):
        # 設定上限 250 bytes，預期只取 3 個 chunk (300 bytes 達上限截斷)
        content = await client.fetch_document_text("https://sample.url", byte_cap=250)
        assert len(content) == 300


@pytest.mark.asyncio
async def test_fetch_xml_root_defused() -> None:
    """測試 fetch_xml_root 正確使用 defusedxml 解析根節點。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    sample_xml = "<testDoc><node>NexusValue</node></testDoc>"

    with patch.object(
        client, "fetch_document_text", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = sample_xml
        root = await client.fetch_xml_root("https://sample.url/doc.xml")
        assert root.tag == "testDoc"
        assert root.find("node").text == "NexusValue"


@pytest.mark.asyncio
async def test_get_cik_dual_class_normalization() -> None:
    """測試雙重股權代碼（如 BRK.B 與 BRK-B）雙向標準化查詢。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    cik_dot = await client.get_cik("BRK.B")
    cik_dash = await client.get_cik("BRK-B")
    assert cik_dot == "0001067983"
    assert cik_dash == "0001067983"
    assert cik_dot == cik_dash
