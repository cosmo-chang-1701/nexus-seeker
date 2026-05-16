import pytest
from click.testing import CliRunner
from unittest.mock import patch, MagicMock, AsyncMock
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cli import cli

def test_cli_help():
    """測試 CLI 說明文字"""
    runner = CliRunner()
    result = runner.invoke(cli, ['--help'])
    assert result.exit_code == 0
    assert 'Nexus Seeker Professional CLI Terminal' in result.output

def test_cli_health():
    """測試 health 指令"""
    with patch('services.market_data_service.get_macro_environment', new_callable=AsyncMock) as mock_macro, \
         patch('services.market_data_service.get_quote', new_callable=AsyncMock) as mock_quote, \
         patch('database.init_db'):
        
        mock_macro.return_value = {"vix": 18.5}
        mock_quote.return_value = {"c": 500.0}
        
        runner = CliRunner()
        result = runner.invoke(cli, ['health'])
        assert result.exit_code == 0
        assert 'VIX Index' in result.output
        assert '18.5' in result.output

def test_cli_quote():
    """測試 quote 指令"""
    with patch('services.market_data_service.get_quote', new_callable=AsyncMock) as mock_quote, \
         patch('database.init_db'):
        
        mock_quote.return_value = {
            "c": 200.0,
            "d": 5.0,
            "dp": 2.5,
            "h": 205.0,
            "l": 195.0
        }
        
        runner = CliRunner()
        result = runner.invoke(cli, ['quote', 'AAPL'])
        assert result.exit_code == 0
        assert 'AAPL' in result.output
        assert '$200.0' in result.output

def test_cli_portfolio_empty():
    """測試 portfolio 指令 (無持倉)"""
    with patch('services.trading_service.TradingService.get_portfolio_pnl', new_callable=AsyncMock) as mock_pnl, \
         patch('database.init_db'):
        
        mock_pnl.return_value = {'trades': [], 'total_unrealized_pnl': 0.0}
        
        runner = CliRunner()
        result = runner.invoke(cli, ['portfolio'])
        assert result.exit_code == 0
        assert '目前無持倉紀錄' in result.output
