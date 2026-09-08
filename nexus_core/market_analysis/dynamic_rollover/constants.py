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
# ⚠️ 注意：_PROFIT_UNLOCK_TOLERANCE 與 _EUPHORIA_SKEW_PERCENTILE 已不再是
# Scenario 3 (anti_washout.py) 的清倉閘門條件——該角色已由下方「微觀結構出場
# 決策矩陣」的 TP1/TP2/SL-主力對沖 取代。兩者現僅由 Scenario 6
# (macro_top_escape_defense.py::_compute_satellite_euphoria_ratio) 獨立引用，
# 作為其「衛星持倉亢奮比例」複合評分因子的輸入，故不可刪除。
_PROFIT_UNLOCK_TOLERANCE: float = (
    0.015  # Scenario 6 專用：現價貼近 call_wall 視為亢奮的容差
)
_EUPHORIA_SKEW_PERCENTILE: float = (
    20.0  # Scenario 6 專用：Skew Percentile <= 此值視為極端亢奮 (Euphoria)
)
_IV_BUBBLE_THRESHOLD: float = 80.0  # IVR > 此值視為 IV 泡沫 (擺脫高波洗籌泥淖)
_BUYER_LOCKOUT_IVR_THRESHOLD: float = 50.0  # IVR > 此值時嚴禁買方策略 (規避 Gamma 陷阱)
_DEFAULT_MAX_ALLOCATION_PCT: float = (
    0.3  # 未設定 max_allocation_pct 時的預設衛星部位上限
)

# --- 邏輯 (5)：核心資金部署 (evaluate_core_deployment) 具名常數 ---
_CORE_EXCESS_MIN_TRADE_PCT: float = 0.005  # CORE 超額配置低於此幅度 (0.5%) 視為誤差雜訊，不觸發部署轉倉，避免 dust trade
_BOXX_DEFENSE_THRESHOLD: float = 50.0  # boxx_allocation_pct (0-100) >= 此值時，超額資金優先防禦轉入 BOXX 而非候選標的
# 機會分支（State A）通過既有六重鐵律 _confirm_entry_signal 後，僅動用超額
# 資金的這個比例部署至候選標的；剩餘部分維持現金/緩衝，不生成第二筆分流
# 指令。BOXX 防禦分支不受此常數影響，仍為 100% 部署。
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
# 六項條件必須同時成立才允許對候選標的實際啟動機會成本轉倉/核心資金部署指令：
#   條件一：結構性右側放量突破 (15m 實體「陽線」收盤站穩 Gamma Flip 估算門檻 +
#           放量 + Session VWAP 站穩確認)。針對極端單邊期權分佈導致 Gamma Flip
#           無零交叉點之邊界：若全鏈動態 Net GEX < 0 則確認處於全域 Short Gamma
#           泥淖，判定為結構性空頭直接不通過；若全鏈動態 Net GEX > 0 則代表做市商
#           處於正 Gamma 吸收波動的自穩定狀態，啟用 Fallback 替代方案改以站穩
#           Session VWAP + 0.5 × ATR₁₅ₘ 作為突破確認標準，避免誤殺；若數據缺失
#           則 fail-safe 判定未通過。
#   條件二：做市商正 Gamma 底牆完好。支撐位物理定義上必須位於現價下方，掃描
#           範圍強制約束在現價下方 (K < Spot)，即 Support Wall = argmax_{K < Spot}
#           (Net GEX(K))，避免將現價上方的阻力牆 (Call Wall) 誤當成支撐底牆；
#           若現價下方無正 GEX 峰值 (或曝險低於 500k 薄紙牆門檻) 則判定未通過；
#           現價須 > 支撐牆，且距離落在 (0, _ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT]
#           之內才算「即時有效防禦」(支撐牆離現價過遠等同缺乏保護)。
#   條件三：做市商阻力結構與非對稱空間 (無 UOA 物理封頂，Call Wall 空間充足)。
#           物理封頂偵測改以 Call Wall（而非現價）作為 strike 位置基準，並將
#           ratio (volume/OI) 門檻提高為 _ENTRY_UOA_CAP_RATIO_THRESHOLD，降低
#           一般 STO 平倉/避險單被誤判為物理封頂的假警報率。
#           Call Wall 距現價空間% 比照分析中心同一函式的 GEX CallWall 欄位，
#           用帶正負號的距離 (call_wall - spot) / spot 判定，不要求 Call Wall
#           必須還在現價之上——現價已觸及/跌破 Call Wall (負距離) 同樣視為
#           空間不足，而非誤判為「已站上、無封頂」。
#   條件四：主力跨週期買盤認證與雜訊過濾 (BTO Call，須同時滿足 DTE、ratio、
#           名目金額 _ENTRY_UOA_MIN_NOTIONAL_USD 三門檻，並排除 strike 低於
#           現價的深實值避險單)
#   條件五：二元宏觀與財報事件安全閥
#   條件六：candidate 自身最近效期選擇權週期雜訊過濾 (避開 0/1 DTE)
_ENTRY_VOLUME_LOOKBACK_BARS: int = 20  # 條件一：15m 成交量基準所需回看根數 (不含確認根)
_ENTRY_VOLUME_SURGE_MULTIPLIER: float = (
    1.5  # 條件一：「放量」門檻，須達回看均量的 1.5 倍
)
_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT: float = (
    0.05  # 條件二：現價距支撐牆的最大有效防禦距離，超過視為缺乏即時保護
)
_ENTRY_UOA_CAP_RATIO_THRESHOLD: float = (
    1.5  # 條件三：單筆 STO Call 視為物理封頂的 ratio (volume/OI) 門檻
)
_ENTRY_ASYMMETRIC_ROOM_PCT: float = (
    0.05  # 條件三：Call Wall 距現價須保留的最低非對稱獲利空間 (帶正負號距離)
)
_ENTRY_UOA_MIN_DTE: int = 7  # 條件四：驅動進場的主力 UOA 買盤最低 DTE 要求
_ENTRY_UOA_MIN_RATIO: float = (
    0.8  # 條件四：驅動進場的主力 UOA 買盤最低 ratio (volume/OI) 要求
)
_ENTRY_UOA_MIN_NOTIONAL_USD: float = (
    200_000.0  # 條件四：驅動進場的主力 UOA 買盤最低權利金名目金額要求
)
_ENTRY_CANDIDATE_MIN_DTE: int = (
    1  # 條件六：標的自身最近效期需 > 此值天數 (避開 0/1 DTE 結算日雜訊)
)

