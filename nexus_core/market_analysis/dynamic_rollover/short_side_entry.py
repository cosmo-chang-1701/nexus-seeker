"""short_side_entry.py — 做空交易六重嚴格過濾鐵律（結構破位追空）。

⚠️ **本模組是本系統唯一的空頭方向進場路徑。** 在此之前 `trading_strategy` 的
三個值 (RIGHT_SIDE / LEFT_SIDE / DYNAMIC) 全部是做多——左側儘管技術定義與右側
相反，本質仍是逆勢均值回歸、做市商 Put Wall 底牆**接刀**（條件三算的是「向上」
回歸空間，條件六在高 IVR 時建議 Bull Call Spread / Short Put）。把左側誤讀為
做空是導入本模組時最容易犯的錯。

檔案結構完全比照 `left_side_entry.py` 與 `opportunity_cost.py` 的六重鐵律組裝
方式：六個模組層級的 `_confirm_short_entry_condition{1..6}_*` 函式 + 一個
orchestrator，共用衍生資料、`reasons` 列表原地累積、條件五/六在前四項未全數
通過時短路略過 (⏭️ 標記)，orchestrator 對六項 AND 後回傳
`(bool, reason_str, structure_directive)` 三元組——與左右側簽章完全一致，
呼叫端因此可以無差別解包。

技術定義全面鏡像右側：右側要求 15m 實體**陽線**收盤站上 Gamma Flip；做空要求
實體**陰線**收盤跌破 Gamma Flip。同一時間點一檔標的技術上幾乎不可能同時滿足
兩套條件一。

兩個子模式共用同一套六重鐵律，差異只在條件三的空間判定：

* **區間內做空**：下行獲利目標位是 Put Wall，空間門檻走
  `room_threshold.compute_dynamic_room_threshold(direction="SHORT")`。
* **破位追空**：現價已跌破 Put Wall，目標改為現價下方第一個顯著負 GEX 節點，
  空間門檻走 `room_threshold.evaluate_next_strike_space()`（>= 2.0 × ATR₁D），
  收復剛跌破的 Put Wall 即論點失效。
"""

import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from market_analysis.atr_utils import compute_atr_15m_from_df
from market_analysis.index_microstructure import estimate_symbol_gamma_flip
from market_analysis.room_threshold import (
    compute_dynamic_room_threshold,
    evaluate_next_strike_space,
    evaluate_wall_buffer,
)

from . import logger
from ._shared import resolve_room_threshold_inputs
from .models import ShortEntryEvaluation
from .constants import (
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _SHORT_ENTRY_CANDIDATE_MIN_DTE,
    _SHORT_ENTRY_IVR_SPREAD_THRESHOLD,
    _SHORT_ENTRY_UOA_CATCH_MIN_PREMIUM_USD,
    _SHORT_ENTRY_UOA_CATCH_RATIO_THRESHOLD,
    _SHORT_ENTRY_UOA_MIN_DTE,
    _SHORT_ENTRY_UOA_MIN_NOTIONAL_USD,
    _SHORT_ENTRY_UOA_MIN_RATIO,
    _SHORT_ENTRY_VOLUME_SURGE_MULTIPLIER,
)
from .structural_signals import _scan_resistance_wall_above_spot


