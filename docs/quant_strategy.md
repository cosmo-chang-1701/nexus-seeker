# 量化與風控策略 (Quantitative Strategy)

Nexus Seeker 的核心價值在於其嚴謹的量化風控與交易引擎。以下摘錄專案中實作的關鍵策略模型：

## 1. 大盤微觀結構與 Gamma Flip (SHORT_GAMMA_CRITICAL)
- 透過解析 SPY 選擇權鏈，計算出 Net GEX 正負變化的零 Gamma 臨界線 (Gamma Flip Line)。
- 當 $VIX > 20$ 且大盤跌破零 Gamma 線（處於 Backwardation 與負 Gamma 區間），系統自動將 GTC 網格買單的間距（`dynamic_grid_step`）拉大 **1.5 倍**，以減緩崩跌時的資金損耗。

## 2. 總體經濟與動態逃頂窗口
- 即時爬取 CME FedWatch 利率機率與 FRED 核心總經數據。
- 系統盤前分析若偵測到 Fed 利率維持高位（hawkish），將自動把使用者自訂的「反彈逃頂窗口」**前移 5 個交易日**；若預期降息則後推 5 天，增強風險偏好。

## 3. TDP (Triple Discount Pricing) 估值三擊
這是一個強力的長線佈局訊號。當標的現價同時跌破以下四大支撐時，觸發「TDP 估值三擊」青色燈號：
- EMA 21 均線
- 期權做市商 Max Pain 痛點
- V-POC (成交量控制點, Volume Point of Control)
- DP-POC (暗池控制點, Dark Pool Point of Control)

## 4. Kelly Criterion 風控與流動性防禦
- **動態倉位分配**：根據使用者設定的 `capital` 與 `risk_limit`，結合期權隱含波動率，推算安全的期權建倉口數。極端 $VIX$ 條件下會進行折讓縮放。
- **流動性滑價閘門**：如果目標合約的買賣點差比率 (Spread Ratio = `(Ask - Bid) / Mid`) 大於 15%，系統將標記為 `is_illiquid = True`，強制將自動執行計畫變更為 `WAIT`，阻止高風險市價交易。

## 5. 動態均價與 Covered Call 解鎖 (CC Recovery)
- 將現有持倉與未成交的活躍網格買單加權，計算出「未來預期新均價 (New Cost Basis)」。
- 針對套牢部位，自動篩選 DTE 30-50 天、Delta < 0.15 且履約價高於新均價的合約，協助使用者啟動防禦性收租策略。
