# 右側動能進場六重鐵律技術規格書

## 1. 核心哲學與適用市場環境

右側動能突破交易（Right-Side Momentum Breakout）的核心哲學在於「順應做市商對沖驅動力，絕不在無結構支撐的半山腰猜測底部」。當股價向上突破關鍵結構臨界點時，做市商的 Gamma 曝險由負翻正，其避險行為將由「追漲殺跌順向砸盤」轉化為「逢低買入自穩定護盤」；若此時疊加機構主力的跨週期認可與充足的向上獲利空間，突破的勝率與期望值將達到極致。

在 Nexus Seeker 系統中，右側六重鐵律是以下兩大核心業務管線的**唯一放行閘門**：
1. **機會成本轉倉 (`OPPORTUNITY_COST`)**：當現有衛星部位動能衰退，系統欲調度資金轉向更具爆發力的候選標的（Candidate）時，該候選標的必須 100% 通過六重鐵律。
2. **核心資金部署 (`CORE_DEPLOYMENT` 機會分支)**：當核心防禦資產（如 VOO）超額配置欲部署至進攻性衛星標的時，亦強制要求標的滿足六重鐵律。

### 階梯過濾與短路優化哲學
六重鐵律設計具備嚴格的計算階梯：
- **前四項條件（條件一至四）**：純記憶體與快取內的量價微觀結構運算，不依賴耗時的外部 I/O。
- **後兩項條件（條件五與六）**：涉及財報行事曆、總經狀態與標的期權到期鏈的實體網路 I/O。只有當前四項條件全數綠燈放行時，系統才會發動條件五與六的外部查詢。若前置條件失敗，條件五與六將短路略過，但仍在回報清單中補上「⏭️ 略過」標記，確保使用者在前端「進場鐵律檢核面板」永遠看到完整的六項檢驗，維持介面知情權的一致性。

---

## 2. 數學模型與量化推導

### 2.1 條件一：結構性右側放量突破與 Gamma Flip 替代模型
標的必須在 15 分鐘級別展現結構性放量突破，排除假突破灌壓。

1. **基本放量與站穩條件**：
   $$
   \text{Close}_{15m} > \text{GammaFlip}
   $$
   $$
   \text{Volume}_{15m} \ge \overline{\text{Volume}}_{20} \times \text{\_ENTRY\_VOLUME\_SURGE\_MULTIPLIER} = 1.5
   $$
   $$
   \text{Close}_{15m} > \text{Open}_{15m} \quad (\text{實體陽線，排除陰線放量摜壓})
   $$
   $$
   \text{Close}_{15m} > \text{SessionVWAP}
   $$

2. **極端單邊 Gamma 分佈與 Fallback 替代方案**：
   當標的期權鏈分佈極度單邊，導致累積 GEX 無法在有效區間內捕捉到零交叉點（$\text{GammaFlip} \le 0$）時：
   - **若全鏈動態 $\text{Net GEX} < 0$**：確認標的處於全域 Short Gamma 泥淖，做市商對沖行為為助跌順向拋售，判定為結構性空頭，條件一**直接不通過**。
   - **若全鏈動態 $\text{Net GEX} > 0$**：代表全鏈做市商整體處於正 Gamma 吸收波動的自穩定狀態。此時若強求 Gamma Flip 門檻將造成「誤殺」，故啟用 Fallback 替代突破門檻：
     $$
     \text{Breakout Threshold} = \text{SessionVWAP} + 0.5 \times \text{ATR}_{15m}
     $$
     要求 15 分鐘收盤價站穩該門檻：$\text{Close}_{15m} > \text{Breakout Threshold}$。
   - 若數據缺失或 $\text{Net GEX} == 0$：Fail-Safe 判定未通過。

### 2.2 條件二：做市商正 Gamma 底牆完好與物理約束
進場點下方必須有實質的做市商被動買盤作為防守護甲。

1. **底牆現價物理約束定理**：
   支撐位在物理定義上必須位於現價下方。掃描範圍強制約束在現價下方：
   $$
   \text{Support Wall} = \operatorname{argmax}_{K < \text{Spot}} \big(\text{Net GEX}(K)\big)
   $$
   - 若現價下方無正 GEX 峰值，或該峰值低於薄紙牆門檻 $\text{GEX\_THIN\_WALL\_THRESHOLD} = 500,000$，則 $\text{Support Wall} = 0.0$，條件二直接判定未通過。此約束徹底杜絕了將現價上方龐大的 Call Wall 阻力誤判為支撐底牆的系統性缺陷。
