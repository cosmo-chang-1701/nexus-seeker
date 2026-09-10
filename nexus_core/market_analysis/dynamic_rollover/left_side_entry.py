import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from market_analysis.atr_utils import compute_atr_15m_from_df
from market_analysis.index_microstructure import estimate_symbol_gamma_flip

from . import logger
from .constants import (
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _LEFT_ENTRY_ASYMMETRIC_ROOM_PCT,
    _LEFT_ENTRY_CALL_BTO_MIN_DTE,
    _LEFT_ENTRY_CALL_BTO_MIN_NOTIONAL_USD,
    _LEFT_ENTRY_CALL_BTO_MIN_RATIO,
    _LEFT_ENTRY_CANDIDATE_MIN_DTE,
    _LEFT_ENTRY_CAPITULATION_RANGE_RATIO,
    _LEFT_ENTRY_DOJI_BODY_RANGE_RATIO,
    _LEFT_ENTRY_DRAGONFLY_WICK_RANGE_RATIO,
    _LEFT_ENTRY_IVR_SPREAD_THRESHOLD,
    _LEFT_ENTRY_PIN_BAR_WICK_RATIO,
    _LEFT_ENTRY_PUT_STO_MIN_DTE,
    _LEFT_ENTRY_PUT_STO_MIN_NOTIONAL_USD,
    _LEFT_ENTRY_PUT_STO_MIN_RATIO,
    _LEFT_ENTRY_PUT_WALL_GEX_PROXY_THRESHOLD,
    _LEFT_ENTRY_PUT_WALL_LOWER_PCT,
    _LEFT_ENTRY_PUT_WALL_UPPER_PCT,
    _LEFT_ENTRY_UOA_CHASE_MIN_PREMIUM_USD,
    _LEFT_ENTRY_UOA_CHASE_RATIO_THRESHOLD,
    _LEFT_ENTRY_VOLUME_EXHAUST_MULT,
    _LEFT_ENTRY_VOLUME_PANIC_MULT,
    _LEFT_ENTRY_VTS_BACKWARDATION_RATIO,
    _LEFT_ENTRY_VWAP_ATR_MULT,
    _LEFT_ENTRY_RSI_MAX,
)


# --- 左側交易六重嚴格過濾鐵律 (逆勢均值回歸／做市商 Put Wall 底牆接刀) ---
# 結構完全比照 opportunity_cost.py 的右側六重鐵律組裝方式：共用衍生資料、
# reasons 列表原地累積、條件五/六在 1-4 未全數通過時短路略過 (⏭️ 標記)，
# orchestrator 對六項條件 AND 後回傳整體結果。技術定義方向與右側相反，故
# 同一時間點同一標的技術上幾乎不可能同時滿足兩套條件一。


