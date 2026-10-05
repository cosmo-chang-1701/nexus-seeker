"""單元測試：SEC 申報事件同步與回填服務 (filing_event_service.py)。"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.fundamental_pipeline.models import FilingCursorRecord
from services.filing_event_service import FilingEventService


@pytest.mark.asyncio
async def test_filing_event_service_sync_symbol_filings() -> None:
    """測試同步單一標的的 Form 4 與 8-K 申報並更新游標。"""
    mock_client = MagicMock()
    mock_client.get_cik = AsyncMock(return_value="0001318605")

    # 模擬 submissions API 回傳之資料結構
    mock_submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001318605-26-000001", "0001318605-26-000002"],
                "form": ["4", "8-K"],
                "filingDate": ["2026-10-02", "2026-10-01"],
                "acceptanceDateTime": ["2026-10-02T16:30:00Z", "2026-10-01T17:00:00Z"],
                "primaryDocument": ["edgar.xml", "doc8k.htm"],
                "items": ["", "4.02"],
            }
        }
    }
    mock_client.fetch_company_submissions = AsyncMock(return_value=mock_submissions)
    mock_client.fetch_document_text = AsyncMock(
        return_value="<ownershipDocument></ownershipDocument>"
    )

    service = FilingEventService(client=mock_client)

    # 模擬現存游標
    cursor = FilingCursorRecord(
        symbol="TSLA",
        cik="0001318605",
        last_accepted_at="2026-09-30T00:00:00Z",
        last_accession="OLD-ACC",
    )

    with (
        patch(
            "services.filing_event_service.get_sec_filing_cursor", return_value=cursor
        ),
        patch(
            "services.filing_event_service.save_sec_filing_events",
            new_callable=AsyncMock,
        ) as mock_save_events,
        patch(
            "services.filing_event_service.save_insider_transactions",
            new_callable=AsyncMock,
        ) as mock_save_txs,
        patch(
            "services.filing_event_service.save_governance_flags",
            new_callable=AsyncMock,
        ) as mock_save_flags,
        patch(
            "services.filing_event_service.upsert_sec_filing_cursor",
            new_callable=AsyncMock,
        ) as mock_upsert_cursor,
    ):
        stats = await service.sync_symbol_filings("TSLA")

        assert stats["events"] == 2
        mock_save_events.assert_awaited_once()
        mock_save_txs.assert_not_awaited()
        mock_save_flags.assert_awaited_once()
        mock_upsert_cursor.assert_awaited_once()
        # 驗證游標更新至最新受理時間
        args, _ = mock_upsert_cursor.call_args
        assert args[0].last_accepted_at == "2026-10-02T16:30:00Z"


@pytest.mark.asyncio
async def test_filing_event_service_first_run_retains_recent_8k_flags() -> None:
    """測試首次執行 (cursor is None) 時，30 天內的 8-K 申報不會因非首筆而被遺漏。"""
    mock_client = MagicMock()
    mock_client.get_cik = AsyncMock(return_value="0001375365")

    # 模擬 SMCI: 第一筆為近期 Form 4，第二筆為 2 天前之 8-K 4.02 重編
    mock_submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001375365-26-000001", "0001375365-26-000002"],
                "form": ["4", "8-K"],
                "filingDate": ["2026-10-04", "2026-10-02"],
                "acceptanceDateTime": ["2026-10-04T16:30:00Z", "2026-10-02T17:00:00Z"],
                "primaryDocument": ["xslF345X03/doc4.xml", "doc8k.htm"],
                "items": ["", "4.02"],
            }
        }
    }
    mock_client.fetch_company_submissions = AsyncMock(return_value=mock_submissions)
    mock_client.fetch_document_text = AsyncMock(
        return_value="<ownershipDocument></ownershipDocument>"
    )

    service = FilingEventService(client=mock_client)

    with (
        patch("services.filing_event_service.get_sec_filing_cursor", return_value=None),
        patch(
            "services.filing_event_service.save_sec_filing_events",
            new_callable=AsyncMock,
        ) as mock_save_events,
        patch(
            "services.filing_event_service.save_insider_transactions",
            new_callable=AsyncMock,
        ),
        patch(
            "services.filing_event_service.save_governance_flags",
            new_callable=AsyncMock,
        ) as mock_save_flags,
        patch(
            "services.filing_event_service.upsert_sec_filing_cursor",
            new_callable=AsyncMock,
        ) as mock_upsert_cursor,
    ):
        stats = await service.sync_symbol_filings("SMCI")

        # 兩筆事件皆應被處理並儲存
        assert stats["events"] == 2
        mock_save_events.assert_awaited_once()
        mock_save_flags.assert_awaited_once()
        mock_upsert_cursor.assert_awaited_once()
        # 驗證抓取 Form 4 時已剔除 xslF345X03 目錄前綴
        fetch_args, _ = mock_client.fetch_document_text.call_args
        assert "xslF345X03" not in fetch_args[0]
        assert fetch_args[0].endswith("/doc4.xml")


@pytest.mark.asyncio
async def test_filing_event_service_first_run_old_filings_initializes_cursor() -> None:
    """測試首次執行時，即使所有申報超出 90 天，游標仍應正常初始化以防止反覆全量掃描。"""
    mock_client = MagicMock()
    mock_client.get_cik = AsyncMock(return_value="0001045810")

    mock_submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001045810-25-000001"],
                "form": ["4"],
                "filingDate": ["2025-01-01"],
                "acceptanceDateTime": ["2025-01-01T16:30:00Z"],
                "primaryDocument": ["doc4.xml"],
                "items": [""],
            }
        }
    }
    mock_client.fetch_company_submissions = AsyncMock(return_value=mock_submissions)
    service = FilingEventService(client=mock_client)

    with (
        patch("services.filing_event_service.get_sec_filing_cursor", return_value=None),
        patch(
            "services.filing_event_service.save_sec_filing_events",
            new_callable=AsyncMock,
        ),
        patch(
            "services.filing_event_service.save_insider_transactions",
            new_callable=AsyncMock,
        ),
        patch(
            "services.filing_event_service.save_governance_flags",
            new_callable=AsyncMock,
        ),
        patch(
            "services.filing_event_service.upsert_sec_filing_cursor",
            new_callable=AsyncMock,
        ) as mock_upsert_cursor,
    ):
        stats = await service.sync_symbol_filings("NVDA")
        assert stats["events"] == 0
        mock_upsert_cursor.assert_awaited_once()
        args, _ = mock_upsert_cursor.call_args
        assert args[0].last_accepted_at == "2025-01-01T16:30:00Z"
