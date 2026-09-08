# 🌌 Nexus Seeker 動態轉倉引擎 (Dynamic Rollover Engine) 詳細邏輯與演算法技術規格說明書

本文件基於對系統程式碼庫的完整審查與實作代碼驗證（核心位於 [`nexus_core/market_analysis/dynamic_rollover/`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover) 及調度層 [`nexus_core/cogs/trading/portfolio_monitor.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/cogs/trading/portfolio_monitor.py)），全面梳理專案中動態轉倉的架構圖解、數學公式、決策邊界與演算法邏輯。

---

## 目錄
1. [系統總覽與設計哲學](#一系統總覽與設計哲學)
2. [架構概覽與模組職責](#二架構概覽與模組職責)
3. [八大轉倉情境詳細邏輯與演算法](#三八大轉倉情境詳細邏輯與演算法)
   - [情境 1：原型假設破滅 (Fundamental Thesis Broken)](#情境-1原型假設破滅-fundamental-thesis-broken)
   - [情境 2：機會成本與期望值比對 (Opportunity Cost & EV Comparison)](#情境-2機會成本與期望值比對-opportunity-cost--ev-comparison)
   - [情境 3：核心衛星再平衡與防洗盤動態停損 (Satellite Rebalance & Anti-Washout Stop)](#情境-3核心衛星再平衡與防洗盤動態停損-satellite-rebalance--anti-washout-stop)
   - [情境 4：槓桿與保證金防禦 (Leverage & Margin Defense)](#情境-4槓桿與保證金防禦-leverage--margin-defense)
   - [情境 5：核心資金部署與 Covered Call Overlay (Core Deployment)](#情境-5核心資金部署與-covered-call-overlay-core-deployment)
   - [情境 6：宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)](#情境-6宏觀逃頂前瞻防禦-macro-top-escape-anticipatory-defense)
   - [情境 7：Covered Call 權利金衰減停利 (Covered Call Profit-Lock)](#情境-7covered-call-權利金衰減停利-covered-call-profit-lock)
   - [情境 8：動態調整狀態切換引擎 (Transition Engine)](#情境-8動態調整狀態切換引擎-transition-engine)
4. [交易策略路由器與進場鐵律體系](#四交易策略路由器與進場鐵律體系)
5. [底層微結構拓撲與共用訊號模組](#五底層微結構拓撲與共用訊號模組)
6. [調度管線、去重機制與審計軌跡](#六調度管線去重機制與審計軌跡)
7. [代碼邊界、限制與未落地功能追蹤](#七代碼邊界限制與未落地功能追蹤)

---

## 一、系統總覽與設計哲學

Nexus Seeker 是面向 Discord 的期權風控與交易營運平台，專為 **低 RAM (1GB RAM VPS) 環境** 優化。動態轉倉引擎（Dynamic Rollover Engine）的設計貫徹以下核心原則：

1. **Cache-Aside 與零盤中阻塞**：
   - 盤中即時掃描嚴禁全量重新抓取與計算全市場選擇權鏈。
   - 所有期權 Greeks、GEX 輪廓（GEX Profile）、Max Pain、Expected Move 與 Skew 均由每日 08:45 ET 的盤前預熱任務批次計算並存入 SQLite `market_cache`。盤中每 15 分鐘的持倉監控優先消費快取，無快取時才進行單標的 Cache-Aside 補算。
2. **純量化演算法分流 + 選擇性 LLM**：
   - 盤中 15 分鐘的主流轉倉決策（情境 2 至 8）**100% 依賴純量化指標、做市商微觀結構 (GEX/VWAP/ATR) 與技術指標**，單次判定延遲 $< 5\text{ms}$。
   - 僅情境 1（基本面原型假設破滅）涉及 LLM 推理，且與盤中 15 分鐘調度徹底解耦：在 08:00 ET 批次執行或手動執行，寫入 SQLite `fundamental_cache` 後，盤中僅讀取快取標記進行攔截。
3. **微觀結構物理約束 (Microstructure Physical Constraints)**：
   - 徹底摒棄單純人為百分比停損，全面改採做市商 GEX 牆壁（Call Wall / Put Wall / Support Wall）、成交量控制點（HVN / LVN）、Gamma Flip 零臨界線與 ATR 波動率緩衝。
   - 強制約束支撐牆必須位於現價下方（$K < \text{Spot}$），杜絕上方阻力牆被誤認為底牆的拓撲逆轉缺陷。
4. **型別安全與向後相容 (`TypedDict`)**：
   - 轉倉指令採用 `RolloverInstruction(TypedDict)`，兼顧靜態型別安全，並相容下游消費端與測試套件既有的 `ins["key"]` 字典存取語法。

---

## 二、架構概覽與模組職責

```
                      ┌────────────────────────────────────────────────┐
                      │              DynamicRolloverEngine             │
                      │  (Facade in market_analysis/dynamic_rollover)  │
                      └───────┬────────────────────────────────┬───────┘
                              │                                │
    ┌─────────────────────────┴───────────────┐   ┌────────────┴──────────────────────────┐
    │           Mixins (情境執行層)            │   │          Auxiliary Core (輔助層)       │
    ├─────────────────────────────────────────┤   ├───────────────────────────────────────┤
    │ 1. fundamental_thesis.py (情境 1)       │   │ • structural_signals.py (微結構拓撲)  │
    │ 2. opportunity_cost.py (情境 2)         │   │ • regime_classifier.py (4-Regime 路由)│
    │ 3. anti_washout.py (情境 3)             │   │ • left_side_entry.py (左側六重鐵律)   │
    │ 4. margin_defense.py (情境 4)           │   │ • transition_engine.py (情境 8 進化)  │
    │ 5. core_deployment.py (情境 5 + Overlay)│   │ • inverse_hedge.py (反向 ETF 路由)    │
    │ 6. macro_top_escape_defense.py (情境 6) │   │ • constants.py / models.py / _shared.py│
    │ 7. covered_call_profit_lock.py (情境 7) │   │                                       │
    └─────────────────────────────────────────┘   └───────────────────────────────────────┘
```

### 核心模組分工表
| 檔案模組 | 角色與職責 |
| :--- | :--- |
| [`__init__.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/__init__.py) | **Facade 入口**：組合六大 Mixin 類別形成 `DynamicRolloverEngine`，並維持舊版 patch 向後相容。 |
| [`models.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/models.py) | **型別定義**：定義 `RolloverScenario` (8大枚舉)、`RolloverInstruction` (TypedDict)、`FundamentalThesisResult`、`TradingStrategyMode` 與 `DynamicRegime`。 |
| [`constants.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/constants.py) | **量化常數庫**：定義所有門檻常數（EV Spread、動能閾值、摩擦成本、ATR 緩衝倍數、各情境轉倉比例等）。 |
| [`structural_signals.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/structural_signals.py) | **微結構共用拓撲**：做市商 GEX 牆掃描、物理約束、薄紙牆過濾、主力大單偵測、短 TTL BoundedCache (256筆)。 |
| [`anti_washout.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/anti_washout.py) | **情境 3 核心**：雙軌防洗盤動態停損演算法、微觀結構出場無狀態階梯、委託單淨額扣抵、稅務提示。 |
| [`opportunity_cost.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py) | **情境 2 核心**：PowerSqueeze 0-100 正規化對照表、Skew-Adjusted EV 模型、右側進場六重鐵律。 |
| [`regime_classifier.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/regime_classifier.py) | **市場環境分類器**：將標的微觀結構分為 4 態 Regime，並進行策略路由分流。 |
| [`left_side_entry.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/left_side_entry.py) | **逆勢進場核心**：左側接刀均值回歸六重鐵律與做市商 Put Wall 密著截擊。 |
| [`margin_defense.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/margin_defense.py) | **情境 4 核心**：系統性流動性危機下的個股結構無勝率清倉與 CASH/反向 ETF/BOXX 路由。 |
| [`core_deployment.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/core_deployment.py) | **情境 5 核心**：核心標的超額資金部署 (50% 部署 / 50% 緩衝) 與 SPX 負 Gamma 泥淖之 Covered Call Overlay。 |
| [`macro_top_escape_defense.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/macro_top_escape_defense.py) | **情境 6 核心**：宏觀五因子共振逃頂評分，有界 25% 減碼轉入 BOXX。 |
| [`covered_call_profit_lock.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/covered_call_profit_lock.py) | **情境 7 核心**：既有空頭 CALL 合約的時間價值衰減停利（50%/80% BTC 回補）。 |
| [`transition_engine.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/transition_engine.py) | **情境 8 核心**：左側部位帶量突破後的保本上移與第二筆金字塔加碼授權。 |

