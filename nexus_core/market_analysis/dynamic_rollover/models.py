from enum import Enum

from pydantic import BaseModel, Field


class RolloverScenario(str, Enum):
    """動態轉倉引擎四大情境的明確識別碼，供 embed 呈現層做顏色/危險等級判斷，
    避免依賴呼叫端自由文字 rollover_type 的子字串比對（該作法曾導致最危險的
    MARGIN_DEFENSE 警報無法正確標紅，詳見 rollover_embeds.py）。"""

    OPPORTUNITY_COST = "OPPORTUNITY_COST"
    SATELLITE_REBALANCE = "SATELLITE_REBALANCE"
    MARGIN_DEFENSE = "MARGIN_DEFENSE"
    FUNDAMENTAL_BROKEN = "FUNDAMENTAL_BROKEN"


class FundamentalThesisResult(BaseModel):
    # 讓模型先進行思考與文字輸出
    reasoning: str = Field(description="Step-by-step reasoning in Traditional Chinese")
    # 思考完後再給出最終判斷
    is_broken: bool = Field(
        description="True if structural thesis is broken, False if just macro/temporary"
    )
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")