2. **即時有效防禦距離約束**：
   $$
   0 < \frac{\text{Spot} - \text{SupportWall}}{\text{Spot}} \le \text{\_ENTRY\_SUPPORT\_WALL\_MAX\_DISTANCE\_PCT} = 0.05 \quad (5\%)
   $$
   現價距離支撐牆超過 5% 視為缺乏即時保護（防守線過遠）。

### 2.3 條件三：做市商阻力結構與非對稱空間
1. **無 UOA 巨鯨物理封頂**：
   以 Call Wall 作為履約價基準，調用 `detect_uoa_sto_call_physical_cap`，嚴禁存在 $\text{Ratio} \ge \text{\_ENTRY\_UOA\_CAP\_RATIO\_THRESHOLD} = 1.5\text{x}$ 且 $\text{Strike} \ge \text{CallWall}$ 的單筆 STO Call 大單。
2. **非對稱向上獲利空間（帶正負號距離）**：
   $$
   \Delta_{\text{CallWall}} = \frac{\text{CallWall} - \text{Spot}}{\text{Spot}} \ge \text{\_ENTRY\_ASYMMETRIC\_ROOM\_PCT} = 0.05 \quad (5\%)
   $$
   若現價已觸及或跌破 Call Wall（距離為負值），視為做市商壓制仍在且向上空間耗竭，拒絕進場。

### 2.4 條件四：主力跨週期買盤認證與雜訊過濾
必須在 UOA 清單（按名目價值 `notional_value` 降序排列）中尋找到至少一筆機構級 CALL BTO，同時滿足四重過濾：
1. **合約效期**：$\text{DTE} \ge \text{\_ENTRY\_UOA\_MIN\_DTE} = 7$ 天（排除 0~6 DTE 賭博或日內造市單）。
2. **異動放量比**：$\text{Ratio} (\text{Volume}/\text{OI}) \ge \text{\_ENTRY\_UOA\_MIN\_RATIO} = 0.8\text{x}$。
3. **權利金名目價值**：
   $$
   \text{Notional Value} = \text{Trade Price} \times \text{Volume} \times 100 \ge \text{\_ENTRY\_UOA\_MIN\_NOTIONAL\_USD} = \$200,000
   $$
4. **履約價物理約束**：$\text{Strike} \ge \text{Spot}$（排除深價內實值 Call 買單，後者通常為 Delta 對沖或領息操作，非實質方向性進攻）。

### 2.5 條件五：二元宏觀與財報事件安全閥
前四項通過後發動實體檢查：
1. **距財報公佈天數**：
   $$
   \text{Days to Earnings} > \text{\_EARNINGS\_PRE\_EVENT\_BUFFER\_DAYS} = 3 \quad (\text{天})
   $$
   避開財報前夕隱含波動率崩塌（IV Crush）與二元跳空風險。
2. **大盤系統性風控狀態**：
   $$
   \text{Macro Regime} \notin \{\text{"SHORT\_GAMMA\_CRITICAL"}, \text{"SYSTEMIC\_LIQUIDITY\_CRISIS"}\}
   $$

### 2.6 條件六：標的自身最近效期選擇權週期雜訊過濾與建議進場結構
1. 抓取標的完整選擇權到期日列表，取得最近效期合約到期天數 $\text{DTE}_{\text{nearest}}$。
2. 雜訊過濾約束（**唯一決定本條件 Pass/Fail 的判準**）：
   $$
   \text{DTE}_{\text{nearest}} > \text{\_ENTRY\_CANDIDATE\_MIN\_DTE} = 1 \quad (\text{天})
   $$
   嚴禁在標的自身處於 0/1 DTE 結算日當日或前夕開倉，徹底規避末日合約結算引發的做市商流動性抽離與劇烈針狀洗盤。

