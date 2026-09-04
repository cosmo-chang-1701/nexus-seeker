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
#           放量 + 個股淨 GEX 須為 LONG_GAMMA)。比照分析中心
#           (cogs/embed_builders/portfolio_embeds.py::create_tactical_symbol_embed())
#           的判讀方式：陰線放量或 SHORT_GAMMA 泥淖下的放量視為空頭摜壓，不算
#           右側突破，即便收盤價與量能兩項代數條件皆達標仍判定未通過。
#   條件二：做市商正 Gamma 底牆完好 (支撐牆存在且現價站上)
#   條件三：做市商阻力結構與非對稱空間 (無 UOA 物理封頂，Call Wall 空間充足)。
#           Call Wall 距現價空間% 比照分析中心同一函式的 GEX CallWall 欄位，
#           用帶正負號的距離 (call_wall - spot) / spot 判定，不要求 Call Wall
#           必須還在現價之上——現價已觸及/跌破 Call Wall (負距離) 同樣視為
#           空間不足，而非誤判為「已站上、無封頂」。
#   條件四：主力跨週期買盤認證與雜訊過濾 (BTO Call，DTE 與 ratio 雙門檻)
#   條件五：二元宏觀與財報事件安全閥
#   條件六：candidate 自身最近效期選擇權週期雜訊過濾 (避開 0/1 DTE)
_ENTRY_VOLUME_LOOKBACK_BARS: int = 20  # 條件一：15m 成交量基準所需回看根數 (不含確認根)
_ENTRY_VOLUME_SURGE_MULTIPLIER: float = (
    1.5  # 條件一：「放量」門檻，須達回看均量的 1.5 倍
)
_ENTRY_UOA_CAP_RATIO_THRESHOLD: float = (
    1.0  # 條件三：單筆 STO Call 視為物理封頂的 ratio (volume/OI) 門檻
)
_ENTRY_ASYMMETRIC_ROOM_PCT: float = (
    0.05  # 條件三：Call Wall 距現價須保留的最低非對稱獲利空間 (帶正負號距離)
)
_ENTRY_UOA_MIN_DTE: int = 7  # 條件四：驅動進場的主力 UOA 買盤最低 DTE 要求
_ENTRY_UOA_MIN_RATIO: float = (
    0.8  # 條件四：驅動進場的主力 UOA 買盤最低 ratio (volume/OI) 要求
)
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
