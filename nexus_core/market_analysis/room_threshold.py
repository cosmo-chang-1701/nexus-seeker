"""room_threshold.py — 動態自適應波動率空間門檻的單一權威實作。

本模組取代先前散落在 7 處、彼此獨立且會各自漂移的固定百分比空間門檻
（4 個具名常數 0.05×3 / 0.035×1，加上 3 處硬編碼 ``spot * 1.05`` 與 ``5.0``）。

固定百分比的根本缺陷是「與標的自身波動率無關」：對 ATR 僅 0.8% 的 KO，5% 是
遙不可及的天花板；對 ATR 4% 的 TSLA，5% 連一根日線都吃不掉，等於沒有盈虧比
保護。本模組改以「該標的實際下行風險 × 盈虧比要求」與「該標的單日波幅」共同
推導門檻，讓 2.2:1 盈虧比成為結構性保證而非巧合。

設計約束（刻意維持）：

* **只依賴 stdlib**。比照 ``market_analysis/sentiment/skew_taxonomy.py`` 的葉
  模組設計，本檔案不得 import ``dynamic_rollover`` / ``cogs`` / ``services``
  任一者，才能同時被 ``dynamic_rollover/``、``gamma_squeeze_engine.py`` 與
  ``cogs/embed_builders/`` 匯入而不產生循環相依。
* **共用的是演算法，不是常數值**。各呼叫站點仍各自獨立呼叫、各自持有自己的
  資料來源與降級行為，``dynamic_rollover/constants.py`` 明訂的「路由層
  (Regime 分類) 與進場確認層 (六重鐵律) 的門檻常數刻意分開、不合併重用」政策
  不因本模組而被破壞——兩層共用的是同一條公式，不是同一個可變旋鈕。
* **任何資料缺失一律降級，絕不拋例外**。呼叫端全部是盤中熱路徑，fail-safe
  語意比照 ``atr_utils.fetch_atr_15m()``（例外一律回退，不中斷流程）。
"""

import math
from typing import Literal, NamedTuple, Optional

# 方向：決定 Stop 由哪一道牆、往哪一個方向推導。
#   LONG  —— 停損在支撐牆 (Put Wall) 下方，獎酬看上方 Call Wall
#   SHORT —— 停損在阻力牆 (Call Wall) 上方，獎酬看下方 Put Wall
Direction = Literal["LONG", "SHORT"]

# 牆體緩衝雙邊界的 Regime 分流剖面。
#   RIGHT —— Regime III 右側動能態 / trading_strategy=RIGHT_SIDE
#   LEFT  —— Regime I  左側接刀態 / trading_strategy=LEFT_SIDE
#   SHORT —— Regime V  破位追空態 / trading_strategy=SHORT_SIDE
BufferProfile = Literal["RIGHT", "LEFT", "SHORT"]

# --- 公式 A：方向性空間門檻具名常數 ---
# Threshold = max(2.2 × Risk_actual, 1.5 × ATR_1D_pct, 0.035)
_ROOM_RISK_MULTIPLIER: float = 2.2  # 盈虧比要求：獎酬須達實際風險的 2.2 倍
_ROOM_ATR_1D_MULTIPLIER: float = 1.5  # 空間至少須涵蓋 1.5 個單日波幅
_ROOM_ABSOLUTE_FLOOR_PCT: float = 0.035  # 絕對底線，任何情況下都不低於 3.5%
_ROOM_STOP_ATR_15M_MULTIPLIER: float = 0.5  # Stop = Wall ∓ 0.5 × ATR₁₅ₘ
# 此值刻意與 anti_washout.py 軌道一的 _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT
# 完全一致——Risk_actual 量的就是「現價到引擎真正會執行的那一條停損」的距離。
#
# 早期版本依文獻規格使用 1.5×，與引擎實際執行的 0.5× 不符，使 Risk_actual
# 系統性高估真實風險、門檻連帶偏嚴。實測（252 點參數掃描）顯示該分歧單獨
# 造成「應保留進場格點」的保留率自 100% 掉到 92.5%，且讓 2.2:1 從精確值退化
# 成無法驗證的保守下界。改為對齊後，2.2:1 是可直接驗證的實際盈虧比。
#
# ⚠️ 修改本值前請先確認 _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT 未一併變動，
#   兩者必須同步，否則門檻會再次與真實停損脫鉤。
_ROOM_STOP_FALLBACK_ATR_15M_MULTIPLIER: float = 2.0  # 牆體拓撲異常時的替代墊片
# ↑ 沿用 cogs/embed_builders/portfolio_embeds.py 既有的「PutWall 異常降級：改用
#   現價 - 2×ATR_15m」慣例，不另立第二套降級邏輯。