async def _confirm_short_entry_condition1_breakdown(
    candidate_symbol: str,
    target_spot: float,
    gex_profile_data: Any,
    session_vwap: float,
    reasons: list,
    net_gex: Optional[float] = None,
    df_15m: Optional[Any] = None,
    atr_15m: Optional[float] = None,
) -> Tuple[bool, Optional[Any]]:
    """做空條件一：結構性放量破位確認（15m 實體陰線收盤跌破 Gamma Flip + 放量
    + 跌破 Session VWAP）。

    Gamma Flip 邊界處理與 Fallback（完全鏡像右側條件一的對稱處理）：
      * 全鏈 Net GEX > 0：做市商處於正 Gamma 自穩定狀態，會買跌賣漲吸收波動，
        結構性多頭，直接判定未通過——這正是右側「Net GEX < 0 直接不通過」的鏡像。
      * 全鏈 Net GEX < 0 但無 Flip 交叉點：全區間已處於負 Gamma 順勢助跌泥淖，
        強求 Flip 交叉門檻會造成誤殺，改以跌破 `VWAP − 0.5 × ATR₁₅ₘ` 作為
        破位確認的替代標準。
      * Net GEX == 0 或數據缺失：fail-safe 判定未通過。

    15m K 線一律先經 `trim_to_confirmed_15m_bars()` 截斷至最近一根**已收盤**
    K 棒，理由同左側條件一：尚在成型的 K 棒其成交量只累積了一部分，「放量」
    判定會系統性低估量能；且實體陰線的 Close 仍在變動，形態判定會失真。

    回傳 `(是否通過, 本次使用的 df_15m)`，供呼叫端重用同一份 frame。
    """
    if target_spot <= 0:
        reasons.append("做空條件一❌：candidate 現價無效")
        return False, df_15m

    gex_profile = (
        gex_profile_data.get("gex_profile")
        if isinstance(gex_profile_data, dict)
        else None
    )

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

    gamma_flip = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )

    is_fallback_mode = False
    if gamma_flip <= 0:
        if effective_net_gex > 0:
            reasons.append(
                "做空條件一❌：全域 Long Gamma 自穩定 (Net GEX > 0 且無 Flip 交叉點)，"
                "做市商買跌賣漲吸收波動，結構性多頭直接不通過"
            )
            return False, df_15m
        elif effective_net_gex < 0:
            is_fallback_mode = True
        else:
            reasons.append(
                "做空條件一❌：無法估算 Gamma Flip 門檻 (GEX Profile 無交叉點且無明確方向)"
            )
            return False, df_15m

    if df_15m is None:
        try:
            from services import market_data_service

            df_15m = await market_data_service.get_history_df(
                candidate_symbol, period="5d", interval="15m", force_refresh=True
            )
        except Exception as e:
            df_15m = None
            logger.warning(f"[{candidate_symbol}] 做空條件一 15m K 線抓取失敗: {e}")

    from market_analysis.price_volume_alert import trim_to_confirmed_15m_bars

    df_confirmed = trim_to_confirmed_15m_bars(df_15m)
    if df_confirmed is None or len(df_confirmed) < _ENTRY_VOLUME_LOOKBACK_BARS + 1:
        reasons.append("做空條件一❌：15m 已收盤 K 線資料不足，無法確認破位")
        return False, df_15m

    if atr_15m is None:
        atr_15m = compute_atr_15m_from_df(df_confirmed)

    if session_vwap <= 0 or math.isnan(session_vwap):
        reasons.append("做空條件一❌：Session VWAP 抓取失敗，無法確認破位")
        return False, df_15m

    if is_fallback_mode:
        if atr_15m <= 0 or math.isnan(atr_15m):
            reasons.append(
                "做空條件一❌：全域 Short Gamma Fallback 模式下 ATR₁₅ₘ 無法取得，"
                "無法計算替代破位門檻"
            )
            return False, df_15m
        breakdown_level = session_vwap - 0.5 * atr_15m
        level_desc = f"VWAP ${session_vwap:.2f} - 0.5×ATR₁₅ₘ ${atr_15m:.2f} (Fallback)"
    else:
        breakdown_level = gamma_flip
        level_desc = f"Gamma Flip ${gamma_flip:.2f}"

    last_bar = df_confirmed.iloc[-1]
    lookback_bars = df_confirmed.iloc[-(_ENTRY_VOLUME_LOOKBACK_BARS + 1) : -1]
    open_val = float(last_bar["Open"])
    close_val = float(last_bar["Close"])
    volume_val = float(last_bar["Volume"])
    avg_volume = float(lookback_bars["Volume"].mean())

    is_below_level = close_val < breakdown_level
    is_bearish_candle = close_val < open_val
    is_volume_surge = (
        avg_volume > 0
        and volume_val >= avg_volume * _SHORT_ENTRY_VOLUME_SURGE_MULTIPLIER
    )
    is_below_vwap = close_val < session_vwap

    c1_passed = (
        is_below_level and is_bearish_candle and is_volume_surge and is_below_vwap
    )
    reasons.append(
        f"做空條件一{'✅' if c1_passed else '❌'}：15m 收盤 ${close_val:.2f} "
        f"{'<' if is_below_level else '>='} {level_desc}，"
        f"{'跌破' if is_below_vwap else '未跌破'} VWAP ${session_vwap:.2f}，"
        f"量 {volume_val:.0f} vs 門檻 "
        f"{avg_volume * _SHORT_ENTRY_VOLUME_SURGE_MULTIPLIER:.0f}，"
        f"K棒{'實體陰線' if is_bearish_candle else '非陰線 (排除陽線縮量假破位)'}"
    )
    return c1_passed, df_15m


