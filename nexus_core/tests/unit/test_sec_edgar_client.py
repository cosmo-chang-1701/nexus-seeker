"""單元測試：SEC EDGAR 非同步客戶端 (sec_edgar_client.py)。"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta

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


_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "sec"


def _mock_tickers_response() -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "0": {"cik_str": 99999, "ticker": "MOCK", "title": "Mock Inc."},
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@pytest.mark.asyncio
async def test_get_cik_unknown_symbol_uses_fresh_map_without_refetch() -> None:
    """映射表新鮮期間查無代碼直接回傳 None，不重複下載 company_tickers.json。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_tickers_response()
        assert await client.get_cik("NOPE1") is None
        assert await client.get_cik("NOPE2") is None
        assert await client.get_cik("MOCK") == "0000099999"
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_get_cik_refetches_after_ttl_expiry() -> None:
    """映射表過期（TTL）後才重新下載。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_tickers_response()
        assert await client.get_cik("NOPE") is None
        # 模擬載入時間早於 TTL
        assert client._ticker_map_loaded_at is not None
        client._ticker_map_loaded_at -= 24 * 3600.0 + 1.0
        assert await client.get_cik("NOPE") is None
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_get_cik_failure_backoff() -> None:
    """下載失敗後冷卻期內不重試。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = RuntimeError("HTTP 503")
        assert await client.get_cik("MOCK") is None
        assert await client.get_cik("MOCK") is None
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_get_cik_concurrent_misses_share_single_download() -> None:
    """併發 miss 經 SingleFlightManager 合併為單一下載。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    gate = asyncio.Event()

    async def _slow_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        await gate.wait()
        return _mock_tickers_response()

    with patch("httpx.AsyncClient.get", side_effect=_slow_get) as mock_get:
        tasks = [asyncio.create_task(client.get_cik("MOCK")) for _ in range(5)]
        await asyncio.sleep(0.01)
        gate.set()
        results = await asyncio.gather(*tasks)
        assert results == ["0000099999"] * 5
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_acceptance_datetime_reads_sgml_header() -> None:
    """以真實 .hdr.sgml 樣本讀取權威美東受理時間，並驗證請求 URL。"""
    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    header_text = (_FIXTURE_DIR / "0001140361-26-038674.hdr.sgml").read_text()
    with patch.object(
        client, "fetch_document_text", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = header_text
        dt = await client.fetch_acceptance_datetime(
            "0000320193", "0001140361-26-038674"
        )
    assert dt.replace(tzinfo=None) == datetime(2026, 10, 5, 18, 42, 45)
    assert dt.utcoffset() == timedelta(hours=-4)
    url = mock_fetch.call_args.args[0]
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000114036126038674/"
        "0001140361-26-038674.hdr.sgml"
    )


@pytest.mark.asyncio
async def test_fetch_filing_documents_reads_index_headers() -> None:
    """申報附件清單讀自 `{accession}-index-headers.html`（實際 AAPL 8-K 2.02 樣本）。"""
    from pathlib import Path

    client = SecEdgarClient(user_agent="NexusSeeker test@sample.com")
    sample = (
        Path(__file__).parent
        / "fixtures"
        / "sec"
        / "0000320193-26-000005-index-headers.html"
    ).read_text(encoding="utf-8")
    filing_dir = SecEdgarClient.filing_directory_url(
        "0000320193", "0000320193-26-000005"
    )
    assert (
        filing_dir
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000005"
    )
    with patch.object(
        client, "fetch_document_text", new=AsyncMock(return_value=sample)
    ) as fetch:
        docs = await client.fetch_filing_documents(
            filing_dir + "/", "0000320193-26-000005"
        )
    fetch.assert_awaited_once()
    assert fetch.await_args_list[-1].args[0] == (
        f"{filing_dir}/0000320193-26-000005-index-headers.html"
    )
    assert ("EX-99.1", "a8-kex991q1202612272025.htm") in [
        (d.doc_type, d.filename) for d in docs
    ]