# --- 公式 B：牆體緩衝雙邊界具名常數 ---
# ΔS_stop = |Spot − Stop| / Spot，其中 Stop 與公式 A 用的是**同一條**停損
# (Wall ∓ 0.5 × ATR₁₅ₘ)。三種剖面全部量停損距離，不再區分「量牆距」與
# 「量停損距離」——語意統一後，這道閘門回答的問題就只有一個：
#     「引擎真正會掛的那條停損，離進場價夠不夠遠、又不會遠得離譜？」
#
# 下界 (依剖面分流，×ATR₁₅ₘ)：防停損落在日內隨機雜訊帶內而遭做市商
#   Liquidity Sweep 掃損。這是條件三的 2.2×Risk **完全沒有**定價的風險，
#   故必須獨立成閘門。
_BUFFER_LOWER_MULTIPLIERS: dict[str, float] = {
    # Regime III 右側動能態 / Regime V 破位追空態：基準值
    "RIGHT": 2.5,
    "SHORT": 2.5,
    # Regime I 左側接刀態：貼牆截擊本質上緩衝極薄，下界須放寬，否則會與既有的
    # Put Wall 密著帶 [-1.0%, +1.5%] (_LEFT_ENTRY_PUT_WALL_*_PCT) 互斥。
    #
    # 為何恰好是 0.5 而不能更高：左側的停損是 PutWall − 0.5 × ATR₁₅ₘ，因此
    # 「現價正好貼齊 Put Wall」(ΔS_wall = 0) 這個教科書級理想進場點的停損距離
    # **恆等於** 0.5 × ATR₁₅ₘ。下界只要 > 0.5，就會把該策略的設計中心點本身
    # 判為過窄——那不是過濾風險，是刪掉策略。設為 0.5 之後，這道閘門的語意
    # 收斂成一句可驗證的話：「允許貼牆，但不允許現價已經跌到停損線附近」，
    # 仍能正確擋下現價穿刺 Put Wall、停損就在腳邊的情境。
    #
    # ⚠️ 本值與 _ROOM_STOP_ATR_15M_MULTIPLIER (0.5) 綁定。早期版本是 1.0，
    #   當時的停損墊片是 1.5×，貼牆時停損距離 1.5 × ATR₁₅ₘ 仍過得了 1.0 的
    #   下界；墊片改對齊引擎的 0.5× 之後，1.0 就成了結構上不可能滿足的門檻。
    "LEFT": 0.5,
}

# 上界 (絕對值，不分剖面)：純粹的「絕對風險上限」兜底，擋掉停損距現價 8% 以上
# 的荒謬配置。
#
# ⚠️ 早期版本用 1.8 × ATR₁D 這個**隨波動率伸縮**的上界，實測證明是錯的：
#   * 它與條件三的 2.2 × Risk 在防同一件事（牆太遠 = 風險太大），而條件三
#     已經用「要求等比例更多的上行空間」對該風險做了**連續**定價；ATR 上界
#     等於同一個風險收第二次費，而且第二次是**二元否決**。
#   * 更致命的是量綱：ATR₁D = 0.8% 的低波標的，可接受帶寬只有 1.05%，而
#     $100 標的的履約價間距是 $2.50 (2.5%)。**帶寬窄於一個履約價間距**時，
#     GEX 牆能否落進帶內純屬運氣——那不是風控，是抽籤。
#   252 點參數掃描：改為絕對上界後，「應保留進場格點」的保留率自 66.2%
#   回升至 92.5%（與下方 0.5× 停損對齊後達 100%），最差盈虧比維持 2.2:1。
_BUFFER_MAX_STOP_DISTANCE_PCT: float = 0.08

