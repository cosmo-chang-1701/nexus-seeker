"""顧問模式 (Advisory Mode)：把 B&H 持倉的停利／停損由「指揮」改為「顧問」。

背景
----
個股持倉預設 ``asset_class = SATELLITE``，核心衛星再平衡 (SATELLITE_REBALANCE)
會對 Buy & Hold 使用者的持倉每輪輸出減碼／換股指令，方向與其策略相反。顧問模式
只告知位階（已抵達目標區／結構失效），**不**輸出任何賣出動作。

開關
----
帳戶層 ``user_settings.portfolio_mode`` 與單檔 ``assets.metadata.advisory_only``
（三態）由派發端 (``cogs/trading/portfolio_monitor.py``) 解析成單一布林，僅在為
True 時寫入 ``asset["advisory_only"]``。本模組只讀該旗標，因此預設 (COMMAND、
未覆寫) 下引擎行為與改動前逐位元一致。

設計約束
--------
* 葉模組：只依賴 stdlib、``models``、``room_threshold``，避免循環相依。
* 只作用於**多頭現貨**：選擇權條目永不帶 ``advisory_only``；空頭現貨的風險語意
  相反，一律維持指令模式（不因顧問而隱藏空頭風險）。
* 轉換於 ``anti_washout`` 迴圈內完成而非回傳前後置過濾——指令上沒有 spot／
  call wall，回傳前無法為 TP 摺疊算出目標價。
* MARGIN_DEFENSE 為帳戶生存線，**不受**顧問模式影響（本模組不被其匯入）。
* ⚠️ ``RolloverInstruction.action`` 是純 str，新增的 ``"ADVISORY"`` 值不會被 mypy
  追蹤；防護在 ``tests/unit/test_advisory_mode.py``。
"""

from typing import Any, Dict, Mapping, Optional

from market_analysis.room_threshold import resolve_effective_target

from . import logger
from .models import AdvisoryPlan, RolloverInstruction

ADVISORY_ACTION = "ADVISORY"

# 結構失效：唯一「結構可能真的壞了」的訊號 → 保留為顧問告知。
_STRUCTURE_TIERS = frozenset({"SL_STRUCTURAL", "EXTREME_TICK_BREACH"})
# 目標區：TP1~TP3 摺疊為單一「已抵達目標區」告知。
_TARGET_TIERS = frozenset({"TP1", "TP2", "TP3"})

# 轉為顧問時必須清除的「指令內容」欄位（含引擎內部狀態提交欄位）。
_COMMAND_ONLY_KEYS = (
    "suggested_strategy",
    "trigger_condition_text",
    "cash_impact",
    "limit_price",
    "extreme_stop_loss",
    "extreme_breach_detail_block",
    "dynamic_state_patch",
    "asset_id",
    "sell_action",
    "buy_action_label",
)


