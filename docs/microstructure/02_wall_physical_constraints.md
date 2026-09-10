# 做市商底牆現價物理約束定理技術規格書

## 1. 核心哲學與適用市場環境

在量化交易與做市商微觀結構分析中，「支撐」與「阻力」具有不可違背的物理空間定義：
- **支撐底牆（Support Wall）**：在物理定義上必然位於當前標的價格的**下方**（$K < \text{Spot}$），代表當價格下跌觸及該處時，做市商的被動買盤與多頭籌碼將對下行形成物理阻尼。
- **阻力天花板（Resistance / Call Wall）**：在物理定義上必然位於當前標的價格的**上方**（$K > \text{Spot}$），代表價格上漲將遭遇做市商拋售對沖或獲利了結壓制。

### 歷史系統性缺陷與架構重構
在早期期權分析演算法中，普遍存在一個嚴重的邏輯漏洞：直接以「全鏈期權最大正 GEX 峰值」作為支撐牆。當個股上方存在極其龐大的價外 Call Wall（例如大量散戶買入 OTM Call 導致做市商在上方聚集巨大正 Gamma 峰值）時，傳統演算法會將這個遠在現價「上方」的阻力峰值錯誤識別為「支撐底牆」。

這種「指天為地」的致命缺陷會引發災難性後果：
1. **進場檢核誤殺或盲信**：策略誤以為現價「站在巨大支撐之上」，甚至在現價早已跌破真正支撐時，仍因上方虛假支撐而給出安全信號。
2. **停損計算紊亂**：以現價上方的價位減去 ATR 計算停損，導致算出的「停損價位竟高於現價」，引發交易邏輯崩潰。

Nexus Seeker 在 `_scan_gex_walls` 與 `structural_signals.py` 中正式確立並實作了**做市商底牆現價物理約束定理**，在數學層面上強制將支撐牆候選範圍約束在現價下方，徹底消除了此類誤判。

---

## 2. 數學模型與量化推導

### 2.1 底牆現價物理約束定理 (The Physical Constraint Theorem)
給定期權鏈履約價集合 $\mathcal{K} = \{K_1, K_2, \dots, K_N\}$ 及其對應的淨 GEX 曝險函數 $\text{GEX}(K)$。當前標的現貨價格為 $\text{Spot} > 0$。

定義合格支撐履約價子集 $\Omega_{\text{Support}}$ 必須嚴格滿足物理空間約束：
$$
\Omega_{\text{Support}} = \{K \in \mathcal{K} \mid K < \text{Spot} \land \text{GEX}(K) > 0\}
$$

支撐底牆的確定依據以下兩階段判定：
1. **極值檢索**：
   若 $\Omega_{\text{Support}} \neq \emptyset$，則最大正 Gamma 支撐候選履約價 $K^*$ 為：
   $$
   K^* = \operatorname{argmax}_{K \in \Omega_{\text{Support}}} \big(\text{GEX}(K)\big)
   $$
2. **深度有效性驗證**：
   $$
   \text{Support Wall} =
   \begin{cases}
   K^*, & \text{若 } \Omega_{\text{Support}} \neq \emptyset \land \text{GEX}(K^*) \ge \text{GEX\_THIN\_WALL\_THRESHOLD} \\
   0.0, & \text{若 } \Omega_{\text{Support}} = \emptyset \lor \text{GEX}(K^*) < \text{GEX\_THIN\_WALL\_THRESHOLD}
   \end{cases}
   $$
   其中 $\text{GEX\_THIN\_WALL\_THRESHOLD} = 500,000$ 美元。

**推論**：若標的價格持續下殺，跌破所有現價下方的正 GEX 峰值，則 $\Omega_{\text{Support}} = \emptyset$。此時系統強制返回 $\text{Support Wall} = 0.0$，代表下方無任何有效做市商防護底牆，絕不向現價上方借取 Call Wall 作為替代。

### 2.2 阻力牆帶正負號距離判定
傳統邏輯往往默認 Call Wall 必然在現價上方。當價格突破或貫穿 Call Wall 時，若僅計算絕對值距離，會誤判為「阻力空間無限大」。

系統採用帶正負號的相對距離公式：
$$
\Delta_{\text{CallWall}} = \frac{\text{CallWall} - \text{Spot}}{\text{Spot}}
$$
- 當 $\Delta_{\text{CallWall}} < \text{\_ENTRY\_ASYMMETRIC\_ROOM\_PCT} = 0.05$（$5\%$）時：
  - 若 $\Delta_{\text{CallWall}} \in [0, 0.05)$：上方空間過窄，盈虧比不足。
  - 若 $\Delta_{\text{CallWall}} \le 0$：現價已觸及或跌破 Call Wall（即現價 $\ge$ Call Wall），做市商阻尼與滯留拋售壓制依然存在，此負值明確表示「空間耗竭且壓制沉重」，判定空間不足拒絕進場。

