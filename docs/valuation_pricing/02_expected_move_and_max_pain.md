# 預期波幅 (Expected Move) 與多 DTE 最大痛點重力過濾體系 (Expected Move & Multi-DTE Max Pain Gravity Filter)

## 1. 核心哲學與適用市場環境

### 1.1 預期波幅 (Expected Move, EM) 的微觀結構本質
預期波幅（Expected Move, EM）代表期權市場造市商在無套利定價（No-Arbitrage Pricing）原則下，對標的資產在指定期限內隱含價格波動空間的群體共識。

傳統技術分析習慣依賴歷史波動率（HV）或固定標準差通道（如布林帶），但這些指標均屬於**滯後性指標**，無法反映即時事件風險（如財報公布、CPI 數據發布、FOMO 資金湧入）。在現代期權市場中，**平價跨式期權（ATM Straddle）的權利金總和直接量化了造市商承擔 Gamma 與 Vega 雙向對沖風險所需的最低保險費**。因此，由 ATM 跨式期權推導出的預期波幅，精確定義了標的資產約 68.2% 概率（1-Sigma）的分佈邊界，是市場公認的「非線性流動性邊界」。

### 1.2 最大痛點 (Max Pain, MP) 重力引力哲學
最大痛點理論指出：**在期權到期結算日，標的資產現貨價格往往具有向「使所有未平倉期權買方總虧損最大、賣方與做市商總利潤最高」的履約價聚集的強烈趨勢**。

其微觀驅動機制並非玄學，而是源自做市商的動態 Delta 對沖行為：
1. 當價格大幅偏離最大痛點時，做市商手中累積了龐大的非對稱 Delta 曝險（如價內 Call 或 Put 的 Delta 逼近 $\pm 1.0$）。
2. 在接近到期日時，時間價值衰減加速（Theta 耗損爆發），若標的價格回歸痛點附近，多數期權將歸零失效（Worthless），做市商即可平倉現貨對沖部位並完整收取時間價值利潤。
3. 因此，最大痛點猶如一個動態重力中心（Gravity Well）。當價格出現極端偏離時，將觸發強烈的均值回歸磁吸效應。

### 1.3 多 DTE 重力過濾體系 (Multi-DTE Gravity Ladder)
單一到期日的最大痛點容易受到單一合約主力異動的干擾。Nexus Seeker 建立了涵蓋「末日到期（0–1 DTE）」與「次週/遠期到期（2–14 DTE）」的雙軌重力過濾體系：
- **0–1 DTE 末日磁吸**：末日合約 Gamma 爆發，微小的價格波動即引發做市商巨額 Delta 調倉，極端偏離時磁吸力道最強。
- **2–14 DTE 次週痛點壓制**：次週痛點反映中期籌碼壁壘。若現價大幅高於次週痛點，代表上方未平倉 Call 牆沉重，上方空間嚴重受限，將觸發下行磁吸預警。

---

## 2. 數學模型與量化推導

### 2.1 雙軌融合預期波幅計算模型

#### 1. ATM 跨式期權隱含波幅 (Straddle-Implied EM)
抓取距離當前最接近且尚未到期的期權合約（$\text{Target DTE} \in [0, 14]$），鎖定最接近現價 $S$ 之 ATM Call 與 Put：
$$\text{Mid}_{\text{Call}} = \frac{\text{Bid}_C + \text{Ask}_C}{2}, \quad \text{Mid}_{\text{Put}} = \frac{\text{Bid}_P + \text{Ask}_P}{2}$$
$$\text{Straddle Price} = \text{Mid}_{\text{Call}} + \text{Mid}_{\text{Put}}$$

依據業界標準 0.85 經驗因子（對應常態分佈 1-Sigma 積分比率），並依據實際 DTE 平移至每週 7 天基準：
$$\text{EM}_{\text{straddle}} = (\text{Straddle Price} \times 0.85) \times \sqrt{\frac{7.0}{\max(7.0, \text{Target DTE})}}$$

