import asyncio
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database.user_settings import get_full_user_context
from market_analysis.index_microstructure import (
    detect_uoa_sto_call_physical_cap,
    estimate_symbol_gamma_flip,
)

from . import logger
from ._shared import (
    format_cash_impact,
    format_illiquidity_warning,
    resolve_current_value,
)
from .constants import (
    CORE_DEFENSE_ETF_SYMBOLS,
    _BREAKOUT_READY_THRESHOLD,
    _EARNINGS_PRE_EVENT_BUFFER_DAYS,
    _ENTRY_ASYMMETRIC_ROOM_PCT,
    _ENTRY_CANDIDATE_MIN_DTE,
    _ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT,
    _ENTRY_UOA_CAP_RATIO_THRESHOLD,
    _ENTRY_UOA_MIN_DTE,
    _ENTRY_UOA_MIN_NOTIONAL_USD,
    _ENTRY_UOA_MIN_RATIO,
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _ENTRY_VOLUME_SURGE_MULTIPLIER,
    _ESTIMATED_ROUND_TRIP_COST_PCT,
    _EV_SPREAD_MIN_THRESHOLD,
    _LOW_IVR_UPPER_BOUND,
    _MOMENTUM_DECAY_THRESHOLD,
    _PROFIT_LOCK_PROFIT_PCT_THRESHOLD,
    _PUT_WALL_PROXIMITY_TOLERANCE,
    _ROLLOVER_RATIO_HIGH_PROFIT,
    _ROLLOVER_RATIO_STANDARD,
    _SKEW_DOWNSIDE_PENALTY_FACTOR,
)
from .models import (
    DynamicRegime,
    RolloverInstruction,
    RolloverScenario,
    TradingStrategyMode,
)
from .structural_signals import _scan_gex_walls, evaluate_option_dte_tier


