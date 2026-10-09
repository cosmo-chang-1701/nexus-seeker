from typing import Any
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


def test_cli_help() -> None:
    """測試 CLI 說明文字"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Nexus Seeker Professional CLI Terminal" in result.output


def test_cli_health() -> None:
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


def test_cli_quote() -> None:
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


def test_cli_portfolio_empty() -> None:
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


def test_cli_watchlist_check() -> None:
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
        iv_percentile=65.0,
        option_skew=4.2,
        skew_percentile=60.0,
        option_skew_state="左偏 (Put 昂貴)",
        pcr=0.88,
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


def _macro_refresh_result(*steps: tuple[str, bool, str]) -> Any:
    from services.macro_refresh_service import MacroRefreshResult, RefreshStep

    return MacroRefreshResult(steps=[RefreshStep(*step) for step in steps])


def test_cli_force_macro_update() -> None:
    """force-macro-update 呼叫共用刷新流程（含 VTS 與核心指標），並逐項呈現結果"""
    result_obj = _macro_refresh_result(
        ("GEX", True, "SPY: $510.00 / Gamma Flip: 515.00"),
        ("流動性指標", True, "CP−T-Bill 利差: 0.15"),
        ("總經日曆", True, "已重新抓取並寫入快取"),
        ("FedWatch", True, "最新利率定價已寫入資料庫"),
        ("CPI 偏差值", True, "最新 CPI YoY 實際值與預測值已寫入資料庫"),
    )
    with patch("database.init_db"), patch(
        "services.macro_refresh_service.refresh_macro_data",
        new_callable=AsyncMock,
        return_value=result_obj,
    ) as mock_refresh:
        runner = CliRunner()
        result = runner.invoke(cli, ["admin", "force-macro-update"])
        assert result.exit_code == 0
        assert "開始手動觸發大盤總經爬蟲" in result.output
        assert "SPY: $510.00 / Gamma Flip: 515.00" in result.output
        assert "FedWatch" in result.output
        assert "CPI 偏差值" in result.output
        assert "全部成功" in result.output
        mock_refresh.assert_awaited_once_with(include_vts_and_core=True)


def test_cli_force_macro_update_reports_partial_failure() -> None:
    """任一步驟失敗時逐項標示失敗原因，並彙總成功/失敗數"""
    result_obj = _macro_refresh_result(
        ("GEX", False, "大盤端點與 SPY 即時估算皆無有效數據"),
        ("總經日曆", True, "已重新抓取並寫入快取"),
        ("CPI 偏差值", False, "日曆快取中無可用的已公布 CPI YoY 數據"),
    )
    with patch("database.init_db"), patch(
        "services.macro_refresh_service.refresh_macro_data",
        new_callable=AsyncMock,
        return_value=result_obj,
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["admin", "force-macro-update"])
        assert result.exit_code == 0
        assert "GEX 更新失敗" in result.output
        assert "大盤端點與 SPY 即時估算皆無有效數據" in result.output
        assert "CPI 偏差值 更新失敗" in result.output
        assert "2 項失敗、1 項成功" in result.output
        assert "全部成功" not in result.output
