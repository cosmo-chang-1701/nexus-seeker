# 5-Regime 市場環境動態路由矩陣技術規格書

## 1. 核心哲學與適用市場環境

在多變的美股量化交易實務中，單一指標或固定偏向的操盤模型（例如純順勢突破或純逆勢抄底）在市場結構轉換時往往遭受重大虧損。當市場處於強趨勢時，逆勢接刀將面臨連續追殺；而在區間震盪或流動性陷阱中，追漲買突破則容易落入做市商假突破洗盤。更危險的是，當系統性流動性危機爆發或上方存在龐大期權實體封頂時，任何未經環境過濾的多頭進場皆屬致命風險。

Nexus Seeker 的核心架構導入了 **5-Regime 市場結構動態路由矩陣**（`DynamicRegime`），將市場行情精確劃分為五種微觀拓撲狀態（依判定優先序列出）：
1. **Regime IV 宏觀鎖定分支（最高優先級，壓過一切）**：當大盤爆發系統性流動性危機（`SYSTEMIC_LIQUIDITY_CRISIS`）、做市商集體翻入負 Gamma 順向踩踏（`SHORT_GAMMA_CRITICAL`）、或 VIX 期限結構深度倒掛（Front-month 溢價超過 10%）時觸發。此狀態下系統硬性凍結**一切方向**的新開倉——包含做空：系統性流動性危機下空頭同樣會被劇烈軋空，不是安全的方向。此分支刻意在 15m K 線與 ATR 抓取**之前**判定，能早退就不為它多發一次網路請求。
2. **Regime V（破位追空態）**：標的現價同時跌破 Gamma Flip、Session VWAP 與 Put Wall，做市商翻入負 Gamma 並對下跌順勢助跌；上方阻力頂牆完好且牆距落在緩衝雙邊界內；下方至次級負 Gamma 節點尚有 $\ge 2.0 \times \text{ATR}_{1D}$ 的空間；伴隨 15 分鐘實體陰線與 1.5 倍放量，RSI 位於弱勢空頭區間（$< 45$）。此狀態路由至「做空破位追空六重鐵律」；通過後產生**獨立的 `SHORT_ENTRY` 做空進場訊號**（見 [`07_short_side_breakdown_ironclad.md`](07_short_side_breakdown_ironclad.md)），絕不進入機會成本轉倉或核心資金部署的多頭下游。由於 Regime V 要求跌破 Put Wall，`DYNAMIC` 模式只會產生「破位追空」子模式。
3. **Regime IV 個股結構封頂分支**：上方阻力牆空間低於動態自適應波動率門檻、或偵測到機構級單筆大額 STO Call 巨鯨物理封頂。此分支**排在 Regime V 之後**是刻意設計——壓頂與破位可以同時成立，若不拆分優先序，做空將永遠被 Regime IV 遮蔽而無法觸發。
4. **Regime III（右側動能態，突破順勢）**：標的現價確認站穩做市商自穩定分界線（Gamma Flip）與當日成交量加權均價（Session VWAP），且上方具備充裕獲利空間（Call Wall 空間 $\ge$ 動態門檻），下方有有效正 Gamma 支撐牆貼身防禦（牆距落在緩衝雙邊界內），同時伴隨 15 分鐘實體陽線與 1.5 倍放量突破，RSI 位於強勢多頭區間（$> 55$）。此狀態路由至「右側動能六重鐵律」。
5. **Regime I（左側接刀態，極端負乖離吸籌）**：標的價格短線遭遇非理性恐慌拋售，現價嚴重偏離當日均價達 $1.5 \times \text{ATR}_{15m}$ 以上，15 分鐘 RSI 進入極度超賣區（$\le 30$），但下方精準密著做市商 Put Wall 底牆（$-1.0\% \sim +1.5\%$ 容差帶）。此狀態下預期做市商被動買盤將提供強力緩衝，路由至「左側均值回歸六重鐵律」——注意左側**仍是做多**。
6. **Regime II（混沌泥淖態，全系統休眠）**：兜底分支。當標的處於無人區過渡震盪，既未展現右側突破動能、亦未到達極端左側做市商底牆、也未構成結構性破位，或任何關鍵量化數據（現價、GEX Profile、15m K 線）缺失時，系統啟用 Fail-Safe 機制，自動休眠觀望，嚴禁盲目交易。

