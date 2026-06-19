"""領域模型：交易員帳戶狀態、期權持倉、標的行情、決策輸出。

從 intraday_pipeline.py 中分離，無外部依賴，可獨立測試。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


class TraderAccountState(BaseModel):
    """交易員帳戶生存狀態"""

    model_config = ConfigDict()

    capital: float = Field(description="總風險本金 (Total Risk Capital)")
    cash_reserve: float = Field(description="生活費現金儲備 (Liquid Cash Reserve)")
    monthly_burn_rate: float = Field(
        description="每月固定生活開銷 (Monthly Living Expenses)"
    )
    current_vix: float = Field(description="即時 VIX 指數 (Real-time VIX level)")


class OptionHolding(BaseModel):
    """現有期權部位持倉"""

    model_config = ConfigDict()

    symbol: str = Field(description="標的代碼")
    quantity: float = Field(description="合約數量 (正數為買方，負數為賣方)")
    theta: float = Field(description="單口每日 Theta 衰退值 (通常買方為負，賣方為正)")


class TickerMarketData(BaseModel):
    """標的市場行情數據"""

    model_config = ConfigDict()

    ticker: str = Field(description="標的代碼")
    spot_price: float = Field(description="標的現價 (Spot Price)")
    market_cap_billion: float = Field(description="公司市值（十億美元）")
    avg_option_volume: int = Field(description="日均期權成交量")
    days_until_earnings: int = Field(description="距離財報公佈天數")
    tomorrow_expiring_otm_calls_premium: float = Field(
        description="明日到期 OTM Call 總成交權利金 (Sum of vol * price * 100)"
    )
    iv_rank: float = Field(description="隱含波動率百分位數 (0-100)")
    option_skew: float = Field(description="期權偏斜度 (Option Skew)")


class AdvancedTraderOutput(BaseModel):
    """量化風控與執行決策輸出 (繁體中文格式化)"""

    model_config = ConfigDict()

    ticker: str
    timestamp: datetime
    market_phase: str  # "Phase A", "Phase B", "Phase C"
    is_applicable: bool
    failed_gates: List[str]
    sddm_route: str  # "SPEAR", "SHIELD", or "WAIT"

    # Financial Runway & Survival Section
    financial_runway_days: int
    theta_coverage_pct: float
    runway_status_msg: str

    # Tactical Execution Section
    magnet_target: Optional[float]
    recommended_actions: List[str]
    vanna_hedging_instruction: Optional[str]
    kelly_position_scaling: float
    risk_mitigation_notes: str
