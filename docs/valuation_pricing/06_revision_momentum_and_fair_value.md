# 分析師修正動能、兩段式現金流折現與同業倍數估值規格書

---

## 1. 核心哲學與適用市場環境

### 1.1 核心哲學
在成熟的股權投資市場中，企業之定價中樞由其未來自由現金流貼現價值決定，而短期至中期的價格發現與估值重塑則由市場分析師之共識預估修正動能（Revision Momentum）所推動。當基本面業績大幅超預期（Positive Surprise）且分析師密集調升未來各期每股盈餘預估時，將引發持久的盈餘公布後漂移（Post-Earnings Announcement Drift, PEAD）。反之，若股價大幅跌破真實內在價值，安全邊際（Margin of Safety, MOS）將為多頭投資人構築堅實的不對稱防禦底線。

Nexus Seeker 基本面分析管線 PR5 聚焦於全景估值與分析師修正動能之深度整合，其核心量化哲學如下：
1. **宏觀流動性體制中樞注入**：拒絕靜態不變的資本成本（Cost of Equity），將芝加哥聯準會金融狀況指數（NFCI）動態注入股權風險溢價（ERP），在貨幣緊縮期主動調高折現率並對同業估值乘數施加指數流動性懲罰。
2. **兩段式自由現金流折現奇點防禦**：採用 2-Stage FCF DCF 模型，以分析師 1 年期預估成長率箝制首階段 5 年成長速度，並嚴推定錨 2.5% 永續成長率。當自由現金流為負或分母利差過窄時，模型優雅退回同業乘數法，杜絕除零極端奇點。
3. **流動性折讓同業乘數法 (Comps Multiple with Liquidity Penalty)**：針對無自由現金流或高成長科技標的，採納至少 3 家同業之中位數前瞻本益比，並結合宏觀流動性體制懲罰因子，防範流動性枯竭時同業泡沫估值之傳染。
4. **基本面修正動能與 PEAD 共振捕獲**：量化追蹤同一財期在 $t$ 與 $t-30d$ 的分析師預估斜率（設計上涵蓋 $0q, +1q, 0y, +1y$ 四個期限；Finnhub 免費方案實際只有 $0q, +1q$，見 §2.5）與調升廣度比率（Breadth Ratio，需分析師層級資料）。在財報披露後 60 個 NYSE 交易日內，若業績驚喜與修正動能同向爆發，標記為 `PEAD_ALIGNED` 漂移共振標的。
5. **次日基本面觀察名單自動生成**：NYSE 交易日 19:00 ET 寫入當日分析師共識快照，20:00 ET 由事件時鐘排定估值與排名（§5.5），排除 `CRITICAL` 治理紅旗、`HIGH` 治理旗標降為觀察，產出次日優先觀察名單（`fundamental_watch_candidate`）。只入庫、不推播。
6. **零交易執行不變量 (Zero-Execution Invariant)**：所有估值結果、安全邊際評定與觀察名單均為純顧問性研究，絕不觸發任何實體帳戶下單或自動清倉。

### 1.2 適用市場環境
- **價值重估與深度折價淘金期**：在市場非理性拋售引發的下行回撤中，透過安全邊際 $\text{MOS} \ge 25\%$ 且無治理紅旗篩選優質深度價值標的。
- **財報發布後 60 日 PEAD 捕捉期**：在標的發布業績後，透過分析師向上修正斜率與正向預期差共振，鎖定具備持續基本面推力之強勢標的。
- **盤前盤後顧問情報映射**：盤後自動排定次日觀察名單，並於 09:00 盤前簡報與 `/fa` 診斷終端中即時視覺化呈現。

---

## 2. 數學模型與量化推導

### 2.1 動態股權風險溢價與權益資本成本折現率模型
折現模型直接採用芝加哥聯準會金融狀況指數（NFCI）對市場股權風險溢價（ERP）進行線性擾動校準：

$$\text{ERP}_t = \text{ERP}_{\text{BASE}} + \lambda_{\text{NFCI}} \cdot \text{clip}(\text{NFCI}_t, -1.0, +2.0)$$

