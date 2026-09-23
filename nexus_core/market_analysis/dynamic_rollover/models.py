from enum import Enum
from typing import Any, Dict, Literal, NamedTuple, Optional, TypedDict

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
    # 做空進場訊號 (short_entry_deployment.py)。刻意獨立成情境、不借用
    # OPPORTUNITY_COST：後者的語意是「賣掉衛星持倉、把資金**買進**候選標的」，
    # 整條下游 (PowerSqueeze > 80 門檻、Buy Shares 工具別、RolloverActionView
    # 的 BUY 數量計算) 全是多頭假設，做空確認走進去只會得到自相矛盾的指令。
    SHORT_ENTRY = "SHORT_ENTRY"
    # 順勢金字塔加碼 (pyramid_add.py)。刻意獨立成情境、不借用 TRANSITION_ENGINE
    # 既有的 OPEN_PYRAMID action：後者是 entry_regime 驅動的一次性狀態切換
    # （Regime I 左側倉進化為右側動能倉，由 state["pyramided"] 旗標保證只觸發
    # 一次），本情境是任何右側獲利倉在趨勢延續時的例行加碼（可觸發至
    # _PYRAMID_MAX_ADDS 次）。兩者觸發源、次數上限皆不同，合併會讓路徑一的
    # 一次性保證失效——但兩者最終都路由到同一個 action=="OPEN_PYRAMID" 下游
    # 派發分支，必須靠 scenario 欄位區分文案與資料。
    PYRAMID_ADD = "PYRAMID_ADD"


class TradingStrategyMode(str, Enum):
    """使用者 /settings 可選的交易策略模式，決定 Scenario 2 (opportunity_cost.py)
    進場閘門要套用哪一套六重鐵律。RIGHT_SIDE 為現行、已上線的預設行為，未選擇的
    使用者一律沿用 RIGHT_SIDE，零行為變化。

    ⚠️ SHORT_SIDE 是本系統唯一的空頭方向進場路徑。左側 (LEFT_SIDE) 儘管技術定義
    與右側相反，本質仍是**做多**——逆勢均值回歸、做市商 Put Wall 底牆接刀，條件三
    算的是「向上」回歸空間，條件六在高 IVR 時建議的是 Bull Call Spread / Short
    Put。不要把左側誤讀為做空。

    新增 enum 值不需要 migration：user_settings.trading_strategy 是
    TEXT DEFAULT 'RIGHT_SIDE'，無 CHECK 約束 (見 v068_add_trading_strategy.py)。"""

    RIGHT_SIDE = (
        "RIGHT_SIDE"  # 右側交易：順勢動能突破六重鐵律 (opportunity_cost.py 現行邏輯)
    )
    LEFT_SIDE = "LEFT_SIDE"  # 左側交易：逆勢均值回歸六重鐵律 (left_side_entry.py)
    SHORT_SIDE = "SHORT_SIDE"  # 做空交易：結構破位追空六重鐵律 (short_side_entry.py)
    DYNAMIC = "DYNAMIC"  # 動態調整：5態 Regime 分類器路由 (regime_classifier.py)


class RiskAppetite(str, Enum):
    """使用者 /settings 可選的風險偏好，決定 TP 階梯比例、EV 轉倉門檻與核心資金
    部署比例要套用哪一組參數 (見 constants.py::resolve_risk_profile)。

    DEFENSIVE 為現行、已上線的預設行為，未選擇的使用者一律沿用，零行為變化。
    AGGRESSIVE 的數值全部來自 calibration/backtest_engine_2025.py 已驗證的
    aggressive 模式（2025 回測顯示其報酬/MDD/Sharpe 三項皆優於 DEFENSIVE，見
    docs/strategies/04_dynamic_rollover_state_machine.md §2.10）。

    新增 enum 值不需要 migration：user_settings.risk_appetite 是
    TEXT DEFAULT 'DEFENSIVE'，無 CHECK 約束（見 v076_add_risk_appetite.py）。
    """

    DEFENSIVE = "DEFENSIVE"
    AGGRESSIVE = "AGGRESSIVE"


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
    # 分類器判定 Regime V / III 時本來就已算出的 15m RSI，供做空倉位的凱利勝率
    # 先驗查表沿用 (kelly_priors.get_win_rate_prior)。未算出時為 NaN。
    rsi_15m: float = float("nan")


EntryDirection = Literal["LONG", "SHORT"]