# --- _confirm_entry_signal 六重進場鐵律各條件的獨立判斷函式 ---
# 每個函式對應一項條件，共用的衍生資料 (gex_profile_data / gex_profile /
# uoa_list / call_wall / net_gex) 由呼叫端 (_confirm_entry_signal) 於迴圈外
# 算好一次後傳入，避免重複解析。reasons 為呼叫端持有的單一列表，各函式依序
# 原地 append 自身條件的判定說明，確保最終合併字串的順序一致。
async def _confirm_entry_condition1_breakout(
    candidate_symbol: str,
    target_spot: float,
    gex_profile: Optional[dict],
    reasons: list,
    net_gex: Optional[float] = None,
    df_15m: Optional[Any] = None,
    session_vwap: Optional[float] = None,
) -> bool:
    """條件一：結構性右側放量突破確認 (15m 實體陽線收盤 + 放量，站穩 Gamma Flip
    估算門檻或全域 Long Gamma 替代門檻，且須站穩 Session VWAP)。

    :param df_15m: 呼叫端 (DYNAMIC 模式下 `regime_classifier.classify_dynamic_regime`
        路由至 REGIME_III 時) 若已抓取過同一標的的 15m K 線 frame，原樣傳入以避免
        本函式內部重複發起網路請求；為 None 時比照既有行為自行抓取。
    :param session_vwap: 同上，避免重複抓取 Session VWAP（`fetch_session_vwap`
        預設 `force_refresh=True`，每次呼叫皆為真實網路請求，重複抓取代價不小）。

    Gamma Flip 邊界處理與 Fallback 機制：
    - 若全鏈期權分佈極端導致無零交叉點 (estimate_symbol_gamma_flip <= 0)：
      - 若全鏈動態 Net GEX < 0：確認處於全域 Short Gamma 泥淖，結構性空頭直接判定未通過。
      - 若全鏈動態 Net GEX > 0：代表全區間處於做市商正 Gamma 吸收波動的自穩定狀態。
        強求 Flip 交叉門檻會造成「誤殺」，故啟用 Fallback 替代方案：改以站穩
        Session VWAP + 0.5 × ATR₁₅ₘ 作為突破確認標準。
      - 若 Net GEX == 0 或數據缺失：fail-safe 判定未通過。

    四項突破要素須同時成立才通過：
    1. 15m 收盤價站穩門檻 (Gamma Flip 或 Fallback VWAP + 0.5 × ATR₁₅ₘ)；
    2. 15m 成交量 ≥ 前 20 根均量 × 1.5 倍 (放量突破)；
    3. K 棒須為實體陽線 (close > open)，排除陰線放量摜壓假突破；
    4. 15m 收盤價須站穩 Session VWAP。"""
    if target_spot <= 0:
        reasons.append("條件一❌：candidate 現價無效")
        return False

    effective_net_gex = net_gex
    if effective_net_gex is None or math.isnan(effective_net_gex):
        if isinstance(gex_profile, dict):
            try:
                effective_net_gex = sum(float(v) for v in gex_profile.values())
            except (ValueError, TypeError):
                effective_net_gex = 0.0
        else:
            effective_net_gex = 0.0
    if math.isnan(effective_net_gex):
        effective_net_gex = 0.0

    gamma_flip_est = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )

    is_fallback_mode = False
    if gamma_flip_est <= 0:
        if effective_net_gex < 0:
            reasons.append(
                "條件一❌：全域 Short Gamma 泥淖 (Net GEX < 0 且無 Flip 交叉點)，結構性空頭直接不通過"
            )
            return False
        elif effective_net_gex > 0:
            is_fallback_mode = True
        else:
            reasons.append(
                "條件一❌：無法估算 Gamma Flip 門檻 (GEX Profile 無交叉點且無明確方向)"
            )
            return False

    async def _fetch_df_15m() -> Any:
        try:
            from services import market_data_service

            return await market_data_service.get_history_df(
                candidate_symbol, period="5d", interval="15m"
            )
        except Exception as e:
            logger.warning(f"[{candidate_symbol}] 15m K 線抓取失敗: {e}")
            return None

    async def _fetch_session_vwap() -> float:
        try:
            from market_analysis.vwap_utils import fetch_session_vwap

            return await fetch_session_vwap(candidate_symbol)
        except Exception as e:
            logger.warning(f"[{candidate_symbol}] Session VWAP 抓取失敗: {e}")
            return 0.0

    # 兩者互不依賴 (皆只需 candidate_symbol)，皆需重新抓取時以 asyncio.gather
    # 併發執行取代原本序列 await，省下一趟網路往返延遲；呼叫端已提供其中一項
    # 或兩項時直接沿用，完全略過對應的網路請求。
    resolved_df_15m: Any = df_15m
    resolved_session_vwap: float
    if df_15m is None and session_vwap is None:
        resolved_df_15m, resolved_session_vwap = await asyncio.gather(
            _fetch_df_15m(), _fetch_session_vwap()
        )
    else:
        if resolved_df_15m is None:
            resolved_df_15m = await _fetch_df_15m()
        resolved_session_vwap = (
            session_vwap if session_vwap is not None else await _fetch_session_vwap()
        )

    if (
        resolved_df_15m is None
        or resolved_df_15m.empty
        or len(resolved_df_15m) < _ENTRY_VOLUME_LOOKBACK_BARS + 1
    ):
        reasons.append("條件一❌：15m K 線資料不足，無法確認突破")
        return False

    df_15m = resolved_df_15m
    session_vwap = resolved_session_vwap

    if is_fallback_mode:
        atr_15m = 0.0
        try:
            if (
                len(df_15m) >= 14
                and "High" in df_15m.columns
                and "Low" in df_15m.columns
            ):
                import pandas_ta as ta

                atr_series = ta.atr(
                    df_15m["High"], df_15m["Low"], df_15m["Close"], length=14
                )
                if atr_series is not None and not atr_series.empty:
                    atr_val = float(atr_series.iloc[-1])
                    if not math.isnan(atr_val):
                        atr_15m = atr_val
        except Exception as e:
            logger.debug(f"[{candidate_symbol}] 內嵌 ATR_15m 計算失敗: {e}")

        if atr_15m <= 0 or math.isnan(atr_15m):
            try:
                from market_analysis.atr_utils import fetch_atr_15m

                atr_15m = await fetch_atr_15m(candidate_symbol)
            except Exception as e:
                atr_15m = 0.0
                logger.warning(f"[{candidate_symbol}] fetch_atr_15m 失敗: {e}")

        if session_vwap <= 0 or math.isnan(session_vwap):
            reasons.append(
                "條件一❌：全域 Long Gamma 替代門檻計算失敗 (Session VWAP 抓取失敗)"
            )
            return False
        if atr_15m <= 0 or math.isnan(atr_15m):
            reasons.append(
                "條件一❌：全域 Long Gamma 替代門檻計算失敗 (ATR₁₅ₘ 無法取得)"
            )
            return False

        breakout_threshold = session_vwap + 0.5 * atr_15m
    else:
        breakout_threshold = gamma_flip_est

    last_bar = df_15m.iloc[-1]
    lookback_bars = df_15m.iloc[-(_ENTRY_VOLUME_LOOKBACK_BARS + 1) : -1]
    open_val = float(last_bar["Open"])
    close_val = float(last_bar["Close"])
    volume_val = float(last_bar["Volume"])
    avg_volume = float(lookback_bars["Volume"].mean())
    is_closed_above = close_val > breakout_threshold
    is_volume_surge = (
        avg_volume > 0 and volume_val >= avg_volume * _ENTRY_VOLUME_SURGE_MULTIPLIER
    )
    is_bullish_candle = close_val > open_val
    is_above_vwap = (
        not math.isnan(session_vwap) and session_vwap > 0 and close_val > session_vwap
    )
    c1_passed = (
        is_closed_above and is_volume_surge and is_bullish_candle and is_above_vwap
    )
    candle_tag = (
        "陽線" if is_bullish_candle else ("陰線" if close_val < open_val else "十字")
    )
    vwap_tag = (
        f"VWAP ${session_vwap:.2f} {'站穩' if is_above_vwap else '未站穩'}"
        if session_vwap > 0
        else "VWAP 抓取失敗"
    )
    if is_fallback_mode:
        reasons.append(
            f"條件一{'✅' if c1_passed else '❌'}：[全域Long Gamma] 15m收盤 ${close_val:.2f} "
            f"{'>' if is_closed_above else '<='} 替代門檻 ${breakout_threshold:.2f} "
            f"(VWAP ${session_vwap:.2f}+0.5×ATR ${atr_15m:.2f})，"
            f"量能 {volume_val:.0f} vs 均量×{_ENTRY_VOLUME_SURGE_MULTIPLIER} "
            f"={avg_volume * _ENTRY_VOLUME_SURGE_MULTIPLIER:.0f}，"
            f"K棒{candle_tag}、{vwap_tag}"
        )
    else:
        reasons.append(
            f"條件一{'✅' if c1_passed else '❌'}：15m收盤 ${close_val:.2f} "
            f"{'>' if is_closed_above else '<='} Gamma Flip估算 ${gamma_flip_est:.2f}，"
            f"量能 {volume_val:.0f} vs 均量×{_ENTRY_VOLUME_SURGE_MULTIPLIER} "
            f"={avg_volume * _ENTRY_VOLUME_SURGE_MULTIPLIER:.0f}，"
            f"K棒{candle_tag}、{vwap_tag}"
        )
    return c1_passed