def _confirm_short_entry_condition2_resistance_wall(
    candidate_symbol: str,
    gex_profile_data: Any,
    target_spot: float,
    reasons: list,
    atr_15m: float = 0.0,
    atr_1d: float = 0.0,
) -> Tuple[bool, float]:
    """做空條件二：做市商負 Gamma 頂牆完好，且緩衝距離落在動態雙邊界之內。

    物理定義約束與右側條件二完全鏡像——阻力位在物理定義上必須位於現價**上方**：

        Resistance Wall = argmax_{K > Spot} (Net GEX(K))

    透過 `_scan_resistance_wall_above_spot()` 把掃描範圍強制約束在現價上方，
    避免把下方的支撐底牆誤當成上方的壓制天花板（右側條件二防的是反向的同一
    種錯誤）。現價上方無任何正 GEX 峰值、或曝險低於 GEX_THIN_WALL_THRESHOLD
    薄紙牆門檻，直接判定未通過。

    緩衝判定走 `evaluate_wall_buffer(profile="SHORT")`，量的是**停損距離**
    (現價到 ResistanceWall + 0.5×ATR₁₅ₘ)：下界 2.5×ATR₁₅ₘ 防空單停損落在日內
    雜訊帶內——任何反抽都會先掃穿停損；上界為絕對 8% 的風險兜底。

    回傳 `(是否通過, resistance_wall)`，後者供條件三與出場矩陣的錨點沿用。
    """
    resistance_wall, resistance_gex = _scan_resistance_wall_above_spot(
        candidate_symbol,
        gex_profile_data if isinstance(gex_profile_data, dict) else None,
        spot=target_spot,
    )
    if resistance_wall <= 0 or resistance_gex <= 0:
        reasons.append(
            "做空條件二❌：未偵測到有效正 Gamma 壓制頂牆 (現價上方無正 GEX 峰值)"
        )
        return False, 0.0
    if target_spot <= 0:
        reasons.append("做空條件二❌：candidate 現價無效，無法計算頂牆距離")
        return False, 0.0

    buffer = evaluate_wall_buffer(
        target_spot, resistance_wall, atr_15m, atr_1d, profile="SHORT"
    )
    degrade_suffix = f"｜⚠️ {buffer.degrade_reason}" if buffer.degrade_reason else ""

    if buffer.state == "TOO_TIGHT" and buffer.buffer_pct <= 0:
        reasons.append(
            f"做空條件二❌：現價 ${target_spot:.2f} >= 正 Gamma 壓制頂牆 "
            f"${resistance_wall:.2f}{degrade_suffix}"
        )
    elif buffer.state == "TOO_TIGHT":
        bound = f"{buffer.min_pct:.2%}" if buffer.min_pct is not None else "下界"
        reasons.append(
            f"做空條件二❌：現價 ${target_spot:.2f} 距正 Gamma 壓制頂牆 "
            f"${resistance_wall:.2f}，停損距離 {buffer.buffer_pct:.2%}（< {bound} "
            f"= 2.5×ATR₁₅ₘ 下界，空單停損落在日內雜訊帶內，易遭反抽掃損）"
            f"{degrade_suffix}"
        )
    elif buffer.state == "TOO_WIDE":
        bound = f"{buffer.max_pct:.2%}" if buffer.max_pct is not None else "上界"
        reasons.append(
            f"做空條件二❌：現價 ${target_spot:.2f} 距正 Gamma 壓制頂牆 "
            f"${resistance_wall:.2f}，停損距離 {buffer.buffer_pct:.2%}（> {bound} "
            f"絕對風險上限，停損距現價過遠）{degrade_suffix}"
        )
    else:
        reasons.append(
            f"做空條件二✅：現價 ${target_spot:.2f} 距正 Gamma 壓制頂牆 "
            f"${resistance_wall:.2f}，停損距離 {buffer.buffer_pct:.2%}（落在緩衝甜蜜點），"
            f"頂牆厚度 ${resistance_gex:,.0f}{degrade_suffix}"
        )
    return buffer.passed, resistance_wall