- 基準風險溢價：$\text{ERP}_{\text{BASE}} = 0.045$（4.5%）。
- 敏感度係數：$\lambda_{\text{NFCI}} = 0.010$（NFCI 每緊縮 1 個標準差，市場風險溢價上升 100 bps）。

資產權益資本成本（Cost of Equity, $r$）依資本資產定價模型（CAPM）計算：

$$r = \text{DGS10} + \beta \times \text{ERP}_t$$

其中 $\text{DGS10}$ 為美國 10 年期國債殖利率（小數計），資產貝他值進行物理界限箝制：$\beta = \text{clip}(\beta_{\text{asset}}, 0.5, 2.0)$。若未獲取貝他值，預設為 $1.0$。

### 2.2 兩段式每股自由現金流折現模型 (Two-Stage FCF DCF)
兩段式折現模型將現金流分為首階段（第 1–5 年）預估成長期與第二階段永續終端價值（Terminal Value）：

$$FV_{\text{DCF}} = \sum_{t=1}^{5} \frac{FCF_0 \times (1 + g_1)^t}{(1 + r)^t} + \frac{FCF_0 \times (1 + g_1)^5 \times (1 + g_T)}{(r - g_T) \times (1 + r)^5}$$

- $FCF_0$：標的近 12 個月每股自由現金流（FCF per share TTM）。Finnhub `/stock/metric?metric=all` 沒有每股 FCF 欄位（2026-10 實測 MSFT／AAPL 回 133 個鍵，不存在 `fcfPerShareTTM`；`cashFlowPerShareTTM` 是營運現金流口徑，不採用），改由現價與 P/FCF 反推：
  $$FCF_0 = \frac{\text{Spot}}{\text{pfcfShareTTM}}$$
  缺 `pfcfShareTTM`、其值為 0 或無現價時記 `FCF_UNAVAILABLE`，DCF 不計；P/FCF 為負即 $FCF_0 < 0$。若 $FCF_0 \le 0$，DCF 模型自動失效並退回同業乘數法。由於 $FCF_0$ 隨現價等比例縮放，MOS 實質上反映「現行 P/FCF 與前瞻本益比」相對內在倍數的折溢價。
- $g_1$：未來 5 年預估複合成長率，以 1 年期預估成長率 $g_{\text{est}}$ 進行區間箝制：
  $$g_1 = \text{clip}(g_{\text{est}}, -0.10, +0.25)$$
  $g_{\text{est}}$ 依序取（免費方案沒有分析師年度成長率欄位）：
  1. **前瞻隱含成長**：$g_{\text{est}} = \text{Forward EPS} / \text{EPS}_{\text{TTM}} - 1$（兩者皆為正；Forward EPS 定義見 §2.3）。
  2. **歷史成長率**（無前瞻值時，記 `G_EST_HISTORICAL`）：`epsGrowthTTMYoy` → `epsGrowth3Y` → `epsGrowth5Y` → `revenueGrowthTTMYoy`，百分比轉小數。
  3. **預設值**（全缺時，記 `G_EST_DEFAULT`）：`DEFAULT_GROWTH_EST = 0.05`。
- $g_T$：永續終端成長率，嚴格定錨於全美長期潛在 GDP 增長率 $2.5\%$（0.025）：
  $$g_T = 0.025$$
- **分母利差防禦門檻 (Spread Constraint)**：
  為杜絕無風險利差過度壓縮或分母除以零之數學奇點，若折現率與終端成長率之差過窄，強制拒絕輸出 DCF：
  $$r - g_T < 0.010 \implies \text{DCF 判定無效 (SPREAD\_TOO\_NARROW)}$$

### 2.3 流動性折讓同業乘數法模型 (Comps Multiple with Liquidity Penalty)
針對處於微利、虧損或現金流重投資期之標的，以同業群體中位數本益比為基礎，並施加宏觀流動性體制懲罰因子：

$$FV_{\text{Comps}} = \text{Forward EPS} \times \left( \text{Median}(\text{Peers Forward PE}) \times e^{-0.10 \times \text{clip}(\text{NFCI}_t, -1.0, +2.0)} \right)$$

