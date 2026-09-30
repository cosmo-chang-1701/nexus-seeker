"""核心交易業務邏輯，將 Discord 機器人的介面與底層計算/資料處理分離。

依領域拆分為：
- capital.py：使用者可用資本調整（get_adjusted_user_capital，BOXX 折算現金）
- execution.py：ExecutionMixin — 執行決策、DDP/IV 掃描、交易驗證管線
- market_scan.py：MarketScanMixin — 盤前財報警報（盤中 NRO 批次掃描已移除）
- vtr.py：VtrMixin — VTR 虛擬交易監控與對沖計算（NRO 自動建倉已移除）
- reports.py：ReportsMixin — 持倉損益、風險審計、盤後結算報告

`TradingService` 透過多重繼承（mixin）組合上述各領域方法；各 mixin 對彼此依賴的
`self.xxx` 屬性/方法皆以 `if TYPE_CHECKING:` 宣告型別存根（僅供 mypy 靜態檢查使用，
執行期永遠不會執行到），實際物件則由本檔案的 `TradingService.__init__` 統一建立。
"""

from typing import Any

from market_analysis.ddp_inspector import DDPInspector
from market_analysis.volatility_inspector import VolatilityInspector
from market_analysis.ghost_trader import GhostTrader
from services.execution_router import ExecutionRouter

from services.trading_service.capital import get_adjusted_user_capital
from services.trading_service.execution import ExecutionMixin
from services.trading_service.market_scan import EarningsAlert, MarketScanMixin
from services.trading_service.vtr import VtrMixin
from services.trading_service.reports import ReportsMixin

__all__ = [
    "get_adjusted_user_capital",
    "EarningsAlert",
    "TradingService",
]


class TradingService(ExecutionMixin, MarketScanMixin, VtrMixin, ReportsMixin):
    """
    提供核心交易業務邏輯，將 Discord 機器人的介面與底層計算/資料處理分離。
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.vtr_engine = GhostTrader()
        self.ddp_inspector = DDPInspector(bot)
        self.vol_inspector = VolatilityInspector(bot)
        self.execution_router = ExecutionRouter()