def _confirm_entry_condition2_support_wall(
    candidate_symbol: str,
    gex_profile_data: Any,
    target_spot: float,
    reasons: list,
) -> bool:
    """條件二：做市商正 Gamma 底牆完好 (現價須站上支撐牆，且距離落在
    (0, _ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT] 之內才算「即時有效防禦」——
    支撐牆離現價過遠即便現價仍在其上方，也不構成短線可依靠的保護)。

    物理定義約束：支撐位在物理定義上必須位於現價下方 (K < Spot)。
    透過 _scan_gex_walls(..., spot=target_spot) 將掃描範圍強制約束在現價下方：
        Support Wall = argmax_{K < Spot} (Net GEX(K))
    避免將現價上方的阻力牆 (Call Wall) 誤當成下方的防禦底牆。若現價下方無任何
    正 GEX 峰值 (或曝險低於 GEX_THIN_WALL_THRESHOLD 門檻)，直接判定未通過。"""
    support_wall, _resistance_wall, support_gex, _resistance_gex = _scan_gex_walls(
        candidate_symbol,
        gex_profile_data if isinstance(gex_profile_data, dict) else None,
        spot=target_spot,
    )
    has_support_wall = support_wall > 0 and support_gex > 0
    dist_pct = (
        (target_spot - support_wall) / target_spot
        if has_support_wall and target_spot > 0
        else None
    )
    is_above_wall = (
        dist_pct is not None and 0 < dist_pct <= _ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT
    )
    c2_passed = has_support_wall and is_above_wall
    if not has_support_wall:
        reasons.append("條件二❌：未偵測到有效正 Gamma 支撐牆 (現價下方無正 GEX 峰值)")
    elif dist_pct is None:
        reasons.append("條件二❌：candidate 現價無效，無法計算支撐牆距離")
    elif dist_pct <= 0:
        reasons.append(
            f"條件二❌：現價 ${target_spot:.2f} <= 正 Gamma 支撐牆 ${support_wall:.2f}"
        )
    else:
        reasons.append(
            f"條件二{'✅' if is_above_wall else '❌'}：現價 ${target_spot:.2f} "
            f"距正 Gamma 支撐牆 ${support_wall:.2f} +{dist_pct:.2%}"
            f"（{'≤' if is_above_wall else '>'}{_ENTRY_SUPPORT_WALL_MAX_DISTANCE_PCT:.0%} "
            f"{'有效防禦' if is_above_wall else '距離過遠，非即時有效保護'}）"
        )
    return c2_passed


def _confirm_entry_condition3_no_physical_cap(
    uoa_list: list,
    call_wall: float,
    target_spot: float,
    reasons: list,
) -> bool:
    """條件三：UOA 無實質物理封頂 (上方空間暢通)。比照分析中心 (Symbol Hub) 的
    GEX CallWall 距現價空間% 判讀：不要求 Call Wall 必須還在現價之上，只要帶
    正負號的距離 (call_wall - spot) / spot 小於門檻，即代表做市商壓制仍在——
    現價已觸及甚至跌破 Call Wall 時（距離為負值）同樣視為空間不足，而非誤判
    為「已站上、無封頂」。物理封頂偵測改以 Call Wall（而非現價）作為 strike
    位置基準，並套用 _ENTRY_UOA_CAP_RATIO_THRESHOLD 較高的 ratio 門檻，降低
    一般 STO 平倉/避險單被誤判為物理封頂的假警報率。"""
    has_physical_cap, capping_strike = detect_uoa_sto_call_physical_cap(
        uoa_list,
        target_spot,
        _ENTRY_UOA_CAP_RATIO_THRESHOLD,
        wall_reference=call_wall if call_wall > 0 else None,
    )

    call_wall_dist_pct = (
        (call_wall - target_spot) / target_spot
        if call_wall > 0 and target_spot > 0
        else None
    )
    has_tight_call_wall = (
        call_wall_dist_pct is not None
        and call_wall_dist_pct < _ENTRY_ASYMMETRIC_ROOM_PCT
    )
    c3_passed = not has_physical_cap and not has_tight_call_wall
    if has_physical_cap:
        reasons.append(
            f"條件三❌：偵測到單筆 ratio>{_ENTRY_UOA_CAP_RATIO_THRESHOLD}x OI 的 "
            f"STO Call 物理封頂 @ ${capping_strike:.2f}（位於 Call Wall 上方）"
        )
    elif has_tight_call_wall and call_wall_dist_pct is not None:
        reasons.append(
            f"條件三❌：Call Wall ${call_wall:.2f} 距現價空間 "
            f"{call_wall_dist_pct:+.2%} 不足 {_ENTRY_ASYMMETRIC_ROOM_PCT:.0%} "
            f"非對稱空間"
        )
    else:
        reasons.append("條件三✅：上方無實質物理封頂，非對稱空間充足")
    return c3_passed


def _confirm_entry_condition4_uoa_dte(
    uoa_list: list, target_spot: float, reasons: list
) -> bool:
    """條件四：主力跨週期買盤認證與雜訊過濾 (主力 UOA BTO Call 買盤須同時滿足
    DTE >= 7、ratio (Volume/OI) >= _ENTRY_UOA_MIN_RATIO、權利金名目金額 >=
    _ENTRY_UOA_MIN_NOTIONAL_USD，且 strike >= 現價，排除深實值避險單)。uoa
    已依權利金金額（名目價值）降序排列，逐筆掃描找出第一筆同時符合四項門檻者。"""
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "CALL":
            continue
        if "BTO" not in str(entry.get("action", "")):
            continue
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        if ratio < _ENTRY_UOA_MIN_RATIO:
            continue
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        if notional_value < _ENTRY_UOA_MIN_NOTIONAL_USD:
            continue
        strike = float(entry.get("strike", 0.0) or 0.0)
        if strike < target_spot:
            continue
        try:
            expiry_str = str(entry.get("expiry", ""))
            exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte = (exp_dt - datetime.now().date()).days
        except (ValueError, TypeError):
            continue
        if dte >= _ENTRY_UOA_MIN_DTE:
            reasons.append(
                f"條件四✅：主力買盤 DTE={dte}、ratio={ratio:.2f}x OI、"
                f"權利金 ${notional_value:,.0f} "
                f"(符合門檻 DTE>={_ENTRY_UOA_MIN_DTE}、ratio>={_ENTRY_UOA_MIN_RATIO}、"
                f"Premium>=${_ENTRY_UOA_MIN_NOTIONAL_USD:,.0f})"
            )
            return True

    reasons.append(
        f"條件四❌：未偵測到符合門檻 (DTE>={_ENTRY_UOA_MIN_DTE}、"
        f"ratio>={_ENTRY_UOA_MIN_RATIO}、Premium>=${_ENTRY_UOA_MIN_NOTIONAL_USD:,.0f}、"
        f"strike>=現價) 的主力 CALL BTO 買盤"
    )
    return False