### 資料快照複用設計 (RegimeMarketData Snapshot)
傳統量化架構中，「盤勢分類器」與「進場鐵律檢核」往往各自獨立請求市場數據（如 15m K 線、Session VWAP、ATR）。這不僅造成重複的網路 I/O 延遲，更會因為毫秒級的時間差取得不同的數據快照，引發「分類器判定為 Regime I，但進場檢核時 K 線更新導致條件失效」的邏輯分歧。Nexus Seeker 設計了 `RegimeMarketData` 命名元組，將分類過程中實際取得與計算的數據快照原樣傳遞至後續進場確認管線，確保系統決策建立在不可變的同一時空切片之上。

---

## 2. 數學模型與量化推導

### 2.1 VIX 期限結構倒掛率 (VTS Ratio)
VIX 期限結構是衡量全市場流動性緊縮與恐慌程度的領先指標：
$$
\text{VTS Ratio} = \frac{\text{VIX}}{\text{VIX3M}}
$$
- 當 $\text{VTS Ratio} \ge \text{\_REGIME\_IV\_VTS\_BACKWARDATION\_RATIO} = 1.10$ 時，代表即期市場避險情緒較 3 個月期出現超過 10% 的極端倒掛溢價，標誌著短期流動性極度匱乏與市場結構性斷裂，強制觸發 Regime IV。

### 2.2 帶正負號的阻力牆空間率 (Call Wall Proximity Pct)
阻力空間判定摒棄「Call Wall 必然在現價上方」的假設，採用帶正負號的相對距離公式：
$$
\Delta_{\text{CallWall}} = \frac{\text{CallWall} - \text{Spot}}{\text{Spot}}
$$
比較基準自固定 $5\%$ 升級為**動態自適應波動率門檻**（完整推導見 [`06_dynamic_adaptive_room_threshold.md`](06_dynamic_adaptive_room_threshold.md) 公式 A）：

$$
\text{Threshold}_{\text{dynamic}} = \max\Big(2.2 \times \text{Risk}_{\text{actual}},\ 1.5 \times \frac{\text{ATR}_{1D}}{\text{Spot}},\ 0.035\Big)
$$

- 若 $\Delta_{\text{CallWall}} < \text{Threshold}_{\text{dynamic}}$（含現價已穿過／跌破 Call Wall 導致值為負數的情形），代表上方壓制極近，不具備多頭獲利盈虧比，觸發 Regime IV 個股結構封頂分支。
- 若 $\Delta_{\text{CallWall}} \ge \text{Threshold}_{\text{dynamic}}$，則滿足 Regime III 右側動能的阻力空間要求。
- 路由層（本分類器）與進場確認層（六重鐵律條件三）共用同一條公式，但仍**各自獨立呼叫、各自持有輸入**——`constants.py` 明訂的「兩層門檻不合併」政策針對的是可變旋鈕，不是演算法。

### 2.3 做市商正 Gamma 支撐牆即時防禦距離
支撐位在物理定義上必須位於現價下方，有效防禦距離計算如下：
$$
d_{\text{Support}} = \frac{\text{Spot} - \text{SupportWall}}{\text{Spot}}
$$
約束條件自固定上限 $5\%$ 升級為**停損距離雙邊界**（公式 B，`profile="RIGHT"`）：

$$
2.5 \times \frac{\text{ATR}_{15m}}{\text{Spot}} \le \frac{\text{Spot} - (\text{SupportWall} - 0.5 \times \text{ATR}_{15m})}{\text{Spot}} \le 0.08
$$

