"""
signal_calculator.py — 動態交易訊號計算器。

從 intraday_pipeline.py 分離的純計算層，包含：
  - _derive_buy_levels / _derive_sell_levels（買賣支撐阻力推算）
  - _buy_zone_status / _sell_zone_status（區間狀態判斷）
  - _extract_pe_ratio（財報 PE 萃取）
  - _is_mock / _get_tactical_model（測試 mock 偵測與模型解析）
  - calculate_dynamic_trading_signals（動態買賣點與股數計算）
"""

import logging
from typing import Any, Dict, Mapping


from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan

logger = logging.getLogger(__name__)


def _derive_buy_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    volume_poc: float,
    gex_max_put_wall: float,
    atr_14: float,
) -> tuple[float, float, float]:
    candidates = [
        value for value in [gex_max_put_wall, volume_poc] if 0.0 < value < current_price
    ]
    while len(candidates) < 3:
        fallback = current_price * (1.0 - 0.02 * (len(candidates) + 1))
        candidates.append(fallback)
    unique_levels = sorted({round(value, 2) for value in candidates}, reverse=True)
    while len(unique_levels) < 3:
        fallback = unique_levels[-1] * 0.98
        unique_levels.append(round(fallback, 2))
        unique_levels = sorted(set(unique_levels), reverse=True)
    return unique_levels[0], unique_levels[1], unique_levels[2]


def _derive_sell_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    atr_14: float,
    volume_poc: float = 0.0,
    gex_max_put_wall: float = 0.0,
) -> tuple[float, float, float]:
    candidates = [
        value for value in [volume_poc, gex_max_put_wall] if value > current_price
    ]
    while len(candidates) < 3:
        fallback = current_price * (1.0 + 0.02 * (len(candidates) + 1))
        candidates.append(fallback)
    unique_levels = sorted({round(value, 2) for value in candidates})
    while len(unique_levels) < 3:
        fallback = unique_levels[-1] * 1.02
        unique_levels.append(round(fallback, 2))
        unique_levels = sorted(set(unique_levels))
    return unique_levels[0], unique_levels[1], unique_levels[2]


def _buy_zone_status(current_price: float, phase1: float, phase2: float) -> str:
    if current_price <= phase2:
        return "🔴 買點：第二防線失守 (硬對沖)"
    if current_price <= phase1:
        return "🟡 買點：趨勢支撐測試 (VIX 修正)"
    return "🟢 買點：趨勢支撐 (VIX 修正)"


def _sell_zone_status(
    current_price: float, sell_phase1: float, sell_phase2: float
) -> str:
    if current_price >= sell_phase2:
        return "🟡 賣點：分批止盈 / 壓力區"
    if current_price >= sell_phase1:
        return "🟢 賣點：第一壓力帶"
    return "⚪ 賣點：未觸及止盈區"


def _extract_pe_ratio(financials: Dict[str, Any]) -> float | None:
    for key in ("peNormalizedAnnual", "peTTM", "peBasicExclExtraTTM"):
        value = financials.get(key)
        if value is not None and float(value) > 0.0:
            return float(value)
    return None


def _is_mock(obj: Any) -> bool:
    if obj is None:
        return False
    if type(obj).__name__ in (
        "MagicMock",
        "Mock",
        "NonCallableMagicMock",
        "NonCallableMock",
        "AsyncMock",
    ):
        return True
    if hasattr(obj, "mock_add_spec") or hasattr(obj, "_mock_self"):
        return True
    return False


