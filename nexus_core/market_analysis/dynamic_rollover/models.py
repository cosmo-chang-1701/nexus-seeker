from enum import Enum
from typing import Any, Dict, NamedTuple, Optional, TypedDict

from pydantic import BaseModel, Field


class RolloverScenario(str, Enum):
    """動態轉倉引擎六大情境的明確識別碼，供 embed 呈現層做顏色/危險等級判斷，
    避免依賴呼叫端自由文字 rollover_type 的子字串比對（該作法曾導致最危險的
    MARGIN_DEFENSE 警報無法正確標紅，詳見 rollover_embeds.py）。"""

    OPPORTUNITY_COST = "OPPORTUNITY_COST"
    SATELLITE_REBALANCE = "SATELLITE_REBALANCE"
    MARGIN_DEFENSE = "MARGIN_DEFENSE"
    FUNDAMENTAL_BROKEN = "FUNDAMENTAL_BROKEN"
    CORE_DEPLOYMENT = "CORE_DEPLOYMENT"
    MACRO_TOP_ESCAPE_DEFENSE = "MACRO_TOP_ESCAPE_DEFENSE"
    COVERED_CALL_PROFIT_LOCK = "COVERED_CALL_PROFIT_LOCK"
    TRANSITION_ENGINE = "TRANSITION_ENGINE"


class TradingStrategyMode(str, Enum):
    """使用者 /settings 可選的交易策略模式，決定 Scenario 2 (opportunity_cost.py)
    進場閘門要套用哪一套六重鐵律。RIGHT_SIDE 為現行、已上線的預設行為，未選擇的
    使用者一律沿用 RIGHT_SIDE，零行為變化。"""

    RIGHT_SIDE = (
        "RIGHT_SIDE"  # 右側交易：順勢動能突破六重鐵律 (opportunity_cost.py 現行邏輯)
    )
    LEFT_SIDE = "LEFT_SIDE"  # 左側交易：逆勢均值回歸六重鐵律 (left_side_entry.py)
    DYNAMIC = "DYNAMIC"  # 動態調整：4態 Regime 分類器路由 (regime_classifier.py)


class RegimeMarketData(NamedTuple):
    """`classify_dynamic_regime()` 判定過程中實際抓取/計算的市場資料。

    供呼叫端在路由至 Regime I 時原樣傳給左側六重鐵律重用——這三項原本會被
    分類器與六重鐵律各自抓取一次 (15m K 線、Session VWAP 皆為
    force_refresh=True 的真實網路請求)，除了多餘的請求成本外，更關鍵的是兩次
    抓取可能取到不同快照，導致「盤勢分類」與「進場確認」建立在不一致的資料上。
    """

    df_15m: Optional[Any] = None
    session_vwap: float = 0.0
    atr_15m: float = 0.0


class DynamicRegime(str, Enum):
    """動態調整模式的 4 態市場結構分類 (regime_classifier.py::classify_dynamic_regime)。"""

    REGIME_I_LEFT_CATCH = "REGIME_I_LEFT_CATCH"  # 左側接刀態：極端負乖離吸籌
    REGIME_II_CHAOS_STANDASIDE = (
        "REGIME_II_CHAOS_STANDASIDE"  # 混沌泥淖態：無人區過渡震盪，全系統休眠
    )
    REGIME_III_RIGHT_MOMENTUM = (
        "REGIME_III_RIGHT_MOMENTUM"  # 右側動能態：結構突破伽馬擠壓
    )
    REGIME_IV_STRUCTURAL_CAP_CRISIS = (
        "REGIME_IV_STRUCTURAL_CAP_CRISIS"  # 結構封頂／危機態：強制鎖定
    )


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
    # 雙軌出場防守引擎軌道二（極端瞬時停損，anchor_base - 3.0×ATR_15m，
    # 現價貫穿即立即觸發，無視 15m 收盤等待）。僅 SATELLITE_REBALANCE 情境
    # 的指令會攜帶此欄位；CORE_DEPLOYMENT 等其餘情境維持 None。
    extreme_stop_loss: Optional[float]
    # 這次指令是否「真的」由軌道二極端瞬時停損觸發（而非例行 15m 收盤破位或
    # 常規再平衡）。extreme_stop_loss 只是參考數值，本欄位才是「這次是否真的
    # 由此觸發」的布林旗標，供呈現層決定是否升級為最高急迫性視覺樣式。
    is_extreme_tick_breach: Optional[bool]
    # is_extreme_tick_breach=True 時，預先組裝好的完整 ANSI 明細字串（觸發價格/
    # 極端熔斷線/做市商底牆/ATR/穿透幅度/Gamma 狀態/執行指引），供呈現層原樣
    # 渲染為獨立欄位。其餘情境維持 None。
    extreme_breach_detail_block: Optional[str]
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
    # 賣方期權時間價值停利 (covered_call_profit_lock.py) 專屬欄位。
    is_covered_call_profit_lock: Optional[bool]
    is_short_option_profit_lock: Optional[bool]
    is_csp: Optional[bool]
    opt_type: Optional[str]
    margin_released: Optional[float]
    entry_premium: Optional[float]
    current_premium: Optional[float]
    decay_pct: Optional[float]
    dte: Optional[int]
    # 微觀結構出場決策矩陣 (anti_washout.py) 觸發的具體分層識別碼，例如
    # "SL_STRUCTURAL"/"SL_REGIME_FLIP"/"SL_WHALE_PUT"/"SL_TRAILING_BREAKEVEN"/
    # "TP1"/"TP2"/"TP3"。純附加欄位，供未來分析各層級觸發率之用；不影響
    # embed 呈現層 (仍僅依賴 scenario+action 決定顏色/文案)。未觸發任何分層
    # 的指令 (例如常規配置超額 REDUCE) 維持 None。
    exit_tier: Optional[str]
    # 交易策略引擎 (regime_classifier.py / left_side_entry.py) 產生此指令時所依據的
    # DynamicRegime 值（例如 "REGIME_I_LEFT_CATCH"）。僅 trading_strategy=DYNAMIC 時
    # 產生的指令會攜帶此欄位，供呈現層顯示「當前 Regime」與分析用途；純右側/左側
    # 手動模式維持 None。
    entry_regime: Optional[str]
    # 動態調整狀態切換引擎 (transition_engine.py) 專屬：本指令若成功推播，派發端
    # 應提交的 dynamic_strategy_state 增量 (例如 {"pyramided": True})，以及要寫入
    # 的資產 id。刻意不在引擎內直接落地——DM 要到 portfolio_monitor 派發迴圈才
    # 送出，中間隔著通知開關、每日 dedup 與 OPTIONS_ROLLOVER_DRY_RUN 三道閘門，
    # 提前寫入會讓一次性切換 (pyramided / lockout) 在推播被抑制時永久燒掉。
    asset_id: Optional[int]
    dynamic_state_patch: Optional[Dict[str, Any]]