def _detect_bullish_reversal_candle_pattern(df_15m: Any) -> Tuple[bool, str]:
    """K棒形態拒絕灌壓判定 (左側條件一子檢查)。

    本模組唯一全新發明的量化原語——全倉搜尋確認程式庫先前完全沒有任何 K 棒
    型態辨識邏輯 (無 hammer/doji/pin_bar 相關函式)。此為啟發式代理判定，非
    嚴謹的技術分析形態庫，門檻為經驗值，非逐 K 線形態學精算。

    排除「大陰線實體灌破」(收盤價貼近最低價的大陰線，代表拋壓仍在加速)；
    必須改為出現以下任一止跌訊號才視為通過：
    (a) 錘頭/Pin Bar：下影線長度 >= 實體 × _LEFT_ENTRY_PIN_BAR_WICK_RATIO；
    (b) 蜻蜓十字 (Dragonfly Doji)：實體 <= 全距 × 10% 且下影線 >= 全距 × 60%。
        錘頭的實體比例判定在實體趨近零時會退化 (任何數 >= 0 恆真)，必須靠
        body > 0 防呆排除墓碑十字，但那同時也排除了這個教科書級的底部反轉
        訊號，故以佔全距比例獨立判定補回；
    (c) 連續 2 根實體收窄且未破前低 (孕線整理雛形的簡化代理：後一根
        實體小於前一根，前一根實體亦不大於再前一根，且後兩根低點皆未低於
        再前一根的低點)。
    資料不足 (< 3 根 K 棒) 一律 fail-safe 判定未通過。
    """
    if df_15m is None or len(df_15m) < 3:
        return False, "K 棒資料不足，無法判定止跌型態"

    last = df_15m.iloc[-1]
    o, c, h, low_v = (
        float(last["Open"]),
        float(last["Close"]),
        float(last["High"]),
        float(last["Low"]),
    )
    rng = h - low_v
    body = abs(c - o)

    is_capitulation = False
    if rng > 0 and c < o:
        close_to_low_ratio = (c - low_v) / rng
        is_capitulation = close_to_low_ratio < _LEFT_ENTRY_CAPITULATION_RANGE_RATIO

    lower_wick = min(o, c) - low_v
    is_hammer = (
        rng > 0 and body > 0 and lower_wick >= body * _LEFT_ENTRY_PIN_BAR_WICK_RATIO
    )
    is_dragonfly = (
        rng > 0
        and body <= rng * _LEFT_ENTRY_DOJI_BODY_RANGE_RATIO
        and lower_wick >= rng * _LEFT_ENTRY_DRAGONFLY_WICK_RANGE_RATIO
    )

    prev = df_15m.iloc[-2]
    prev2 = df_15m.iloc[-3]
    prev_body = abs(float(prev["Close"]) - float(prev["Open"]))
    prev2_body = abs(float(prev2["Close"]) - float(prev2["Open"]))
    is_narrowing = (
        body < prev_body
        and prev_body <= prev2_body
        and low_v >= float(prev["Low"])
        and float(prev["Low"]) >= float(prev2["Low"])
    )

    has_reversal_signal = is_hammer or is_dragonfly or is_narrowing

    if is_capitulation:
        return False, "大陰線實體灌破 (Close≈Low)，拋壓仍在加速，非止跌訊號"
    if not has_reversal_signal:
        return False, "未偵測到錘頭/Pin Bar、蜻蜓十字或連續 2 根實體收窄止跌訊號"
    if is_hammer:
        return True, "錘頭/Pin Bar 止跌訊號"
    if is_dragonfly:
        return True, "蜻蜓十字 (Dragonfly Doji) 止跌訊號"
    return True, "連續 2 根實體收窄止跌訊號"