async def _confirm_entry_condition5_macro_earnings_gate(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    reasons: list,
) -> bool:
    """條件五：總經負 Gamma 與財報黑天鵝防禦閘門 (前四項通過時才發動判定，避免
    為了一個已經確定會失敗的整體結果，仍去打財報行事曆/總經 Regime 這類真實
    I/O)。未發動時仍在 reasons 補上一行「⏭️ 略過」標記 (不觸發任何額外 I/O)，
    確保「進場鐵律檢核」面板永遠完整列出六項條件，不會因短路優化而讓使用者
    誤以為只有四重鐵律。"""
    if not prior_conditions_passed:
        reasons.append("條件五⏭️：前四項未全數通過，略過總經/財報安全閥檢查")
        return True

    c5_passed = True
    try:
        from database.calendar_cache import get_cached_earnings

        earn = get_cached_earnings(candidate_symbol)
        if earn and earn.get("earnings_date"):
            earn_date_str = str(earn["earnings_date"])[:10]
            earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
            days_to_er = (earn_dt - datetime.now().date()).days
            if 0 <= days_to_er <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                c5_passed = False
                reasons.append(
                    f"條件五❌：即將於 {days_to_er} 天內發布財報，避開高波事件風險"
                )
    except Exception as e:
        c5_passed = False
        reasons.append(f"條件五❌：財報行事曆資料抓取失敗，安全起見判定未通過: {e}")

    if c5_passed:
        try:
            from market_analysis.index_microstructure import get_market_regime

            regime = await get_market_regime()
            if regime in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS"):
                c5_passed = False
                reasons.append(
                    f"條件五❌：大盤處於 `{regime}` 負 Gamma 踩踏模式，嚴禁開倉個股買方"
                )
        except Exception as e:
            c5_passed = False
            reasons.append(
                f"條件五❌：大盤總經風控狀態抓取失敗，安全起見判定未通過: {e}"
            )

    if c5_passed:
        reasons.append("條件五✅：總經環境與財報事件風控安全")

    return c5_passed


async def _confirm_entry_condition6_candidate_dte(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    reasons: list,
) -> bool:
    """條件六：避開 candidate 自身最近效期選擇權週期的結算日前夕/當日雜訊
    (0/1 DTE)。前五項通過時才發動判定，比照條件五同樣的短路優化理由，未發動
    時仍在 reasons 補上一行「⏭️ 略過」標記，維持六項條件在檢核面板永遠完整
    列出。"""
    if not prior_conditions_passed:
        reasons.append("條件六⏭️：前五項未全數通過，略過 candidate 自身 DTE 雜訊檢查")
        return True

    c6_passed = False
    try:
        from services import market_data_service

        expiries = await market_data_service.get_all_option_expiries(candidate_symbol)
    except Exception as e:
        expiries = []
        logger.warning(f"[{candidate_symbol}] 選擇權到期日清單抓取失敗: {e}")

    if not expiries:
        reasons.append("條件六❌：無法取得標的最近效期選擇權到期日清單")
        return c6_passed

    try:
        nearest_expiry_dt = datetime.strptime(expiries[0], "%Y-%m-%d").date()
        dte_nearest = (nearest_expiry_dt - datetime.now().date()).days
        c6_passed = dte_nearest > _ENTRY_CANDIDATE_MIN_DTE
        reasons.append(
            f"條件六{'✅' if c6_passed else '❌'}：標的最近效期 {expiries[0]} "
            f"DTE={dte_nearest}"
            f"（{'符合' if c6_passed else '低於'} 門檻 >{_ENTRY_CANDIDATE_MIN_DTE}）"
        )
        return c6_passed
    except (ValueError, TypeError) as e:
        reasons.append(f"條件六❌：標的最近效期到期日解析失敗: {e}")
        return False


