# VIX 戰情階梯 6 階矩陣與動態分數凱利資金配置公式 (VIX Battle Ladder & Dynamic Fractional Kelly Criterion)

## 1. 核心哲學與適用市場環境

### 1.1 逆向流動性定價哲學
在量化期權交易中，最常見的致命錯誤是「在市場平靜時重倉賣期權，在市場暴跌恐慌時停損砍倉」。
- 當市場平靜時（VIX < 15），隱含波動率被壓縮至極致，期權權利金極度廉價。此時賣出賣權（Put）宛如在壓路機前撿一分錢，一旦黑天鵝降臨，微薄的利潤將在瞬間被非線性虧損吞噬；
- 反之，當市場遭遇系統性拋售（VIX 飆升至 25、30 甚至 35 以上），隱含波動率極度超買，市場瀰漫著非理性的恐慌溢價。此時期權賣方收取的權利金具備深厚的安全墊，是長期期望值最高、勝率賠率俱佳的黃金進攻窗口。

Nexus Seeker 將此逆向哲學規格化為 **VIX 戰情階梯（VIX Battle Ladder）**：由低至高劃分為 6 大戰情等級。**VIX 越低，系統越約束賣方行為；VIX 越高，系統越主動放大進攻資金配額**。

### 1.2 分數凱利公式 (Fractional Kelly Criterion) 的資金生存底線
約翰·凱利（John L. Kelly）於 1956 年提出的純凱利公式，旨在最大化長期資產組合的幾何平均增長率。然而，純凱利公式假設交易者具備無限資本且能承受極端的短期回撤（例如 80% 本金回撤）。在真實金融市場中，過度激進的純凱利下注將在遭遇連輸時引發破產或心理崩潰。

因此，系統嚴格採取**分數凱利（Fractional Kelly）體系**：
- 默認採用 **四分之一凱利（Quarter-Kelly, 0.25x）** 至 **半凱利（Half-Kelly, 0.50x）** 縮放；
- 對單筆交易設立物理硬上限（賣方策略最高 5%，買方策略最高 3%）；
- 結合 VIX 歷史分位數進行動態插值，在宏觀極端恐慌時，才謹慎調升至半凱利。

---

## 2. 數學模型與量化推導

### 2.1 凱利公式推導 (Capital Growth Maximization)
設單筆交易的資金下注比例為 $f$。當獲勝時，淨賠率為 $b$（每下注 1 單位淨賺 $b$ 單位）；失敗時損失全部下注額 1 單位。設獲勝概率為 $p$，失敗概率為 $q = 1 - p$。
經過 $N$ 次交易後，投資組合的總資產增長率期望值為：
$$G(f) = \mathbb{E}[\ln(W_N / W_0)] = p \ln(1 + b f) + (1 - p) \ln(1 - f)$$

為了求解最佳資金下注比例 $f^*$，對 $f$ 進行一階求導並令導數為零：
$$\frac{d G}{d f} = \frac{p \cdot b}{1 + b f} - \frac{1 - p}{1 - f} = 0$$
$$p \cdot b (1 - f) = (1 - p)(1 + b f)$$
$$p b - p b f = 1 + b f - p - p b f$$
$$p b - 1 + p = b f \implies f^* = \frac{p b - (1 - p)}{b} = p - \frac{1 - p}{b}$$

### 2.2 期權交易的勝率 ($p$) 與賠率 ($b$) 映射模型
選擇權具備非線性的合約特徵，系統依據 BSM 定價 Greeks 與預期波幅進行精確映射：

#### 1. 賣方策略 (STO_PUT / STO_CALL)
- **勝率映射**：賣方的主要獲利來源是期權歸零失效，其到期處於價外的理論概率由 Delta 近似：
  $$p_{\text{STO}} = 1.0 - |\Delta_{\text{BSM}}|$$
- **賠率映射**：收益為收取的權利金（Bid），承擔的最大保證金風險為 $\text{Margin Required}$：
  $$b_{\text{STO}} = \frac{\text{Bid}}{\text{Margin Required}}$$

#### 2. 買方策略 (BTO_CALL / BTO_PUT)
- **勝率映射**：買方期權在到期日具備內含價值的概率由 Delta 定義：
  $$p_{\text{BTO}} = |\Delta_{\text{BSM}}|$$
- **賠率映射**：潛在期望收益由 7 天預期波幅（Expected Move）扣除買入權利金（Ask）決定：
  $$\text{Potential Profit} = \max(0, \text{Expected Move} - \text{Ask})$$
  $$b_{\text{BTO}} = \frac{\text{Potential Profit}}{\text{Ask}}$$

### 2.3 分數凱利縮放與物理倉位上限
由核心函式 `kelly_position_fraction()` 實現縮放與截斷：
$$f_{\text{allocated}} = \max\Big(0.0, \; \min\big(f^* \times \text{kelly\_scale}, \; \text{Cap}\big)\Big)$$
- 若 $b \le 0$ 或 $f^* \le 0$：期望值為負，強制 $f_{\text{allocated}} = 0.0$；
- **賣方倉位天花板**：$\text{Cap}_{\text{STO}} = 0.05$（單筆最高不超過總資金 5%）；
- **買方倉位天花板**：$\text{Cap}_{\text{BTO}} = 0.03$（單筆最高不超過總資金 3%）。