# --- 華爾街資深交易員與機構風控量化常數 ---
# 雙軌出場防守引擎軌道二：極端瞬時停損 (Extreme Tick Breach) 的 ATR 墊片倍數。
# 任何 DTE 皆適用的獨立「極端瞬時停損」防線（黑天鵝/流動性真空最後防線），
# 產生獨立的 extreme_stop_loss 欄位，不套用 LVN 吸附，現價 (SPOT 亦然，不等待
# 15m 收盤) 貫穿即立即觸發。微觀結構出場決策矩陣 (SL-結構失效等) 未涵蓋此
# 黑天鵝情境，故本常數維持不變，獨立於下方矩陣常數之外。
_ANTI_WASHOUT_EXTREME_ATR_MULT: float = 3.0
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

# --- 邏輯 (4) 延伸：保證金防禦第三轉倉目的地 —— 反向ETF對照表 (inverse_hedge.py) ---
# 個股直接映射清單：僅收錄使用者確認、具備官方單股反向ETF商品的標的，每檔標的視
# 發行商實際推出的槓桿倍率收錄 "1x"/"2x" 其中一個或兩個 key。
# ⚠️ 這類商品規模小、成交量薄，且發行商會不定期清算/下市/更名，本表僅為初始骨架，
# 需使用者定期核實現行是否仍在市場交易、成交量/點差是否足夠再繼續使用。
SINGLE_STOCK_INVERSE_MAP: dict[str, dict[str, str]] = {
    "NVDA": {"1x": "NVDD", "2x": "NVD"},  # Direxion -1x / GraniteShares -2x
    "TSLA": {"1x": "TSLS", "2x": "TSDD"},  # Direxion -1x / GraniteShares -2x
    "AAPL": {"1x": "AAPD"},  # Direxion -1x（無已確認 -2x 商品）
    "AMD": {"1x": "AMDD", "2x": "DAMD"},  # Direxion -1x / Defiance -2x
    "TSM": {"1x": "TSMZ", "2x": "STSM"},  # Direxion -1x / Defiance -2x
    "AMZN": {"1x": "AMZD"},  # Direxion -1x（無已確認 -2x 商品）
    "AAOI": {"2x": "AAOZ"},  # Tradr -2x（無已確認 -1x 商品）
}

