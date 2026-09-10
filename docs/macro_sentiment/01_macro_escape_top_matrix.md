# 宏觀逃頂推演矩陣與流動性退潮防禦規格書

---

## 1. 核心哲學與適用市場環境

### 1.1 核心哲學
在現代美股選擇權市場中，做市商（Market Makers）的流動性供應與 Gamma 曝險水位，根本性地受到總體經濟貨幣政策與資金利率定價的制約。傳統技術分析的擇時指標（例如均線死叉或超買振盪器指標）屬於落後指標，當個股價格確認向下破位時，做市商往往早已因流動性退潮而撤除流動性買單，引發買賣價差（Bid-Ask Spread）擴大與滑價衝擊。

Nexus Seeker 確立**「宏觀流動性引導窗口平移，微觀微結構確認踩踏臨界」**之核心哲學，建構兩套互為表裡的宏觀防禦引擎：
1. **即時流動性窗口平移矩陣（Escape Window Regime Matrix）**：監控 FOMC 聯準會利率期貨、通膨/能源（CPI/WTI）、VIX 期限結構與大盤 Net GEX，動態計算轉倉與防禦窗口的平移天數（前移收縮 vs 後推擴張）。
2. **五因子複合逃頂評分階梯（Macro Top-Escape Score）**：疊加極端市場情緒（CNN Fear & Greed 指數）與使用者衛星持倉的亢奮廣度（Euphoria Breadth），形成多頭極致過熱的領先量化預警階梯，直接聯動動態轉倉引擎之情境 6（Scenario 6），強制將 25% 衛星曝險防禦性輪動至無風險現金等價物 BOXX。

### 1.2 適用市場環境與制度角色
- **牛市末期極度亢奮**：當市場指數屢創新高、零售情緒陷入極度貪婪，但利率期貨已悄然隱含鷹派緊縮、或 VIX 期限結構出現倒掛時，系統提前收緊持倉窗口。
- **降息預期落空與流動性再定價**：在 FOMC 會議前夕，若利率定價發生劇烈階梯位移，系統主動提前逃頂防禦窗口，防範流動性踩踏。
- **大盤負 Gamma 踩踏加速區間**：當大盤微觀結構跌入負 Gamma 區間時，做市商將由「逢低買進逆向穩定」轉為「順向追殺放量對沖」，系統將緊縮閥門調至最高警戒。

---

## 2. 數學模型與量化推導

### 2.1 CME 30 天聯邦基金期貨 (ZQ) 官方階梯定價反推模型
聯邦公開市場委員會（FOMC）利率決策定價由芝加哥商業交易所（CBOT）30 天期聯邦基金期貨合約（合約代號格式 `ZQ{MonthCode}{YearSuffix}.CBT`）即時反推。

期貨報價 $P_{ZQ}$ 反映該月份隔夜有效聯邦基金利率（EFFR）的日曆日算術平均值：
$$\bar{R} = 100.0 - P_{ZQ}$$

設當前目標 FOMC 會議月份的總日曆天數為 $N$。會議召開於該月第 $d_{prior}$ 天，則會議前維持舊利率 $R_1$ 之天數為 $d_{prior}$ 天，會議後新基準利率 $R_2$ 之生效天數為：
$$d_{post} = \max(1, N - d_{prior})$$

其中舊基準利率 $R_1$ 由前一月份連續期貨合約報價錨定：$R_1 = 100.0 - P_{prior}$；若無前月報價，則以 13 週美國國庫券利率（`^IRX`）動態推導之利率區間中位數作為錨點。

根據日曆加權平均等式：
$$N \cdot \bar{R} = d_{prior} \cdot R_1 + d_{post} \cdot R_2$$

推導出會議後隱含目標利率 $R_2$ 與利率變動預期 $\Delta R$：
$$R_2 = \frac{N \cdot \bar{R} - d_{prior} \cdot R_1}{d_{post}}$$
$$\Delta R = R_2 - R_1$$

以聯準會標準 25 個基點（25 bp = 0.25%）為一階梯單位，進行多桶階梯分解（Ladder Decomposition）：
$$\text{steps} = \frac{|\Delta R|}{0.25}$$
$$\text{floor\_steps} = \lfloor \text{steps} \rfloor$$
$$\text{frac\_pct} = \operatorname{round}\left( (\text{steps} - \text{floor\_steps}) \times 100.0, 1 \right)$$
$$\text{ladder\_saturated} = (\text{floor\_steps} \ge 1)$$

