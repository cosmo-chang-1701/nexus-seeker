import math
from typing import Any, Optional, Tuple

from market_analysis.room_threshold import (
    compute_dynamic_room_threshold,
    evaluate_next_strike_space,
    evaluate_wall_buffer,
    resolve_effective_target,
)

from . import logger
from .constants import (
    _ENTRY_UOA_CAP_RATIO_THRESHOLD,
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _REGIME_I_PUT_WALL_LOWER_PCT,
    _REGIME_I_PUT_WALL_UPPER_PCT,
    _REGIME_I_RSI_MAX,
    _REGIME_I_VWAP_ATR_MULT,
    _REGIME_III_B_LOOKBACK_BARS,
    _REGIME_III_B_MIN_HELD_BARS,
    _REGIME_III_B_RSI_MAX,
    _REGIME_III_B_RSI_MIN,
    _REGIME_III_RSI_MIN,
    _REGIME_III_VOLUME_SURGE_MULT,
    _REGIME_IV_VTS_BACKWARDATION_RATIO,
    _REGIME_V_RSI_MAX,
    _REGIME_V_VOLUME_SURGE_MULT,
)
from .models import DynamicRegime, RegimeMarketData
from .short_side_entry import _find_next_negative_gex_peak
from .structural_signals import (
    _scan_gex_walls,
    _scan_resistance_wall_above_spot,
    count_structure_held_bars,
)


async def classify_dynamic_regime(
    candidate_symbol: str,
    target_spot: float,
    gex_profile_data: Optional[dict],
    uoa_list: Optional[list] = None,
    df_15m: Optional[Any] = None,
) -> Tuple[DynamicRegime, str, RegimeMarketData]:
    """`_classify_dynamic_regime_impl` 的公開入口：分類後寫入前向蒐集紀錄。

    記錄只是 O(1) 的緩衝區 append (evaluation_recorder.py)，且僅在呼叫端標記
    了評估來源時才生效；分類邏輯本身見 `_classify_dynamic_regime_impl`。
    """
    regime, reason, market_data = await _classify_dynamic_regime_impl(
        candidate_symbol, target_spot, gex_profile_data, uoa_list, df_15m
    )
    from market_analysis.evaluation_recorder import record_regime_classification

    record_regime_classification(
        candidate_symbol,
        target_spot,
        regime.value,
        reason,
        gex_profile_data,
        session_vwap=market_data.session_vwap,
        atr_15m=market_data.atr_15m,
        rsi_15m=market_data.rsi_15m,
    )
    return regime, reason, market_data


