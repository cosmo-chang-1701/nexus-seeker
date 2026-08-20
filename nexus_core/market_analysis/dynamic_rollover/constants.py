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

# --- 進場訊號四重嚴格過濾鐵律 (_confirm_entry_signal) 具名常數 ---
_ENTRY_VOLUME_LOOKBACK_BARS: int = 20  # 條件一：15m 成交量基準所需回看根數 (不含確認根)
_ENTRY_VOLUME_SURGE_MULTIPLIER: float = 1.2  # 條件一：「放量」門檻，須達回看均量的倍數
_ENTRY_UOA_CAP_RATIO_THRESHOLD: float = (
    1.0  # 條件三：單筆 STO Call 視為物理封頂的 ratio (volume/OI) 門檻
)
_ENTRY_ASYMMETRIC_ROOM_PCT: float = 0.05  # 條件三：上方須保留的最低非對稱獲利空間
_ENTRY_UOA_MIN_DTE: int = 7  # 條件四：驅動進場的主力 UOA 買盤最低 DTE 要求

# --- 華爾街資深交易員與機構風控量化常數 ---
_BEAR_CALL_SPREAD_WING_ATR_MULT: float = (
    1.5  # Bear Call Spread 保護腳距賣方腳的 15m ATR 寬度倍數
)
_DYNAMIC_STOP_MIN_ATR_MULT: float = (
    1.0  # 動態防洗盤停損相對於現價的最小 ATR 距離 (防止太窄被洗)
)
_DYNAMIC_STOP_MAX_ATR_MULT: float = (
    3.0  # 動態防洗盤停損相對於現價的最大 ATR 距離 (防止太寬失控)
)
_SKEW_DOWNSIDE_PENALTY_FACTOR: float = (
    0.5  # Skew 偏空 (<50%) 時 EV 計算之最大下行風險懲罰係數
)
_EARNINGS_PRE_EVENT_BUFFER_DAYS: int = (
    3  # 機會成本轉倉候選標的避開即將發布財報的最小緩衝天數
)