# 資料不足以計算下界時，降級回本模組出現前的既有單邊判定 0 < ΔS <= 5%
# (_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT / _REGIME_III_SUPPORT_WALL_MAX_DIST_PCT)。
_LEGACY_BUFFER_MAX_PCT: float = 0.05

# --- 公式 C：破位追空次級節點空間具名常數 ---
_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER: float = 2.0

# --- 公式 D：晴空萬里有效目標天花板具名常數 ---
# EffTarget = max(CallWall, High60d, Spot + κ×ATR₁D)，若 Spot 貼近或突破 60 日高點
_BLUE_SKY_ATR_MULTIPLIER: float = 3.0
_BLUE_SKY_PROXIMITY_PCT: float = 0.02  # 距 60 日高點 2% 內即視為晴空萬里

# --- ATR 量綱折算 ---
# 美股單日 390 分鐘 = 26 根 15 分鐘 K 棒，依隨機遊走平方根法則折算。
# 沿用 signal_calculator.py 既有的 math.sqrt(26.0) 寫法，刻意不使用
# intraday_pipeline/evaluation.py 那個寫死的 5.099 字面值（同一常數兩種拼法）。
_BARS_PER_SESSION: float = 26.0
# EnhancedWatchlistMetrics.atr_14 的欄位約束是 gt=0.0，因此「日線 ATR 取不到」
# 時寫入的是 0.01 佔位值而非 0（見 models/schemas.py 與 intraday_pipeline/
# metrics.py 的 fallback 分支）。判定有效性時必須排除這個佔位值，否則會得到
# 一個 $0.015 的假緩衝。
_ATR_14_PLACEHOLDER: float = 0.01


class RoomThreshold(NamedTuple):
    """``compute_dynamic_room_threshold()`` 的結構化輸出。

    ``degrade_reason`` 為可直接輸出的繁體中文字串，供展示層（GEX 空間欄位）與
    六重鐵律的 ``reasons`` 文案原樣引用，避免各呼叫端各自拼裝而彼此分歧。
    """

    threshold_pct: float
    risk_actual_pct: Optional[float]
    atr_1d_pct: Optional[float]
    binding_term: str  # "RISK" | "ATR_1D" | "FLOOR"
    is_degraded: bool
    degrade_reason: Optional[str]


class BufferEvaluation(NamedTuple):
    """``evaluate_wall_buffer()`` 的結構化輸出。

    ``buffer_pct`` 量的是**停損距離**（現價到 Wall ∓ 0.5×ATR₁₅ₘ），不是牆距。

    ``state`` 三態：
      TOO_TIGHT   —— 停損落在日內雜訊帶內，易遭 Liquidity Sweep 掃損
      SWEET_SPOT  —— 落在雙邊界之內，進場甜蜜點
      TOO_WIDE    —— 停損距現價超過絕對風險上限 (_BUFFER_MAX_STOP_DISTANCE_PCT)
    """

    buffer_pct: float
    min_pct: Optional[float]
    max_pct: Optional[float]
    state: str
    is_degraded: bool
    degrade_reason: Optional[str]

    @property
    def passed(self) -> bool:
        return self.state == "SWEET_SPOT"


def _is_valid(value: float) -> bool:
    """有限且為正才視為有效輸入（同時擋掉 NaN / Inf / 0 / 負值）。"""
    return math.isfinite(value) and value > 0.0


