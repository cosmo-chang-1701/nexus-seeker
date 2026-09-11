# 個股與大盤 Gamma Flip 翻轉線估算模型技術規格書

## 1. 核心哲學與適用市場環境

Gamma Flip 臨界翻轉線（Gamma Neutral Level / Flip Line）是做市商整體 Gamma 曝險由負轉正（或由正轉負）的零交叉臨界價格。它是美股市場波動率體系（Volatility Regime）的「水庫水位線」：
- **水線之上（$\text{Spot} > \text{Gamma Flip}$）**：做市商處於正 Gamma 區間，採取逆勢對沖（逢跌買入、逢漲賣出），市場呈現高流動性、低波動、價格自穩定收斂特性。
- **水線之下（$\text{Spot} < \text{Gamma Flip}$）**：做市商跌入負 Gamma 泥淖，被迫採取順勢對沖（逢跌砸盤、逢漲追買），市場呈現流動性黑洞、波動率飆升與連鎖踩踏。

### 個股輕量估算與大盤端點分離架構
在 Nexus Seeker 系統中：
1. **大盤 SPY**：直接調用宏觀數據端點（`/api/v1/scrape/macro/gex`），取得與機構端比對的高精度全鏈 Gamma Flip。
2. **個股標的**：因個股期權即時端點（`fetch_symbol_gex_metrics`）並未直接提供 Flip 欄位，系統設計了**輕量客戶端逐履約價符號變化估算演算法**（`estimate_symbol_gamma_flip`）。該演算法直接複用已抓取的 `gex_profile`，完全零額外網路請求，透過對相鄰履約價對掃描個別 Net GEX 值的符號變化（由負轉非負），並結合「$\pm 30\%$ Bracket 邊界」與「Net GEX Regime 方向一致性校驗」，徹底排除價外雜訊與偽交叉點。

---

## 2. 數學模型與量化推導

### 2.1 五步零交叉點估算演算法
給定某標的當前現貨價格 $\text{Spot}$ 與期權鏈 GEX 分佈字典 $\text{GEXProfile} = \{(K_1, g_1), (K_2, g_2), \dots, (K_N, g_N)\}$。

#### 第一步：履約價升序排列
將所有履約價由低至高嚴格排序：
$$
K_{(1)} < K_{(2)} < \dots < K_{(N)}, \quad g_{(j)} = \text{GEX}(K_{(j)})
$$

#### 第二步：相鄰履約價對符號變化捕捉 (Adjacent Pair Net GEX Sign Flip Scan)
舊版實作採用「全鏈累積加總 (Cumulative Sum) 尋找轉正點」，但在真實選擇權市場中，低履約價由 Put 主導產生龐大負 GEX，累積值往往深度探底，直到高履約價由 Call 主導才拉回正值，導致累積零交叉點幾乎必然落在現價上方（$K > \text{Spot}$）；在 LONG_GAMMA 鏈中再疊加方向性約束時，唯一的候選點被全數清空而恆回傳 0.0。

為此，現行演算法改採逐履約價的符號變化偵測，直接掃描相鄰履約價對的個別 Net GEX 符號變化，捕捉做市商單一履約價層級由負轉非負的交叉點集合 $\mathcal{Z}$：
$$
\mathcal{Z} = \{K_{(i)} \mid g_{(i-1)} < 0 \le g_{(i)}, \quad 2 \le i \le N\}
$$
以履約價 $K_{(i)}$ 作為由負轉正 Gamma 的臨界轉折履約價候選。

#### 第三步：Bracket 雜訊防禦過濾
深度價外（Deep OTM）期權常常因個別大單在遠端形成局部的微弱翻轉，若誤採為 Flip 會產生偏離現價數倍的失真數據。演算法引入現價 $\pm 30\%$ 的容差區間（Bracket）：
$$
\mathcal{Z}_{\text{bracket}} = \{K \in \mathcal{Z} \mid 0.70 \times \text{Spot} \le K \le 1.30 \times \text{Spot}\}
$$
*註：當 $\text{Spot} \le 0$ 時無法定義合理的 Bracket 範圍，退回不設邊界（保留所有候選點）。*

#### 第四步：Net GEX Regime 方向一致性驗證
以全鏈所有履約價的 Net GEX 總和 $G_{\text{total}} = \sum_{j=1}^N g_{(j)}$（嚴格等同於呼叫端的 `net_gex`）判定整體做市商曝險體系：
- 若全鏈呈現正 Gamma（$G_{\text{total}} > 0$），做市商在現價處於自穩定區間，Flip 臨界翻轉線理應位於現價下方或等於現價（$\text{Flip} \le \text{Spot}$）；
- 若全鏈呈現負 Gamma（$G_{\text{total}} < 0$），做市商在現價跌入泥淖，Flip 臨界翻轉線理應位於現價上方或等於現價（$\text{Flip} \ge \text{Spot}$）。