在降息情境（$\Delta R < 0$）下的離散機率分佈推導如下：
$$P_{\text{cut}} = \begin{cases} \text{frac\_pct} & \text{floor\_steps} = 0 \\ 100.0 & \text{floor\_steps} \ge 1 \end{cases}$$
$$P_{\text{cut\_25}} = \begin{cases} \text{frac\_pct} & \text{floor\_steps} = 0 \\ 100.0 - \text{frac\_pct} & \text{floor\_steps} = 1 \\ 0.0 & \text{floor\_steps} \ge 2 \end{cases}$$
$$P_{\text{cut\_50}} = \begin{cases} 0.0 & \text{floor\_steps} = 0 \\ \text{frac\_pct} & \text{floor\_steps} = 1 \\ 100.0 & \text{floor\_steps} \ge 2 \end{cases}$$
$$P_{\text{maintain}} = 100.0 - P_{\text{cut}}$$

在升息情境（$\Delta R \ge 0$）下的機率分佈採對稱推導。

### 2.2 鷹派傾向純量壓縮分數 (Hawkish Intensity Probability)
為了將離散的升息/降息百分比轉化為可納入全域風控矩陣的連續純量 $prob \in [0.05, 0.95]$，系統實施以下分段壓縮映射：
$$prob = \begin{cases}
\min\left(0.95, \frac{50.0 + P_{\text{hike}} / 2.0}{100.0}\right) & \text{若 } P_{\text{hike}} \ge 50.0 \lor (P_{\text{hike}} \ge 30.0 \land P_{\text{hike}} > P_{\text{cut}} \land P_{\text{hike}} > P_{\text{maintain}}) \\
\max\left(0.05, \frac{50.0 - P_{\text{cut}} / 2.0}{100.0}\right) & \text{若 } P_{\text{cut}} \ge 50.0 \lor (P_{\text{cut}} \ge 30.0 \land P_{\text{cut}} > P_{\text{hike}} \land P_{\text{cut}} > P_{\text{maintain}}) \\
\operatorname{clamp}\left(0.05, 0.95, \frac{50.0 + (P_{\text{hike}} - P_{\text{cut}}) / 2.0}{100.0}\right) & \text{其餘中性維持或分歧情境}
\end{cases}$$

### 2.3 四因子宏觀流動性矩陣與窗口平移推導
`evaluate_escape_window_regime` 整合四項宏觀總經與微觀結構因子：
1. **因子 1：FedWatch 利率定價**
   $$\text{緊縮評分 } T_1 = \mathbb{I}(prob > 0.70), \quad \text{寬鬆評分 } E_1 = \mathbb{I}(prob \le 0.40)$$
2. **因子 2：通膨與原油衝擊 (CPI / WTI)**
   $$\text{緊縮評分 } T_2 = \mathbb{I}(cpi\_dev > 0.1 \lor wti > 85.0), \quad \text{寬鬆評分 } E_2 = \mathbb{I}(cpi\_dev \le 0.0 \land wti \le 80.0)$$
3. **因子 3：VIX 期限結構比例 (VTS Ratio)**
   $$VTS = \frac{VIX}{VIX3M}$$
   $$\text{緊縮評分 } T_3 = \mathbb{I}(VTS \ge 1.0), \quad \text{寬鬆評分 } E_3 = \mathbb{I}(VTS < 0.90)$$
4. **因子 4：大盤微觀結構 Net GEX 狀態**
   $$\text{緊縮評分 } T_4 = \mathbb{I}(is\_negative\_gamma = \text{True}), \quad \text{寬鬆評分 } E_4 = \mathbb{I}(is\_negative\_gamma = \text{False})$$

加總總緊縮分 $T = \sum_{i=1}^4 T_i$ 與總寬鬆分 $E = \sum_{i=1}^4 E_i$。窗口位移天數 $\Delta D_{shift}$ 與狀態分級遵循：
$$\Delta D_{shift} = \begin{cases}
-8 \text{ 天 (前移)} & \text{若 } T \ge 3 \\
-5 \text{ 天 (前移)} & \text{若 } T = 2 \lor (prob > 0.70 \land is\_negative\_gamma) \\
+5 \text{ 天 (後推)} & \text{若 } prob \le 0.40 \land E \ge 2 \land T = 0 \\
0 \text{ 天 (維持)} & \text{其餘中性平衡情況}
\end{cases}$$

### 2.4 五因子複合逃頂評分階梯 (Macro Top-Escape Score)
`evaluate_macro_top_escape_score` 計算逃頂綜合評分 $S_{esc} \in [0, 5]$，融合持倉端微觀過熱廣度：
$$S_{esc} = \mathbb{I}(VTS \ge 1.0) + \mathbb{I}(FearGreed \ge 75.0) + \mathbb{I}(prob > 0.70) + \mathbb{I}(is\_negative\_gamma) + \mathbb{I}(Ratio_{sat} \ge 0.50)$$

