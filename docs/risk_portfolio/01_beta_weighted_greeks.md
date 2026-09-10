# Beta 加權 Delta 與投資組合二階 Gamma 曝險數學模型 (Beta-Weighted Delta & Portfolio Second-Order Gamma Metrics)

## 1. 核心哲學與適用市場環境

### 1.1 多元期權投資組合的維度災難與基準錨定
在多策略、多標的的現代期權投資組合管理中，交易員面臨的最根本風控障礙在於**「敏感度不可相加性」**。若單純將 AAPL 的 Delta、TSLA 的 Delta 與 NVDA 的 Delta 線性累加，其結果在統計學與金融工程上毫無意義——因為 \$100 的低波動公用事業股波動 1%，與 \$1000 的高 Beta 科技股波動 1%，對帳戶資產淨值的實質衝擊有著天壤之別。

為了解決此維度災難，Nexus Seeker 採用美股頂級自營量化基金通用的**標準化指數加權法**：以標普 500 指數 ETF（SPY）作為全域基準錨點（Anchor Benchmark），透過資本資產定價模型（CAPM）的 $\beta$ 係數與相對股價比值，將所有現貨持倉與各到期日、各履約價之期權部位，全部換算為等效的 **SPY Beta 加權 Delta（$\Delta_{\text{SPY}}$）**。

### 1.2 二階 Gamma 曝險與二次微商連鎖律定理
Delta 僅代表價格的一階敏感度（一階導數），在小幅度價格變動下有效；然而美股市場充斥著非線性跳空與做市商踩踏，此時決定投資組合生存命脈的是二階敏感度——**Gamma（$\Gamma$）**。

Gamma 衡量 Delta 隨標的價格變動的加速度。當從個別標的資產 $S_i$ 映射至 SPY 價格時，許多風控系統犯下了直接將 Gamma 乘以單純權重 $w_i$ 的數學謬誤。依據多元微積分連鎖律，**二階導數的變數變換必須引入權重的一階導數平方項 $(w_i)^2$**。忽略此平方效應，將嚴重低估高價、高 Beta 個股在極端市場波動時引發的非線性爆倉風險。

### 1.3 投資組合熱度 (Portfolio Heat) 與動態對沖
透過將全帳戶所有現貨、期權買方、期權賣方統一映射至 SPY，系統即時計算：
1. **Delta Dollars（淨方向性美元曝險）**；
2. **Portfolio Heat %（組合熱度百分比）**；
3. **Daily Theta（每日無風險時間價值現金流）**。
當組合熱度逼近警戒上限（80%）時，風控系統將自動觸發部位縮減或反向指數期權對沖（VTR 對沖機制）。

---

## 2. 數學模型與量化推導

### 2.1 標的 Beta 係數計量估計模型
設基準資產為標普 500 ETF（代碼：`SPY`）。採樣標的資產 $i$ 與 SPY 在過去至少 60 個交易日（$N \ge 60$）的日收盤價序列，計算每日連續對數收益率：
$$R_{i, t} = \ln\left(\frac{P_{i, t}}{P_{i, t-1}}\right), \quad R_{\text{SPY}, t} = \ln\left(\frac{P_{\text{SPY}, t}}{P_{\text{SPY}, t-1}}\right)$$

標的資產相對於 SPY 的 Beta 係數由樣本協方差與 SPY 樣本變異數定義：
$$\beta_i = \frac{\operatorname{Cov}(R_i, R_{\text{SPY}})}{\operatorname{Var}(R_{\text{SPY}})} = \frac{\sum_{t=1}^N (R_{i, t} - \bar{R}_i)(R_{\text{SPY}, t} - \bar{R}_{\text{SPY}})}{\sum_{t=1}^N (R_{\text{SPY}, t} - \bar{R}_{\text{SPY}})^2}$$

**約束與剪裁（Clipping）**：
$$\beta_i \leftarrow \operatorname{clip}(\beta_i, -5.0, 5.0)$$
若樣本歷史小於 60 日、對數收益率無有效變動、或 $\operatorname{Var}(R_{\text{SPY}}) \le 10^{-9}$，系統平滑降級預設 $\beta_i = 1.0$。

