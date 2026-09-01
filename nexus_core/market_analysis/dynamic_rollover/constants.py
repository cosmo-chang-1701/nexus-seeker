# _compute_structural_breakdown_signals 每 30 分鐘週期會被 Scenario 3
# (check_satellite_rebalancing) 與 Scenario 4 (evaluate_margin_defense) 對同一批
# portfolio_assets 各呼叫一次，對同一標的重跑一次完整 GEX 逐履約價掃描屬重複運算。
# 短 TTL 足以涵蓋同一輪次內兩次呼叫，且短到不會跨到下一個 30 分鐘週期造成資料陳舊。
_STRUCTURAL_SIGNALS_CACHE_TTL: float = 300.0

# 核心防禦性 ETF 排除清單：機會成本轉倉 (_find_best_rollover_target) 與槓桿保證金
# 防禦 (evaluate_margin_defense) 共用同一份定義，避免各自維護造成分歧
# (曾發生 VXX 在部分清單中被排除、部分清單中未被排除的不一致)。
CORE_DEFENSE_ETF_SYMBOLS: frozenset[str] = frozenset(
    {"QQQ", "SPY", "VOO", "VXX", "IVV", "VTI"}
)

# _generate_rule_based_rebalance_report 在快取與現價皆無法取得目標資產參考價格時
# 使用的最終備援估計值（僅用於股數建議粗估，非交易執行依據）。
_FALLBACK_TARGET_PRICE_ESTIMATE = 500.0

# evaluate_opportunity_cost 中，機會成本轉倉的 EV Spread 門檻須額外扣除的保守
# 往返交易成本估計值 (佣金 + 預期滑價)，避免轉倉在扣除交易成本後實質虧損。
# 非逐券商精算，僅作保守閘門，涵蓋常規轉倉與極致不對稱勝率強制全倉分支
# (後者巢狀於同一 ev_spread 門檻之內，故單一常數即可覆蓋兩者)。
_ESTIMATED_ROUND_TRIP_COST_PCT: float = 0.003

# --- 決策門檻具名常數 (純重構，零行為變化；不串接 risk_limit 或新增 per-user 設定) ---
_MOMENTUM_DECAY_THRESHOLD: float = 20.0  # PowerSqueeze < 此值視為原持倉動能衰退
_BREAKOUT_READY_THRESHOLD: float = 80.0  # PowerSqueeze > 此值視為新標的突破待發
_EV_SPREAD_MIN_THRESHOLD: float = 0.05  # 機會成本轉倉最低期望值差距門檻
_ROLLOVER_RATIO_HIGH_PROFIT: float = 0.5  # 原持倉獲利 > 30% 時的機會成本轉倉比例
_ROLLOVER_RATIO_STANDARD: float = 0.3  # 原持倉獲利一般/虧損時的機會成本轉倉比例
_PROFIT_LOCK_PROFIT_PCT_THRESHOLD: float = 0.3  # 判定「獲利豐厚」的持倉獲利率門檻
_LOW_IVR_UPPER_BOUND: float = 30.0  # 極致不對稱勝率條件之「低 IVR」上限
_PUT_WALL_PROXIMITY_TOLERANCE: float = 0.01  # 極致不對稱勝率條件之貼近 put_wall 容差
_PROFIT_UNLOCK_TOLERANCE: float = 0.015  # 現價貼近 call_wall 視為目標區獲利解鎖的容差
_EUPHORIA_SKEW_PERCENTILE: float = (
    20.0  # Skew Percentile <= 此值視為極端亢奮 (Euphoria)
)
_IV_BUBBLE_THRESHOLD: float = 80.0  # IVR > 此值視為 IV 泡沫 (擺脫高波洗籌泥淖)
_EXHAUSTION_SKEW_PERCENTILE: float = 30.0  # 雙重動能衰竭確認制之 Skew 回升門檻
_EUPHORIA_CAPITAL_SPLIT_PRIMARY: float = 0.9  # Euphoria 雙軌機制主要轉倉資金比例
_EUPHORIA_CAPITAL_SPLIT_RESIDUAL: float = 0.1  # Euphoria 雙軌機制留存原標的資金比例
_BUYER_LOCKOUT_IVR_THRESHOLD: float = 50.0  # IVR > 此值時嚴禁買方策略 (規避 Gamma 陷阱)
_DEFAULT_MAX_ALLOCATION_PCT: float = (
    0.3  # 未設定 max_allocation_pct 時的預設衛星部位上限
)

# --- 邏輯 (5)：核心資金部署 (evaluate_core_deployment) 具名常數 ---
_CORE_EXCESS_MIN_TRADE_PCT: float = 0.005  # CORE 超額配置低於此幅度 (0.5%) 視為誤差雜訊，不觸發部署轉倉，避免 dust trade
_BOXX_DEFENSE_THRESHOLD: float = 50.0  # boxx_allocation_pct (0-100) >= 此值時，超額資金優先防禦轉入 BOXX 而非候選標的
# 機會分支（State A）通過既有六重鐵律 _confirm_entry_signal 且額外通過
# market_analysis.entry_ironclad.check_entry_ironclad_rules（進場四重鐵律）
# 後，僅動用超額資金的這個比例部署至候選標的；剩餘部分維持現金/緩衝，
# 不生成第二筆分流指令。BOXX 防禦分支不受此常數影響，仍為 100% 部署。
_CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO: float = 0.5