- **Forward EPS 口徑**：$\text{Forward EPS} = \text{Spot} / \text{forwardPE}$（Finnhub 前瞻本益比，口徑由 Finnhub 定義的前瞻共識）。缺 `forwardPE` 時以近四季 EPS（`epsTTM`）代理並記 `FORWARD_EPS_TRAILING`——此時實為 trailing，不是前瞻。
- **同業本益比**：優先取同業的 `forwardPE`；缺值時退回近四季本益比（`peTTM` → `peExclExtraTTM` → `peNormalizedAnnual`），並記 `PEER_PE_TRAILING`。同業至多抽樣 10 家。
- 有效性約束：
  1. $\text{Forward EPS} \le 0$ 時同業乘數法失效。
  2. 具備正向本益比之有效同業數量必須滿足 $N_{\text{peers}} \ge 3$。
  3. $\text{Median}(\text{Peers Forward PE}) \le 0$ 則乘數無效。

### 2.4 公允價值整合與安全邊際模型 (Margin of Safety, MOS)
系統依模型有效性執行公允價值中樞整合：

$$FV = \begin{cases}
\frac{FV_{\text{DCF}} + FV_{\text{Comps}}}{2} & \text{若兩者皆有效 (BLENDED)} \\
FV_{\text{Comps}} & \text{若 } FCF_0 \le 0 \lor \text{DCF 拒絕 (COMPS\_ONLY)} \\
FV_{\text{DCF}} & \text{若同業不足 3 家 (DCF\_ONLY)} \\
\text{None} & \text{若兩者皆無效 (NONE)}
\end{cases}$$

安全邊際（MOS）定義為公允價值相對於當前現貨市價之折價幅度：

$$MOS = \frac{FV - \text{Spot Price}}{FV}$$

- 深度價值評估：當 $MOS \ge 0.25$（安全邊際達 25% 以上）且標的沒有生效中的 `HIGH`／`CRITICAL` 治理旗標時，正式評估為深度價值標的（`DEEP_VALUE`）；有 `HIGH`／`CRITICAL` 旗標時改記 `DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE`。`REVIEW`／`INFO`（例如預設為 `REVIEW` 的 8-K 5.02）只待人工複核，不壓制。
- **無效值存 NULL**：方法為 `NONE` 時 `fair_value` 與 `margin_of_safety` 皆存 NULL；有公允價值但無現價時 `margin_of_safety` 存 NULL 並記 `SPOT_UNAVAILABLE`。不以 0.0 作為哨兵值。下游 `/fa` 與離線報告依 NULL 與 `method = 'NONE'` 排除。
- **預設輸入必須標註**：10 年債殖利率缺值時套用 `DEFAULT_US10Y_PCT = 4.25`（記 `US10Y_DEFAULT`）、NFCI 缺值（記 `NFCI_MISSING`，ERP 與同業倍數不做流動性調整）、Beta 缺值套用 1.0（記 `BETA_DEFAULT`）、$g_{\text{est}}$ 套用 0.05（記 `G_EST_DEFAULT`）。旗標寫入 `fair_value_log.flags_json`，`/fa` 逐條以中文呈現。

### 2.5 分析師修正動能模型與 PEAD 捕捉 (Revision Momentum & PEAD)
依據標的同一財期在 $t$ 與 $t-30d$ 的每股盈餘預估值變動斜率與調升廣度，量化分析師修正動能。

**快照與財期配對**：`eps_estimate_snapshot` 每個 NYSE 交易日 19:00 ET 寫入一份當日快照（§5.5），每列帶財期 `fiscal_period`（季度 `YYYY-Qn`、年度 `YYYY-FY`）。$t$ 為估值日（含）以前最新的快照日；$t-30d$ 取窗口 $[t-35, t-28]$ 內、與 $t$ 同財期、快照日最接近 $t-30$ 者，窗口外不退回更舊的快照。horizon 標籤只決定權重：跨季後 $t-30d$ 的 $+1q$ 在 $t$ 變成 $0q$，斜率以財期 $F$ 配對：

$$\text{Slope}_h = \frac{\text{EPS}_{F(h), t} - \text{EPS}_{F(h), t-30d}}{\max(|\text{EPS}_{F(h), t-30d}|, \text{FLOOR}_{\text{EPS}})}, \quad h \in \{0q, +1q, 0y, +1y\}$$