### 2.2 Beta 加權換算因子 (Weight Factor)
將標的 $i$ 的股價變動等效轉換為 SPY 股價變動的無因次轉換權重因子 $w_i$：
$$w_i = \beta_i \times \left(\frac{S_i}{S_{\text{SPY}}}\right)$$

- **推導直覺**：當 SPY 價格變動 1 美元時，其相對百分比變動為 $\frac{1}{S_{\text{SPY}}}$；由 CAPM 定理，標的 $i$ 的相對百分比變動預期為 $\beta_i \times \frac{1}{S_{\text{SPY}}}$；換算為標的 $i$ 的絕對美元變動即為 $\beta_i \times \frac{S_i}{S_{\text{SPY}}} = w_i$ 美元。

### 2.3 一階 Beta 加權 Delta 與投資組合熱度

#### 1. 現貨部位 (Stock Holdings)
現貨每股對自身的 Delta 恆為 1.0。若持有 $Q_i$ 股（正數代表做多，負數代表做空）：
$$\Delta_{\text{SPY}, i}^{\text{stock}} = Q_i \times 1.0 \times w_i = Q_i \times \beta_i \times \left(\frac{S_i}{S_{\text{SPY}}}\right)$$

#### 2. 期權部位 (Option Contracts)
若持有 $Q_i$ 口期權合約（每口代表 100 股），其 BSM 一階 Delta 為 $\Delta_{\text{BSM}, i} \in [-1.0, 1.0]$：
$$\Delta_{\text{SPY}, i}^{\text{option}} = \Delta_{\text{BSM}, i} \times Q_i \times 100 \times w_i = \Delta_{\text{BSM}, i} \times Q_i \times 100 \times \beta_i \times \left(\frac{S_i}{S_{\text{SPY}}}\right)$$

#### 3. 投資組合總 Beta 加權 Delta
全帳戶 $M$ 個標的與所有合約的總等效 SPY 股數為：
$$\Delta_{\text{portfolio, SPY}} = \sum_{k \in \text{Positions}} \Delta_{\text{SPY}, k}$$

#### 4. 美元淨曝險 (Delta Dollars) 與組合熱度 (Portfolio Heat)
$$\text{Delta Dollars} = \Delta_{\text{portfolio, SPY}} \times S_{\text{SPY}}$$
$$\text{Portfolio Heat \%} = \frac{|\Delta_{\text{portfolio, SPY}}| \times S_{\text{SPY}}}{\text{Total Capital}} \times 100\%$$

### 2.4 二階 Beta 加權 Gamma 連鎖律二次微商數學推導
設投資組合在標的 $i$ 上的期權總價值為 $V_i$。依據定義：
$$\Delta_i = \frac{\partial V_i}{\partial S_i}, \quad \Gamma_i = \frac{\partial^2 V_i}{\partial S_i^2}$$

依據微積分一階連鎖律，該部位關於 SPY 價格的一階變動為：
$$\frac{\partial V_i}{\partial S_{\text{SPY}}} = \frac{\partial V_i}{\partial S_i} \cdot \frac{d S_i}{d S_{\text{SPY}}} = \Delta_i \cdot w_i$$

現在對 SPY 價格 $S_{\text{SPY}}$ 再次求導以計算二階敏感度 $\Gamma_{\text{SPY}, i}$：
$$\Gamma_{\text{SPY}, i} = \frac{\partial^2 V_i}{\partial S_{\text{SPY}}^2} = \frac{d}{d S_{\text{SPY}}} \left( \frac{\partial V_i}{\partial S_i} \cdot \frac{d S_i}{d S_{\text{SPY}}} \right)$$

根據乘積法則（Product Rule）：
$$\frac{\partial^2 V_i}{\partial S_{\text{SPY}}^2} = \left( \frac{d}{d S_{\text{SPY}}} \frac{\partial V_i}{\partial S_i} \right) \cdot \frac{d S_i}{d S_{\text{SPY}}} + \frac{\partial V_i}{\partial S_i} \cdot \left( \frac{d^2 S_i}{d S_{\text{SPY}}^2} \right)$$

