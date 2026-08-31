from enum import Enum
from typing import Optional, TypedDict

from pydantic import BaseModel, Field


class RolloverScenario(str, Enum):
    """動態轉倉引擎五大情境的明確識別碼，供 embed 呈現層做顏色/危險等級判斷，
    避免依賴呼叫端自由文字 rollover_type 的子字串比對（該作法曾導致最危險的
    MARGIN_DEFENSE 警報無法正確標紅，詳見 rollover_embeds.py）。"""

    OPPORTUNITY_COST = "OPPORTUNITY_COST"
    SATELLITE_REBALANCE = "SATELLITE_REBALANCE"
    MARGIN_DEFENSE = "MARGIN_DEFENSE"
    FUNDAMENTAL_BROKEN = "FUNDAMENTAL_BROKEN"
    CORE_DEPLOYMENT = "CORE_DEPLOYMENT"


class FundamentalThesisResult(BaseModel):
    # 讓模型先進行思考與文字輸出
    reasoning: str = Field(description="Step-by-step reasoning in Traditional Chinese")
    # 思考完後再給出最終判斷
    is_broken: bool = Field(
        description="True if structural thesis is broken, False if just macro/temporary"
    )
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")


class _RolloverInstructionRequired(TypedDict):
    symbol: str
    action: str
    sell_ratio: float
    target_core: str
    reason: str


class RolloverInstruction(_RolloverInstructionRequired, total=False):
    """四個情境驅動函式 (opportunity_cost.py / anti_washout.py /
    margin_defense.py / core_deployment.py) 共用的轉倉建議指令結構。

    刻意採用 TypedDict 而非 Pydantic BaseModel：唯一的下游消費端
    (cogs/trading/portfolio_monitor.py) 與 tests/unit/test_dynamic_rollover.py
    的既有斷言皆大量使用 `ins["key"]` / `ins.get("key")` dict 下標存取語法，
    BaseModel 預設不支援下標存取，強行改為 BaseModel 會需要同時重寫消費端與
    整份測試檔案的斷言方式，超出本次純型別標註重構的範圍。TypedDict 在執行期
    仍是一般 dict，對呼叫端與既有測試零影響，僅提供靜態型別檢查層級的保障。

    僅 symbol/action/sell_ratio/target_core/reason 五欄位在所有情境下皆會被
    portfolio_monitor.py 以 `ins["key"]`（而非 `.get`）存取，故列為必要欄位；
    其餘欄位各情境視需要選填。
    """

    suggested_strategy: str
    scenario: str
    is_manual_override_required: bool
    cash_impact: Optional[str]
    limit_price: Optional[float]
    trigger_condition_text: Optional[str]
    sell_action: str
    buy_action_label: Optional[str]
    strike: Optional[str]
    expiry: Optional[str]
    direction: Optional[str]
    is_covered_call_overlay: Optional[bool]
    # "OPTIONS" 或 "SPOT"：供 portfolio_monitor.py 組成 (symbol, instrument_type)
    # 複合去重鍵與每日 kv_cache dedup key，避免同一標的的現貨與期權部位互相
    # 誤判為同一筆已處理的建議。未提供時各消費端一律 fallback 為 "SPOT"。
    instrument_type: str