# --- 邏輯 (5) 延伸：Covered Call Overlay (evaluate_covered_call_overlay) 具名常數 ---
# 與 evaluate_core_deployment 的兩個既有分支不同，本分支刻意不要求
# target_allocation_pct opt-in (詳見該函式 docstring)，只要求 CORE 持倉股數
# 達 1 口門檻，故獨立於上方兩個常數之外另立一組。
_COVERED_CALL_MIN_SHARES: int = 100  # 1 口最低股數門檻
_COVERED_CALL_MAX_LOTS: int = (
    1  # 使用者明確規格：固定 1 口，未來若放寬為 N 口只需調整此常數
)
_COVERED_CALL_MIN_DTE: int = 18
_COVERED_CALL_MAX_DTE: int = 25

# --- 進場訊號六重嚴格過濾鐵律 (opportunity_cost.py::_confirm_entry_signal) 具名常數 ---
# (原註解誤標「四重」，已修正——本函式實為六項條件，見其自身 docstring)
#
# 與 market_analysis/entry_ironclad.py 的「進場四重嚴格過濾鐵律」
# (check_entry_ironclad_rules) 交叉對照表：兩套鐵律刻意完全獨立、不合併
# (見 entry_ironclad.py 模組 docstring 的設計理由)，條件一/三/四在概念上
# 有對應但門檻/範圍刻意收得更嚴，條件二為簡化版，條件五/六在四重鐵律中
# 無對應（四重鐵律是零 I/O 純函式，不含總經/財報/candidate 自身 DTE 檢查）：
#   六重條件一 (放量倍數 1.2x)         <-> 四重規則一 (entry_ironclad._IRONCLAD_VOLUME_SURGE_MULTIPLIER = 1.5x，更嚴)
#   六重條件二 (_scan_gex_walls 完整掃描) <-> 四重規則二 (僅檢查 put_wall > 0 且現價站上，簡化版)
#   六重條件三 (無上緣界限、嚴格 >)      <-> 四重規則三 (entry_ironclad._IRONCLAD_UOA_CAP_RATIO_THRESHOLD /
#                                          _IRONCLAD_UOA_CAP_UPSIDE_ROOM_PCT，限定 (spot, spot*1.05] 視窗、改用 >=)
#   六重條件四 (僅檢查 DTE>=7，不檢查 ratio) <-> 四重規則四 (entry_ironclad._IRONCLAD_UOA_ENTRY_MIN_DTE /
#                                          _IRONCLAD_UOA_ENTRY_MIN_RATIO，額外要求 ratio>=0.8)
#   六重條件五 (總經/財報安全閥)         <-> 四重鐵律無對應（純函式不含此 I/O）
#   六重條件六 (candidate 自身 DTE)      <-> 四重鐵律無對應（純函式不含此 I/O）
_ENTRY_VOLUME_LOOKBACK_BARS: int = 20  # 條件一：15m 成交量基準所需回看根數 (不含確認根)
_ENTRY_VOLUME_SURGE_MULTIPLIER: float = 1.2  # 條件一：「放量」門檻，須達回看均量的倍數
_ENTRY_UOA_CAP_RATIO_THRESHOLD: float = (
    1.0  # 條件三：單筆 STO Call 視為物理封頂的 ratio (volume/OI) 門檻
)
_ENTRY_ASYMMETRIC_ROOM_PCT: float = 0.05  # 條件三：上方須保留的最低非對稱獲利空間
_ENTRY_UOA_MIN_DTE: int = 7  # 條件四：驅動進場的主力 UOA 買盤最低 DTE 要求
_ENTRY_CANDIDATE_MIN_DTE: int = (
    1  # 條件六：標的自身最近效期需 > 此值天數 (避開 0/1 DTE 結算日雜訊)
)

