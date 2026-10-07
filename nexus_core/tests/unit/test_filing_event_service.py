"""單元測試：SEC 申報事件同步與回填服務 (filing_event_service.py)。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from market_analysis.fundamental_pipeline.models import FilingCursorRecord
from services.filing_event_service import FilingEventService
from services.sec_edgar_client import SecConfigError

_ET = ZoneInfo("America/New_York")

_VALID_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik><rptOwnerName>DOE JOHN</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionDate><value>2026-10-01</value></transactionDate>
            <transactionAmounts>
                <transactionShares><value>100</value></transactionShares>
                <transactionPricePerShare><value>10.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""


def _et(
    year: int, month: int, day: int, hour: int, minute: int, second: int
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=_ET)


def _make_client(
    recent: dict[str, list[str]],
    acceptance: dict[str, datetime],
    cik: str = "0001318605",
) -> MagicMock:
    """建立模擬 SEC 客戶端：表頭受理時間依 accession 查表，缺漏者拋出例外。"""
    client = MagicMock()
    client.get_cik = AsyncMock(return_value=cik)
    client.fetch_company_submissions = AsyncMock(
        return_value={"filings": {"recent": recent}}
    )
    client.fetch_document_text = AsyncMock(return_value=_VALID_FORM4_XML)

    async def _acceptance(_cik: str, accession: str) -> datetime:
        if accession not in acceptance:
            raise RuntimeError(f"header unavailable: {accession}")
        return acceptance[accession]

    client.fetch_acceptance_datetime = AsyncMock(side_effect=_acceptance)
    return client


@contextmanager
def _patched_db(cursor: FilingCursorRecord | None) -> Iterator[dict[str, Any]]:
    with (
        patch(
            "services.filing_event_service.get_sec_filing_cursor", return_value=cursor
        ),
        patch(
            "services.filing_event_service.save_sec_filing_events",
            new_callable=AsyncMock,
        ) as save_events,
        patch(
            "services.filing_event_service.save_insider_transactions",
            new_callable=AsyncMock,
        ) as save_txs,
        patch(
            "services.filing_event_service.save_governance_flags",
            new_callable=AsyncMock,
        ) as save_flags,
        patch(
            "services.filing_event_service.upsert_sec_filing_cursor",
            new_callable=AsyncMock,
        ) as upsert_cursor,
    ):
        yield {
            "save_events": save_events,
            "save_txs": save_txs,
            "save_flags": save_flags,
            "upsert_cursor": upsert_cursor,
        }


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _cursor(last_accepted_at: str, last_accession: str = "ACC-0") -> FilingCursorRecord:
    return FilingCursorRecord(
        symbol="TSLA",
        cik="0001318605",
        last_accepted_at=last_accepted_at,
        last_accession=last_accession,
    )


@pytest.mark.asyncio
async def test_filing_event_service_sync_symbol_filings() -> None:
    """增量同步 Form 4 與 8-K：事件與游標皆使用 SGML 表頭之權威美東時間。"""
    client = _make_client(
        {
            "accessionNumber": ["0001318605-26-000001", "0001318605-26-000002"],
            "form": ["4", "8-K"],
            "filingDate": ["2026-10-02", "2026-10-01"],
            "acceptanceDateTime": ["2026-10-02T20:30:00Z", "2026-10-01T21:00:00Z"],
            "primaryDocument": ["edgar.xml", "doc8k.htm"],
            "items": ["", "4.02"],
        },
        {
            "0001318605-26-000001": _et(2026, 10, 2, 16, 30, 0),
            "0001318605-26-000002": _et(2026, 10, 1, 17, 0, 0),
        },
    )
    service = FilingEventService(client=client)

    with _patched_db(_cursor("2026-09-30T00:00:00-04:00", "OLD-ACC")) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["events"] == 2
    assert stats["insider_txs"] == 1
    assert stats["failed"] == 0
    db["save_events"].assert_awaited_once()
    by_acc = {e.accession: e for e in db["save_events"].call_args.args[0]}
    assert by_acc["0001318605-26-000001"].accepted_at == "2026-10-02T16:30:00-04:00"
    assert by_acc["0001318605-26-000001"].session == "AMC"
    db["save_flags"].assert_awaited_once()
    db["upsert_cursor"].assert_awaited_once()
    new_cursor = db["upsert_cursor"].call_args.args[0]
    assert new_cursor.last_accepted_at == "2026-10-02T16:30:00-04:00"
    assert new_cursor.last_accession == "0001318605-26-000001"


@pytest.mark.asyncio
async def test_inflated_json_timestamp_uses_header_for_session() -> None:
    """JSON 值比真實 UTC 多出美東偏移（AAPL 實測）時，時段以表頭為準。

    真實樣本 AAPL 10-Q 0000320193-26-000020：JSON 2026-07-31T14:01:02Z（若當 UTC 為
    10:01 ET → RTH），SGML 表頭 20260731060102 → 06:01 ET → BMO。
    """
    client = _make_client(
        {
            "accessionNumber": ["0000320193-26-000020"],
            "form": ["10-Q"],
            "filingDate": ["2026-07-31"],
            "acceptanceDateTime": ["2026-07-31T14:01:02.000Z"],
            "primaryDocument": ["aapl-20260627.htm"],
            "items": [""],
        },
        {"0000320193-26-000020": _et(2026, 7, 31, 6, 1, 2)},
        cik="0000320193",
    )
    service = FilingEventService(client=client)
    with _patched_db(
        _cursor("2026-07-30T16:30:28-04:00", "0000320193-26-000018")
    ) as db:
        await service.sync_symbol_filings("AAPL")

    event = db["save_events"].call_args.args[0][0]
    assert event.session == "BMO"
    assert event.accepted_at == "2026-07-31T06:01:02-04:00"


@pytest.mark.asyncio
async def test_inflated_json_old_filing_is_not_reprocessed() -> None:
    """JSON 值偏高使舊申報看似晚於游標時，以表頭時間判定為舊申報並略過。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-NEW", "ACC-OLD"],
            "form": ["8-K", "8-K"],
            "filingDate": ["2026-10-05", "2026-10-05"],
            # ACC-OLD 真實 18:00 ET，JSON 偏高為次日 02:00Z（晚於游標 19:00 ET = 23:00Z）
            "acceptanceDateTime": ["2026-10-06T00:30:00Z", "2026-10-06T02:00:00Z"],
            "primaryDocument": ["new.htm", "old.htm"],
            "items": ["4.02", "4.02"],
        },
        {
            "ACC-NEW": _et(2026, 10, 5, 20, 30, 0),
            "ACC-OLD": _et(2026, 10, 5, 18, 0, 0),
        },
    )
    service = FilingEventService(client=client)
    with _patched_db(_cursor("2026-10-05T19:00:00-04:00", "ACC-PREV")) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["events"] == 1
    assert [e.accession for e in db["save_events"].call_args.args[0]] == ["ACC-NEW"]