在線性資產定價假設下，$S_i$ 與 $S_{\text{SPY}}$ 呈現一階線性關係，二階微商 $\frac{d^2 S_i}{d S_{\text{SPY}}^2} \approx 0$。對前項應用連鎖律：
$$\frac{d}{d S_{\text{SPY}}} \frac{\partial V_i}{\partial S_i} = \frac{\partial^2 V_i}{\partial S_i^2} \cdot \frac{d S_i}{d S_{\text{SPY}}} = \Gamma_i \cdot w_i$$

將其代回原式，嚴格導出二次方微商乘數：
$$\Gamma_{\text{SPY}, i} = \Gamma_i \cdot w_i \cdot \frac{d S_i}{d S_{\text{SPY}}} = \Gamma_i \cdot (w_i)^2$$

因此，持有 $Q_i$ 口期權合約時，換算至 SPY 的二階 Gamma 公式為：
$$\Gamma_{\text{SPY}, i} = \Gamma_{\text{BSM}, i} \times Q_i \times 100 \times (w_i)^2 = \Gamma_{\text{BSM}, i} \times Q_i \times 100 \times \left(\beta_i \frac{S_i}{S_{\text{SPY}}}\right)^2$$
$$\Gamma_{\text{portfolio, SPY}} = \sum_{k \in \text{Options}} \Gamma_{\text{SPY}, k}$$

### 2.5 Daily Theta、Vega 與 Vanna 加權聚合模型
1. **每日時間價值衰減 (Daily Theta)**：
   由於 `py_vollib` 與標準 BSM 公式輸出的 Theta 為年化值，系統將其嚴格除以 365 轉換為每日現金流：
   $$\Theta_{\text{daily}} = \sum_{k \in \text{Options}} \frac{\Theta_{\text{annual}, k} \times Q_k \times 100}{365.0}$$
2. **Beta 加權 Vega（波動率曝險）**：
   $$\text{Vega}_{\text{SPY}} = \sum_{k \in \text{Options}} \text{Vega}_k \times Q_k \times 100 \times w_k$$