def _find_next_negative_gex_peak(gex_profile_data: Any, target_spot: float) -> float:
    """找出現價下方第一個顯著負 GEX 節點（做市商順勢助跌拋壓的落點）。

    破位追空的目標位：現價已跌破 Put Wall 後，下一個會讓做市商被迫加速賣出
    對沖的履約價。取「現價下方、GEX 為負、且絕對曝險最大」的履約價；無符合者
    回傳 0.0，交由呼叫端 fail-closed。
    """
    if not isinstance(gex_profile_data, dict) or target_spot <= 0:
        return 0.0
    gex_profile = gex_profile_data.get("gex_profile")
    if not isinstance(gex_profile, dict):
        return 0.0
    best_strike = 0.0
    best_magnitude = 0.0
    for k, v in gex_profile.items():
        try:
            strike = float(k)
            val = float(v)
        except (ValueError, TypeError):
            continue
        if not (math.isfinite(strike) and math.isfinite(val)):
            continue
        if (
            strike >= target_spot
            or math.isclose(strike, target_spot, abs_tol=1e-4)
            or val >= 0
        ):
            continue
        if abs(val) > best_magnitude:
            best_magnitude = abs(val)
            best_strike = strike
    return best_strike


def _confirm_short_entry_condition3_downside_room(
    uoa_list: list,
    gex_profile_data: Any,
    target_spot: float,
    resistance_wall: float,
    reasons: list,
    atr_15m: float = 0.0,
    atr_1d: float = 0.0,
) -> Tuple[bool, str]:
    """做空條件三：下行獲利空間充足 + 無主力大額接刀。

    子模式自動分流（依現價是否已跌破 Put Wall）：

    * **區間內做空** (Spot > PutWall)：目標位 = Put Wall，
      `Reward_down = (Spot − PutWall)/Spot` 須 >= 動態門檻
      `max(2.2 × Risk, 1.5 × ATR₁D/Spot, 3.5%)`，其中
      `Risk = ((ResistanceWall + 0.5 × ATR₁₅ₘ) − Spot)/Spot`（停損設在頂牆上方，
      墊片與出場矩陣 `_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT` 同步）。
    * **破位追空** (Spot <= PutWall)：Put Wall 已被打穿，目標改為現價下方第一個
      顯著負 GEX 節點，空間要求改走公式 C（>= 2.0 × ATR₁D）。「收復 Put Wall」
      是論點失效的訊號；實際倉位停損仍以頂牆錨點計算 (見
      short_entry_sizing.py)，與出場矩陣真正會執行的停損一致。

    接刀過濾器鏡像左側條件三的「追空踩踏」偵測，方向反轉為 **PUT STO**：主力
    大額賣出 PUT 是在為下跌提供流動性接盤（做市商據此買進現貨對沖），代表
    有人正在承接，追空的拋壓路徑會被墊住。

    回傳 `(是否通過, 子模式標籤)`。
    """
    put_wall = (
        float(gex_profile_data.get("put_wall", 0.0) or 0.0)
        if isinstance(gex_profile_data, dict)
        else 0.0
    )

    has_catch_bid = False
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "PUT":
            continue
        if "STO" not in str(entry.get("action", "")):
            continue
        ratio = float(entry.get("paced_ratio", entry.get("ratio", 0.0)) or 0.0)
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        strike = float(entry.get("strike", 0.0) or 0.0)
        if (
            ratio > _SHORT_ENTRY_UOA_CATCH_RATIO_THRESHOLD
            and notional_value >= _SHORT_ENTRY_UOA_CATCH_MIN_PREMIUM_USD
            and 0 < strike <= target_spot
        ):
            has_catch_bid = True
            break

    if has_catch_bid:
        reasons.append(
            f"做空條件三❌：偵測到主力大額 PUT STO 接刀 "
            f"(ratio>{_SHORT_ENTRY_UOA_CATCH_RATIO_THRESHOLD}x、"
            f"權利金>=${_SHORT_ENTRY_UOA_CATCH_MIN_PREMIUM_USD:,.0f}、strike<=現價)，"
            f"下方有人承接，拋壓路徑被墊住"
        )
        return False, "N/A"

    if put_wall <= 0 or target_spot <= 0:
        reasons.append("做空條件三❌：Put Wall 或現價資料缺失，無法計算下行空間")
        return False, "N/A"

    if target_spot > put_wall:
        # --- 子模式 A：區間內做空，目標位 = Put Wall ---
        room_pct = (target_spot - put_wall) / target_spot
        room = compute_dynamic_room_threshold(
            target_spot, resistance_wall, atr_15m, atr_1d, direction="SHORT"
        )
        degrade_suffix = f"｜⚠️ {room.degrade_reason}" if room.degrade_reason else ""
        passed = room_pct >= room.threshold_pct
        reasons.append(
            f"做空條件三{'✅' if passed else '❌'}[區間內]：下行空間 {room_pct:+.2%} "
            f"{'>=' if passed else '<'} 動態門檻 {room.threshold_pct:.2%}"
            f"（目標 Put Wall ${put_wall:.2f}）{degrade_suffix}"
        )
        return passed, "區間內做空"

    # --- 子模式 B：破位追空，目標位 = 次級負 GEX 節點 ---
    next_peak = _find_next_negative_gex_peak(gex_profile_data, target_spot)
    passed, space_pct, required_pct = evaluate_next_strike_space(
        target_spot, next_peak, atr_1d
    )
    if next_peak <= 0:
        reasons.append(
            f"做空條件三❌[破位追空]：現價 ${target_spot:.2f} 已跌破 Put Wall "
            f"${put_wall:.2f}，但下方無顯著負 Gamma 節點可作磁吸目標，fail-closed"
        )
        return False, "破位追空"
    if required_pct is None:
        reasons.append(
            "做空條件三❌[破位追空]：ATR₁D 無法取得，無法計算次級節點空間要求，"
            "fail-closed"
        )
        return False, "破位追空"
    reasons.append(
        f"做空條件三{'✅' if passed else '❌'}[破位追空]：現價已跌破 Put Wall "
        f"${put_wall:.2f}，至次級負 Gamma 節點 ${next_peak:.2f} 空間 {space_pct:.2%} "
        f"{'>=' if passed else '<'} {required_pct:.2%} (2.0×ATR₁D)"
        f"｜收復 Put Wall ${put_wall:.2f} 即論點失效"
    )
    return passed, "破位追空"