---

## 三、八大轉倉情境詳細邏輯與演算法

### 情境 1：原型假設破滅 (Fundamental Thesis Broken)
- **代碼位置**：[`fundamental_thesis.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/fundamental_thesis.py)
- **調度機制**：非同步快取解耦。
  - 每日 08:00 ET 由 [`fundamental_filing_monitor.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/cogs/trading/fundamental_filing_monitor.py) 針對目前「持有標的 (Holdings-only)」批次比對 SEC EDGAR submissions API 最新 `accession_number`，或由使用者執行 `/verify_thesis <symbol>` 手動觸發。
  - 評估結果寫入 SQLite `fundamental_cache`（包含 `is_broken`, `confidence`, `reasoning`, `filing_accession`）。
  - 盤中每 15 分鐘的 `portfolio_monitor_task` 僅以唯讀方式檢查 `fundamental_cache`，零即時 LLM 運算延遲。
- **結構化分析附錄 (Filing Format Adaptation)**：
  - `10-K`：強調全年度長期趨勢，重度權衡 Item 1A Risk Factors 與 Item 7 MD&A。
  - `10-Q`：季度報告噪音多，嚴格要求連續惡化趨勢，單季 miss 不得判定破滅。
  - `8-K`：重大事件驅動。高權重項目包含 Item 2.05（重組/業務退出）、Item 4.02（財報不可信/重編）、Item 5.02（高層非正常解僱）、Item 1.01/1.02（重大合約終止）；低權重為 Item 7.01/8.01。
  - `NEWS`：即時新聞過濾，要求核實官方消息，排除未經證實傳言。
