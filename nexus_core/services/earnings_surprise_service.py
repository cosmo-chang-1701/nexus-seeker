"""財務預期差與管理層指引協調服務 (EarningsSurpriseService)。

職責：
1. 協調 8-K Item 2.02 新聞稿文本（Exhibit 99.1）拉取與解析。
2. 透過 OpenAI Beta 結構化輸出解析管理層前瞻指引 (GuidanceExtraction) 與語意態度評分。
3. 整合 ConsensusProvider (Finnhub 等免費源) 與 WhisperProvider。
4. 計算財務雙維預期差 (EPS / Revenue / Whisper) 與綜合驚喜分數，持久化至 earnings_surprise 表。
5. 維護分析師 EPS 預估快照 (eps_estimate_snapshot)。
6. 遵循 1GB–2GB VPS 記憶體守衛 (is_memory_safe) 與 Single-Writer 持久化架構。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import config
from database.fundamental_pipeline import (
    get_latest_earnings_surprise,
    get_latest_guidance_extraction,
    save_earnings_surprise,
    save_eps_estimate_snapshots,
    save_guidance_extraction,
)
from market_analysis.fundamental_pipeline.earnings_surprise import (
    evaluate_earnings_surprise,
)
from market_analysis.fundamental_pipeline.guidance_delta import (
    calculate_management_tone_score,
    calculate_tone_delta,
)
from market_analysis.fundamental_pipeline.models import (
    EarningsSurpriseDTO,
    EarningsSurpriseStatus,
    EPSEstimateSnapshotRecord,
    FilingEventRecord,
    FilingSession,
    GuidanceExtraction,
    GuidanceExtractionDTO,
)
from services.fundamental_providers import (
    ConsensusData,
    ConsensusProvider,
    FinnhubConsensusProvider,
    NullWhisperProvider,
    WhisperProvider,
)
from services.llm_service import client as llm_client
from services.llm_service import is_memory_safe
from services.sec_edgar_client import SecEdgarClient

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")


class EarningsSurpriseService:
    """財務預期差、指引擷取與共識快照協調服務。"""

    def __init__(
        self,
        sec_client: SecEdgarClient | None = None,
        consensus_provider: ConsensusProvider | None = None,
        whisper_provider: WhisperProvider | None = None,
        bot: Any | None = None,
    ) -> None:
        self._sec_client = sec_client
        self._consensus_provider = (
            consensus_provider
            if consensus_provider is not None
            else FinnhubConsensusProvider()
        )
        self._whisper_provider = (
            whisper_provider if whisper_provider is not None else NullWhisperProvider()
        )
        self._bot = bot

    async def get_sec_client(self) -> SecEdgarClient:
        """惰性獲取或初始化 SEC EDGAR 客戶端。"""
        if self._sec_client is None:
            self._sec_client = SecEdgarClient()
        return self._sec_client

    async def process_filing_event(
        self, event: FilingEventRecord
    ) -> EarningsSurpriseDTO | None:
        """處理單一 SEC 申報事件；若為 8-K Item 2.02 (EARNINGS) 則執行預期差分析與指引擷取。"""
        sym_upper = event.symbol.strip().upper()
        items_str = event.items or ""
        routes_str = event.routes_json or ""

        # 判定是否為 8-K Item 2.02 財報發布
        is_earnings = event.form in ("8-K", "8-K/A") and (
            "2.02" in items_str or "EARNINGS" in routes_str
        )
        if not is_earnings:
            return None

        logger.info(
            f"[EarningsSurpriseService] 捕捉到 {sym_upper} 8-K Item 2.02 財報事件: {event.accession}"
        )

        # 1. 嘗試拉取 Exhibit 99.1 新聞稿或主文件文本 (硬性限制 1.5MB)
        doc_text: str = ""
        sec_client = await self.get_sec_client()
        if event.primary_doc_url:
            try:
                doc_text = await sec_client.fetch_document_text(event.primary_doc_url)
            except Exception as e:
                logger.warning(
                    f"[EarningsSurpriseService] 拉取 8-K 原始文件文本失敗 ({event.accession}): {e}"
                )

        # 2. 結構化指引擷取 (若配置了 LLM 且記憶體安全)
        extraction_dto: GuidanceExtractionDTO | None = None
        extracted_guidance: GuidanceExtraction | None = None

        if doc_text and config.API_KEY and is_memory_safe():
            try:
                extracted_guidance = await self._extract_guidance_via_llm(
                    symbol=sym_upper,
                    accession=event.accession,
                    doc_text=doc_text,
                )
                if extracted_guidance:
                    tone_score = calculate_management_tone_score(extracted_guidance)
                    prior_record = await asyncio.to_thread(
                        get_latest_guidance_extraction, sym_upper
                    )
                    prior_tone = prior_record.tone_delta_score if prior_record else None
                    tone_delta = calculate_tone_delta(tone_score, prior_tone)

                    extraction_dto = GuidanceExtractionDTO(
                        symbol=sym_upper,
                        fiscal_period=extracted_guidance.fiscal_period,
                        source_accession=event.accession,
                        model_version=config.LLM_MODEL_NAME,
                        confidence_score=0.90,
                        tone_delta_score=tone_delta,
                        data_json=extracted_guidance.model_dump_json(),
                    )
                    await save_guidance_extraction(extraction_dto)
                    logger.info(
                        f"[EarningsSurpriseService] {sym_upper} 結構化指引入庫完成 (Tone: {tone_score:+.1f})"
                    )
            except Exception as e:
                logger.warning(
                    f"[EarningsSurpriseService] LLM 解析指引失敗 ({sym_upper}, {event.accession}): {e}"
                )

        # 3. 獲取分析師共識與實際業績
        target_period: str | None = (
            extracted_guidance.fiscal_period if extracted_guidance else None
        )
        consensus: ConsensusData | None = None
        try:
            consensus = await self._consensus_provider.get_consensus(
                sym_upper, target_period
            )
        except Exception as e:
            logger.warning(
                f"[EarningsSurpriseService] 共識資料讀取異常 ({sym_upper}): {e}"
            )

        # 4. 獲取買方耳語預期 (Whisper)
        whisper_val: float | None = None
        try:
            whisper_val = await self._whisper_provider.get_whisper(
                sym_upper, target_period
            )
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] Whisper 讀取異常: {e}")

        # 5. 執行財務預期差綜合評估
        actual_eps = consensus.actual_eps if consensus else None
        consensus_eps = consensus.consensus_eps if consensus else None
        actual_rev = consensus.actual_revenue if consensus else None
        consensus_rev = consensus.consensus_revenue if consensus else None

        # 若指引中有提取當期數字且共識為空時補充
        if (
            actual_eps is None
            and extracted_guidance
            and extracted_guidance.eps_guidance_midpoint_usd is not None
        ):
            pass  # 指引為前瞻預期，不混入已結算之 actual_eps

        surprise_result = evaluate_earnings_surprise(
            actual_eps=actual_eps,
            consensus_eps=consensus_eps,
            actual_revenue=actual_rev,
            consensus_revenue=consensus_rev,
            whisper_eps=whisper_val,
        )

        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        fiscal_period = (
            consensus.fiscal_period
            if consensus and consensus.fiscal_period
            else (target_period if target_period else now_et.strftime("%Y-Q%m"))
        )

        session_str: FilingSession | Literal["UNKNOWN"] = (
            event.session if event.session in ("BMO", "AMC") else "UNKNOWN"
        )
        status_val: EarningsSurpriseStatus = (
            "PROCESSED" if surprise_result.composite_score is not None else "PENDING"
        )

        surprise_dto = EarningsSurpriseDTO(
            symbol=sym_upper,
            fiscal_period=fiscal_period,
            actual_eps=actual_eps,
            consensus_eps=consensus_eps,
            eps_surprise_pct=surprise_result.eps_surprise_pct,
            actual_revenue=actual_rev,
            consensus_revenue=consensus_rev,
            revenue_surprise_pct=surprise_result.revenue_surprise_pct,
            whisper_eps=whisper_val,
            composite_score=surprise_result.composite_score,
            session=session_str,
            eps_basis="VENDOR_ADJUSTED",
            status=status_val,
        )

        await save_earnings_surprise(surprise_dto)

        # 6. 維護並更新分析師預估快照 (0q, +1q, 0y, +1y)
        try:
            snapshots = await self._consensus_provider.get_estimate_snapshots(sym_upper)
            if snapshots:
                await save_eps_estimate_snapshots(snapshots)
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] 快照更新失敗: {e}")

        return surprise_dto

    async def _extract_guidance_via_llm(
        self, symbol: str, accession: str, doc_text: str
    ) -> GuidanceExtraction | None:
        """透過 OpenAI beta.chat.completions.parse 提取結構化前瞻指引。"""
        # 截取文本前 15,000 字以內之關鍵章節 (防護 token 上限)
        snippet = doc_text[:15000]

        system_prompt = (
            "你是美股頂級買方基本面研究員。請自所提供的 8-K Item 2.02 新聞稿文本中，"
            "嚴格抽取管理層對未來季度的結構化財務前瞻指引 (Guidance) 與語意態度。\n"
            "1. 若有具體數值 (營收/EPS 指引區間)，請換算為中點金額 (USD)。\n"
            "2. 毛利率/營益率請識別擴張 (EXPANDING)、壓縮 (COMPRESSING) 或持平 (FLAT)。\n"
            "3. 態度指標 (score) 介於 -2 至 +2，必須包含原文摘錄 quote_snippet。\n"
            "4. reasoning_traditional_chinese 必須使用 100% 繁體中文撰寫完整質化論述。\n"
            "5. 禁止幻想不存在的數字，若新聞稿未提供明確數值則填寫 None。"
        )

        user_prompt = (
            f"標的代號: {symbol}\n申報編號: {accession}\n\n新聞稿原文內容:\n{snippet}"
        )

        completion = await llm_client.beta.chat.completions.parse(
            model=config.LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=GuidanceExtraction,
            temperature=0.0,
            max_tokens=800,
        )
        parsed = completion.choices[0].message.parsed if completion.choices else None
        if isinstance(parsed, GuidanceExtraction):
            return parsed
        return None

    async def sync_symbol_estimates(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        """主動同步單一標的之分析師預估共識快照。"""
        sym_upper = symbol.strip().upper()
        snapshots = await self._consensus_provider.get_estimate_snapshots(sym_upper)
        if snapshots:
            await save_eps_estimate_snapshots(snapshots)
        return snapshots

    async def evaluate_symbol_surprise(
        self, symbol: str, fiscal_period: str | None = None
    ) -> EarningsSurpriseDTO | None:
        """查詢或即時評估標的之最新財務預期差。"""
        sym_upper = symbol.strip().upper()
        cached = await asyncio.to_thread(get_latest_earnings_surprise, sym_upper)
        if cached:
            return cached

        # 若無快取，向提供者查詢
        consensus = await self._consensus_provider.get_consensus(
            sym_upper, fiscal_period
        )
        if not consensus:
            return None

        result = evaluate_earnings_surprise(
            actual_eps=consensus.actual_eps,
            consensus_eps=consensus.consensus_eps,
            actual_revenue=consensus.actual_revenue,
            consensus_revenue=consensus.consensus_revenue,
        )

        session_str: FilingSession | Literal["UNKNOWN"] = (
            cast(FilingSession, consensus.session)
            if consensus.session in ("BMO", "AMC")
            else "UNKNOWN"
        )
        status_val: EarningsSurpriseStatus = (
            "PROCESSED" if result.composite_score is not None else "PENDING"
        )
        surprise_dto = EarningsSurpriseDTO(
            symbol=sym_upper,
            fiscal_period=consensus.fiscal_period,
            actual_eps=consensus.actual_eps,
            consensus_eps=consensus.consensus_eps,
            eps_surprise_pct=result.eps_surprise_pct,
            actual_revenue=consensus.actual_revenue,
            consensus_revenue=consensus.consensus_revenue,
            revenue_surprise_pct=result.revenue_surprise_pct,
            composite_score=result.composite_score,
            session=session_str,
            eps_basis="VENDOR_ADJUSTED",
            status=status_val,
        )
        await save_earnings_surprise(surprise_dto)
        return surprise_dto