def _confirm_short_entry_condition4_smart_money_pressure(
    uoa_list: list, target_spot: float, reasons: list
) -> bool:
    """做空條件四：主力跨週期賣壓認證與雜訊過濾。

    接受兩種主力賣壓訊號（任一成立即通過），皆須同時滿足 DTE / ratio / 名目
    金額三門檻：

    * **PUT BTO**：主力買進 PUT 押注下跌。排除 strike 遠低於現價的深度價外
      樂透單（`strike >= 現價 × 0.85`）——那是低成本尾部投機，不代表機構
      對方向有真實信心，與右側條件四排除「深實值避險單」的用意對稱。
    * **CALL STO**：主力賣出 CALL 築頂。須 `strike >= 現價`，即在上方賣壓築頂，
      而非賣出價內 Call 的平倉單。

    ⚠️ 規格中的「須在過去 N 小時內偵測到」時間窗與左側條件四同樣**無法實作**：
    UOA 清單來自對選擇權鏈的當日累計快照，每一列都不帶成交時間戳。
    """
    now = datetime.now().date()
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        opt_type = str(entry.get("type", "")).upper()
        action = str(entry.get("action", "")).upper()
        ratio = float(entry.get("paced_ratio", entry.get("ratio", 0.0)) or 0.0)
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        strike = float(entry.get("strike", 0.0) or 0.0)
        try:
            expiry = str(entry.get("expiry", ""))
            dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - now).days
        except (ValueError, TypeError):
            continue

        if (
            ratio < _SHORT_ENTRY_UOA_MIN_RATIO
            or notional_value < _SHORT_ENTRY_UOA_MIN_NOTIONAL_USD
            or dte < _SHORT_ENTRY_UOA_MIN_DTE
        ):
            continue

        if opt_type == "PUT" and "BTO" in action and strike >= target_spot * 0.85:
            reasons.append(
                f"做空條件四✅：主力 PUT BTO 方向性押注 DTE={dte}、ratio={ratio:.2f}x、"
                f"權利金 ${notional_value:,.0f} @ ${strike:.2f}"
            )
            return True
        if opt_type == "CALL" and "STO" in action and strike >= target_spot:
            reasons.append(
                f"做空條件四✅：主力 CALL STO 上方築頂 DTE={dte}、ratio={ratio:.2f}x、"
                f"權利金 ${notional_value:,.0f} @ ${strike:.2f}"
            )
            return True

    reasons.append(
        f"做空條件四❌：未偵測到符合門檻的主力 PUT BTO 方向性押注或 CALL STO 築頂 "
        f"(DTE>={_SHORT_ENTRY_UOA_MIN_DTE}、ratio>={_SHORT_ENTRY_UOA_MIN_RATIO}、"
        f"Premium>=${_SHORT_ENTRY_UOA_MIN_NOTIONAL_USD:,.0f})"
    )
    return False


