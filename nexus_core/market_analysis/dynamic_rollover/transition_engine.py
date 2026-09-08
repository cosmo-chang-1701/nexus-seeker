"""動態調整狀態切換引擎 (Transition Engine)。

管理使用者透過 `/add_trade`、`/add_holding` 手動標記 `dynamic_strategy_state`
(entry_mode="DYNAMIC") 的部位，隨市場結構從 Regime I/III 演化時的生命週期：

- 路徑 1：左側部位進化為右側動能倉 (加碼 + 停損上移保本)
- 路徑 2：左側失效硬停損 (破 Put Wall 踩踏防禦)
- 路徑 3：右側假突破防禦性平倉
- 路徑 4：推進至 Call Wall (非對稱風報比耗盡，任一 Regime 皆適用)

僅接管有標記且未 `lockout` 的部位；未標記部位完全不受影響，仍走
`anti_washout.py` 既有的通用微觀結構出場決策矩陣 (SL/TP 分層)。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from market_analysis.index_microstructure import detect_uoa_sto_call_physical_cap

from . import logger
from .constants import (
    _LEFT_ENTRY_UOA_CHASE_MIN_PREMIUM_USD,
    _LEFT_ENTRY_UOA_CHASE_RATIO_THRESHOLD,
    _TRANSITION_PATH1_VWAP_VOLUME_MULT,
    _TRANSITION_PATH2_PUT_WALL_BREACH_PCT,
    _TRANSITION_PATH4_CALL_WALL_ROOM_PCT,
)
from .models import RolloverInstruction, RolloverScenario


def build_initial_dynamic_strategy_state(
    entry_regime: str, entry_bar_low: Optional[float] = None
) -> Dict[str, Any]:
    """建立部位首次標記為動態調整引擎管理時的初始 `dynamic_strategy_state`
    JSON 結構，供 `/add_trade`、`/add_holding` 手動標記時使用。

    `entry_regime` 僅接受 `REGIME_I_LEFT_CATCH` 或 `REGIME_III_RIGHT_MOMENTUM`
    ——Regime II (混沌泥淖態) 與 Regime IV (結構封頂危機態) 由定義上不會產生
    已建立部位，呼叫端不應傳入。

    `entry_bar_low` 為標記當下最近一根已收盤 15m K 棒的低點，供切換路徑 3
    的「下破進場 K 棒低點」判定使用 (見
    `build_dynamic_strategy_state_for_symbol`)；未提供時路徑 3 自動退回僅以
    Session VWAP 判定。
    """
    return {
        "entry_mode": "DYNAMIC",
        "entry_regime": entry_regime,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_bar_low": entry_bar_low,
        "ratchet_applied": False,
        "pyramided": False,
        "pyramid_parent_asset_id": None,
        "lockout": False,
    }


async def build_dynamic_strategy_state_for_symbol(
    symbol: str, entry_regime: str
) -> Dict[str, Any]:
    """標記入口 (`/add_trade`、`/add_holding`、`/edit_*`) 專用的組裝函式：先擷取
    標記當下最近一根**已收盤** 15m K 棒的低點，再組裝初始 state。

    ⚠️ 語意界定：本平台完全沒有自動下單，標記時機完全由使用者決定，因此這裡記到
    的是「本部位進入動態調整引擎託管當下」那根 K 棒的低點，**不必然等於實際成交
    當下**那根。使用者若在進場後隔了一段時間才標記，該值會偏離真實的進場 K 棒——
    這是手動標記模型的先天限制，非實作缺陷。

    抓取失敗一律留空 (None) 而非猜測，路徑 3 會自動退回僅以 Session VWAP 判定
    (等同未記錄此欄位時的行為)。
    """
    entry_bar_low: Optional[float] = None
    try:
        from market_analysis.price_volume_alert import get_confirmed_15m_bar

        bar = await get_confirmed_15m_bar(symbol)
        if bar is not None and bar.low is not None and bar.low > 0:
            entry_bar_low = float(bar.low)
        else:
            logger.warning(
                f"[{symbol}] 標記動態調整部位時無可用的已收盤 15m K 棒，"
                "entry_bar_low 留空 (路徑3 將僅以 Session VWAP 判定)"
            )
    except Exception as e:
        logger.warning(f"[{symbol}] 標記動態調整部位時擷取進場 K 棒低點失敗: {e}")

    return build_initial_dynamic_strategy_state(
        entry_regime, entry_bar_low=entry_bar_low
    )


def set_asset_dynamic_state(user_id: int, asset_id: int, **patch: Any) -> bool:
    """讀取現有 `dynamic_strategy_state`、套用 patch 後寫回 `assets.metadata`。

    複用 `services/asset_manager.py::AssetManager` 既有的 get-merge-write
    CRUD（TRADE/HOLDING 皆存於同一張 `assets` 表，`AssetManager` 對兩者一視
    同仁，這也是 `/add_trade`/`/add_holding`/`/edit_trade`/`/edit_holding`
    實際使用的寫入層），刻意不在 `database/holdings.py`、`database/
    portfolio.py` 各自另外維護一份幾乎逐字重複的 read-merge-write helper。
    """
    from services.asset_manager import AssetManager

    manager = AssetManager()
    asset = manager.get_asset_by_id(user_id, asset_id)
    if not asset:
        return False
    current_state = dict(asset.metadata.get("dynamic_strategy_state") or {})
    current_state.update(patch)
    return manager.update_asset_metadata(
        user_id, asset_id, {"dynamic_strategy_state": current_state}
    )


def _detect_chasing_put_bto(uoa_list: list, put_wall: float) -> bool:
    """掃描 UOA 清單偵測是否存在追空踩踏 PUT BTO (strike < put_wall、
    ratio/權利金超過門檻)。與 `left_side_entry.py::
    _confirm_left_entry_condition3_no_panic_cliff` 使用完全相同的判定邏輯，
    僅語意相反：左側條件三要求「不存在」此類印花才允許進場；此處路徑 2
    要求「確實存在」此類印花才確認硬停損（追跌對沖已實際發生，而非僅結構
    破位本身）。刻意各自實作而非直接呼叫該函式——後者是一個回傳 bool 並
    同時 append reasons 的完整進場條件判定，介面不適合單純的訊號偵測用途。
    """
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
            return True
    return False


async def evaluate_transition_for_position(
    engine: Any,
    user_id: int,
    asset: Dict[str, Any],
    metrics: Dict[str, Any],
    uoa_list: Optional[List[Dict[str, Any]]] = None,
) -> List[RolloverInstruction]:
    """評估單一已標記部位的狀態切換路徑，回傳本輪應發出的 `RolloverInstruction`
    列表（可能為空 list = 本輪無動作；路徑 1 會同時回傳停損上移 HOLD 與
    OPEN_PYRAMID 加碼建議兩筆指令）。

    呼叫端 (`anti_washout.py::check_satellite_rebalancing_impl`) 負責先行
    篩選出 `dynamic_strategy_state.entry_mode == "DYNAMIC"` 且未 `lockout`
    的部位才呼叫本函式；本函式不重複這道閘門判斷。
    """
    state = asset.get("dynamic_strategy_state") or {}
    entry_regime = state.get("entry_regime")
    asset_id = asset.get("asset_id")
    symbol = str(asset.get("symbol", ""))
    current_value = float(asset.get("current_value", 0.0))
    avg_cost = float(asset.get("avg_cost", 0.0))
    asset_class = str(
        asset.get("instrument_type", asset.get("asset_type", "SPOT"))
    ).upper()
    asset_class = (
        "OPTIONS" if ("OPT" in asset_class or "CONTRACT" in asset_class) else "SPOT"
    )

    spot = float(metrics.get("spot_price", 0.0))
    price_15m_close = float(metrics.get("price_15m_close", spot))
    price_15m_open = float(metrics.get("price_15m_open", spot))
    call_wall = float(metrics.get("call_wall", 0.0))
    put_wall = float(metrics.get("put_wall", 0.0))
    gamma_flip = float(metrics.get("gamma_flip", 0.0))
    session_vwap = float(metrics.get("session_vwap", 0.0))
    vwap_reclaim_with_volume = bool(metrics.get("vwap_reclaim_with_volume", False))
    uoa_list = uoa_list if uoa_list is not None else (asset.get("uoa", []) or [])

    cash_impact = f"${current_value:,.0f}" if current_value > 0 else None

    if entry_regime == "REGIME_I_LEFT_CATCH":
        # 路徑 1：左側部位進化為右側動能倉 (加碼 + 停損上移保本)。只觸發一次
        # (pyramided 旗標)，需同時站上 Session VWAP 與 Gamma Flip 且帶量。
        if (
            not state.get("pyramided")
            and vwap_reclaim_with_volume
            and price_15m_close > 0
            and gamma_flip > 0
            and price_15m_close > gamma_flip
            and session_vwap > 0
            and price_15m_close > session_vwap
        ):
            anchor_base, _ = engine._correct_wall_topology(metrics)
            new_stop = anchor_base if avg_cost <= 0 else max(avg_cost, anchor_base)
            if asset_id is not None:
                set_asset_dynamic_state(
                    user_id, int(asset_id), ratchet_applied=True, pyramided=True
                )
            return [
                {
                    "symbol": symbol,
                    "action": "HOLD",
                    "sell_ratio": 0.0,
                    "target_core": symbol,
                    "reason": (
                        "🔀 **動態調整・路徑1：左側進化為右側動能倉**\n"
                        f"{symbol} 於 Regime I (左側接刀態) 建立的部位，15m 收盤已帶量 "
                        f"(>= {_TRANSITION_PATH1_VWAP_VOLUME_MULT}x) 站上 Session VWAP "
                        f"(${session_vwap:.2f}) 與 Gamma Flip (${gamma_flip:.2f})，"
                        "確認由做市商底牆吸籌反轉為右側動能延續。停損上移至保本點，"
                        "鎖定無風險利潤。"
                    ),
                    "suggested_strategy": f"移動止盈 (保本) @ ${new_stop:.2f}",
                    "scenario": RolloverScenario.TRANSITION_ENGINE.value,
                    "exit_tier": "TRANSITION_RATCHET",
                    "entry_regime": entry_regime,
                    "instrument_type": asset_class,
                },
                {
                    "symbol": symbol,
                    "action": "OPEN_PYRAMID",
                    "sell_ratio": 0.0,
                    "target_core": symbol,
                    "reason": (
                        "🔀 **動態調整・路徑1：順勢加碼 (Pyramiding)**\n"
                        f"{symbol} 已確認由 Regime I 演化至 Regime III (右側動能態)，"
                        "系統授權開立第二筆高動能右側部位（短天期 DTE 7-21，"
                        "順勢加速），原 Regime I 部位維持不動，僅停損上移保本。"
                    ),
                    "suggested_strategy": "新開短天期 (DTE 7-21) 右側動能部位",
                    "scenario": RolloverScenario.TRANSITION_ENGINE.value,
                    "exit_tier": "TRANSITION_PYRAMID",
                    "entry_regime": entry_regime,
                    "instrument_type": asset_class,
                },
            ]

        # 路徑 2：左側失效硬停損 (破 Put Wall 踩踏防禦)。
        #
        # ⚠️ 規格自身存在兩種說法，此處採用「風險防線」版本：
        #   (a) Regime I 矩陣的「風險防線」欄：15m 實體陰線實質跌破 Put Wall
        #       下緣 > 1.5% 即刻硬停損。
        #   (b) 狀態切換引擎路徑 2 的「觸發事件」欄：放量擊穿 Put Wall 下緣
        #       超過 1.5%，且伴隨次週期價外大額 PUT BTO 追擊。
        # (b) 比 (a) 嚴格得多——若無追空 PUT BTO 印花，破牆將完全不觸發停損，
        # 使 Regime I 部位在真實結構失效時失去保護 (該部位依定義正緊貼 Put
        # Wall，破牆 1.5% 已是實質結構失效)。「風險防線」欄是停損規則的權威
        # 定義，故以 (a) 為準：破牆幅度 + 實體陰線確認即觸發，追空 PUT BTO
        # 降為 reason 中的升級標記而非觸發前提。
        if put_wall > 0 and price_15m_close > 0:
            breach_pct = (put_wall - price_15m_close) / put_wall
            is_bearish_body = price_15m_open > 0 and price_15m_close < price_15m_open
            has_chasing_put = _detect_chasing_put_bto(uoa_list, put_wall)
            if breach_pct >= _TRANSITION_PATH2_PUT_WALL_BREACH_PCT and is_bearish_body:
                if asset_id is not None:
                    set_asset_dynamic_state(user_id, int(asset_id), lockout=True)
                return [
                    {
                        "symbol": symbol,
                        "action": "LIQUIDATE",
                        "sell_ratio": 1.0,
                        "target_core": "BOXX",
                        "reason": (
                            "🔀 **動態調整・路徑2：左側失效硬停損**\n"
                            f"{symbol} 15m 實體陰線收盤已擊穿 Put Wall (${put_wall:.2f}) "
                            f"下緣達 {breach_pct:.2%}"
                            f"（>= {_TRANSITION_PATH2_PUT_WALL_BREACH_PCT:.1%}），"
                            "做市商底牆潰堤、結構實質失效"
                            + (
                                "，且偵測到次週期價外大額 PUT BTO 追擊 (負 Gamma "
                                "螺旋式對沖拋售已實際發生)"
                                if has_chasing_put
                                else ""
                            )
                            + "。即刻全數砍倉，強制回歸現金觀望，禁止在此處二次摸底。"
                        ),
                        "suggested_strategy": "100% LIQUIDATE / STC → 轉入 BOXX 現金觀望",
                        "scenario": RolloverScenario.TRANSITION_ENGINE.value,
                        "exit_tier": "TRANSITION_LEFT_HARD_STOP",
                        "entry_regime": entry_regime,
                        "instrument_type": asset_class,
                        "cash_impact": cash_impact,
                    }
                ]

    elif entry_regime == "REGIME_III_RIGHT_MOMENTUM":
        # 路徑 3：右側假突破防禦性平倉。Regime III 的風險防線為「15m 收盤跌破
        # Session VWAP **或**下破進場 K 棒低點」，兩者為 OR 關係。
        # entry_bar_low 由標記當下擷取並存入 dynamic_strategy_state (見
        # build_dynamic_strategy_state_for_symbol)；未記錄時該子條件自動略過，
        # 退回僅以 Session VWAP 判定。
        entry_bar_low = float(state.get("entry_bar_low") or 0.0)
        is_vwap_loss = (
            session_vwap > 0 and price_15m_close > 0 and price_15m_close < session_vwap
        )
        is_entry_bar_low_break = (
            entry_bar_low > 0
            and price_15m_close > 0
            and price_15m_close < entry_bar_low
        )
        if is_vwap_loss or is_entry_bar_low_break:
            return [
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE",
                    "sell_ratio": 1.0,
                    "target_core": "VOO",
                    "reason": (
                        "🔀 **動態調整・路徑3：右側假突破防禦性平倉**\n"
                        f"{symbol} 右側突破進場後，15m 實體 K 棒無法維持強度，收盤 "
                        f"(${price_15m_close:.2f}) 已"
                        + (
                            f"跌破 Session VWAP (${session_vwap:.2f})"
                            if is_vwap_loss
                            else ""
                        )
                        + ("，且" if (is_vwap_loss and is_entry_bar_low_break) else "")
                        + (
                            f"下破進場 K 棒低點 (${entry_bar_low:.2f})"
                            if is_entry_bar_low_break
                            else ""
                        )
                        + "，確認突破失敗，右側部位無條件全平，策略切回混沌泥淖態"
                        "現金觀望。"
                    ),
                    "suggested_strategy": "100% LIQUIDATE / STC (假突破防禦性平倉)",
                    "scenario": RolloverScenario.TRANSITION_ENGINE.value,
                    "exit_tier": "TRANSITION_FALSE_BREAKOUT",
                    "entry_regime": entry_regime,
                    "instrument_type": asset_class,
                    "cash_impact": cash_impact,
                }
            ]

    # 路徑 4：推進至 Call Wall (非對稱風報比耗盡)，任一已標記 Regime 皆適用。
    if (
        entry_regime in ("REGIME_I_LEFT_CATCH", "REGIME_III_RIGHT_MOMENTUM")
        and call_wall > 0
        and spot > 0
    ):
        call_wall_room_pct = (call_wall - spot) / spot
        has_sto_call_cap = False
        try:
            has_sto_call_cap, _capping_strike = detect_uoa_sto_call_physical_cap(
                uoa_list, spot, wall_reference=call_wall
            )
        except Exception as e:
            logger.warning(f"[{symbol}] TransitionEngine 路徑4 UOA 封頂偵測失敗: {e}")

        if (
            call_wall_room_pct < _TRANSITION_PATH4_CALL_WALL_ROOM_PCT
            or has_sto_call_cap
        ):
            cap_note = "，且偵測到機構大額 STO Call 壓制" if has_sto_call_cap else ""
            return [
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE",
                    "sell_ratio": 1.0,
                    "target_core": "VOO",
                    "reason": (
                        "🔀 **動態調整・路徑4：推進至 Call Wall 獲利了結**\n"
                        f"{symbol} 現價已逼近 Call Wall (${call_wall:.2f})，剩餘空間 "
                        f"{call_wall_room_pct:.2%}（< {_TRANSITION_PATH4_CALL_WALL_ROOM_PCT:.1%}"
                        f"）{cap_note}，右側動能天花板已至，全面觸發獲利了結，所有"
                        "多頭部位平倉完畢，靜待回調重新築底。"
                    ),
                    "suggested_strategy": "100% LIQUIDATE / STC (Call Wall 獲利了結)",
                    "scenario": RolloverScenario.TRANSITION_ENGINE.value,
                    "exit_tier": "TRANSITION_TAKE_PROFIT",
                    "entry_regime": entry_regime,
                    "instrument_type": asset_class,
                    "cash_impact": cash_impact,
                }
            ]

    return []