def resolve_atr_15m(atr_15m: float, atr_14: float) -> float:
    """解析可用的 15m ATR，真實值優先、日線折算次之，皆無則回 0.0。

    階梯完全比照 ``signal_calculator.py::calculate_dynamic_trading_signals`` 既有
    的 4 段式 ATR 解析：真實 15m K 棒 ATR > 由日線 ATR(14) 依 √26 折算 > 放棄。

    ``atr_14`` 恰好等於 ``_ATR_14_PLACEHOLDER`` (0.01) 時視為無效——那是
    ``EnhancedWatchlistMetrics.atr_14`` 因 ``gt=0.0`` 約束而無法寫 0 時的佔位值，
    直接採用會得到 $0.015 的假緩衝。
    """
    if _is_valid(atr_15m):
        return atr_15m
    if _is_valid(atr_14) and not math.isclose(atr_14, _ATR_14_PLACEHOLDER):
        return atr_14 / math.sqrt(_BARS_PER_SESSION)
    return 0.0


def compute_reference_stop(
    spot: float, stop_wall: float, atr_15m: float, direction: Direction
) -> float:
    """推導名目參考停損價。

    LONG  : StopWall - 0.5 × ATR₁₅ₘ；若結果 >= 現價（牆體資料異常高於現價，
            即牆體拓撲逆轉），改用 現價 - 2.0 × ATR₁₅ₘ。
    SHORT : StopWall + 0.5 × ATR₁₅ₘ；若結果 <= 現價（牆體異常低於現價），
            改用 現價 + 2.0 × ATR₁₅ₘ。

    墊片倍數與出場矩陣 `_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT` 同步。公開此
    函式是為了讓 SHORT_ENTRY 倉位計算 (short_entry_sizing.py) 與進場鐵律條件
    二／三使用同一個停損定義，不另寫一份。
    """
    if direction == "LONG":
        stop = stop_wall - _ROOM_STOP_ATR_15M_MULTIPLIER * atr_15m
        if stop >= spot:
            stop = spot - _ROOM_STOP_FALLBACK_ATR_15M_MULTIPLIER * atr_15m
        return stop
    stop = stop_wall + _ROOM_STOP_ATR_15M_MULTIPLIER * atr_15m
    if stop <= spot:
        stop = spot + _ROOM_STOP_FALLBACK_ATR_15M_MULTIPLIER * atr_15m
    return stop


# 保留私有別名：既有測試與模組內部呼叫點沿用。
_compute_reference_stop = compute_reference_stop


