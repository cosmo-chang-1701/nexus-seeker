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
        return_value=50.0,
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
    assert rows[0]["suggested_target_allocation_pct"] == 50.0
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


async def _rows_with_suggestion(
    assets: list[Any],
) -> tuple[list[dict[str, Any]], AsyncMock]:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "market_analysis.index_microstructure.suggest_target_allocation_pct",
        new_callable=AsyncMock,
        return_value=50.0,
    ) as mock_sug:
        rows = await build_holdings_display_rows(assets)
    return rows, mock_sug


@pytest.mark.asyncio
async def test_core_suggestion_split_equally_among_unset() -> None:
    rows, _ = await _rows_with_suggestion(
        [_asset("VOO"), _asset("QQQ"), _asset("SPY"), _asset("NVDA")]
    )
    for r in rows[:3]:
        assert r["suggested_target_allocation_pct"] == pytest.approx(50 / 3)
        assert r["suggested_core_bucket_pct"] == 50.0
        assert r["suggested_core_set_pct"] == 0.0
    assert "suggested_target_allocation_pct" not in rows[3]


@pytest.mark.asyncio
async def test_core_suggestion_subtracts_set_targets() -> None:
    rows, _ = await _rows_with_suggestion(
        [_asset("VOO", target_allocation_pct=0.2), _asset("QQQ"), _asset("SPY")]
    )
    assert "suggested_target_allocation_pct" not in rows[0]
    for r in rows[1:]:
        assert r["suggested_target_allocation_pct"] == pytest.approx(15.0)
        assert r["suggested_core_set_pct"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_core_suggestion_floors_at_zero_when_targets_exceed_bucket() -> None:
    rows, _ = await _rows_with_suggestion(
        [_asset("VOO", target_allocation_pct=0.6), _asset("QQQ")]
    )
    assert rows[1]["suggested_target_allocation_pct"] == 0.0


@pytest.mark.asyncio
async def test_core_suggestion_capped_by_max_allocation() -> None:
    rows, _ = await _rows_with_suggestion([_asset("QQQ", max_allocation_pct=0.1)])
    assert rows[0]["suggested_target_allocation_pct"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_core_suggestion_not_fetched_without_unset_core() -> None:
    rows, mock_sug = await _rows_with_suggestion([_asset("NVDA"), _asset("AMD")])
    mock_sug.assert_not_awaited()
    assert all("suggested_target_allocation_pct" not in r for r in rows)