- **LLM 推理架構與嚴格排除條款 (Strict Exclusion Rule)**：
  Prompt 要求模型在 `reasoning` 欄位以繁體中文進行 Chain-of-Thought 推理，區分「總經/週期性逆風 (A)」與「公司個體結構性惡化 (B)」：
  - **嚴格排除條款**：凡因利率、通膨、匯率、產業景氣循環下行、或單季輕微財測不符者，**嚴禁**判定為 `is_broken=true`。
  - 僅當確認公司定價權永久喪失、技術核心落後、重要客戶轉單、市占率持續萎縮時，方判定為 `is_broken=true`。
- **執行指令**：
  若 `is_broken=True` 且 `confidence >= 0.7`，下達 100% 清倉指令（`LIQUIDATE` 轉入 `"VOO"`），並全面封殺所有加碼。

---

### 情境 2：機會成本與期望值比對 (Opportunity Cost & EV Comparison)
- **代碼位置**：[`opportunity_cost.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py)
- **觸發核心三要素**：
  1. 持倉動能衰退：$\text{PowerSqueeze}(\text{holding}) < 20.0$ (`_MOMENTUM_DECAY_THRESHOLD`)。
  2. 候選標的突破待發：$\text{PowerSqueeze}(\text{candidate}) > 80.0$ (`_BREAKOUT_READY_THRESHOLD`)。
  3. 期望值差距覆蓋摩擦成本：
     $$\text{EV Spread} = \text{Adjusted EV}_{\text{candidate}} - \text{Adjusted EV}_{\text{holding}} > 0.05 + \text{friction\_cost\_pct}$$
- **量化演算法公式**：
  1. **PowerSqueeze 0-100 正規化對照表 (`_normalize_power_squeeze`)**：
     根據 `analyze_psq()` 的擠壓強度與動能方向映射為數值分值：
     $$\text{Score} = \begin{cases}
     \text{Release:} & \text{bull}=75, \text{bear}=5, \text{neutral}=10 \\
     \text{Normal:} & \text{bull}=40, \text{bear}=20, \text{neutral}=30 \\
     \text{Mid:} & \text{bull}=70, \text{bear}=45, \text{neutral}=60 \\
     \text{High:} & \text{bull}=90, \text{bear}=10, \text{neutral}=50
     \end{cases}$$
     若偵測到 `is_breakout_long` 則強制截斷為 $\max(\text{Score}, 95.0)$；若 `is_breakout_short` 則截斷為 $\min(\text{Score}, 5.0)$。
  2. **Skew-Adjusted EV 期望值模型 (`_calculate_ev_proxy`)**：
     $$\text{Base EV} = \frac{\text{expected\_move\_upper} - \text{Spot}}{\text{Spot}}$$
     若快取之 Skew 百分位 $< 50.0$（存在下行尾部恐慌風險），施加懲罰折價：
     $$\text{Downside Penalty} = \left(\frac{50.0 - \text{Skew Percentile}}{50.0}\right) \times 0.5$$
     $$\text{Adjusted EV} = \max\left(0.0, \text{Base EV} \times (1.0 - \text{Downside Penalty})\right)$$
  3. **動態摩擦成本 (`friction_cost_pct`)**：
     基準摩擦成本為 $0.3\%$ (`_ESTIMATED_ROUND_TRIP_COST_PCT`)。若候選標的近月 ATM 期權買賣報價可得，自動動態擴展：
     $$\text{friction\_cost\_pct} = \max\left(0.003, \frac{\text{Ask} - \text{Bid}}{\text{Target Spot}} \times 1.5\right)$$
- **轉倉比例與滿載滿配分支**：
  - **一般轉倉**：持倉獲利 $> 30\%$ 轉出 $50\%$ (`_ROLLOVER_RATIO_HIGH_PROFIT`)；一般部位轉出 $30\%$ (`_ROLLOVER_RATIO_STANDARD`)。
  - **極致不對稱勝率（滿載滿配分支）**：
    若候選標的同時滿足：
    - 低 IVR：$0 < \text{Target IVR} < 30.0\%$
    - 密著 Put Wall：$|\text{Spot} - \text{PutWall}| / \text{PutWall} \le 1.0\%$
    - UOA 巨鯨掃貨：`target_uoa_sweep == True`
    策略自動升級為 `Shares + ITM Call`（履約價錨定 $0.95 \times \text{Target Spot}$，約 70Δ, 30-45 DTE），並下達 **100% 滿載轉倉指令**。

---

### 情境 3：核心衛星再平衡與防洗盤動態停損 (Satellite Rebalance & Anti-Washout Stop)
- **代碼位置**：[`anti_washout.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/anti_washout.py)
- **防守錨點微結構拓撲解析 (`_resolve_canonical_anchor_base`)**：
  $$\text{AnchorBase} = \text{SupportWall} \to \min(\text{PutWall}, \text{CallWall}) \text{ [拓撲逆轉修復]} \to \text{PutWall} \to \text{GammaFlip} \to \text{HVN} \to \text{Spot}$$
  - **支撐牆物理約束**：支撐位物理上必須位於現價下方。`_scan_gex_walls` 強制掃描 $K < \text{Spot}$：
    $$\text{Support Wall} = \operatorname{argmax}_{K < \text{Spot}} (\text{Net GEX}(K))$$
    若現價下方無正 GEX 峰值，或最大正 GEX 曝險低於 500k（`GEX_THIN_WALL_THRESHOLD`），直接回傳 0.0，嚴防誤把上方阻力牆當作防守底牆。