- 門檻參數：$\text{FLOOR}_{\text{EPS}} = 0.05$ 美元。
- 同財期在窗口內找不到基準的 horizon **不計入**（記入 `unmatched_horizons`）；v094 以前寫入、沒有財期的快照一律不配對。
- **Finnhub 免費方案只有 $0q, +1q$**：`/stock/eps-estimate` 回 HTTP 403，改以財報日曆（`company_earnings` 錨定財季、`earnings_calendar` 的 `epsEstimate`）推得本季與下季預估，沒有 $0y, +1y$。403 後 24 小時內不再呼叫 eps-estimate（每次 403 仍消耗限流配額）。只有 $0q, +1q$ 時權重重新正規化為 $w_{0q} = 0.4, w_{+1q} = 0.6$。
- 調升/調降廣度比率（需分析師層級調升／調降家數）：
  $$\text{Breadth} = \frac{N_{\text{up, 30d}} - N_{\text{down, 30d}}}{N_{\text{up, 30d}} + N_{\text{down, 30d}}}$$
  有計數但兩者皆為 0 時 $\text{Breadth} = 0.0$。免費方案沒有分析師層級資料，此時廣度項**不計**（記 `BREADTH_UNAVAILABLE`），也不以各期斜率方向充當廣度——那會與斜率項重複計分。

**基本面修正動能綜合評分 (Revision Momentum Score)**：

$$\text{Score}_{\text{Rev}} = 100 \times \left[ 0.70 \times S + 0.30 \times \text{Breadth} \right], \qquad S = \sum_{h} w_h \cdot \text{clip}\left(\frac{\text{Slope}_h}{0.20}, -1.0, 1.0\right)$$

- 無廣度資料時：$\text{Score}_{\text{Rev}} = 100 \times S$（斜率項權重 100%）。
- 期限權重：$w_{0q} = 0.20, w_{+1q} = 0.30, w_{0y} = 0.20, w_{+1y} = 0.30$（若部分期限缺失，現存項自動重新正規化至權重和為 1.0）。
- 沒有任何可配對財期時 $\text{Score}_{\text{Rev}}$ 為 NULL（記 `NO_CURRENT_SNAPSHOT`／`NO_PRIOR_SNAPSHOT`／`NO_MATCHED_PERIOD`），不以 0 充當「動能持平」。
- 分數範圍箝制於 $[-100.0, +100.0]$。

**PEAD 共振標記 (PEAD Alignment)**：
只取估值日以前最近一筆 `status = 'PROCESSED'` 的財報預期差（`PENDING`／`FAILED` 沒有綜合評分）。發布日取 `earnings_surprise.announced_on`（8-K Item 2.02 的 SEC 受理日，美東），距離以 NYSE 交易日計：發布日（不含）之後到估值日（含）的交易日數 $d$（`market_time.count_nyse_sessions_after`）。發布日不明（非事件觸發寫入的舊列）時不判定 PEAD 並記 `PEAD_DATE_UNKNOWN`，不以預設天數代入。當 $d \le 60$，且業績預期差綜合評分與分析師修正動能同向且具備顯著性時：

$$\text{sign}(\text{Surprise Score}) == \text{sign}(\text{Score}_{\text{Rev}}) \quad \land \quad |\text{Surprise Score}| \ge 15.0 \quad \land \quad |\text{Score}_{\text{Rev}}| \ge 15.0$$

系統正式標註該標的為 `PEAD_ALIGNED`（盈餘公布後漂移共振）。

---

## 3. 決策邏輯與狀態機 / 流程圖

基本面管線在估值與動能評定中的狀態轉移與次日候選生成架構如下：

