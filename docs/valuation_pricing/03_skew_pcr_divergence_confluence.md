# Skew 偏斜與 Volume PCR 瀑布流背離及三重結構性風險合流閘門 (Skew/PCR Divergence & Triple Structural Risk Confluence)

## 1. 核心哲學與適用市場環境

### 1.1 期權 Skew 偏斜與尾部風險定價哲學
期權偏斜度（Volatility Skew）是造市商與機構對沖基金對市場下行黑天鵝風險定價的最敏感晴雨表。在 Black-Scholes-Merton 模型的理想幾何布朗運動假設下，相同標的與到期日的不同履約價隱含波動率（IV）應當相等（水平直線）。然而在真實美股市場中，由於市場參與者對暴跌的恐慌遠高於對暴漲的渴求，虛值賣權（OTM Put）的隱含波動率長期顯著高於虛值買權（OTM Call），形成所謂的「波動率微笑」或「偏斜（Skew）」。

當市場面臨潛在的流動性危機、宏觀事件衝擊或做市商預見內部籌碼破裂時，機構投資人會不計代價搶購 25-Delta 甚至 10-Delta 的深度價外 Put 避險，導致 Skew 分位數（Skew Percentile）急劇飆升至歷史極端（例如 $\ge 98\%$）。

### 1.2 Volume PCR 破位順向殺盤 (Waterfall Divergence)
成交量買賣權比率（Volume Put-Call Ratio, Volume PCR）反映了盤中即時流動性訂單流的極性傾斜。
當標的現貨價格跌破**做市商正 Gamma 底牆（Put Wall）**，且盤中 **$\text{Volume PCR} \ge 1.2$** 時，觸發系統最高危險等級的「破位順向殺盤」。

其微觀物理傳導機制如下：
1. **底牆崩塌**：現貨跌破 Put Wall，意味著市場失去了做市商最大的多頭正 Gamma 緩衝墊，瞬間跌入負 Gamma 踩踏泥淖。
2. **對沖反噬**：大量湧入的看跌期權（$\text{PCR} \ge 1.2$）迫使做市商持有巨額做空 Delta（$-\Delta$）。為了維持 Delta 中性，做市商必須在現貨與期貨市場進行「順向砸盤對沖」（跌越深越要拋售）。
3. **流動性真空暴跌**：做市商的主動砸盤與多頭追繳保證金爆倉合流，引發垂直下墜的瀑布流。此時若盲目進行基本面「價值抄底」或左側「技術接刀」，將遭遇不可逆的毀滅性本金虧損。

### 1.3 三重結構性風險合流 (Triple Structural Risk Confluence)
單一指標往往存在噪音，例如：
- Skew 極高有時僅是特定機構的事件前短暫過度對沖；
- Max Pain 向下引力有時會被強大現貨動能暫時突破；
- 缺乏機構大單有時僅是例行交投清淡。

然而，當 **「極端避險背離（$\text{Skew} \ge 98.0\%$）」**、**「痛點向下重力引力（$\text{mp\_gravity\_strong\_down}$）」** 與 **「機構買盤徹底真空（完全無 $\text{DTE} \ge 7$ 之多頭大單）」** 同時共振合流時，代表：
1. 造市商預判極端尾部下行；
2. 結算日重力中心死死拖拽價格向下；
3. 具有一週以上持倉視野的真正機構主力完全不願在現價進場承接。

這構成了美股微觀結構中的「完美風暴」。系統在此刻啟動一票否決制，強制宣告 `🚨 三重結構性風險合流：避險背離+痛點引力+機構真空，嚴禁抄底`。

---

## 2. 數學模型與量化推導

### 2.1 Skew 偏斜度與歷史百分位推導
定義標的合約 25-Delta Put 與 25-Delta Call 的隱含波動率之差為該期限的即時偏斜值：
$$\text{Skew} = \sigma_{\text{IV}}(25\Delta \text{ Put}) - \sigma_{\text{IV}}(25\Delta \text{ Call})$$

採集標的資產在過去 252 個交易日（1 年）的歷史偏斜觀測樣本集 $\mathcal{S}_{252} = \{\text{Skew}_1, \text{Skew}_2, \dots, \text{Skew}_{252}\}$。當前偏斜度相對於歷史分佈的百分位數（Skew Percentile）計算如下：
$$\text{Skew Percentile} = \left( \frac{1}{252} \sum_{t=1}^{252} \mathbf{1}_{\{\text{Skew}_t < \text{Skew}_{\text{curr}}\}} \right) \times 100\%$$

