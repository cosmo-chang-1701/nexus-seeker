from enum import Enum
from typing import Optional


class MarketScenario(Enum):
    GOLDEN_LEFT = "黃金左側加碼"
    STRONG_BREAKOUT = "強勢突破加碼"
    GOLDEN_TAKE_PROFIT = "黃金波段止盈"
    FAKE_SUPPORT_TRAP = "假性支撐陷阱"
    STRUCTURAL_BREAKDOWN = "結構破位轉倉"


def classify_market_scenario(
    price: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float,
    is_squeezing: bool,
    uoa_skew: float,
    ivr: float,
    hvn: float,
    lvn: float,
) -> Optional[MarketScenario]:
    if not all([price, put_wall, call_wall, gamma_flip]):
        return None

    try:
        price = float(price)
        put_wall = float(put_wall)
        call_wall = float(call_wall)
        gamma_flip = float(gamma_flip) if gamma_flip is not None else 0.0
        ivr = float(ivr) if ivr is not None else 0.0
        hvn = float(hvn) if hvn is not None else 0.0
        lvn = float(lvn) if lvn is not None else 0.0
    except (ValueError, TypeError):
        return None

    if price == 0.0 or put_wall == 0.0 or call_wall == 0.0:
        return None

    def is_near(val1: float, val2: float, tolerance: float = 0.015) -> bool:
        if val1 == 0 or val2 == 0:
            return False
        return abs(val1 - val2) / val2 <= tolerance

    # 5. 結構破位轉倉 (Structural Breakdown Roll)
    if price < gamma_flip and price < put_wall and is_near(price, lvn):
        return MarketScenario.STRUCTURAL_BREAKDOWN

    # 4. 假性支撐陷阱 (Fake Support Trap)
    if (
        price < gamma_flip
        and is_near(price, put_wall)
        and lvn < put_wall
        and ivr > 80.0
    ):
        return MarketScenario.FAKE_SUPPORT_TRAP

    # 3. 黃金波段止盈 (Golden Swing Take-Profit)
    if (
        price > gamma_flip
        and is_near(price, call_wall)
        and is_near(price, hvn)
        and ivr > 70.0
    ):
        return MarketScenario.GOLDEN_TAKE_PROFIT

    # 2. 強勢突破加碼 (Strong Breakout Scaling)
    if price > gamma_flip and price > call_wall and is_near(price, lvn) and ivr < 30.0:
        return MarketScenario.STRONG_BREAKOUT

    # 1. 黃金左側加碼 (Golden Left-Side Scaling)
    if (
        price > gamma_flip
        and is_near(price, put_wall)
        and is_near(price, hvn)
        and ivr > 50.0
    ):
        return MarketScenario.GOLDEN_LEFT

    return None