- **雙軌防洗盤動態停損演算法 (`_compute_anti_washout_stop`)**：
  1. **Track 1 結構性停損 (Structural Stop)**：
     $$\text{BaseStop} = \text{AnchorBase} - (0.5 \times ATR_{15m})$$
  2. **量價拓撲吸附 (LVN Snapping)**：
     當 $\text{BaseStop}$ 落在低成交量真空區（LVN）$1.5\%$ 容差範圍內時，向下吸附至次級高成交量節點（Secondary HVN）上緣：
     $$\text{AdjustedStop} = \text{Secondary HVN} + (0.2 \times ATR_{15m})$$
     若無次級 HVN 則退回 $LVN - (1.0 \times ATR_{15m})$。
  3. **狀態轉換保本地板銜接**：
     讀取部位 metadata 中的 `ratchet_stop`，$\text{FinalStop} = \max(\text{AdjustedStop}, \text{ratchet\_stop})$。
  4. **建議限價 (Limit Price)**：
     $$\text{LimitPrice} = \max\left(\text{FinalStop} - 0.5 \times ATR_{15m}, \text{FinalStop} \times 0.995\right)$$
  5. **Track 2 極端瞬時停損 (Extreme Tick Breach)**：
     $$\text{ExtremeStop} = \text{AnchorBase} - (3.0 \times ATR_{15m})$$
     現貨與期權皆適用的黑天鵝熔斷防線，現價盤中一旦跌破立即市價平倉，無視 15m 收盤等待。
- **微觀結構出場決策階梯 (`_apply_decision_matrix`)**：
  無狀態、嚴格按優先序執行的分層裁決體系：
  1. **TP 分層 (止盈優先)**：
     - `TP3-終局平倉` (平倉 20%)：期權 Delta $\ge 0.85$、或 15m VWAP 帶量失守、或 $1 < DTE \le 5$。
     - `TP2-空間擴展` (平倉 30%)：現價突破 Call Wall $\ge 1.5\%$，或做市商阻力牆向上遷移 $\ge 3\%$（`_MICROSTRUCTURE_TP2_WALL_MIGRATION_PCT`，透過 Migration `v069` 與 `market_cache.previous_call_wall` 歷史快照追蹤）且現價站穩舊阻力牆。
     - `TP1-阻力初探` (平倉 50%)：現價達 $\text{CallWall} \times 99.5\%$。
  2. **Track 2 極端瞬時停損** (100% 清倉)：
     `spot < extreme_stop_loss`。**受 `not tp_tier` 守衛保護**，確保獲利了結時絕不誤觸緊急紅色熔斷警報。
  3. **期權 IV 驟降快速出場** (100% 清倉)：
     僅適用 OPTIONS 部位，當 $\Delta IVR \ge 20\%$ 立即平倉，杜絕 Vega 暴跌與 Delta 踩踏雙殺。
  4. **SL 分層 (止損次之，100% 強制撤退至 `"VOO"`)**：
     - `SL-結構失效`：跌破 Track 1 防守線（現貨看 15m 收盤，期權看即時 Spot）。
     - `SL-狀態翻轉`：個股 Net GEX 翻負 ($\le 0.0$)，做市商自穩定消亡。
     - `SL-主力對沖`：偵測到近平值單筆大額 PUT BTO（名目金額 $\ge \$500k$，時段正規化 $paced\_ratio \ge 1.5x$）。
     - `SL-動態保本`：現價推進達 AnchorBase 到 CallWall 空間的 $50\%$ 時，發出 HOLD 指令並將停損線移至 $\max(\text{avg\_cost}, \text{AnchorBase})$。
  5. **常規配置超額**：超過最大配置比例執行 `REDUCE`。
  6. **灰階量化裁決**：護城河完好，維持 `HOLD` 續抱（發送安心防守卡）。
- **止損強制回防 VOO vs 獲利高 EV 輪動**：
  - 若觸發 **TP 分層**，`next_target` 呼叫 `_find_best_rollover_target` 尋找下一檔高 EV 候選標的進行資金輪動。
  - 若觸發 **SL 分層**，`next_target` **強制鎖定為 `"VOO"`**，嚴禁在停損時追逐另一檔高波衛星（避免「燙手山芋輪動」擴大虧損）。
- **委託單淨額扣抵演算法 (`_net_against_existing_order`)**：
  查詢使用者掛在券商的待成交 SELL 委託單，若委託股數 $\ge$ 建議賣出股數，系統自動將 `sell_ratio` 歸零並降級為 HOLD 觀察，在 Embed 附加 `♻️ 委託單淨額扣抵` 標籤，杜絕重複下單。
