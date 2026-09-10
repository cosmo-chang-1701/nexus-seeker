# 對沖績效 Brinson 歸因分析與動態 Tau 自我進化閉環 (Brinson Attribution Proxy & Dynamic Tau Self-Evolving Loop)

## 1. 核心哲學與適用市場環境

### 1.1 經典 Brinson 歸因理論與期權架構的幾何演進
在傳統主動式股票資產管理中，經典的 Brinson-Hood-Beebower (BHB 1986) 績效歸因模型將超額投資組合收益（Active Return）分解為三大正交維度：
1. **資產配置效應 (Allocation Effect)**：決定各板塊或資產類別的超配/低配權重；
2. **標的選擇效應 (Selection Effect)**：在各板塊內挑選超越基準標的個股的能力；
3. **交互效應 (Interaction Effect)**：配置權重與選股能力的交叉耦合。

然而，在現代**以期權為核心的非對稱對沖投資組合**中，傳統的板塊劃分完全無法捕捉非線性衍生品的微觀動態：
- 投資組合的多頭部位往往由「高 Alpha 優質成長股現貨 + 價內買權」構成；
- 對沖部位則由「高 Beta 大盤指數反向賣權（SPY Put）或波動率期權（VIX Call）」構成。
為此，Nexus Seeker 提出專為衍生性商品設計的 **Brinson 歸因代理模型（Brinson Attribution Proxy）**：將總盈虧直接解構為正交的 **Alpha 選股收益（$\text{PnL}_{\alpha}$）** 與 **市場對沖效應（$\text{PnL}_{\text{hedge}}$）**。

### 1.2 動態對沖強度 $\tau$ (Tau) 的自我進化哲學
在量化對沖操作中，固定參數的對沖模型注定失敗：
- 若對沖強度過高（Over-Hedged），在牛市主升段中，指數 Put 的持續磨損將全額吞噬個股 Alpha 浮盈；
- 若對沖強度過低（Under-Hedged），在黑天鵝暴跌降臨時，避險部位無法提供足夠的 Delta 緩衝，引發巨額回撤。

Nexus Seeker 構建了**閉環自我進化機制（Self-Evolving Feedback Loop）**：
- 系統每日自動追蹤過去 7 天的**對沖有效性（Hedge Effectiveness）**；
- 透過線性時間加權評估對沖是「有效吸收波動」還是「過度磨損拖累」；
- 自適應動態調適對沖強度參數 $\tau$（值域嚴格約束在 $[0.5, 1.5]$），實現「牛市自動減磨損、熊市自動加防禦」的智能自我進化。

---

## 2. 數學模型與量化推導

### 2.1 投資組合總盈虧正交分解方程
投資組合的真實總已實現與未實現淨損益（Net PnL）嚴格分解為兩大非重疊集合：
$$\text{Net PnL} = \text{PnL}_{\alpha} + \text{PnL}_{\text{hedge}}$$

#### 1. Alpha 選股效益 ($\text{PnL}_{\alpha}$)
來自所有一般現貨持倉與投機性方向期權部位（標註為 `trade_category != 'HEDGE'`）：
$$\text{PnL}_{\alpha} = \sum_{i \notin \text{HEDGE}} (P_{i, \text{curr}} - P_{i, \text{entry}}) \times Q_i \times 100$$
其對應的等效 SPY 一階敏感度為：
$$\Delta_{\alpha} = \sum_{i \notin \text{HEDGE}} \Delta_{\text{SPY}, i}$$

#### 2. 市場系統性對沖效益 ($\text{PnL}_{\text{hedge}}$)
來自大盤反向避險、VTR 對沖或指數防衛合約（標註為 `trade_category == 'HEDGE'`）：
$$\text{PnL}_{\text{hedge}} = \sum_{j \in \text{HEDGE}} (P_{j, \text{curr}} - P_{j, \text{entry}}) \times Q_j \times 100$$
其對應的等效 SPY 一階敏感度為：
$$\Delta_{\text{hedge}} = \sum_{j \in \text{HEDGE}} \Delta_{\text{SPY}, j}$$

