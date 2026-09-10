# DITM 深價內凸性防護與獲利鎖定決策階梯 (Deep In-The-Money Convexity Protection & Profit-Locking Ladder)

## 1. 核心哲學與適用市場環境

### 1.1 選擇權買方的靈魂：凸性 (Convexity / Gamma)
在衍生性金融工具的微觀物理中，買方期權（Long Call / Long Put）之所以具備非對稱的吸引力，其根本原因並非槓桿，而是源自二階微商敏感度——**凸性（Convexity），即 Gamma（$\Gamma = \frac{\partial^2 V}{\partial S^2}$）**。

在凸性保護下：
- 當標的價格朝有利方向前進時，$\text{Delta}$ 自動擴張，部位獲利速度加速增長；
- 當標的價格朝不利方向逆轉時，$\text{Delta}$ 自動萎縮，部位虧損速度減速收斂。
這種「贏時加速、輸時減速」的非線性結構，是期權買方對抗現貨與期貨線性風險的唯一數學護城河。

### 1.2 凸性衰竭 (Convexity Exhaustion) 的殘酷困境
然而，當期權多頭獲得巨額盈利，合約深入價內（Deep In-The-Money, DITM）時，會發生嚴重的**微觀結構退化**：
1. **凸性歸零**：隨著履約價被遠遠甩在身後，$\Delta \to 1.0$（或負向達到 $-1.0$），而 $\Gamma \to 0$。
2. **槓桿劣化為現股**：期權的非線性優勢徹底消失，每一美元的股價波動對應恰好一美元的期權價值波動（1:1 線性曝險）。
3. **對稱下行暴跌風險**：此時交易員名義上持有期權，實質上等同於持有全額現股；一旦現貨價格回檔，帳面巨額利潤將全額回吐。
4. **持續的時間價值消耗**：更糟的是，期權即便在價內仍承受時間價值衰減（Theta 耗損）與高昂的買賣價差（Bid-Ask Spread）。

這種「承擔正股的全部下行風險，卻繼續支付期權的時間租金」的狀態，在金融工程中被稱為 **凸性衰竭陷阱（Convexity Exhaustion Trap）**。

### 1.3 DITM 防禦與主動獲利鎖定
Nexus Seeker 的 `evaluate_ditm_defense()` 模組是專為解決凸性衰竭而設計的自動化資產保護階梯：
- 當部位達到絕對 Delta $\ge 0.85$、未實現利潤 $> 150\%$ 且剩餘到期時間 $\le 21$ 天時，系統主動發出警報介入；
- 若剩餘天數極短（$\le 7$ 天），強制建議 **`DEFENSIVE_CLOSE`（平倉落袋）**，徹底規避末日波動與流動性擠壓；
- 若仍有 8 至 21 天，強制建議 **`ROLL_UP_OUT`（轉倉防禦）**：向上平移履約價並延長期限，全數抽回前期本金與大部分利潤，重新換取高凸性合約。

---

## 2. 數學模型與量化推導

### 2.1 二階泰勒展開式與凸性衰竭證明
設期權合約價值為 $V(S, t)$。依據伊藤引理與二階泰勒展開，在極小時間間隔 $\Delta t$ 內，期權價值的增量可展開為：
$$\Delta V \approx \Delta \cdot (\Delta S) + \frac{1}{2} \Gamma \cdot (\Delta S)^2 + \Theta \cdot (\Delta t)$$

根據 Black-Scholes-Merton 模型，歐式看漲期權的 Delta 與 Gamma 公式為：
$$\Delta = \mathcal{N}(d_1), \quad \Gamma = \frac{\phi(d_1)}{S \sigma \sqrt{T}}$$
其中：
$$d_1 = \frac{\ln(S/K) + (r + \frac{\sigma^2}{2})T}{\sigma \sqrt{T}}$$

當標的價格大幅超越履約價時（$S \gg K$）：
$$\lim_{S/K \to \infty} \ln(S/K) = +\infty \implies \lim_{S/K \to \infty} d_1 = +\infty$$
代入標準常態分佈累計分佈函數 $\mathcal{N}$ 與概率密度函數 $\phi$：
$$\lim_{d_1 \to \infty} \mathcal{N}(d_1) = 1.0, \quad \lim_{d_1 \to \infty} \phi(d_1) = \lim_{d_1 \to \infty} \frac{1}{\sqrt{2\pi}} e^{-\frac{d_1^2}{2}} = 0$$

