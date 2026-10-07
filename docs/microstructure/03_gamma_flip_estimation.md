# 個股與大盤 Gamma Flip 翻轉線估算模型技術規格書

## 1. 核心哲學與適用市場環境

Gamma Flip 臨界翻轉線（Gamma Neutral Level / Flip Line）是做市商整體 Gamma 曝險由負轉正（或由正轉負）的零交叉臨界價格。它是美股市場波動率體系（Volatility Regime）的「水庫水位線」：
- **水線之上（$\text{Spot} > \text{Gamma Flip}$）**：做市商處於正 Gamma 區間，採取逆勢對沖（逢跌買入、逢漲賣出），市場呈現高流動性、低波動、價格自穩定收斂特性。
- **水線之下（$\text{Spot} < \text{Gamma Flip}$）**：做市商跌入負 Gamma 泥淖，被迫採取順勢對沖（逢跌砸盤、逢漲追買），市場呈現流動性黑洞、波動率飆升與連鎖踩踏。

### 個股輕量估算與大盤端點分離架構
在 Nexus Seeker 系統中：
1. **大盤 SPY**：直接調用宏觀數據端點（`/api/v1/scrape/macro/gex`），取得與機構端比對的高精度全鏈 Gamma Flip；端點缺值、標記備援或離群時，改以 SPY 個股期權鏈經 `estimate_macro_spy_gamma_flip` 備援估算，兩者都須通過大盤合理性閘門（§2.2）。
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
深度價外（Deep OTM）期權常常因個別大單在遠端形成局部的微弱翻轉，若誤採為 Flip 會產生偏離現價數倍的失真數據。演算法引入現價下方 $b_{\downarrow}$、上方 $b_{\uparrow}$ 的容差區間（Bracket）：
$$
\mathcal{Z}_{\text{bracket}} = \{K \in \mathcal{Z} \mid (1 - b_{\downarrow}) \times \text{Spot} \le K \le (1 + b_{\uparrow}) \times \text{Spot}\}
$$
參數為 `estimate_symbol_gamma_flip(gex_profile, spot, bracket_pct, bracket_above_pct)`：$b_{\downarrow}$ = `bracket_pct`（預設 $0.30$），$b_{\uparrow}$ = `bracket_above_pct`（未指定時等於 $b_{\downarrow}$）。個股閘門一律使用預設對稱 $\pm 30\%$；大盤 SPY 備援估算 `estimate_macro_spy_gamma_flip()` 使用 $b_{\downarrow} = 8\%$、$b_{\uparrow} = 20\%$（與 §2.2 的大盤閘門相同）。
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

### 2.2 大盤 SPY Gamma Flip 合理性閘門（非對稱）
大盤 Flip 經過 edge 抓取、KV 快取、last-known-good 快取三層，任何一層的錯誤值都會直接決定 `SHORT_GAMMA_CRITICAL` 與逃頂窗口。定義偏離率 $d = (\text{Flip} - \text{Spot}) / \text{Spot}$（同一尺度：SPY 對 SPY、SPX 翻轉線對 SPX），`is_macro_gamma_flip_outlier(flip, spot)` 判定：
$$
\text{Outlier} = \begin{cases}
d > \text{MACRO\_GEX\_FLIP\_MAX\_ABOVE\_SPOT\_PCT} = 0.20, & d \ge 0 \quad (\text{Flip 在現價上方：short gamma 方向}) \\
-d > \text{MACRO\_GEX\_FLIP\_MAX\_BELOW\_SPOT\_PCT} = 0.08, & d < 0 \quad (\text{Flip 在現價下方：long gamma 方向})
\end{cases}
$$
Flip 或 Spot 缺值、非數值、$\le 0$ 時回傳 `False`——「缺值」與「離群」分開處理，由呼叫端各自視為未知或觸發自癒。

**為什麼非對稱**：
- **上方 20%**：崩跌時做市商被壓進負 Gamma，真實 Flip 會遠高於現價（SPY 急跌 12% 時約高 10–14%）。對稱 $\pm 8\%$ 會在最需要的時候把真訊號當成雜訊丟掉，`short_gamma_critical` 因此失效。20% 與 edge `find_gamma_flip()` 的搜尋區間（$[0.8, 1.2] \times \text{Spot}$）一致：edge 不可能算出超過此範圍的合法 Flip，超過者必為資料錯誤（例如 SPY 774.8 時的 948.9，+22.5%）。
- **下方 8%**：Flip 在現價下方代表正 Gamma（寬鬆）。誤丟只會讓判定降為未知（fail-closed，不開新倉），誤收遠端雜訊卻會把 regime 確定判成寬鬆，因此維持較嚴門檻。