# 大盤指數ETF直接映射：供 CORE_DEFENSE_ETF_SYMBOLS 中的 QQQ/SPY/IWM/DIA 若因故被
# 判定為 SATELLITE 持倉時使用（現行架構下極少見），亦作為下方產業分類與最終回退
# 依據。皆為長期存在、流動性有長期驗證紀錄的商品，不分槓桿倍率層級（僅收錄一檔）。
INDEX_INVERSE_MAP: dict[str, str] = {
    "QQQ": "SQQQ",  # ProShares UltraPro Short QQQ (-3x)
    "SPY": "SH",  # ProShares Short S&P500 (-1x)
    "IWM": "SRTY",  # ProShares UltraPro Short Russell2000 (-3x)
    "DIA": "SDOW",  # ProShares UltraPro Short Dow30 (-3x)
}

# 產業分類反向ETF：與 market_analysis.risk_engine.SECTOR_BENCHMARK_MAP 的產業 ETF
# 對應，供未列入 SINGLE_STOCK_INVERSE_MAP 的個股依產業分類回退。僅收錄長期存在、
# 流動性有長期驗證紀錄的商品；SECTOR_BENCHMARK_MAP 涵蓋但此表未收錄的產業
# (XLV/XLY/XLI) 代表尚無足夠信心確認的反向商品，一律回退至 INDEX_INVERSE_MAP["SPY"]
# (SH)，而非強行猜測代號。
SECTOR_INVERSE_MAP: dict[str, str] = {
    "SMH": "SOXS",  # Direxion 每日3倍反向半導體 ETF (-3x)
    "XLK": "TECS",  # Direxion 每日3倍反向科技 ETF (-3x)
    "XLF": "FAZ",  # Direxion 每日3倍反向金融 ETF (-3x)
    "XLE": "ERY",  # Direxion 每日2倍反向能源 ETF (-2x)
}

# 個股槓桿倍率動態選擇門檻：is_structural_breakdown 與 is_whale_sto_block 「雙重
# 確認」同時成立時，視為高信心度空頭情境，優先採用 "2x" 商品（若該標的有收錄）；
# 僅單一條件成立時，採用槓桿較低的 "1x" 商品，降低槓桿反向ETP的波動耗損風險。
_INVERSE_HEDGE_HIGH_CONVICTION_LEVERAGE_TIER: str = "2x"
_INVERSE_HEDGE_DEFAULT_LEVERAGE_TIER: str = "1x"

# 反向ETF現貨動能確認 (inverse_hedge.py::confirm_inverse_hedge_spot_momentum) 具名
# 門檻：刻意不查詢選擇權鏈，僅用現貨技術面 (RSI + 短期均線 + 成交額流動性) 做最後
# 一道確認，任何資料不足/例外一律 fail-closed 回傳 False，呼叫端退回既有 BOXX 行為。
_INVERSE_HEDGE_HISTORY_PERIOD: str = "3mo"  # 取歷史日K的區間長度
_INVERSE_HEDGE_MA_LOOKBACK: int = 10  # 短期均線回看天數
_INVERSE_HEDGE_VOLUME_LOOKBACK_BARS: int = 20  # 平均成交額回看天數
_INVERSE_HEDGE_RSI_BULLISH_THRESHOLD: float = 50.0  # RSI14 需高於此值視為動能偏多
_INVERSE_HEDGE_MIN_ADV_USD: float = (
    5_000_000.0  # 最低日均成交額門檻(美元)，避免推薦流動性過薄的反向ETF
)