- $\text{Skew Percentile} > 85.0\%$：市場進入高戒備避險區間；
- $\text{Skew Percentile} \ge 98.0\%$：市場進入全域極端尾部對沖狀態。

### 2.2 Volume PCR 與破位順向殺盤定理
設 $V_{\text{Put}}(K_i)$ 與 $V_{\text{Call}}(K_j)$ 分別為全鏈所有賣權與買權的盤中即時累積成交量：
$$\text{Volume PCR} = \frac{\sum_{i} V_{\text{Put}}(K_i)}{\sum_{j} V_{\text{Call}}(K_j)}$$

設 $\text{Put Wall}$ 為做市商累積最大正 GEX 之底牆履約價。當滿足以下聯立物理條件時，宣告觸發破位順向殺盤：
$$\text{Waterfall Condition} \iff \begin{cases} \text{Put Wall} > 0 \\ P_{\text{spot}} < \text{Put Wall} \\ \text{Volume PCR} \ge 1.20 \end{cases}$$

**處置邏輯**：
$$\text{Action Override} = \text{STOP\_ALL\_BUY}$$
全面凍結系統內所有多頭進場建議（包含現貨買入 SPOT_BUY、價內看漲買權 BTO_CALL 以及賣出賣權 CSP），直到價格有效重返 Put Wall 之上。

### 2.3 結構性情緒背離判定 (Structural Sentiment Divergence)
當做市商的尾部定價與散戶的即時成交量極性出現劇烈衝突時，代表市場內部籌碼發生嚴重撕裂：
$$\text{Structural Divergence} \iff \begin{cases} (\text{Skew Percentile} > 85.0 \land 0.0 < \text{Volume PCR} < 0.40) & \text{[散戶盲目追多，機構瘋狂買 Put 避險]} \\ \lor \\ (\text{Skew Percentile} < 15.0 \land \text{Volume PCR} > 1.50) & \text{[散戶恐慌踩踏，做市商 Put 溢價極低]} \end{cases}$$

輸出風控警告：
`[⚠️ 警告：結構性情緒背離] Skew 分位極端且 PCR 指向相反極端，市場內部籌碼嚴重分歧，指標失真度極高；建議暫停單方向開倉。`

### 2.4 微觀結構背離閘道 (Micro-Divergence Gate)
在動能交易體系中，PSQ (Pro Squeeze) 動能柱向上翻綠（$\text{SQZ Momentum} > 0$）通常被視為多頭突破信號。但若此時做市商避險情緒高漲（$\text{Skew Percentile} > 85.0\%$），該突破往往是做市商掩護出貨或假突破流動性獵殺：
$$\text{Micro Divergence} \iff (\text{SQZ Momentum} > 0) \land (\text{Skew Percentile} > 85.0)$$
輸出結果：
$$\text{Status Label} = \text{🚫 偽突破 (嚴禁單腿看多)}$$

### 2.5 三重結構性風險合流複合邏輯 (Triple Structural Risk Confluence)
定義三個正交的布林微觀結構命題：
1. **命題一：極端避險背離 ($C_{\text{skew}}$)**
   $$C_{\text{skew}} \iff \text{Skew Percentile} \ge 98.0$$
2. **命題二：多 DTE 痛點強下行引力 ($C_{\text{gravity}}$)**
   $$C_{\text{gravity}} \iff \text{mp\_gravity\_strong\_down} == \text{True}$$
   即存在 $0 \le \text{DTE} \le 1$ 且 $\text{dev} > +8.0\%$，或存在 $1 < \text{DTE} \le 14$ 且 $\text{dev} > +10.0\%$。
3. **命題三：主力買盤真空 ($C_{\text{vacuum}}$)**
   在過去一個交易日內檢測到的所有 UOA 異常訂單集合 $\mathcal{U}$ 中，不存在任何期限大於等於一週的實質多頭主力建倉單：
   $$C_{\text{vacuum}} \iff \neg \exists u \in \mathcal{U} \;\text{s.t.}\; \left( \text{DTE}(u) \ge 7 \;\land\; \big((\text{Action} = \text{BTO} \land \text{Type} = \text{CALL}) \lor (\text{Action} = \text{STO} \land \text{Type} = \text{PUT})\big) \right)$$

**合流判定公式**：
$$\text{Triple Confluence} = C_{\text{skew}} \land C_{\text{gravity}} \land C_{\text{vacuum}}$$

