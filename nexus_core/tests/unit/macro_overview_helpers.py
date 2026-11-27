"""get_macro_overview_data 的離線測試輔助：替換報價、kv 快取與所有對外抓取。"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


async def run_macro_overview(
    quotes: dict[str, Any], kv: dict[str, Any]
) -> tuple[dict[str, Any], MagicMock]:
    """在完全離線的環境下執行 get_macro_overview_data，並回傳結果與 KV 寫入 mock。"""
    from cogs.unified_terminal.utils import get_macro_overview_data

    save_mock = AsyncMock()

    async def _quote(symbol: str) -> Any:
        return quotes.get(symbol)

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("database.get_kv_cache", side_effect=lambda k, *a, **kw: kv.get(k)),
        patch("database.save_kv_cache", save_mock),
        patch("services.market_data_service.get_quote", side_effect=_quote),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "cogs.unified_terminal.utils.get_credit_spreads",
            new_callable=AsyncMock,
            return_value={"hy": None, "ig": None},
        ),
    ):
        data = await get_macro_overview_data(1)
        # 讓 create_task 寫入有機會執行
        await asyncio.sleep(0)
    return data, save_mock
