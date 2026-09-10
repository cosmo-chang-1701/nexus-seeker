# TDP 估值三擊與 DDP 雙重折價定價模型 (Triple Discount Pricing & Davis Double Play)

## 1. 核心哲學與適用市場環境

### 1.1 戴維斯雙擊 (Davis Double Play) 基本面哲學
戴維斯雙擊（Davis Double Play, DDP）是由著名投資人謝爾比·戴維斯（Shelby Davis）提出的經典長線價值增長模型。其核心投資哲學在於：**企業每股盈餘（EPS）的實質增長，將驅動市場對該企業進行價值重估，進而賦予更高的本益比（P/E Multiple Expansion），最終推動股價實現幾何級數的乘數非線性暴漲**。

反之，若在市場狂熱給予極高本益比時盲目追價，即使企業未來 EPS 維持增長，一旦成長率放緩遭遇本益比壓縮（Multiple Contraction），亦將面臨慘烈的「戴維斯雙殺」。因此，DDP 檢驗引擎嚴格要求標的必須處於「成長加速但估值極度壓抑」的低位拐點。

### 1.2 TDP (Triple Discount Pricing) 籌碼與技術多重折價共振
在現代美股微觀結構中，僅憑基本面數據無法保證短期買入時機的安全邊際。做市商流動性獵殺、暗池對倒吸籌以及期權造市商的 Gamma 泥淖，經常使具備基本面優勢的個股在發動前經歷漫長的沉澱甚至深幅洗盤。

Nexus Seeker 的 **TDP (Triple Discount Pricing) 估值三擊模型**，將基本面安全邊際延伸至做市商微觀結構與期權籌碼面。當個股現價同時折價於**技術面均線（EMA 21 / MA 20）**、**期權最大痛點（Max Pain）**、**公開市場成交量密集核心（Volume-POC）** 以及 **機構暗池大單籌碼峰（DP-POC）** 時，形成四重折價共振。此時下行空間被多重結構性籌碼底部封死，構成極高勝率的左側均值回歸與右側反轉支撐。

### 1.3 TDPQ 波動率擠壓共振升級
當標的同時通過 DDP 基本面過濾與 TDP 四重折價確認，且其微觀波動率結構正處於 PSQ (Pro Squeeze Momentum) 擠壓狀態（布林通道被完全包裹於肯特納通道內，能量極度蓄積）時，系統將其標記升級為 **`⚡ TDPQ 突破共振 (Triple Discount + Squeeze)`**。這代表「基本面爆發力 + 超跌結構支撐 + 波動率即將方向性釋放」的三位一體最強進攻型態。

### 1.4 適用市場環境與產業邊界
- **最佳適用場景**：大盤處於牛市回檔期、市場震盪整理（Low Volatility / Consolidation）、或優質成長股受短期情緒衝擊導致估值非理性挫跌之時。
- **嚴格排除產業**：強週期性產業（能源 Energy、基礎材料 Basic Materials）。週期性產業在景氣頂峰時往往呈現 EPS 虛高、歷史本益比極低的假象，若機械式套用 DDP 容易陷入毀滅性的「週期頂峰價值陷阱」。

---

## 2. 數學模型與量化推導

### 2.1 戴維斯雙擊分解定理
股票價格 $P$ 可以嚴格分解為每股盈餘 $\text{EPS}$ 與本益比倍數 $\frac{P}{E}$ 之乘積：
$$P = \text{EPS} \times \left(\frac{P}{E}\right)$$

對時間 $t$ 進行全微分與對數微分展開，可得資產回報率的非線性驅動結構：
$$\frac{dP}{P} = \frac{d\text{EPS}}{\text{EPS}} + \frac{d(P/E)}{P/E} + \left(\frac{d\text{EPS}}{\text{EPS}} \times \frac{d(P/E)}{P/E}\right)$$