def compute_dynamic_room_threshold(
    spot: float,
    stop_wall: float,
    atr_15m: float,
    atr_1d: float,
    direction: Direction = "LONG",
) -> RoomThreshold:
    """計算動態自適應波動率空間門檻（公式 A）。

    :param spot: 現價。
    :param stop_wall: 停損所依託的牆體——LONG 傳 Put Wall，SHORT 傳 Call Wall。
        注意這**不是**獎酬參考位（LONG 的獎酬看 Call Wall、SHORT 看 Put Wall），
        獎酬距離由呼叫端自行計算後與本函式回傳的門檻比較。
    :param atr_15m: 真實 15 分鐘 K 棒 ATR(14)，缺失請先經 ``resolve_atr_15m()``。
    :param atr_1d: 日線 ATR(14)。
    :param direction: "LONG" 或 "SHORT"。

    降級規則：任一輸入缺失，該項即自 ``max()`` 中剔除，其餘項與 3.5% 絕對底線
    照常參與；三項皆不可得時門檻即為 3.5%。任何降級都會標記 ``is_degraded``
    並附上可直接呈現的繁中原因，呼叫端有義務向使用者揭露。
    """
    wall_label = "PutWall" if direction == "LONG" else "CallWall"

    if not _is_valid(spot):
        return RoomThreshold(
            threshold_pct=_ROOM_ABSOLUTE_FLOOR_PCT,
            risk_actual_pct=None,
            atr_1d_pct=None,
            binding_term="FLOOR",
            is_degraded=True,
            degrade_reason=(
                f"數據缺失（現價），已退回 {_ROOM_ABSOLUTE_FLOOR_PCT:.1%} 絕對底線"
            ),
        )

    missing: list[str] = []

    # 第一項：2.2 × 實際下行（上行）風險
    risk_actual_pct: Optional[float] = None
    if _is_valid(stop_wall) and _is_valid(atr_15m):
        stop = _compute_reference_stop(spot, stop_wall, atr_15m, direction)
        raw_risk = (spot - stop) / spot if direction == "LONG" else (stop - spot) / spot
        # 夾為非負：停損落在進場價的錯誤一側時數學上退化，該項不應反向壓低門檻。
        risk_actual_pct = max(raw_risk, 0.0)
    else:
        if not _is_valid(stop_wall):
            missing.append(wall_label)
        if not _is_valid(atr_15m):
            missing.append("ATR₁₅ₘ")

    # 第二項：1.5 × 單日波幅佔比
    atr_1d_pct: Optional[float] = atr_1d / spot if _is_valid(atr_1d) else None
    if atr_1d_pct is None:
        missing.append("ATR₁D")

    term_risk = (
        _ROOM_RISK_MULTIPLIER * risk_actual_pct if risk_actual_pct is not None else None
    )
    term_atr = _ROOM_ATR_1D_MULTIPLIER * atr_1d_pct if atr_1d_pct is not None else None

    threshold = _ROOM_ABSOLUTE_FLOOR_PCT
    binding_term = "FLOOR"
    if term_risk is not None and term_risk > threshold:
        threshold = term_risk
        binding_term = "RISK"
    if term_atr is not None and term_atr > threshold:
        threshold = term_atr
        binding_term = "ATR_1D"

    degrade_reason: Optional[str] = None
    if missing:
        joined = "、".join(missing)
        if binding_term == "FLOOR":
            degrade_reason = (
                f"數據缺失（{joined}），已退回 "
                f"{_ROOM_ABSOLUTE_FLOOR_PCT:.1%} 絕對底線"
            )
        else:
            term_label = "風險盈虧比" if binding_term == "RISK" else "單日波幅"
            degrade_reason = f"數據缺失（{joined}），門檻改由{term_label}項決定"

    return RoomThreshold(
        threshold_pct=threshold,
        risk_actual_pct=risk_actual_pct,
        atr_1d_pct=atr_1d_pct,
        binding_term=binding_term,
        is_degraded=bool(missing),
        degrade_reason=degrade_reason,
    )