```mermaid
flowchart TD
    Snap([19:00 ET 寫入當日 EPS 共識快照 帶財期]) --> Start
    Start([20:00 ET 估值時鐘啟動 等待快照刷新完成]) --> Uni[讀取全域基本面標的池]
    Uni --> GovCheck{公司治理閘門審查}

    GovCheck -->|觸發 CRITICAL 級別紅旗| Exclude[標記為 EXCLUDED 排除候選<br/>記錄風控審查事由]
    GovCheck -->|HIGH / REVIEW / INFO 或無旗標| FetchData[獲取流動性體制 / 財務指標 / 共識快照]

    FetchData --> MacroCalc[計算動態 ERP 與資本成本 r]
    MacroCalc --> DCFCheck{FCF_0 > 0 且 r - g_T >= 0.010?}

    DCFCheck -->|是| CalcDCF[計算 2-Stage FCF DCF 公允價值]
    DCFCheck -->|否| RejectDCF[標記 DCF 失效退回 Comps]

    CalcDCF --> CompsCheck{有效同業 >= 3 且 Forward EPS > 0?}
    RejectDCF --> CompsCheck

    CompsCheck -->|是| CalcComps[計算流動性折讓同業乘數公允價值]
    CompsCheck -->|否| RejectComps[標記 Comps 失效]

    CalcComps --> BlendFV{DCF 與 Comps 皆有效?}
    RejectComps --> BlendFV

    BlendFV -->|雙模型皆有效| BlendedVal[50% DCF + 50% Comps 融合中樞]
    BlendFV -->|僅 DCF 有效| DCFOnlyVal[採用 DCF 公允價值]
    BlendFV -->|僅 Comps 有效| CompsOnlyVal[採用 Comps 公允價值]
    BlendFV -->|皆無效| NullVal[公允價值判定無效]

    BlendedVal --> RevCalc[t 與 t-35 至 t-28 窗口同財期配對 計算修正動能與 PEAD]
    DCFOnlyVal --> RevCalc
    CompsOnlyVal --> RevCalc
    NullVal --> RevCalc

    RevCalc --> CompositeScore[計算多因子綜合評估分<br/>動能 40% + MOS 40% + PEAD 20分]
    CompositeScore --> RankSort[全池降冪排名與篩選]

    RankSort --> Classify{排名與綜合分數判定}
    Classify -->|有效公允價值 且非 HIGH 且綜合分 > 0 的前 5 名| Candidate[標定為 CANDIDATE 優先候選]
    Classify -->|其他 含 HIGH 治理旗標與無有效公允價值| Watch[標定為 WATCH 觀察名單]

    Exclude --> SaveDB[持久化至 fundamental_watch_candidate]
    Candidate --> SaveDB
    Watch --> SaveDB
    SaveDB --> End([寫入完成並發布日誌])
```

---

## 4. 關鍵具名常數與物理約束

本模型中所有核心常數與量化邊界均採用具名常數配置，禁止在邏輯中出現未命名的魔法數字：