@pytest.mark.asyncio
async def test_form4_failure_holds_cursor_before_first_failure() -> None:
    """Form 4 下載失敗時，游標停在第一筆失敗之前；較新的成功申報下次重處理。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-3", "ACC-2", "ACC-1"],
            "form": ["4", "4", "4"],
            "filingDate": ["2026-10-03", "2026-10-02", "2026-10-01"],
            "acceptanceDateTime": [
                "2026-10-03T22:00:00Z",
                "2026-10-02T22:00:00Z",
                "2026-10-01T22:00:00Z",
            ],
            "primaryDocument": ["a3.xml", "a2.xml", "a1.xml"],
            "items": ["", "", ""],
        },
        {
            "ACC-3": _et(2026, 10, 3, 18, 0, 0),
            "ACC-2": _et(2026, 10, 2, 18, 0, 0),
            "ACC-1": _et(2026, 10, 1, 18, 0, 0),
        },
    )

    async def _fetch(url: str, byte_cap: int = 1_500_000) -> str:
        if "/ACC2/" in url:
            raise RuntimeError("HTTP 503")
        return _VALID_FORM4_XML

    client.fetch_document_text = AsyncMock(side_effect=_fetch)
    service = FilingEventService(client=client)
    with _patched_db(_cursor("2026-09-30T18:00:00-04:00")) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["failed"] == 1
    # 成功者照常入庫（重處理時為冪等 upsert）
    saved = {e.accession for e in db["save_events"].call_args.args[0]}
    assert saved == {"ACC-3", "ACC-1"}
    # 游標只推進到 ACC-1（失敗的 ACC-2 之前），不得越過到 ACC-3
    new_cursor = db["upsert_cursor"].call_args.args[0]
    assert new_cursor.last_accession == "ACC-1"
    assert new_cursor.last_accepted_at == "2026-10-01T18:00:00-04:00"


@pytest.mark.asyncio
async def test_parse_failure_with_no_older_success_keeps_cursor() -> None:
    """唯一一筆新申報解析失敗時，游標維持原值（不寫入）。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-BAD"],
            "form": ["4"],
            "filingDate": ["2026-10-03"],
            "acceptanceDateTime": ["2026-10-03T22:00:00Z"],
            "primaryDocument": ["bad.xml"],
            "items": [""],
        },
        {"ACC-BAD": _et(2026, 10, 3, 18, 0, 0)},
    )
    client.fetch_document_text = AsyncMock(return_value="<ownershipDocument><broken>")
    service = FilingEventService(client=client)
    with _patched_db(_cursor("2026-09-30T18:00:00-04:00")) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["failed"] == 1
    db["save_txs"].assert_not_awaited()
    db["upsert_cursor"].assert_not_awaited()