**關鍵物理約束**：分母採用 $\max(7.0, \text{Target DTE})$，防止在 DTE < 7（例如 0–1 DTE 或包含財報等事件性跳躍）時，被平方根除法錯誤放大（Event-Jump Extrapolation Failure）。不足的天數方差後續透過 BSM 30 天期 IV 自動補足。

#### 2. BSM 波動率預期波幅 (IV-Derived EM)
根據 Black-Scholes-Merton 模型的擴散假設，7 天期週波動幅度公式為：
$$\text{EM}_{\text{iv}} = S \times \sigma_{\text{IV}} \times \sqrt{\frac{7.0}{365.0}}$$

#### 3. 動態取大與三階保底融合機制
綜合跨式期權與全鏈 IV，取兩者之最大值作為最終每週預期波幅：
$$\text{EM}_{\text{weekly}} = \begin{cases} \max(\text{EM}_{\text{straddle}}, \text{EM}_{\text{iv}}), & \text{若兩者皆有效} \\ \text{EM}_{\text{straddle}}, & \text{若僅 Straddle 有效} \\ \text{EM}_{\text{iv}}, & \text{若僅 IV 有效} \\ S \times \max(\text{HV}_{20}, 0.15) \times \sqrt{\frac{7.0}{365.0}}, & \text{若均失效 (降級至 20日歷史波動率)} \\ S \times 0.15 \times \sqrt{\frac{7.0}{365.0}}, & \text{終極保底 (15% 年化波動率底線)} \end{cases}$$

#### 4. EM 邊界軌道與標準化 Z-Score
以參考基準價 $S_{\text{ref}}$ 建立對稱軌道：
$$\text{EM}_{\text{upper}} = S_{\text{ref}} + \text{EM}_{\text{weekly}}, \quad \text{EM}_{\text{lower}} = S_{\text{ref}} - \text{EM}_{\text{weekly}}$$

現價在預期波幅軌道中的相對標準化位置（EM Z-Score）推導如下：
$$\text{Pos}_{\%} = \frac{S - \text{EM}_{\text{lower}}}{\text{EM}_{\text{upper}} - \text{EM}_{\text{lower}}} \times 100\%$$
$$\text{EM Z-Score} = \frac{\text{Pos}_{\%} - 50.0}{50.0} = \frac{S - S_{\text{ref}}}{\text{EM}_{\text{weekly}}}$$
- 當 $\text{Z-Score} > +0.90\sigma$：現價逼近 EM 上軌，多頭動能耗盡，需防洗盤壓回。
- 當 $\text{Z-Score} < -0.90\sigma$：現價逼近 EM 下軌，做市商下行超跌，容易觸發磁吸反彈。

### 2.2 最大痛點 (Max Pain) 損益最佳化數學推導
設有效履約價集合為 $\mathcal{K} = \{K_1, K_2, \dots, K_M\}$，且滿足物理邊界條件：
$$\mathcal{K}_{\text{valid}} = \left\{ K \in \mathcal{K} \;\middle|\; 0.25 \times S_{\text{spot}} \le K \le 4.0 \times S_{\text{spot}} \right\}$$

對於假定的到期結算價 $S \in \mathcal{K}_{\text{valid}}$，全市場買方的總內含價值損失函數 $\text{Pain}(S)$ 定義為：
$$\text{Pain}(S) = \sum_{K_i < S} W_{\text{Call}}(K_i) \cdot (S - K_i) + \sum_{K_j > S} W_{\text{Put}}(K_j) \cdot (K_j - S)$$

其中權重 $W(K)$ 優先採用未平倉合約數 $\text{OI}(K)$，盤中資料缺失時平滑降級為當日成交量 $\text{Volume}(K)$。
最大痛點價位即為使該損失函數達到全域最小值的履約價：
$$\text{Max Pain} = \arg\min_{S \in \mathcal{K}_{\text{valid}}} \text{Pain}(S)$$