class _OpportunityCostMixin:
    """邏輯 (2)：機會成本與期望值比對 (Opportunity Cost & EV Comparison)。"""

    def _calculate_ev_proxy(
        self, symbol: str, skew_percentile: Optional[float] = None
    ) -> float:
        """
        Skew-Adjusted EV 期望值模型：
        以快取的 expected_move_upper 相對現貨的正規化上緣空間為基礎，
        並結合 Skew 偏斜度進行下行風險調整：
        Adjusted EV = Base EV * (1.0 - Downside Risk Penalty)
        當 Skew Percentile < 50% (偏恐慌/偏空) 時施加懲罰，避免單純因為波動大而誤判為高期望值。
        僅使用 market_cache（Cache-Aside），零額外 API 呼叫。
        is_stale 或 is_degraded 的快取視為不可信，回傳 0.0。
        """
        from database.market_cache import get_market_cache

        row = get_market_cache(symbol)
        if not row or row.get("is_stale") or row.get("is_degraded"):
            return 0.0
        spot = float(row.get("reference_spot_price") or 0.0)
        upper = float(row.get("expected_move_upper") or 0.0)
        if spot <= 0.0:
            return 0.0
        base_ev = (upper - spot) / spot

        # 若未提供 skew_percentile，嘗試從快取讀取
        if skew_percentile is None:
            try:
                from database.cache import get_kv_cache

                cached_sp = get_kv_cache(f"skew_percentile_{symbol.upper()}")
                if cached_sp is not None:
                    skew_percentile = float(cached_sp)
            except Exception:
                pass

        if skew_percentile is not None and skew_percentile < 50.0:
            downside_penalty = (
                (50.0 - skew_percentile) / 50.0
            ) * _SKEW_DOWNSIDE_PENALTY_FACTOR
            return float(max(0.0, base_ev * (1.0 - downside_penalty)))

        return float(base_ev)

    def _find_best_rollover_target(
        self, user_id: int, exclude_symbols: Optional[set] = None
    ) -> str:
        """掃描使用者 Watchlist 與 market_cache 快取尋找下一個高 EV 衛星標的，若無則回傳 VOO。
        自動避開即將在 3 天內發布財報的高波事件標的。"""
        from database.calendar_cache import get_cached_earnings
        from database.watchlist import get_user_watchlist

        exclude = {
            s.upper() for s in (exclude_symbols or set())
        } | CORE_DEFENSE_ETF_SYMBOLS
        try:
            watchlist = get_user_watchlist(user_id)
        except Exception as e:
            logger.error(f"取得 user {user_id} watchlist 失敗: {e}")
            return "VOO"

        today_dt = datetime.now().date()
        best_symbol = "VOO"
        best_ev = _EV_SPREAD_MIN_THRESHOLD  # 門檻 EV > _EV_SPREAD_MIN_THRESHOLD
        for sym, _ in watchlist:
            sym_u = str(sym).upper()
            if sym_u in exclude:
                continue

            # 避開即將發布財報的標的 (機構風控：避開二元事件黑天鵝)
            try:
                earn = get_cached_earnings(sym_u)
                if earn and earn.get("earnings_date"):
                    earn_date_str = str(earn["earnings_date"])[:10]
                    earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                    diff_days = (earn_dt - today_dt).days
                    if 0 <= diff_days <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                        continue
            except Exception:
                pass

            ev = self._calculate_ev_proxy(sym_u)
            if ev > best_ev:
                best_ev = ev
                best_symbol = sym_u
        return best_symbol

    def _normalize_power_squeeze(self, psq: Dict[str, Any]) -> float:
        """
        將 analyze_psq() 產生的 PSQResult (dict 形式，如 radar cache 中的 psq_result)
        正規化為 0-100 的 PowerSqueeze 分數，供 evaluate_opportunity_cost() 使用。
        重用既有的 squeeze_level / signal_direction / momentum_color / is_breakout_long/short
        分級，而非發明新的量化門檻。
        """
        level = str(psq.get("squeeze_level", "Normal"))
        direction = str(psq.get("signal_direction", "Neutral"))
        mom_color = str(psq.get("momentum_color", "Neutral"))
        is_bullish = direction == "Long" or mom_color in ("LightBlue", "Golden")
        is_bearish = direction == "Short" or mom_color in ("Red", "DarkBlue")

        table = {
            "Release": {"neutral": 10.0, "bull": 75.0, "bear": 5.0},
            "Normal": {"neutral": 30.0, "bull": 40.0, "bear": 20.0},
            "Mid": {"neutral": 60.0, "bull": 70.0, "bear": 45.0},
            "High": {"neutral": 50.0, "bull": 90.0, "bear": 10.0},
        }
        bucket = table.get(level, table["Normal"])
        score = (
            bucket["bull"]
            if is_bullish
            else (bucket["bear"] if is_bearish else bucket["neutral"])
        )

        if psq.get("is_breakout_long"):
            score = max(score, 95.0)
        elif psq.get("is_breakout_short"):
            score = min(score, 5.0)

        return float(max(0.0, min(100.0, score)))

    def evaluate_opportunity_cost(
        self,
        current_holding_symbol: str,
        current_holding_power_squeeze: float,
        current_holding_profit_pct: float,
        target_watchlist_symbol: str,
        target_power_squeeze: float,
        target_expected_value: float,
        current_holding_expected_value: float,
        target_ivr: float = 0.0,
        target_uoa_sweep: bool = False,
        target_spot: float = 0.0,
        target_put_wall: float = 0.0,
        friction_cost_pct: float = _ESTIMATED_ROUND_TRIP_COST_PCT,
    ) -> Dict[str, Any]:
        """
        邏輯 (2): 機會成本與期望值比對 (包含勝率傾斜)
        結合 PowerSqueeze 動能指標，當持倉動能衰退且 Watchlist 具備突破條件時，
        計算期望值並給出具備清晰履約價規格的轉倉建議。

        friction_cost_pct：往返交易摩擦成本估計值，預設為靜態保守值
        _ESTIMATED_ROUND_TRIP_COST_PCT。呼叫端 (evaluate_opportunity_cost_for_satellites)
        於高波動環境下會改傳入動態計算值 (候選標的近價期權合約 Bid-Ask 點差
        推算)，確保 EV 門檻在流動性摩擦擴大時自動提高。本函式維持純運算、
        零 I/O，僅接受呼叫端已算好的數值，不在此處發動網路請求。
        """
        # 假設 PowerSqueeze 指標中，數值越低代表動能越弱，越高代表突破動能強烈
        holding_momentum_decaying = (
            current_holding_power_squeeze < _MOMENTUM_DECAY_THRESHOLD
        )
        target_breakout_ready = target_power_squeeze > _BREAKOUT_READY_THRESHOLD

        # 期望值差距
        ev_spread = target_expected_value - current_holding_expected_value

        should_rollover = False
        rollover_ratio = 0.0
        strategy = "Buy Shares"

        if (
            holding_momentum_decaying
            and target_breakout_ready
            and ev_spread > (_EV_SPREAD_MIN_THRESHOLD + friction_cost_pct)
        ):
            should_rollover = True
            if current_holding_profit_pct > _PROFIT_LOCK_PROFIT_PCT_THRESHOLD:
                # 獲利豐厚，可轉換 50%
                rollover_ratio = _ROLLOVER_RATIO_HIGH_PROFIT
            else:
                # 獲利一般或虧損，轉換 30% 或全轉，視風險偏好而定
                rollover_ratio = _ROLLOVER_RATIO_STANDARD

            # ----------------------------------------------------
            # 條件二：新標的出現「極致不對稱勝率」
            # ----------------------------------------------------
            is_low_ivr = 0 < target_ivr < _LOW_IVR_UPPER_BOUND
            is_near_put_wall = (target_put_wall > 0 and target_spot > 0) and (
                abs(target_spot - target_put_wall) / target_put_wall
                <= _PUT_WALL_PROXIMITY_TOLERANCE
            )
            is_extreme_asymmetric = is_low_ivr and is_near_put_wall and target_uoa_sweep

            if is_extreme_asymmetric:
                strategy = "Shares + ITM Call"
                target_strike = round(target_spot * 0.95, 2) if target_spot > 0 else 0.0
                strike_note = (
                    f" (ITM 70Δ Call @ ${target_strike:.2f}, 30-45 DTE)"
                    if target_spot > 0
                    else ""
                )
                reason_suffix = f" (🎯 條件二極致勝率觸發: 低IVR({target_ivr:.1f}%) + 鋼鐵牆築底 + 巨鯨掃貨{strike_note}，強制啟動轉倉)"
            else:
                strategy = "Buy Shares"
                reason_suffix = ""

            # 強制優先採用極致不對稱勝率條件
            if holding_momentum_decaying and is_extreme_asymmetric:
                should_rollover = True
                rollover_ratio = 1.0  # 條件三要求 100% 滿載運算 / 不留戀
                return {
                    "should_rollover": should_rollover,
                    "rollover_ratio": rollover_ratio,
                    "strategy": strategy,
                    "reason": (
                        f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                        f"Target {target_watchlist_symbol} hit asymmetric win-rate. "
                        + reason_suffix
                    ),
                }

            return {
                "should_rollover": should_rollover,
                "rollover_ratio": rollover_ratio,
                "strategy": strategy,
                "reason": (
                    f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                    f"Target {target_watchlist_symbol} showing breakout potential (PSQ={target_power_squeeze}) "
                    f"with EV spread +{ev_spread * 100:.1f}%." + reason_suffix
                ),
            }

        return {
            "should_rollover": False,
            "rollover_ratio": 0.0,
            "strategy": "N/A",
            "reason": "No action required.",
        }

    async def _confirm_entry_signal(
        self,
        candidate_symbol: str,
        candidate_radar: Dict[str, Any],
        target_spot: float,
        df_15m: Optional[Any] = None,
        session_vwap: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        防洗盤實戰策略：進場訊號六重嚴格過濾鐵律。六項條件必須同時成立才允許
        evaluate_opportunity_cost_for_satellites 對 candidate_symbol 實際啟動
        機會成本轉倉指令。

        Fail-safe 原則（比照 gamma_cliff_confirmation.is_gamma_cliff_confirmed）：
        任何一項條件所需資料缺失、抓取失敗或無法確認，一律判定該條件未通過
        (不進場)，不預設通過、不略過。

        回傳 (六項條件是否全數通過, 逐項原因說明字串，供 log 觀察用)。

        六項條件各自的判斷邏輯拆分至模組層級的 _confirm_entry_condition{1..6}_*
        函式（本檔案類別定義之前），此處僅負責準備各條件共用的衍生資料
        （避免重複解析 candidate_radar）、依序呼叫並串接 gating 關係。

        :param df_15m: DYNAMIC 模式下 `regime_classifier.classify_dynamic_regime`
            路由至 REGIME_III 時，原樣傳入分類階段已抓取的同一份 15m K 線 frame，
            比照 `left_side_entry._confirm_left_entry_signal` 既有的重用模式，
            避免條件一內部重複發起網路請求、也避免「盤勢分類」與「進場確認」
            建立在不同時間點抓取的資料快照上。
        :param session_vwap: 同上，避免重複抓取 Session VWAP。
        """
        reasons: list[str] = []

        gex_profile_data = candidate_radar.get("gex_profile_data") or {}
        gex_profile = (
            gex_profile_data.get("gex_profile")
            if isinstance(gex_profile_data, dict)
            else None
        )
        uoa_list = candidate_radar.get("uoa") or []
        call_wall = (
            float(gex_profile_data.get("call_wall", 0.0) or 0.0)
            if isinstance(gex_profile_data, dict)
            else 0.0
        )
        net_gex = (
            float(gex_profile_data.get("net_gex", 0.0) or 0.0)
            if isinstance(gex_profile_data, dict)
            else 0.0
        )
        if (net_gex == 0.0 or math.isnan(net_gex)) and "net_gex" in candidate_radar:
            try:
                net_gex = float(candidate_radar.get("net_gex", 0.0) or 0.0)
            except (ValueError, TypeError):
                net_gex = 0.0
        if (net_gex == 0.0 or math.isnan(net_gex)) and gex_profile:
            try:
                net_gex = sum(float(v) for v in gex_profile.values())
            except (ValueError, TypeError):
                net_gex = 0.0
        if math.isnan(net_gex):
            net_gex = 0.0

        c1_passed = await _confirm_entry_condition1_breakout(
            candidate_symbol,
            target_spot,
            gex_profile,
            reasons,
            net_gex=net_gex,
            df_15m=df_15m,
            session_vwap=session_vwap,
        )
        c2_passed = _confirm_entry_condition2_support_wall(
            candidate_symbol, gex_profile_data, target_spot, reasons
        )
        c3_passed = _confirm_entry_condition3_no_physical_cap(
            uoa_list, call_wall, target_spot, reasons
        )
        c4_passed = _confirm_entry_condition4_uoa_dte(uoa_list, target_spot, reasons)
        c5_passed = await _confirm_entry_condition5_macro_earnings_gate(
            candidate_symbol,
            c1_passed and c2_passed and c3_passed and c4_passed,
            reasons,
        )
        c6_passed = await _confirm_entry_condition6_candidate_dte(
            candidate_symbol,
            c1_passed and c2_passed and c3_passed and c4_passed and c5_passed,
            reasons,
        )

        all_passed = (
            c1_passed
            and c2_passed
            and c3_passed
            and c4_passed
            and c5_passed
            and c6_passed
        )
        return all_passed, " | ".join(reasons)

    async def evaluate_opportunity_cost_for_satellites(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
        candidate_symbol: str,
        candidate_radar: Optional[Dict[str, Any]],
    ) -> Tuple[List[RolloverInstruction], Optional[Tuple[bool, str]]]:
        """
        邏輯 (2) 批次橋接：對每一個尚未被 Scenario 3 標記的 SATELLITE 持倉，
        比對其 PowerSqueeze/EV 與單一預篩選候選標的 (candidate_symbol) 的機會成本，
        產生與 check_satellite_rebalancing 相同結構的 instruction dict。

        candidate_radar: 由呼叫端 (cog 層) 預先透過既有 radar 抓取機制取得的單一候選標的資料，
        純資料 dict，避免 market_analysis 層依賴 cogs。

        回傳 (instructions, entry_confirmation)：entry_confirmation 為
        (is_entry_confirmed, entry_reason) 或 None (未觸及 _confirm_entry_signal，
        例如 candidate_symbol 為 "VOO" 或無 candidate_radar 而提早返回)。呼叫端
        (portfolio_monitor.py) 將此結果原樣轉交邏輯 (5) 核心資金部署，避免針對
        同一 candidate_symbol 在同一輪次內重複執行 _confirm_entry_signal 的六重
        條件驗證 (內含未快取的 get_market_regime() 呼叫與歷史 K 線/選擇權到期日抓取)。
        """
        instructions: List[RolloverInstruction] = []
        if candidate_symbol == "VOO" or not candidate_radar:
            return instructions, None  # 沒有找到高 EV 候選標的，不強制轉倉

        target_psq = candidate_radar.get("psq_result", {}) or {}
        target_power_squeeze = self._normalize_power_squeeze(target_psq)
        target_expected_value = self._calculate_ev_proxy(candidate_symbol)
        target_spot = float(
            candidate_radar.get("quote", {}).get("c", 0.0)
            if candidate_radar.get("quote")
            else 0.0
        )
        target_ivr = float(
            candidate_radar.get("iv_metrics", {}).get("iv_rank", 0.0)
            if candidate_radar.get("iv_metrics")
            else 0.0
        )
        target_put_wall = (
            float(
                candidate_radar.get("gex_profile_data", {}).get("put_wall", 0.0) or 0.0
            )
            if isinstance(candidate_radar.get("gex_profile_data"), dict)
            else 0.0
        )
        target_uoa_sweep = len(candidate_radar.get("uoa", []) or []) > 0

        # 交易策略引擎：依使用者 /settings 選擇的 trading_strategy (右側交易/
        # 左側交易/動態調整) 決定要套用哪一套進場鐵律。RIGHT_SIDE 為預設值，
        # 呼叫既有六重鐵律，行為與改動前完全一致 (零行為變化)。LEFT_SIDE 呼叫
        # 全新的逆勢均值回歸六重鐵律 (left_side_entry.py)。DYNAMIC 先透過
        # 4-Regime 分類器 (regime_classifier.py) 判定盤勢，再路由至對應鐵律或
        # 直接判定未通過 (Regime II 混沌泥淖態/IV 結構封頂危機態)。
        try:
            trading_strategy = get_full_user_context(user_id).trading_strategy
        except Exception as e:
            trading_strategy = TradingStrategyMode.RIGHT_SIDE.value
            logger.warning(
                f"[{candidate_symbol}] 讀取使用者 {user_id} 交易策略設定失敗，"
                f"退回右側交易預設: {e}"
            )

        suggested_strategy_override: Optional[str] = None
        entry_regime: Optional[str] = None

        if trading_strategy == TradingStrategyMode.LEFT_SIDE.value:
            from .left_side_entry import _confirm_left_entry_signal

            (
                is_entry_confirmed,
                entry_reason,
                suggested_strategy_override,
            ) = await _confirm_left_entry_signal(
                candidate_symbol, candidate_radar, target_spot
            )
        elif trading_strategy == TradingStrategyMode.DYNAMIC.value:
            from .left_side_entry import _confirm_left_entry_signal
            from .regime_classifier import classify_dynamic_regime

            gex_profile_data_for_regime = candidate_radar.get("gex_profile_data") or {}
            uoa_list_for_regime = candidate_radar.get("uoa") or []
            regime, regime_reason, regime_market_data = await classify_dynamic_regime(
                candidate_symbol,
                target_spot,
                gex_profile_data_for_regime,
                uoa_list_for_regime,
            )
            entry_regime = regime.value
            if regime == DynamicRegime.REGIME_III_RIGHT_MOMENTUM:
                is_entry_confirmed, entry_reason = await self._confirm_entry_signal(
                    candidate_symbol,
                    candidate_radar,
                    target_spot,
                    # 原樣沿用分類階段已抓取的 15m frame / Session VWAP，避免對
                    # 同一標的重複發起網路請求 (比照下方 REGIME_I 分支既有作法)。
                    df_15m=regime_market_data.df_15m,
                    session_vwap=regime_market_data.session_vwap,
                )
            elif regime == DynamicRegime.REGIME_I_LEFT_CATCH:
                (
                    is_entry_confirmed,
                    entry_reason,
                    suggested_strategy_override,
                ) = await _confirm_left_entry_signal(
                    candidate_symbol,
                    candidate_radar,
                    target_spot,
                    # 原樣沿用分類階段已抓取的 15m frame / Session VWAP /
                    # ATR₁₅ₘ，確保「盤勢分類」與「進場確認」建立在同一份資料
                    # 快照上，並省去對同一標的的重複網路請求。
                    df_15m=regime_market_data.df_15m,
                    session_vwap=regime_market_data.session_vwap,
                    atr_15m=regime_market_data.atr_15m,
                )
            else:
                is_entry_confirmed = False
                entry_reason = f"⛔ Regime `{regime.value}`：{regime_reason}"
        else:
            # 防洗盤實戰策略：進場訊號六重嚴格過濾鐵律 (右側交易，預設行為)。
            # 六項條件必須同時成立才允許對 candidate_symbol 啟動任何機會成本
            # 轉倉指令；未通過時比照上方「找不到候選標的」的早退模式，靜默
            # 略過、不產生任何指令。
            is_entry_confirmed, entry_reason = await self._confirm_entry_signal(
                candidate_symbol, candidate_radar, target_spot
            )

        entry_confirmation: Optional[Tuple[bool, str]] = (
            is_entry_confirmed,
            entry_reason,
        )
        if not is_entry_confirmed:
            logger.info(
                f"[{candidate_symbol}] 進場訊號未確認，靜默略過機會成本轉倉: {entry_reason}"
            )
            return instructions, entry_confirmation

        # 高波環境滑點與摩擦成本動態化：以候選標的近價期權合約的實際 Bid-Ask
        # 點差取代固定 0.3% 往返成本估算，確保極端高波 (寬點差) 時 EV 門檻
        # 自動提高，避免在流動性摩擦過大時仍放行進場。任何抓取失敗一律退回
        # 靜態保守值 (find_best_contract 本身已內含 try/except 永不拋例外，
        # 此處另包一層防禦僅為了 bid/ask 數值驗證與除法本身)。
        friction_cost_pct = _ESTIMATED_ROUND_TRIP_COST_PCT
        try:
            from market_analysis.strategy import find_best_contract

            near_atm_contract = await find_best_contract(
                candidate_symbol, "STO_CALL", 0.50, 21, 45
            )
            if near_atm_contract and target_spot > 0:
                bid = float(near_atm_contract.get("bid", 0.0))
                ask = float(near_atm_contract.get("ask", 0.0))
                if bid > 0 and ask > bid:
                    spread_pct = (ask - bid) / target_spot
                    friction_cost_pct = max(
                        _ESTIMATED_ROUND_TRIP_COST_PCT, spread_pct * 1.5
                    )
        except Exception as e:
            logger.warning(
                f"[{candidate_symbol}] 近價期權合約點差抓取失敗，退回靜態摩擦成本: {e}"
            )

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            if asset.get("asset_class") != "SATELLITE":
                continue
            instrument_class = str(
                asset.get("instrument_type", asset.get("asset_type", "SPOT"))
            ).upper()
            instrument_class = (
                "OPTIONS"
                if ("OPT" in instrument_class or "CONTRACT" in instrument_class)
                else "SPOT"
            )
            if (symbol, instrument_class) in already_flagged_symbols:
                continue
            if symbol == candidate_symbol:
                continue

            # DTE 三態狀態機：機會成本轉倉本質即是「新增轉倉」(NEW_OPPORTUNITY)，
            # dte<7 一律封鎖 (末日流動性雜訊，不適合驅動新轉倉決策)。dte<=1
            # 已由 Scenario 3 的強制結算保護接管，此處靜默跳過避免重複/矛盾指令。
            if instrument_class == "OPTIONS":
                dte_tier = evaluate_option_dte_tier(
                    int(asset.get("dte", 99)), "NEW_OPPORTUNITY"
                )
                if dte_tier != "NORMAL_EXECUTION":
                    continue

            holding_psq = asset.get("psq_result", {}) or {}
            current_power_squeeze = self._normalize_power_squeeze(holding_psq)
            current_ev = self._calculate_ev_proxy(symbol)

            avg_cost = float(asset.get("avg_cost", 0.0))
            spot = float(asset.get("spot_price", 0.0))
            profit_pct = (spot - avg_cost) / avg_cost if avg_cost > 0 else 0.0

            result = self.evaluate_opportunity_cost(
                current_holding_symbol=symbol,
                current_holding_power_squeeze=current_power_squeeze,
                current_holding_profit_pct=profit_pct,
                target_watchlist_symbol=candidate_symbol,
                target_power_squeeze=target_power_squeeze,
                target_expected_value=target_expected_value,
                current_holding_expected_value=current_ev,
                target_ivr=target_ivr,
                target_uoa_sweep=target_uoa_sweep,
                target_spot=target_spot,
                target_put_wall=target_put_wall,
                friction_cost_pct=friction_cost_pct,
            )
            if not result["should_rollover"]:
                continue

            # 預估資金影響與建議限價：現貨持倉市值優先，缺失時退回股數*現價估算；
            # 限價採用候選標的即時報價 (target_spot)，取代呼叫端過去恆為
            # "Market" 的佔位字串。
            current_value = resolve_current_value(
                float(asset.get("current_value", 0.0)),
                float(asset.get("quantity", 0.0)),
                spot,
            )
            recovered_cash = current_value * result["rollover_ratio"]
            cash_impact = format_cash_impact(recovered_cash)

            # 流動性閘門：比照 Scenario 3/4 既有做法，期權部位若帶有 bid/ask 且
            # 點差過寬時強制要求手動確認執行 (ManualOverrideView)，而非放行一鍵
            # 執行按鈕 (RolloverActionView)，避免使用者在滑價風險下誤觸一鍵轉倉。
            bid = float(asset.get("bid", 0.0))
            ask = float(asset.get("ask", 0.0))
            illiquidity_warning = (
                format_illiquidity_warning(bid, ask)
                if instrument_class == "OPTIONS"
                else None
            )
            is_illiquid_warning = illiquidity_warning is not None
            reason_text = f"💡 **機會成本轉倉 (Opportunity Cost)**\n{result['reason']}"
            if illiquidity_warning:
                reason_text += illiquidity_warning

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE"
                    if result["rollover_ratio"] >= 1.0
                    else "REDUCE",
                    "sell_ratio": result["rollover_ratio"],
                    "target_core": candidate_symbol,
                    "reason": reason_text,
                    "suggested_strategy": suggested_strategy_override
                    or result["strategy"],
                    "scenario": RolloverScenario.OPPORTUNITY_COST.value,
                    "is_manual_override_required": is_illiquid_warning,
                    "cash_impact": cash_impact,
                    "limit_price": target_spot if target_spot > 0 else None,
                    "instrument_type": instrument_class,
                    "entry_regime": entry_regime,
                }
            )
        return instructions, entry_confirmation