@pytest.mark.asyncio
async def test_header_failure_holds_cursor_below_json_lower_bound() -> None:
    """表頭讀取失敗時，以 JSON 上界減最大偏差作為失敗下界，游標不得越過。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-NEW", "ACC-NOHDR", "ACC-OLD"],
            "form": ["8-K", "8-K", "8-K"],
            "filingDate": ["2026-10-03", "2026-10-02", "2026-10-01"],
            "acceptanceDateTime": [
                "2026-10-03T21:00:00Z",
                "2026-10-02T21:00:00Z",
                "2026-10-01T21:00:00Z",
            ],
            "primaryDocument": ["n.htm", "x.htm", "o.htm"],
            "items": ["", "", ""],
        },
        {
            "ACC-NEW": _et(2026, 10, 3, 17, 0, 0),
            "ACC-OLD": _et(2026, 10, 1, 17, 0, 0),
        },
    )
    service = FilingEventService(client=client)
    with _patched_db(_cursor("2026-09-30T17:00:00-04:00")) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["failed"] == 1
    assert db["upsert_cursor"].call_args.args[0].last_accession == "ACC-OLD"


@pytest.mark.asyncio
async def test_filing_event_service_first_run_retains_recent_8k_flags() -> None:
    """首次執行 (cursor is None) 時，30 天內的 8-K 申報不會因非首筆而被遺漏。"""
    client = _make_client(
        {
            "accessionNumber": ["0001375365-26-000001", "0001375365-26-000002"],
            "form": ["4", "8-K"],
            "filingDate": [_days_ago(1), _days_ago(3)],
            "acceptanceDateTime": ["2026-10-04T20:30:00Z", "2026-10-02T21:00:00Z"],
            "primaryDocument": ["xslF345X03/doc4.xml", "doc8k.htm"],
            "items": ["", "4.02"],
        },
        {
            "0001375365-26-000001": _et(2026, 10, 4, 16, 30, 0),
            "0001375365-26-000002": _et(2026, 10, 2, 17, 0, 0),
        },
        cik="0001375365",
    )
    service = FilingEventService(client=client)

    with _patched_db(None) as db:
        stats = await service.sync_symbol_filings("SMCI")

    assert stats["events"] == 2
    db["save_events"].assert_awaited_once()
    assert all(e.is_backfill for e in db["save_events"].call_args.args[0])
    db["save_flags"].assert_awaited_once()
    db["upsert_cursor"].assert_awaited_once()
    # 驗證抓取 Form 4 時已剔除 xslF345X03 目錄前綴
    fetch_args, _ = client.fetch_document_text.call_args
    assert "xslF345X03" not in fetch_args[0]
    assert fetch_args[0].endswith("/doc4.xml")


@pytest.mark.asyncio
async def test_first_run_failure_does_not_create_cursor() -> None:
    """首次回填有任何失敗時不建立游標，下次以回填模式（不推播）整批重試。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-A", "ACC-B"],
            "form": ["4", "4"],
            "filingDate": [_days_ago(1), _days_ago(2)],
            "acceptanceDateTime": ["2026-10-05T22:00:00Z", "2026-10-04T22:00:00Z"],
            "primaryDocument": ["a.xml", "b.xml"],
            "items": ["", ""],
        },
        {"ACC-A": _et(2026, 10, 5, 18, 0, 0)},  # ACC-B 表頭讀取失敗
    )
    service = FilingEventService(client=client)
    with _patched_db(None) as db:
        stats = await service.sync_symbol_filings("TSLA")

    assert stats["failed"] == 1
    db["upsert_cursor"].assert_not_awaited()


@pytest.mark.asyncio
async def test_filing_event_service_first_run_old_filings_initializes_cursor() -> None:
    """首次執行時，即使所有申報超出 90 天，游標仍以最新一筆的表頭時間初始化。"""
    client = _make_client(
        {
            "accessionNumber": ["0001045810-25-000001"],
            "form": ["4"],
            "filingDate": ["2025-01-01"],
            "acceptanceDateTime": ["2025-01-01T21:30:00Z"],
            "primaryDocument": ["doc4.xml"],
            "items": [""],
        },
        {"0001045810-25-000001": _et(2025, 1, 1, 16, 30, 0)},
        cik="0001045810",
    )
    service = FilingEventService(client=client)

    with _patched_db(None) as db:
        stats = await service.sync_symbol_filings("NVDA")

    assert stats["events"] == 0
    db["upsert_cursor"].assert_awaited_once()
    new_cursor = db["upsert_cursor"].call_args.args[0]
    assert new_cursor.last_accepted_at == "2025-01-01T16:30:00-05:00"
    assert new_cursor.last_accession == "0001045810-25-000001"