因此，在極限深價內條件下：
$$\lim_{S \gg K} \Gamma = 0$$
將此極限代回二階展開式：
$$\Delta V \approx 1.0 \cdot (\Delta S) + 0 + \Theta \cdot (\Delta t) = \Delta S + \Theta \cdot \Delta t$$

**數學結論**：
期權不再享有 $\frac{1}{2}\Gamma (\Delta S)^2$ 的上行非線性加速紅利，但在價格反向回檔（$\Delta S < 0$）時，卻必須承受 $100\%$ 的現貨暴跌損失加上持續為負的 $\Theta \cdot \Delta t$。其風險報酬特徵在數學上嚴格劣於平價期權。

### 2.2 DITM 防禦觸發四重門檻
系統對投資組合中的每一筆持倉進行即時連續掃描，必須同時滿足四大邊界條件：
$$\text{DITM Trigger} \iff \begin{cases} Q > 0 & \text{[持有買方多頭持倉]} \\ |\Delta| \ge 0.85 & \text{[合約進入深度價內，凸性嚴重衰竭]} \\ \text{PnL}_{\%} > 1.50 & \text{[未實現獲利超過 150\%，積累豐厚浮盈]} \\ \text{DTE} \le 21 & \text{[剩餘到期時間小於等於 21 天，面臨 Gamma 懸崖]} \end{cases}$$

### 2.3 決策映射函數與狀態轉移規則
由 `evaluate_ditm_defense()` 定義狀態轉移函數：
$$\text{Action} = \begin{cases} \text{DEFENSIVE\_CLOSE}, & \text{若 Trigger 為真 且 } \text{DTE} \le 7 \\ \text{ROLL\_UP\_OUT}, & \text{若 Trigger 為真 且 } 7 < \text{DTE} \le 21 \\ \text{HOLD}, & \text{其他} \end{cases}$$

### 2.4 轉倉抽回本金 (Capital Extraction) 與凸性重置模型
當觸發 `ROLL_UP_OUT` 時，交易員將現有高 Delta（$\Delta \approx 0.85$）合約平倉，並以部分資金買入較高履約價（$K_{\text{new}} > K_{\text{old}}$）且遠期（$\text{DTE}_{\text{new}} > \text{DTE}_{\text{old}}$）的平價合約（$\Delta_{\text{new}} \approx 0.50$）。

設原持倉收盤現值為 $V_{\text{curr}}$，原始開倉成本為 $V_{\text{entry}}$，新合約每口成本為 $V_{\text{new}}$：
1. **抽回無風險現金 (Extracted Cash)**：
   $$\text{Cash Extracted} = (V_{\text{curr}} - V_{\text{new}}) \times Q \times 100$$
2. **本金安全回收率**：
   $$\text{Principal Extraction Ratio} = \frac{\text{Cash Extracted}}{V_{\text{entry}} \times Q \times 100}$$
   通常 $\text{Cash Extracted} > V_{\text{entry}} \times Q \times 100$，意味著全額抽回原始投入本金並鎖定部分純利潤。