- **下界**防「停損落在日內雜訊帶內」而遭做市商 Liquidity Sweep 洗出場。舊版的 $0 < d_{\text{Support}}$ 只要求牆在下方，對此完全無防護。
- **上界為絕對 $8\%$**的風險兜底。刻意不用 ATR 縮放——那會與 Call Wall 空間門檻的 $2.2 \times \text{Risk}$ 重複定價同一風險，且低波標的的可接受帶會窄於一個履約價間距。
- $\text{ATR}_{15m}$ 不可得時自動退回舊版的 $0 < d_{\text{Support}} \le 5\%$ 單邊牆距判定。

### 2.4 15 分鐘放量突破倍數
回看基準採用排除未成型當前根的前 20 根 15 分鐘已收盤 K 棒平均成交量：
$$
\overline{\text{Volume}}_{20} = \frac{1}{20} \sum_{i=1}^{20} \text{Volume}_{t-i}
$$
$$
\text{Surge Ratio} = \frac{\text{Volume}_{t}}{\overline{\text{Volume}}_{20}} \ge \text{\_REGIME\_III\_VOLUME\_SURGE\_MULT} = 1.5
$$
配合實體陽線條件：$\text{Close}_t > \text{Open}_t$。

### 2.5 左側極端負乖離與 Put Wall 密著帶
左側接刀要求現價向下深度偏離 Session VWAP 超過 1.5 倍的 15 分鐘 ATR：
$$
\text{Spot} \le \text{SessionVWAP} - \text{\_REGIME\_I\_VWAP\_ATR\_MULT} \times \text{ATR}_{15m}
$$
其中，做市商 Put Wall 密著容差帶定義為：
$$
d_{\text{Put}} = \frac{\text{Spot} - \text{PutWall}}{\text{Spot}} \in [-0.010, \; +0.015]
$$
即現價允許在 Put Wall 下方 1.0%（微幅跌破洗盤）至上方 1.5% 的極窄區間內。同時疊加 RSI 極端超賣：$\text{RSI}_{14} \le \text{\_REGIME\_I\_RSI\_MAX} = 30.0$。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([開始: 標的動態市場結構分類]) --> CheckData{"資料有效性檢查<br/>Spot > 0 且 GEX 有效?"}
    CheckData -- 否 --> ReturnRegimeII["返回 Regime II: 混沌泥淖態<br/>Fail-Safe 休眠觀望"]
    CheckData -- 是 --> CheckMacro{"Regime IV 宏觀鎖定審查<br/>(在 15m/ATR 抓取之前，可早退)"}

    CheckMacro -- 大盤 SHORT_GAMMA 或 LIQUIDITY_CRISIS --> ReturnRegimeIV["返回 Regime IV: 結構封頂/危機態<br/>全面鎖定 / 嚴禁任何方向進場"]
    CheckMacro -- VTS Ratio >= 1.10 --> ReturnRegimeIV

    CheckMacro -- 全數否 --> Fetch["抓取 15m K 線 + 計算 ATR_15m / ATR_1D<br/>(動態門檻所需)"]
    Fetch --> CheckCap["計算個股結構封頂旗標<br/>Call Wall 空間 < 動態門檻?<br/>或 STO Call 巨鯨物理封頂?"]
    CheckCap --> CheckBars{"15m K 線檢查<br/>已收盤 K 棒 >= 21 根?"}

    CheckBars -- "否，且已結構封頂" --> ReturnRegimeIV
    CheckBars -- "否，且未結構封頂" --> ReturnRegimeII
    CheckBars -- 是 --> CheckRegimeV{"Regime V 破位追空審查<br/>全部條件是否成立?"}

    CheckRegimeV -- "Spot < GammaFlip 且 Spot < VWAP 且 Spot < PutWall<br/>次級負 GEX 節點空間 >= 2.0 x ATR_1D<br/>上方頂牆停損距離落在 [2.5 x ATR_15m, 絕對 8%]<br/>15m 實體陰線且放量 >= 1.5x<br/>15m RSI < 45" --> ReturnRegimeV["返回 Regime V: 破位追空態<br/>路由至: 做空破位追空六重鐵律"]

    CheckRegimeV -- 否 --> CheckCapGate{"個股結構封頂旗標成立?"}
    CheckCapGate -- 是 --> ReturnRegimeIV
    CheckCapGate -- 否 --> CheckRegimeIII{"Regime III 順勢突破審查<br/>全部條件是否成立?"}

    CheckRegimeIII -- "Spot > GammaFlip 且 Spot > VWAP<br/>Call Wall 空間 >= 動態門檻<br/>支撐牆停損距離落在 [2.5 x ATR_15m, 絕對 8%]<br/>15m 實體陽線且放量 >= 1.5x<br/>15m RSI > 55" --> ReturnRegimeIII["返回 Regime III: 右側動能態<br/>路由至: 右側突破六重鐵律"]

    CheckRegimeIII -- 否 --> CheckRegimeI{"Regime I 逆勢接刀審查<br/>全部條件是否成立?"}

    CheckRegimeI -- "Spot <= VWAP - 1.5 * ATR_15m<br/>15m RSI <= 30<br/>Put Wall 密著帶 [-1.0%, +1.5%]" --> ReturnRegimeI["返回 Regime I: 左側接刀態<br/>路由至: 左側均值回歸六重鐵律 (做多)"]

    CheckRegimeI -- 否 --> ReturnRegimeII
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 | 數值 / 門檻 | 物理 / 代碼約束 | 程式碼檔案路徑 |
| :--- | :--- | :--- | :--- |
| `_REGIME_IV_VTS_BACKWARDATION_RATIO` | `1.10` | Front-month VIX 溢價 3-month 超過 10% 視為流動性危機 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ROOM_RISK_MULTIPLIER` / `_ROOM_ATR_1D_MULTIPLIER` / `_ROOM_ABSOLUTE_FLOOR_PCT` | `2.2` / `1.5` / `0.035` | 動態空間門檻三項；取代退役的 `_REGIME_IV_CALL_WALL_PROXIMITY_PCT` 固定 $5\%$ | `nexus_core/market_analysis/room_threshold.py` |
| `_ENTRY_UOA_CAP_RATIO_THRESHOLD` | `1.5` | STO Call 視為物理封頂的單筆 Volume/OI 門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_BUFFER_LOWER_MULTIPLIERS["RIGHT"]` | `2.5` | 停損距離下界倍率；取代退役的 `_REGIME_III_SUPPORT_WALL_MAX_DIST_PCT` 固定 $5\%$ 上限 | `nexus_core/market_analysis/room_threshold.py` |
| `_BUFFER_MAX_STOP_DISTANCE_PCT` | `0.08` ($8\%$) | 停損距離的絕對上限兜底 | `nexus_core/market_analysis/room_threshold.py` |
| `_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT` | `0.05` ($5\%$) | ATR 兩項皆缺時退回的舊版單邊上限（降級路徑） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_III_VOLUME_SURGE_MULT` | `1.5` | 15m K 棒成交量相對於過去 20 根均量的放大倍數 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_III_RSI_MIN` | `55.0` | 右側動能突破 15m RSI 最低多頭門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_I_VWAP_ATR_MULT` | `1.5` | 左側負乖離門檻：$\text{Spot} \le \text{VWAP} - 1.5 \times \text{ATR}_{15m}$ | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_I_RSI_MAX` | `30.0` | 左側接刀 15m RSI 最高超賣門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_I_PUT_WALL_LOWER_PCT` | `-0.01` ($-1.0\%$) | 左側現價距 Put Wall 允許下穿洗盤之下界 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_I_PUT_WALL_UPPER_PCT` | `0.015` ($+1.5\%$) | 左側現價距 Put Wall 允許密著吸附之上界 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_VOLUME_LOOKBACK_BARS` | `20` | 成交量基準回看已收盤 15m K 棒根數 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_V_RSI_MAX` | `45.0` | 破位追空 15m RSI 最高空頭門檻（鏡像推導值，未經回測校準） | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_REGIME_V_VOLUME_SURGE_MULT` | `1.5` | Regime V 的 15m 放量倍數門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER` | `2.0` | Regime V 次級負 GEX 節點空間之最低倍數 | `nexus_core/market_analysis/room_threshold.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

1. **未成型 K 棒雜訊消除 (`trim_to_confirmed_15m_bars`)**：
   盤中當前最後一根 15 分鐘 K 棒仍在跳動，其成交量僅累積部分時間，若直接計算放量倍數會造成放量誤判，或在左側誤判為「縮量窒息」。分類器強制調用截斷函式，嚴格使用最近一根**已收盤**的完整 K 棒。若可用已收盤 K 棒不足 $20 + 1 = 21$ 根，一律回退至 Regime II。
2. **零除防護與負距離語意**：
   在計算 $\Delta_{\text{CallWall}}$ 與 $d_{\text{Support}}$ 時，皆先校驗 $\text{Spot} > 0$。若現價大於 Call Wall，$\Delta_{\text{CallWall}}$ 計算結果為負數，動態門檻必然大於它，直接覆蓋此情境判定空間不足（非誤判為已突破無限空間）。
3. **判定優先序不可任意調換**：Regime V 必須排在 Regime IV 的**個股結構封頂**分支之前，但排在**宏觀鎖定**分支之後。壓頂（Call Wall 空間不足）與破位（跌破 Put Wall）可以同時成立，若把個股結構封頂提前，做空將永遠被遮蔽而無法觸發；反之宏觀鎖定必須壓過 Regime V——系統性流動性危機下做空同樣會被劇烈軋空。
4. **動態門檻的降級是刻意的 fail-open**：15m K 線或 ATR 抓取失敗時，`room_threshold` 自動降級至 $3.5\%$ 絕對底線——比舊版固定 $5\%$ **寬鬆**。這是刻意選擇：資料缺失不應把標的誤鎖進「結構封頂危機態」而連帶封鎖整個動態轉倉引擎。降級狀態會在分類理由字串中以 `⚠️` 明確揭露。
5. **大盤總經 API 異常防禦**：
   呼叫 `get_market_regime()` 或 `get_vix_term_structure()` 若遭遇超時或網路例外，日誌發出警告，大盤狀態預設降級為 `"NORMAL"`，VTS Ratio 預設為 `0.0`，由個股微觀結構條件承擔最終風控，避免因外部 API 抖動癱瘓整個排程。
4. **極端單邊 Gamma 分佈 Fallback**：
   當標的期權分佈極端導致累積 GEX 無零交叉點時（`estimate_symbol_gamma_flip <= 0`），Regime III 的 `Spot > GammaFlip` 判定將無法滿足，系統將自動拒絕 Regime III 並導向後續審查，防止在無法界定 Gamma 翻轉線時進行高風險追漲。

---

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/dynamic_rollover/regime_classifier.py`：公開入口 `classify_dynamic_regime()`（分類後寫入前向蒐集紀錄）與分類主體 `_classify_dynamic_regime_impl()`；`RegimeMarketData.rsi_15m` 供做空凱利先驗沿用
- `nexus_core/market_analysis/evaluation_recorder.py`：Regime 分類與進場鐵律評估的前向蒐集（見 [`05_calibration_harness_and_forward_collection.md`](../architecture/05_calibration_harness_and_forward_collection.md)）
- `nexus_core/market_analysis/dynamic_rollover/models.py`：枚舉 `DynamicRegime`, `TradingStrategyMode` 及資料載體 `RegimeMarketData`
- `nexus_core/market_analysis/dynamic_rollover/constants.py`：所有門檻常數定義與物理約束
- `nexus_core/market_analysis/dynamic_rollover/structural_signals.py`：正負 Gamma 牆體掃描 `_scan_gex_walls()`
- `nexus_core/market_analysis/index_microstructure.py`：`detect_uoa_sto_call_physical_cap()`, `estimate_symbol_gamma_flip()`, `get_market_regime()`
- `nexus_core/market_analysis/vwap_utils.py`：日內加權均價抓取 `fetch_session_vwap()`
- `nexus_core/market_analysis/atr_utils.py`：`compute_atr_15m_from_df()`
- `nexus_core/market_analysis/price_volume_alert.py`：`trim_to_confirmed_15m_bars()`