### 2.2 對沖比率 (Hedge Ratio) 與三態矩陣
定義對沖部位相對於主倉部位的一階 Delta 抵消比率：
$$\text{Hedge Ratio} = \begin{cases} \left| \frac{\Delta_{\text{hedge}}}{\Delta_{\alpha}} \right|, & \text{若 } \Delta_{\alpha} \neq 0 \\ 0.0, & \text{若 } \Delta_{\alpha} = 0 \end{cases}$$

系統據此輸出三態對沖診斷狀態：
$$\text{Hedge Status} = \begin{cases} \text{OVER\_HEDGED}, & \text{若 } \text{Hedge Ratio} > 1.10 \quad \text{[對沖過度，過度磨損牛市收益]} \\ \text{UNDER\_HEDGED}, & \text{若 } \text{Hedge Ratio} < 0.80 \quad \text{[對沖不足，下行曝險敞口過大]} \\ \text{OPTIMAL}, & \text{若 } 0.80 \le \text{Hedge Ratio} \le 1.10 \quad \text{[黃金對沖區間，完美平衡]} \end{cases}$$

### 2.3 對沖有效性公式 (Hedge Effectiveness)
對沖有效性 $E$ 衡量對沖部位是否成功在不擴大總波動的情況下中和主倉風險：
$$E = \begin{cases} \operatorname{clip}\left( 1.0 - \frac{|\text{Net PnL}|}{|\text{PnL}_{\alpha}|}, \; 0.0, \; 1.0 \right), & \text{若 } |\text{PnL}_{\alpha}| > 0 \\ 0.0, & \text{若 } |\text{PnL}_{\alpha}| = 0 \end{cases}$$
- 當 $|\text{Net PnL}| \to 0$：代表對沖收益精準抵消了 Alpha 部位的虧損，有效性 $E \to 1.0$；
- 當 $|\text{Net PnL}| \ge |\text{PnL}_{\alpha}|$：代表對沖不僅未能減震，反而造成了額外的同向磨損，有效性 $E = 0.0$。

### 2.4 動態 Tau ($\tau$) 自我進化閉環算法
設每日對沖有效性與盈虧資料被寫入歷史日誌庫。系統採樣過去 $K$ 個交易日（$3 \le K \le 7$）之觀測樣本集：
$$\mathcal{H} = \left\{ (E_t, \text{PnL}_{\alpha, t}, \text{PnL}_{\text{hedge}, t}) \;\middle|\; t = 1, \dots, K \right\}$$

計算線性加權平均有效性 $\bar{E}$，越接近當前的交易日給予更高權重（權重向量從 0.5 線性遞增至 1.0）：
$$w_t = 0.5 + 0.5 \times \left(\frac{t - 1}{K - 1}\right), \quad \bar{E} = \frac{\sum_{t=1}^K w_t E_t}{\sum_{t=1}^K w_t}$$

#### 狀態調整與更新方程
設當前對沖強度參數為 $\tau_t$：
$$\tau_{t+1} = \begin{cases} \operatorname{clip}(\tau_t - 0.05, \; 0.5, \; 1.5), & \text{若 } \bar{E} < 0.50 \land \sum \text{PnL}_{\alpha} > 0 \land \sum \text{PnL}_{\text{hedge}} < 0 \\ \operatorname{clip}(\tau_t + 0.10, \; 0.5, \; 1.5), & \text{若 } \sum \text{Net PnL} < 0 \land \bar{E} < 0.70 \\ \tau_t, & \text{其他 (維持原狀)} \end{cases}$$

- **調降規則**：若主倉賺錢、對沖賠錢且有效性低於 50%，表明市場正處於無阻力單邊牛市，對沖部位產生不必要的「保險費拖累」，系統自適應將 $\tau$ 下調 0.05；
- **調升規則**：若帳戶整體為淨虧損且有效性低於 70%，表明下行保護嚴重不足，系統自適應將 $\tau$ 上調 0.10，加強防禦靈敏度。

