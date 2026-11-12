"""build_holdings_display_rows 與 /dash 📦 共用性測試。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.terminal.holdings import build_holdings_display_rows


def _asset(symbol: str, **meta: Any) -> MagicMock:
    a = MagicMock()
    a.id = 1
    a.symbol = symbol
    a.metadata = meta
    return a


@pytest.mark.asyncio
async def test_build_rows_core_default_and_fields() -> None:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "market_analysis.index_microstructure.suggest_target_allocation_pct",
        new_callable=AsyncMock,
        return_value=0.5,
    ):
        rows = await build_holdings_display_rows(
            [
                _asset("VOO", quantity=1, acquired_at="2024-01-02"),
                _asset("NVDA", quantity=2),
            ]
        )
    assert rows[0]["asset_class"] == "CORE"
    assert rows[0]["max_allocation_pct"] == 1.0
    assert rows[0]["acquired_at"] == "2024-01-02"
    assert rows[0]["suggested_target_allocation_pct"] == 0.5
    assert rows[1]["asset_class"] == "SATELLITE"
    assert rows[1]["max_allocation_pct"] == 0.3


@pytest.mark.asyncio
async def test_build_rows_fetches_quotes_once_per_symbol() -> None:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 10.0},
    ) as mock_quote:
        rows = await build_holdings_display_rows(
            [_asset("NVDA", quantity=1), _asset("NVDA", quantity=2)]
        )
    assert len(rows) == 2
    mock_quote.assert_awaited_once_with("NVDA")


@pytest.mark.asyncio
async def test_btn_holdings_uses_shared_helper() -> None:
    from cogs.unified_terminal.portfolio_view import PortfolioHubView

    view = PortfolioHubView(user_id=123, bot=MagicMock())
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    with patch(
        "services.asset_manager.AssetManager.get_assets", return_value=[]
    ), patch(
        "cogs.terminal.holdings.build_holdings_display_rows",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_helper, patch(
        "database.user_settings.get_full_user_context",
        return_value=MagicMock(capital=1000),
    ):
        await view.btn_holdings.callback(interaction)
    mock_helper.assert_awaited_once()