def is_advisory_asset(asset: Mapping[str, Any]) -> bool:
    """該持倉是否處於顧問模式（嚴格 ``is True``，MagicMock／字串皆不誤判）。

    僅多頭現貨適用：空頭部位（股數為負）與選擇權一律維持指令模式。
    """
    if asset.get("advisory_only") is not True:
        return False
    try:
        return float(asset.get("quantity", 0.0) or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _structure_reason(symbol: str, exit_tier: str, spot: float, stop: float) -> str:
    tier_label = (
        "極端瞬時停損線" if exit_tier == "EXTREME_TICK_BREACH" else "結構停損線"
    )
    stop_text = f"（{tier_label} `${stop:,.2f}`）" if stop > 0 else ""
    return (
        f"🧭 **顧問模式・結構失效告知**\n"
        f"`{symbol}` 現價 `${spot:,.2f}` 已失守做市商結構支撐{stop_text}。\n"
        f"本部位為顧問模式，**僅告知位階，不建議動作**——這可能是基本面未變的洗盤，"
        f"是否處置由你依自己的論點決定。"
    )


def _target_reason(
    symbol: str, spot: float, call_wall: float, target: float, is_blue_sky: bool
) -> str:
    wall_text = f"Call Wall `${call_wall:,.2f}`、" if call_wall > 0 else ""
    sky_text = "（已進入歷史新高區，目標為 ATR 外推）" if is_blue_sky else ""
    return (
        f"🧭 **顧問模式・已抵達目標區**\n"
        f"`{symbol}` 現價 `${spot:,.2f}` 已貼近上方壓力區：{wall_text}"
        f"有效目標 `${target:,.2f}`{sky_text}。\n"
        f"本部位為顧問模式，**不建議減碼**，決定權交還給你。"
    )


async def build_advisory_instruction(
    ins: RolloverInstruction,
    asset: Mapping[str, Any],
    metrics: Mapping[str, Any],
    stop_loss: float,
) -> Optional[RolloverInstruction]:
    """把一筆 SATELLITE_REBALANCE 指令轉為顧問指令；回傳 ``None`` 代表丟棄。

    規則（依序）：

    1. ``action == "HOLD"``（灰帶、TP1 趨勢豁免、動態保本、淨額化為 0）→ 丟棄。
       這些 HOLD 帶有 ``dynamic_state_patch`` (棘輪停損) 的情況也一併丟棄：棘輪
       不再提交只會讓停損維持在結構位、較寬鬆，對 B&H 是可接受的取捨。
    2. ``exit_tier`` 為 SL_STRUCTURAL／EXTREME_TICK_BREACH → 結構失效告知。
    3. ``exit_tier`` 為 TP1／TP2／TP3 → 已抵達目標區告知（唯一會發網路請求的分支）。
    4. 其餘（SL_REGIME_FLIP／SL_WHALE_PUT／比例控管 ``None``）→ 丟棄，戰術性
       雜訊。
    """
    if ins.get("action") == "HOLD":
        return None

    exit_tier = ins.get("exit_tier")
    symbol = str(ins["symbol"])
    spot = float(metrics.get("spot_price", asset.get("spot_price", 0.0)) or 0.0)

    plan: AdvisoryPlan
    if exit_tier in _STRUCTURE_TIERS:
        plan = {
            "kind": "STRUCTURE_FAILURE",
            "spot": spot,
            "stop_loss": float(stop_loss) if stop_loss and stop_loss > 0 else None,
            "call_wall": None,
            "target": None,
            "is_blue_sky": False,
        }
        reason = _structure_reason(
            symbol, str(exit_tier), spot, float(stop_loss or 0.0)
        )
    elif exit_tier in _TARGET_TIERS:
        call_wall = float(metrics.get("call_wall", 0.0) or 0.0)
        atr_1d = float(metrics.get("atr_14", 0.0) or 0.0)
        high_60d = 0.0
        try:
            from market_analysis.atr_utils import fetch_high_60d

            high_60d = float(await fetch_high_60d(symbol) or 0.0)
        except Exception as e:  # 缺失時 resolve_effective_target 自有降級階梯
            logger.warning(f"[AdvisoryMode] {symbol} 60 日高點取得失敗，降級: {e}")
        eff = resolve_effective_target(spot, call_wall, high_60d, atr_1d)
        plan = {
            "kind": "TARGET_REACHED",
            "spot": spot,
            "stop_loss": None,
            "call_wall": call_wall if call_wall > 0 else None,
            "target": float(eff.target),
            "is_blue_sky": bool(eff.is_blue_sky),
        }
        reason = _target_reason(
            symbol, spot, call_wall, float(eff.target), eff.is_blue_sky
        )
    else:
        return None

    advisory: Dict[str, Any] = dict(ins)
    for key in _COMMAND_ONLY_KEYS:
        advisory.pop(key, None)
    advisory.update(
        {
            "action": ADVISORY_ACTION,
            "sell_ratio": 0.0,
            "target_core": "",
            "reason": reason,
            "is_manual_override_required": False,
            "is_extreme_tick_breach": False,
            "advisory_plan": plan,
        }
    )
    return advisory  # type: ignore[return-value]
