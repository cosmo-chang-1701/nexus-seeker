from enum import Enum
from typing import Optional


class MarketScenario(Enum):
    RANGE_BOUND = "區間抽取時間價值"
    SUPPORT_BUILD = "多頭支撐建倉"
    MOMENTUM_SQUEEZE = "動能軋空爆發"
    STRUCTURAL_BREAKDOWN = "結構破位與轉倉"


def classify_market_scenario(
    price: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float,
    is_squeezing: bool,
    uoa_skew: float,
) -> Optional[MarketScenario]:
    """
    Classifies the current market scenario based on quant parameters.
    Returns None if the market is in a normal state that does not warrant an alert.
    """
    if not all([price, put_wall, call_wall]):
        return None

    try:
        price = float(price)
        put_wall = float(put_wall)
        call_wall = float(call_wall)
        gamma_flip = float(gamma_flip) if gamma_flip is not None else 0.0
        uoa_skew = float(uoa_skew) if uoa_skew is not None else 0.0
    except (ValueError, TypeError):
        return None

    if price == 0.0 or put_wall == 0.0 or call_wall == 0.0:
        return None

    # 4. 結構破位與轉倉: 現價跌破 PutWall 且 現價 < Gamma Flip
    if price < put_wall and price < gamma_flip:
        return MarketScenario.STRUCTURAL_BREAKDOWN

    # 3. 動能軋空爆發: 突破 CallWall 且 UOA 偏向 Call (或正在擠壓)
    if price > call_wall and (is_squeezing or uoa_skew > 0.0):
        return MarketScenario.MOMENTUM_SQUEEZE

    # 2. 多頭支撐建倉: 現價回測 PutWall (誤差 1.5% 內) 且 未跌破 Gamma Flip
    if abs(price - put_wall) / put_wall <= 0.015 and price >= gamma_flip:
        return MarketScenario.SUPPORT_BUILD

    # 1. 區間抽取時間價值: PutWall < 現價 < CallWall 且 現價 > Gamma Flip
    # 由於這是一個較為常態的區間，我們可選擇加上 IVR 條件限制，或預設直接回傳
    if put_wall < price < call_wall and price > gamma_flip:
        return MarketScenario.RANGE_BOUND

    return None