其中衛星持倉亢奮比例 $Ratio_{sat}$ 計算如下：
$$Ratio_{sat} = \frac{\sum_{k \in \text{Satellite}} \left[ \mathbb{I}(Spot_k \ge CallWall_k \lor \frac{|Spot_k - CallWall_k|}{CallWall_k} < 0.005) \lor \mathbb{I}(Skew_k < 0 \land SkewPct_k \le 20.0\%) \right]}{N_{\text{Satellite}}}$$

若使用者未持有任何衛星持倉，則該項傳入 `None`，不參與評分。分級判斷：
$$\text{Tier} = \begin{cases}
\text{CRITICAL (🚨🚨 逃頂確認)} & S_{esc} \ge 3 \\
\text{ELEVATED (🚨 逃頂警戒)} & S_{esc} = 2 \\
\text{WATCH (⚠️ 前哨觀察)} & S_{esc} = 1 \\
\text{NORMAL (🟢 常態)} & S_{esc} = 0
\end{cases}$$

### 2.5 聯動情境 6 宏觀逃頂防禦與 25% BOXX 輪動資金模型
當評分達最高階 $\text{Tier} = \text{CRITICAL}$ 時，動態轉倉引擎啟動情境 6 防禦：
$$V_{trim} = _MACRO\_TOP\_ESCAPE\_TRIM\_RATIO \times V_{satellite} = 0.25 \times V_{satellite}$$
去化目的地嚴格限制為無風險現金等價物 `BOXX`，嚴禁轉入股票大盤 ETF（如 VOO），防範系統性系統風險下股債同跌。

---

## 3. 決策邏輯與狀態機 / 流程圖

### 3.1 總經流動性評估與逃頂推演決策流程

```mermaid
flowchart TD
    Start([排程觸發: 04:00/08:45/15m 心跳]) --> FetchMacro[抓取 CME ZQ 期貨, CPI, WTI, VTS, Net GEX]
    FetchMacro --> SanityCheck{數值合理性閘門<br/>Sanity Gating}

    SanityCheck -- 異常/未解釋飽和 --> FallbackState[啟動 Atlanta Fed Excel 或 0.50 備援<br/>macro_fedwatch_is_fallback=1]
    SanityCheck -- 數值正常 --> CalculateProb[反推 R2 並計算緊縮分數 prob]

    FallbackState --> EscapeRegime[評估四因子流動性矩陣<br/>evaluate_escape_window_regime]
    CalculateProb --> EscapeRegime

    EscapeRegime --> WindowShift{矩陣狀態評估}
    WindowShift -- "T >= 2 或 (prob>0.7 且 負Gamma)" --> ShiftEarly[🚨 收縮警戒: 逃頂窗口前移 5~8 天]
    WindowShift -- "prob<=0.4 且 E>=2 且 T==0" --> ShiftLate[🟢 寬鬆擴張: 逃頂窗口後推 5 天]
    WindowShift -- 其他中性條件 --> ShiftNeutral[🟡 中性平衡: 窗口維持 0 天]

    ShiftEarly --> TopEscapeScore[計算五因子複合逃頂評分<br/>evaluate_macro_top_escape_score]
    ShiftLate --> TopEscapeScore
    ShiftNeutral --> TopEscapeScore

    TopEscapeScore --> CheckTier{分級判定}
    CheckTier -- "Score >= 3" --> CriticalTier[🚨🚨 CRITICAL 逃頂確認]
    CheckTier -- "Score == 2" --> ElevatedTier[🚨 ELEVATED 逃頂警戒]
    CheckTier -- "Score == 1" --> WatchTier[⚠️ WATCH 前哨觀察]
    CheckTier -- "Score == 0" --> NormalTier[🟢 NORMAL 常態]

    CriticalTier --> Scenario6Gate{使用者開啟<br/>enable_macro_top_escape_defense?}
    Scenario6Gate -- 是 --> ExecScenario6[觸發轉倉情境 6:<br/>衛星持倉防禦性減碼 25% 轉入 BOXX]
    Scenario6Gate -- 否 --> EndReport[僅呈現終端 Embed 警報]
    ElevatedTier --> EndReport
    WatchTier --> EndReport
    NormalTier --> EndReport
    ExecScenario6 --> EndReport([結束])
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 | 數值 / 門檻 | 物理約束與代碼意涵 | 程式碼檔案路徑 |
| :--- | :--- | :--- | :--- |
| `_MACRO_ESCAPE_WATCH_THRESHOLD` | `1` | 逃頂評分 WATCH 前哨觀察門檻 | `nexus_core/market_analysis/index_microstructure.py:934` |
| `_MACRO_ESCAPE_ELEVATED_THRESHOLD` | `2` | 逃頂評分 ELEVATED 警戒門檻 | `nexus_core/market_analysis/index_microstructure.py:935` |
| `_MACRO_ESCAPE_CRITICAL_THRESHOLD` | `3` | 逃頂評分 CRITICAL 確認門檻（啟動情境 6 減碼） | `nexus_core/market_analysis/index_microstructure.py:936` |
| `_MACRO_ESCAPE_BREADTH_TRIGGER_RATIO`| `0.5` | 衛星持倉亢奮廣度門檻（超過 50% 標的觸頂則觸發因子） | `nexus_core/market_analysis/index_microstructure.py:937` |
| `_FEAR_GREED_EXTREME_GREED_BOUND` | `75.0` | CNN 恐懼貪婪指數極度貪婪臨界值 | `nexus_core/market_analysis/constants.py:141` |
| `_MACRO_TOP_ESCAPE_TRIM_RATIO` | `0.25` (25%) | 情境 6 防禦性減碼比例（轉入 BOXX） | `nexus_core/market_analysis/dynamic_rollover/constants.py:140` |
| `_MACRO_TOP_ESCAPE_MIN_TIER` | `"CRITICAL"` | 情境 6 啟動最低門檻階級 | `nexus_core/market_analysis/dynamic_rollover/constants.py:143` |
| `_PROFIT_UNLOCK_TOLERANCE` | `0.005` (0.5%) | 衛星持倉現價逼近 Call Wall 的判斷容差 | `nexus_core/market_analysis/dynamic_rollover/constants.py:14` |
| `_EUPHORIA_SKEW_PERCENTILE` | `20.0` | 衛星持倉 Skew 倒掛亢奮門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py:11` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 CME 期貨休市與數據源異常阻斷（Sanity Gating）
- **假日與休市保護**：當交易所週末或國定假日休市時，`ZQ` 期貨報價為空，系統自動 fallback 讀取次選 `ZQ=F`，若仍為空則呼叫 Atlanta Fed Excel 檔案（`mpt_histdata.xlsx`）進行歷史分佈解析；若均不可用，回傳預設中性 50% 基準值，並標記 `macro_fedwatch_is_fallback = 1`。
- **異常數值攔截（Sanity Gating）**：
  在 `services/calendar_service.py` 寫入 SQLite 快取前，強制執行合理性檢查：
  若滿足下列任一條件：
  1. $prob \ge 0.99$ 或 $prob \le 0.01$
  2. $(P_{\text{cut}} \ge 99.0 \lor P_{\text{hike}} \ge 99.0) \land \neg (\text{ladder\_saturated} \lor \text{"Atlanta Fed"} \in source)$

  系統判定為歷史污染或失真數值，立即觸發防禦阻斷，中止資料庫寫入並在快取記錄 `macro_fedwatch_is_fallback = 1`。