async def _confirm_short_entry_condition5_macro_earnings_gate(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    reasons: list,
) -> bool:
    """做空條件五：總經與財報事件安全閥。

    **完全重用** `opportunity_cost.py::_confirm_entry_condition5_macro_earnings_gate`
    ——財報前的二元事件風險對做多做空同樣致命（空單遇上優於預期的財報會被跳空
    軋空），門檻校準沒有理由分家。刻意不額外疊加 VIX 期限結構倒掛檢查：左側
    把倒掛視為「流動性凍結、不可接刀」，但對做空而言倒掛正是順風，兩者語意
    相反，硬套會方向性地誤殺。

    前四項未全數通過時短路略過，理由同左右側：避免對已確定失敗的整體結果仍
    發動真實 I/O。
    """
    if not prior_conditions_passed:
        reasons.append("做空條件五⏭️：前四項未全數通過，略過總經/財報安全閥檢查")
        return True

    from .opportunity_cost import _confirm_entry_condition5_macro_earnings_gate

    shared_reasons: list = []
    c5_passed, _days_to_earnings = await _confirm_entry_condition5_macro_earnings_gate(
        candidate_symbol, True, shared_reasons, direction="SHORT"
    )
    for r in shared_reasons:
        reasons.append(r.replace("條件五", "做空條件五", 1))
    return c5_passed


async def _confirm_short_entry_condition6_candidate_dte_ivr(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    target_ivr: float,
    reasons: list,
) -> Tuple[bool, Optional[str]]:
    """做空條件六：Candidate 自身效期防禦與 IVR 結構分流。

    DTE 門檻 (`_SHORT_ENTRY_CANDIDATE_MIN_DTE` = 14) 高於右側的 1、接近左側的
    21：破位後常有劇烈反抽回測前低/前高，空單需要承受震盪期，短天期會在回測
    過程中被 Theta 與 Vega 雙殺。

    IVR 分流方向與左右側一致——高隱波下不當單腳買方：
      * IVR <= 50：Long Put（輕度 OTM）
      * IVR >  50：Bear Call Spread，改當賣方收取恐慌溢價，避免破位當下買進
        已被灌滿恐慌溢價的 Put，事後隨反抽遭 IV Crush 的 Vega 崩塌。
    """
    if not prior_conditions_passed:
        reasons.append("做空條件六⏭️：前五項未全數通過，略過 candidate 效期檢查")
        return True, None

    try:
        from services import market_data_service

        expiries = await market_data_service.get_all_option_expiries(candidate_symbol)
    except Exception as e:
        expiries = []
        logger.warning(f"[{candidate_symbol}] 做空條件六選擇權到期日清單抓取失敗: {e}")

    if not expiries:
        reasons.append("做空條件六❌：無法取得標的最近效期選擇權到期日清單")
        return False, None

    try:
        nearest_expiry_dt = datetime.strptime(expiries[0], "%Y-%m-%d").date()
        dte_nearest = (nearest_expiry_dt - datetime.now().date()).days
    except (ValueError, TypeError) as e:
        reasons.append(f"做空條件六❌：標的最近效期到期日解析失敗: {e}")
        return False, None

    if dte_nearest < _SHORT_ENTRY_CANDIDATE_MIN_DTE:
        reasons.append(
            f"做空條件六❌：標的最近效期 {expiries[0]} DTE={dte_nearest} "
            f"低於門檻 >={_SHORT_ENTRY_CANDIDATE_MIN_DTE}"
        )
        return False, None

    if target_ivr <= _SHORT_ENTRY_IVR_SPREAD_THRESHOLD:
        structure_directive = "Long Put (輕度 OTM)"
    else:
        structure_directive = (
            "Bear Call Spread (IVR 過高，改當賣方收取恐慌溢價，避免 Vega 崩塌)"
        )

    reasons.append(
        f"做空條件六✅：標的最近效期 {expiries[0]} DTE={dte_nearest}，"
        f"IVR={target_ivr:.1f}% -> {structure_directive}"
    )
    return True, structure_directive