# --- 微觀結構出場決策矩陣 (anti_washout.py 決策矩陣) 具名常數 ---
# 取代舊版 Euphoria 90/10 二分獲利了結與 1.5x ATR Track 1 停損，Scenario 3
# (check_satellite_rebalancing) 對既有 SATELLITE 部位的止盈/止損判定改由此組
# 4 層 SL + 3 層 TP 矩陣決定；Track 2 極端瞬時停損 (_ANTI_WASHOUT_EXTREME_ATR_MULT)
# 為黑天鵝最後防線，維持不變、不受本矩陣影響。
_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT: float = (
    0.5  # SL-結構失效：Track 1 停損 = anchor_base - 此值 × ATR_15m（取代舊版 1.5x）
)
_MICROSTRUCTURE_SL_NET_GEX_THRESHOLD: float = (
    0.0  # SL-狀態翻轉：個股 Net GEX <= 此值視為做市商避險邏輯消亡
)
_MICROSTRUCTURE_SL_WHALE_PUT_MIN_NOTIONAL_USD: float = (
    500_000.0  # SL-主力對沖：單筆近平值 PUT BTO 最低權利金名目金額
)
_MICROSTRUCTURE_SL_WHALE_PUT_MIN_RATIO: float = (
    1.5  # SL-主力對沖：單筆 PUT BTO 最低 ratio (Volume/OI)
)
_MICROSTRUCTURE_SL_WHALE_PUT_NEAR_ATM_PCT: float = 0.05  # SL-主力對沖：近平值判定容差，沿用 _ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT 既有 5% 慣例
_MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT: float = (
    0.5  # SL-動態保本：現價漲幅達距 Call Wall 空間此比例時，停損上移至保本點
)
_MICROSTRUCTURE_TP1_CALLWALL_PCT: float = (
    0.995  # TP1-阻力初探：現價 >= Call Wall 此比例即視為觸及阻力
)
_MICROSTRUCTURE_TP1_RATIO: float = 0.5  # TP1 執行比例 (50%)
_MICROSTRUCTURE_TP2_WALL_BREAK_PCT: float = (
    0.015  # TP2-空間擴展：穿越 Call Wall 幅度門檻（v1 僅實作此子條件，
    # 「新舊 Call Wall 轉移點」比對需要跨週期快照，列為後續 fast-follow）
)
_MICROSTRUCTURE_TP2_RATIO: float = 0.3  # TP2 執行比例 (30%)
_MICROSTRUCTURE_TP3_DELTA_THRESHOLD: float = (
    0.85  # TP3-終局平倉：期權 Delta >= 此值視為趨勢耗竭 (深實值 Pinning 風險)
)
_MICROSTRUCTURE_TP3_DTE_THRESHOLD: int = (
    5  # TP3-終局平倉：DTE <= 此值視為末日 Theta 耗損風險（dte<=1 已由既有
    # EXPIRATION_SETTLEMENT_ALERT 強制結算保護接管，故此處實際生效區間為 1<dte<=5）
)
_MICROSTRUCTURE_TP3_RATIO: float = 0.2  # TP3 執行比例 (20%)

