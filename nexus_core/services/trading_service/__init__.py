"""核心交易業務邏輯，將 Discord 機器人的介面與底層計算/資料處理分離。

依領域拆分為：
- capital.py：使用者可用資本調整（get_adjusted_user_capital，BOXX 折算現金）
- execution.py：ExecutionMixin — 執行決策、DDP/IV 掃描、交易驗證管線
- market_scan.py：MarketScanMixin — 市場批次掃描（run_market_scan）與盤前財報警報
- vtr.py：VtrMixin — VTR 虛擬交易自動建倉、監控與對沖計算
- reports.py：ReportsMixin — 持倉損益、風險審計、盤後結算報告

`TradingService` 透過多重繼承（mixin）組合上述各領域方法；各 mixin 對彼此依賴的
`self.xxx` 屬性/方法皆以 `if TYPE_CHECKING:` 宣告型別存根（僅供 mypy 靜態檢查使用，
執行期永遠不會執行到），實際物件則由本檔案的 `TradingService.__init__` 統一建立。

`run_market_scan()` 內部原本有一個約 440 行、透過閉包捕捉 `df_spy`/`spy_price`/
`vix_spot` 的巢狀函式 `_scan_single_target`，拆檔時已攤平為 `MarketScanMixin` 自己
的一個方法（改以顯式參數傳遞取代閉包捕捉），呼叫端相應改為
`self._scan_single_target(t, df_spy, spy_price, vix_spot)`。
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