### 2.3 多 DTE 重力過濾階梯規則
計算現價 $S$ 相對於各到期日最大痛點的偏離百分比：
$$\text{dev} = \frac{S - \text{MP}}{\text{MP}} \times 100\%$$

系統建立雙重判定階梯：
1. **末日痛點階梯 ($0 \le \text{DTE} \le 1$)**：
   $$|\text{dev}| > 8.0\% \implies \text{觸發【做市商末日對沖磁吸力】}$$
   - 若 $\text{dev} > 0$（現價顯著高於痛點），判定強烈下行引力：`mp_gravity_strong_down = True`。
   - 若 $\text{dev} < 0$（現價顯著低於痛點），判定強烈向上修復引力。
2. **次週/遠期痛點階梯 ($1 < \text{DTE} \le 14$)**：
   $$\text{dev} > +10.0\% \implies \text{觸發【下行磁吸預警 ⚠️】，上方空間受限，標記 } \text{mp_gravity_strong_down} = \text{True}$$

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([請求標的 EM 與 Max Pain]) --> CacheCheck{SQLite 快取是否存在<br/>且年齡 < 6小時?}

    CacheCheck -- 是 --> DriftCheck{現價偏離基準價 > 2%<br/>且冷卻時間 > 30秒?}
    DriftCheck -- 否 (命中快取) --> ReturnCache[直接返回快取數值]
    DriftCheck -- 是 (偏離過大) --> Recompute[觸發 Cache-Aside 即時重算]
    CacheCheck -- 否 (快取缺失) --> Recompute

    Recompute --> FetchChain[拉取即時期權鏈與 IV]
    FetchChain --> CalcEM[計算 ATM Straddle EM<br/>與 BSM IV EM 取大]
    FetchChain --> CalcMP[遍歷履約價求解 Pain 最小值]

    CalcMP --> CBCheck{|MaxPain - Spot| / Spot > 30%?}
    CBCheck -- 是 --> TriggerCB[🚨 觸發 30% 異常斷路器<br/>max_pain=None, 背景清快取]
    CBCheck -- 否 --> MultiDTE[遍歷月度合約計算各 DTE 痛點]

    MultiDTE --> GravityLadder{DTE 階梯評估}
    GravityLadder -->|0 <= DTE <= 1 且 |dev| > 8%| NearGravity[觸發: 末日對沖磁吸力 ⚠️<br/>dev>0 設 mp_gravity_strong_down=True]
    GravityLadder -->|1 < DTE <= 14 且 dev > +10%| FarGravity[觸發: 下行磁吸預警 ⚠️<br/>設 mp_gravity_strong_down=True]
    GravityLadder -->|正常範圍| NormalState[正常運行 / 磁吸回升]

    NormalState --> SaveCache[非同步寫回 SQLite market_cache]
    NearGravity --> SaveCache
    FarGravity --> SaveCache
    TriggerCB --> ReturnDegraded[返回降級 EM 數據]
    SaveCache --> Complete([輸出量化雷達欄位])
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 門檻 | 數值 / 設定 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `_MARKET_CACHE_MAX_AGE_SECONDS` | `21600` (6 小時) | SQLite `market_cache` 絕對過期淘汰時間 | `nexus_core/market_analysis/sentiment/max_pain.py` |
| `spot_deviation_threshold` | `0.02` ($2.0\%$) | 現價相對於快取基準價偏離超過 2% 強制觸發重算 | `nexus_core/market_analysis/sentiment/max_pain.py` |
| `cooldown_seconds` | `30.0` 秒 | 短時間內防止高頻重複計算的冷卻窗口 | `nexus_core/market_analysis/sentiment/max_pain.py` |
| `circuit_breaker_threshold` | `0.30` ($30.0\%$) | 痛點與現價偏離逾 30% 判定數據污染，啟動自癒斷路 | `nexus_core/market_analysis/sentiment/max_pain.py` |
| `max_expiry_days` | `30` 天 | 痛點與波幅計算嚴格限制在 30 天內合約，逾期阻斷 | `nexus_core/market_analysis/sentiment/max_pain.py` |
| `straddle_sigma_factor` | `0.85` | ATM Straddle 轉化為 1-Sigma 預期波幅的標準常數 | `nexus_core/market_analysis/sentiment/iv_metrics.py` |
| `weekly_days_baseline` | `7.0` 天 | 預期波幅時間標準化週基準天數 | `nexus_core/market_analysis/sentiment/iv_metrics.py` |
| `near_dte_dev_limit` | `8.0%` | 0–1 DTE 末日合約偏離觸發磁吸的百分比門檻 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `far_dte_dev_limit` | `10.0%` | 2–14 DTE 次週合約正偏離觸發下行壓制的百分比門檻 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `min_volatility_floor` | `0.15` ($15.0\%$) | 所有預期波幅計算降級失敗時的年化波動率絕對底線 | `nexus_core/market_analysis/sentiment/iv_metrics.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 30% 異常偏離斷路器與快取自癒 (Circuit Breaker)
在極端行情或期權流動性突發枯竭（如合約報價出現錯單或價外合約異常成交）時，數值計算可能得出偏離現價數倍的荒謬痛點。`max_pain.py:270` 設立 30% 風控斷路器：
```python
if spot_price > 0 and abs(max_pain - spot_price) / spot_price > 0.30:
    logger.warning(f"[{symbol}] Max pain {max_pain} deviates >30% from spot {spot_price}")
    max_pain = None
    circuit_breaker_triggered = 1
