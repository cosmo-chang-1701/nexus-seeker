"""動態調整狀態切換引擎 (Transition Engine)。

**職責邊界**：Regime 只負責「環境識別與進場權限許可」(Gatekeeper)，部位的
生死存亡一律回歸獨立的風控階梯 (anti_washout.py 的微觀結構出場決策矩陣)。

因此本模組只保留唯一一條真正屬於「狀態轉換」的路徑：

- 路徑 1：左側部位進化為右側動能倉——停損上移至保本點，並**授權**開立第二筆
  高動能部位 (Pyramiding)。這是在「授予新的進場權限」，而非決定既有部位存亡。

曾經存在的路徑 2 (破 Put Wall 硬停損)、路徑 3 (右側假突破平倉)、路徑 4
(推進至 Call Wall 獲利了結) 已全部移除，因為它們是出場決策矩陣既有分層的
重複實作，而且更糟的是：它們把「部位能否活下去」綁在**進場當下貼上的
entry_regime 標籤**上。實務後果是 entry_regime 從不更新，路徑 1 觸發後部位
仍掛著 REGIME_I 標籤，導致只服務 REGIME_III 的路徑 3 永遠不會套用到它，而
路徑 2 又因為部位剛站上 VWAP 而不可達——等於演化後的部位失去所有例行停損。

既有階梯本就更嚴格且不看標籤：
  - SL-結構失效在 anchor_base − 0.5×ATR₁₅ₘ 觸發，而 anchor_base 的優先序
    本就包含 put_wall，對貼著底牆的部位比舊路徑 2 的 put_wall×0.985 更早。
  - TP1/TP2/TP3 依實際價格與 Call Wall 距離分層減碼，取代舊路徑 4 的一次性
    100% 平倉；TP3 的 VWAP 帶量失守判定則涵蓋舊路徑 3。

移除後，anti_washout.py 掛載點不再需要為已標記部位開「軌道二極端瞬時停損」
與「OPTIONS IV 崩塌快速通道」兩個例外孔——階梯對所有部位一律照跑。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import logger
from .constants import _TRANSITION_PATH1_VWAP_VOLUME_MULT
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


async def evaluate_transition_for_position(
    engine: Any,
    user_id: int,
    asset: Dict[str, Any],
    metrics: Dict[str, Any],
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
    avg_cost = float(asset.get("avg_cost", 0.0))
    asset_class = str(
        asset.get("instrument_type", asset.get("asset_type", "SPOT"))
    ).upper()
    asset_class = (
        "OPTIONS" if ("OPT" in asset_class or "CONTRACT" in asset_class) else "SPOT"
    )

    spot = float(metrics.get("spot_price", 0.0))
    price_15m_close = float(metrics.get("price_15m_close", spot))
    gamma_flip = float(metrics.get("gamma_flip", 0.0))
    session_vwap = float(metrics.get("session_vwap", 0.0))
    vwap_reclaim_with_volume = bool(metrics.get("vwap_reclaim_with_volume", False))

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
            # 狀態刻意**不在此處**落地：DM 要到 portfolio_monitor 的派發迴圈才
            # 實際送出，中間還隔著通知開關、每日 dedup 與 OPTIONS_ROLLOVER_DRY_RUN
            # (預設為 true) 三道閘門。若在這裡就寫入 pyramided=True，一旦推播被
            # 抑制，這個一次性切換就永久燒掉、加碼與保本停損建議再也不會發出。
            # 改為附在指令上，由派發端在確認送出後才提交 (見 dynamic_state_patch)。
            _state_patch = {"ratchet_applied": True, "pyramided": True}
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
                    "asset_id": asset_id,
                    "dynamic_state_patch": _state_patch,
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
                    "asset_id": asset_id,
                    "dynamic_state_patch": _state_patch,
                },
            ]

    return []