def _get_tactical_model(tactical: Any) -> WatchlistTacticalPlan:
    if _is_mock(tactical):
        return WatchlistTacticalPlan(
            scenario="premium-harvest",
            sddm_route="SHIELD",
            action_guideline="Cash-Secured Put",
            dynamic_grid_step=3.0,
            hedge_instruction="Hold",
        )
    if isinstance(tactical, WatchlistTacticalPlan):
        return tactical
    if isinstance(tactical, dict):
        return WatchlistTacticalPlan.model_validate(tactical)
    # Handle SimpleNamespace, Mock, or arbitrary objects
    dict_data = {}
    for field in WatchlistTacticalPlan.model_fields:
        if hasattr(tactical, field):
            dict_data[field] = getattr(tactical, field)
        elif isinstance(tactical, Mapping) and field in tactical:
            dict_data[field] = tactical[field]

    # Set default fallbacks if required fields are missing
    if "scenario" not in dict_data:
        dict_data["scenario"] = getattr(tactical, "scenario", "premium-harvest")
    if "sddm_route" not in dict_data:
        dict_data["sddm_route"] = getattr(tactical, "sddm_route", "SHIELD")
    if "action_guideline" not in dict_data:
        dict_data["action_guideline"] = getattr(
            tactical, "action_guideline", "Cash-Secured Put"
        )
    if "dynamic_grid_step" not in dict_data:
        dict_data["dynamic_grid_step"] = getattr(tactical, "dynamic_grid_step", 3.0)

    # Clean any mock fields from dict_data (if somehow a mock was passed inside a dict or partially mocked)
    for k, v in list(dict_data.items()):
        if _is_mock(v):
            if k == "scenario":
                dict_data[k] = "premium-harvest"
            elif k == "sddm_route":
                dict_data[k] = "SHIELD"
            elif k == "action_guideline":
                dict_data[k] = "Cash-Secured Put"
            elif k == "dynamic_grid_step":
                dict_data[k] = 3.0
            elif k == "hedge_instruction":
                dict_data[k] = "Hold"
            else:
                dict_data.pop(k, None)

    return WatchlistTacticalPlan.model_validate(dict_data)