async def _confirm_left_entry_condition1_exhaustion_reversal(
    candidate_symbol: str,
    target_spot: float,
    session_vwap: float,
    reasons: list,
    df_15m: Optional[Any] = None,
    atr_15m: Optional[float] = None,
) -> Tuple[bool, Optional[Any]]:
    """左側條件一：結構性空頭力竭與極值乖離確認。

    15m K 線抓取刻意使用 force_refresh=True，且一律先經
    `price_volume_alert.trim_to_confirmed_15m_bars()` 截斷至最近一根**已收盤**
    K 棒：AGENTS.md 與 price_volume_alert.py 的模組 docstring 都明確警告過
    opportunity_cost.py 現行右側條件一沿用的抓取方式不檢查 bar 完整性、不強制
    刷新，並指名「新的日內K棒邏輯不應照抄該模式」。這對本條件尤其關鍵——
    「縮量窒息 (量 <= 0.7x 均量)」若用尚在成型的 K 棒判定，會在每根 K 棒的
    前段時間恆為真，形成危險方向的偽陽性；錘頭/Pin Bar 形態判定同樣會因
    Close/Low 仍在變動而失真。

    回傳 (是否通過, 本次抓取的 df_15m)，供呼叫端 (regime_classifier.py 路由至
    Regime I 時) 重用同一份 frame，避免對同一標的重複抓取。
    """
    if target_spot <= 0:
        reasons.append("左側條件一❌：candidate 現價無效")
        return False, df_15m

    if df_15m is None:
        try:
            from services import market_data_service

            df_15m = await market_data_service.get_history_df(
                candidate_symbol, period="5d", interval="15m", force_refresh=True
            )
        except Exception as e:
            df_15m = None
            logger.warning(f"[{candidate_symbol}] 左側條件一 15m K 線抓取失敗: {e}")

    from market_analysis.price_volume_alert import trim_to_confirmed_15m_bars

    df_confirmed = trim_to_confirmed_15m_bars(df_15m)
    if df_confirmed is None or len(df_confirmed) < _ENTRY_VOLUME_LOOKBACK_BARS + 1:
        reasons.append("左側條件一❌：15m 已收盤 K 線資料不足，無法確認極值乖離")
        return False, df_15m

    # ATR₁₅ₘ 就地從上方已抓取的同一份 frame 計算，不再另外呼叫 fetch_atr_15m()
    # ——後者抓的正是同樣的 period="5d", interval="15m"，重複呼叫等於對同一標的
    # 多發一次 force_refresh 真實請求，且可能取到與手上 K 棒不同快照的 ATR。
    # 呼叫端 (Regime 分類器路由至 Regime I) 已算好時直接沿用同一個值。
    if atr_15m is None:
        atr_15m = compute_atr_15m_from_df(df_confirmed)

    if session_vwap <= 0 or math.isnan(session_vwap):
        reasons.append("左側條件一❌：Session VWAP 抓取失敗，無法計算乖離門檻")
        return False, df_15m
    if atr_15m <= 0 or math.isnan(atr_15m):
        reasons.append("左側條件一❌：ATR₁₅ₘ 無法取得，無法計算乖離門檻")
        return False, df_15m

    deviation_threshold = session_vwap - _LEFT_ENTRY_VWAP_ATR_MULT * atr_15m
    is_deep_deviation = target_spot <= deviation_threshold

    try:
        import pandas_ta as ta

        rsi_series = ta.rsi(df_confirmed["Close"], length=14)
        rsi_val = (
            float(rsi_series.iloc[-1])
            if rsi_series is not None and not rsi_series.empty
            else float("nan")
        )
    except Exception as e:
        rsi_val = float("nan")
        logger.warning(f"[{candidate_symbol}] 左側條件一 RSI 計算失敗: {e}")

    is_oversold = not math.isnan(rsi_val) and rsi_val <= _LEFT_ENTRY_RSI_MAX

    pattern_ok, pattern_reason = _detect_bullish_reversal_candle_pattern(df_confirmed)

    last_bar = df_confirmed.iloc[-1]
    lookback_bars = df_confirmed.iloc[-(_ENTRY_VOLUME_LOOKBACK_BARS + 1) : -1]
    close_val = float(last_bar["Close"])
    low_val = float(last_bar["Low"])
    high_val = float(last_bar["High"])
    volume_val = float(last_bar["Volume"])
    avg_volume = float(lookback_bars["Volume"].mean())

    is_exhaustion_volume = (
        avg_volume > 0 and volume_val <= avg_volume * _LEFT_ENTRY_VOLUME_EXHAUST_MULT
    )
    rng = high_val - low_val
    closed_at_low = (
        rng > 0 and (close_val - low_val) / rng < _LEFT_ENTRY_CAPITULATION_RANGE_RATIO
    )
    is_panic_absorption = (
        avg_volume > 0
        and volume_val >= avg_volume * _LEFT_ENTRY_VOLUME_PANIC_MULT
        and not closed_at_low
    )
    is_volume_signature_ok = is_exhaustion_volume or is_panic_absorption

    c1_passed = (
        is_deep_deviation and is_oversold and pattern_ok and is_volume_signature_ok
    )

    reasons.append(
        f"左側條件一{'✅' if c1_passed else '❌'}：現價 ${target_spot:.2f} "
        f"{'<=' if is_deep_deviation else '>'} 乖離門檻 ${deviation_threshold:.2f} "
        f"(VWAP ${session_vwap:.2f}-{_LEFT_ENTRY_VWAP_ATR_MULT}×ATR ${atr_15m:.2f})，"
        f"RSI={rsi_val:.1f}{'≤' if is_oversold else '>'}{_LEFT_ENTRY_RSI_MAX:.0f}，"
        f"{pattern_reason}，量能 {volume_val:.0f} vs 均量 {avg_volume:.0f} "
        f"({'縮量窒息' if is_exhaustion_volume else ('恐慌吸收' if is_panic_absorption else '未達量能訊號')})"
    )
    return c1_passed, df_15m