### 5.2 零除與邊界防護
- **除數保護**：在計算 $\Delta R$ 權重時，會議後天數嚴格約束為 $d_{post} = \max(1, N - d_{prior})$，杜絕月底最後一日開會導致除以零崩潰。
- **衛星持倉空集合**：當使用者投資組合中無任何 `SATELLITE` 資產時，`satellite_euphoria_ratio` 返回 `None`，五因子模型自動無縫退化為四因子模型，門檻常數保持一致。

### 5.3 減碼執行衝突隔離（Conflict Isolation）
- 在 `dynamic_rollover` 排程派發器中，情境 6 刻意排在最後順序（3 → 2 → 5 → 4 → 6）。
- 若某檔標的已在情境 2（機會成本轉倉）、情境 3（Call Wall 亢奮獲利鎖定）、情境 4（流動性危機停損）或情境 5（核心配置超額）被標記處理，情境 6 自動將該標的加入 `already_flagged_symbols` 跳過，嚴禁對同一標的下發相互矛盾的指令。

---

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/index_microstructure.py`
  - `evaluate_escape_window_regime`: 四因子宏觀流動性矩陣與窗口位移計算
  - `evaluate_macro_top_escape_score`: 五因子複合逃頂評分階梯
- `nexus_edge_scraper/local_api/macro.py`
  - `scrape_fedwatch`: CME 30 天期聯邦基金期貨（ZQ）反推及階梯定價演算法
- `nexus_core/services/calendar_service.py`
  - `update_economic_calendar`: CME FedWatch 定價寫入與數值合理性閘門（Sanity Gating）
- `nexus_core/market_analysis/dynamic_rollover/macro_top_escape_defense.py`
  - `evaluate_macro_top_escape_defense`: 情境 6 宏觀逃頂前瞻防禦與 25% BOXX 輪動邏輯
- `nexus_core/market_analysis/dynamic_rollover/constants.py`
  - 具名常數 `_MACRO_TOP_ESCAPE_TRIM_RATIO`, `_MACRO_TOP_ESCAPE_MIN_TIER`