async def _classify_dynamic_regime_impl(
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
    # 全鏈 Net GEX：Regime III-B 條件二「做市商未翻負」需要。解析順序比照
    # opportunity_cost.py 條件一的既有 fallback——優先用上游算好的純量，缺失或
    # NaN 時才從 gex_profile 逐檔加總，兩者皆不可得時為 NaN（III-B 隨之
    # fail-safe 不成立，絕不以 0.0 冒充「已知為非負」）。
    net_gex = float(gex_profile_data.get("net_gex", float("nan")) or float("nan"))
    if math.isnan(net_gex) and isinstance(gex_profile, dict):
        try:
            net_gex = sum(float(v) for v in gex_profile.values())
        except (TypeError, ValueError):
            net_gex = float("nan")
    gamma_flip = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )

    # --- Regime IV 宏觀鎖定分支：優先於一切，且刻意在 15m/ATR 抓取「之前」---
    # 系統性流動性危機 / 大盤負 Gamma 踩踏 / VIX 深度倒掛完全不依賴個股 K 線或
    # 該標的的波動率，能在此早退就不該為它多發一次 15m force_refresh 請求
    # （每輪次對每個候選標的都會走這條路徑）。做多做空皆禁——系統性流動性危機
    # 下做空同樣會被劇烈軋空，不是安全的方向。
    try:
        macro_regime = await get_market_regime()
    except Exception as e:
        macro_regime = "NORMAL"
        logger.warning(f"[{candidate_symbol}] Regime 分類器大盤 Regime 抓取失敗: {e}")

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
    # SYSTEMIC_LIQUIDITY_CRISIS 同屬「禁止任何開倉」的鎖定情境，比照既有
    # index_microstructure.py 與 Scenario 4 margin_defense.py 對這兩個 regime
    # 一視同仁的既有慣例。
    is_macro_lockout = macro_regime in (
        "SYSTEMIC_LIQUIDITY_CRISIS",
        "SHORT_GAMMA_CRITICAL",
    )

    if is_macro_lockout or is_deep_backwardation:
        reason = f"大盤 Regime={macro_regime}"
        if is_deep_backwardation:
            reason += f"，VIX 期限結構深度倒掛 (vts={vts_ratio:.2f})"
        return (
            DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS,
            reason,
            RegimeMarketData(df_15m=df_15m),
        )

    # --- 15m K 線與 ATR 抓取 ---
    # 這一段原本位於整個 Regime IV 區塊之後。Call Wall 空間門檻改為動態自適應
    # 波動率門檻後，「個股結構封頂」分支與 Regime III/V 都需要 ATR₁₅ₘ / ATR₁D，
    # 故上移至此（但仍在宏觀鎖定早退之後，見上方）。呼叫端已傳入 df_15m 時照常
    # 沿用，不新增網路成本；抓取失敗時 atr 兩項維持 0.0，room_threshold 會自動
    # 降級至 3.5% 絕對底線——比舊版固定 5% 寬鬆，屬刻意的 fail-open：資料缺失
    # 不應把標的誤鎖進「結構封頂危機態」而連帶封鎖整個動態轉倉引擎。
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

    # ATR₁₅ₘ 就地從上方已抓取的同一份 frame 計算 (fetch_atr_15m() 抓的正是同樣的
    # period="5d", interval="15m")，避免對同一標的重複發動 force_refresh 請求。
    from market_analysis.atr_utils import (
        compute_atr_15m_from_df,
        fetch_atr_1d,
        fetch_high_60d,
    )

    atr_15m = compute_atr_15m_from_df(df_confirmed)
    atr_1d = await fetch_atr_1d(candidate_symbol)
    # 晴空萬里天花板 (room_threshold.py 公式 D)：標的創新高時上方沒有存量 OI
    # 形成有效 Call Wall，裸 Call Wall 反映的是流動性真空而非真實阻力，需以
    # 60 日高點／ATR 外推的有效目標取代，否則創新高標的必然被本分支結構性
    # 誤判為封頂（見 handoff.md §1.4）。
    high_60d = await fetch_high_60d(candidate_symbol)

    # --- Regime IV 個股結構封頂偵測（需要動態門檻，故在抓取之後）---
    eff_target = resolve_effective_target(target_spot, call_wall, high_60d, atr_1d)
    call_wall_room_pct = (
        (eff_target.target - target_spot) / target_spot
        if eff_target.target > 0 and target_spot > 0
        else None
    )
    # Call Wall 空間門檻自固定 5% 升級為動態自適應波動率門檻 (room_threshold.py
    # 公式 A)。Regime 分類器 (路由層) 與六重鐵律條件三 (進場確認層) 共用的是同
    # 一條公式，但仍各自獨立呼叫、各自持有輸入——constants.py 明訂的「兩層門檻
    # 不合併」政策針對的是可變旋鈕，不是演算法。
    room = compute_dynamic_room_threshold(
        target_spot, put_wall, atr_15m, atr_1d, direction="LONG"
    )
    is_call_wall_capped = (
        call_wall_room_pct is not None and call_wall_room_pct < room.threshold_pct
    )
    # ratio 門檻必須顯式傳入 _ENTRY_UOA_CAP_RATIO_THRESHOLD (1.5)：函式預設值仍是
    # 較寬鬆的 1.0，而右側條件三早已刻意調高為 1.5，用意就是避免一般 STO 平倉/
    # 避險單被誤判為物理封頂。此處若沿用預設值，單筆 ratio 1.2 的例行 STO 印花就
    # 會把正常盤況分類成 Regime IV 全面鎖倉。
    has_sto_call_cap, capping_strike = detect_uoa_sto_call_physical_cap(
        uoa_list,
        target_spot,
        _ENTRY_UOA_CAP_RATIO_THRESHOLD,
        wall_reference=call_wall if call_wall > 0 else None,
    )

    # 判定優先序（見 models.py::DynamicRegime docstring）：
    #   1. Regime IV 宏觀鎖定分支（已於上方早退）
    #   2. Regime V  破位追空態
    #   3. Regime IV 個股結構封頂分支 (Call Wall 空間不足 / STO 封頂)
    #   4. Regime III → I → II
    # 第 2 與第 3 的先後是刻意的：壓頂與破位可以同時成立，若不拆分優先序，
    # 做空將永遠被 Regime IV 遮蔽而無法觸發。
    is_structural_cap = is_call_wall_capped or has_sto_call_cap

    def _build_regime_iv_reason() -> str:
        reason = f"大盤 Regime={macro_regime}"
        if is_call_wall_capped and call_wall_room_pct is not None:
            target_label = "晴空萬里有效目標" if eff_target.is_blue_sky else "Call Wall"
            reason += (
                f"，{target_label} ${eff_target.target:.2f} 空間 "
                f"{call_wall_room_pct:+.2%} 不足動態門檻 {room.threshold_pct:.2%}"
            )
            if room.degrade_reason:
                reason += f"（⚠️ {room.degrade_reason}）"
            if eff_target.degrade_reason:
                reason += f"（⚠️ {eff_target.degrade_reason}）"
        if has_sto_call_cap:
            wall_desc = "位於 Call Wall 上方" if call_wall > 0 else "位於現價上方"
            reason += f"，偵測到 STO Call 壓頂 @ ${capping_strike:.2f}（{wall_desc}）"
        return reason

    # --- 15m 已收盤 K 線充足性檢查 ---
    if df_confirmed is None or len(df_confirmed) < _ENTRY_VOLUME_LOOKBACK_BARS + 1:
        if is_structural_cap:
            # K 線不足無法判定 Regime V，但個股結構封頂本身不依賴 K 線，
            # 維持既有的 Regime IV 鎖定行為（不因缺 K 線而放寬成混沌態）。
            return (
                DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS,
                _build_regime_iv_reason(),
                RegimeMarketData(df_15m=df_15m, atr_15m=atr_15m),
            )
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
    has_support_wall = support_wall > 0 and support_gex > 0
    # 支撐牆距離判定自固定上限 5% 升級為緩衝雙邊界 (公式 B，profile="RIGHT")，
    # 量的是停損距離而非牆距：下界 2.5×ATR₁₅ₘ 防停損落在日內雜訊帶內而遭
    # Liquidity Sweep 掃損；上界為絕對 8% 的風險兜底。
    support_buffer = (
        evaluate_wall_buffer(
            target_spot, support_wall, atr_15m, atr_1d, profile="RIGHT"
        )
        if has_support_wall
        else None
    )

    # --- Regime V：破位追空態 (優先於 Regime IV 的個股結構封頂分支) ---
    # 完全鏡像 Regime III 的七項條件，方向全面反轉：站上 → 跌破、陽線 → 陰線、
    # RSI > 55 → RSI < 45；測距對象由現價下方的支撐牆改為上方的阻力頂牆；
    # 並額外要求跌破 Put Wall 後下方仍有 >= 2.0×ATR₁D 的次級負 Gamma 節點空間
    # （公式 C），否則追空等於在真空區追高殺低。
    resistance_wall, resistance_gex = _scan_resistance_wall_above_spot(
        candidate_symbol, gex_profile_data, spot=target_spot
    )
    resistance_buffer = (
        evaluate_wall_buffer(
            target_spot, resistance_wall, atr_15m, atr_1d, profile="SHORT"
        )
        if resistance_wall > 0 and resistance_gex > 0
        else None
    )
    next_peak = _find_next_negative_gex_peak(gex_profile_data, target_spot)
    has_next_strike_space, next_space_pct, _next_required = evaluate_next_strike_space(
        target_spot, next_peak, atr_1d
    )
    is_bearish_candle = close_val < open_val
    is_volume_surge_short = (
        avg_volume > 0 and volume_val >= avg_volume * _REGIME_V_VOLUME_SURGE_MULT
    )
    is_regime_v = (
        gamma_flip > 0
        and target_spot < gamma_flip
        and session_vwap > 0
        and target_spot < session_vwap
        and put_wall > 0
        and target_spot < put_wall
        and has_next_strike_space
        and resistance_buffer is not None
        and resistance_buffer.passed
        and is_bearish_candle
        and is_volume_surge_short
        and not math.isnan(rsi_val)
        and rsi_val < _REGIME_V_RSI_MAX
    )
    if is_regime_v:
        return (
            DynamicRegime.REGIME_V_BREAKDOWN_CHASE,
            f"結構破位負 Gamma 順勢助跌確認：Spot ${target_spot:.2f} 跌破 "
            f"Gamma Flip ${gamma_flip:.2f}、VWAP ${session_vwap:.2f} 與 Put Wall "
            f"${put_wall:.2f}，RSI={rsi_val:.1f}，放量陰線破位；至次級負 Gamma "
            f"節點 ${next_peak:.2f} 尚有 {next_space_pct:.2%} 空間",
            RegimeMarketData(
                df_15m=df_15m,
                session_vwap=session_vwap,
                atr_15m=atr_15m,
                rsi_15m=rsi_val,
            ),
        )

    # --- Regime IV：個股結構封頂分支 (未達 Regime V 時才判定) ---
    if is_structural_cap:
        return (
            DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS,
            _build_regime_iv_reason(),
            RegimeMarketData(
                df_15m=df_15m,
                session_vwap=session_vwap,
                atr_15m=atr_15m,
                rsi_15m=rsi_val,
            ),
        )

    # --- Regime III：右側動能態 ---
    is_regime_iii = (
        gamma_flip > 0
        and target_spot > gamma_flip
        and session_vwap > 0
        and target_spot > session_vwap
        and call_wall_room_pct is not None
        and call_wall_room_pct >= room.threshold_pct
        and support_buffer is not None
        and support_buffer.passed
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
            RegimeMarketData(
                df_15m=df_15m,
                session_vwap=session_vwap,
                atr_15m=atr_15m,
                rsi_15m=rsi_val,
            ),
        )

    # --- Regime III-B：右側趨勢延續態（必須排在 Regime III 之後）---
    # Regime III 問「突破是否正在發生」，III-B 問「趨勢是否仍然成立」。突破當下
    # 兩者都會成立，歸類為 III 是刻意的：它帶著更強的進場證據，且 III-B 的 UOA
    # 時間窗放寬不應套用在突破態上。
    # 條件一刻意**不要求**放量與實體陽線——那正是事件式判定的兩項特徵，保留即
    # 退化回 Regime III。趨勢的續航段是縮量、陰陽交錯的（handoff.md §1.3）。
    held_bars, held_window, held_same_session = count_structure_held_bars(
        df_confirmed, gamma_flip, session_vwap
    )
    is_trend_structure_held = (
        held_same_session
        and held_window >= _REGIME_III_B_LOOKBACK_BARS
        and held_bars >= _REGIME_III_B_MIN_HELD_BARS
    )
    is_regime_iii_b = (
        is_trend_structure_held
        and gamma_flip > 0
        and session_vwap > 0
        and not math.isnan(net_gex)
        and net_gex > 0.0
        and call_wall_room_pct is not None
        and call_wall_room_pct >= room.threshold_pct
        and support_buffer is not None
        and support_buffer.passed
        and not math.isnan(rsi_val)
        and _REGIME_III_B_RSI_MIN < rsi_val < _REGIME_III_B_RSI_MAX
    )
    if is_regime_iii_b:
        target_label = "晴空萬里有效目標" if eff_target.is_blue_sky else "Call Wall"
        return (
            DynamicRegime.REGIME_III_B_TREND_CONTINUATION,
            f"趨勢延續確認：近 {held_window} 根已收盤 15m K 棒有 {held_bars} 根同時站穩 "
            f"Gamma Flip ${gamma_flip:.2f} 與 VWAP ${session_vwap:.2f}"
            f"（門檻 {_REGIME_III_B_MIN_HELD_BARS}/{_REGIME_III_B_LOOKBACK_BARS}），"
            f"Net GEX {net_gex:+,.0f} 仍為正，RSI={rsi_val:.1f}；"
            f"{target_label} ${eff_target.target:.2f} 尚有 {call_wall_room_pct:.2%} 空間",
            RegimeMarketData(
                df_15m=df_15m,
                session_vwap=session_vwap,
                atr_15m=atr_15m,
                rsi_15m=rsi_val,
            ),
        )

    # --- Regime I：左側接刀態 ---
    # atr_15m 已於本函式開頭自同一份 df_confirmed 就地算好（Regime IV 的動態
    # 門檻亦需要），此處直接沿用，不重算也不重抓。
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
            RegimeMarketData(
                df_15m=df_15m,
                session_vwap=session_vwap,
                atr_15m=atr_15m,
                rsi_15m=rsi_val,
            ),
        )

    return (
        DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
        "無人區過渡震盪：未滿足 Regime I/III/IV 任一結構條件",
        RegimeMarketData(
            df_15m=df_15m,
            session_vwap=session_vwap,
            atr_15m=atr_15m,
            rsi_15m=rsi_val,
        ),
    )