def _confirm_left_entry_condition2_put_wall_test(
    candidate_symbol: str,
    gex_profile_data: Any,
    target_spot: float,
    reasons: list,
) -> bool:
    """左側條件二：做市商 Put Wall / 負 Gamma 吸附牆密著截擊。

    ⚠️ 資料缺口與代理設計：文獻規格要求「Put OI 名目價值 >= $1,000,000,000」，
    但現有 GEX 爬蟲資料完全沒有「每履約價 OI 名目金額」欄位，僅有 Net GEX
    曝險值。改用該履約價絕對 GEX 曝險量級是否超過 _LEFT_ENTRY_PUT_WALL_GEX_
    PROXY_THRESHOLD 代理門檻（比照既有 GEX_THIN_WALL_THRESHOLD 薄紙牆判定
    慣例），作為「防禦厚度」的近似代理，非真實 OI 名目金額。
    """
    put_wall = (
        float(gex_profile_data.get("put_wall", 0.0) or 0.0)
        if isinstance(gex_profile_data, dict)
        else 0.0
    )
    if put_wall <= 0 or target_spot <= 0:
        reasons.append("左側條件二❌：Put Wall 或現價資料缺失")
        return False

    dist_pct = (target_spot - put_wall) / target_spot
    is_densely_attached = (
        _LEFT_ENTRY_PUT_WALL_LOWER_PCT <= dist_pct <= _LEFT_ENTRY_PUT_WALL_UPPER_PCT
    )

    gex_profile = (
        gex_profile_data.get("gex_profile")
        if isinstance(gex_profile_data, dict)
        else None
    )
    wall_gex_magnitude = 0.0
    if isinstance(gex_profile, dict) and gex_profile:
        try:
            closest_key = min(
                gex_profile.keys(), key=lambda k: abs(float(k) - put_wall)
            )
            wall_gex_magnitude = abs(float(gex_profile.get(closest_key, 0.0) or 0.0))
        except (ValueError, TypeError):
            wall_gex_magnitude = 0.0

    is_thick_wall = wall_gex_magnitude >= _LEFT_ENTRY_PUT_WALL_GEX_PROXY_THRESHOLD

    c2_passed = is_densely_attached and is_thick_wall
    reasons.append(
        f"左側條件二{'✅' if c2_passed else '❌'}：現價 ${target_spot:.2f} 距 Put Wall "
        f"${put_wall:.2f} {dist_pct:+.2%}"
        f"（{'密著區間內' if is_densely_attached else '未密著'}），防禦厚度代理值 "
        f"${wall_gex_magnitude:,.0f} {'>=' if is_thick_wall else '<'} "
        f"${_LEFT_ENTRY_PUT_WALL_GEX_PROXY_THRESHOLD:,.0f} "
        f"(⚠️GEX曝險量級代理，非真實OI名目金額)"
    )
    return c2_passed


def _confirm_left_entry_condition3_no_panic_cliff(
    uoa_list: list,
    gex_profile_data: Any,
    target_spot: float,
    session_vwap: float,
    reasons: list,
) -> bool:
    """左側條件三：下檔無恐慌踩踏斷崖 + 向上均值回歸空間 >= 3.5%。"""
    put_wall = (
        float(gex_profile_data.get("put_wall", 0.0) or 0.0)
        if isinstance(gex_profile_data, dict)
        else 0.0
    )

    has_chase_selloff = False
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "PUT":
            continue
        if "BTO" not in str(entry.get("action", "")):
            continue
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        strike = float(entry.get("strike", 0.0) or 0.0)
        if (
            ratio > _LEFT_ENTRY_UOA_CHASE_RATIO_THRESHOLD
            and notional_value >= _LEFT_ENTRY_UOA_CHASE_MIN_PREMIUM_USD
            and put_wall > 0
            and strike < put_wall
        ):
            has_chase_selloff = True
            break

    gex_profile = (
        gex_profile_data.get("gex_profile")
        if isinstance(gex_profile_data, dict)
        else None
    )
    gamma_flip = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )

    candidates = [v for v in (session_vwap, gamma_flip) if v and v > 0]
    if not candidates or target_spot <= 0:
        reasons.append(
            "左側條件三❌：Session VWAP 與 Gamma Flip 皆無法取得，無法計算回歸空間"
        )
        return False

    reference_level = min(candidates)
    room_pct = (reference_level - target_spot) / target_spot
    has_room = room_pct >= _LEFT_ENTRY_ASYMMETRIC_ROOM_PCT

    c3_passed = (not has_chase_selloff) and has_room
    if has_chase_selloff:
        reasons.append(
            f"左側條件三❌：偵測到追空踩踏 PUT BTO "
            f"(ratio>{_LEFT_ENTRY_UOA_CHASE_RATIO_THRESHOLD}x、"
            f"權利金>=${_LEFT_ENTRY_UOA_CHASE_MIN_PREMIUM_USD:,.0f}、strike<Put Wall)"
        )
    elif not has_room:
        reasons.append(
            f"左側條件三❌：回歸空間 {room_pct:+.2%} 不足 "
            f"{_LEFT_ENTRY_ASYMMETRIC_ROOM_PCT:.1%}"
            f"（參考 min(VWAP,GammaFlip)=${reference_level:.2f}）"
        )
    else:
        reasons.append(
            f"左側條件三✅：無追空踩踏，回歸空間 {room_pct:+.2%}"
            f"（參考 min(VWAP,GammaFlip)=${reference_level:.2f}）"
        )
    return c3_passed


