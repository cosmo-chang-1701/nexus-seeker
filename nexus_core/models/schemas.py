from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WatchlistLegAction: TypeAlias = Literal["BUY", "SELL"]
WatchlistOptionType: TypeAlias = Literal["CALL", "PUT"]
WatchlistPremiumType: TypeAlias = Literal["debit", "credit"]
WatchlistRiskMode: TypeAlias = Literal[
    "normal", "macro-guard", "earnings-guard", "event-lock"
]


class EnhancedWatchlistMetrics(BaseModel):
    """結合技術位階、期權情緒與 NRO 欄位的 watchlist 監控模型。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    current_price: float = Field(gt=0.0)

    buy_zone_status: str = Field(min_length=1)
    buy_price_phase1: float = Field(gt=0.0)
    buy_price_phase2: float = Field(gt=0.0)
    buy_price_phase3: float = Field(gt=0.0)

    sell_zone_status: str = Field(min_length=1)
    sell_price_phase1: float = Field(gt=0.0)
    sell_price_phase2: float = Field(gt=0.0)
    sell_price_phase3: float = Field(gt=0.0)

    pe_ratio: float | None = Field(default=None, gt=0.0)
    pe_outlier_warning: str | None = Field(default=None)
    rsi_14: float = Field(default=50.0, ge=0.0, le=100.0)
    atr_14: float = Field(default=0.01, gt=0.0)
    beta: float = Field(ge=-5.0, le=5.0)
    ma20: float = Field(default=1.0, gt=0.0)
    ma50: float = Field(default=1.0, gt=0.0)
    ma200: float = Field(default=1.0, gt=0.0)
    bias_ma20: float = 0.0

    iv_rank: float | None = Field(default=None, ge=0.0, le=100.0)
    iv_percentile: float | None = Field(default=None, ge=0.0, le=100.0)

    # Option Skew = IV(OTM Put) - IV(OTM Call) in percentage points
    option_skew: float | None = None
    skew_percentile: float | None = Field(default=None, ge=0.0, le=100.0)
    option_skew_state: str = Field(min_length=1)

    # Put/Call Ratio (volume-based), used for skew consistency checks
    pcr: float | None = Field(default=None, ge=0.0)

    volume_poc: float = Field(gt=0.0)
    gex_max_put_wall: float | None = Field(default=None)
    vanna_sensitivity: float | None = None
    relative_strength_spy: float
    iv_source: str = "LIVE_IV"
    is_premarket: bool = False
    volume_pcr: float | None = None
    oi_pcr: float | None = None
    has_earnings_event: bool = False
    has_macro_event: bool = False
    iv_term_structure_status: str | None = None
    term_structure_ratio: float | None = None
    squeeze_status: bool | None = None
    squeeze_momentum: float | None = None
    squeeze_direction: str | None = None

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.upper()

    @field_validator("exchange")
    @classmethod
    def _normalize_exchange(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _validate_levels(self) -> "EnhancedWatchlistMetrics":
        if not (
            self.buy_price_phase1 >= self.buy_price_phase2 >= self.buy_price_phase3
        ):
            raise ValueError("buy phases must be ordered phase1 >= phase2 >= phase3")
        if not (
            self.sell_price_phase1 <= self.sell_price_phase2 <= self.sell_price_phase3
        ):
            raise ValueError("sell phases must be ordered phase1 <= phase2 <= phase3")

        self.bias_ma20 = 0.0
        return self

    @property
    def distance_to_absolute_support(self) -> float:
        support = (
            self.gex_max_put_wall
            if self.gex_max_put_wall is not None
            else self.buy_price_phase3
        )
        support = min(self.buy_price_phase3, support)
        return (self.current_price - support) / self.current_price


class WatchlistTacticalPlan(BaseModel):
    """Watchlist tactical routing output consumed by CLI and Discord reporting."""

    model_config = ConfigDict(extra="forbid")

    scenario: Literal["premium-harvest", "hard-hedge", "wait"]
    sddm_route: str = Field(min_length=1)
    action_guideline: str = Field(min_length=1)
    dynamic_grid_step: float = Field(ge=0.0)
    hidden_delta_risk: float = 0.0
    hedge_instruction: str | None = None
    hedge_allocation_shares: int = 0
    alert_level: Literal["green", "yellow", "red"] = "green"


class WatchlistOptionLeg(BaseModel):
    """Single options leg selected for the watchlist heartbeat."""

    model_config = ConfigDict(extra="forbid")

    action: WatchlistLegAction
    opt_type: WatchlistOptionType
    strike: float = Field(gt=0.0)
    expiry: str = Field(min_length=1)
    mid_price: float = Field(ge=0.0)


class WatchlistOptionPlan(BaseModel):
    """Executable options plan attached to a watchlist heartbeat."""

    model_config = ConfigDict(extra="forbid")

    strategy_name: str = Field(min_length=1)
    premium_type: WatchlistPremiumType
    estimated_net_premium: float = Field(ge=0.0)
    suggested_contracts: int = Field(ge=1)
    max_risk_amount: float = Field(ge=0.0)
    rationale: str = Field(min_length=1)
    stock_action: str = Field(min_length=1)
    legs: list[WatchlistOptionLeg] = Field(min_length=1)


class WatchlistEventContext(BaseModel):
    """Upcoming earnings / macro events affecting watchlist execution."""

    model_config = ConfigDict(extra="forbid")

    earnings_date: str | None = None
    earnings_tte_hours: float | None = None
    macro_event: str | None = None
    macro_event_time: str | None = None
    macro_tte_hours: float | None = None
    risk_mode: WatchlistRiskMode = "normal"
    summary: str = Field(min_length=1)


class WatchlistEvaluation(BaseModel):
    """Structured watchlist monitoring snapshot."""

    model_config = ConfigDict(extra="forbid")

    metrics: EnhancedWatchlistMetrics
    tactical: WatchlistTacticalPlan
    event_context: WatchlistEventContext
    symbol_gex: dict | None = Field(default=None)