為此，在 $\text{Spot} > 0$ 且 $G_{\text{total}} \neq 0$ 時，強制執行方向一致性約束：
$$
\mathcal{Z}_{\text{filtered}} =
\begin{cases}
\{K \in \mathcal{Z}_{\text{bracket}} \mid K \le \text{Spot}\}, & \text{若 } G_{\text{total}} > 0 \quad (\text{LONG\_GAMMA: 翻轉線應在現價或下方}) \\
\{K \in \mathcal{Z}_{\text{bracket}} \mid K \ge \text{Spot}\}, & \text{若 } G_{\text{total}} < 0 \quad (\text{SHORT\_GAMMA: 翻轉線應在現價或上方})
\end{cases}
$$
方向矛盾的候選點一律剔除；若 $G_{\text{total}} == 0$ 或 $\text{Spot} \le 0$ 則跳過方向檢查。

#### 第五步：最近距離唯一選取
若經篩選後存在多個局部交叉點，優先選取最接近現價者作為有效翻轉線：
$$
\text{Gamma Flip} =
\begin{cases}
\operatorname{argmin}_{K \in \mathcal{Z}_{\text{filtered}}} |K - \text{Spot}|, & \text{若 } \mathcal{Z}_{\text{filtered}} \neq \emptyset \\
0.0, & \text{若 } \mathcal{Z}_{\text{filtered}} = \emptyset
\end{cases}
$$
若 $\text{Spot} \le 0$ 且 $\mathcal{Z}_{\text{filtered}} \neq \emptyset$，則回傳第一筆候選 $\mathcal{Z}_{\text{filtered}}[0]$。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([輸入 gex_profile 與 Spot]) --> CheckProfile{gex_profile 有效?}
    CheckProfile -- 否 --> ReturnZero[返回 0.0: 無法估算]
    CheckProfile -- 是 --> SortStrikes[由低至高升序排列履約價: K_1, K_2, ..., K_N]

    SortStrikes --> ScanSignFlip[掃描相鄰履約價對: g_prev < 0 <= g_curr]
    ScanSignFlip --> CheckFlip{找到負轉正交叉點?}
    CheckFlip -- 否 --> ReturnZero
    CheckFlip -- 是 --> ApplyBracket["套用 Bracket 雜訊過濾:<br/>僅保留落在 0.7 * Spot 至 1.3 * Spot 之候選"]

    ApplyBracket --> CheckRegimeDir{全鏈總和 G_total 方向校驗}
    CheckRegimeDir -- "G_total > 0 (LONG_GAMMA)" --> FilterBelow[僅保留 Strike <= Spot 之候選]
    CheckRegimeDir -- "G_total < 0 (SHORT_GAMMA)" --> FilterAbove[僅保留 Strike >= Spot 之候選]
    CheckRegimeDir -- "G_total == 0 或 Spot <= 0" --> KeepAll[保留 Bracket 內所有候選]

    FilterBelow --> EvalCandidates{篩選後候選清單為空?}
    FilterAbove --> EvalCandidates
    KeepAll --> EvalCandidates

    EvalCandidates -- 是 --> ReturnZero
    EvalCandidates -- 否 --> PickClosest[選取距離現價最近者: min |K - Spot|]
    PickClosest --> ReturnFlip([返回 Gamma Flip 價格])
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 | 數值 / 門檻 | 物理 / 代碼約束 | 程式碼檔案路徑 |
| :--- | :--- | :--- | :--- |
| `bracket_low` | `0.7 * Spot` ($-30\%$) | 翻轉線有效候選範圍下界，排除深價外雜訊 | `nexus_core/market_analysis/index_microstructure.py` |
| `bracket_high` | `1.3 * Spot` ($+30\%$) | 翻轉線有效候選範圍上界，排除深價外雜訊 | `nexus_core/market_analysis/index_microstructure.py` |
| `Gamma Flip Fallback` | `VWAP + 0.5 ATR_15m` | 當 Flip 估算為 0.0 且全鏈正 Gamma 時的替代突破門檻 | `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

1. **單邊純正或純負期權分佈之 Fail-Safe**：
   若標的期權鏈所有履約價的 GEX 全數為正（無任何負值）或全數為負（無任何正值），相鄰履約價對永不觸發 $g_{(i-1)} < 0 \le g_{(i)}$，演算法自然返回 `0.0`。下游進場條件一嚴格判定 `gamma_flip <= 0`，並啟動 Fallback 檢驗，防止拋出例外。
2. **方向矛盾候選點的自動消解**：
   在全鏈為正 Gamma（$G_{\text{total}} > 0$）但候選交叉點位於現價上方時，該點會被「方向一致性校驗」精準剔除並返回 `0.0`，杜絕向交易終端呈現自相矛盾的錯誤指標。
3. **資料解析例外保護**：
   若履約價或 GEX 曝險含有非法字元或為空字典，函式內部透過 `try-except` 捕獲並安全回傳 `0.0`，保證背景排程的穩健運行。

---

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/index_microstructure.py`：
  - 核心估算函式：`estimate_symbol_gamma_flip()`（第 768–849 行）
- `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py`：
  - 右側突破引用與 Fallback 替代：`_confirm_entry_condition1_breakout()`（第 55–255 行）
- `nexus_core/market_analysis/dynamic_rollover/regime_classifier.py`：
  - 4-Regime 路由引用：`classify_dynamic_regime()`
- `nexus_core/cogs/embed_builders/portfolio_embeds.py`：Symbol Hub 個股 GEX Flip 線呈現