# --- 左側六重嚴格過濾鐵律 (left_side_entry.py::_confirm_left_entry_signal) 具名常數 ---
# 逆勢均值回歸／做市商 Put Wall 底牆接刀，結構完全比照上方右側六重鐵律
# (opportunity_cost.py) 的組裝方式，僅技術定義方向相反（右側要求收盤站上
# 突破線；左側要求深跌破 VWAP 且密著 Put Wall），故同一時間點一檔標的
# 技術上幾乎不可能同時滿足兩套條件一。
# 條件一：結構性空頭力竭與極值乖離確認
_LEFT_ENTRY_VWAP_ATR_MULT: float = 1.5  # 極度負乖離：Spot <= VWAP - 此倍數 × ATR₁₅ₘ
_LEFT_ENTRY_RSI_MAX: float = 30.0  # 15m RSI <= 此值視為超賣
_LEFT_ENTRY_PIN_BAR_WICK_RATIO: float = (
    1.5  # 下影線長度 >= 實體 × 此倍數 視為錘頭/Pin Bar
)
# 蜻蜓十字 (Dragonfly Doji) 判定：實體趨近於零時，錘頭的「下影線 >= 實體 × 1.5」
# 比例判定會退化為恆真 (任何數 >= 0)，若不額外要求 body > 0，連墓碑十字 (長上影、
# 無下影) 都會被誤判為錘頭。但 body > 0 這道防呆同時也把「實體為零 + 長下影」這個
# 教科書級的底部反轉訊號整個排除，故改以下列兩個「佔全距比例」門檻獨立判定。
_LEFT_ENTRY_DOJI_BODY_RANGE_RATIO: float = 0.1  # 實體 <= 全距 × 此比例 視為十字
_LEFT_ENTRY_DRAGONFLY_WICK_RANGE_RATIO: float = 0.6  # 下影線 >= 全距 × 此比例 視為蜻蜓
_LEFT_ENTRY_VOLUME_EXHAUST_MULT: float = 0.7  # 縮量窒息門檻 (<= 前20根均量 × 此倍數)
_LEFT_ENTRY_VOLUME_PANIC_MULT: float = 2.0  # 恐慌吸收門檻 (>= 前20根均量 × 此倍數)
_LEFT_ENTRY_CAPITULATION_RANGE_RATIO: float = (
    0.1  # (close-low)/(high-low) < 此值 視為「大陰線實體灌破 (Close≈Low)」
)
# 條件二：做市商 Put Wall / 負 Gamma 吸附牆密著截擊
_LEFT_ENTRY_PUT_WALL_LOWER_PCT: float = (
    -0.01
)  # (spot-put_wall)/spot 下界 (允許微幅穿刺)
_LEFT_ENTRY_PUT_WALL_UPPER_PCT: float = 0.015  # (spot-put_wall)/spot 上界
# ⚠️ 資料缺口代理值：文獻規格要求「Put OI 名目價值 >= $1,000,000,000」，但現有
# GEX 爬蟲資料 (fetch_symbol_gex_metrics) 完全沒有「每履約價 OI 名目金額」欄位，
# 僅有 Net GEX 曝險值。改用該履約價絕對 GEX 曝險量級是否超過此代理門檻，比照
# 既有 GEX_THIN_WALL_THRESHOLD (500k) 薄紙牆判定慣例並取整數量級上調，作為
# 「防禦厚度」的近似代理，非真實 OI 名目金額。呈現層需依 AGENTS.md「啟發式
# 代理數據揭露」慣例，在對應欄位附近標註此為代理值。
_LEFT_ENTRY_PUT_WALL_GEX_PROXY_THRESHOLD: float = 5_000_000.0
# 條件三：下檔無恐慌踩踏斷崖 + 向上均值回歸空間
_LEFT_ENTRY_UOA_CHASE_RATIO_THRESHOLD: float = 1.2  # 追空踩踏 PUT BTO 的 ratio 門檻
_LEFT_ENTRY_UOA_CHASE_MIN_PREMIUM_USD: float = (
    200_000.0  # 追空踩踏 PUT BTO 的最低權利金
)
_LEFT_ENTRY_ASYMMETRIC_ROOM_PCT: float = 0.035  # 向上均值回歸空間門檻 (3.5%)
# ⚠️ 校準備註：文獻規格宣稱此 3.5% 是「在停損設於 Put Wall 下方 1% 的前提下，
# 隱含風報比達 3:1 以上」，但該推導只在現價幾乎正好貼齊 Put Wall 時成立。
# 令 d = (Spot-PutWall)/Spot、停損 = PutWall × 0.99，則風險 = 0.01 + 0.99d：
#   d = 0      → 風險 1.00%  → R:R 3.50 ✅
#   d = +1.5%  → 風險 2.49%  → R:R 1.41 ❌ (條件二允許的上界)
#   d = -1.0%  → 風險 0.01%  → 停損落在進場價下方 0.01%，數學上退化
# 即 R:R >= 3 僅在 d <= 0.168% 時成立。本引擎目前**沒有**實作那個
# 「PutWall 下方 1%」的停損 (左側部位仍走 anti_washout.py 的錨點停損體系)，
# 故此處僅是門檻本身；若日後真要實作該停損並保證整個密著帶都有 3:1，需將本
# 門檻提高至約 7.5%、或把 _LEFT_ENTRY_PUT_WALL_UPPER_PCT 收斂至 0.17% 左右。
# 條件四：主力大額 PUT STO 接刀或長天期 CALL BTO 佈局
_LEFT_ENTRY_PUT_STO_MIN_DTE: int = 14
_LEFT_ENTRY_PUT_STO_MIN_RATIO: float = 1.0
_LEFT_ENTRY_PUT_STO_MIN_NOTIONAL_USD: float = 300_000.0
_LEFT_ENTRY_CALL_BTO_MIN_DTE: int = 30
_LEFT_ENTRY_CALL_BTO_MIN_RATIO: float = 0.8
_LEFT_ENTRY_CALL_BTO_MIN_NOTIONAL_USD: float = 200_000.0
# 條件五：總經流動性危機與財報黑天鵝安全閥 (疊加在重用的右側條件五之上)
_LEFT_ENTRY_VTS_BACKWARDATION_RATIO: float = (
    1.10  # front-month VIX 溢價 3-month 超過此比例 (vts_ratio) 視為倒掛防禦
)
# 條件六：Candidate 自身 Theta 磨底防禦
_LEFT_ENTRY_CANDIDATE_MIN_DTE: int = 21  # 嚴禁 0~7 DTE 合約，左側需承受底部震盪整理期
_LEFT_ENTRY_IVR_SPREAD_THRESHOLD: float = (
    50.0  # IVR > 此值強制改 Bull Call Spread/Short Put，避免恐慌插針時高買隱波
)