其中第三項即為 **戴維斯雙擊交互乘數效應**（Cross-Product Leveraged Expansion）。當基本面與估值倍數同步擴張時，整體回報顯著超越單純利潤增長的線性幅度。

### 2.2 基本面審計五大維度量化公式

#### 1. EPS 成長動能 (EPS Momentum YoY)
選取最近季度每股稀釋盈餘 $\text{EPS}_t$ 與去年同期每股盈餘 $\text{EPS}_{t-4}$：
$$g_{\text{EPS}} = \frac{\text{EPS}_t - \text{EPS}_{t-4}}{\text{EPS}_{t-4}}$$
系統強制門檻條件為：
$$g_{\text{EPS}} \ge 15.0\% \quad \text{且} \quad \text{EPS}_{t-4} > 0$$
若去年同期 $\text{EPS}_{t-4} \le 0$（基期為負或零），直接熔斷阻斷，防範基期扭曲引發的偽高成長。

#### 2. 營收加速 (Revenue Acceleration)
營收年增率計算：
$$g_{\text{rev}, t} = \frac{\text{Rev}_t - \text{Rev}_{t-4}}{\text{Rev}_{t-4}}, \quad g_{\text{rev}, t-1} = \frac{\text{Rev}_{t-1} - \text{Rev}_{t-5}}{\text{Rev}_{t-5}}$$
當歷史財報完整（資料長度 $\ge 6$ 季）時，要求當期年增率超越前一期年增率：
$$\text{Acceleration} = g_{\text{rev}, t} > g_{\text{rev}, t-1}$$
當資料不足 6 季但滿 5 季時，降級要求當期營收年增率維持雙位數增長：
$$\text{Acceleration} = g_{\text{rev}, t} > 10.0\%$$

#### 3. 本益比估值壓縮 (P/E Compression)
以 5 年平均本益比 $\text{PE}_{\text{5Y Avg}}$ 為基準錨點，要求當前滾動本益比 $\text{PE}_{\text{trailing}}$ 顯著折價至 25 百分位以下（約等於 80% 均值）：
$$\text{PE}_{\text{compressed}} = \text{PE}_{\text{trailing}} < 0.80 \times \text{PE}_{\text{5Y Avg}}$$
若市場無 5 年平均 P/E 資料，降級採樣過去 3 年（156 週）週 K 棒收盤價序列 $\mathcal{P}_{\text{3Y}}$：
$$P_{\text{current}} < \operatorname{Percentile}_{25}(\mathcal{P}_{\text{3Y}})$$

#### 4. 前瞻預期對齊 (Forward Alignment)
前瞻本益比 $\text{PE}_{\text{forward}}$ 必須低於當前滾動本益比，確認華爾街分析師共識預期未來盈利持續擴張：
$$\text{PE}_{\text{forward}} < \text{PE}_{\text{trailing}}$$
若缺乏前瞻本益比預估，系統自動穿透至最新季度現金流量表，要求其營運現金流必須實質為正：
$$\text{Operating Cash Flow} > 0$$

#### 5. 營運利潤率擴張加分 (Operating Margin Bonus)
計算當季與上一季之營運利潤率：
$$\text{Margin}_t = \frac{\text{Operating Income}_t}{\text{Rev}_t}, \quad \text{Margin}_{t-1} = \frac{\text{Operating Income}_{t-1}}{\text{Rev}_{t-1}}$$
$$\text{MarginBonus} = \begin{cases} 5, & \text{若 } \text{Margin}_t \ge \text{Margin}_{t-1} \\ 0, & \text{其他} \end{cases}$$

#### 6. 20 日相對成交量催化 (RVOL Catalyst)
計算當日成交量相對於過去 20 日均量的倍數：
$$\text{RVOL} = \frac{\text{Volume}_{\text{curr}}}{\frac{1}{20}\sum_{k=1}^{20} \text{Volume}_{t-k}}$$
$$\text{RVOLBonus} = \begin{cases} 5, & \text{若 } \text{RVOL} > 1.5 \\ 0, & \text{其他} \end{cases}$$

