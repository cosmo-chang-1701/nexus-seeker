import pandas as pd
import pandas_ta as ta
import numpy as np
from dataclasses import dataclass
from typing import Optional

@dataclass
class PSQResult:
    squeeze_level: str     # "High" (Red), "Mid" (Orange), "Normal" (Pink), "Release" (Gray)
    is_squeezing: bool     # 是否處於任何形式的擠壓狀態
    momentum_value: float
    momentum_color: str    # "LightBlue", "DarkBlue", "Red", "Golden", "Neutral"
    signal_direction: str  # "Long", "Short", "Neutral"
    is_near_support: bool
    is_breakout_long: bool # 多頭能量釋放突破
    is_breakout_short: bool# 空頭能量釋放突破
    sma_distance_pct: float
    # VIX 戰情標記
    vix_momentum_label: str = "NORMAL"   # "NORMAL", "OVEREXTENDED_RISK", "HIGH_CONVICTION_RECOVERY"
    vix_timeframe_note: str = ""         # 低 VIX 時建議使用的時間框架

def analyze_psq(df: pd.DataFrame, length: int = 20, bb_mult: float = 2.0, kc_mults: list = [1.0, 1.5, 2.0], near_pct: float = 1.5, vix_spot: float = None) -> Optional[PSQResult]:
    """
    計算 PowerSqueeze (PSQ) 量化指標 (Ultimate Edition v2)。
    輸入資料為包含 'Open', 'High', 'Low', 'Close' 的 DataFrame。
    
    Args:
        vix_spot: VIX 即時價格。用於動能標記（OVEREXTENDED_RISK / HIGH_CONVICTION_RECOVERY）
                  以及低波環境時間框架建議。
    """
    if df is None or df.empty or len(df) < length * 2:
        return None

    try:
        # 防止改變原始 DataFrame
        df = df.copy()
        
        # 1. Bollinger Bands (20, 2)
        bb = ta.bbands(df['Close'], length=length, std=bb_mult)
        if bb is None: return None
        
        # 避開 pandas-ta 版本導致的動態欄位名稱變更，改用語意固定的位置索引
        bb_lower = bb.iloc[:, 0] # Lower Band
        basis = bb.iloc[:, 1]    # Middle Band (SMA)
        bb_upper = bb.iloc[:, 2] # Upper Band
        
        # 2. Keltner Channels (using default True Range)
        kc1 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[0])
        kc2 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[1])
        kc3 = ta.kc(df['High'], df['Low'], df['Close'], length=length, scalar=kc_mults[2])
        
        if kc1 is None or kc2 is None or kc3 is None: return None
        
        # 區分不同強度的擠壓通道 (KC回傳格式依序為 Lower, Basis, Upper)
        kc1_lower = kc1.iloc[:, 0]
        kc1_upper = kc1.iloc[:, 2]
        kc2_lower = kc2.iloc[:, 0]
        kc2_upper = kc2.iloc[:, 2]
        kc3_lower = kc3.iloc[:, 0]
        kc3_upper = kc3.iloc[:, 2]
        
        # 判定各級別的擠壓狀態
        sqz_high = (bb_lower > kc1_lower) & (bb_upper < kc1_upper)    # 高強度 (紅)
        sqz_mid = (bb_lower > kc2_lower) & (bb_upper < kc2_upper)     # 中強度 (橘)
        sqz_normal = (bb_lower > kc3_lower) & (bb_upper < kc3_upper)  # 一般強度 (粉)
        
        is_squeezing = sqz_normal # 只要 BB 縮入最寬的 2.0 KC 內，即屬擠壓狀態
        
        # 3. Momentum (線性回歸動能)
        high_max = df['High'].rolling(length).max()
        low_min = df['Low'].rolling(length).min()
        avg_price = (high_max + low_min) / 2.0
        
        momentum_source = df['Close'] - (avg_price + basis) / 2.0
        momentum_value = ta.linreg(momentum_source, length=length)
        
        if momentum_value is None or momentum_value.isna().all():
            return None

        mom_diff = momentum_value.diff()

        # 4. 回調支撐判定
        # 價格與 20 SMA 的百分比距離
        sma_distance_pct = ((df['Close'] - basis) / basis) * 100
        is_near_support = sma_distance_pct.abs() <= near_pct
        
        # 取得最後一筆與前一筆狀態作判斷
        curr_mom = momentum_value.iloc[-1]
        prev_mom = momentum_value.iloc[-2] if len(momentum_value) > 1 else 0
        curr_diff = mom_diff.iloc[-1]
        
        # 判斷動能柱體顏色 (Momentum Histogram)
        if curr_mom > 0:
            mom_color = "LightBlue" if curr_diff > 0 else "DarkBlue"
        elif curr_mom < 0:
            mom_color = "Red" if curr_diff < 0 else "Golden"
        else:
            mom_color = "Neutral"

        # 判斷當前擠壓層級 (Squeeze Level)
        if sqz_high.iloc[-1]:
            squeeze_level = "High"
        elif sqz_mid.iloc[-1]:
            squeeze_level = "Mid"
        elif sqz_normal.iloc[-1]:
            squeeze_level = "Normal"
        else:
            squeeze_level = "Release"
            
        # 判斷基本訊號 (轉強/轉弱)
        if curr_mom > 0 and curr_mom > prev_mom:
            signal = "Long"
        elif curr_mom < 0 and curr_mom < prev_mom:
            signal = "Short"
        else:
            signal = "Neutral"
            
        # 判斷是否為「擠壓突破」(Breakout)
        # 前段期間處於「高強度擠壓(Red)」，當前 K 線完全解除擠壓 (Release)
        prev_sqz_high = sqz_high.iloc[-2] if len(sqz_high) > 1 else False
        curr_sqz_any = is_squeezing.iloc[-1]
        
        is_breakout_long = bool(prev_sqz_high and (not curr_sqz_any) and (curr_mom > 0))
        is_breakout_short = bool(prev_sqz_high and (not curr_sqz_any) and (curr_mom < 0))

        # ---------- VIX 動能標記 (VIX Momentum Labeling) ----------
        vix_momentum_label = "NORMAL"
        vix_timeframe_note = ""

        if vix_spot is not None:
            # 匯入分位數邊界
            from config import VIX_QUANTILE_BOUNDS
            upper_3 = VIX_QUANTILE_BOUNDS.get('upper_3', 24.6)

            # 休兵期間的多頭訊號 → 過度延伸風險
            if vix_spot < 15.0 and signal == "Long":
                vix_momentum_label = "OVEREXTENDED_RISK"

            # 高波動期間的 Golden 柱體（空頭減速）→ 高確信反彈
            elif vix_spot > upper_3 and mom_color == "Golden":
                vix_momentum_label = "HIGH_CONVICTION_RECOVERY"

            # 低波環境時間框架建議
            if vix_spot < 18.0:
                vix_timeframe_note = "低波期，建議以日K/4H為主，忽略30m雜訊"
        # -----------------------------------------------------------
        
        return PSQResult(
            squeeze_level=squeeze_level,
            is_squeezing=bool(curr_sqz_any),
            momentum_value=float(curr_mom),
            momentum_color=mom_color,
            signal_direction=signal,
            is_near_support=bool(is_near_support.iloc[-1]),
            is_breakout_long=is_breakout_long,
            is_breakout_short=is_breakout_short,
            sma_distance_pct=float(sma_distance_pct.iloc[-1]),
            vix_momentum_label=vix_momentum_label,
            vix_timeframe_note=vix_timeframe_note,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"PSQ 計算發生錯誤: {e}")
        return None