# --- 動態調整 4 態 Regime Classifier (regime_classifier.py::classify_dynamic_regime) 具名常數 ---
# Regime I 左側接刀態：門檻與上方 _LEFT_ENTRY_VWAP_ATR_MULT/_LEFT_ENTRY_RSI_MAX/
# _LEFT_ENTRY_PUT_WALL_*_PCT 刻意分開命名而不合併重用——本組是「是否進入左側
# 接刀盤勢」的較寬鬆分類門檻，_LEFT_ENTRY_* 是「六重鐵律本身」的進場確認門檻，
# 語意不同（前者決定路由，後者決定是否真的允許下單），數值目前恰好相同純屬
# 校準巧合，未來可能各自獨立調整。
_REGIME_I_VWAP_ATR_MULT: float = 1.5
_REGIME_I_RSI_MAX: float = 30.0
_REGIME_I_PUT_WALL_LOWER_PCT: float = -0.01
_REGIME_I_PUT_WALL_UPPER_PCT: float = 0.015
# Regime III 右側動能態
_REGIME_III_CALL_WALL_MIN_ROOM_PCT: float = 0.05
_REGIME_III_SUPPORT_WALL_MAX_DIST_PCT: float = 0.05
_REGIME_III_RSI_MIN: float = 55.0
_REGIME_III_VOLUME_SURGE_MULT: float = 1.5
# Regime IV 結構封頂／危機態 (最優先判定，全面鎖定態)
_REGIME_IV_CALL_WALL_PROXIMITY_PCT: float = 0.05
# VIX 期限結構深度倒掛 (front-month 溢價於 3-month 超過 10%)。與左側條件五的
# _LEFT_ENTRY_VTS_BACKWARDATION_RATIO 數值相同但刻意分開命名：前者決定「盤勢
# 是否已進入全面鎖定態」(路由層)，後者是「六重鐵律本身是否放行」(進場確認層)，
# 語意不同，未來可各自獨立調整。
_REGIME_IV_VTS_BACKWARDATION_RATIO: float = 1.10

# --- 動態調整狀態切換引擎 (transition_engine.py::evaluate_transition_for_position) 具名常數 ---
# 只剩路徑 1：Regime 僅負責進場權限，部位出場一律回歸 anti_washout.py 的
# 微觀結構出場決策矩陣，故原路徑 2/4 的門檻常數已隨該邏輯一併移除。
# 僅接管使用者透過 /add_trade、/add_holding 手動標記 dynamic_strategy_state 的
# 部位；未標記部位完全不受影響，仍走既有 anti_washout.py 通用 SL/TP 矩陣。
# 切換路徑 1：左側部位進化為右側動能倉 (加碼 + 停損上移保本)
_TRANSITION_PATH1_VWAP_VOLUME_MULT: float = (
    1.5  # 15m 收盤站上 VWAP 與 Gamma Flip 同時須伴隨量能放大達此倍數，方視為真突破
)
# 注意：實際的量能比較發生在 cogs/trading/portfolio_monitor.py::_build_symbol_metrics
# (與 TP3 的 vwap_loss_with_volume 共用同一次 get_confirmed_15m_bar 呼叫結果)，
# 結果以 metrics["vwap_reclaim_with_volume"] 布林值傳入 transition_engine.py；
# 後者只在文案中引用本常數。調整此值時請一併確認該處。