@pytest.mark.asyncio
async def test_backfill_flags_are_not_notified() -> None:
    """首次回填的治理旗標只入庫、不推播。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-8K"],
            "form": ["8-K"],
            "filingDate": [_days_ago(2)],
            "acceptanceDateTime": ["2026-10-03T21:00:00Z"],
            "primaryDocument": ["d.htm"],
            "items": ["4.02"],
        },
        {"ACC-8K": _et(2026, 10, 3, 17, 0, 0)},
    )
    service = FilingEventService(client=client, bot=MagicMock())
    with (
        _patched_db(None) as db,
        patch("services.filing_event_service.config") as mock_config,
        patch.object(
            service, "_dispatch_governance_notifications", new_callable=AsyncMock
        ) as mock_dispatch,
    ):
        mock_config.FUNDAMENTAL_PIPELINE_DRY_RUN = False
        await service.sync_symbol_filings("TSLA")

    db["save_flags"].assert_awaited_once()
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_incremental_critical_flag_notifies_with_dedup_key() -> None:
    """增量模式 CRITICAL 旗標推播帶 dedup_key（使用者 + symbol + flag_kind + 事件日）。"""
    client = _make_client(
        {
            "accessionNumber": ["ACC-8K"],
            "form": ["8-K"],
            "filingDate": ["2026-10-05"],
            "acceptanceDateTime": ["2026-10-05T21:00:00Z"],
            "primaryDocument": ["d.htm"],
            "items": ["4.02,5.02"],
        },
        {"ACC-8K": _et(2026, 10, 5, 17, 0, 0)},
    )
    service = FilingEventService(client=client, bot=MagicMock())
    with (
        _patched_db(_cursor("2026-10-01T17:00:00-04:00")),
        patch("services.filing_event_service.config") as mock_config,
        patch(
            "database.portfolio.get_all_portfolio_symbol_pairs",
            return_value=[(111, "TSLA"), (222, "AAPL")],
        ),
        patch(
            "services.notification_dispatcher.notify", new_callable=AsyncMock
        ) as mock_notify,
    ):
        mock_config.FUNDAMENTAL_PIPELINE_DRY_RUN = False
        await service.sync_symbol_filings("TSLA")

    # 只有 4.02 CRITICAL 推播；5.02 僅 item code → REVIEW 不推播；只推給持有 TSLA 者
    mock_notify.assert_awaited_once()
    args, kwargs = mock_notify.call_args
    assert args[1] == 111
    assert args[2] == "defense_fundamental_thesis"
    assert (
        kwargs["dedup_key"]
        == "governance_flag_111_TSLA_ITEM_4_02_RESTATEMENT_2026-10-05"
    )


@pytest.mark.asyncio
async def test_sync_universe_fails_fast_on_sec_config_error() -> None:
    """缺少合規 User-Agent 時，在 gather 之前即拋出 SecConfigError。"""
    service = FilingEventService()
    with (
        patch(
            "services.filing_event_service.SecEdgarClient",
            side_effect=SecConfigError("missing UA"),
        ),
        patch(
            "services.filing_event_service.get_fundamental_universe",
            new_callable=AsyncMock,
        ) as mock_universe,
    ):
        with pytest.raises(SecConfigError):
            await service.sync_universe_filings()
    mock_universe.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_universe_logs_per_symbol_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """個別標的例外以 logger.error 記錄，不中斷其他標的統計。"""
    service = FilingEventService(client=MagicMock())

    async def _sync(sym: str, backfill_form4_days: int = 90) -> dict[str, int]:
        if sym == "BAD":
            raise RuntimeError("boom")
        return {"events": 2, "insider_txs": 1, "governance_flags": 0, "failed": 0}

    with (
        patch(
            "services.filing_event_service.get_fundamental_universe",
            new_callable=AsyncMock,
            return_value=["GOOD", "BAD"],
        ),
        patch.object(service, "sync_symbol_filings", side_effect=_sync),
        caplog.at_level("ERROR", logger="services.filing_event_service"),
    ):
        totals = await service.sync_universe_filings()

    assert totals["events"] == 2
    assert any(
        "BAD" in r.getMessage() and "boom" in r.getMessage() for r in caplog.records
    )