### 2.5 事件保護評分與 Polymarket 預測市場邊界增益
在 `AttributionEngine` 中，針對平倉的對沖事件計算保護得分：
$$\text{Base Efficiency} = \left( \frac{\text{Loss Avoided}}{\text{Cost of Hedge}} \right) \times 50.0$$

#### Polymarket 相關性修正乘數 ($M_{\text{poly}}$)
若對沖建倉時的 Polymarket 宏觀事件快照顯示存在機率極端事件（市場已定價明確方向）：
$$M_{\text{poly}} = \begin{cases} 1.20, & \text{若存在事件 } \text{Odds} > 0.70 \lor \text{Odds} < 0.30 \\ 1.00, & \text{其他} \end{cases}$$

最終保護評分：
$$\text{Protection Score} = \operatorname{clip}\Big(\text{Base Efficiency} \times M_{\text{poly}}, \; 0.0, \; 100.0\Big)$$

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([觸發每日對沖歸因審計]) --> FetchTrades[抓取用戶真實與虛擬持倉]
    FetchTrades --> SplitTrades[依 trade_category 正交拆分: Alpha 部位 vs Hedge 部位]

    SplitTrades --> CalcPnL[計算各部位 PnL 與加權 Delta]
    CalcPnL --> CalcRatio[計算 Hedge Ratio = |Delta_hedge / Delta_alpha|]

    CalcRatio --> RatioEval{Hedge Ratio 水位評估}
    RatioEval -->|> 1.10| StatusOver[OVER_HEDGED 對沖過度<br/>磨損牛市 Alpha 收益]
    RatioEval -->|< 0.80| StatusUnder[UNDER_HEDGED 對沖不足<br/>暴露系統性下行風險]
    RatioEval -->|0.80 <= Ratio <= 1.10| StatusOpt[OPTIMAL 黃金平衡<br/>對沖比例最佳適配]

    StatusOver --> CalcEff[計算對沖有效性 Effectiveness = 1 - |Net PnL| / |Alpha PnL|]
    StatusUnder --> CalcEff
    StatusOpt --> CalcEff

    CalcEff --> LogDaily[寫入資料庫 hedge_history 日誌]
    LogDaily --> QueryHistory[查詢過去 7 天對沖歷史記錄]

    QueryHistory --> SampleCheck{歷史有效天數 >= 3 天?}
    SampleCheck -- 否 --> KeepTau[樣本不足: 維持當前 Tau 不變]
    SampleCheck -- 是 --> CalcWeightedEff[計算 7日線性加權平均有效性 E_bar]

    CalcWeightedEff --> EvolveDecision{自我進化調適規則}
    EvolveDecision -->|E_bar < 0.5 且 Alpha>0 且 Hedge<0| ReduceTau[牛市對沖磨損過大<br/>Tau 縮減: tau = clip tau - 0.05, 0.5, 1.5]
    EvolveDecision -->|Net PnL < 0 且 E_bar < 0.7| IncreaseTau[下行保護不足產生淨虧<br/>Tau 強化: tau = clip tau + 0.10, 0.5, 1.5]
    EvolveDecision -->|其他穩健狀況| KeepTau

    ReduceTau --> UpdateUserConfig[持久化寫入 user_config.dynamic_tau]
    IncreaseTau --> UpdateUserConfig
    KeepTau --> OutputReport([輸出歸因分析報告與 Evolution Dashboard])
    UpdateUserConfig --> OutputReport
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 / 門檻 | 數值 / 設定 | 物理意義與代碼約束 | 核心程式碼檔案路徑 |
|---|---|---|---|
| `HEDGE_RATIO_OVER_THRESHOLD` | $> 1.10$ | 判定對沖過度（OVER_HEDGED）的上限門檻 | `nexus_core/market_analysis/hedging.py` |
| `HEDGE_RATIO_UNDER_THRESHOLD` | $< 0.80$ | 判定對沖不足（UNDER_HEDGED）的下限門檻 | `nexus_core/market_analysis/hedging.py` |
| `TAU_CLIP_LOWER` | `0.50` | 動態 Tau 參數物理下限，防完全關閉對沖保護 | `nexus_core/market_analysis/hedging.py` |
| `TAU_CLIP_UPPER` | `1.50` | 動態 Tau 參數物理上限，防過度對沖拖垮本金 | `nexus_core/market_analysis/hedging.py` |
| `TAU_STEP_INCREASE` | `+0.10` | 遭遇下行淨虧損時的保護強化自適應步長 | `nexus_core/market_analysis/hedging.py` |
| `TAU_STEP_DECREASE` | `-0.05` | 牛市遭遇持續無效磨損時的靈敏度衰減步長 | `nexus_core/market_analysis/hedging.py` |
| `TAU_LOOKBACK_DAYS` | `7` 日 | 動態自我進化閉環的樣本滾動窗口長度 | `nexus_core/market_analysis/hedging.py` |
| `TAU_MIN_SAMPLES` | `3` 日 | 觸發動態 Tau 參數自我進化所需的最小有效樣本數 | `nexus_core/market_analysis/hedging.py` |
| `POLY_ODDS_UPPER` | `0.70` | Polymarket 判定事件定價高概率邊界 | `nexus_core/market_analysis/attribution.py` |
| `POLY_ODDS_LOWER` | `0.30` | Polymarket 判定事件定價低概率邊界 | `nexus_core/market_analysis/attribution.py` |
| `POLY_CORRELATION_MULT` | `1.20` | 宏觀預測事件強相關加成修正乘數 | `nexus_core/market_analysis/attribution.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 樣本不足冷啟動防護 (Cold Start Guard)
當新使用者建倉或剛啟用對沖模組時，資料庫中的歷史對沖記錄少於 3 天（`len(history) < 3`）。此時缺乏足夠的統計顯著性，若盲目更新 Tau 會引發嚴重的過度擬合或噪音震盪。`hedging.py:268` 明確約束：
```python
if not history or len(history) < 3:
    return 1.0
