"""單元測試：財務預期差與指引協調服務 (earnings_surprise_service.py)。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from market_analysis.fundamental_pipeline.models import (
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    FilingEventRecord,
    GuidanceExtraction,
    GuidanceExtractionDTO,
    MarginGuidance,
    ToneMetric,
)
from market_analysis.fundamental_pipeline.press_release import (
    parse_index_headers_documents,
)
from services.earnings_surprise_service import (
    LLM_GUIDANCE_MAX_TOKENS,
    EarningsSurpriseService,
)
from services.fundamental_providers import ConsensusData

_FIXTURES = Path(__file__).parent / "fixtures" / "sec"
_AAPL_DIR = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000005"
_AAPL_EX991_URL = f"{_AAPL_DIR}/a8-kex991q1202612272025.htm"
_AAPL_MAIN_URL = f"{_AAPL_DIR}/aapl-20260129.htm"
_SVC = "services.earnings_surprise_service"


def _read(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _aapl_event(accession: str = "0000320193-26-000005") -> FilingEventRecord:
    return FilingEventRecord(
        accession=accession,
        symbol="AAPL",
        form="8-K",
        items="2.02,9.01",
        accepted_at="2026-01-29T16:30:33-05:00",
        session="AMC",
        primary_doc_url=_AAPL_MAIN_URL,
        routes_json='["EARNINGS"]',
    )


def _aapl_sec_client() -> MagicMock:
    """模擬 SEC 客戶端：申報目錄回傳實際 AAPL index-headers，EX-99.1 回傳實際新聞稿片段。"""
    docs = parse_index_headers_documents(
        _read("0000320193-26-000005-index-headers.html")
    )
    client = MagicMock()
    client.fetch_filing_documents = AsyncMock(return_value=docs)

    async def fetch_text(url: str, byte_cap: int = 1_500_000) -> str:
        if url == _AAPL_EX991_URL:
            return _read("aapl_ex991_0000320193-26-000005_head.htm")
        if url == _AAPL_MAIN_URL:
            return _read("aapl_8k_cover_0000320193-26-000005_head.htm")
        raise AssertionError(f"unexpected url {url}")

    client.fetch_document_text = AsyncMock(side_effect=fetch_text)
    return client


def _aapl_consensus(**overrides: Any) -> ConsensusData:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "fiscal_period": "2026-Q1",
        "actual_eps": 2.84,
        "consensus_eps": 2.67,
        "actual_revenue": 143.8e9,
        "consensus_revenue": 138.4e9,
        "session": "AMC",
    }
    base.update(overrides)
    return ConsensusData(**base)


def _consensus_provider(consensus: ConsensusData | None) -> MagicMock:
    provider = MagicMock()
    provider.get_consensus = AsyncMock(return_value=consensus)
    provider.get_estimate_snapshots = AsyncMock(
        return_value=[
            EPSEstimateSnapshotRecord(
                symbol="AAPL",
                snapshot_date="2026-01-30",
                horizon="0q",
                source="finnhub",
                eps_mean=1.95,
            )
        ]
    )
    return provider


def _grounded_guidance(
    fiscal_period: str = "Q1 FY2026", **kw: Any
) -> GuidanceExtraction:
    params: dict[str, Any] = {
        "symbol": "AAPL",
        "fiscal_period": fiscal_period,
        "guidance_target_period": None,
        "margin_guidance": [],
        "backlog_tone": ToneMetric(
            score=2,
            quote_snippet="iPhone had its best-ever quarter driven by unprecedented demand",
        ),
        "pricing_power_tone": ToneMetric(score=0, quote_snippet=""),
        "supply_chain_tone": ToneMetric(score=0, quote_snippet=""),
        "defensive_posture_tone": ToneMetric(score=0, quote_snippet=""),
        "reasoning_traditional_chinese": "需求創紀錄。",
    }
    params.update(kw)
    return GuidanceExtraction(**params)


@pytest.mark.asyncio
async def test_process_filing_event_non_earnings() -> None:
    """測試非 8-K Item 2.02 事件跳過處理。"""
    service = EarningsSurpriseService()
    event = FilingEventRecord(
        accession="ACC-FORM-4",
        symbol="TSLA",
        form="4",
        items=None,
        accepted_at="2026-10-05 16:30:00",
        session="AMC",
    )
    result = await service.process_filing_event(event)
    assert result is None


@pytest.mark.asyncio
async def test_process_filing_event_feeds_exhibit_99_1_text_to_llm() -> None:
    """LLM 收到的是清洗後的 EX-99.1 新聞稿，而非 8-K 封面或 iXBRL 表頭。"""
    sec_client = _aapl_sec_client()
    provider = _consensus_provider(_aapl_consensus())
    service = EarningsSurpriseService(
        sec_client=sec_client, consensus_provider=provider
    )
    llm_mock = AsyncMock(return_value=_grounded_guidance())

    with (
        patch.object(service, "_extract_guidance_via_llm", new=llm_mock),
        patch(f"{_SVC}.get_prior_guidance_extraction", return_value=None),
        patch(f"{_SVC}.save_guidance_extraction", new=AsyncMock()) as save_g,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()) as save_snap,
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch("config.API_KEY", "mock-key"),
    ):
        dto = await service.process_filing_event(_aapl_event())

    # 財季由受理日對齊 Finnhub 推導
    provider.get_consensus.assert_awaited_once_with(
        "AAPL", None, as_of=date(2026, 1, 29)
    )
    sec_client.fetch_filing_documents.assert_awaited_once_with(
        _AAPL_DIR, "0000320193-26-000005"
    )
    fetched_urls = [c.args[0] for c in sec_client.fetch_document_text.await_args_list]
    assert fetched_urls == [_AAPL_EX991_URL]

    doc_text = llm_mock.await_args_list[-1].kwargs["doc_text"]
    assert "Apple reports first quarter results" in doc_text
    assert "<" not in doc_text and "xbrli" not in doc_text

    assert dto is not None
    assert dto.fiscal_period == "2026-Q1"
    assert dto.status == "PROCESSED"
    assert dto.composite_score is not None and dto.composite_score > 0
    save_s.assert_awaited_once()
    save_snap.assert_awaited_once()

    save_g.assert_awaited_once()
    saved_g: GuidanceExtractionDTO = save_g.await_args_list[-1].args[0]
    # 主鍵期別取推導財季，而非 LLM 自由輸出之 "Q1 FY2026"
    assert saved_g.fiscal_period == "2026-Q1"
    # 無前期可比：NOT NULL 欄寫 0.0
    assert saved_g.tone_delta_score == 0.0
    # 信心 = 1 個可溯源態度維度 / 7
    assert saved_g.confidence_score == round(1 / 7, 4)


@pytest.mark.asyncio
async def test_process_filing_event_skips_guidance_when_no_ex99() -> None:
    """申報目錄沒有 EX-99 附件時不呼叫 LLM、不退回 8-K 封面，預期差照常計算。"""
    sec_client = MagicMock()
    sec_client.fetch_filing_documents = AsyncMock(return_value=[])
    sec_client.fetch_document_text = AsyncMock()
    service = EarningsSurpriseService(
        sec_client=sec_client,
        consensus_provider=_consensus_provider(_aapl_consensus()),
    )
    llm_mock = AsyncMock()
    with (
        patch.object(service, "_extract_guidance_via_llm", new=llm_mock),
        patch(f"{_SVC}.save_guidance_extraction", new=AsyncMock()) as save_g,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch("config.API_KEY", "mock-key"),
    ):
        dto = await service.process_filing_event(_aapl_event())

    llm_mock.assert_not_awaited()
    sec_client.fetch_document_text.assert_not_awaited()
    save_g.assert_not_awaited()
    save_s.assert_awaited_once()
    assert dto is not None


@pytest.mark.asyncio
async def test_process_filing_event_drops_ungrounded_tone() -> None:
    """非零態度分數的引文在新聞稿找不到時放棄該筆指引，不寫入編造分數。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(_aapl_consensus()),
    )
    fabricated = _grounded_guidance(
        pricing_power_tone=ToneMetric(
            score=2, quote_snippet="We raised prices across every product line"
        )
    )
    with (
        patch.object(
            service,
            "_extract_guidance_via_llm",
            new=AsyncMock(return_value=fabricated),
        ),
        patch(f"{_SVC}.get_prior_guidance_extraction", return_value=None),
        patch(f"{_SVC}.save_guidance_extraction", new=AsyncMock()) as save_g,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch("config.API_KEY", "mock-key"),
    ):
        await service.process_filing_event(_aapl_event())

    save_g.assert_not_awaited()
    save_s.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_filing_event_prior_tone_excludes_same_and_newer_quarter() -> (
    None
):
    """前期態度以「早於當期財季且非同一申報」查詢，delta = 當期分數 − 前期分數。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(_aapl_consensus()),
    )
    prior_obj = _grounded_guidance(
        fiscal_period="2025-Q4",
        backlog_tone=ToneMetric(score=1, quote_snippet="x"),
    )
    prior_record = GuidanceExtractionDTO(
        symbol="AAPL",
        fiscal_period="2025-Q4",
        source_accession="0000320193-25-000071",
        model_version="gpt-4o",
        confidence_score=0.5,
        tone_delta_score=15.0,
        data_json=prior_obj.model_dump_json(),
    )
    prior_lookup = MagicMock(return_value=prior_record)
    with (
        patch.object(
            service,
            "_extract_guidance_via_llm",
            new=AsyncMock(return_value=_grounded_guidance()),
        ),
        patch(f"{_SVC}.get_prior_guidance_extraction", new=prior_lookup),
        patch(f"{_SVC}.save_guidance_extraction", new=AsyncMock()) as save_g,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()),
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch("config.API_KEY", "mock-key"),
    ):
        await service.process_filing_event(_aapl_event())

    prior_lookup.assert_called_once_with("AAPL", "2026-Q1", "0000320193-26-000005")
    saved: GuidanceExtractionDTO = save_g.await_args_list[-1].args[0]
    # 當期 (2,0,0,0) -> 25.0；前期 (1,0,0,0) -> 12.5；delta 用前期分數而非前期 delta
    assert saved.tone_delta_score == 12.5


@pytest.mark.asyncio
async def test_process_filing_event_no_reliable_quarter_writes_nothing() -> None:
    """受理日對齊不到 Finnhub 財報條目時不推算日曆季、不寫入任何記錄。"""
    provider = _consensus_provider(None)
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(), consensus_provider=provider
    )
    llm_mock = AsyncMock()
    with (
        patch.object(service, "_extract_guidance_via_llm", new=llm_mock),
        patch(f"{_SVC}.save_guidance_extraction", new=AsyncMock()) as save_g,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch("config.API_KEY", "mock-key"),
    ):
        res = await service.process_filing_event(_aapl_event())

    assert res is None
    llm_mock.assert_not_awaited()
    save_g.assert_not_awaited()
    save_s.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_filing_event_unparseable_period_writes_nothing() -> None:
    """共識回傳之期別無法正規化（空字串）時同樣不寫入（取代舊的「現在日曆季」fallback）。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(_aapl_consensus(fiscal_period="")),
    )
    with (
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=False),
    ):
        res = await service.process_filing_event(_aapl_event())
    assert res is None
    save_s.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_filing_event_pending_does_not_overwrite_processed() -> None:
    """實際值尚未更新（PENDING）時不得覆蓋同季既有 PROCESSED 記錄。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(
            _aapl_consensus(actual_eps=None, actual_revenue=None)
        ),
    )
    existing = EarningsSurpriseDTO(
        symbol="AAPL",
        fiscal_period="2026-Q1",
        composite_score=40.0,
        status="PROCESSED",
    )
    with (
        patch(f"{_SVC}.get_earnings_surprise", return_value=existing),
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=False),
    ):
        res = await service.process_filing_event(_aapl_event())
    assert res is None
    save_s.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_filing_event_pending_with_consensus_is_written() -> None:
    """尚無實際值但有共識值、且無既有記錄時寫入 PENDING（之後 evaluate 會重查）。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(
            _aapl_consensus(actual_eps=None, actual_revenue=None)
        ),
    )
    with (
        patch(f"{_SVC}.get_earnings_surprise", return_value=None),
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=False),
    ):
        res = await service.process_filing_event(_aapl_event())
    assert res is not None
    assert res.status == "PENDING"
    assert res.consensus_eps == 2.67
    save_s.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_filing_event_all_empty_consensus_not_written() -> None:
    """共識與實際值全空時不寫入全空 PENDING 列。"""
    service = EarningsSurpriseService(
        sec_client=_aapl_sec_client(),
        consensus_provider=_consensus_provider(
            ConsensusData(symbol="AAPL", fiscal_period="2026-Q1")
        ),
    )
    with (
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
        patch(f"{_SVC}.save_eps_estimate_snapshots", new=AsyncMock()),
        patch(f"{_SVC}.is_memory_safe", return_value=False),
    ):
        res = await service.process_filing_event(_aapl_event())
    assert res is None
    save_s.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_guidance_via_llm_token_budget() -> None:
    """LLM 輸出上限足以容納四維引文與繁中論述（>= 2000 tokens）。"""
    assert LLM_GUIDANCE_MAX_TOKENS >= 2000
    service = EarningsSurpriseService(consensus_provider=_consensus_provider(None))
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.parsed = _grounded_guidance(
        margin_guidance=[MarginGuidance(metric_name="Gross Margin", direction="FLAT")]
    )
    parse_mock = AsyncMock(return_value=completion)
    with patch(f"{_SVC}.llm_client") as llm_client:
        llm_client.beta.chat.completions.parse = parse_mock
        res = await service._extract_guidance_via_llm("AAPL", "ACC", "text")
    assert res is not None
    assert (
        parse_mock.await_args_list[-1].kwargs["max_tokens"] == LLM_GUIDANCE_MAX_TOKENS
    )


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_processed_cache_hit() -> None:
    """目標財季已有 PROCESSED 記錄時直接回傳，不查詢提供者。"""
    provider = _consensus_provider(None)
    service = EarningsSurpriseService(consensus_provider=provider)
    cached_record = EarningsSurpriseDTO(
        symbol="NVDA",
        fiscal_period="2026-Q2",
        actual_eps=0.68,
        consensus_eps=0.65,
        composite_score=18.5,
        status="PROCESSED",
    )
    with patch(f"{_SVC}.get_earnings_surprise", return_value=cached_record) as get_es:
        res = await service.evaluate_symbol_surprise("NVDA", "Q2 2026")
    assert res is cached_record
    get_es.assert_called_once_with("NVDA", "2026-Q2")
    provider.get_consensus.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_pending_cache_is_refetched() -> None:
    """PENDING 快取不算命中：重查提供者並以 PROCESSED 結果更新。"""
    provider = _consensus_provider(
        _aapl_consensus(fiscal_period="2026-Q1", session="UNKNOWN")
    )
    service = EarningsSurpriseService(consensus_provider=provider)
    pending = EarningsSurpriseDTO(
        symbol="AAPL", fiscal_period="2026-Q1", consensus_eps=2.67, status="PENDING"
    )
    with (
        patch(f"{_SVC}.get_earnings_surprise", return_value=pending),
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
    ):
        res = await service.evaluate_symbol_surprise("AAPL", "2026-Q1")
    provider.get_consensus.assert_awaited_once_with("AAPL", "2026-Q1")
    assert res is not None and res.status == "PROCESSED"
    save_s.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_latest_resolves_period_before_cache() -> None:
    """未指定財季時先由提供者決定最近已公布財季，再以該財季查快取。"""
    provider = _consensus_provider(_aapl_consensus(fiscal_period="2026-Q3"))
    service = EarningsSurpriseService(consensus_provider=provider)
    processed = EarningsSurpriseDTO(
        symbol="AAPL",
        fiscal_period="2026-Q3",
        composite_score=5.0,
        status="PROCESSED",
    )
    with (
        patch(f"{_SVC}.get_earnings_surprise", return_value=processed) as get_es,
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as save_s,
    ):
        res = await service.evaluate_symbol_surprise("AAPL")
    get_es.assert_called_once_with("AAPL", "2026-Q3")
    assert res is processed
    save_s.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_fetches_whisper() -> None:
    """測試 evaluate_symbol_surprise 正確整合 whisper 數據（以推導財季查詢）。"""
    mock_consensus = MagicMock()
    mock_consensus.get_consensus = AsyncMock(
        return_value=ConsensusData(
            symbol="AAPL",
            fiscal_period="2026-Q3",
            actual_eps=1.50,
            consensus_eps=1.45,
            actual_revenue=85000000000.0,
            consensus_revenue=84000000000.0,
        )
    )
    mock_whisper = MagicMock()
    mock_whisper.get_whisper = AsyncMock(return_value=1.48)

    service = EarningsSurpriseService(
        consensus_provider=mock_consensus,
        whisper_provider=mock_whisper,
    )

    with (
        patch(f"{_SVC}.get_earnings_surprise", return_value=None),
        patch(f"{_SVC}.save_earnings_surprise", new=AsyncMock()) as mock_save,
    ):
        res = await service.evaluate_symbol_surprise("AAPL")
        assert res is not None
        assert res.whisper_eps == 1.48
        mock_whisper.get_whisper.assert_awaited_once_with("AAPL", "2026-Q3")
        mock_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_rejects_unparseable_period() -> None:
    provider = _consensus_provider(None)
    service = EarningsSurpriseService(consensus_provider=provider)
    assert await service.evaluate_symbol_surprise("AAPL", "third quarter") is None
    provider.get_consensus.assert_not_awaited()