def _confirm_left_entry_condition4_smart_money_absorption(
    uoa_list: list, target_spot: float, reasons: list
) -> bool:
    """左側條件四：主力大額 PUT STO 接刀或長天期跨期 CALL BTO 佈局。

    ⚠️ 規格中的「須在過去 4 小時內偵測到」時間窗**刻意未實作**，且在現有資料源
    上無法實作：UOA 清單來自 `uoa_detector.py` 對選擇權鏈的**當日累計快照**
    (`volume` 是該履約價的當日累計成交量、`oi` 是前一交易日收盤未平倉量)，
    並非逐筆 time-and-sales tape，因此每一列都不帶、也無從帶有成交時間戳。
    同模組的 `paced_ratio` (依交易時段進度正規化) 之所以存在，正是為了補償這個
    資料模型的先天限制。若日後接上真實的選擇權逐筆成交資料源，此處才有條件
    加上時間窗過濾。
    """
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        opt_type = str(entry.get("type", "")).upper()
        action = str(entry.get("action", ""))
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        strike = float(entry.get("strike", 0.0) or 0.0)
        try:
            expiry_str = str(entry.get("expiry", ""))
            exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte = (exp_dt - datetime.now().date()).days
        except (ValueError, TypeError):
            continue

        if (
            opt_type == "PUT"
            and "STO" in action
            and dte >= _LEFT_ENTRY_PUT_STO_MIN_DTE
            and ratio >= _LEFT_ENTRY_PUT_STO_MIN_RATIO
            and notional_value >= _LEFT_ENTRY_PUT_STO_MIN_NOTIONAL_USD
            and strike <= target_spot
        ):
            reasons.append(
                f"左側條件四✅：主力 PUT STO 護盤 DTE={dte}、ratio={ratio:.2f}x、"
                f"權利金 ${notional_value:,.0f}"
            )
            return True
        if (
            opt_type == "CALL"
            and "BTO" in action
            and dte >= _LEFT_ENTRY_CALL_BTO_MIN_DTE
            and ratio >= _LEFT_ENTRY_CALL_BTO_MIN_RATIO
            and notional_value >= _LEFT_ENTRY_CALL_BTO_MIN_NOTIONAL_USD
        ):
            reasons.append(
                f"左側條件四✅：機構長天期 CALL BTO 底層潛伏 DTE={dte}、ratio={ratio:.2f}x、"
                f"權利金 ${notional_value:,.0f}"
            )
            return True

    reasons.append(
        f"左側條件四❌：未偵測到符合門檻的主力 PUT STO 護盤 "
        f"(DTE>={_LEFT_ENTRY_PUT_STO_MIN_DTE}、ratio>={_LEFT_ENTRY_PUT_STO_MIN_RATIO}、"
        f"Premium>=${_LEFT_ENTRY_PUT_STO_MIN_NOTIONAL_USD:,.0f}) 或 CALL BTO 底層潛伏 "
        f"(DTE>={_LEFT_ENTRY_CALL_BTO_MIN_DTE}、ratio>={_LEFT_ENTRY_CALL_BTO_MIN_RATIO}、"
        f"Premium>=${_LEFT_ENTRY_CALL_BTO_MIN_NOTIONAL_USD:,.0f})"
    )
    return False