#### 7. 綜合置信度評分模型 (Confidence Score)
綜合基礎評分與各維度增益，輸出 0 至 100 區間的置信度分數：
$$\text{Score} = \min\Big(100.0, \; 60.0 + \min(20.0, (g_{\text{EPS}} - 0.15) \times 100) + 10 \cdot \mathbf{1}_{\text{rev\_accel}} + 10 \cdot \mathbf{1}_{\text{pe\_compressed}} + \text{MarginBonus} + \text{RVOLBonus}\Big)$$

### 2.3 TDP 四重折價共振量化定義
設 $P_{\text{spot}}$ 為標的最新市場成交價。TDP 折價體系評估四大核心籌碼水位：
1. **技術均線支撐**：$P_{\text{spot}} < \text{EMA}_{21}$（或日線 $\text{MA}_{20}$）
2. **期權做市商痛點**：$P_{\text{spot}} < \text{Max Pain}$
3. **公開訂單薄成交量密集核心**：$P_{\text{spot}} < \text{Volume-POC}$
4. **機構暗池大單籌碼峰**：$P_{\text{spot}} < \text{DP-POC}$

TDP 啟用布林值判定如下：
$$\text{is\_tdp} = (P_{\text{spot}} < \text{EMA}_{21}) \land (P_{\text{spot}} < \text{Max Pain}) \land (P_{\text{spot}} < \text{Volume-POC}) \land (P_{\text{spot}} < \text{DP-POC})$$

當關鍵指標全數缺失時，系統啟用防護阻斷，強制 `is_tdp = False`。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([啟動 DDP/TDP 估值審計]) --> SectorCheck{產業是否為能源或基礎材料?}
    SectorCheck -- 是 --> RejectSector[拒絕: 週期性產業排除]
    SectorCheck -- 否 --> FetchData[抓取季度損益表與市場合約數據]

    FetchData --> EPSCheck{EPS YoY >= 15% 且 基期 EPS > 0?}
    EPSCheck -- 否 --> FailDDP[基本面不符: EPS 動能不足]
    EPSCheck --  GridPane --> RevCheck{營收成長是否加速?}

    RevCheck -- 否 --> FailDDP
    RevCheck -- 是 --> PECheck{PE < 80% 5Y均值 或 週K 25%分位?}

    PECheck -- 否 --> FailDDP
    PECheck -- 是 --> FwdCheck{Forward PE < Trailing PE 或 OCF > 0?}

    FwdCheck -- 否 --> FailDDP
    FwdCheck -- 是 --> CalcScore[計算置信度評分 60-100]
    CalcScore --> MarkDDP[標記 is_ddp = True]

    MarkDDP --> TDPCheck{現價 < EMA21 且<br/>現價 < MaxPain 且<br/>現價 < VPOC 且<br/>現價 < DPPOC?}
    TDPCheck -- 否 --> NormalDDP[輸出: DDP 基本面低估標的]
    TDPCheck -- 是 --> MarkTDP[標記 [🔵 TDP 三擊] 籌碼四重折價]

    MarkTDP --> SQZCheck{PSQ is_squeezing 是否為 True?}
    SQZCheck -- 否 --> OutputTDP[輸出: TDP 估值三擊共振信號]
    SQZCheck -- 是 --> OutputTDPQ[升級: ⚡ TDPQ 突破共振<br/>基本面+籌碼折價+波動率壓縮]
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 變數 | 數值 / 門檻 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `eps_growth_threshold` | $\ge 15.0\%$ | 季度 EPS 同比增長硬門檻，基期 $\le 0$ 時自動拒絕 | `nexus_core/market_analysis/ddp_inspector.py` |
| `rev_accel_threshold` | $> 10.0\%$ | 當財報僅有 5 季資料時的營收同比增長備用底線 | `nexus_core/market_analysis/ddp_inspector.py` |
| `pe_compression_factor` | $0.80$ ($80\%$) | 當前 P/E 必須小於 5 年均值之 80%（25 分位） | `nexus_core/market_analysis/ddp_inspector.py` |
| `pe_upper_safety_cap` | $500.0$ | Trailing P/E 超過 500 倍視為極端泡沫直接剔除 | `nexus_core/market_analysis/ddp_inspector.py` |
| `rvol_bonus_threshold` | $> 1.50$ | 20 日相對量能超越 1.5 倍給予 5 分加分 | `nexus_core/market_analysis/ddp_inspector.py` |
| `confidence_base_score` | $60.0$ | 通過基礎四項審計的基準起步分數 | `nexus_core/market_analysis/ddp_inspector.py` |
| `require_tdp_signal` | `True` / `False` | 終端雷達掃描時是否強制要求四重折價同時成立 | `nexus_core/market_analysis/intraday_pipeline/skew_commentary.py` |
| `excluded_sectors` | `["Energy", "Basic Materials"]` | 嚴格排除之強週期性產業分類 | `nexus_core/market_analysis/ddp_inspector.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 除以零與負基期防護 (Zero Division Protection)
在計算 EPS 同比增長率時，若去年同期盈餘 $\text{EPS}_{t-4} \le 0$，傳統除法會產生數學錯誤或扭曲的超大正值（例如由虧損 -\$0.01 轉為獲利 +\$0.02，數學計算會出現 -300% 甚至負值除法錯誤）。代碼於 `ddp_inspector.py:84` 設置嚴格防護：
```python
if prev_y_eps_val <= 0:
    logger.info(f"[{symbol}] DDP Fail: Base EPS <= 0 ({prev_y_eps_val})")
    return None
