from unittest.mock import patch, AsyncMock, MagicMock

import pandas as pd
import pytest

from market_analysis.portfolio import (
    PortfolioStatusOrchestrator,
    get_option_chain_mid_iv,
)


def _make_orchestrator() -> PortfolioStatusOrchestrator:
    orchestrator = PortfolioStatusOrchestrator(user_capital=50000.0)
    orchestrator.spy_price = 500.0
    return orchestrator


@pytest.mark.asyncio
async def test_process_symbol_positions_fetches_option_chain_without_pruning() -> None:
    """既有持倉的 Greeks/P&L 計算應透過集中快取路徑
    (market_data_service.get_option_chain) 抓取期權鏈，且明確不裁減履約價範圍
    (prune_pct=None)，因為既有合約可能落在現價 ±10% 之外。"""
    orchestrator = _make_orchestrator()

    chain_mock = MagicMock()
    chain_mock.calls = pd.DataFrame(
        [
            {
                "strike": 150.0,
                "lastPrice": 5.0,
                "impliedVolatility": 0.3,
                "bid": 4.9,
                "ask": 5.1,
            }
        ]
    )
    chain_mock.puts = pd.DataFrame()

    row = ("AAPL", "call", 150.0, "2026-07-20", 5.0, 1, 0.0)

    with (
        patch(
            "market_analysis.portfolio.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 155.0},
        ),
        patch(
            "market_analysis.portfolio.market_data_service.is_etf",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("iv unavailable"),
        ),
        patch(
            "market_analysis.portfolio.get_option_chain",
            new_callable=AsyncMock,
            return_value=chain_mock,
        ) as mock_chain,
    ):
        await orchestrator._process_symbol_positions("AAPL", [row])

        mock_chain.assert_awaited_once_with("AAPL", "2026-07-20", prune_pct=None)
        assert len(orchestrator.report_lines) == 1


@pytest.mark.asyncio
async def test_process_symbol_positions_handles_none_chain_gracefully() -> None:
    """當 get_option_chain 因所有降級層皆失敗而回傳 None 時，該筆持倉應被安全
    跳過，而不是拋出例外中斷整個持倉風控結算。"""
    orchestrator = _make_orchestrator()
    row = ("AAPL", "call", 150.0, "2026-07-20", 5.0, 1, 0.0)

    with (
        patch(
            "market_analysis.portfolio.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 155.0},
        ),
        patch(
            "market_analysis.portfolio.market_data_service.is_etf",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "market_analysis.portfolio.market_data_service.get_dividend_yield",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("iv unavailable"),
        ),
        patch(
            "market_analysis.portfolio.get_option_chain",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        # 不應拋出例外
        await orchestrator._process_symbol_positions("AAPL", [row])

        assert orchestrator.report_lines == []


@pytest.mark.asyncio
async def test_get_option_chain_mid_iv_uses_centralized_option_chain() -> None:
    """get_option_chain_mid_iv 應透過集中快取路徑抓取期權鏈，不裁減履約價，
    且在 None 回傳時優雅退回 (0.0, 0.0) 而非拋出例外。"""
    chain_mock = MagicMock()
    chain_mock.calls = pd.DataFrame(
        [
            {
                "strike": 150.0,
                "bid": 4.9,
                "ask": 5.1,
                "lastPrice": 5.0,
                "impliedVolatility": 0.3,
            }
        ]
    )
    chain_mock.puts = pd.DataFrame()

    with patch(
        "market_analysis.portfolio.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain_mock,
    ) as mock_chain:
        mid, iv = await get_option_chain_mid_iv("AAPL", "2026-07-20", 150.0, "call")

        assert mid == pytest.approx(5.0)
        assert iv == pytest.approx(0.3)
        mock_chain.assert_awaited_once_with("AAPL", "2026-07-20", prune_pct=None)

    with patch(
        "market_analysis.portfolio.get_option_chain",
        new_callable=AsyncMock,
        return_value=None,
    ):
        mid, iv = await get_option_chain_mid_iv("AAPL", "2026-07-20", 150.0, "call")
        assert (mid, iv) == (0.0, 0.0)