當 $\text{Triple Confluence} == \text{True}$ 時，觸發最高優先級覆寫：
$$\text{Status Label} \leftarrow \text{"三重結構性風險合流 🚨"}$$
$$\text{Tactical Advice} \leftarrow \text{"🚨 三重結構性風險合流：避險背離+痛點引力+機構真空，嚴禁抄底"}$$

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([盤中即時掃描標的期權結構]) --> PutWallCheck{現價 < PutWall 做市商底牆?}

    PutWallCheck -- 是 --> PCRCheck{Volume PCR >= 1.20?}
    PCRCheck -- 是 --> TriggerWaterfall["🚨 破位順向殺盤<br/>做市商負 Gamma 追殺<br/>指令: STOP_ALL_BUY"]
    PCRCheck -- 否 --> CheckConfluence

    PutWallCheck -- 否 --> CheckConfluence

    subgraph 三重結構性風險合流評估
        CheckConfluence --> Gate1{條件 1: Skew Percentile >= 98.0%?}
        Gate1 -- 否 --> NormalSkewCheck
        Gate1 -- 是 --> Gate2{"條件 2: mp_gravity_strong_down 是否成立?<br/>末日偏離>8% 或 次週偏離>10%"}
        Gate2 -- 否 --> NormalSkewCheck
        Gate2 -- 是 --> Gate3{"條件 3: 是否完全不存在<br/>DTE >= 7 的 BTO Call 或 STO Put?"}
        Gate3 -- 是 (機構真空) --> TriggerTriple["🚨 三重結構性風險合流<br/>避險背離+痛點引力+機構真空<br/>輸出: 嚴禁抄底"]
        Gate3 -- 否 (有主力買盤) --> NormalSkewCheck
    end

    NormalSkewCheck --> HighSkewCheck{Skew Percentile > 90.0%?}
    HighSkewCheck -- 是 --> SkewStop["🛑 防洗盤處置<br/>嚴守 15分鐘實體K線撤退線"]
    HighSkewCheck -- 否 --> DivergenceCheck{"Skew > 85% 且 PCR < 0.40?<br/>或 Skew < 15% 且 PCR > 1.50?"}

    DivergenceCheck -- 是 --> StructDiv["⚠️ 警告：結構性情緒背離<br/>市場籌碼嚴重撕裂，暫停開倉"]
    DivergenceCheck -- 否 --> FakeoutCheck{"SQZ Momentum > 0<br/>且 Skew Percentile > 85%?"}

    FakeoutCheck -- 是 --> FakeoutAlert["🚫 偽突破 嚴禁單腿看多<br/>散戶追高但做市商買 Put 避險"]
    FakeoutCheck -- 否 --> NormalTrade[進入一般期權策略匹配流程]
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 門檻 | 數值 / 設定 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `vol_pcr_waterfall_threshold` | $\ge 1.20$ | 觸發破位順向殺盤的買賣權成交量比率門檻 | `nexus_core/market_analysis/insights_engine.py` |
| `skew_triple_confluence_pctl` | $\ge 98.0\%$ | 三重合流極端避險背離判定基準分位數 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `skew_high_defense_pctl` | $> 90.0\%$ | 一般防洗盤處置與嚴守 15 分鐘撤退線門檻 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `micro_divergence_skew_pctl` | $> 85.0\%$ | 偽突破微觀結構背離閘道門檻 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `pcr_fomo_lower_threshold` | $< 0.40$ | 散戶極度瘋狂追多門檻（結構背離比對用） | `nexus_core/market_analysis/intraday_pipeline/skew_commentary.py` |
| `pcr_panic_upper_threshold` | $> 1.50$ | 散戶非理性恐慌殺跌門檻（結構背離比對用） | `nexus_core/market_analysis/intraday_pipeline/skew_commentary.py` |
| `uoa_institutional_min_dte` | $\ge 7$ 天 | 判定實質機構買盤護航的最小到期日要求 | `nexus_core/cogs/embed_builders/market_embeds.py` |
| `uoa_aligned_actions` | `["BTO CALL", "STO PUT"]` | 視為實質看多/托底的異常期權交易動作定義 | `nexus_core/cogs/trading/heartbeat.py` |
| `_MIN_PERCENTILE_SAMPLES` | `20` 筆 | 樣本數低於此值時百分位直接回傳 `None`，而非用不足樣本假造極端值 | `nexus_core/market_analysis/sentiment/history_storage.py` |
| `SKEW_D25` | 歷史指標命名空間 | 真 25-Delta Skew 的專屬 history key，與改版前的 `SKEW`（±5% 價平代理）樣本互不混用 | `nexus_core/market_analysis/sentiment/skew_taxonomy.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 判定層級優先序約束 (Evaluation Precedence)
在代碼實作中，`Triple Structural Risk Confluence` 的條件集合在邏輯上是 `skew_percentile > 90.0` 的嚴例子集。若代碼先執行：
```python
elif skew_percentile_val > 90.0:
    tactical_adv = "🛑 防洗盤處置，嚴守 15 分鐘實體 K 線撤退線"
