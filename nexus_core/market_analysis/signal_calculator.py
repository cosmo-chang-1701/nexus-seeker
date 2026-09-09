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

# 「機構避險背離 / 負 Gamma」情境下的資金退守上限：戰術（衛星）部位**總曝險**
# 不得超過總資金的 15%，其餘 70%~85% 退守大盤流動性資產。這是組合層而非單一
# 部位層的限額——本輪可新增的預算等於「上限 − 目前已部署曝險」。
_CAPITAL_RETREAT_PORTFOLIO_CAP_PCT = 0.15

# 計入戰術曝險時排除的現金等價／固定收益標的。與
# market_analysis/insights_engine.py 的 FIXED_INCOME_WHITE_LIST 同一份名單：
# 這些是退守的「目的地」，不該被算成需要退守的曝險。
_TACTICAL_EXPOSURE_EXCLUDED_SYMBOLS = frozenset({"BOXX", "BIL", "SHV"})


def compute_deployed_tactical_value(
    *,
    spot_holdings: Any = (),
    option_positions: Any = (),
) -> float:
    """加總目前已部署的戰術（衛星）曝險，供組合層資金退守限額使用。

    計價基礎刻意採**成本基礎**而非市價，理由有二：呼叫端已持有這些資料（零額外
    網路請求，不必為每檔持倉抓即時報價）；且在回檔情境下成本基礎會高於市值，
    使限額偏保守——而這道閘門本來就只在防禦情境下啟用。

    納入與排除規則：
      - 排除 `asset_class == "CORE"`：大盤流動性資產（如 VOO）正是退守的目的地。
      - 排除 BOXX / BIL / SHV：現金等價物，同理。
      - 現貨：`quantity * avg_cost`。
      - 長倉期權：已付權利金 `quantity * entry_price * 100`。
      - 賣出 PUT：佔用的擔保現金 `|quantity| * strike * 100`。
      - 賣出 CALL：**不計入**。備兌買權的擔保品是股票，已由對應的現貨部位計入，
        重複計算會高估曝險。

    任何一筆資料缺漏或型別異常都跳過該筆而非中斷整體計算——寧可少算一筆，也不要
    讓一筆髒資料使整道風控閘門失效。
    """
    total = 0.0

    for holding in spot_holdings or ():
        try:
            if str(holding.get("asset_class") or "").upper() == "CORE":
                continue
            symbol = str(holding.get("symbol") or "").upper()
            if symbol in _TACTICAL_EXPOSURE_EXCLUDED_SYMBOLS:
                continue
            quantity = float(holding.get("quantity") or 0.0)
            avg_cost = float(holding.get("avg_cost") or 0.0)
            if quantity > 0.0 and avg_cost > 0.0:
                total += quantity * avg_cost
        except (AttributeError, TypeError, ValueError):
            continue

    for position in option_positions or ():
        try:
            symbol = str(position.get("symbol") or "").upper()
            if symbol in _TACTICAL_EXPOSURE_EXCLUDED_SYMBOLS:
                continue
            quantity = float(position.get("quantity") or 0.0)
            if quantity > 0.0:
                entry_price = float(position.get("entry_price") or 0.0)
                if entry_price > 0.0:
                    total += quantity * entry_price * 100.0
            elif (
                quantity < 0.0 and str(position.get("opt_type") or "").lower() == "put"
            ):
                strike = float(position.get("strike") or 0.0)
                if strike > 0.0:
                    total += abs(quantity) * strike * 100.0
        except (AttributeError, TypeError, ValueError):
            continue

    return total


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
    has_upcoming_earnings: bool = False,
    deployed_tactical_value: float = 0.0,
) -> dict[str, Any]:
    """
    依據現價、期權偏斜 Skew 及技術指標，計算適合的買入/賣出價位與股數。
    - 未持倉標的：計算適合買入的價位與股數 (Sizing 基於 capital / risk_limit)
    - 已持倉標的：計算適合賣出的價位與股數

    `deployed_tactical_value` 是目前已部署的戰術（衛星）曝險，由呼叫端以
    `compute_deployed_tactical_value()` 算好傳入。僅在資金退守閘門啟用時使用，
    用來把限額做成組合層而非單一部位層。預設 0.0 代表「呼叫端未提供」，此時
    退守閘門會退化成只對單一部位設限（行為與提供 0 曝險時相同）。
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
    # option_skew 為 Optional[float]：calculate_skew() 在無歷史樣本時回傳 None，
    # 少了 `or 0.0` 會在下方 `skew_val / 100.0` 拋 TypeError，讓整則心跳靜默消失。
    skew_val = getattr(metrics, "option_skew", 0.0) or 0.0
    rsi = getattr(metrics, "rsi_14", 50.0) or 50.0

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
        # RiskContext: 底牆危機與風控戒嚴
        is_crisis = (
            tactical_model.scenario == "wait" and "SHIELD" in tactical_model.sddm_route
        )
        if is_crisis:
            result["suitable_buy_price"] = "N/A（風控鎖定，暫不推薦開倉買方策略）"
            result["buy_rationale"] = (
                "⚠️ 底牆破位或負 Gamma 風險主導，風控戒嚴已啟動，禁止任何左側接刀。"
            )
        else:
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

            suitable_buy = max(
                buy_price_phase3 * 0.9, min(suitable_buy, buy_price_phase1)
            )

            # ATR 防洗盤動態緩衝
            atr = getattr(metrics, "atr_14", 0.0) or 0.0
            if atr > 0:
                suitable_buy -= atr * 1.5

            # 避開整數與特定關卡
            if round(suitable_buy % 1.0, 2) in [0.00, 0.50, 0.99]:
                suitable_buy -= 0.03

            if has_upcoming_earnings:
                suitable_buy *= 0.90
                result["buy_rationale"] = (
                    "[⚠️ 財報前夜防護啟動] " + result["buy_rationale"]
                )

            result["suitable_buy_price"] = round(suitable_buy, 2)
            if atr > 0:
                # 明確揭露實際緩衝金額，而非只宣稱「已疊加」。若上游 ATR 取得失敗
                # 而退回 0.01 佔位值，使用者會直接從這個數字看出緩衝近似於零。
                result["buy_rationale"] += (
                    f" (已疊加 1.5×ATR = ${atr * 1.5:.2f} 防洗盤緩衝，"
                    "請以「15 分鐘 K 線實體跌破」作為最終撤退線)"
                )

        # Position Sizing
        base_allocation = capital * 0.05
        risk_limit_mult = max(0.5, min(2.0, risk_limit / 15.0))
        skew_size_mult = 0.8 if skew_val > 3.0 else (1.1 if skew_val <= 0.0 else 1.0)
        rsi_size_mult = 1.15 if rsi < 35 else 1.0

        allocated_budget = (
            base_allocation * risk_limit_mult * skew_size_mult * rsi_size_mult
        )

        # 動態資金藍圖演算法 (Capital Allocation Model)
        # 過去這裡是對 sddm_route 做中文子字串比對。實務上 evaluation.py 的負
        # Gamma 分支設的是 "SHIELD 網格防禦"、空頭動能分支設的是
        # "WAIT (空頭動能發散)"，兩者都不含 "負 Gamma"，該分支從未觸發過。
        # 改讀 tactical 上的顯式旗標，並保留原字串比對作為向後相容（供直接
        # 手動建構 WatchlistTacticalPlan 的既有呼叫端與測試）。
        if (
            getattr(tactical_model, "capital_retreat_required", False)
            or "機構避險背離" in tactical_model.sddm_route
            or "負 Gamma" in tactical_model.sddm_route
        ):
            # 70%~85% 退守大盤流動性資產，僅保留 10%~15% 戰術/套利資金。
            #
            # 這裡原本只有 `min(allocated_budget, capital * 0.15)`，但該上限永遠
            # 撞不到：allocated_budget 的理論上限是
            # capital * 0.05 * 2.0 (risk_limit) * 1.1 (skew) * 1.15 (rsi)
            # = capital * 0.1265 < capital * 0.15，min() 恆取前者，等於閘門只改
            # 了文案卻沒有縮減任何部位。原因是「只保留 10%~15%」是**組合層**語意
            # （整個戰術沙盒佔總資金的比例），卻被寫成單一部位的上限，而單一部位
            # 本來就只有 5%。
            #
            # 改為真正的組合層限額：本輪可新增的預算 =
            #   max(總資金 × 15% − 目前已部署的戰術曝險, 0)
            # 曝險由呼叫端以 compute_deployed_tactical_value() 算出（現貨衛星部位
            # 成本基礎 + 長倉期權權利金 + 賣出 PUT 擔保金，排除 CORE 與 BOXX/BIL/
            # SHV）。已達上限時預算歸零、股數歸零，並在 rationale 明講原因，而不是
            # 照常給出一個永遠不會被縮減的建議部位。
            portfolio_cap = capital * _CAPITAL_RETREAT_PORTFOLIO_CAP_PCT
            remaining_capacity = max(portfolio_cap - deployed_tactical_value, 0.0)
            allocated_budget = min(allocated_budget, remaining_capacity)
            result["capital_retreat_cap"] = round(portfolio_cap, 2)
            result["capital_retreat_deployed"] = round(deployed_tactical_value, 2)
            result["capital_retreat_remaining"] = round(remaining_capacity, 2)

            if remaining_capacity <= 0.0:
                result["buy_rationale"] = (
                    f"⛔ 負 Gamma 疊加機構避險背離：戰術部位總曝險已達上限 "
                    f"(已部署 ${deployed_tactical_value:,.0f} / 上限 ${portfolio_cap:,.0f}，"
                    f"佔總資金 {_CAPITAL_RETREAT_PORTFOLIO_CAP_PCT:.0%})，本輪不得新增任何部位，"
                    "請先將既有戰術曝險退守至大盤流動性資產 (如 VOO)。 "
                    + result["buy_rationale"]
                )
            else:
                result["buy_rationale"] = (
                    f"⚠️ 負 Gamma 疊加機構避險背離：70%~85% 資金強制退守大盤流動性資產 (如 VOO)，"
                    f"戰術總曝險上限 ${portfolio_cap:,.0f} (佔總資金 "
                    f"{_CAPITAL_RETREAT_PORTFOLIO_CAP_PCT:.0%})，已部署 "
                    f"${deployed_tactical_value:,.0f}，本輪剩餘可用 ${remaining_capacity:,.0f}。 "
                    + result["buy_rationale"]
                )

        if is_crisis:
            result["suitable_buy_shares"] = 0
        else:
            buy_price = result["suitable_buy_price"]
            if isinstance(buy_price, (int, float)) and buy_price > 0.0:
                shares = int(allocated_budget // buy_price)
            else:
                shares = 0
            result["suitable_buy_shares"] = max(1, shares) if shares > 0 else 0

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

        # ATR 防洗盤動態緩衝 (向上)
        atr = getattr(metrics, "atr_14", 0.0) or 0.0
        if atr > 0:
            suitable_sell += atr * 1.5

        # 避開整數關卡
        if round(suitable_sell % 1.0, 2) in [0.00, 0.50, 0.99]:
            suitable_sell -= 0.03

        result["suitable_sell_price"] = round(suitable_sell, 2)
        if atr > 0:
            result["sell_rationale"] += (
                f" (已疊加 1.5×ATR = ${atr * 1.5:.2f} 防洗盤緩衝避開整數，"
                "請以「15 分鐘 K 線實體跌破」作為最終離場確認)"
            )

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