```
未滿 3 日強制返回標準基準值 $\tau = 1.0$，直到積累足夠的多空檢驗數據。

### 5.2 Alpha 收益為零時的有效性除以零防護 (Zero Alpha PnL Guard)
若投資組合處於完全剛建倉狀態，標的價格尚未變動（$|\text{PnL}_{\alpha}| = 0$），在計算有效性 $1 - \frac{|\text{Net PnL}|}{|\text{PnL}_{\alpha}|}$ 時會引發除以零錯誤。代碼在 `hedging.py:230` 中設置安全條件運算式：
```python
effectiveness = (
    max(0.0, min(1.0, 1.0 - (abs(net_pnl) / abs(alpha_pnl))))
    if abs(alpha_pnl) > 0
    else 0.0
)
```
確保在此邊界下有效性精確回傳 `0.0`，不拋出異常。

### 5.3 對沖成本為零防護 (Zero Cost Protection Guard)
在計算事件保護評分時，若未支付任何對沖成本（$\text{Cost of Hedge} \le 0$），若避免了損失則給予最高分 100 分，否則為 0 分，避免數值無窮大溢出。

---

## 6. 核心程式碼檔案路徑關聯

- **對沖績效分析與動態 Tau 自我進化閉環**:
  - `nexus_core/market_analysis/hedging.py`: `analyze_hedge_performance()` (lines 168–247), `calculate_daily_effectiveness()` (lines 249–262), `calculate_dynamic_tau()` (lines 264–285)
- **分析中心投資組合執行管線 (Brinson Proxy 整合)**:
  - `nexus_core/market_analysis/analyst_runners/portfolio_runner.py`: lines 68–124 (`brinson_attribution_proxy`)
- **自我進化歸因引擎與事件保護評分**:
  - `nexus_core/market_analysis/attribution.py`: `AttributionEngine.calculate_protection_score()` (lines 17–45), `AttributionEngine.finalize_vtr_attribution()` (lines 80–158)
- **資料庫持久化支援**:
  - `nexus_core/database/portfolio.py`: `add_hedge_history()`, `get_hedge_history()`, `upsert_user_config()`
