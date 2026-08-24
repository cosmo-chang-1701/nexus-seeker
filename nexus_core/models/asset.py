from typing import Any
from enum import Enum
from typing import Optional, Dict
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
    model_config = ConfigDict(extra="ignore")
    pass


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
    # 核心/衛星再平衡引擎 (Dynamic Rollover Scenario 3) 所需的資產分類與配置上限，
    # 透過 /edit_holding 由使用者手動設定；未設定時由引擎依 CORE_DEFENSE_ETF_SYMBOLS
    # 白名單與預設值 (CORE=100%, SATELLITE=30%) 自動推斷，此處保持 Optional。
    asset_class: Optional[str] = None  # "CORE" | "SATELLITE"
    max_allocation_pct: Optional[float] = None  # 0.0 - 1.0
    target_allocation_pct: Optional[float] = None  # 0.0 - 1.0
    # 核心資金部署引擎 (Dynamic Rollover Scenario 5) 判定超額配置應優先防禦轉入
    # BOXX 而非投入候選標的的閾值，透過 /edit_holding 由使用者手動設定；未設定
    # 時由引擎依 suggest_boxx_allocation_pct() 依當前總經數據自動評估建議值。
    boxx_allocation_pct: Optional[float] = None  # 0.0 - 1.0
    # 建倉日期 (YYYY-MM-DD)，供動態轉倉引擎的稅務提醒粗估長/短期資本利得稅率
    # 區間。單一日期為簡化估計，非完整多批次 (Lot-based FIFO) 成本基礎追蹤。
    acquired_at: Optional[str] = None

    @field_validator("weighted_delta", mode="before")
    @classmethod
    def default_weighted_delta(cls, v: Any):  # type: ignore
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
    tags: Optional[str] = None
    last_scan_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def get_metadata_model(self) -> Any:
        if self.context_type == ContextType.WATCH:
            return WatchMetadata(**self.metadata)
        elif self.context_type == ContextType.TRADE:
            return TradeMetadata(**self.metadata)
        elif self.context_type == ContextType.HOLDING:
            return HoldingMetadata(**self.metadata)
        return None