async def evaluate_short_entry(
    candidate_symbol: str,
    candidate_radar: Dict[str, Any],
    target_spot: float,
    df_15m: Optional[Any] = None,
    session_vwap: Optional[float] = None,
    atr_15m: Optional[float] = None,
) -> ShortEntryEvaluation:
    """做空交易進場訊號六重嚴格過濾鐵律 orchestrator（完整評估版）。

    除了「過不過」之外，一併回傳六重鐵律評估過程中本來就已算出的價位中間值
    (頂牆、Put Wall、Gamma Flip、次級負 GEX 節點、ATR)，供 SHORT_ENTRY 情境
    直接建立進場／停損／目標價位，不重新抓取、不重算——確保「確認」與「下單
    價位」建立在同一份資料快照上。

    `df_15m` / `session_vwap` / `atr_15m` 可選：`regime_classifier.py` 路由至
    Regime V 時會原樣傳入分類判定階段已抓取的同一份資料 (見 RegimeMarketData)。
    """
    reasons: list[str] = []

    gex_profile_data = candidate_radar.get("gex_profile_data") or {}
    uoa_list = candidate_radar.get("uoa") or []
    target_ivr = float(
        candidate_radar.get("iv_metrics", {}).get("iv_rank", 0.0)
        if candidate_radar.get("iv_metrics")
        else 0.0
    )
    net_gex: Optional[float] = None
    if isinstance(gex_profile_data, dict):
        try:
            net_gex = float(gex_profile_data.get("net_gex", 0.0) or 0.0)
        except (ValueError, TypeError):
            net_gex = None

    if session_vwap is None:
        try:
            from market_analysis.vwap_utils import fetch_session_vwap

            session_vwap = await fetch_session_vwap(candidate_symbol)
        except Exception as e:
            session_vwap = 0.0
            logger.warning(
                f"[{candidate_symbol}] 做空訊號 Session VWAP 共用抓取失敗: {e}"
            )

    c1_passed, _df_15m_used = await _confirm_short_entry_condition1_breakdown(
        candidate_symbol,
        target_spot,
        gex_profile_data,
        session_vwap,
        reasons,
        net_gex=net_gex,
        df_15m=df_15m,
        atr_15m=atr_15m,
    )

    # 動態空間門檻所需輸入在此一次解析，同時餵給條件二與條件三。
    # 刻意不傳 target_spot（不啟動 PutWall 重錨）：做空的停損牆是現價**上方**的
    # Call Wall，由條件二的 _scan_resistance_wall_above_spot 自行解析並以
    # resistance_wall 傳給條件三；resolve_room_threshold_inputs 的重錨邏輯只處理
    # 支撐底牆，對做空不適用，故此處 _pw 一律丟棄，只取兩個 ATR。
    _pw, _atr15, _atr1d = await resolve_room_threshold_inputs(
        candidate_symbol,
        candidate_radar,
        gex_profile_data,
        _df_15m_used if _df_15m_used is not None else df_15m,
    )
    if atr_15m is not None and atr_15m > 0:
        _atr15 = atr_15m

    c2_passed, resistance_wall = _confirm_short_entry_condition2_resistance_wall(
        candidate_symbol,
        gex_profile_data,
        target_spot,
        reasons,
        atr_15m=_atr15,
        atr_1d=_atr1d,
    )
    c3_passed, sub_mode = _confirm_short_entry_condition3_downside_room(
        uoa_list,
        gex_profile_data,
        target_spot,
        resistance_wall,
        reasons,
        atr_15m=_atr15,
        atr_1d=_atr1d,
    )
    c4_passed = _confirm_short_entry_condition4_smart_money_pressure(
        uoa_list, target_spot, reasons
    )
    c1_to_c4 = c1_passed and c2_passed and c3_passed and c4_passed
    c5_passed = await _confirm_short_entry_condition5_macro_earnings_gate(
        candidate_symbol, c1_to_c4, reasons
    )
    (
        c6_passed,
        structure_directive,
    ) = await _confirm_short_entry_condition6_candidate_dte_ivr(
        candidate_symbol,
        c1_to_c4 and c5_passed,
        target_ivr,
        reasons,
    )

    all_passed = c1_to_c4 and c5_passed and c6_passed

    # 價位中間值：全部是純計算 (零 I/O)，重用條件函式已用過的同一份輸入。
    put_wall = _pw
    call_wall = 0.0
    gamma_flip = 0.0
    if isinstance(gex_profile_data, dict):
        try:
            put_wall = float(gex_profile_data.get("put_wall", 0.0) or 0.0) or _pw
            call_wall = float(gex_profile_data.get("call_wall", 0.0) or 0.0)
        except (ValueError, TypeError):
            call_wall = 0.0
        gex_profile = gex_profile_data.get("gex_profile")
        if isinstance(gex_profile, dict) and target_spot > 0:
            gamma_flip = estimate_symbol_gamma_flip(gex_profile, target_spot)
    next_negative_node = (
        _find_next_negative_gex_peak(gex_profile_data, target_spot)
        if sub_mode == "破位追空"
        else 0.0
    )

    evaluation = ShortEntryEvaluation(
        all_passed=bool(all_passed),
        reason=" | ".join(reasons),
        structure_directive=structure_directive,
        sub_mode=sub_mode,
        conditions=(
            c1_passed,
            c2_passed,
            c3_passed,
            c4_passed,
            c5_passed if c1_to_c4 else None,
            c6_passed if (c1_to_c4 and c5_passed) else None,
        ),
        spot=float(target_spot),
        resistance_wall=float(resistance_wall),
        call_wall=call_wall,
        put_wall=float(put_wall),
        gamma_flip=float(gamma_flip),
        next_negative_node=float(next_negative_node),
        net_gex=net_gex,
        session_vwap=float(session_vwap or 0.0),
        atr_15m=float(_atr15 or 0.0),
        atr_1d=float(_atr1d or 0.0),
        ivr=target_ivr,
    )
    from market_analysis.evaluation_recorder import record_short_evaluation

    record_short_evaluation(evaluation, candidate_symbol)
    return evaluation


async def _confirm_short_entry_signal(
    candidate_symbol: str,
    candidate_radar: Dict[str, Any],
    target_spot: float,
    df_15m: Optional[Any] = None,
    session_vwap: Optional[float] = None,
    atr_15m: Optional[float] = None,
) -> Tuple[bool, str, Optional[str]]:
    """`evaluate_short_entry` 的三元組包裝。

    簽章與 `_confirm_left_entry_signal` / `_confirm_entry_signal` 完全一致，
    呼叫端可無差別解包 `(all_passed, reason_str, structure_directive)`。
    需要價位的呼叫端 (SHORT_ENTRY 情境) 請直接呼叫 `evaluate_short_entry`。
    """
    ev = await evaluate_short_entry(
        candidate_symbol,
        candidate_radar,
        target_spot,
        df_15m=df_15m,
        session_vwap=session_vwap,
        atr_15m=atr_15m,
    )
    return ev.all_passed, ev.reason, ev.structure_directive