async def _confirm_left_entry_condition5_macro_earnings_vts_gate(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    reasons: list,
) -> bool:
    """左側條件五：總經流動性危機與財報黑天鵝安全閥。重用
    opportunity_cost.py 既有的 _confirm_entry_condition5_macro_earnings_gate
    做共用的財報緩衝期 + 大盤 Regime 子檢查 (避免重複邏輯，維持門檻校準單一
    來源)，額外疊加 VIX 期限結構倒掛防禦。前四項未全數通過時短路略過，比照
    右側條件五同樣的短路優化理由，避免對已確定失敗的整體結果仍發動真實 I/O。
    """
    if not prior_conditions_passed:
        reasons.append("左側條件五⏭️：前四項未全數通過，略過總經/財報/VTS 安全閥檢查")
        return True

    from .opportunity_cost import _confirm_entry_condition5_macro_earnings_gate

    shared_reasons: list = []
    # 右側條件五現已一併回傳距財報天數 (供右側條件六收斂 DTE band)；左側條件六
    # 的 DTE 門檻是固定的 _LEFT_ENTRY_CANDIDATE_MIN_DTE (磨底需求)，不做財報收斂，
    # 故此處僅取用通過與否。
    (
        c5_base_passed,
        _days_to_earnings,
    ) = await _confirm_entry_condition5_macro_earnings_gate(
        candidate_symbol, True, shared_reasons
    )
    for r in shared_reasons:
        reasons.append(r.replace("條件五", "左側條件五", 1))

    if not c5_base_passed:
        return False

    try:
        from services.market_data_service import get_vix_term_structure

        vts = await get_vix_term_structure()
        vts_ratio = float(vts.get("vts_ratio", 0.0) or 0.0)
    except Exception as e:
        reasons.append(f"左側條件五❌：VIX 期限結構抓取失敗，安全起見判定未通過: {e}")
        return False

    is_backwardation = vts_ratio >= _LEFT_ENTRY_VTS_BACKWARDATION_RATIO
    if is_backwardation:
        reasons.append(
            f"左側條件五❌：VIX 期限結構倒掛 (vts_ratio={vts_ratio:.2f} >= "
            f"{_LEFT_ENTRY_VTS_BACKWARDATION_RATIO:.2f})，流動性凍結風險"
        )
        return False

    reasons.append(f"左側條件五✅：VIX 期限結構正常 (vts_ratio={vts_ratio:.2f})")
    return True


async def _confirm_left_entry_condition6_candidate_dte_ivr(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    target_ivr: float,
    reasons: list,
) -> Tuple[bool, Optional[str]]:
    """左側條件六：Candidate 自身 Theta 磨底防禦。回傳
    (是否通過, structure_directive)。structure_directive 為依 IVR 分流的建議
    策略字串——這是本函式與右側六重鐵律 (`_confirm_entry_condition6_candidate_
    dte`，僅回傳布林值) 唯一的簽章差異。target_ivr 由呼叫端從 candidate_radar
    的 iv_metrics 萃取傳入 (與 evaluate_opportunity_cost_for_satellites 既有
    target_ivr 同一來源)，避免另外發動一次 IV 抓取。"""
    if not prior_conditions_passed:
        reasons.append(
            "左側條件六⏭️：前五項未全數通過，略過 candidate 自身 Theta 磨底防禦"
        )
        return True, None

    try:
        from services import market_data_service

        expiries = await market_data_service.get_all_option_expiries(candidate_symbol)
    except Exception as e:
        expiries = []
        logger.warning(f"[{candidate_symbol}] 左側條件六選擇權到期日清單抓取失敗: {e}")

    if not expiries:
        reasons.append("左側條件六❌：無法取得標的最近效期選擇權到期日清單")
        return False, None

    try:
        nearest_expiry_dt = datetime.strptime(expiries[0], "%Y-%m-%d").date()
        dte_nearest = (nearest_expiry_dt - datetime.now().date()).days
    except (ValueError, TypeError) as e:
        reasons.append(f"左側條件六❌：標的最近效期到期日解析失敗: {e}")
        return False, None

    dte_ok = dte_nearest >= _LEFT_ENTRY_CANDIDATE_MIN_DTE
    if not dte_ok:
        reasons.append(
            f"左側條件六❌：標的最近效期 {expiries[0]} DTE={dte_nearest} "
            f"低於門檻 >={_LEFT_ENTRY_CANDIDATE_MIN_DTE}"
        )
        return False, None

    if target_ivr <= _LEFT_ENTRY_IVR_SPREAD_THRESHOLD:
        structure_directive = "輕度 ITM/ATM Call 買進"
    else:
        structure_directive = "Bull Call Spread 或 Short Put (IVR 過高，避免單腳買方)"

    reasons.append(
        f"左側條件六✅：標的最近效期 {expiries[0]} DTE={dte_nearest}，"
        f"IVR={target_ivr:.1f}% -> {structure_directive}"
    )
    return True, structure_directive


