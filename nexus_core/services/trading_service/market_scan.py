"""盤前財報警報 Mixin。

原本的盤中 NRO 市場批次掃描 (`run_market_scan`) 已隨「盤中情報」模組移除。
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Set, TypedDict
from zoneinfo import ZoneInfo

import database

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


class EarningsAlert(TypedDict):
    symbol: str
    is_portfolio: bool
    earnings_date: date
    days_left: int


class MarketScanMixin:
    async def get_pre_market_alerts_data(
        self, warning_days: int
    ) -> Dict[int, Dict[str, Any]]:
        """
        取得盤前財報警報數據。
        """
        from services.calendar_service import calendar_service

        today = datetime.now(ny_tz).date()
        all_portfolios = database.get_all_portfolio()
        all_watchlists = database.get_all_watchlist()

        user_symbols: Dict[int, Dict[str, Set[str]]] = {}
        unique_symbols = set()

        for row in all_portfolios:
            uid, sym = row[0], row[2]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["port"].add(
                sym
            )
            unique_symbols.add(sym)

        for row in all_watchlists:
            uid, sym = row[0], row[1]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["watch"].add(
                sym
            )
            unique_symbols.add(sym)

        earnings_infos = await calendar_service.get_symbol_earnings_batch(
            list(unique_symbols)
        )
        earnings_cache: Dict[str, date] = {}
        for sym, earnings_info in earnings_infos.items():
            if earnings_info is None:
                continue
            e_date = datetime.strptime(earnings_info.date, "%Y-%m-%d").date()
            earnings_cache[sym] = e_date

        results = {}
        for uid, symbols_data in user_symbols.items():
            alerts: List[EarningsAlert] = []
            combined_symbols = symbols_data["port"].union(symbols_data["watch"])

            for sym in combined_symbols:
                cached_earnings_date: date | None = earnings_cache.get(sym)
                if cached_earnings_date:
                    days_left = (cached_earnings_date - today).days
                    if 0 <= days_left <= warning_days:
                        item: EarningsAlert = {
                            "symbol": sym,
                            "is_portfolio": sym in symbols_data["port"],
                            "earnings_date": cached_earnings_date,
                            "days_left": days_left,
                        }
                        alerts.append(item)

            # 🚀 根據距離財報天數升冪排序 (0天優先)
            alerts.sort(key=lambda x: x["days_left"])

            results[uid] = {
                "alerts": alerts,
                "scanned_symbols": sorted(combined_symbols),
            }
        return results
