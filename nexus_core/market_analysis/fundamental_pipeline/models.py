"""基本面分析管線 (Fundamental Event Pipeline) 核心資料模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LiquidityRegime = Literal["EASY", "NEUTRAL", "TIGHT", "UNKNOWN"]


@dataclass(frozen=True)
class LiquidityReading:
    """央行淨流動性與體制狀態讀數。"""

    trading_date: date
    nfci: float | None
    anfci: float | None
    net_liquidity_bn: float | None
    net_liquidity_chg_13w_pct: float | None
    reserves_chg_13w_pct: float | None
    us10y: float | None
    regime: LiquidityRegime
    equity_risk_premium: float | None


@dataclass(frozen=True)
class MacroSurpriseReading:
    """宏觀數據發布預期差標準化讀數。"""

    event_key: str
    release_time_utc: str
    actual: float
    forecast: float
    raw_diff: float
    z_score: float | None
    growth_sign: int  # 1: 正向經濟增長指標, -1: 反向/通膨/緊縮指標


# ============================================================================
# PR2 SEC 申報與治理審查資料模型
# ============================================================================

FilingSession = Literal["BMO", "RTH", "AMC", "OVERNIGHT"]
GovernanceSeverity = Literal["INFO", "REVIEW", "HIGH", "CRITICAL"]
InsiderSignalVerdict = Literal["CLUSTER_BUY", "HEAVY_INSIDER_SALE", "NEUTRAL"]


@dataclass(frozen=True)
class FilingCursorRecord:
    """SEC 申報追蹤游標記錄。"""

    symbol: str
    cik: str
    last_accepted_at: str
    last_accession: str
    updated_at: str = ""


@dataclass(frozen=True)
class FilingEventRecord:
    """SEC 申報事件記錄。"""

    accession: str
    symbol: str
    form: str
    items: str | None
    accepted_at: str
    session: FilingSession
    primary_doc_url: str | None = None
    routes_json: str | None = None
    is_backfill: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class InsiderTxRecord:
    """內部人交易明細記錄。"""

    accession: str
    line_no: int
    symbol: str
    owner_name: str
    owner_role: str = "OTHER"
    is_c_suite: bool = False
    tx_date: str = ""
    tx_code: str = "UNKNOWN"
    shares: float = 0.0
    price: float = 0.0
    acquired_disposed: str = "D"
    shares_after: float = 0.0
    is_10b5_1: bool = False
    is_backfill: bool = False
    created_at: str = ""


InsiderTransactionDTO = InsiderTxRecord


@dataclass(frozen=True)
class GovernanceFlagRecord:
    """公司治理審查旗標記錄。"""

    symbol: str
    source_accession: str
    flag_kind: str
    severity: GovernanceSeverity
    detail_json: str | None = None
    expires_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class InsiderSignalSummary:
    """內部人交易訊號聚合評估。"""

    symbol: str
    as_of_date: str
    window_days: int
    cluster_buy_count: int
    c_suite_buy_count: int
    total_net_bought_shares: float
    total_net_bought_usd: float
    cluster_sale_count: int
    total_net_sold_usd: float
    verdict: InsiderSignalVerdict
    summary_text: str


@dataclass(frozen=True)
class ActivistSignal:
    """激進投資人 / 13D 訊號。"""

    symbol: str
    accession: str
    investor_name: str
    ownership_pct: float
    is_delayed_filing: bool
    key_intents: list[str]
    summary_text: str


@dataclass(frozen=True)
class GovernanceStatus:
    """標的治理審查狀態。"""

    symbol: str
    is_clean: bool
    max_severity: GovernanceSeverity | None
    active_flags: list[GovernanceFlagRecord]


# ============================================================================
# PR3 財務預期差、分析師共識快照與前瞻指引資料模型
# ============================================================================

EstimateHorizon = Literal["0q", "+1q", "0y", "+1y"]
EpsBasis = Literal["VENDOR_ADJUSTED", "GAAP_EX99"]
EarningsSurpriseStatus = Literal["PENDING", "PROCESSED", "FAILED"]
GuidanceVerdict = Literal["RAISED", "LOWERED", "MAINTAINED", "UNKNOWN"]
MarginDirection = Literal["EXPANDING", "COMPRESSING", "FLAT", "UNKNOWN"]


@dataclass(frozen=True)
class EarningsSurpriseDTO:
    """財務預期差與綜合評分資料結構。"""

    symbol: str
    fiscal_period: str
    actual_eps: float | None = None
    consensus_eps: float | None = None
    eps_surprise_pct: float | None = None
    actual_revenue: float | None = None
    consensus_revenue: float | None = None
    revenue_surprise_pct: float | None = None
    whisper_eps: float | None = None
    composite_score: float | None = None
    session: FilingSession | Literal["UNKNOWN"] = "UNKNOWN"
    eps_basis: EpsBasis = "VENDOR_ADJUSTED"
    status: EarningsSurpriseStatus = "PROCESSED"
    created_at: str = ""


EarningsSurpriseRecord = EarningsSurpriseDTO


@dataclass(frozen=True)
class EPSEstimateSnapshotRecord:
    """分析師每股盈餘預估共識快照記錄。"""

    symbol: str
    snapshot_date: str
    horizon: EstimateHorizon
    source: str
    eps_mean: float
    eps_high: float | None = None
    eps_low: float | None = None
    analyst_count: int | None = None
    created_at: str = ""


EPSEstimateSnapshotDTO = EPSEstimateSnapshotRecord


class MarginGuidance(BaseModel):
    """毛利率 / 營業利益率指引結構。"""

    model_config = ConfigDict(frozen=True)

    metric_name: str = Field(description="例如 Gross Margin 或 Operating Margin")
    guidance_midpoint_pct: float | None = Field(
        default=None, description="指引中點百分比數值，若無明確數值填 None"
    )
    direction: MarginDirection = Field(
        default="UNKNOWN",
        description="EXPANDING (擴張), COMPRESSING (壓縮), FLAT (持平), UNKNOWN (未知)",
    )


class ToneMetric(BaseModel):
    """管理層態度語意評分項目。"""

    model_config = ConfigDict(frozen=True)

    score: int = Field(
        ge=-2,
        le=2,
        description="-2 代表極度惡化/防禦，0 代表中性，+2 代表極具定價自信/擴張",
    )
    quote_snippet: str = Field(
        description="支持評分的管理層原文直接引用摘錄（限 200 字以內）"
    )


class GuidanceExtraction(BaseModel):
    """嚴格支援 OpenAI beta.chat.completions.parse 的結構化指引擷取定義。"""

    model_config = ConfigDict(frozen=True)

    symbol: str
    fiscal_period: str
    revenue_guidance_midpoint_usd: float | None = Field(
        default=None, description="營收指引中點金額 (美元)，無指引填 None"
    )
    eps_guidance_midpoint_usd: float | None = Field(
        default=None, description="EPS 指引中點金額 (美元)，無指引填 None"
    )
    margin_guidance: list[MarginGuidance] = Field(default_factory=list)

    @field_validator("margin_guidance", mode="before")
    @classmethod
    def coerce_margin_guidance(cls, v: Any) -> Any:
        if v is None:
            empty_list: list[Any] = []
            return empty_list
        return v

    backlog_tone: ToneMetric
    pricing_power_tone: ToneMetric
    supply_chain_tone: ToneMetric
    defensive_posture_tone: ToneMetric
    reasoning_traditional_chinese: str = Field(description="100% 繁體中文質化摘要論述")


@dataclass(frozen=True)
class GuidanceExtractionDTO:
    """管理層指引擷取結果持久化資料結構。"""

    symbol: str
    fiscal_period: str
    source_accession: str
    model_version: str
    confidence_score: float
    tone_delta_score: float
    data_json: str
    created_at: str = ""


GuidanceExtractionRecord = GuidanceExtractionDTO


@dataclass(frozen=True)
class EarningsSurpriseResult:
    """財務預期差綜合計算結果。"""

    eps_surprise_pct: float | None
    revenue_surprise_pct: float | None
    whisper_surprise_pct: float | None
    composite_score: float | None
    small_base: bool


@dataclass(frozen=True)
class GuidanceDeltaSummary:
    """前瞻指引邊際變動與態度摘要。"""

    tone_score: float
    tone_delta: float
    revenue_guidance_delta_pct: float | None
    eps_guidance_delta_pct: float | None
    margin_trend: str
    verdict: GuidanceVerdict
    summary_text: str


# ============================================================================
# PR5 分析師修正動能、兩段式 DCF / Comps 估值與次日觀察名單模型
# ============================================================================

WatchCandidateStatus = Literal["CANDIDATE", "WATCH", "EXCLUDED"]


@dataclass(frozen=True)
class RevisionScoreRecord:
    """分析師修正動能評分記錄。"""

    symbol: str
    trading_date: str
    score_30d: float
    breadth_ratio: float
    is_pead_aligned: bool
    detail_json: str
    created_at: str = ""


RevisionScoreDTO = RevisionScoreRecord


@dataclass(frozen=True)
class FairValueRecord:
    """內在公允價值與安全邊際記錄。"""

    symbol: str
    trading_date: str
    dcf_value: float | None
    comps_value: float | None
    fair_value: float
    margin_of_safety: float
    discount_rate: float
    equity_risk_premium: float
    flags_json: str
    created_at: str = ""


FairValueDTO = FairValueRecord


@dataclass(frozen=True)
class WatchCandidateRecord:
    """基本面次日候選觀察名單記錄。"""

    trading_date: str
    symbol: str
    rank: int
    status: WatchCandidateStatus
    reasons_json: str
    excluded_reason: str | None = None
    created_at: str = ""


WatchCandidateDTO = WatchCandidateRecord


@dataclass(frozen=True)
class DCFInputs:
    """兩段式現金流折現 (2-Stage DCF) 計算輸入。"""

    fcf_per_share: float
    growth_rate_1y: float
    cost_of_equity: float
    perpetual_growth_rate: float = 0.025


@dataclass(frozen=True)
class DCFResult:
    """兩段式現金流折現計算結果。"""

    dcf_value: float | None
    is_valid: bool
    rejection_reason: str | None = None


@dataclass(frozen=True)
class CompsInputs:
    """流動性折讓同業乘數法計算輸入。"""

    forward_eps: float
    peer_pes: list[float]
    nfci: float


@dataclass(frozen=True)
class CompsResult:
    """同業乘數法計算結果。"""

    comps_value: float | None
    median_pe: float | None
    liquidity_penalty_factor: float
    is_valid: bool
    rejection_reason: str | None = None


@dataclass(frozen=True)
class FairValueResult:
    """公允價值綜合計算與安全邊際結果。"""

    fair_value: float | None
    margin_of_safety: float | None
    dcf_value: float | None
    comps_value: float | None
    discount_rate: float
    equity_risk_premium: float
    is_deep_value: bool
    flags: list[str]
    method: str


@dataclass(frozen=True)
class RevisionMomentumResult:
    """分析師修正動能綜合計算結果。"""

    score_30d: float
    breadth_ratio: float
    is_pead_aligned: bool
    slopes: dict[str, float]
    up_count: int
    down_count: int
    details: dict[str, Any]
