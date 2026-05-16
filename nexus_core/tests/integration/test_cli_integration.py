from click.testing import CliRunner
from unittest.mock import patch, AsyncMock
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cli import cli
import config


def test_cli_health_integration(mock_market_data):
    """測試 CLI health 指令與市場數據服務的整合"""
    mock_price, _ = mock_market_data
    mock_price.return_value = {"c": 510.0}

    with patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro:
        mock_macro.return_value = {"vix": 15.0}

        runner = CliRunner()
        # 指定使用測試資料庫
        result = runner.invoke(cli, ["--db", config.DB_NAME, "health"])

        assert result.exit_code == 0
        assert "15.0" in result.output
        assert "$510.0" in result.output


def test_cli_portfolio_integration(db_conn, mock_market_data):
    """測試 CLI portfolio 指令與資料庫及 PnL 計算的整合"""
    from database.portfolio import add_portfolio_record

    user_id = 123456789

    # 插入測試資料
    add_portfolio_record(
        user_id, "AAPL", "CALL", 150.0, "2026-06-19", 5.0, 1, 0.0, 0.5, -0.1, 0.01
    )

    # Mock 期權價格獲取
    with patch("market_analysis.portfolio.get_option_chain_mid_iv") as mock_mid:
        mock_mid.return_value = (6.0, 0.25)  # mid_price, iv

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--db", config.DB_NAME, "--user-id", str(user_id), "portfolio"]
        )

        assert result.exit_code == 0
        assert "AAPL" in result.output
        assert "$100.00" in result.output


def test_cli_scan_ddp_integration(db_conn):
    """測試 CLI scan-ddp 指令與 Watchlist 的整合"""
    from database.watchlist import add_watchlist_symbol

    user_id = 123456789

    add_watchlist_symbol(user_id, "MSFT")

    with patch(
        "services.trading_service.TradingService.run_ddp_scan", new_callable=AsyncMock
    ) as mock_scan:
        mock_scan.return_value = [{"symbol": "MSFT", "reason": "High Growth"}]

        runner = CliRunner()
        result = runner.invoke(cli, ["--db", config.DB_NAME, "scan-ddp"])

        assert result.exit_code == 0
        assert "正在掃描 1 個標的" in result.output
        assert "MSFT" in result.output