3. **建議進場結構 (`structure_directive`) 之推導**：
   通過上述判準後，額外輸出一組「建議合約天期 (DTE band) 與部位結構」。此輸出**不參與 Pass/Fail 判定**，且三個輸入全部取自六重鐵律評估過程中本來就已算好／查到的值（條件三的 Call Wall 空間、`candidate_radar` 既有的 IV Rank、條件五既有的距財報天數），**零額外網路 I/O**。

   - **天期分流（跑道長度）**：
     $$
     [\text{DTE}_{lo}, \text{DTE}_{hi}] =
     \begin{cases}
     (21, 45) \text{ 波段} & \text{若} \Delta_{\text{CallWall}} \ge \text{\_ENTRY\_ROOM\_EXTENDED\_PCT} = 10\% \\
     (7, 21) \text{ 短線} & \text{否則}
     \end{cases}
     $$
     物理意義：目標價位就是上方的 Call Wall。空間不足 10% 代表目標近在咫尺，不需要長跑道承擔額外 Theta；空間充足才值得用波段天期換取行情延展。現價已觸及或跌破 Call Wall（$\Delta_{\text{CallWall}} < 0$）一律歸入短線。

   - **財報收斂**：
     $$
     \text{若 } 0 < \text{DaysToEarnings} < \text{DTE}_{hi}
     \;\Rightarrow\;
     \text{DTE}_{hi} \leftarrow \text{DaysToEarnings},\;
     \text{DTE}_{lo} \leftarrow \min(\text{DTE}_{lo}, \text{DTE}_{hi})
     $$
     不建議持有跨越財報的合約。條件五已保證距財報 $> 3$ 天，故此處只會收斂、不會歸零；$\text{DTE}_{lo}$ 一併塌陷是為了避免輸出 `21-10` 這類反向區間。

   - **結構分流（IVR）**：與左側條件六**共用同一門檻、同一理由**：
     $$
     \begin{cases}
     \text{Long Call (ATM/輕度 OTM)} & \text{若 IVR} \le \text{\_ENTRY\_IVR\_SPREAD\_THRESHOLD} = 50\% \\
     \text{Bull Call Spread} & \text{若 IVR} > 50\%
     \end{cases}
     $$
     高隱波位階下開單腳買方，一旦行情兌現引發 IV Crush 將遭遇 Vega 崩塌，故強制轉為垂直價差。

4. **設計原則：天期是輸出參數，不是部位標籤**。
   `structure_directive` 於**每一輪重評時依當下市況重新計算**，不會在進場當下蓋章後固定於部位上。這是刻意的設計約束：`transition_engine.py` 早期版本的路徑 2/3/4 正是因為把部位存亡綁在進場時貼上、之後永不更新的 `entry_regime` 標籤而被整批移除（演化後的部位因標籤陳舊而失去所有例行停損）。天期恰恰是最容易隨市況改變的屬性，若以標籤實作必然重蹈覆轍。

