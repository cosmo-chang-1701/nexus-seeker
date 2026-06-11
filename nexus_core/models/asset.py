from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator
from datetime import datetime


class ContextType(str, Enum):
    WATCH = "WATCH"
    TRADE = "TRADE"
    HOLDING = "HOLDING"


class HoldingType(str, Enum):
    PURE_STOCK_100X = "PURE_STOCK_100X"
    LEVERAGED_MARGIN = "LEVERAGED_MARGIN"
    COMPLEX_OPTIONS = "COMPLEX_OPTIONS"


class WatchMetadata(BaseModel):
    model_config = ConfigDict()
    use_llm: bool = True


class TradeMetadata(BaseModel):
    model_config = ConfigDict()
    opt_type: str  # 'call' or 'put'
    strike: float
    expiry: str  # YYYY-MM-DD
    entry_price: float
    quantity: int
    stock_cost: float = 0.0
    weighted_delta: float = 0.0
    theta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    vanna: float = 0.0
    category: str = "SPEC"


class HoldingMetadata(BaseModel):
    model_config = ConfigDict()
    quantity: float
    avg_cost: float
    weighted_delta: float = 0.0

    @field_validator("weighted_delta", mode="before")
    @classmethod
    def default_weighted_delta(cls, v):
        if v is None:
            return 0.0
        return v


class Asset(BaseModel):
    model_config = ConfigDict()
    id: Optional[int] = None
    user_id: int
    symbol: str
    context_type: ContextType
    risk_weight: float = 1.0  # Beta
    entry_price: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    last_scan_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def get_metadata_model(self):
        if self.context_type == ContextType.WATCH:
            return WatchMetadata(**self.metadata)
        elif self.context_type == ContextType.TRADE:
            return TradeMetadata(**self.metadata)
        elif self.context_type == ContextType.HOLDING:
            return HoldingMetadata(**self.metadata)
        return None