| 具名常數代碼 | 數值 / 預設值 | 物理量綱 | 業務語意與物理約束說明 |
|---|---|---|---|
| `ERP_BASE` | `0.045` | 小數比率 (4.5%) | 股權風險溢價中樞基準值 |
| `LAMBDA_NFCI` | `0.010` | 敏感度因子 | NFCI 每緊縮 1 個標準差對 ERP 的線性擾動增量 |
| `DEFAULT_PERPETUAL_GROWTH` | `0.025` | 小數比率 (2.5%) | 兩段式 DCF 永續終端成長率定錨 ($g_T$) |
| `MIN_SPREAD_THRESHOLD` | `0.010` | 小數比率 (1.0%) | 折現率與終端成長率分母安全利差防禦下限 ($r - g_T \ge 0.010$) |
| `MIN_PEERS_COUNT` | `3` | 家數整數 | 流動性折讓同業乘數法最低有效正本益比同業數量門檻 |
| `DEEP_VALUE_MOS_THRESHOLD` | `0.25` | 小數比率 (25%) | 評定深度價值標的所需之最低安全邊際門檻 |
| `FLOOR_EPS` | `0.05` | 美元 ($) | 分析師預估變動斜率計算之每股盈餘分母安全門檻 |
| `SLOPE_NORMALIZER` | `0.20` | 小數比率 (20%) | 分析師 30 天每股盈餘上修斜率滿分歸一化尺度 |
| `SLOPE_WEIGHT` | `0.70` | 權重比率 (70%) | 分析師修正動能分數中斜率項權重 |
| `BREADTH_WEIGHT` | `0.30` | 權重比率 (30%) | 分析師修正動能分數中廣度項權重 |
| `PEAD_MAX_DAYS` | `60` | 交易日數 | 盈餘公布後漂移 (PEAD) 共振標記有效觀察窗口上限 |
| `PEAD_THRESHOLD` | `15.0` | 分數絕對值 | 觸發 PEAD 共振所需之業績驚喜與修正動能最低顯著性門檻 |
| `PRIOR_TARGET_DAYS` | `30` | 日曆日 | $t-30d$ 基準快照的目標距離 |
| `PRIOR_WINDOW_MIN_DAYS` / `PRIOR_WINDOW_MAX_DAYS` | `28` / `35` | 日曆日 | $t-30d$ 基準快照的接受窗口 $[t-35, t-28]$，窗口外不退回 |
| `DEFAULT_US10Y_PCT` | `4.25` | 百分比 (%) | 流動性體制尚無 10 年債殖利率時的預設值，套用時記 `US10Y_DEFAULT` |
| `DEFAULT_GROWTH_EST` | `0.05` | 小數比率 (5%) | 前瞻與歷史成長率皆缺時的 $g_{\text{est}}$，套用時記 `G_EST_DEFAULT` |
| `DEFAULT_BETA` | `1.0` | 無因次 | Beta 缺值時的預設值，套用時記 `BETA_DEFAULT` |
| `MODERATE_DISCOUNT_MOS` / `FAIR_VALUE_MOS_BAND` | `0.10` / `0.10` | 小數比率 | 中度折價下限與合理估值區間半寬（`/fa` 與離線報告共用） |
| `MAX_PEERS` | `10` | 家數整數 | 同業本益比抽樣上限 |
| `REVISION_WEIGHT` / `MOS_WEIGHT` | `0.40` / `0.40` | 權重比率 | 綜合評估分中修正動能與安全邊際（×100、箝制 ±100）的權重 |
| `PEAD_BONUS` | `20.0` | 分數 | 綜合評估分中 PEAD 共振的加分 |
| `MAX_CANDIDATES` | `5` | 檔數整數 | 每日 `CANDIDATE` 名額上限 |
| `SNAPSHOT_WAIT_TIMEOUT_SECONDS` | `3600.0` | 秒 | 20:00 估值等待 19:00 快照刷新完成的上限 |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 負自由現金流與成長率極值箝制
1. **負現金流退回保護**：當企業處於擴張投資期導致每股自由現金流 $FCF_0 \le 0$ 時，DCF 模組強制輸出無效並記錄 `FCF_NON_POSITIVE`，決策路由自動退回同業乘數法。
2. **分析師極端成長率箝制**：若分析師發布極度樂觀之預估（例如 $+80\%$ 增長），首階段 5 年複合增長率強制箝制於 $+25\%$ 上限；若分析師預估極度悲觀，則箝制於 $-10\%$ 下限，防範極端參數扭曲長期價值。

### 5.2 宏觀利差倒掛與分母奇點熔斷
當美國國債殖利率倒掛或折現率受到極度壓制，使得 $r - g_T < 0.010$ 時，若強行折現將導致終端價值膨脹至無限大。系統強制拒絕 DCF 計算，並輸出 `SPREAD_TOO_NARROW` 拒絕原因，防止系統產出荒謬估值。

### 5.3 重大公司治理審查熔斷
依旗標嚴重度分級處理（嚴重度定義見 [`06_sec_event_stream_and_governance_gate.md`](../macro_sentiment/06_sec_event_stream_and_governance_gate.md)）：
- **`CRITICAL`**（如 8-K Item 4.02 財報不可信賴性重編）：直接列入 `EXCLUDED` 排除狀態、不估值，排除原因附旗標種類。
- **`HIGH`**：不排除，綜合評估分照常計算（不加減分），但**不得晉升 `CANDIDATE`**（上限 `WATCH`），並壓制深度價值判定（`DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE`）；`reasons_json` 標註 `governance_note`，`/fa` 顯示。排除只保留給 `CRITICAL`，是為了讓 `HIGH` 標的仍留有估值紀錄可供追蹤與離線校準。
- **`REVIEW`／`INFO`**（含預設為 `REVIEW` 的 8-K 5.02 高管異動）：不影響評分與狀態，只在 `reasons_json` 標註待人工複核。
- 綜合評估分不含任何治理加減分；名單只入庫、不推播，嚴禁向使用者推送高風險治理瑕疵標的。

