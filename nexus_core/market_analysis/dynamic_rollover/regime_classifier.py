import math
from typing import Any, Optional, Tuple

from . import logger
from .constants import (
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _REGIME_I_PUT_WALL_LOWER_PCT,
    _REGIME_I_PUT_WALL_UPPER_PCT,
    _REGIME_I_RSI_MAX,
    _REGIME_I_VWAP_ATR_MULT,
    _REGIME_III_CALL_WALL_MIN_ROOM_PCT,
    _REGIME_III_RSI_MIN,
    _REGIME_III_SUPPORT_WALL_MAX_DIST_PCT,
    _REGIME_III_VOLUME_SURGE_MULT,
    _REGIME_IV_CALL_WALL_PROXIMITY_PCT,
    _REGIME_IV_VTS_BACKWARDATION_RATIO,
)
from .models import DynamicRegime, RegimeMarketData
from .structural_signals import _scan_gex_walls


async def classify_dynamic_regime(
    candidate_symbol: str,
    target_spot: float,
    gex_profile_data: Optional[dict],
    uoa_list: Optional[list] = None,
    df_15m: Optional[Any] = None,
) -> Tuple[DynamicRegime, str, RegimeMarketData]:
    """動態調整模式 4 態市場結構分類器。依既有結構性指標 (Put Wall / Call
    Wall / Gamma Flip / Session VWAP / ATR₁₅ₘ / 15m RSI / 成交量 / UOA / 大盤
    Regime) 將盤勢分類為 Regime I (左側接刀態) / II (混沌泥淖態，全系統休眠)
    / III (右側動能態) / IV (結構封頂／危機態，強制鎖定)，決定
    opportunity_cost.py Scenario 2 進場閘門要路由至左側六重鐵律、右側六重
    鐵律、還是完全不進場。

    Fail-safe 精神比照 _confirm_entry_signal：任何必要資料缺失一律回傳
    REGIME_II_CHAOS_STANDASIDE (最保守，不進場也不強制平倉)。

    回傳 (regime, reason, market_data)。market_data 為本次判定實際抓取/計算
    的 15m K 線 frame、Session VWAP 與 ATR₁₅ₘ (見 RegimeMarketData)，供呼叫端
    在路由至 Regime I 時原樣傳給 _confirm_left_entry_signal 重用——除了省下
    重複的網路請求，更重要的是確保「盤勢分類」與「進場確認」建立在同一份
    資料快照上。尚未執行到該步驟就提前回傳的分支，對應欄位維持預設 0.0。
    """
    uoa_list = uoa_list or []
    if target_spot <= 0 or not isinstance(gex_profile_data, dict):
        return (
            DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
            "資料缺失 (現價或 GEX Profile 無效)，fail-safe 判定混沌泥淖態",
            RegimeMarketData(df_15m=df_15m),
        )

    from market_analysis.index_microstructure import (
        detect_uoa_sto_call_physical_cap,
        estimate_symbol_gamma_flip,
        get_market_regime,
    )

    put_wall = float(gex_profile_data.get("put_wall", 0.0) or 0.0)
    call_wall = float(gex_profile_data.get("call_wall", 0.0) or 0.0)
    gex_profile = gex_profile_data.get("gex_profile")
    gamma_flip = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )

    # --- Regime IV：結構封頂／危機態 (最優先判定，全面鎖定態) ---
    try:
        macro_regime = await get_market_regime()
    except Exception as e:
        macro_regime = "NORMAL"
        logger.warning(f"[{candidate_symbol}] Regime 分類器大盤 Regime 抓取失敗: {e}")

    call_wall_room_pct = (
        (call_wall - target_spot) / target_spot
        if call_wall > 0 and target_spot > 0
        else None
    )
    is_call_wall_capped = (
        call_wall_room_pct is not None
        and call_wall_room_pct < _REGIME_IV_CALL_WALL_PROXIMITY_PCT
    )
    has_sto_call_cap, _capping_strike = detect_uoa_sto_call_physical_cap(
        uoa_list, target_spot, wall_reference=call_wall if call_wall > 0 else None
    )

    # VIX 期限結構深度倒掛：get_market_regime() 的 SHORT_GAMMA_CRITICAL 需同時
    # 滿足 VIX>20、vts>=1.0 與 SPY 跌破 Gamma Flip 三項，單純的深度倒掛
    # (>10% 溢價) 並不會觸發它，故此處獨立檢查。get_vix_term_structure() 內部
    # 走 get_history_df 的 6 小時快取，不構成額外抓取負擔。
    vts_ratio = 0.0
    try:
        from services.market_data_service import get_vix_term_structure

        vts = await get_vix_term_structure()
        vts_ratio = float(vts.get("vts_ratio", 0.0) or 0.0)
    except Exception as e:
        logger.warning(f"[{candidate_symbol}] Regime 分類器 VIX 期限結構抓取失敗: {e}")
    is_deep_backwardation = vts_ratio >= _REGIME_IV_VTS_BACKWARDATION_RATIO

    # 大盤 SHORT_GAMMA_CRITICAL (做市商翻入負 Gamma 踩踏) 與
    # SYSTEMIC_LIQUIDITY_CRISIS 同屬「禁止任何多頭開倉」的鎖定情境，比照既有
    # index_microstructure.py 與 Scenario 4 margin_defense.py 對這兩個 regime
    # 一視同仁的既有慣例。
    is_macro_lockout = macro_regime in (
        "SYSTEMIC_LIQUIDITY_CRISIS",
        "SHORT_GAMMA_CRITICAL",
    )

    if (
        is_macro_lockout
        or is_deep_backwardation
        or is_call_wall_capped
        or has_sto_call_cap
    ):
        reason = f"大盤 Regime={macro_regime}"
        if is_deep_backwardation:
            reason += f"，VIX 期限結構深度倒掛 (vts={vts_ratio:.2f})"
        if is_call_wall_capped and call_wall_room_pct is not None:
            reason += f"，Call Wall 空間 {call_wall_room_pct:+.2%} 不足"
        if has_sto_call_cap:
            reason += "，偵測到 STO Call 壓頂"
        return (
            DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS,
            reason,
            RegimeMarketData(df_15m=df_15m),
        )

    # --- Regime I/III 判定皆需要 15m K 線，此處共用一次抓取 ---
    if df_15m is None:
        try:
            from services import market_data_service

            df_15m = await market_data_service.get_history_df(
                candidate_symbol, period="5d", interval="15m", force_refresh=True
            )
        except Exception as e:
            df_15m = None
            logger.warning(f"[{candidate_symbol}] Regime 分類器 15m K 線抓取失敗: {e}")

    # 一律截斷至最近一根已收盤 K 棒：盤中最後一根仍在成型，其成交量只累積了
    # 一部分，直接用於下方的放量倍數判定會系統性低估量能 (與 left_side_entry.py
    # 條件一共用同一份 trim 定義)。
    from market_analysis.price_volume_alert import trim_to_confirmed_15m_bars

    df_confirmed = trim_to_confirmed_15m_bars(df_15m)
    if df_confirmed is None or len(df_confirmed) < _ENTRY_VOLUME_LOOKBACK_BARS + 1:
        return (
            DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
            "15m 已收盤 K 線資料不足，fail-safe 判定混沌泥淖態",
            RegimeMarketData(df_15m=df_15m),
        )

    try:
        from market_analysis.vwap_utils import fetch_session_vwap

        session_vwap = await fetch_session_vwap(candidate_symbol)
    except Exception as e:
        session_vwap = 0.0
        logger.warning(f"[{candidate_symbol}] Regime 分類器 Session VWAP 抓取失敗: {e}")

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
        logger.warning(f"[{candidate_symbol}] Regime 分類器 RSI 計算失敗: {e}")

    last_bar = df_confirmed.iloc[-1]
    lookback_bars = df_confirmed.iloc[-(_ENTRY_VOLUME_LOOKBACK_BARS + 1) : -1]
    open_val = float(last_bar["Open"])
    close_val = float(last_bar["Close"])
    volume_val = float(last_bar["Volume"])
    avg_volume = float(lookback_bars["Volume"].mean())
    is_bullish_candle = close_val > open_val
    is_volume_surge = (
        avg_volume > 0 and volume_val >= avg_volume * _REGIME_III_VOLUME_SURGE_MULT
    )

    support_wall, _resistance_wall, support_gex, _resistance_gex = _scan_gex_walls(
        candidate_symbol, gex_profile_data, spot=target_spot
    )
    support_dist_pct = (
        (target_spot - support_wall) / target_spot
        if support_wall > 0 and support_gex > 0 and target_spot > 0
        else None
    )

    # --- Regime III：右側動能態 ---
    is_regime_iii = (
        gamma_flip > 0
        and target_spot > gamma_flip
        and session_vwap > 0
        and target_spot > session_vwap
        and call_wall_room_pct is not None
        and call_wall_room_pct >= _REGIME_III_CALL_WALL_MIN_ROOM_PCT
        and support_dist_pct is not None
        and 0 < support_dist_pct <= _REGIME_III_SUPPORT_WALL_MAX_DIST_PCT
        and is_bullish_candle
        and is_volume_surge
        and not math.isnan(rsi_val)
        and rsi_val > _REGIME_III_RSI_MIN
    )
    if is_regime_iii:
        return (
            DynamicRegime.REGIME_III_RIGHT_MOMENTUM,
            f"結構突破伽馬擠壓確認：Spot ${target_spot:.2f} > Gamma Flip ${gamma_flip:.2f} "
            f"且站穩 VWAP ${session_vwap:.2f}，RSI={rsi_val:.1f}，放量突破",
            RegimeMarketData(df_15m=df_15m, session_vwap=session_vwap),
        )

    # --- Regime I：左側接刀態 ---
    # ATR₁₅ₘ 就地從上方已抓取的同一份 frame 計算 (fetch_atr_15m() 抓的正是同樣的
    # period="5d", interval="15m")，避免對同一標的重複發動 force_refresh 請求。
    from market_analysis.atr_utils import compute_atr_15m_from_df

    atr_15m = compute_atr_15m_from_df(df_confirmed)

    put_wall_dist_pct = (
        (target_spot - put_wall) / target_spot
        if put_wall > 0 and target_spot > 0
        else None
    )
    is_densely_attached_put_wall = (
        put_wall_dist_pct is not None
        and _REGIME_I_PUT_WALL_LOWER_PCT
        <= put_wall_dist_pct
        <= _REGIME_I_PUT_WALL_UPPER_PCT
    )
    is_deep_deviation = (
        session_vwap > 0
        and atr_15m > 0
        and target_spot <= session_vwap - _REGIME_I_VWAP_ATR_MULT * atr_15m
    )
    is_regime_i = (
        is_deep_deviation
        and not math.isnan(rsi_val)
        and rsi_val <= _REGIME_I_RSI_MAX
        and is_densely_attached_put_wall
    )
    if is_regime_i:
        return (
            DynamicRegime.REGIME_I_LEFT_CATCH,
            f"極端負乖離吸籌確認：Spot ${target_spot:.2f} 密著 Put Wall ${put_wall:.2f}，"
            f"RSI={rsi_val:.1f}",
            RegimeMarketData(df_15m=df_15m, session_vwap=session_vwap, atr_15m=atr_15m),
        )

    return (
        DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
        "無人區過渡震盪：未滿足 Regime I/III/IV 任一結構條件",
        RegimeMarketData(df_15m=df_15m, session_vwap=session_vwap, atr_15m=atr_15m),
    )