- **三大稅務提示 (Tax Compliance)**：
  - **Wash Sale 警示**：若標的在過去 30 天內曾以虧損平倉，提示洗售規則。
  - **Assignment 風險**：若持倉包含空頭期權且進入 ITM，提示履約指派風險。
  - **長短期資本利得**：依持倉 `acquired_at` 判定持有是否超過 365 天，提供長期 (LTCG) 或短期 (STCG) 稅率警示。

---

### 情境 4：槓桿與保證金防禦 (Leverage & Margin Defense)
- **代碼位置**：[`margin_defense.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/margin_defense.py)
- **觸發雙條件**：
  1. 大盤宏觀風控紅線：`get_market_regime()` 進入 `SHORT_GAMMA_CRITICAL` 或 `SYSTEMIC_LIQUIDITY_CRISIS`。
  2. 帳戶保證金赤字：GTC 買單現金赤字 $>$ 現金儲備（若無 GTC 買單，啟用退化代理：$\text{SATELLITE 總市值} > \text{現金儲備}$）。
- **個股結構無勝率檢核 (`_evaluate_structural_no_edge`)**：
  逐一檢驗每檔 SATELLITE 持倉，僅對確認「結構性破位」或「主力空頭封殺（PUT BTO 巨鯨）」者下達 100% 強制平倉；具備抗跌技術優勢者維持不動。
- **三大去化目的地路由演算法**：
  ```
  系統性風控紅線亮起 + 個股結構無勝率
             │
             ├─ [有真實 GTC 買單赤字?] ─── YES ──> 轉入 "CASH" (立即補足保證金儲備)
             │
             └─ NO (退化代理觸發)
                  │
                  ├─ 解析反向 ETF (個股 -> 指數 -> 產業 -> SH)
                  │    └─ 信心度槓桿選擇: 雙重確認 2x / 單一確認 1x
                  │
                  └─ 純現貨技術動能確認 (RSI>50, Close>10MA, ADV>=$5M)?
                       ├─ 通過 ──> 轉入反向 ETF (方向性對沖)
                       └─ 失敗 ──> 轉入 "BOXX" (純防禦收息，絕不轉入 VOO)
  ```
- **委託單矛盾預警**：若偵測到該標的掛有現有 GTC 買入網格單，於內文明確標註警示，要求使用者手動取消反向委託。

---

### 情境 5：核心資金部署與 Covered Call Overlay (Core Deployment)
- **代碼位置**：[`core_deployment.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/core_deployment.py)
- **分支 A：核心資金超額部署 (`evaluate_core_deployment`)**：
  - **嚴格 Opt-in 門檻**：使用者必須透過 `/edit_holding` 明確設定過 `target_allocation_pct`（未設定者預設 `max_alloc=1.0`，永不主動修剪）。
  - **超額門檻**：$\text{當前配置} - \text{target\_allocation\_pct} > 0.5\%$ (`_CORE_EXCESS_MIN_TRADE_PCT`)。
  - **路徑分流**：
    - `boxx_allocation_pct >= 50.0` **防禦分支**：超額資金 100% 部署至 `"BOXX"` 鎖定利息，無需等待候選標的。
    - `boxx_allocation_pct < 50.0` **機會分支**：候選標的必須通過進場六重鐵律確認（複用 Scenario 2 計算結果）。通過後**僅動用超額資金的 50% (`_CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO`)** 進行部署，剩餘 50% 留存為現金緩衝。
- **分支 B：Covered Call Overlay (`evaluate_covered_call_overlay`)**：
  - **觸發時機**：大盤 Regime 為 NORMAL，但 SPX 受制於上方負 Gamma 泥淖與 STO 封頂（`get_spx_capped_from_above_signal`）。
  - **持股門檻**：CORE 資產持股 $\ge 100$ 股（不要求 `target_allocation_pct` opt-in）。
  - **動作**：推薦賣出 1 口 OTM Covered Call（DTE 18-25），履約價下限取 $\max(\text{avg\_cost}, \text{SPY swamp\_strike})$。發送 `action="HOLD"` 的專屬 Embed（`create_covered_call_overlay_embed`），不賣出任何既有現貨股票。

---