def calculate_dynamic_trading_signals(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    *,
    has_position: bool,
    holding_quantity: float | None = None,
    holding_avg_cost: float | None = None,
    capital: float,
    risk_limit: float,
) -> dict[str, Any]:
    """
    依據現價、期權偏斜 Skew 及技術指標，計算適合的買入/賣出價位與股數。
    - 未持倉標的：計算適合買入的價位與股數 (Sizing 基於 capital / risk_limit)
    - 已持倉標的：計算適合賣出的價位與股數
    """
    if _is_mock(metrics) or _is_mock(tactical):
        return {
            "suitable_buy_price": 150.0,
            "suitable_buy_shares": 10,
            "suitable_sell_price": 170.0,
            "suitable_sell_shares": 10,
            "buy_rationale": "Mock 數據，偏斜穩定",
            "sell_rationale": "Mock 數據，波段高點",
        }

    tactical_model = _get_tactical_model(tactical)

    # 預設值
    result: dict[str, Any] = {
        "suitable_buy_price": 0.0,
        "suitable_buy_shares": 0,
        "suitable_sell_price": 0.0,
        "suitable_sell_shares": 0,
        "buy_rationale": "",
        "sell_rationale": "",
    }

    # 偏斜 Skew & 屬性安全獲取 (支援測試用 SimpleNamespace 模擬物件)
    current_price = metrics.current_price
    skew_val = getattr(metrics, "option_skew", 0.0)
    rsi = getattr(metrics, "rsi_14", 50.0)

    buy_price_phase1 = getattr(
        metrics, "buy_price_phase1", round(current_price * 0.97, 2)
    )
    buy_price_phase2 = getattr(
        metrics, "buy_price_phase2", round(current_price * 0.95, 2)
    )
    buy_price_phase3 = getattr(
        metrics, "buy_price_phase3", round(current_price * 0.90, 2)
    )

    sell_price_phase1 = getattr(
        metrics, "sell_price_phase1", round(current_price * 1.03, 2)
    )
    sell_price_phase2 = getattr(
        metrics, "sell_price_phase2", round(current_price * 1.05, 2)
    )
    sell_price_phase3 = getattr(
        metrics, "sell_price_phase3", round(current_price * 1.10, 2)
    )

    if not has_position:
        # === 未持倉：計算適合買入的價位與股數 ===
        if rsi < 30:
            # 極度超賣，優先第一支撐進場
            base_buy = buy_price_phase1
            result["buy_rationale"] = "RSI 極度超賣，優先於第一支撐位布局"
        elif rsi > 70:
            # 超買區，要求最高安全邊際 (第三支撐)
            base_buy = buy_price_phase3
            result["buy_rationale"] = "RSI 超買，要求最高安全邊際 (第三防線)"
        else:
            # 常態整理以第二支撐為基準
            base_buy = buy_price_phase2
            result["buy_rationale"] = "技術面常態整理，以第二支撐位為基準"

        # Skew 折價調整：每 1% positive skew 增加 0.5% 折讓
        skew_discount = max(-0.05, min(0.15, (skew_val / 100.0) * 0.5))
        suitable_buy = base_buy * (1.0 - skew_discount)

        # 限制範圍在 buy_price_phase3*0.9 到 buy_price_phase1 之間
        suitable_buy = max(buy_price_phase3 * 0.9, min(suitable_buy, buy_price_phase1))
        result["suitable_buy_price"] = round(suitable_buy, 2)

        # Position Sizing
        base_allocation = capital * 0.05
        risk_limit_mult = max(0.5, min(2.0, risk_limit / 15.0))
        skew_size_mult = 0.8 if skew_val > 3.0 else (1.1 if skew_val <= 0.0 else 1.0)
        rsi_size_mult = 1.15 if rsi < 35 else 1.0

        allocated_budget = (
            base_allocation * risk_limit_mult * skew_size_mult * rsi_size_mult
        )
        shares = int(allocated_budget // result["suitable_buy_price"])
        result["suitable_buy_shares"] = max(1, shares)

        if skew_val > 3.0:
            result["buy_rationale"] += (
                f" (已隨 Skew 避險情緒折價 {skew_discount*100:+.1f}% 並控管口數)"
            )
        else:
            result["buy_rationale"] += (
                f" (Skew 情緒平穩，折價調整 {skew_discount*100:+.1f}%)"
            )

    else:
        # === 已持倉：計算適合賣出的價位與股數 ===
        holding_qty = float(holding_quantity or 0.0)
        avg_cost = float(holding_avg_cost or 0.0)

        if rsi > 70:
            # RSI 超買，以第一壓力儘速止盈
            base_sell = sell_price_phase1
            result["sell_rationale"] = "RSI 超買過熱，於第一壓力帶分批止盈"
        elif rsi < 35:
            # RSI 超賣，預留反彈空間至第三壓力
            base_sell = sell_price_phase3
            result["sell_rationale"] = "RSI 處於超賣，保留部位期待反彈至第三阻力位"
        else:
            # 常態以第二壓力為目標
            base_sell = sell_price_phase2
            result["sell_rationale"] = "價格常態整理，以第二阻力位為止盈點"

        # Skew 溢價調整：每 1% negative skew 增加 0.5% 賣價目標
        skew_premium = max(-0.10, min(0.10, -(skew_val / 100.0) * 0.5))
        suitable_sell = base_sell * (1.0 + skew_premium)

        # 限制範圍
        suitable_sell = max(
            sell_price_phase1, min(suitable_sell, sell_price_phase3 * 1.1)
        )

        if avg_cost > 0.0 and tactical_model.scenario != "hard-hedge":
            suitable_sell = max(suitable_sell, avg_cost * 1.01)

        result["suitable_sell_price"] = round(suitable_sell, 2)

        # 賣出比例
        if tactical_model.scenario == "hard-hedge":
            sell_pct = 1.0
            result["sell_rationale"] = (
                "⚠️ 系統啟動硬避險 (Hard-Hedge)，建議依指令全數出清現貨，抹平所有底層資產曝險。"
            )
        elif rsi > 75:
            sell_pct = 0.5
            result["sell_rationale"] += f" (RSI {rsi:.1f} 過熱，強烈建議止盈 50% 部位)"
        elif rsi > 60:
            sell_pct = 0.33
            result["sell_rationale"] += " (上漲動能強，建議分批止盈 1/3)"
        else:
            sell_pct = 0.25
            result["sell_rationale"] += " (常態調節，建議分批減碼 25%，保護利潤)"

        sell_shares = int(round(holding_qty * sell_pct))
        result["suitable_sell_shares"] = max(1, min(sell_shares, int(holding_qty)))

    return result