async def _confirm_left_entry_signal(
    candidate_symbol: str,
    candidate_radar: Dict[str, Any],
    target_spot: float,
    df_15m: Optional[Any] = None,
    session_vwap: Optional[float] = None,
    atr_15m: Optional[float] = None,
) -> Tuple[bool, str, Optional[str]]:
    """左側交易進場訊號六重嚴格過濾鐵律 orchestrator。結構完全比照
    opportunity_cost.py::_confirm_entry_signal 的六重鐵律組裝方式 (共用衍生
    資料、reasons 列表、條件五/六短路略過)。回傳 (六項條件是否全數通過,
    逐項原因說明字串, structure_directive)。

    df_15m / session_vwap / atr_15m 可選：regime_classifier.py 路由至 Regime I
    時會原樣傳入分類判定階段已抓取的同一份資料 (見 RegimeMarketData)，除了避免
    對同一標的重複抓取，更確保「盤勢分類」與「進場確認」建立在同一份快照上。
    """
    reasons: list[str] = []

    gex_profile_data = candidate_radar.get("gex_profile_data") or {}
    uoa_list = candidate_radar.get("uoa") or []
    target_ivr = float(
        candidate_radar.get("iv_metrics", {}).get("iv_rank", 0.0)
        if candidate_radar.get("iv_metrics")
        else 0.0
    )

    if session_vwap is None:
        try:
            from market_analysis.vwap_utils import fetch_session_vwap

            session_vwap = await fetch_session_vwap(candidate_symbol)
        except Exception as e:
            session_vwap = 0.0
            logger.warning(
                f"[{candidate_symbol}] 左側訊號 Session VWAP 共用抓取失敗: {e}"
            )

    c1_passed, _df_15m_used = await _confirm_left_entry_condition1_exhaustion_reversal(
        candidate_symbol,
        target_spot,
        session_vwap,
        reasons,
        df_15m=df_15m,
        atr_15m=atr_15m,
    )
    c2_passed = _confirm_left_entry_condition2_put_wall_test(
        candidate_symbol, gex_profile_data, target_spot, reasons
    )
    c3_passed = _confirm_left_entry_condition3_no_panic_cliff(
        uoa_list, gex_profile_data, target_spot, session_vwap, reasons
    )
    c4_passed = _confirm_left_entry_condition4_smart_money_absorption(
        uoa_list, target_spot, reasons
    )
    c5_passed = await _confirm_left_entry_condition5_macro_earnings_vts_gate(
        candidate_symbol,
        c1_passed and c2_passed and c3_passed and c4_passed,
        reasons,
    )
    (
        c6_passed,
        structure_directive,
    ) = await _confirm_left_entry_condition6_candidate_dte_ivr(
        candidate_symbol,
        c1_passed and c2_passed and c3_passed and c4_passed and c5_passed,
        target_ivr,
        reasons,
    )

    all_passed = (
        c1_passed and c2_passed and c3_passed and c4_passed and c5_passed and c6_passed
    )
    return all_passed, " | ".join(reasons), structure_directive