### 情境 6：宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)
- **代碼位置**：[`macro_top_escape_defense.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/macro_top_escape_defense.py)
- **定位**：六大情境中唯一的「純領先機率評分防禦」，排在調度佇列最末位（3 $\to$ 2 $\to$ 5 $\to$ 4 $\to$ 6），絕不搶先任何已確定的技術破位指令。
- **三道必要閘門**：
  1. 使用者設定開啟：`user_settings.enable_macro_top_escape_defense == True`。
  2. 宏觀五因子評分達 `CRITICAL`（至少 3 項共振）：
     - VIX 期限結構倒掛（$vts\_ratio \ge 1.0$）
     - CNN Fear & Greed 極度貪婪（$\ge 75$）
     - FedWatch 鷹派定價
     - 大盤負 Gamma 踩踏模式
     - 衛星持倉亢奮廣度（觸及 Call Wall 或 Skew 百分位 $\le 20$ 的持倉比例）
  3. 排除 `already_flagged_symbols`。
- **動作**：對 SATELLITE 部位進行 $25\%$ (`_MACRO_TOP_ESCAPE_TRIM_RATIO`) 的輕量防禦性減碼轉入 `"BOXX"`，保留 75% 曝險，兼顧逃頂防護與抗踏空。

---

### 情境 7：賣方期權權利金衰減停利 (Short Option Profit-Lock)
- **代碼位置**：[`covered_call_profit_lock.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/covered_call_profit_lock.py)
- **定位**：專門處理既有空頭期權部位（`quantity < 0`，包含 Covered Call 與 Cash-Secured Put, CSP），獨立於轉倉體系外，專注於提前 BTC（Buy To Close）回補了結。
- **判定門檻**：
  $$\text{Decay \%} = \frac{\text{Entry Premium} - \text{Current Premium}}{\text{Entry Premium}}$$
  - **末日結算保護**：若 $DTE \le 1$，無論衰減幅度或報價狀況，強制 100% BTC 回補。
  - **全額停利**：$\text{Decay \%} \ge 80\%$ (`_COVERED_CALL_PROFIT_LOCK_FULL_DECAY_PCT`)，建議 100% BTC 回補鎖定時間價值收益（若為 CSP 則提示釋放 100% 現金擔保金）。
  - **局部停利**：$\text{Decay \%} \ge 50\%$ (`_COVERED_CALL_PROFIT_LOCK_PARTIAL_DECAY_PCT`)，建議 50% BTC 回補（若為 CSP 則提示釋放 50% 現金擔保金）。
- **專屬去重與 Embed**：去重鍵附加履約價、到期日與 `opt_type`（`{symbol}_{instrument}_{scenario}_{action}_{date}_{strike}_{expiry}_{opt_type}`），確保同標的之 Short Call 與 Short Put 獨立提醒互不干擾；渲染專屬 Embed（`create_covered_call_profit_lock_embed` / `create_short_option_profit_lock_embed`），清晰呈現預估釋放之現金擔保金。

---

### 情境 8：動態調整狀態切換引擎 (Transition Engine)
- **代碼位置**：[`transition_engine.py`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/transition_engine.py)
- **職責界線 (Boundary)**：Regime 只負責「環境識別與進場權限」，部位存亡一律交由微觀結構出場階梯（`anti_washout.py`）。因此本引擎僅保留**路徑 1：左側部位進化為右側動能倉**。
- **進化邏輯**：
  針對在 Regime I 建立且標記為 `entry_mode="DYNAMIC"` 的部位，若 15m 收盤帶量（$\ge 1.5\times$ 均量）站穩 Session VWAP 與 Gamma Flip：
  1. 發出 `action="HOLD"` 指令，停損上移至保本點：$\text{new\_stop} = \max(\text{avg\_cost}, \text{AnchorBase})$。
  2. 發出 `action="OPEN_PYRAMID"` 建議，**授權開立第二筆短天期（DTE 7-21）右側動能加碼部位**。
- **非同步狀態提交安全設計**：
  上移狀態（`{"ratchet_applied": True, "pyramided": True, "ratchet_stop": new_stop}`）掛載於指令的 `dynamic_state_patch`，只有當 Discord DM 真正投遞成功後（`_delivered == True`），才寫回 SQLite `assets.metadata`，杜絕因通知抑制或 Dry-run 導致狀態永久被燒毀。

---

## 四、交易策略路由器與進場鐵律體系

在 Scenario 2（機會成本轉倉）與 Scenario 5（核心資金部署機會分支）中，候選標的必須通過相應的進場鐵律驗證。系統提供三種模式：

```
                      ┌─────────────────────────────────┐
                      │    user_settings.trading_strategy│
                      └────────────────┬────────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         ▼                             ▼                             ▼
   [RIGHT_SIDE]                   [LEFT_SIDE]                    [DYNAMIC]
(順勢突破六重鐵律)            (逆勢均值回歸六重鐵律)         (4-Regime 分類路由器)
                                                                     │
                                      ┌──────────────────────────────┼──────────────────────────────┐
                                      ▼                              ▼                              ▼
                                 [Regime IV]                    [Regime III]                   [Regime I]
                             (封頂/危機: 全面鎖定)           (右側動能態: 順勢突破)         (左側接刀態: 逆勢回歸)
                                                                                                    │
                                                                                       (零重複 I/O 重用 K線/VWAP/ATR)
```

### 1. 右側順勢突破六重鐵律 (`_confirm_entry_signal`)
1. **結構性放量突破**：15m 實體陽線（`close > open`）收盤站穩 Gamma Flip，且成交量 $\ge 1.5\times$ 前 20 根均量，且站穩 Session VWAP。
   - *Gamma Flip 邊界處理*：若無零交叉點且動態 Net GEX < 0，直接判定空頭不通過；若 Net GEX > 0（做市商自穩定），啟用 Fallback 門檻：$\text{Session VWAP} + 0.5 \times ATR_{15m}$。
2. **正 Gamma 支撐牆完好**：現價位於支撐牆上方，且距離在 $(0, 5\%]$ 內。支撐牆強制限定在現價下方（$K < \text{Spot}$），且 GEX 深度 $\ge 500k$。
3. **上方非對稱空間充足**：Call Wall 距現價帶正負號空間 $\ge 5\%$，且 Call Wall 上方無 ratio $\ge 1.5$ 的單筆 STO Call 物理封頂。
4. **主力跨週期買盤認證**：存在 UOA CALL BTO，同時滿足 DTE $\ge 7$、ratio $\ge 0.8$、權利金名目金額 $\ge \$200k$、履約價 $\ge$ 現價。
5. **總經與財報安全閥**：大盤非負 Gamma 踩踏模式，且避開 3 天內財報。
6. **合約週期雜訊過濾**：標的最近期權到期日 DTE $> 1$。

