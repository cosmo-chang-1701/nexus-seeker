from click.testing import CliRunner
from unittest.mock import patch, AsyncMock
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cli import cli
import config
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEventContext,
    WatchlistEvaluation,
    WatchlistTacticalPlan,
)


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
        result = runner.invoke(cli, ["--db", config.DB_NAME, "sys", "health"])

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
            cli, ["--db", config.DB_NAME, "--user-id", str(user_id), "pf", "pnl"]
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
        result = runner.invoke(cli, ["--db", config.DB_NAME, "mkt", "ddp"])

        assert result.exit_code == 0
        assert "正在對 1 個標的執行 DDP 掃描" in result.output
        assert "MSFT" in result.output


def test_cli_watchlist_check_integration(db_conn):
    """測試 CLI watchlist_check 指令與資料庫 watchlist 的整合。"""
    from database.watchlist import add_watchlist_symbol

    user_id = 123456789
    add_watchlist_symbol(user_id, "NVDA")
    add_watchlist_symbol(user_id, "TSLA")

    def _evaluation(symbol: str) -> WatchlistEvaluation:
        metrics = EnhancedWatchlistMetrics(
            symbol=symbol,
            exchange="NASDAQ",
            current_price=150.0,
            buy_zone_status="🟢 買點：趨勢支撐 (VIX 修正)",
            buy_price_phase1=145.0,
            buy_price_phase2=140.0,
            buy_price_phase3=135.0,
            sell_zone_status="🟢 賣點：第一壓力帶",
            sell_price_phase1=155.0,
            sell_price_phase2=160.0,
            sell_price_phase3=166.0,
            pe_ratio=25.0,
            rsi_14=52.0,
            atr_14=5.0,
            beta=1.15,
            ma20=146.0,
            ma50=142.0,
            ma200=130.0,
            iv_rank=72.0,
            iv_percentile=70.0,
            option_skew=5.4,
            skew_percentile=85.0,
            option_skew_state="⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）",
            pcr=1.05,
            volume_poc=144.0,
            gex_max_put_wall=138.0,
            vanna_sensitivity=0.3,
            relative_strength_spy=0.02,
        )
        return WatchlistEvaluation(
            metrics=metrics,
            tactical=WatchlistTacticalPlan(
                scenario="premium-harvest",
                sddm_route="SHIELD (防禦網格 - 左側權利金收集)",
                action_guideline=f"{symbol} 建議以 Phase 2 建立 Cash-Secured Put。",
                dynamic_grid_step=2.5,
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

    with patch(
        "market_analysis.intraday_pipeline.evaluate_watchlist_symbol",
        new_callable=AsyncMock,
    ) as mock_evaluate:
        mock_evaluate.side_effect = lambda symbol: _evaluation(symbol)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--db",
                config.DB_NAME,
                "--user-id",
                str(user_id),
                "mkt",
                "watchlist_check",
            ],
        )

    assert result.exit_code == 0
    assert result.output.count("```ansi") == 2
    assert "NVDA | NASDAQ" in result.output
    assert "TSLA | NASDAQ" in result.output
    assert "SHIELD (防禦網格 - 左側權利金收集)" in result.output