5. **與 `suggested_strategy` 的職責分界**：
   `structure_directive` 以**獨立欄位**攜帶於 `RolloverInstruction`，**不覆寫** `suggested_strategy`。後者由 `_calculate_rollover_decision()` 自行決策「用什麼工具進場」（`Buy Shares` / `Shares + ITM Call`），前者回答「若以期權表達，該選哪個天期與結構」，兩者互補而非互斥。若以覆寫實作，`Shares + ITM Call` 連同它自帶的 ITM 70Δ 履約價與 30-45 DTE 指引會被一併抹除。

   ⚠️ **核心資金部署 (Scenario 5) 機會分支刻意不消費此欄位**：該分支產生的指令為 `instrument_type="SPOT"` / `suggested_strategy="Buy Shares"`，是把 CORE 超額現金部署為候選標的的**現貨股票**，附上期權天期／價差建議會直接誤導使用者。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([開始: 候選標的右側進場六重鐵律檢核]) --> C1{"條件一: 結構性突破<br/>15m 實體陽線 + 放量 1.5x<br/>站穩 VWAP + GammaFlip / Fallback?"}

    C1 -- 失敗 --> Fail1[條件一❌: 突破未確認] --> StopFail([進場未通過: 拒絕轉倉/部署])
    C1 -- 通過 --> C2{"條件二: 做市商正 Gamma 底牆<br/>Support Wall 位於現價下方 (K < Spot)?<br/>距離現價 <= 5% 且 GEX >= 500k?"}

    C2 -- 失敗 --> Fail2[條件二❌: 缺乏有效正 Gamma 底牆] --> StopFail
    C2 -- 通過 --> C3{"條件三: 阻力空間與物理封頂<br/>Call Wall 距離 >= 5%?<br/>無 STO Call 巨鯨封頂?"}

    C3 -- 失敗 --> Fail3[條件三❌: 上方空間受阻或存在封頂] --> StopFail
    C3 -- 通過 --> C4{"條件四: 主力跨週期買盤<br/>存在 CALL BTO: DTE >= 7<br/>Ratio >= 0.8x, 名目 >= $200k<br/>Strike >= Spot?"}

    C4 -- 失敗 --> Fail4[條件四❌: 無主力跨週期買盤背書] --> StopFail
    C4 -- 通過 --> TriggerIO[前四項全數通過: 發動實體外部 I/O]

    TriggerIO --> C5{"條件五: 總經與財報安全閥<br/>距離財報 > 3 天?<br/>大盤非 SHORT_GAMMA / 危機?"}
    C5 -- 失敗 --> Fail5[條件五❌: 遭遇總經踩踏或財報黑天鵝] --> StopFail
    C5 -- 通過 --> C6{"條件六: 標的自身 DTE 雜訊<br/>最近效期 DTE > 1 天?"}

    C6 -- 失敗 --> Fail6[條件六❌: 標的處於 0/1 DTE 結算雜訊期] --> StopFail
    C6 -- 通過 --> Room{"建議進場結構推導 (不影響 Pass/Fail)<br/>Call Wall 空間 >= 10%?"}

    Room -- "是 (延伸跑道)" --> BandSwing["DTE band = 21-45 波段"]
    Room -- "否 (目標貼近)" --> BandShort["DTE band = 7-21 短線"]
    BandSwing --> EarnCap{"財報落在 band 區間內?"}
    BandShort --> EarnCap
    EarnCap -- 是 --> Capped["band 上限收斂至財報前<br/>(下限一併塌陷，避免反向區間)"] --> CheckIVR
    EarnCap -- 否 --> CheckIVR{標的 IVR 位階評估}

    CheckIVR -- "IVR <= 50.0%" --> PassCall(["六重鐵律全數通過 ✅<br/>建議結構: Long Call (ATM/輕度 OTM)"])
    CheckIVR -- "IVR > 50.0%" --> PassSpread(["六重鐵律全數通過 ✅<br/>建議結構: Bull Call Spread (防 Vega 崩塌)"])

    %% 短路註記
    StopFail -. 前四項失敗時 .-> SkipC5C6[標記: 條件五/六 ⏭️ 略過]
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 | 數值 / 門檻 | 物理 / 代碼約束 | 程式碼檔案路徑 |
| :--- | :--- | :--- | :--- |
| `_ENTRY_VOLUME_LOOKBACK_BARS` | `20` | 15m K 線均量回看根數（排除當前根） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_VOLUME_SURGE_MULTIPLIER` | `1.5` | 15m 突破放量倍數門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT` | `0.05` ($5\%$) | 現價距離正 Gamma 支撐底牆之最大防禦距離 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_UOA_CAP_RATIO_THRESHOLD` | `1.5` | 單筆 STO Call 判定為物理封頂之最低 Volume/OI 比值 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_ASYMMETRIC_ROOM_PCT` | `0.05` ($5\%$) | Call Wall 距現價最低非對稱獲利空間 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_UOA_MIN_DTE` | `7` | 主力 UOA 買盤最低到期天數 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_UOA_MIN_RATIO` | `0.8` | 主力 UOA 買盤最低 Volume/OI 比值 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_UOA_MIN_NOTIONAL_USD` | `$200,000.0` | 主力 UOA 買盤最低權利金名目金額 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_EARNINGS_PRE_EVENT_BUFFER_DAYS` | `3` | 避開財報發布的最小安全天數緩衝 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_CANDIDATE_MIN_DTE` | `1` | 標的自身最近效期選擇權最低 DTE 要求（避開 0/1 DTE） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_ROOM_EXTENDED_PCT` | `0.10` ($10\%$) | Call Wall 空間達此值視為「延伸跑道」，建議波段天期（僅影響建議文字） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_DTE_BAND_SHORT` | `(7, 21)` | 短線建議 DTE band，沿用 `transition_engine.py` 路徑一加碼的既有天期慣例 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_DTE_BAND_SWING` | `(21, 45)` | 波段建議 DTE band，沿用既有「次月合約」慣例 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_IVR_SPREAD_THRESHOLD` | `50.0` ($50\%$) | IVR 高於此值改建議 Bull Call Spread（與左側 `_LEFT_ENTRY_IVR_SPREAD_THRESHOLD` 同值同理由） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `GEX_THIN_WALL_THRESHOLD` | `500,000.0` | 做市商有效正 Gamma 牆體之最低曝險深度門檻 | `nexus_core/market_analysis/index_microstructure.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