### 2.4 VIX 歷史分位數動態線性插值模型
系統採樣 VIX 歷史 10 年分佈，設定第 90 百分位上限：
$$\text{VIX}_{\text{upper\_10}} = 29.5, \quad \text{VIX}_{\text{ceiling}} = 45.0$$

當市場 VIX 突破 29.5 時，系統啟動動態 Kelly 縮放，在 29.5 至 45.0 之間對帳戶風險額度進行線性插值放大：
$$t = \min\left( \frac{\text{VIX} - 29.5}{45.0 - 29.5}, \; 1.0 \right)$$
$$\text{Kelly Scale Factor} = 1.0 + 0.5 t \quad (\in [1.0, 1.5])$$
$$\text{Effective Risk Limit} \leftarrow \text{Current Risk Limit} \times \text{Kelly Scale Factor}$$

### 2.5 日曆 Vanna 與尾部風險折價 (Calendar Vanna & Tail Risk Haircuts)
1. **日曆事件 Vanna 折價**：
   若標的距離重大事件（如財報、FDA 開牌）小於 72 小時：
   $$w_{\text{vanna}} = 1.5 + \max\left(0, \frac{72.0 - \text{TTE}}{72.0}\right)$$
   $$\text{Current Risk Limit} \leftarrow \text{Current Risk Limit} \times \left(\frac{1}{w_{\text{vanna}}}\right)$$
2. **高尾部風險 Gamma 脆性折價**：
   若標的微觀結構顯示正 Gamma 枯竭（$\text{is\_high\_tail\_risk} == \text{True}$），風險限額直接砍半：
   $$\text{Current Risk Limit} \leftarrow \text{Current Risk Limit} \times 0.50$$

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([啟動風控優化器 NRO / 倉位配置]) --> FetchVIX[獲取即時 VIX 與宏觀數據]

    FetchVIX --> VIXTierCheck{VIX 水位落在哪一階梯?}

    VIXTierCheck -->|VIX < 15.0| TierDormant[⚪ 休兵 Tier: STO 策略強制阻斷<br/>配額 0.0x / 禁止開倉]
    VIXTierCheck -->|15.0 <= VIX < 18.0| TierCaution[🟡 少買 Tier: 謹慎進場<br/>STO Delta Cap -0.12 / 配額 0.5x]
    VIXTierCheck -->|18.0 <= VIX < 24.0| TierReady[🟠 摩拳擦掌 Tier: 標準配置<br/>STO Delta Cap -0.20 / 配額 1.0x]
    VIXTierCheck -->|24.0 <= VIX < 30.0| TierAggressive[🔴 大買 Tier: 主動進攻<br/>STO Delta Cap -0.20 / 配額 1.2x]
    VIXTierCheck -->|30.0 <= VIX < 35.0| TierHeavy[🔴 重砲進場 Tier: 積極加碼<br/>STO Delta Cap -0.25 / 配額 1.5x]
    VIXTierCheck -->|VIX >= 35.0| TierExtreme[🟥 All-in Tier: 終極逆向進攻<br/>STO Delta Cap -0.35 / 配額 2.0x<br/>啟用 Half-Kelly 0.50 覆寫]

    TierDormant --> RejectTrade[終止操作 / 拒絕信號]
    TierCaution --> CalcKelly[計算勝率 p 與賠率 b]
    TierReady --> CalcKelly
    TierAggressive --> CalcKelly
    TierHeavy --> CalcKelly
    TierExtreme --> CalcKelly

    CalcKelly --> KellyFormula[計算純凱利 f* = p - 1-p / b]
    KellyFormula --> ApplyScaling[應用分數縮放與 Cap<br/>STO: 5% / BTO: 3%]

    ApplyScaling --> InterpCheck{VIX > 29.5?}
    InterpCheck -- 是 --> DynamicInterp[線性插值放大 Risk Limit<br/>最高放大 1.5x]
    InterpCheck -- 否 --> HaircutCheck

    DynamicInterp --> HaircutCheck{檢查重大事件與尾部風險}
    HaircutCheck -->|TTE < 72小時| VannaCut[應用 Vanna Haircut: 乘 1 / w_vanna]
    HaircutCheck -->|高尾部風險| TailCut[Gamma 脆性: 風險限額砍半 x0.5]
    HaircutCheck -->|無異常| OutputPosition

    VannaCut --> OutputPosition[輸出最終建議開倉合約口數 safe_qty]
    TailCut --> OutputPosition