### 2. 左側逆勢均值回歸六重鐵律 (`_confirm_left_entry_signal`)
1. **空頭力竭與極值負乖離**：$Spot \le \text{Session VWAP} - 1.5 \times ATR_{15m}$、15m RSI $\le 30$、出現止跌 K 線形態（錘頭/蜻蜓十字/連續 2 根收窄孕線）、成交量縮量 $\le 0.7\times$ 均量或恐慌吸收 $\ge 2.0\times$ 且非大陰線灌破（收盤貼近低點 $< 10\%$）。
2. **做市商 Put Wall 密著截擊**：現價距 Put Wall 在 $[-1.0\%, +1.5\%]$ 內，且 Put Wall 絕對 GEX 曝險代理量級 $\ge \$5M$。
3. **下檔無追空踩踏 + 向上空間**：無主力追空 PUT BTO（ratio $\ge 1.2$, 金額 $\ge \$200k$），且向上至 $\min(\text{VWAP}, \text{GammaFlip})$ 空間 $\ge 3.5\%$。
4. **主力吸收認證**：PUT STO（DTE $\ge 14$, ratio $\ge 1.0$, 金額 $\ge \$300k$）或長天期 CALL BTO（DTE $\ge 30$, ratio $\ge 0.8$, 金額 $\ge \$200k$）。
5. **總經/財報安全閥 + VIX 期限結構**：避開財報/負 Gamma，且 $vts\_ratio < 1.10$（無深度倒掛）。
6. **Theta 磨底防禦**：近期合約 DTE $\ge 21$；若 IVR $> 50\%$ 強制建議 Bull Call Spread / Short Put 替代單腳買方。

### 3. 動態 4-Regime 分類器 (`classify_dynamic_regime`)
- **Regime IV (結構封頂／危機態，最優先)**：大盤危機、VIX 深度倒掛 ($vts \ge 1.10$)、Call Wall 空間不足 5% 或 STO 封頂 $\implies$ 全面鎖定，禁止開倉。
- **Regime III (右側動能態)**：突破 Gamma Flip 與 VWAP、RSI $> 55$、放量陽線 $\implies$ 路由至右側六重鐵律。
- **Regime I (左側接刀態)**：深跌破 VWAP 1.5x ATR、RSI $\le 30$、密著 Put Wall $\implies$ 路由至左側六重鐵律（原樣轉交已抓取的 `df_15m`、`session_vwap`、`atr_15m` 快照，零重複網路請求）。
- **Regime II (混沌泥淖態，Fallback)**：無人區過渡震盪 $\implies$ 全系統休眠觀望。

---

## 五、底層微結構拓撲與共用訊號模組

1. **做市商 GEX 牆物理掃描 (`_scan_gex_walls`)**：
   - 傳入現價時，支撐牆掃描範圍強制限定在 $K < \text{Spot}$。
   - 呼叫 `classify_gex_wall` 判定，若最大正 GEX 曝險 $< 500k$（`GEX_THIN_WALL_THRESHOLD`），分類為 `THIN_SUPPORT_WALL`，直接落空回傳 0.0，杜絕停損掛在單薄紙牆上。
2. **DTE 三態狀態機 (`evaluate_option_dte_tier`)**：
   - $DTE \le 1$：`EXPIRATION_SETTLEMENT_ALERT`（無論新舊合約，強制結算保護）。
   - $1 < DTE < 7$：若為新開倉/轉倉（`NEW_OPPORTUNITY`）則 `LOCKOUT_SKIP`；既有持倉風控（`MANAGE_EXISTING`）則 `MAINTAIN_RISK_MONITORING`。
   - $DTE \ge 7$：`NORMAL_EXECUTION`。
3. **主力 PUT BTO 偵測 (`_detect_whale_put_bto_block`)**：
   掃描 UOA 訂單流中是否存在近平值（$|\text{Strike} - \text{Spot}| / \text{Spot} \le 5\%$）單筆 PUT BTO。比率採用時段進度正規化比率（$paced\_ratio = ratio / \text{day\_elapsed}$），門檻 $paced\_ratio \ge 1.5$ 且名目金額 $\ge \$500,000$。
4. **短 TTL 記憶化快取**：
   `_structural_signals_cache` 採用 `BoundedCache(max_size=256)`，TTL 為 300 秒，以 13 元組特徵為鍵，確保同一 15 分鐘週期內 Scenario 3 與 Scenario 4 呼叫時零重複運算。

---

## 六、調度管線、去重機制與審計軌跡

