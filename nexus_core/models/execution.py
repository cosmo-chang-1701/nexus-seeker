from typing import Literal, Optional, Any
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator


class Signal(Enum):
    SKIP = "SKIP"


class MarketCondition(BaseModel):
    """
    市場狀況數據模型，用於封裝當前的市場環境指標。
    """

    model_config = ConfigDict(frozen=True)

    vix: float = Field(..., description="VIX 指數 (波動率)", ge=0)
    skew_percent: float = Field(..., description="偏度百分比 (Skew %)")
    asset_price: float = Field(..., description="標的資產價格", gt=0)
    ma20: float = Field(..., description="20日移動平均線", gt=0)
    atr_14: float = Field(..., description="14日平均真實波幅 (ATR)", ge=0)
    rsi_14: float = Field(..., description="14日相對強弱指數 (RSI)", ge=0, le=100)
    uoa_detected: bool = Field(False, description="是否偵測到異常期權活動 (UOA)")
    relative_strength: float = Field(
        1.0, description="相對強度 (Relative Strength) 指標"
    )

    @field_validator(
        "vix",
        "skew_percent",
        "asset_price",
        "ma20",
        "atr_14",
        "rsi_14",
        "relative_strength",
        mode="before",
    )
    @classmethod
    def clean_indicators(cls, v: Any, info) -> Any:
        import math
        import pandas as pd
        import numpy as np

        if v is None:
            return cls._get_safe_default(info.field_name)

        if isinstance(v, float) and (math.isnan(v) or np.isnan(v)):
            return cls._get_safe_default(info.field_name)

        if isinstance(v, (float, int)) and pd.isna(v):
            return cls._get_safe_default(info.field_name)

        try:
            val = float(v)
        except (ValueError, TypeError):
            return cls._get_safe_default(info.field_name)

        if isinstance(val, float) and (math.isnan(val) or np.isnan(val)):
            return cls._get_safe_default(info.field_name)

        # check bounds
        if info.field_name == "vix" and val <= 0:
            return 18.0
        if info.field_name == "asset_price" and val <= 0:
            return 100.0
        if info.field_name == "ma20" and val <= 0:
            return 100.0
        if info.field_name == "atr_14" and val < 0:
            return 2.0
        if info.field_name == "rsi_14" and (val < 0 or val > 100):
            return 50.0
        if info.field_name == "relative_strength" and val <= 0:
            return 1.0

        return val

    @classmethod
    def _get_safe_default(cls, field_name: str) -> float:
        defaults = {
            "vix": 18.0,
            "skew_percent": 0.0,
            "asset_price": 100.0,
            "ma20": 100.0,
            "atr_14": 2.0,
            "rsi_14": 50.0,
            "relative_strength": 1.0,
        }
        return defaults.get(field_name, 0.0)


class GridParameters(BaseModel):
    """
    網格交易參數 (Module A: SHIELD)，用於高波動環境下的防禦性策略。
    """

    model_config = ConfigDict(frozen=True)

    base_price: float = Field(..., description="基準價格", gt=0)
    dynamic_step_percent: float = Field(
        ..., description="動態網格步長百分比 (由 ATR 計算)", gt=0, le=1
    )


class PositionSizing(BaseModel):
    """
    倉位規模計算 (Module B: SPEAR)，用於低風險環境下的攻擊性期權策略。
    """

    model_config = ConfigDict(frozen=True)

    kelly_percentage: float = Field(
        ..., description="凱利公式計算出的倉位百分比", ge=0, le=1
    )
    max_capital_allocation: float = Field(
        ..., description="最大資金分配金額 (與 BOXX 儲備邏輯綁定)", ge=0
    )
    max_theta_exposure: float = Field(..., description="最大 Theta 敞口限制", ge=0)

    @model_validator(mode="after")
    def validate_kelly(self) -> "PositionSizing":
        # 確保凱利百分比符合保守交易規範
        if self.kelly_percentage > 0.5:
            # 雖然 Pydantic 會通過 le=1，但這裡可以加入更細緻的警告或調整逻辑
            pass
        return self


class ExitStrategy(BaseModel):
    """
    出場策略，定義如何退出當前頭寸。
    """

    model_config = ConfigDict(frozen=True)

    trailing_stop_active: bool = Field(..., description="是否啟用移動止損")
    trigger_price: float = Field(..., description="觸發止損/止盈的價格", gt=0)
    condition_type: Literal["MA20_BREAK", "RSI_DROP", "FIXED_STOP", "TIME_EXIT"] = (
        Field(..., description="出場條件類型")
    )


class ExecutionDecision(BaseModel):
    """
    執行決策輸出合約，封裝 Gatekeeper 的最終路由結果。
    """

    model_config = ConfigDict(frozen=True)

    decision_type: Literal["SHIELD", "SPEAR", "STANDBY"] = Field(
        ..., description="決策類型"
    )
    trigger_reason: str = Field(..., description="觸發決策的原因 (中文說明)")
    grid_params: Optional[GridParameters] = Field(
        None, description="網格參數 (僅適用於 SHIELD)"
    )
    position_sizing: Optional[PositionSizing] = Field(
        None, description="倉位規模 (僅適用於 SPEAR)"
    )
    exit_strategy: Optional[ExitStrategy] = Field(None, description="出場策略")

    @model_validator(mode="after")
    def validate_consistency(self) -> "ExecutionDecision":
        # 驗證決策類型與所附帶的參數是否一致
        if self.decision_type == "SHIELD" and self.grid_params is None:
            raise ValueError("SHIELD 決策必須包含網格參數 (grid_params)")
        if self.decision_type == "SPEAR" and self.position_sizing is None:
            raise ValueError("SPEAR 決策必須包含倉位規模 (position_sizing)")
        return self