class ShortEntryEvaluation(NamedTuple):
    """做空六重鐵律的完整評估結果 (short_side_entry.evaluate_short_entry)。

    舊的 `(bool, reason, structure_directive)` 三元組只回答「過不過」，但下游
    建立做空指令需要進場／停損／目標三個價位——它們全是六重鐵律評估過程中
    本來就已經算出的中間值 (頂牆、Put Wall、次級負 GEX 節點、ATR)。若只回傳
    三元組，下游勢必重新抓取與重算，除了多餘 I/O，更會讓「確認」與「下單價位」
    建立在兩份不同的資料快照上。

    `conditions` 逐項記錄六條件：True／False，或 None 表示被短路略過 (⏭️)。
    """

    all_passed: bool
    reason: str
    structure_directive: Optional[str]
    sub_mode: str  # "區間內做空" | "破位追空" | "N/A"
    conditions: tuple[Optional[bool], ...]
    spot: float
    resistance_wall: float
    call_wall: float
    put_wall: float
    gamma_flip: float
    next_negative_node: float
    net_gex: Optional[float]
    session_vwap: float
    atr_15m: float
    atr_1d: float
    ivr: float


class EntryConfirmation(NamedTuple):
    """Scenario 2 進場鐵律的確認結果，轉交 Scenario 5 與 SHORT_ENTRY 情境沿用。

    早期是 `(is_confirmed, reason)` 二元組，**不帶方向**——`core_deployment.py`
    因此把做空確認當成「候選標的可以買進」，把 CORE 超額資金以 Buy Shares 部署
    進剛被確認要做空的標的。`direction` 是修復該缺陷的最小必要資訊。
    """

    is_confirmed: bool
    reason: str
    direction: EntryDirection = "LONG"
    short_evaluation: Optional[ShortEntryEvaluation] = None
    entry_regime: Optional[str] = None
    # 分類器算出的 15m RSI (Regime V 路徑)，供做空倉位的凱利先驗查表沿用。
    rsi_15m: Optional[float] = None


class DynamicRegime(str, Enum):
    """動態調整模式的 6 態市場結構分類 (regime_classifier.py::classify_dynamic_regime)。

    判定優先序（非 enum 宣告序）：

        1. Regime IV 的**宏觀鎖定**分支 (SYSTEMIC_LIQUIDITY_CRISIS /
           SHORT_GAMMA_CRITICAL / VIX 深度倒掛) —— 壓過一切，做多做空皆禁。
           系統性流動性危機下做空同樣會被劇烈軋空，不是安全的方向。
        2. Regime V  破位追空態
        3. Regime IV 的**個股結構封頂**分支 (Call Wall 空間不足 / STO 封頂)
        4. Regime III → Regime III-B → Regime I → Regime II

    第 2 與第 3 的先後是刻意的：個股結構封頂與破位追空的條件可以同時成立
    (壓頂 + 跌破底牆)，若不拆分優先序，做空將永遠被 Regime IV 遮蔽而無法觸發。

    Regime III-B 緊接在 Regime III 之後、且**必須**在其之後：兩者都是多頭右側
    路徑，III 是「突破正在發生」的事件式判定 (放量 + 實體陽線 + RSI > 55)，
    III-B 是「趨勢仍然成立」的狀態式判定 (近 6 根已收盤 K 棒至少 5 根站穩結構)。
    突破當下兩者都會成立，此時應歸類為 III——它帶著更強的進場證據，且 III-B 的
    UOA 時間窗放寬不應套用在突破態上。
    """

    REGIME_I_LEFT_CATCH = "REGIME_I_LEFT_CATCH"  # 左側接刀態：極端負乖離吸籌
    REGIME_II_CHAOS_STANDASIDE = (
        "REGIME_II_CHAOS_STANDASIDE"  # 混沌泥淖態：無人區過渡震盪，全系統休眠
    )
    REGIME_III_RIGHT_MOMENTUM = (
        "REGIME_III_RIGHT_MOMENTUM"  # 右側動能態：結構突破伽馬擠壓
    )
    # 右側趨勢延續態：已在趨勢中、非突破瞬間。路由至右側六重鐵律，但條件一改判
    # 「持續站穩」、條件四改為 _ENTRY_UOA_LOOKBACK_DAYS 交易日回看窗。
    # 乾跑期間由 config.REGIME_III_B_DRY_RUN 在派發端攔截，不推播只記稽核。
    REGIME_III_B_TREND_CONTINUATION = "REGIME_III_B_TREND_CONTINUATION"
    REGIME_IV_STRUCTURAL_CAP_CRISIS = (
        "REGIME_IV_STRUCTURAL_CAP_CRISIS"  # 結構封頂／危機態：強制鎖定
    )
    REGIME_V_BREAKDOWN_CHASE = (
        "REGIME_V_BREAKDOWN_CHASE"  # 破位追空態：跌破底牆 + 負 Gamma 順勢助跌
    )