### 1. 15 分鐘背景調度流水線 (`portfolio_monitor.py`)
```text
[portfolio_monitor_task 每 15 分鐘 (:05, :20, :35, :50)]
  │
  ├─ 1. 批次組裝 portfolio_assets (現貨 + 期權合約 + 空頭 CALL)
  │    └─ Semaphore(3) 併發抓取期權即時 mid/bid/ask/iv 報價
  │
  └─ [依序執行轉倉情境 evaluation 迴圈 (嚴格優先序)]
       ├─ [優先權 1] Scenario 3: 核心衛星再平衡與防洗盤微觀結構出場 (anti_washout.py)
       │    ├─ 內含 Scenario 8: 動態調整狀態切換 (transition_engine.py)
       │    └─ 收集實質賣出標的至 already_flagged
       │
       ├─ [優先權 2] Scenario 2: 機會成本轉倉 (opportunity_cost.py)
       │    ├─ 交易策略路由器 (右側/左側/動態 4-Regime) 判定
       │    └─ 產出候選標的驗證快照 candidate_entry_confirmation
       │
       ├─ [優先權 3] Scenario 5: 核心資金部署 (core_deployment.py)
       │    ├─ 重用 Scenario 2 之候選與驗證快照，超額資金分流 BOXX / 候選標的
       │    └─ 延伸分支: Covered Call Overlay (加碼收租)
       │
       ├─ [優先權 4] Scenario 4: 槓桿與保證金防禦 (margin_defense.py)
       │    └─ 系統性紅線時清倉無勝率部位至 CASH / 反向 ETF / BOXX
       │
       ├─ [優先權 5] Scenario 6: 宏觀逃頂前瞻防禦 (macro_top_escape_defense.py)
       │    └─ 最末位評估，純機率評分有界減碼 25% 至 BOXX
       │
       └─ [獨立通道] Scenario 7: Covered Call 權利金衰減停利 (covered_call_profit_lock.py)
            └─ 純 BTC 回補，不參與 already_flagged 互斥
```

### 2. 去重機制與冷卻
- **標的去重集合 (`already_flagged`)**：
  採用 `(symbol, instrument_type)` 複合鍵（例如 `("NVDA", "SPOT")` 與 `("NVDA", "OPTIONS")` 互不干擾）。只有 `action != "HOLD"` 的指令才會加入集合，確保 Scenario 3 的安心防守卡（HOLD）不會掩蓋 Scenario 4 的系統性保證金平倉警報。
- **每日推播去重 (`dedup_key`)**：
  `rollover_alert_{uid}_{symbol}_{instrument}_{scenario}_{action}_{YYYYMMDD}`。若為 Covered Call 停利，額外追加 `_{strike}_{expiry}`。

### 3. Options Dry-Run 灰度隔離與審計軌跡
- **Dry-Run 閘門**：`config.OPTIONS_ROLLOVER_DRY_RUN`（預設 `True`）。期權轉倉建議在灰度期間僅寫入日誌與審計表，不推送 Discord DM。
- **審計資料庫**：SQLite `rollover_audit_log`（Migration `v064`），記錄 `user_id, symbol, scenario, action, sell_ratio, target_core, suggested_price, cash_impact`。使用者可透過 `/rollover_history` 隨時回溯調閱。

---

## 七、代碼邊界、限制與未落地功能追蹤

1. **版本演進與已落地項目 (Migration v069)**：
   - ✅ **TP2-空間擴展之「新舊 Call Wall 轉移點」**：已透過 Migration `v069` 於 `market_cache` 新增 `call_wall` 與 `previous_call_wall` 欄位，並於 `anti_washout.py` 完整實作阻力牆向上遷移 $\ge 3\%$ 且現價站穩舊阻力位的判定與專屬 Embed 理由文案。
   - ✅ **賣方停利引擎擴展至 CSP (Cash-Secured Put)**：已升級 `covered_call_profit_lock.py` 為通用 `evaluate_short_option_profit_lock`，盤中調度完整放行 Short Put 合約，共用時間價值衰減階梯（$DTE \le 1$ 末日保護、50% 局部、80% 全額），並於 Embed 動態標註預估釋放之擔保金。
2. **依賴啟發式代理數據之處**：
   - **左側條件二之 Put Wall 防禦厚度**：文獻原規格要求「Put OI 名目價值 $\ge \$1\text{B}$」，但現有 GEX 抓取管線不含每履約價 OI 名目金額，目前以絕對 GEX 曝險 $\ge \$5\text{M}$ 作為代理指標（[`left_side_entry.py:L287-L293`](file:///home/cosmo_chang/Projects/nexus-seeker/nexus_core/market_analysis/dynamic_rollover/left_side_entry.py#L287-L293)）。
   - **左側條件四之「4 小時內主力印花」時間窗**：選擇權鏈的成交量與 OI 是當日累計快照，缺少 Time & Sales 逐筆時間戳，目前以時段進度正規化比率（`paced_ratio`）做代理補償。
   - **動態調整部位之進場 K 棒低點 (`entry_bar_low`)**：手動標記當下擷取的已收盤 15m K 棒低點，若使用者在進場後延遲標記，可能偏離真實成交時的 K 棒低點。
3. **後續調優建議**：
   - 持續觀察 `OPTIONS_ROLLOVER_DRY_RUN` 在生產環境下的指令命中分佈，待誤報率驗證低於閾值後正式切換為 live DM 推播。
