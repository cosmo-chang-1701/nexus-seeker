from enum import Enum
from typing import Optional

from market_analysis.gamma_cliff_confirmation import is_below_gamma_defense_line


class MarketScenario(Enum):
    GOLDEN_LEFT = "黃金左側加碼"
    STRONG_BREAKOUT = "強勢突破加碼"
    GOLDEN_TAKE_PROFIT = "黃金波段止盈"
    FAKE_SUPPORT_TRAP = "假性支撐陷阱"
    STRUCTURAL_BREAKDOWN = "結構破位轉倉"
    STRUCTURAL_BREAKDOWN_PENDING = "結構破位轉倉_待確認"
    WHALE_ESCORT_RESONANCE = "巨鯨護航共振"


def classify_market_scenario(
    price: float,
    high: float,
    low: float,
    current_volume: float,
    avg_volume_20: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float,
    is_squeezing: bool,
    uoa_skew: float,
    ivr: float,
    hvn: float,
    lvn: float,
    skew_percentile: float = 50.0,
    is_uoa_aligned: bool = False,
) -> Optional[MarketScenario]:
    if not all([price, put_wall, call_wall, gamma_flip]):
        return None

    try:
        price = float(price)
        high = float(high) if high is not None else price
        low = float(low) if low is not None else price
        current_volume = float(current_volume) if current_volume is not None else 0.0
        avg_volume_20 = float(avg_volume_20) if avg_volume_20 is not None else 0.0
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

    def is_price_near_wall(
        high_val: float, low_val: float, wall: float, tolerance: float = 0.015
    ) -> bool:
        if wall == 0:
            return False
        if low_val <= wall <= high_val:
            return True
        if low_val > wall:
            return (low_val - wall) / wall <= tolerance
        if high_val < wall:
            return (wall - high_val) / wall <= tolerance
        return False

    # [ Step 4: 轉倉觸發 ]
    # 現價貫穿 PutWall 且跌破 Gamma Flip ──► 100% 資金動態轉倉至 QQQ / SPY
    if is_below_gamma_defense_line(price, put_wall, gamma_flip):
        return MarketScenario.STRUCTURAL_BREAKDOWN_PENDING

    # [ Step 1: 體質檢查 ] ──現價是否 > Gamma Flip？
    if price > gamma_flip:
        # --- YES (正 Gamma/平穩) 允許進行均值回歸與逢低加碼 ---

        # [ 巨鯨護航共振 ]
        # 點位驗證: K棒高低點回測 PutWall (GEX 正 Gamma 牆確立)
        # 且 Skew 避險分位 < 50.0，且 UOA 方向一致 (STO Put / BTO Call)
        if (
            is_price_near_wall(high, low, put_wall)
            and skew_percentile < 50.0
            and is_uoa_aligned
        ):
            return MarketScenario.WHALE_ESCORT_RESONANCE

        # [ 黃金左側加碼 ]
        # 點位驗證: K棒高低點回測 PutWall，且 PutWall 與 HVN 重疊 (鋼鐵牆成型)
        if is_price_near_wall(high, low, put_wall) and is_near(put_wall, hvn):
            # 工具匹配: 高 IVR (> 50%)
            if ivr > 50.0:
                return MarketScenario.GOLDEN_LEFT

        # [ 黃金波段止盈 ]
        # 點位驗證: K棒高低點推升至 CallWall，且 CallWall 與 HVN 重疊 (上檔天花板)
        if is_price_near_wall(high, low, call_wall) and is_near(call_wall, hvn):
            return MarketScenario.GOLDEN_TAKE_PROFIT

        # [ 強勢突破加碼 ]
        # 點位驗證: 現價突破 CallWall，且落在 LVN (紙糊牆/真空區，提供無阻力加速)
        # 帶量定義: 當前 K 棒成交量 > 20 日均量 * 1.5
        if price > call_wall and is_near(price, lvn):
            if current_volume > avg_volume_20 * 1.5 and ivr < 30.0:
                return MarketScenario.STRONG_BREAKOUT
    else:
        # --- NO (負 Gamma/暴高) 進入防守狀態，嚴禁左側抄底 ---

        # [ 假性支撐陷阱 ]
        # 點位驗證: K棒高低點觸及 PutWall (不論是 LVN 或 HVN)
        if is_price_near_wall(high, low, put_wall):
            return MarketScenario.FAKE_SUPPORT_TRAP

    return None