1. **底牆現價物理約束之嚴格執行**：
   在 `_scan_gex_walls` 中，若現價下方無任何正 GEX 峰值，支撐牆回傳 `0.0`。條件二代碼嚴格檢查 `support_wall > 0` 與 `dist_pct <= 0.05`，防止在無支撐保護的懸崖結構中進場。
2. **全鏈 Short Gamma 泥淖即時熔斷**：
   在條件一中，若 `gamma_flip_est <= 0` 且 `effective_net_gex < 0`，系統立即終止後續判定，回報「全域 Short Gamma 泥淖，結構性空頭直接不通過」，防止在市場極端單邊下殺時誤觸發突破買進。
3. **外部行事曆與到期日異常防呆**：
   若財報快取讀取失敗，或無法取得選擇權到期日清單，系統遵循 Fail-Closed 原則，一律判定該條件未通過，拒絕承擔未知的事件風險。
4. **短路展示一致性保證**：
   當前四項條件有任一項未通過時，條件五與六不會發起任何 HTTP 或資料庫查詢，但在 `reasons` 陣列中主動寫入 `條件五⏭️` 與 `條件六⏭️`，避免前端視圖只渲染四項條件造成的使用者混淆。

---

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py`：
  - 核心檢核入口：`_confirm_entry_signal()`（第 801 行起，回傳 `(是否通過, 逐項原因, structure_directive)`）
  - 條件一實作：`_confirm_entry_condition1_breakout()`（第 59 行起）
  - 條件二實作：`_confirm_entry_condition2_support_wall()`（第 261 行起）
  - 條件三實作：`_confirm_entry_condition3_no_physical_cap()`（第 309 行起）
  - 條件四實作：`_confirm_entry_condition4_uoa_dte()`（第 355 行起）
  - 條件五實作：`_confirm_entry_condition5_macro_earnings_gate()`（第 401 行起，另回傳距財報天數供條件六收斂 DTE band）
  - 建議結構推導：`_derive_entry_structure_directive()`（第 461 行起，純函式、零 I/O）
  - 條件六實作：`_confirm_entry_condition6_candidate_dte()`（第 509 行起）
- `nexus_core/market_analysis/dynamic_rollover/models.py`：`RolloverInstruction.structure_directive` 欄位
- `nexus_core/cogs/embed_builders/rollover_embeds.py`：`create_dynamic_rollover_embed(structure_directive=...)` 於「📥 轉入資產」區塊渲染
- `nexus_core/cogs/embed_builders/portfolio_embeds.py`：`create_entry_rules_embed(structure_directive=...)` 於「🔐 進場鐵律檢核」頁籤渲染「🎯 建議進場結構」欄位
- `nexus_core/market_analysis/dynamic_rollover/constants.py`：具名常數 `_ENTRY_*`
- `nexus_core/market_analysis/dynamic_rollover/structural_signals.py`：支撐牆掃描 `_scan_gex_walls()`
- `nexus_core/market_analysis/index_microstructure.py`：`estimate_symbol_gamma_flip()`, `detect_uoa_sto_call_physical_cap()`, `get_market_regime()`
- `nexus_core/market_analysis/vwap_utils.py`：`fetch_session_vwap()`
- `nexus_core/market_analysis/atr_utils.py`：`fetch_atr_15m()`
- `nexus_core/database/calendar_cache.py`：財報快取查詢 `get_cached_earnings()`