def evaluate_wall_buffer(
    spot: float,
    wall_price: float,
    atr_15m: float,
    atr_1d: float,
    profile: BufferProfile = "RIGHT",
) -> BufferEvaluation:
    """評估「引擎真正會掛的那條停損」與進場價的距離是否合理（公式 B）。

    :param wall_price: RIGHT/LEFT 傳現價下方的支撐牆；SHORT 傳現價上方的阻力牆。
    :param profile: Regime 分流剖面，決定下界倍率（見 ``_BUFFER_LOWER_MULTIPLIERS``）。

    三種剖面全部量**停損距離**（不是牆距）：

        ΔS_stop = |Spot − Stop| / Spot,  Stop = Wall ∓ 0.5 × ATR₁₅ₘ

    與公式 A 的 ``Risk_actual`` 是同一條停損、同一個推導（含牆體拓撲異常時的
    替代墊片）。量停損距離而非牆距的理由：

    * 這道閘門要防的是「停損被日內雜訊掃掉」，那取決於**停損**離進場價多遠，
      不是牆離進場價多遠。
    * 左側條件二本來就要求現價密著 Put Wall，牆距在設計上趨近於零；若量牆距，
      下界會把「現價正好貼齊 Put Wall」這個理想進場點判為過窄。

    ``atr_1d`` 已不參與判定（上界改為絕對值），保留於簽章僅為呼叫端相容性與
    未來擴充；傳 0.0 不影響結果。
    """
    if not _is_valid(spot) or not _is_valid(wall_price):
        return BufferEvaluation(
            buffer_pct=0.0,
            min_pct=None,
            max_pct=_BUFFER_MAX_STOP_DISTANCE_PCT,
            state="TOO_TIGHT",
            is_degraded=True,
            degrade_reason="數據缺失（現價或牆體價位），無法評估緩衝距離",
        )

    direction: Direction = "SHORT" if profile == "SHORT" else "LONG"

    if _is_valid(atr_15m):
        stop = _compute_reference_stop(spot, wall_price, atr_15m, direction)
        buffer_pct = (
            (stop - spot) / spot if direction == "SHORT" else (spot - stop) / spot
        )
        min_pct: Optional[float] = _BUFFER_LOWER_MULTIPLIERS[profile] * atr_15m / spot
    else:
        # ATR₁₅ₘ 缺失時無從推導停損，退回量牆距並走降級判定。
        buffer_pct = (
            (wall_price - spot) / spot
            if direction == "SHORT"
            else (spot - wall_price) / spot
        )
        min_pct = None

    max_pct = _BUFFER_MAX_STOP_DISTANCE_PCT

    if min_pct is None:
        # 下界不可得 → 退回本模組出現前的既有單邊判定，行為與舊版一致。
        if buffer_pct <= 0:
            state = "TOO_TIGHT"
        elif buffer_pct > _LEGACY_BUFFER_MAX_PCT:
            state = "TOO_WIDE"
        else:
            state = "SWEET_SPOT"
        return BufferEvaluation(
            buffer_pct=buffer_pct,
            min_pct=None,
            max_pct=_LEGACY_BUFFER_MAX_PCT,
            state=state,
            is_degraded=True,
            degrade_reason=(
                f"數據缺失（ATR₁₅ₘ），已退回 "
                f"{_LEGACY_BUFFER_MAX_PCT:.0%} 固定上限判定"
            ),
        )

    if buffer_pct < min_pct:
        state = "TOO_TIGHT"
    elif buffer_pct > max_pct:
        state = "TOO_WIDE"
    else:
        state = "SWEET_SPOT"

    return BufferEvaluation(
        buffer_pct=buffer_pct,
        min_pct=min_pct,
        max_pct=max_pct,
        state=state,
        is_degraded=False,
        degrade_reason=None,
    )


class EffectiveTarget(NamedTuple):
    """``resolve_effective_target()`` 的結構化輸出（公式 D：晴空萬里天花板擴展）。

    標的創新高時，上方沒有存量 OI 可形成有效 Call Wall，此時 Call Wall
    反映的是「流動性真空」而非「真實阻力」；``target`` 在該情境下改以 60 日
    高點與 ATR 外推目標取代裸 Call Wall。``is_blue_sky is False`` 時
    ``target == call_wall``（傳入值原樣，缺失時為 0.0），即現行行為不變。
    """

    target: float
    is_blue_sky: bool
    is_degraded: bool
    degrade_reason: Optional[str]