```
則所有大於 98% 的三重合流情況將被該分支攔截，造成致命的**分流死碼（Dead Branch）**。因此在 `market_embeds.py:1263` 中，必須嚴格將三重合流判定置於 `> 90.0` 之前：
```python
elif (
    skew_percentile_val >= 98.0
    and mp_gravity_strong_down
    and not has_dte7_institutional_buy_support
):
    status_label = "三重結構性風險合流 🚨"
    tactical_adv = "🚨 三重結構性風險合流：避險背離+痛點引力+機構真空，嚴禁抄底"
```

### 5.2 UOA 機構買盤真空判定之邊界防護 (UOA Vacuum Guard)
若標的在全市場期權鏈中本日完全無任何 UOA 異動（`uoa_list` 為空列表），則遍歷迴圈將正常結束，`has_dte7_institutional_buy_support` 自然保持預設的 `False`。這符合量化物理直覺：完全沒有主力大單介入，就是最徹底的機構真空狀態。

### 5.3 單邊流動性與零成交量防護 (Zero Division in PCR)
若當日買權成交量為零（$\sum V_{\text{Call}} = 0$），在計算 `Volume PCR` 時可能引發除以零錯誤。代碼在 `insights_engine.py` 與 `market_embeds.py` 中均對 `vol_pcr` 進行空值與安全轉換（`_safe_float`），當成交量為零時安全賦值為 `None`，不觸發破位順向殺盤。

### 5.4 Skew 百分位的真實統計視窗與樣本邊界防護 (Real Sample Window & Edge Guards)
§2.1 的 $\mathcal{S}_{252}$ 是理想化的年度樣本描述；`get_indicator_percentile()`（`nexus_core/market_analysis/sentiment/history_storage.py`）實際採 `SELECT ... ORDER BY timestamp DESC LIMIT 100`，而 `calculate_skew()` 的呼叫點遍布心跳與批次掃描，盤中每小時約累積 4-8 筆樣本——換算下來 **100 筆樣本約僅涵蓋 2-4 個交易日**，而非 252 個交易日的年度分佈。這代表所有鍵在此百分位上的閘門（三重合流的 $\ge 98.0\%$、$> 90.0\%$ 防洗盤、§2.3 的 85/15 背離門檻）本質上是「近幾日相對排名」而非真正的年度尾部分位；此視窗設計是刻意維持不變的（切換為固定時間窗會同時改變多個已上線閘門的觸發頻率，須獨立評估），本節僅記錄其邊界防護：
- **最小樣本數防呆**：樣本數低於 `_MIN_PERCENTILE_SAMPLES = 20` 時回傳 `None`，而非讓單一筆歷史紀錄假造出 `0.0%` 或 `100.0%` 的極端偽訊號。
- **中位排序處理平局 (Midrank for Ties)**：採 $(count_{<} + 0.5 \times count_{=}) / n$ 計算，避免資料源卡死、樣本全相同時被誤判為 `0.0%`（歷史極端低點）。
- **`None` 而非虛假中性值**：歷史紀錄為空或查詢異常時回傳 `None`（呼叫端各自 fail-safe 為中性 `50.0` 或跳過極端分支），取代先前會落入 `skew_commentary` 30–70 常態抑制區間的硬編碼 `50.0` 預設值。
- **`SKEW_D25` 命名空間隔離**：25-Delta 改版後的數值寫入獨立的 `SKEW_D25` history key，與改版前 ±5% 價平代理寫入的舊 `SKEW` 樣本永不混用比較。

---

## 6. 核心程式碼檔案路徑關聯

- **Volume PCR 破位順向殺盤引擎**:
  - `nexus_core/market_analysis/insights_engine.py`: `generate_terminal_insights()` (lines 58–70)
- **Skew 偏斜與結構性情緒背離規則**:
  - `nexus_core/market_analysis/intraday_pipeline/skew_commentary.py`: `build_watchlist_skew_rule_commentary()` (lines 14–182)
- **三重結構性風險合流與微觀背離閘道**:
  - `nexus_core/cogs/embed_builders/market_embeds.py`: lines 930–940 (微觀背離), lines 1100–1122 (DTE$\ge$7 機構買盤審計), lines 1263–1274 (三重合流處置)
- **機構多頭對齊定義**:
  - `nexus_core/cogs/trading/heartbeat.py`: `is_uoa_aligned` 判定邏輯