### 5.4 缺失資料優雅降級
- 同業數量不足 3 家時，自動放棄同業倍數法，僅依賴有效之 DCF 模型。
- 兩模型皆無效（如 ETF：`/stock/metric` 只回 19 個鍵，沒有 P/FCF 與前瞻本益比）時 `method = NONE`、公允價值與安全邊際存 NULL；此類標的**不得晉升 `CANDIDATE`**，排名排在有有效公允價值者之後。
- 綜合評估分中無效的動能（NULL）或安全邊際（NULL）該項貢獻為 0，不以哨兵值參與。
- 窗口 $[t-35, t-28]$ 內沒有同財期快照時（例如上線未滿 28 天），該 horizon 不計；全部不計時 $\text{Score}_{\text{Rev}}$ 為 NULL。不以窗口外的舊快照代替，也不以斜率方向計數充當廣度。

### 5.5 排程、背景執行與推播政策
- **19:00 ET `eps_estimate_snapshot_1900`**（NYSE 交易日，priority 70）：`EstimateSnapshotRunner` 逐檔呼叫 `ValuationService.refresh_estimate_snapshots`，寫入當日快照。每個交易日一份，確保任何估值日的 $[t-35, t-28]$ 窗口（8 個日曆日）內都有快照。
- **20:00 ET `fundamental_watch_candidate_2000`**（NYSE 交易日，priority 80）：`ValuationJobRunner` 先等待 19:00 快照刷新完成（上限 `SNAPSHOT_WAIT_TIMEOUT_SECONDS`，逾時以既有最新快照估值），再逐檔估值與排名。
- 兩者皆只在 leader 執行、觸發時通過 `is_memory_safe()`、以背景 `asyncio.Task` 執行（Finnhub 背景限流等待不阻塞 5 分鐘時鐘輪詢）、上一輪未完成時略過；長時間執行中每檔開始前複檢 leader 與 `is_memory_safe()`，超標即中止（估值中止時不寫不完整的候選名單）；單一標的例外隔離。
- **推播**：本規格不定義任何推播；兩個工作只入庫，結果由 `/fa` 與 09:00 盤前簡報讀取。盤前簡報標註名單日，名單日早於前一個已收盤交易日（20:00 排程漏跑或失敗）時不展示過期名單。

### 5.6 呈現與離線校準
- `/fa` 的估值區塊顯示估值日、方法、DCF／同業倍數、MOS、資本成本、輸入預設值旗標與修正動能（含降級原因）；`method = NONE` 或公允價值為 NULL 時明示「無有效估值」，不顯示 0。`/fa` 共 6 個區塊，總長超過 NexusEmbed 5800 字元時截短最長的欄位內文，不整欄丟棄。
- `calibration/fundamental_forward_report.py` 統計時排除 `method = NONE` 與 NULL 的估值、NULL 的修正動能；缺表或查詢失敗不默默略過，於報告開頭列出。

---

## 6. 核心程式碼檔案路徑關聯

本規格書對應之核心實作檔案與持久層模組清單如下：

- 純量化演算法葉模組：
  - `nexus_core/market_analysis/fundamental_pipeline/revision_momentum.py`
  - `nexus_core/market_analysis/fundamental_pipeline/fair_value.py`
- 非同步協調與時鐘服務：
  - `nexus_core/services/valuation_service.py`
  - `nexus_core/services/fundamental_clock_service.py`
- 資料庫持久層與遷移腳本：
  - `nexus_core/database/fundamental_pipeline.py`
  - `nexus_core/database/migrations/v094_add_valuation_and_watch.py`
- 排程註冊：
  - `nexus_core/cogs/trading/fundamental_pipeline_monitor.py`
- 共識快照提供者與財報發布日：
  - `nexus_core/services/fundamental_providers.py`
  - `nexus_core/services/earnings_surprise_service.py`
- 呈現層與互動診斷終端：
  - `nexus_core/cogs/fundamental_terminal.py`
  - `nexus_core/cogs/embed_builders/fundamental_embeds.py`
  - `nexus_core/cogs/embed_builders/order_embeds/pre_market_briefing.py`
  - `nexus_core/cogs/analyst_agent.py`
- 離線前向校準報告：
  - `nexus_core/calibration/fundamental_forward_report.py`