**套用點（單一判定函式）**：
1. `fetch_gex_metrics()` 寫入端：edge 回傳離群、`gamma_flip` 缺值或 $\le 0$、帶 `is_fallback` 或舊版 510/515 常數時，一律不寫入 `macro_spy_gamma_flip`／`macro_gamma_flip_line`／`macro_gex_metrics_cache`，並標記 `macro_gex_is_fallback = 1`。
2. `fetch_gex_metrics()` 的 last-known-good 讀取端：快取內的離群值不再回傳。
3. `get_market_regime()`：以即時 SPY 報價重新判定，離群時 Flip 視為未知（三值邏輯，見 [`../macro_sentiment/01_macro_escape_top_matrix.md`](../macro_sentiment/01_macro_escape_top_matrix.md) §2.3）。
4. `/market` 面板（`get_macro_overview_data`）：KV 讀到離群值即丟棄並走自癒（重抓大盤端點 → SPY 個股備援估算）；自癒結果與 SPX 尺度換算後的翻轉線再過一次閘門，仍離群才降為 `None`。
5. 交易員終端 header：KV 的 Flip 相對 `macro_spy_spot` 離群時改顯示「快取數值離群，已濾除」。
6. `/force_macro_update`（`services/macro_refresh_service.py`）：離群時改走 SPY 即時估算，失敗則如實回報。

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
    CheckFlip -- 是 --> ApplyBracket["套用 Bracket 雜訊過濾:<br/>個股 0.7–1.3 × Spot；大盤備援 0.92–1.20 × Spot"]

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
| `bracket_low` | `(1 - bracket_pct) * Spot`，個股預設 `0.7 * Spot` ($-30\%$) | 翻轉線有效候選範圍下界，排除深價外雜訊 | `nexus_core/market_analysis/index_microstructure.py` |
| `bracket_high` | `(1 + bracket_above_pct) * Spot`，個股預設 `1.3 * Spot` ($+30\%$) | 翻轉線有效候選範圍上界，排除深價外雜訊 | `nexus_core/market_analysis/index_microstructure.py` |
| `MACRO_GEX_FLIP_MAX_ABOVE_SPOT_PCT` | `0.20` | 大盤 Flip 高於現價（short gamma 方向）的容許上限；與 edge `find_gamma_flip()` 搜尋區間一致；亦為大盤備援 bracket 上界 | `nexus_core/market_analysis/index_microstructure.py` |
| `MACRO_GEX_FLIP_MAX_BELOW_SPOT_PCT` | `0.08` | 大盤 Flip 低於現價（long gamma 方向）的容許上限；亦為大盤備援 bracket 下界 | `nexus_core/market_analysis/index_microstructure.py` |
| `GAMMA_FLIP_MATERIALITY_CANDIDATES` | `(0.02, 0.05, 0.10)` | 重要性比的離線比較候選門檻（§5.6），閘門不使用 | `nexus_core/market_analysis/index_microstructure.py` |
| `GAMMA_FLIP_MATERIALITY_DISPLAY` | `0.05` | 呈現層「雜訊交叉」標註與前向記錄分組門檻；**未經校準，不得用於閘門** | `nexus_core/market_analysis/index_microstructure.py` |
| `Gamma Flip Fallback` | `VWAP + 0.5 ATR_15m` | 當 Flip 估算為 0.0 且全鏈正 Gamma 時的替代突破門檻 | `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

1. **單邊純正或純負期權分佈之 Fail-Safe**：
   若標的期權鏈所有履約價的 GEX 全數為正（無任何負值）或全數為負（無任何正值），相鄰履約價對永不觸發 $g_{(i-1)} < 0 \le g_{(i)}$，演算法自然返回 `0.0`。下游進場條件一嚴格判定 `gamma_flip <= 0`，並啟動 Fallback 檢驗，防止拋出例外。
2. **方向矛盾候選點的自動消解**：
   在全鏈為正 Gamma（$G_{\text{total}} > 0$）但候選交叉點位於現價上方時，該點會被「方向一致性校驗」精準剔除並返回 `0.0`，杜絕向交易終端呈現自相矛盾的錯誤指標。
3. **資料解析例外保護**：
   若履約價或 GEX 曝險含有非法字元或為空字典，函式內部透過 `try-except` 捕獲並安全回傳 `0.0`，保證背景排程的穩健運行。

4. **局部 Gamma 體制（呈現層，雙向交叉）**：
   `estimate_symbol_gamma_flip()` 只偵測由負轉正的交叉，對「下方正 Gamma、上方負 Gamma」的鏈型一律回傳 `0.0`，而全鏈 Net GEX 為正時體制標籤仍顯示 LONG_GAMMA——現價其實已落入負 Gamma 區。`analyze_local_gamma_regime()` 以相鄰履約價線性內插求現價處淨 GEX $G(\text{Spot})$，並雙向搜尋離現價最近的零交叉：
   $$K^* = K_1 + \frac{0 - G_1}{G_2 - G_1}(K_2 - K_1),\quad \text{sign}(G_1) \ne \text{sign}(G_2),\ K^* \in [0.7\,\text{Spot}, 1.3\,\text{Spot}]$$
   分析中心在「⚙️ 體制判讀」加列「局部體制 (現價處)」，與全鏈體制相反時註明；閘門用的 flip 找不到交叉時，改列局部翻轉線與其方向（以上／以下轉負 Gamma）。刻意**另立函式**、不改 `estimate_symbol_gamma_flip()`：後者是右側／左側／做空進場與 Regime 分類器共用的門檻線，改動需先經 `calibration` 比對。

   **後續觀察事項（閘門是否參考局部體制，決定前不得改動閘門）**：
   - **第一步：先補記錄，不改行為**。前向蒐集目前只在 `regime_evaluation_log` 記錄 `gamma_flip`（單向定義）與全鏈 `net_gex`，沒有局部體制，無從驗證。應在 `evaluation_recorder.py` 的 `features_json` 加入 `analyze_local_gamma_regime()` 的 `local_gex`、`flip_strike`、`flip_side`，並補「加上記錄點後輸出逐位元不變」的測試（比照 `test_exit_tier_forward_collection.py`）。
   - **分組比較**：累積後以 `calibration forward-report` 將 `ENTRY_RIGHT`／`ENTRY_LEFT`／`REGIME_CLASSIFIER` 紀錄分成「全鏈 LONG 且局部 SHORT」與「全鏈 LONG 且局部 LONG」兩組，比較事後逆向先觸及率與 ±k×ATR 路徑標註。
   - **判讀準則**：每組 $n \ge 100$，且局部 SHORT 組的逆向先觸及率顯著較高（bootstrap CI 不重疊），才評估讓右側進場在局部負 Gamma 區降級或加嚴；否則維持 `estimate_symbol_gamma_flip()` 的單向定義。
   - **改動時的連帶工作**：右側條件一的 `VWAP + 0.5×ATR₁₅ₘ` 替代門檻（見 §4 `Gamma Flip Fallback`）是建立在「無交叉點＝回傳 0」的語意上；若閘門改用雙向交叉，必須同步重新定義替代門檻的觸發條件。

5. **離散履約價格點 vs 內插零軸（呈現層）**：
   `estimate_symbol_gamma_flip()` 回傳的是 $\mathcal{Z}$ 中的履約價 $K_{(i)}$（第一個非負的格點），真正的零軸落在 $K_{(i-1)}$ 與 $K_{(i)}$ 之間。例如 $g(225)=-1{,}434{,}309\text{K}$、$g(227.5)=+18{,}108{,}163\text{K}$ 時閘門取 $227.50$，內插零軸約 $225.18$。分析中心因此將標籤寫成「Gamma Flip (轉正履約價)」，並以 `interpolate_gamma_flip_zero()`（與 §5.4 同一條內插式，只取 $(K_{(i-1)}, K_{(i)})$ 這一對）加列「相鄰履約價內插零軸 ≈ …（閘門以履約價格點為準）」。閘門維持取格點：格點版本已是所有進場閘門與 Regime 分類器共用的門檻線，改成內插值會讓「站上 Flip」提早觸發，須先經 `calibration` 比對才能改。

6. **重要性門檻（呈現層＋校準中）**：
   第二步的交叉判定沒有量級要求，夾在兩個大正值之間的一檔微小負值就會構成 Flip。實例：2026-10-02 11:15 ET 的 `/x MU`，$g(1072.5)=-9.4\text{M}$ 夾在 $g(1070)=+4.16\text{B}$ 與 $g(1075)=+3.78\text{B}$ 之間（全鏈 +42.9B），閘門判為「Gamma Flip \$1075（緩衝 +0.04%）」，內插零軸 ≈ \$1072.51 也沒有意義。
   定義**重要性比**：
   $$\rho = \frac{\max\{|g_{(j)}| : K_{(j)} \text{ 屬於 } K_{(i-1)} \text{ 往下的連續負值區段}\}}{\max\{|g_{(j)}| : K_{(j)} \in [0.7\,\text{Spot}, 1.3\,\text{Spot}]\}}$$
   分母至少等於分子，所以 $\rho \in [0, 1]$。負側取「連續負值區段」的峰值而不是只看 $K_{(i-1)}$，因此 $-5\text{B}, -10\text{M}, +4\text{B}$ 這種鏈型不會因為緊鄰一檔很小就被誤判為雜訊。上例 $\rho \approx 0.2\%$。
   - `gamma_flip_materiality()`：回傳閘門 Flip 交叉點的 $\rho$。`estimate_material_gamma_flip(profile, spot, min_ratio)`：同一套五步流程，只多一條「候選 $\rho \ge$ `min_ratio`」；`min_ratio = 0` 時與 `estimate_symbol_gamma_flip()` 逐值相同（有隨機化等價測試保護）。
   - **呈現層**：`/x` 與 watchlist 面板在閘門 Flip 的 $\rho <$ `GAMMA_FLIP_MATERIALITY_DISPLAY`（5%）時加註「⚠️ 翻轉檔負值僅 …（視窗最大 |GEX| 的 …%），屬雜訊交叉；排除後 Flip: …（閘門仍以原值為準）」，`/x` 同時不再列內插零軸。「緩衝 %」照舊顯示，因為閘門仍用它。
   - **前向記錄**：`features_json` 記錄 `gamma_flip_raw`、`gamma_flip_ratio`、`gamma_flip_neg_peak`、`gamma_flip_material_5pct`（見 [`../architecture/05_calibration_harness_and_forward_collection.md`](../architecture/05_calibration_harness_and_forward_collection.md) §5.13）。
   - **離線報表**：`micro-report` 的 `GammaFlip重要性` 輸出 $\rho$ 分位，以及 2%／5%／10% 門檻下原始 Flip 被剔除、改選到其他履約價、完全消失的次數，用來挑選門檻；`forward-report` 的 `gamma_flip_materiality` 將閘門紀錄分成雜訊組與重要組，比較逆向先觸及率。
   - **判讀準則**：兩組皆 $n \ge 100$，且雜訊組逆向先觸及率的 bootstrap CI（依日期叢集）下界高於重要組 CI 上界，才提案讓 `estimate_symbol_gamma_flip()` 套用門檻；否則維持無門檻定義，只保留呈現層揭露。
   - **改閘門時的連帶工作**：右側條件一的 `VWAP + 0.5×ATR₁₅ₘ` Fallback 建立在「無交叉＝回傳 0」上，門檻會讓更多標的落入 Fallback，觸發條件須重新確認；`regime_classifier`、左側／做空進場、`opportunity_cost` 的測試夾具多以小量負值構造交叉，需逐一檢查。

---

### 5.x 局部體制揭露與後續觀察（2026-10-07）

`analyze_local_gamma_regime()` 以相鄰履約價的逐檔 GEX 線性內插作為「現價處」體制，履約價柱是密度而非現價的 Gamma 曲線，現價跨一檔即可能翻號（SPCX 實測 $165 = −624M、$167.5 = +113M）。全鏈與局部號相反時，/x 現附註此限制。

**後續觀察（僅進 calibration，不接閘門）**：以「掃描假設現價、重算總 GEX 曲線求零點」取代逐檔內插，再與前向價格路徑比較。

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/index_microstructure.py`：
  - 核心估算函式：`estimate_symbol_gamma_flip()`；大盤 SPY 備援：`estimate_macro_spy_gamma_flip()`
  - 大盤合理性閘門（§2.2）：`is_macro_gamma_flip_outlier()`、`MACRO_GEX_FLIP_MAX_ABOVE_SPOT_PCT`、`MACRO_GEX_FLIP_MAX_BELOW_SPOT_PCT`；套用於 `fetch_gex_metrics()`、`_compute_market_regime_uncached()`
