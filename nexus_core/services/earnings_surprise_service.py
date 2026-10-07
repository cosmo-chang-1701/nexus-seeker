"""財務預期差與管理層指引協調服務 (EarningsSurpriseService)。

職責：
1. 自 8-K Item 2.02 申報目錄（`{accession}-index-headers.html`）定位 Exhibit 99.1 新聞稿，
   去除 HTML / iXBRL 標記後截斷，再交由 LLM 擷取結構化前瞻指引與語意態度。
2. 財季一律由 SEC 受理日對齊 Finnhub 財報日曆 / 歷史業績推導（`YYYY-Qn`），LLM 輸出之
   期別僅作參考；推導不出可靠財季時不寫入任何記錄。
3. 整合 ConsensusProvider (Finnhub 等免費源) 與 WhisperProvider，計算雙維預期差與綜合分數。
4. 維護分析師 EPS 預估快照 (eps_estimate_snapshot)。
5. 遵循 1GB–2GB VPS 記憶體守衛 (is_memory_safe) 與 Single-Writer 持久化架構。

接線狀態：本服務目前**沒有 production 排程或呼叫端**（docs/valuation_pricing/05 §0）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import config
from database.fundamental_pipeline import (
    get_earnings_surprise,
    get_prior_guidance_extraction,
    save_earnings_surprise,
    save_eps_estimate_snapshots,
    save_guidance_extraction,
)
from market_analysis.fundamental_pipeline.earnings_surprise import (
    evaluate_earnings_surprise,
)
from market_analysis.fundamental_pipeline.fiscal_period import (
    normalize_fiscal_period,
)
from market_analysis.fundamental_pipeline.guidance_delta import (
    calculate_extraction_confidence,
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
from market_analysis.fundamental_pipeline.press_release import (
    evaluate_tone_grounding,
    html_to_plain_text,
    select_press_release_document,
    truncate_for_llm,
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

LLM_GUIDANCE_MAX_TOKENS: int = 2400  # 四維引文 + 繁中論述 + 利潤率清單所需輸出長度


def _accepted_date_et(accepted_at: str) -> date | None:
    """將 sec_filing_event.accepted_at（美東 ISO 8601）解析為美東日期。"""
    try:
        dt = datetime.fromisoformat(accepted_at.strip())
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_ET_ZONE)
    return dt.date()


def _has_any_value(dto: EarningsSurpriseDTO) -> bool:
    return any(
        v is not None
        for v in (
            dto.actual_eps,
            dto.consensus_eps,
            dto.actual_revenue,
            dto.consensus_revenue,
            dto.whisper_eps,
        )
    )


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
        """處理單一 SEC 申報事件；若為 8-K Item 2.02 (EARNINGS) 則執行預期差分析與指引擷取。

        推導不出可靠財季（受理日無法對齊 Finnhub 財報條目）時回傳 None 且不寫入任何記錄。
        """
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

        # 1. 以 SEC 受理日對齊 Finnhub 財報條目，推導權威財季 (YYYY-Qn)
        as_of = _accepted_date_et(event.accepted_at)
        if as_of is None:
            logger.warning(
                f"[EarningsSurpriseService] 受理時間無法解析，放棄處理 ({sym_upper}, {event.accession}): "
                f"{event.accepted_at!r}"
            )
            return None

        consensus: ConsensusData | None = None
        try:
            consensus = await self._consensus_provider.get_consensus(
                sym_upper, None, as_of=as_of
            )
        except Exception as e:
            logger.warning(
                f"[EarningsSurpriseService] 共識資料讀取異常 ({sym_upper}): {e}"
            )

        fiscal_period = (
            normalize_fiscal_period(consensus.fiscal_period) if consensus else None
        )
        if fiscal_period is None:
            logger.warning(
                f"[EarningsSurpriseService] {sym_upper} 無法由受理日 {as_of} 對齊財報日曆推導可靠財季，"
                f"不寫入預期差與指引 ({event.accession})"
            )
            return None

        # 2. 結構化指引擷取 (配置 LLM 且記憶體安全時)
        if config.API_KEY and is_memory_safe():
            await self._process_guidance(event, sym_upper, fiscal_period)

        # 3. 獲取買方耳語預期 (Whisper)
        whisper_val: float | None = None
        try:
            whisper_val = await self._whisper_provider.get_whisper(
                sym_upper, fiscal_period
            )
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] Whisper 讀取異常: {e}")

        # 4. 執行財務預期差綜合評估並入庫
        session_str: FilingSession | Literal["UNKNOWN"] = (
            event.session if event.session in ("BMO", "AMC") else "UNKNOWN"
        )
        surprise_dto = self._build_surprise_dto(
            sym_upper, fiscal_period, consensus, whisper_val, session_str
        )
        saved = await self._persist_surprise(surprise_dto)

        # 5. 維護並更新分析師預估快照 (0q, +1q, 0y, +1y)
        try:
            snapshots = await self._consensus_provider.get_estimate_snapshots(sym_upper)
            if snapshots:
                await save_eps_estimate_snapshots(snapshots)
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] 快照更新失敗: {e}")

        return surprise_dto if saved else None

    @staticmethod
    def _build_surprise_dto(
        sym_upper: str,
        fiscal_period: str,
        consensus: ConsensusData | None,
        whisper_val: float | None,
        session_str: FilingSession | Literal["UNKNOWN"],
    ) -> EarningsSurpriseDTO:
        actual_eps = consensus.actual_eps if consensus else None
        consensus_eps = consensus.consensus_eps if consensus else None
        actual_rev = consensus.actual_revenue if consensus else None
        consensus_rev = consensus.consensus_revenue if consensus else None

        surprise_result = evaluate_earnings_surprise(
            actual_eps=actual_eps,
            consensus_eps=consensus_eps,
            actual_revenue=actual_rev,
            consensus_revenue=consensus_rev,
            whisper_eps=whisper_val,
        )
        status_val: EarningsSurpriseStatus = (
            "PROCESSED" if surprise_result.composite_score is not None else "PENDING"
        )
        return EarningsSurpriseDTO(
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

    async def _persist_surprise(self, dto: EarningsSurpriseDTO) -> bool:
        """寫入預期差記錄；全空記錄不寫，PENDING 不得覆蓋既有 PROCESSED。回傳是否已寫入。"""
        if not _has_any_value(dto):
            logger.info(
                f"[EarningsSurpriseService] {dto.symbol} {dto.fiscal_period} 無任何共識 / 實際值，不寫入"
            )
            return False
        if dto.status != "PROCESSED":
            existing = await asyncio.to_thread(
                get_earnings_surprise, dto.symbol, dto.fiscal_period
            )
            if existing is not None and existing.status == "PROCESSED":
                logger.info(
                    f"[EarningsSurpriseService] {dto.symbol} {dto.fiscal_period} 已有 PROCESSED 記錄，"
                    "不以 PENDING 覆蓋"
                )
                return False
        await save_earnings_surprise(dto)
        return True

    async def _fetch_press_release_text(
        self, event: FilingEventRecord, sym_upper: str
    ) -> str:
        """定位並下載 Exhibit 99.1 新聞稿，回傳清洗後之純文字（未截斷）；找不到時回傳空字串。"""
        sec_client = await self.get_sec_client()
        if event.primary_doc_url:
            filing_dir = event.primary_doc_url.rsplit("/", 1)[0]
        else:
            cik = await sec_client.get_cik(sym_upper)
            if not cik:
                logger.warning(
                    f"[EarningsSurpriseService] 無法取得 {sym_upper} CIK，跳過新聞稿擷取"
                )
                return ""
            filing_dir = SecEdgarClient.filing_directory_url(cik, event.accession)

        documents = await sec_client.fetch_filing_documents(filing_dir, event.accession)
        press_doc = select_press_release_document(documents)
        if press_doc is None:
            logger.warning(
                f"[EarningsSurpriseService] {event.accession} 申報目錄中找不到 EX-99 新聞稿附件，"
                f"跳過指引擷取（文件類型: {[d.doc_type for d in documents]}）"
            )
            return ""
        raw = await sec_client.fetch_document_text(f"{filing_dir}/{press_doc.filename}")
        return html_to_plain_text(raw)

    async def _process_guidance(
        self, event: FilingEventRecord, sym_upper: str, fiscal_period: str
    ) -> GuidanceExtractionDTO | None:
        """擷取、溯源驗證並寫入管理層指引；任何環節失敗只記 log，不影響預期差計算。"""
        try:
            full_text = await self._fetch_press_release_text(event, sym_upper)
        except Exception as e:
            logger.warning(
                f"[EarningsSurpriseService] 拉取 Exhibit 99.1 新聞稿失敗 ({event.accession}): {e}"
            )
            return None
        if not full_text:
            return None
        snippet = truncate_for_llm(full_text)

        try:
            extracted = await self._extract_guidance_via_llm(
                symbol=sym_upper, accession=event.accession, doc_text=snippet
            )
        except Exception as e:
            logger.warning(
                f"[EarningsSurpriseService] LLM 解析指引失敗 ({sym_upper}, {event.accession}): {e}"
            )
            return None
        if extracted is None:
            return None

        llm_period = normalize_fiscal_period(extracted.fiscal_period)
        if llm_period is not None and llm_period != fiscal_period:
            logger.warning(
                f"[EarningsSurpriseService] {sym_upper} LLM 期別 {extracted.fiscal_period!r} 與推導財季 "
                f"{fiscal_period} 不一致，以推導財季為準"
            )

        grounding = evaluate_tone_grounding(extracted, snippet)
        if not grounding.is_acceptable:
            logger.warning(
                f"[EarningsSurpriseService] {sym_upper} {event.accession} 態度評分缺乏原文依據 "
                f"({', '.join(grounding.ungrounded_directional)})，放棄本筆指引擷取，不寫入"
            )
            return None

        tone_score = calculate_management_tone_score(extracted)
        prior_tone: float | None = None
        try:
            prior_record = await asyncio.to_thread(
                get_prior_guidance_extraction, sym_upper, fiscal_period, event.accession
            )
            if prior_record is not None and prior_record.data_json:
                prior_g = GuidanceExtraction.model_validate_json(prior_record.data_json)
                prior_tone = calculate_management_tone_score(prior_g)
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] 前期指引讀取 / 解析失敗: {e}")
            prior_tone = None

        tone_delta = calculate_tone_delta(tone_score, prior_tone)
        dto = GuidanceExtractionDTO(
            symbol=sym_upper,
            fiscal_period=fiscal_period,
            source_accession=event.accession,
            model_version=str(config.LLM_MODEL_NAME or "unknown"),
            confidence_score=calculate_extraction_confidence(
                extracted, len(grounding.grounded)
            ),
            # schema NOT NULL：無前期可比時寫 0.0，呈現層以前期記錄重算
            tone_delta_score=tone_delta if tone_delta is not None else 0.0,
            data_json=extracted.model_dump_json(),
        )
        try:
            await save_guidance_extraction(dto)
        except Exception as e:
            logger.warning(f"[EarningsSurpriseService] 指引入庫失敗 ({sym_upper}): {e}")
            return None
        logger.info(
            f"[EarningsSurpriseService] {sym_upper} {fiscal_period} 結構化指引入庫完成 "
            f"(Tone: {tone_score:+.1f}, 信心: {dto.confidence_score:.2f})"
        )
        return dto

    async def _extract_guidance_via_llm(
        self, symbol: str, accession: str, doc_text: str
    ) -> GuidanceExtraction | None:
        """透過 OpenAI beta.chat.completions.parse 提取結構化前瞻指引。

        doc_text 須為已清洗並截斷之 Exhibit 99.1 純文字。
        """
        system_prompt = (
            "你是美股頂級買方基本面研究員。請自所提供的 8-K Exhibit 99.1 財報新聞稿文本中，"
            "嚴格抽取管理層對未來期別的結構化財務前瞻指引 (Guidance) 與語意態度。\n"
            "1. 若有具體數值 (營收/EPS 指引區間)，請換算為中點，並填完整美元數值"
            "（例如 94.5 billion 填 94500000000），同時在 guidance_target_period 註明指引所針對之期別。\n"
            "2. 毛利率/營益率請識別擴張 (EXPANDING)、壓縮 (COMPRESSING) 或持平 (FLAT)。\n"
            "3. 態度指標 (score) 介於 -2 至 +2，quote_snippet 必須是新聞稿英文原文的逐字摘錄；"
            "原文沒有相關論述時 score 填 0、quote_snippet 填空字串，不可編造。\n"
            "4. reasoning_traditional_chinese 必須使用 100% 繁體中文撰寫完整質化論述。\n"
            "5. 禁止幻想不存在的數字，若新聞稿未提供明確數值則填寫 None。"
        )

        user_prompt = (
            f"標的代號: {symbol}\n申報編號: {accession}\n\n新聞稿原文內容:\n{doc_text}"
        )

        completion = await llm_client.beta.chat.completions.parse(
            model=config.LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=GuidanceExtraction,
            temperature=0.0,
            max_tokens=LLM_GUIDANCE_MAX_TOKENS,
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
        """查詢或即時評估標的之財務預期差。

        只有「目標財季」已存在 status == PROCESSED 之記錄時才命中快取；PENDING 一律重查。
        未指定 fiscal_period 時，目標財季為 Finnhub 最近一筆已公布實際 EPS 之財季。
        """
        sym_upper = symbol.strip().upper()
        target = normalize_fiscal_period(fiscal_period) if fiscal_period else None
        if fiscal_period and target is None:
            logger.warning(
                f"[EarningsSurpriseService] 無法辨識之財季格式 ({sym_upper}): {fiscal_period!r}"
            )
            return None

        if target is not None:
            cached = await asyncio.to_thread(get_earnings_surprise, sym_upper, target)
            if cached is not None and cached.status == "PROCESSED":
                return cached

        consensus = await self._consensus_provider.get_consensus(sym_upper, target)
        if not consensus:
            return None
        period = normalize_fiscal_period(consensus.fiscal_period)
        if period is None or (target is not None and period != target):
            logger.warning(
                f"[EarningsSurpriseService] {sym_upper} 共識期別 {consensus.fiscal_period!r} "
                f"無法對齊目標財季 {target or '(最近已公布)'}，不寫入"
            )
            return None

        if target is None:
            cached = await asyncio.to_thread(get_earnings_surprise, sym_upper, period)
            if cached is not None and cached.status == "PROCESSED":
                return cached

        whisper_val: float | None = None
        try:
            whisper_val = await self._whisper_provider.get_whisper(sym_upper, period)
        except Exception as e:
            logger.debug(f"[EarningsSurpriseService] Whisper 讀取異常: {e}")

        session_str: FilingSession | Literal["UNKNOWN"] = (
            cast(FilingSession, consensus.session)
            if consensus.session in ("BMO", "AMC")
            else "UNKNOWN"
        )
        surprise_dto = self._build_surprise_dto(
            sym_upper, period, consensus, whisper_val, session_str
        )
        saved = await self._persist_surprise(surprise_dto)
        return surprise_dto if saved else None
