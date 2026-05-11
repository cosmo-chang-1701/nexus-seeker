from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class GapStatus(str, Enum):
    GAP_HOLDING = "GAP_HOLDING"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    NO_GAP = "NO_GAP"


class GapMetrics(BaseModel):
    symbol: str
    gap_size: float = Field(description="Today's Open - Yesterday's Close")
    gap_pct: float = Field(description="Gap size as a percentage of previous close")
    gap_zone: tuple[float, float] = Field(
        description="(Lower Bound, Upper Bound) of the gap"
    )
    current_fill_status: GapStatus
    is_support_confirmed: bool = Field(
        default=False, description="True if price entered zone but rebounded strongly"
    )
    intraday_low: float
    prev_close: float


class GapAnalyzer:
    """
    量化跳空分析引擎 (Gap & Fill Monitor Engine)：
    監控盤中價格與跳空區間 (Gap Zone) 的互動，驗證技術支撐與阻力。
    """

    @staticmethod
    def analyze_gap(df: pd.DataFrame) -> Optional[GapMetrics]:
        """
        分析 OHLCV 數據以計算跳空指標。
        df 必須包含至少兩天的數據，最後一列為當前交易日。
        """
        if df is None or len(df) < 2:
            return None

        try:
            prev_day = df.iloc[-2]
            curr_day = df.iloc[-1]

            prev_close = float(prev_day["Close"])
            curr_open = float(curr_day["Open"])
            curr_low = float(curr_day["Low"])
            curr_high = float(curr_day["High"])
            curr_price = float(curr_day["Close"])

            gap_size = curr_open - prev_close
            gap_pct = (gap_size / prev_close) * 100

            # 門檻判定：若跳空幅度小於 0.3%，視為無跳空以過濾雜訊
            if abs(gap_pct) < 0.3:
                return None

            # 定義跳空區間 (Gap Zone)
            gap_zone = (min(prev_close, curr_open), max(prev_close, curr_open))

            # 判定填補狀態 (以向上跳空為例)
            status = GapStatus.GAP_HOLDING
            is_support_confirmed = False

            if gap_size > 0:  # Up-Gap
                if curr_low <= prev_close:
                    status = GapStatus.FULL_FILL
                elif curr_low < curr_open:
                    status = GapStatus.PARTIAL_FILL
                    # 支撐確認邏輯：若最低價進入區間但收盤價回升至區間上方，且留有下影線
                    if (
                        curr_price > curr_open
                        and (curr_price - curr_low) > (curr_open - prev_close) * 0.5
                    ):
                        is_support_confirmed = True
                else:
                    status = GapStatus.GAP_HOLDING
            else:  # Down-Gap
                if curr_high >= prev_close:
                    status = GapStatus.FULL_FILL
                elif curr_high > curr_open:
                    status = GapStatus.PARTIAL_FILL
                else:
                    status = GapStatus.GAP_HOLDING

            return GapMetrics(
                symbol=str(df.index.name or "UNKNOWN"),
                gap_size=round(gap_size, 2),
                gap_pct=round(gap_pct, 2),
                gap_zone=gap_zone,
                current_fill_status=status,
                is_support_confirmed=is_support_confirmed,
                intraday_low=curr_low,
                prev_close=prev_close,
            )

        except Exception as e:
            logger.error(f"Gap 分析失敗: {e}")
            return None