- `nexus_core/cogs/unified_terminal/utils.py`：`get_macro_overview_data()` 的 KV 讀取端閘門與自癒路徑
- `nexus_core/services/macro_refresh_service.py`：`/force_macro_update` 的離群改走 SPY 即時估算
- `nexus_core/cogs/embed_builders/market_embeds.py`：交易員終端 header 的離群 Flip 濾除
  - 重要性門檻（呈現與校準，§5.6）：`gamma_flip_materiality()`、`estimate_material_gamma_flip()`、`GammaFlipMateriality`
  - 局部體制與雙向翻轉線（呈現層）：`analyze_local_gamma_regime()`、`LocalGammaRegime`
  - 內插零軸（呈現層）：`interpolate_gamma_flip_zero()`
- `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py`：
  - 右側突破引用與 Fallback 替代：`_confirm_entry_condition1_breakout()`（第 55–255 行）
- `nexus_core/market_analysis/dynamic_rollover/regime_classifier.py`：
  - 6-Regime 路由引用：`classify_dynamic_regime()`
- `nexus_core/cogs/embed_builders/portfolio_embeds.py`：Symbol Hub 個股 GEX Flip 線呈現
- `nexus_core/cogs/embed_builders/_embed_helpers.py`：`gamma_flip_noise_note()` 雜訊交叉揭露文字（`portfolio_embeds.py`、`watchlist_embeds.py` 共用）
- `nexus_core/market_analysis/evaluation_recorder.py`：`features_json` 的 Gamma Flip 重要性欄位
- `nexus_core/calibration/microstructure.py`：`gamma_flip_materiality_stats()`；`nexus_core/calibration/forward_log.py`：`gamma_flip_materiality` 前向研究