3. **Beta 加權 Vanna（二階波動-方向交叉敏感度）**：
   $$\text{Vanna}_{\text{SPY}} = \sum_{k \in \text{Options}} \text{Vanna}_k \times Q_k \times 100 \times w_k$$

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([觸發投資組合 Greeks 刷新循環]) --> FetchSPY[抓取 SPY 即時價格與波動率]
    FetchSPY --> LoopPositions[遍歷所有未平倉部位 Position_i]

    LoopPositions --> TypeCheck{部位類型是現貨還是期權?}

    TypeCheck -- 現貨 (Stock/Perpetual) --> CalcStockBeta[計算 60日對數收益率 Beta]
    CalcStockBeta --> CalcWeightStock[權重因子 w = Beta * Spot / SPY]
    CalcStockBeta --> StockDelta[Delta_SPY = Q * w]
    StockDelta --> AccumulateDelta[累加至 total_beta_delta]

    TypeCheck -- 期權 (Option) --> FetchChain[拉取期權鏈，保留全履約價不裁減]
    FetchChain --> BSMGreeks[求解 BSM: Delta, Gamma, Theta, Vega, Vanna]
    BSMGreeks --> CalcWeightOpt[權重因子 w = Beta * Spot / SPY]

    CalcWeightOpt --> OptDelta[Delta_SPY = Delta_BSM * Q * 100 * w]
    CalcWeightOpt --> OptGamma[Gamma_SPY = Gamma_BSM * Q * 100 * w^2]
    CalcWeightOpt --> OptTheta[Daily Theta = Theta_annual * Q * 100 / 365]
    CalcWeightOpt --> OptVega[Vega_SPY = Vega_BSM * Q * 100 * w]

    OptDelta --> AccumulateAll[累加全域總曝險]
    OptGamma --> AccumulateAll
    OptTheta --> AccumulateAll
    OptVega --> AccumulateAll

    AccumulateAll --> NextPos{是否遍歷完畢?}
    NextPos -- 否 --> LoopPositions
    NextPos -- 是 --> HeatCheck[計算 Portfolio Heat = |Total Delta SPY| * SPY / Capital]

    HeatCheck --> LimitEval{Portfolio Heat >= 80%?}
    LimitEval -- 是 --> HeatAlarm["🚨 投資組合過熱警報<br/>限制開倉 / 觸發 VTR 對沖建議"]
    LimitEval -- 否 --> SafeState["🟢 風控水位正常<br/>輸出 Portfolio Dashboard"]
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 門檻 | 數值 / 設定 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `BETA_MIN_SAMPLE_DAYS` | `60` 日 | 標的與 SPY 收益率協方差計算之最小有效樣本長度 | `nexus_core/market_analysis/risk_engine.py` |
| `BETA_CLIP_LOWER` | `-5.0` | 標的 Beta 剪裁下限，防止極端反向槓桿扭曲 | `nexus_core/market_analysis/risk_engine.py` |
| `BETA_CLIP_UPPER` | `5.0` | 標的 Beta 剪裁上限，防止高波動仙股數值爆炸 | `nexus_core/market_analysis/risk_engine.py` |
| `BETA_DEFAULT_FALLBACK` | `1.0` | 資料異常或無共變異數時的平滑降級默認值 | `nexus_core/market_analysis/risk_engine.py` |
| `DAYS_IN_YEAR` | `365.0` | 年化 Theta 轉化為每日時間價值消耗的分母常數 | `nexus_core/market_analysis/portfolio.py` |
| `PORTFOLIO_HEAT_LIMIT` | `80.0\%` | 總淨 Delta 美元曝險佔帳戶總資產的安全預警紅線 | `nexus_core/market_analysis/risk_engine.py` |
| `CONTRACT_MULTIPLIER` | `100` | 標準美股期權合約乘數 | `nexus_core/market_analysis/portfolio.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 SPY 價格缺失與零除防護 (Zero Division Guard)
若在盤前或資料流中斷時，`self.spy_price <= 0`，計算權重因子 $w_i = \beta_i \frac{S_i}{S_{\text{SPY}}}$ 會引發 `ZeroDivisionError`。`portfolio.py:130` 與 `risk_engine.py:258` 設立前置硬鎖：
```python
weight_factor = (
    beta * (current_stock_price / self.spy_price)
    if self.spy_price > 0
    else 0.0
)
```
若基準價無效，加權因子安全退回 `0.0`，並阻斷倉位開立。

### 5.2 深度價外持倉期權鏈保留原則 (No-Pruning Contract Guarantee)
在計算歷史持倉 Greeks 時，部分持倉在建倉後現價可能已產生巨大位移，合約淪為深度價外（DOTM）或深度價內（DITM）。若行情抓取時套用常規的履約價範圍裁剪（如只抓 ATM $\pm 10\%$），這些真實持倉合約將憑空從期權鏈中消失，導致 Greeks 暴跌失真。`portfolio.py:162` 嚴格要求：
```python
option_chains_cache[expiry] = await get_option_chain(
    symbol, expiry, prune_pct=None
)
```
強制保留全鏈所有履約價，確保投資組合帳面資產與風險曝險的物理連續性。

---

## 6. 核心程式碼檔案路徑關聯

- **Beta 估計與基準計算**:
  - `nexus_core/market_analysis/risk_engine.py`: `calculate_beta()` (lines 84–112)
- **投資組合 Greeks 刷新與二階 Gamma 計算**:
  - `nexus_core/market_analysis/portfolio.py`: `PortfolioManager._process_symbol_positions()` (lines 97–215), `refresh_portfolio_greeks()` (lines 440–470)
- **單一期權合約 Greeks 求解器**:
  - `nexus_core/market_analysis/greeks.py`: `calculate_greeks()`, `calculate_vanna()` (lines 22–88)
- **保證金佔用與槓桿計算**:
  - `nexus_core/market_analysis/portfolio.py`: `calculate_option_margin()`