```
此舉直接阻斷所有由虧轉盈但盈利基底尚不穩固的高投機標的。

### 5.2 估值極值與本益比缺失降級 (Valuation Degradation)
若企業無 5 年平均 P/E 資料（常見於掛牌 2 至 4 年的次新成長股），引擎不直接棄審，而是自動平滑降級至過去 3 年（156 週）週線收盤價分佈。若當前股價低於該 3 年週線之第 25 百分位，則認定估值壓縮成立，並自動以股價百分位估算出等效 P/E 供終端介面顯示。

### 5.3 TDP 缺失指標防誤判 (Incomplete Metrics Guard)
在盤中即時雷達掃描中，若因期權鏈流動性不足或行情源中斷導致 `ma20`、`max_pain` 與 `dp_poc` 均為 `None`，若機械式比對 `current_price < metric` 會導致條件被繞過而誤判為折價成立。`skew_commentary.py:336` 設立安全閥：
```python
if ma20 is None and max_pain is None and dp_poc is None:
    is_tdp = False
```
強制確保在關鍵微觀籌碼指標缺失時，不輸出虛假進場信號。

---

## 6. 核心程式碼檔案路徑關聯

- **DDP 基本面檢驗引擎**:
  - `nexus_core/market_analysis/ddp_inspector.py`: `DDPInspector.inspect_symbol()`, `DDPInspector.run_scan()`
- **TDP 籌碼折價與雷達過濾器**:
  - `nexus_core/market_analysis/intraday_pipeline/skew_commentary.py`: `build_watchlist_skew_rule_commentary()` (lines 319–344)
- **個股深度分析面板整合與 TDPQ 狀態機**:
  - `nexus_core/cogs/unified_terminal/symbol_deep_dive.py`: lines 423–447
- **終端 Embed 渲染與標籤展現**:
  - `nexus_core/cogs/embed_builders/portfolio_embeds.py`: lines 1479–1483
  - `nexus_core/cogs/embed_builders/market_embeds.py`: lines 1280–1310
- **數據傳輸綱要**:
  - `nexus_core/models/schemas.py`: lines 231–232