def resolve_effective_target(
    spot: float,
    call_wall: float,
    high_60d: float,
    atr_1d: float,
) -> EffectiveTarget:
    """解析有效目標天花板（公式 D：晴空萬里天花板擴展）。

    :param spot: 現價。
    :param call_wall: 裸 Call Wall（既有天花板），資料缺失請傳 0.0。
    :param high_60d: 前 60 個交易日最高價，須先經 shift(1) 防前視（見
        ``atr_utils.fetch_high_60d()`` / ``compute_high_60d_from_daily_df()``）。
    :param atr_1d: 日線 ATR(14)。

    演算法：

        EffTarget = max(CallWall, High60d, Spot + 3.0 × ATR₁D)
                    若 Spot >= High60d × (1 − 2%)；否則 EffTarget = CallWall

    物理意義：標的進入歷史新高區間時，上方沒有存量 OI 可形成有效 Call Wall，
    此時 Call Wall 反映的是「流動性真空」而非「真實阻力」。以 ATR 外推的動態
    目標取代之，才能解鎖趨勢追價權限。

    刻意放在本模組而非 ``dynamic_rollover/``：Regime IV 封頂判定、六重鐵律
    條件三（非對稱空間）、``PYRAMID_ADD`` 條件四三處都要用同一個天花板定義，
    分散實作必然漂移——這正是本模組當初把 7 處固定百分比收斂成單一權威演算法
    的同一個理由。

    降級規則（``high_60d``／``atr_1d`` 缺失一律傳 0.0 或非正值）：

    * 兩者皆缺失 → 退回裸 Call Wall（即現行行為），標記 ``is_degraded``。
    * 僅其中一項缺失 → 自 ``max()`` 中剔除該項，其餘項照常參與；且此時
      **觸發判定本身也視為成立**（``high_60d`` 缺失時無從驗證是否貼近前高）。
      這是刻意的 fail-open：``regime_classifier.py`` 對 ATR 缺失已有相同慣例
      ——「資料缺失不應把標的誤鎖進結構封頂危機態」，寧可多算一次擴展，也不要
      因單純的抓取失敗就把可能正在創新高的標的誤判為封頂。
    """
    if not _is_valid(spot):
        return EffectiveTarget(
            target=0.0,
            is_blue_sky=False,
            is_degraded=True,
            degrade_reason="數據缺失（現價），無法解析有效目標天花板",
        )

    call_wall_val = call_wall if _is_valid(call_wall) else 0.0

    if not _is_valid(high_60d) and not _is_valid(atr_1d):
        return EffectiveTarget(
            target=call_wall_val,
            is_blue_sky=False,
            is_degraded=True,
            degrade_reason="數據缺失（60 日高點、ATR₁D），已退回裸 Call Wall（現行行為）",
        )

    # high_60d 缺失時，觸發判定 fail-open（見上方 docstring）；有效時才做真正
    # 的貼近前高判定。
    is_near_high = (not _is_valid(high_60d)) or spot >= high_60d * (
        1.0 - _BLUE_SKY_PROXIMITY_PCT
    )
    if not is_near_high:
        return EffectiveTarget(
            target=call_wall_val,
            is_blue_sky=False,
            is_degraded=False,
            degrade_reason=None,
        )

    missing: list[str] = []
    candidates: list[float] = [call_wall_val] if call_wall_val > 0 else []
    if _is_valid(high_60d):
        candidates.append(high_60d)
    else:
        missing.append("60 日高點")
    if _is_valid(atr_1d):
        candidates.append(spot + _BLUE_SKY_ATR_MULTIPLIER * atr_1d)
    else:
        missing.append("ATR₁D")

    if not candidates:
        return EffectiveTarget(
            target=0.0,
            is_blue_sky=False,
            is_degraded=True,
            degrade_reason=f"數據缺失（{'、'.join(missing)}），已退回裸 Call Wall（亦缺失）",
        )

    degrade_reason = None
    if missing:
        degrade_reason = f"數據缺失（{'、'.join(missing)}），晴空萬里天花板以剩餘項推導"

    return EffectiveTarget(
        target=max(candidates),
        is_blue_sky=True,
        is_degraded=bool(missing),
        degrade_reason=degrade_reason,
    )


def evaluate_next_strike_space(
    spot: float, next_peak: float, atr_1d: float
) -> tuple[bool, float, Optional[float]]:
    """破位追空的次級節點空間判定（公式 C）。

    NextStrikeSpace = (Spot − NextPutPeak) / Spot >= 2.0 × ATR₁D / Spot

    ``next_peak`` 為現價下方第一個顯著負 GEX 節點（做市商順勢助跌拋壓的落點）。
    本公式只判定「目標空間夠不夠」，不含停損項——「收復 Put Wall」是論點失效
    訊號，實際倉位停損由 SHORT_ENTRY 情境以頂牆錨點另行計算。

    回傳 ``(是否通過, 實際空間佔比, 要求門檻佔比)``。資料缺失時 fail-safe 回傳
    ``(False, 0.0, None)``——追空是進攻動作，無法確認空間一律不進場。
    """
    if not _is_valid(spot) or not _is_valid(next_peak) or not _is_valid(atr_1d):
        return (False, 0.0, None)
    space_pct = (spot - next_peak) / spot
    required_pct = _BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER * atr_1d / spot
    return (space_pct >= required_pct, space_pct, required_pct)
