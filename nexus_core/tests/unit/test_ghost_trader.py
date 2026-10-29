from unittest.mock import patch, MagicMock, AsyncMock

import pandas as pd
import pytest

from market_analysis.ghost_trader import GhostTrader


@pytest.mark.asyncio
async def test_get_option_mid_price_uses_centralized_option_chain() -> None:
    """get_option_mid_price 應透過 market_data_service.get_option_chain 抓取期權鏈
    (享有 edge tunnel 降級與快取保護)，而非直接呼叫裸 yfinance，且不裁減履約價範圍。"""
    chain_mock = MagicMock()
    chain_mock.calls = pd.DataFrame(
        [
            {
                "strike": 150.0,
                "bid": 1.0,
                "ask": 1.2,
                "lastPrice": 1.1,
                "impliedVolatility": 0.3,
            }
        ]
    )
    chain_mock.puts = pd.DataFrame()

    with patch(
        "market_analysis.ghost_trader.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain_mock,
    ) as mock_chain:
        trader = GhostTrader()
        mid, iv = await trader.get_option_mid_price("AAPL", "call", 150.0, "2026-07-20")

        assert mid == pytest.approx(1.1)
        assert iv == pytest.approx(0.3)
        mock_chain.assert_awaited_once_with("AAPL", "2026-07-20", prune_pct=None)


@pytest.mark.asyncio
async def test_get_option_mid_price_handles_none_chain_gracefully() -> None:
    """當 get_option_chain 因所有降級層皆失敗而回傳 None 時，應優雅回傳
    (None, None)，而不是拋出 AttributeError。"""
    with patch(
        "market_analysis.ghost_trader.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=None,
    ):
        trader = GhostTrader()
        mid, iv = await trader.get_option_mid_price("AAPL", "call", 150.0, "2026-07-20")

        assert mid is None
        assert iv is None


@pytest.mark.asyncio
async def test_find_target_contract_uses_centralized_fetch_and_handles_none_chain() -> (
    None
):
    """_find_target_contract 應改用 market_data_service.get_all_option_expiries /
    get_option_chain，並在 chain 為 None 時安全回傳 None。"""
    trader = GhostTrader()
    trader.today = pd.Timestamp("2026-06-11").date()

    with (
        patch(
            "market_analysis.ghost_trader.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=["2026-07-20"],
        ) as mock_expiries,
        patch(
            "market_analysis.ghost_trader.market_data_service.get_option_chain",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_chain,
    ):
        result = await trader._find_target_contract("AAPL", "call", 150.0)

        assert result is None
        mock_expiries.assert_awaited_once_with("AAPL")
        mock_chain.assert_awaited_once_with("AAPL", "2026-07-20", prune_pct=None)


def _dividend_chain() -> MagicMock:
    chain = MagicMock()
    chain.calls = pd.DataFrame({"strike": [150.0], "impliedVolatility": [0.3]})
    chain.puts = pd.DataFrame({"strike": [150.0], "impliedVolatility": [0.3]})
    return chain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "yield_value, expected_q",
    [(0.05, 0.05), (None, 0.0)],
)
async def test_find_target_contract_passes_dividend_yield_to_delta(
    yield_value: float | None, expected_q: float
) -> None:
    """_find_target_contract 的 delta 應帶入實際股息率；未知（None）時為 0.0。"""
    trader = GhostTrader()
    trader.today = pd.Timestamp("2026-06-11").date()

    with (
        patch(
            "market_analysis.ghost_trader.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=["2026-07-20"],
        ),
        patch(
            "market_analysis.ghost_trader.market_data_service.get_option_chain",
            new_callable=AsyncMock,
            return_value=_dividend_chain(),
        ),
        patch(
            "market_analysis.ghost_trader.market_data_service.get_dividend_yield_strict",
            new_callable=AsyncMock,
            return_value=yield_value,
        ),
        patch("market_analysis.ghost_trader.delta", return_value=0.2) as mock_delta,
    ):
        result = await trader._find_target_contract("KO", "call", 150.0)

    assert result == {"expiry": "2026-07-20", "strike": 150.0}
    assert mock_delta.call_args.args[-1] == expected_q


@pytest.mark.asyncio
async def test_dividend_yield_helper_falls_back_to_zero_on_error() -> None:
    """股息率查詢例外時應退回 0.0，不中斷流程。"""
    trader = GhostTrader()
    with patch(
        "market_analysis.ghost_trader.market_data_service.get_dividend_yield_strict",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        assert await trader._dividend_yield("KO") == 0.0


@pytest.mark.asyncio
async def test_execute_virtual_roll_passes_dividend_yield_to_delta() -> None:
    """execute_virtual_roll 的 delta 應帶入實際股息率。"""
    trader = GhostTrader()
    trader.today = pd.Timestamp("2026-06-11").date()
    trade = {
        "quantity": -1,
        "symbol": "KO",
        "opt_type": "put",
        "strike": 60.0,
        "expiry": "2026-07-20",
    }
    with (
        patch(
            "market_analysis.ghost_trader.get_all_open_virtual_trades",
            return_value=[trade],
        ),
        patch(
            "market_analysis.ghost_trader.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 62.0},
        ),
        patch.object(
            trader,
            "get_option_mid_price",
            new_callable=AsyncMock,
            return_value=(1.0, 0.25),
        ),
        patch(
            "market_analysis.ghost_trader.market_data_service.get_dividend_yield_strict",
            new_callable=AsyncMock,
            return_value=0.0314,
        ),
        patch("market_analysis.ghost_trader.delta", return_value=-0.1) as mock_delta,
    ):
        await trader.execute_virtual_roll()

    assert mock_delta.call_args.args[-1] == 0.0314
