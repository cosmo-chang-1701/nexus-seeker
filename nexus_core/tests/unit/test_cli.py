from click.testing import CliRunner
from unittest.mock import patch, AsyncMock
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cli import cli
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEventContext,
    WatchlistEvaluation,
    WatchlistTacticalPlan,
)


def test_cli_help():
    """測試 CLI 說明文字"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Nexus Seeker Professional CLI Terminal" in result.output


def test_cli_health():
    """測試 health 指令"""
    with patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch("database.init_db"):
        mock_macro.return_value = {"vix": 18.5}
        mock_quote.return_value = {"c": 500.0}

        runner = CliRunner()
        result = runner.invoke(cli, ["sys", "health"])
        assert result.exit_code == 0
        assert "VIX Index" in result.output
        assert "18.5" in result.output


def test_cli_quote():
    """測試 quote 指令"""
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch("database.init_db"):
        mock_quote.return_value = {
            "c": 200.0,
            "d": 5.0,
            "dp": 2.5,
            "h": 205.0,
            "l": 195.0,
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["mkt", "quote", "AAPL"])
        assert result.exit_code == 0
        assert "AAPL" in result.output
        assert "$200.0" in result.output


def test_cli_portfolio_empty():
    """測試 portfolio 指令 (無持倉)"""
    with patch(
        "services.trading_service.TradingService.get_portfolio_pnl",
        new_callable=AsyncMock,
    ) as mock_pnl, patch("database.init_db"):
        mock_pnl.return_value = {"trades": [], "total_unrealized_pnl": 0.0}

        runner = CliRunner()
        result = runner.invoke(cli, ["pf", "pnl"])
        assert result.exit_code == 0
        assert "目前無持倉紀錄" in result.output


def test_cli_watchlist_check():
    metrics = EnhancedWatchlistMetrics(
        symbol="AAPL",
        exchange="NASDAQ",
        current_price=180.0,
        buy_zone_status="🟢 買點：趨勢支撐 (VIX 修正)",
        buy_price_phase1=178.0,
        buy_price_phase2=172.0,
        buy_price_phase3=165.0,
        sell_zone_status="🟢 賣點：第一壓力帶",
        sell_price_phase1=185.0,
        sell_price_phase2=190.0,
        sell_price_phase3=196.0,
        pe_ratio=28.5,
        rsi_14=54.0,
        atr_14=4.2,
        beta=1.1,
        ma20=176.0,
        ma50=170.0,
        ma200=158.0,
        iv_rank=71.0,
        option_skew=4.2,
        option_skew_state="正常",
        volume_poc=174.5,
        gex_max_put_wall=168.0,
        vanna_sensitivity=0.42,
        relative_strength_spy=0.03,
    )
    evaluation = WatchlistEvaluation(
        metrics=metrics,
        tactical=WatchlistTacticalPlan(
            scenario="premium-harvest",
            sddm_route="SHIELD (防禦網格 - 左側權利金收集)",
            action_guideline="建議以 Phase 2 建立 Cash-Secured Put。",
            dynamic_grid_step=2.1,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="yellow",
        ),
        event_context=WatchlistEventContext(
            risk_mode="normal",
            summary="未偵測到近期需調整參數的重大事件。",
        ),
    )

    with patch("database.init_db"), patch(
        "database.watchlist.get_user_watchlist", return_value=[("AAPL", 1)]
    ), patch(
        "market_analysis.intraday_pipeline.evaluate_watchlist_symbol",
        new_callable=AsyncMock,
        return_value=evaluation,
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["mkt", "watchlist_check"])
        assert result.exit_code == 0
        assert "AAPL | NASDAQ" in result.output
        assert "SHIELD (防禦網格 - 左側權利金收集)" in result.output
        assert "```ansi" in result.output
