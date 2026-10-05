"""單元測試：財務預期差與指引協調服務 (earnings_surprise_service.py)。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from market_analysis.fundamental_pipeline.models import (
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    FilingEventRecord,
    GuidanceExtraction,
    MarginGuidance,
    ToneMetric,
)
from services.earnings_surprise_service import EarningsSurpriseService
from services.fundamental_providers import ConsensusData


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
async def test_process_filing_event_item_2_02_success() -> None:
    """測試 8-K Item 2.02 事件協調解析與綜合入庫流程。"""
    mock_sec_client = MagicMock()
    mock_sec_client.fetch_document_text = AsyncMock(
        return_value="Tesla reports record Q3 earnings with $25.1B revenue and $0.72 EPS."
    )

    mock_consensus_provider = MagicMock()
    mock_consensus_provider.get_consensus = AsyncMock(
        return_value=ConsensusData(
            symbol="TSLA",
            fiscal_period="2026-Q3",
            actual_eps=0.72,
            consensus_eps=0.68,
            actual_revenue=25100000000.0,
            consensus_revenue=24800000000.0,
            session="AMC",
            source="finnhub",
        )
    )
    mock_consensus_provider.get_estimate_snapshots = AsyncMock(
        return_value=[
            EPSEstimateSnapshotRecord(
                symbol="TSLA",
                snapshot_date="2026-10-05",
                horizon="0q",
                source="finnhub",
                eps_mean=0.68,
            )
        ]
    )

    service = EarningsSurpriseService(
        sec_client=mock_sec_client,
        consensus_provider=mock_consensus_provider,
    )

    event = FilingEventRecord(
        accession="0001318605-26-000030",
        symbol="TSLA",
        form="8-K",
        items="2.02,9.01",
        accepted_at="2026-10-05 16:15:00",
        session="AMC",
        primary_doc_url="https://sec.gov/tsla-8k.htm",
        routes_json='["EARNINGS"]',
    )

    mock_guidance = GuidanceExtraction(
        symbol="TSLA",
        fiscal_period="2026-Q3",
        revenue_guidance_midpoint_usd=25500000000.0,
        eps_guidance_midpoint_usd=0.75,
        margin_guidance=[
            MarginGuidance(metric_name="Gross Margin", direction="EXPANDING")
        ],
        backlog_tone=ToneMetric(score=1, quote_snippet="交車積壓"),
        pricing_power_tone=ToneMetric(score=1, quote_snippet="毛利提升"),
        supply_chain_tone=ToneMetric(score=0, quote_snippet="正常"),
        defensive_posture_tone=ToneMetric(score=0, quote_snippet="無"),
        reasoning_traditional_chinese="管理層對第四季交付展望樂觀。",
    )

    with (
        patch.object(
            service,
            "_extract_guidance_via_llm",
            new=AsyncMock(return_value=mock_guidance),
        ),
        patch(
            "services.earnings_surprise_service.save_guidance_extraction",
            new=AsyncMock(),
        ) as mock_save_guidance,
        patch(
            "services.earnings_surprise_service.save_earnings_surprise", new=AsyncMock()
        ) as mock_save_surprise,
        patch(
            "services.earnings_surprise_service.save_eps_estimate_snapshots",
            new=AsyncMock(),
        ) as mock_save_snapshots,
    ):
        surprise_dto = await service.process_filing_event(event)

        assert surprise_dto is not None
        assert surprise_dto.symbol == "TSLA"
        assert surprise_dto.fiscal_period == "2026-Q3"
        assert surprise_dto.actual_eps == 0.72
        assert surprise_dto.consensus_eps == 0.68
        assert surprise_dto.eps_surprise_pct is not None
        assert surprise_dto.eps_surprise_pct > 0
        assert surprise_dto.composite_score is not None
        assert surprise_dto.composite_score > 0
        assert surprise_dto.status == "PROCESSED"

        mock_save_guidance.assert_awaited_once()
        mock_save_surprise.assert_awaited_once()
        mock_save_snapshots.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_symbol_surprise_cache_and_fetch() -> None:
    """測試 evaluate_symbol_surprise 快取命中與即時評估。"""
    service = EarningsSurpriseService()

    cached_record = EarningsSurpriseDTO(
        symbol="NVDA",
        fiscal_period="2026-Q2",
        actual_eps=0.68,
        consensus_eps=0.65,
        composite_score=18.5,
    )

    with patch(
        "services.earnings_surprise_service.get_latest_earnings_surprise",
        return_value=cached_record,
    ):
        res = await service.evaluate_symbol_surprise("NVDA")
        assert res is not None
        assert res.symbol == "NVDA"
        assert res.composite_score == 18.5
