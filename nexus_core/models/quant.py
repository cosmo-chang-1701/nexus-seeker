from pydantic import BaseModel, Field, ConfigDict
from typing import List, Literal
from enum import Enum


class TradeSide(Enum):
    BTO = "BTO"
    STO = "STO"
    BTC = "BTC"
    STC = "STC"


class AssetType(Enum):
    STOCK = "STOCK"
    OPTION = "OPTION"
    CASH = "CASH"


class PositionRisk(BaseModel):
    """Model for individual position risk metrics."""

    model_config = ConfigDict()

    symbol: str
    quantity: float
    weighted_delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    vanna: float = 0.0
    margin_used: float = 0.0
    pnl_unrealized: float = 0.0
    pnl_pct: float = 0.0


class OptimizationResult(BaseModel):
    """Result of NRO position size optimization."""

    model_config = ConfigDict()

    suggested_contracts: int
    exposure_pct: float
    suggested_hedge_spy: float = 0.0
    risk_score: float = 0.0
    warnings: List[str] = Field(default_factory=list)


class MacroRiskMetrics(BaseModel):
    """Aggregated macro risk metrics for the entire portfolio."""

    model_config = ConfigDict()

    net_exposure_dollars: float
    exposure_pct: float
    total_beta_delta: float
    gamma_threshold: float
    theta_yield: float
    portfolio_heat: float
    portfolio_heat_limit: float
    total_gamma: float
    total_theta: float
    total_margin_used: float
    total_vega: float = 0.0
    total_vanna: float = 0.0
    vix_tier_name: str = "N/A"
    vix_scale_multiplier: float = 1.0


class PortfolioSummary(BaseModel):
    """Aggregate portfolio metrics."""

    model_config = ConfigDict()

    total_capital: float
    total_weighted_delta: float
    total_theta: float
    total_gamma: float
    total_vanna: float = 0.0
    total_unrealized_pnl: float
    exposure_pct: float
    margin_utilization: float

    @property
    def is_over_exposed(self) -> bool:
        return abs(self.exposure_pct) > 30.0


class IVMetrics(BaseModel):
    """Model for symbol options implied volatility metrics."""

    model_config = ConfigDict()

    symbol: str
    current_iv: float | None = None  # fraction, e.g. 0.585 for 58.5%
    iv_rank: float | None = None  # 0.0 to 100.0
    iv_percentile: float | None = None  # 0.0 to 100.0
    expected_move_weekly: float | None = None
    iv_status: Literal["Low", "Normal", "High", "Extreme"] | None = "Normal"
    is_premarket: bool = False
    iv_source: Literal["LIVE_IV", "STORED_IV", "HV_PROXY", "UNAVAILABLE"] = "LIVE_IV"
    reference_spot_price: float | None = None
    has_event_loading_applied: bool = False