```

---

## 4. 關鍵具名常數與物理約束

### 4.1 VIX 戰情階梯 6 階矩陣 (`VIX_LADDER_CONFIG`)

| 階梯名稱 (Tier) | VIX 區間 $[V_{\min}, V_{\max})$ | 訊號許可 (`allow_signal`) | STO Delta 上限 (`sto_delta_cap`) | 倉位乘數 (`sizing_multiplier`) | 凱利覆寫 (`kelly_fraction_override`) | VTR 許可 | 狀態視覺 |
|---|---|---|---|---|---|---|---|
| **休兵 (Dormant)** | $[0.0, 15.0)$ | `False` | $0.00$ | $0.0\times$ | `None` | `False` | ⚪ 灰色 |
| **少買 (Caution)** | $[15.0, 18.0)$ | `True` | $-0.12$ | $0.5\times$ | `None` | `True` | 🟡 金黃 |
| **摩拳擦掌 (Ready)** | $[18.0, 24.0)$ | `True` | $-0.20$ | $1.0\times$ | `None` | `True` | 🟠 橙色 |
| **大買 (Aggressive)** | $[24.0, 30.0)$ | `True` | $-0.20$ | $1.2\times$ | `None` | `True` | 🔴 紅色 |
| **重砲進場 (Heavy)** | $[30.0, 35.0)$ | `True` | $-0.25$ | $1.5\times$ | `None` | `True` | 🔴 深紅 |
| **All-in (Extreme)** | $[35.0, 999.0)$ | `True` | $-0.35$ | $2.0\times$ | $0.50$ (Half-Kelly) | `True` | 🟥 暗紅 |

### 4.2 歷史分位數與風控參數 (`VIX_QUANTILE_BOUNDS`)

| 常數名稱 | 數值 / 設定 | 物理約束與代碼功能 | 核心程式碼路徑 |
|---|---|---|---|
| `upper_10` | `29.5` | VIX 歷史第 90 百分位，動態 Kelly 插值觸發起點 | `nexus_core/config.py` |
| `vix_ceiling` | `45.0` | 動態 Kelly 線性插值天花板，防止無窮外推 | `nexus_core/market_analysis/risk_engine.py` |
| `sto_kelly_cap` | `0.05` ($5.0\%$) | 單筆賣方交易佔總資本之凱利物理硬上限 | `nexus_core/market_analysis/strategy/liquidity_risk.py` |
| `bto_kelly_cap` | `0.03` ($3.0\%$) | 單筆買方交易佔總資本之凱利物理硬上限 | `nexus_core/market_analysis/strategy/liquidity_risk.py` |
| `event_tte_limit` | `72.0` 小時 | 觸發日曆 Vanna 隱含 Delta 折價的時間窗口 | `nexus_core/market_analysis/risk_engine.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

### 5.1 VIX < 15.0 休兵狀態一票否決 (Dormant Tier Lockout)
當 VIX 低於 15.0 時，市場波動率過低。若有交易員嘗試執行 STO 賣方建倉，`risk_engine.py:275` 設置絕對熔斷：
```python
if macro_data.vix < 15.0 and "STO" in strategy:
    logger.info(f"NRO Reject: VIX {macro_data.vix:.1f} is in Dormant tier. STO entry forbidden.")
    return OptimizationResult(
        suggested_contracts=0, exposure_pct=0.0, warnings=["VIX Dormant: STO 禁用"]
    )
```
此規則直接返回 `suggested_contracts = 0`，防止在低波動死水區過早消耗保證金。

### 5.2 賠率為零或為負防護 (Zero or Negative Odds Guard)
若因為數據延遲或深度價外，期權權利金報價為零（`bid <= 0`）或潛在利潤為負，賠率 $b \le 0$。若直接套入公式將引發除以零錯誤。`risk_engine.py:229` 設置前置防護：
```python
if odds <= 0:
    return 0.0
```
保證在無實質勝率空間時，輸出倉位配額精確為零。

### 5.3 All-in 模式的宏觀修正因子繞過 (Bypass Attenuation in All-in Mode)
在一般市場狀況下，若原油暴漲或 Skew 偏大，宏觀修正因子（$d_{\text{oil}}, d_{\text{regime}}$）會衰減風險限額。然而，當 $\text{VIX} \ge 35.0$ 時，系統判定這屬於歷史級世紀大底，此時若繼續套用原油或偏斜衰減將錯失最佳逆向建倉良機。因此 `risk_engine.py:296` 特別設計：
```python
if vix_spot is not None and vix_spot >= 35.0:
    current_risk_limit = risk_limit * d_vix  # d_vix = 2.0
    warnings.append("VIX Extreme: All-in 模式啟動")
```
直接以雙倍基準限額（$2.0\times$）繞過衰減，全力提供流動性支持。

---

## 6. 核心程式碼檔案路徑關聯

- **VIX 戰情階梯與分位數配置**:
  - `nexus_core/config.py`: `VIX_LADDER_CONFIG` (lines 99–172), `VIX_QUANTILE_BOUNDS` (lines 175–182)
- **凱利公式核心運算與 NRO 風險優化器**:
  - `nexus_core/market_analysis/risk_engine.py`: `kelly_position_fraction()` (lines 210–233), `optimize_position_risk()` (lines 235–360), `get_macro_modifiers()` (lines 169–207)
- **策略層流動性與倉位分配執行**:
  - `nexus_core/market_analysis/strategy/liquidity_risk.py`: lines 234–259
- **下單路由與執行閘門**:
  - `nexus_core/services/trading_service/execution.py`: lines 175–181