```
一旦觸發，系統將 `max_pain` 設為 `None`，向資料庫寫入 `circuit_breaker_triggered = 1`，並發起非同步背景任務清除受污染的本地快取，防止錯誤數據持久化。

### 5.2 0-DTE 事件跳躍平方根除法失真防護 (Event-Jump Extrapolation Guard)
在計算 ATM Straddle EM 時，若合約當日即將到期（$\text{DTE} \to 0$），若機械式套用 $\sqrt{7 / \text{DTE}}$，分母趨近於零將導致波幅被成倍放大至天文數字。因此在 `iv_metrics.py:197` 中強制採用：
$$\sqrt{\frac{7.0}{\max(7.0, \text{Target DTE})}}$$
小於 7 天的合約僅保留當前跨式期權實質定價，絕不進行外推膨脹。

### 5.3 買賣價差過大與零成交量防護 (Wide Bid-Ask Spread Guard)
若 ATM 期權無有效成交價且未提供 Bid/Ask 報價（`call_mid <= 0` 且 `put_mid <= 0`），系統自動跳過 Straddle 計算，平滑切換至 BSM 公式與歷史波動率降級管線，確保不拋出未捕捉異常。

---

## 6. 核心程式碼檔案路徑關聯

- **預期波幅雙軌計算引擎**:
  - `nexus_core/market_analysis/sentiment/iv_metrics.py`: `_calculate_straddle_implied_em()`, `fetch_and_calculate_iv_metrics()`
- **最大痛點最佳化與快取斷路器**:
  - `nexus_core/market_analysis/sentiment/max_pain.py`: `_calculate_max_pain_with_weights()`, `get_unified_max_pain()`, `_calculate_max_pain_raw()`
- **多 DTE 重力過濾與終端雷達狀態整合**:
  - `nexus_core/cogs/embed_builders/market_embeds.py`: lines 892–929 (多週期引力階梯), lines 1048–1055 (EM Z-Score 渲染)
- **本地 SQLite 預熱快取資料表定義**:
  - `nexus_core/database/schema.py`: `market_cache` 資料表結構