3. **凸性重置增益**：
   新合約的 $\Gamma_{\text{new}} \gg \Gamma_{\text{old}} \approx 0$，使投資組合重新取回強大的上行非線性加速護城河。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([投資組合監控循環 Portfolio Monitor]) --> PosLoop[審計單一持倉 Position]

    PosLoop --> LongCheck{是否為買方多頭持倉?<br/>quantity > 0}
    LongCheck -- 否 (賣方空頭部位) --> ShortEval[交由賣方防禦狀態機審計 evaluate_defense_status]
    LongCheck -- 是 (買方部位) --> DITMCheck{滿足 DITM 四重條件?<br/>|Delta| >= 0.85<br/>且 PnL > 150%<br/>且 DTE <= 21天?}

    DITMCheck -- 否 --> GeneralLongCheck{常規買方規則審計}
    GeneralLongCheck -->|PnL >= 100%| TPAlert[✅ 建議停利 Sell to Close]
    GeneralLongCheck -->|PnL <= -50%| SLAlert[⚠️ 停損警戒 本金回撤 50%]
    GeneralLongCheck -->|DTE <= 21| GammaAlert[🚨 動能衰竭 建議保留殘值]
    GeneralLongCheck -->|其他| HoldLong[⏳ 繼續持有 HOLD]

    DITMCheck -- 是 (凸性衰竭警報) --> DTEGate{剩餘到期時間 DTE <= 7 天?}

    DTEGate -- 是 (極短天期) --> ActionClose[🛑 DEFENSIVE_CLOSE<br/>防禦性平倉落袋<br/>規避末日流動性獵殺與踩踏]
    DTEGate -- 否 (7 < DTE <= 21) --> ActionRoll[🔄 ROLL_UP_OUT<br/>動態轉倉防禦<br/>向上平移履約價 + 延長到期日<br/>抽回原始本金 + 重置 Gamma 凸性]

    ActionClose --> GenerateAlert[發送 Discord 嵌入式風險警報]
    ActionRoll --> GenerateAlert
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 門檻 | 數值 / 設定 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `DITM_DELTA_THRESHOLD` | $\ge 0.85$ | 判定期權進入深價內、凸性喪失的絕對 Delta 閾值 | `nexus_core/market_analysis/risk_engine.py` |
| `DITM_PNL_PCT_THRESHOLD` | $> 1.50$ ($150\%$) | 判定累積超額浮盈、必須啟動利潤鎖定的損益百分比 | `nexus_core/market_analysis/risk_engine.py` |
| `DITM_MAX_DTE` | $\le 21$ 天 | 進入 Gamma 懸崖與時間價值加速衰竭的剩餘天數上限 | `nexus_core/market_analysis/risk_engine.py` |
| `DITM_CLOSE_CRITICAL_DTE` | $\le 7$ 天 | 由轉倉防禦升級為強制平倉落袋的極短天期閾值 | `nexus_core/market_analysis/risk_engine.py` |
| `CONVEXITY_RESET_DELTA` | $\approx 0.50$ | 轉倉建議買入之目標平價合約基準 Delta 水平 | `nexus_core/cogs/trading/portfolio_monitor.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 賣方部位隔離防護 (Short Position Isolation)
`evaluate_ditm_defense()` 的數學模型專門針對「買方期權凸性衰竭」。若誤將賣方部位（`quantity < 0`）傳入，其持倉 Delta 亦可能達到極大絕對值（例如賣出的 Put 跌入價內）。但賣方在價內屬於虧損擴大狀態，其處置邏輯為停損（Stop-Loss）或向下轉倉（Roll Down & Out），絕非「獲利鎖定」。因此函式在第 45 行建立前置隔離：
```python
if quantity <= 0:
    return DITMDefenseAction.HOLD
```
強制賣方持倉交由獨立的 `evaluate_defense_status()` 進行黑天鵝強制停損或 Gamma 陷阱防禦。

### 5.2 虛假暴利與數據尖刺過濾 (Price Spike Guard)
若標的期權交投稀疏，盤中出現做市商抽單引發的荒謬買賣報價（例如 Ask 報價瞬間被拉抬 10 倍導致帳面 PnL 虛高達 500%），系統在 `portfolio_monitor.py` 中以成交量量能與期權中間價（Mid Price）進行有效性校驗，避免在虛假報價下過早平倉真實持倉。

---

## 6. 核心程式碼檔案路徑關聯

- **DITM 凸性防禦決策引擎**:
  - `nexus_core/market_analysis/risk_engine.py`: `evaluate_ditm_defense()` (lines 30–58), `DITMDefenseAction` (lines 15–18)
- **投資組合即時風控監控器**:
  - `nexus_core/cogs/trading/portfolio_monitor.py`: lines 381–420, lines 1280–1300
- **幽靈交易與模擬執行器**:
  - `nexus_core/market_analysis/ghost_trader.py`: lines 134–195
- **Discord 警報 Embed 構建器**:
  - `nexus_core/cogs/embed_builders/alert_embeds/quote_and_risk_alerts.py`: lines 32–90