### 2.3 拓撲逆轉修復公式 (Topology Inversion Correction)
在異常期權結構或資料源偶發翻轉時，若偵測到 $\text{Put Wall} > \text{Call Wall}$，系統啟動拓撲逆轉修復：
$$
\text{Anchor Base} = \min(\text{Put Wall}, \; \text{Call Wall})
$$
$$
\text{Effective Resistance Wall} = \max(\text{Put Wall}, \; \text{Call Wall})
$$
確保較低者歸位為防守底牆，較高者歸位為天花板。

---

## 3. 決策邏輯與狀態機 / 流程圖

```mermaid
flowchart TD
    Start([呼叫 _scan_gex_walls 進行牆體掃描]) --> InputCheck{現價 Spot > 0?}

    InputCheck -- 否 --> Unconstrained[退回不設限的全鏈掃描]
    InputCheck -- 是 --> FilterSupport[強制物理約束過濾:<br/>選取所有 Strike < Spot 且 GEX > 0 之履約價]

    FilterSupport --> CheckEmpty{合格集合是否為空?}
    CheckEmpty -- 是 (下方無正 GEX 峰值) --> WallZero[Support Wall = 0.0<br/>Support GEX = 0.0]

    CheckEmpty -- 否 --> FindArgMax[找出下方最大正 GEX 履約價 Strike*]
    FindArgMax --> CheckThreshold{GEX Strike* >= 500,000?}

    CheckThreshold -- 否 (屬於薄紙牆) --> WallZero
    CheckThreshold -- 是 --> WallValid[Support Wall = Strike*<br/>Support GEX = GEX Strike*]

    WallZero --> Out([輸出: 正 Gamma 支撐底牆判定完畢])
    WallValid --> Out
```

---

## 4. 關鍵具名常數與物理約束

| 常數名稱 | 數值 / 門檻 | 物理 / 代碼約束 | 程式碼檔案路徑 |
| :--- | :--- | :--- | :--- |
| `GEX_THIN_WALL_THRESHOLD` | `500,000.0` | 支撐底牆有效性之最低 GEX 深度門檻 | `nexus_core/market_analysis/index_microstructure.py` |
| `_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT` | `0.05` ($5\%$) | 現價距離約束支撐牆之最大即時防禦有效距離 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |
| `_ENTRY_ASYMMETRIC_ROOM_PCT` | `0.05` ($5\%$) | 帶正負號 Call Wall 空間率最低門檻 | `nexus_core/market_analysis/dynamic_rollover/constants.py` |

---

## 5. 邊界條件、風控熔斷與例外處理

1. **現價缺失或無效時的回退機制**：
   若呼叫端傳入 $\text{Spot} \le 0$（例如極早期尚未取得最新報價），代碼自動退回不設限的全鏈掃描，以維持舊有組件的向後相容性。但只要在實時交易與進場檢核路徑中，現價皆保證大於零，強制啟用 $K < \text{Spot}$ 約束。
2. **多個相同正 GEX 峰值之唯一性選取**：
   若現價下方存在多個相同數值的正 GEX 峰值，Python 內建的列表推導與 `max` 函式將按排序穩定返回，確保演算法執行的確定性（Deterministic）。
3. **Call Wall 穿透後的狀態標記**：
   當現價突破 Call Wall 時，系統不會將阻力標記為「無阻力」，而是透過 $\Delta_{\text{CallWall}} \le 0$ 觸發 TP2 獲利了結，同時在進場端關閉買進權限，防範高位滯留風險。

---

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/dynamic_rollover/structural_signals.py`：
  - 核心實作函式：`_scan_gex_walls()`（第 67–151 行）
  - 錨點解析與拓撲修復：`_resolve_canonical_anchor_base()`（第 38–65 行）
- `nexus_core/market_analysis/dynamic_rollover/opportunity_cost.py`：
  - 條件二約束檢核：`_confirm_entry_condition2_support_wall()`（第 257–303 行）
  - 條件三帶符號空間檢核：`_confirm_entry_condition3_no_physical_cap()`（第 305–349 行）
- `nexus_core/market_analysis/index_microstructure.py`：`classify_gex_wall()`, `GEX_THIN_WALL_THRESHOLD`
- `nexus_core/market_analysis/dynamic_rollover/anti_washout.py`：`_correct_wall_topology()`
