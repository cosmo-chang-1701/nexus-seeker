import pandas as pd
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class GapType(Enum):
    UPWARD = "UPWARD"
    DOWNWARD = "DOWNWARD"
    NONE = "NONE"

class FillStatus(Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    HOLDING = "HOLDING"
    NONE = "NONE"

@dataclass
class GapStatus:
    """
    Data structure representing the quantitative gap analysis result.
    """
    gap_type: GapType
    gap_size: float
    gap_percentage: float
    fill_status: FillStatus
    fill_percentage: float
    is_filled: bool

class GapAnalyzer:
    """
    Quantitative engine for Gap & Fill analysis.
    Evaluates overnight price discontinuities and subsequent intraday mean reversion.
    """

    @staticmethod
    def analyze_gap(df: pd.DataFrame) -> Optional[GapStatus]:
        """
        Ingests OHLCV data to determine gap metrics.
        
        Args:
            df (pd.DataFrame): DataFrame containing 'Open', 'High', 'Low', 'Close' columns.
                               Must have at least two rows (Previous Day, Current Day).
        
        Returns:
            Optional[GapStatus]: Structured gap metrics or None if insufficient data.
        """
        if df is None or len(df) < 2:
            return None

        # Extract relevant price points
        prev_close = float(df['Close'].iloc[-2])
        curr_open = float(df['Open'].iloc[-1])
        curr_high = float(df['High'].iloc[-1])
        curr_low = float(df['Low'].iloc[-1])
        curr_close = float(df['Close'].iloc[-1])

        gap_size = curr_open - prev_close
        gap_percentage = (gap_size / prev_close) * 100
        
        if abs(gap_size) < 1e-6:
            return GapStatus(
                gap_type=GapType.NONE,
                gap_size=0.0,
                gap_percentage=0.0,
                fill_status=FillStatus.NONE,
                fill_percentage=0.0,
                is_filled=True
            )

        gap_type = GapType.UPWARD if gap_size > 0 else GapType.DOWNWARD
        
        # Calculate Fill Status
        # Upward Gap Fill: Price moves down to previous close
        # Downward Gap Fill: Price moves up to previous close
        
        fill_percentage = 0.0
        is_filled = False
        fill_status = FillStatus.HOLDING

        if gap_type == GapType.UPWARD:
            # Low must reach or break previous close for a full fill
            if curr_low <= prev_close:
                fill_percentage = 100.0
                is_filled = True
                fill_status = FillStatus.FULL
            else:
                # Partial fill calculation based on how much of the gap zone was entered
                # Gap zone is [prev_close, curr_open]
                movement_into_gap = curr_open - curr_low
                if movement_into_gap > 0:
                    fill_percentage = (movement_into_gap / gap_size) * 100
                    fill_status = FillStatus.PARTIAL
        
        elif gap_type == GapType.DOWNWARD:
            # High must reach or break previous close for a full fill
            if curr_high >= prev_close:
                fill_percentage = 100.0
                is_filled = True
                fill_status = FillStatus.FULL
            else:
                # Partial fill calculation
                # Gap zone is [curr_open, prev_close] (gap_size is negative)
                movement_into_gap = curr_high - curr_open
                if movement_into_gap > 0:
                    fill_percentage = (movement_into_gap / abs(gap_size)) * 100
                    fill_status = FillStatus.PARTIAL

        return GapStatus(
            gap_type=gap_type,
            gap_size=gap_size,
            gap_percentage=gap_percentage,
            fill_status=fill_status,
            fill_percentage=min(max(fill_percentage, 0.0), 100.0),
            is_filled=is_filled
        )