# --- 華爾街資深交易員與機構風控量化常數 ---
_BEAR_CALL_SPREAD_WING_ATR_MULT: float = (
    1.5  # Bear Call Spread 保護腳距賣方腳的 15m ATR 寬度倍數
)
# 防洗盤動態停損機制 2 的基礎 ATR 墊片倍數，相對於 anchor_base（非現價）：
# base_stop_loss = anchor_base - _ANTI_WASHOUT_BASE_ATR_MULT * atr_15m。
# 注意：DTE<=1 的部位不會流經此計算——已由 evaluate_option_dte_tier() 判定為
# EXPIRATION_SETTLEMENT_ALERT，在 check_satellite_rebalancing_impl 迴圈最前段
# 直接短路為強制結算保護指令（見 _build_forced_settlement_instruction()），
# 不再走「擴大停損空間抗單」的舊行為（該行為已於 DTE 三態狀態機重構中移除）。
_ANTI_WASHOUT_BASE_ATR_MULT: float = 1.5
# 雙軌出場防守引擎軌道二：極端瞬時停損 (Extreme Tick Breach) 的 ATR 墊片倍數。
# 刻意獨立於上方 _ANTI_WASHOUT_BASE_ATR_MULT 之外另立常數（數值恰好為 2 倍純屬
# 巧合，兩者互不疊加）：任何 DTE 皆適用的獨立「極端瞬時停損」防線，產生獨立的
# extreme_stop_loss 欄位，不套用 LVN 吸附，現價 (SPOT 亦然，不等待 15m 收盤)
# 貫穿即立即觸發，作為與 OPTIONS 既有即時 tick 熔斷同等級的最後防線。
_ANTI_WASHOUT_EXTREME_ATR_MULT: float = 3.0
_BEAR_CALL_SPREAD_WING_FALLBACK_PCT: float = (
    0.05  # atr_15m 無效時，Bear Call Spread Wing 距離退回以賣方履約價的百分比估算
)
_TRAILING_STOP_ATR_MULT: float = (
    0.5  # 極端亢奮區剩餘 10% 部位移動止盈：距離 call_wall 的 15m ATR 倍數
)
_TRAILING_STOP_SPOT_FLOOR_PCT: float = (
    0.98  # 移動止盈價位相對現價的下限保護 (不得低於現價 98%)
)
_SKEW_DOWNSIDE_PENALTY_FACTOR: float = (
    0.5  # Skew 偏空 (<50%) 時 EV 計算之最大下行風險懲罰係數
)
_EARNINGS_PRE_EVENT_BUFFER_DAYS: int = (
    3  # 機會成本轉倉候選標的避開即將發布財報的最小緩衝天數
)

# --- 邏輯 (6)：宏觀逃頂前瞻防禦 (evaluate_macro_top_escape_defense) 具名常數 ---
# 校準基準：Scenario 3 (反應式，個股結構已破) 用 90%；Scenario 4 (反應式，系統性
# regime + 保證金壓力已雙重確認) 用 100%；本情境是純粹的「領先訊號」(組合式機率
# 評分，尚無任何個股結構真正破位)，假陽性風險明顯高於前兩者，故 25% 明顯保守，
# 只做風險曝險的部分削減，不強迫在可能誤判的訊號上全額出場。
_MACRO_TOP_ESCAPE_TRIM_RATIO: float = 0.25
# 對應 evaluate_macro_top_escape_score() (index_microstructure.py) 的分級輸出，
# 僅最高分級 (CRITICAL，>= 3 項因子同時觸發) 才會啟動本情境的實際減碼動作。
_MACRO_TOP_ESCAPE_MIN_TIER: str = "CRITICAL"

# --- DTE 三態狀態機 (structural_signals.py::evaluate_option_dte_tier) 具名常數 ---
# 僅對 OPTIONS 部位有意義。與既有 _ENTRY_UOA_MIN_DTE(7)/_ENTRY_CANDIDATE_MIN_DTE(1)
# 刻意分開命名而不合併重用：後兩者是「候選標的」進場確認條件的一部分，本組常數
# 是「既有持倉」本身的到期日分級門檻，語意不同，數值恰好相同純屬巧合。
_HOLDING_DTE_LOCKOUT_THRESHOLD: int = 7  # dte < 此值時，Scenario 2 的機會成本轉倉
# 與 Scenario 3 Euphoria 分支的「開立全新 Bear Call Spread」判定一律封鎖 (末日
# 流動性雜訊，不適合用於驅動新開倉/轉倉決策)；既有部位的雙軌停損監控不受影響。
_HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD: int = 1  # dte <= 此值時，無論停損是否
# 觸發，一律強制結算保護 (LIQUIDATE 100%，轉倉至同標的次月主力合約)，取代舊版
# 「擴大停損空間 + 口數縮放」的漸進式風險平價機制。
# 「次月主力合約」文案敘述用的效期窗口，沿用 opportunity_cost.py 既有
# find_best_contract(..., 21, 45) 與 Covered Call 效期窗口的慣例。
_FORCED_SETTLEMENT_ROLL_MIN_DTE: int = 21
_FORCED_SETTLEMENT_ROLL_MAX_DTE: int = 45

# --- Covered Call 權利金衰減停利 (covered_call_profit_lock.py) 具名常數 ---
# 僅適用於既有的空頭 CALL 部位 (Covered Call)；空頭 PUT (CSP) 不在範圍內。
# decay_pct = (entry_premium - current_premium) / entry_premium。
_COVERED_CALL_PROFIT_LOCK_PARTIAL_DECAY_PCT: float = 0.50  # 達此衰減幅度局部停利
_COVERED_CALL_PROFIT_LOCK_FULL_DECAY_PCT: float = 0.80  # 達此衰減幅度全額停利
_COVERED_CALL_PROFIT_LOCK_PARTIAL_RATIO: float = 0.5  # 局部停利門檻的 BTC 比例