class FundamentalThesisResult(BaseModel):
    # 讓模型先進行思考與文字輸出
    reasoning: str = Field(description="Step-by-step reasoning in Traditional Chinese")
    # 思考完後再給出最終判斷
    is_broken: bool = Field(
        description="True if structural thesis is broken, False if just macro/temporary"
    )
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")


class ShortEntryPlan(TypedDict):
    """SHORT_ENTRY 指令攜帶的進場計畫 (short_entry_sizing.py 產出)。

    停損刻意列出兩個來源：`stop_price_structural` 是進場鐵律條件二／三的
    參考停損 (頂牆 + 0.5×ATR₁₅ₘ)，`stop_price_exit_engine` 是部位登錄後做空
    鏡像出場矩陣實際會執行的停損。`stop_price` 取兩者較遠者用於倉位計算——
    倉位必須以「真的會被執行的停損」為準，否則風險預算會被低估。
    """

    sub_mode: str
    entry_price: float
    stop_price: float
    stop_price_structural: float
    stop_price_exit_engine: float
    target_price: float
    reward_risk_ratio: float
    risk_budget_usd: float
    share_qty: int
    notional_usd: float
    binding_constraint: str
    vix_spot: Optional[float]
    vix_tier_name: str
    short_vix_multiplier: float
    kelly_fraction: float
    invalidation_note: Optional[str]


class PyramidAddPlan(TypedDict):
    """PYRAMID_ADD 指令攜帶的加碼計畫 (pyramid_add.py 產出)。

    倉位模型與 SHORT_ENTRY 的「風險預算 ÷ 停損距離」同源、方向反轉：
    停損距離為 `spot - ratchet_stop`（棘輪停損已由條件二保證 >= avg_cost，
    加碼因此只動用「已實現的帳面利潤」承險，不增加原始部位的本金曝險）。
    """

    entry_price: float
    stop_price: float
    stop_distance_usd: float
    reward_risk_ratio: float
    risk_budget_usd: float
    share_qty: int
    notional_usd: float
    binding_constraint: str
    vix_spot: Optional[float]
    vix_tier_name: str
    vix_multiplier: float
    kelly_fraction: float
    pyramid_count_after: int


class AdvisoryPlan(TypedDict, total=False):
    """顧問模式 (portfolio_mode=ADVISORY / advisory_only) 的位階資訊 (advisory_mode.py 產出)。

    顧問指令 (`action == "ADVISORY"`) 只告知位階，不攜帶任何賣出動作：
    `sell_ratio` 恆為 0.0、`target_core` 恆為 ""。

    kind:
      * ``STRUCTURE_FAILURE`` — 結構失效 (SL-結構失效／極端瞬時停損)，附 `stop_loss`。
      * ``TARGET_REACHED`` — 已抵達目標區 (TP1~TP3 摺疊)，附 `call_wall`／`target`。
    """

    kind: Literal["STRUCTURE_FAILURE", "TARGET_REACHED"]
    spot: float
    stop_loss: Optional[float]
    call_wall: Optional[float]
    target: Optional[float]
    is_blue_sky: bool


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
    # 進場鐵律條件六依「當下市況」現算的建議合約天期與部位結構 (右側來自
    # opportunity_cost._derive_entry_structure_directive，左側來自 left_side_entry
    # 的 IVR 分流)。刻意獨立成欄位、不覆寫上方 suggested_strategy——後者回答的是
    # 「用什麼工具進場」(Buy Shares / Shares + ITM Call，由 _calculate_rollover_
    # decision 自行決策)，本欄位回答的是「若以期權表達，該選哪個天期與結構」，
    # 兩者互補而非互斥。早期版本以覆寫實作，會把 "Shares + ITM Call" 連同它自帶
    # 的 ITM 70Δ 履約價/DTE 指引一起抹掉。僅 OPPORTUNITY_COST 情境會攜帶此欄位。
    structure_directive: Optional[str]
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
    # SHORT_ENTRY 情境專屬：進場／停損／目標價位與倉位計算結果。
    short_entry_plan: Optional[ShortEntryPlan]
    # PYRAMID_ADD 情境專屬：加碼股數／風險預算／停損距離倉位計算結果。
    pyramid_add_plan: Optional[PyramidAddPlan]
    # 顧問模式專屬：`action == "ADVISORY"` 指令攜帶的位階資訊。⚠️ `action` 是純 str
    # 而非 Literal，新增 "ADVISORY" 值時 mypy 不會提示任何未處理的消費端分支，
    # 唯一的防護是 tests/unit/test_advisory_mode.py 的參數化不變式測試。
    advisory_plan: Optional[AdvisoryPlan]
