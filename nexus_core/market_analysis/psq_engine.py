import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass
from typing import Optional

@dataclass
class PSQResult:
    is_squeezing: bool
    momentum_value: float
    signal_direction: str  # "Long", "Short", "Neutral"
    is_near_support: bool
    is_breakout_high: bool
    sma_distance_pct: float

def analyze_psq(df: pd.DataFrame, length: int = 20, bb_mult: float = 2.0, kc_mults: list = [1.0, 1.5, 2.0], near_pct: float = 1.5) -> Optional[PSQResult]:
    """
    計算 PowerSqueeze (PSQ) 量化指標。
    輸入資料為包含 'Open', 'High', 'Low', 'Close' 的 DataFrame。
    """
    if df is None or df.empty or len(df) < length * 2:
        return None

    try:
        # 防止改變原始 DataFrame
        df = df.copy()
        
        # 1. Bollinger Bands (20, 2)
        bb = ta.bbands(df['Close'], length=length, std=bb_mult)
        if bb is None: return None
        bb_lower = bb[f'BBL_{length}_{bb_mult}']
        bb_upper = bb[f'BBU_{length}_{bb_mult}']
        basis = bb[f'BBM_{length}_{bb_mult}'] # 20 SMA
        
        # 2. Keltner Channels (using default True Range)
        kc1 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[0])
        kc2 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[1])
        kc3 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[2])
        
        if kc1 is None or kc2 is None or kc3 is None: return None
        
        # Usually squeeze is when BB is completely inside KC. We check against KC multiplier 2.0
        kc_widest_lower = kc3[f'KCLs_{length}_{kc_mults[2]}']
        kc_widest_upper = kc3[f'KCUs_{length}_{kc_mults[2]}']
        
        is_squeezing = (bb_lower > kc_widest_lower) & (bb_upper < kc_widest_upper)
        
        # 3. Momentum
        # avg_price = (highest_high + lowest_low) / 2
        high_max = df['High'].rolling(length).max()
        low_min = df['Low'].rolling(length).min()
        avg_price = (high_max + low_min) / 2.0
        
        momentum_source = df['Close'] - (avg_price + basis) / 2.0
        # pandas_ta linear regression length=20
        momentum_value = ta.linreg(momentum_source, length=length)
        
        if momentum_value is None or momentum_value.isna().all():
            return None

        # 4. 回調支撐判定
        # 價格與 20 SMA (basis) 的百分比距離
        sma_distance_pct = ((df['Close'] - basis) / basis) * 100
        is_near_support = sma_distance_pct.abs() <= near_pct
        
        # 判斷訊號方向
        curr_mom = momentum_value.iloc[-1]
        prev_mom = momentum_value.iloc[-2] if len(momentum_value) > 1 else 0
        
        if curr_mom > 0 and curr_mom > prev_mom:
            signal = "Long"
        elif curr_mom < 0 and curr_mom < prev_mom:
            signal = "Short"
        else:
            signal = "Neutral"
            
        # 判斷是否為「擠壓釋放」(Breakout High)
        # 上一根 K 線處於 squeeze，目前 K 線解除 squeeze，且動能為正
        curr_sqz = is_squeezing.iloc[-1]
        prev_sqz = is_squeezing.iloc[-2] if len(is_squeezing) > 1 else False
        is_breakout_high = (not curr_sqz) and prev_sqz and (curr_mom > 0)
        
        return PSQResult(
            is_squeezing=bool(curr_sqz),
            momentum_value=float(curr_mom),
            signal_direction=signal,
            is_near_support=bool(is_near_support.iloc[-1]),
            is_breakout_high=bool(is_breakout_high),
            sma_distance_pct=float(sma_distance_pct.iloc[-1])
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"PSQ 計算發生錯誤: {e}")
        return None
