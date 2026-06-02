import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import pandas as pd
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.analyst_agent import AnalystAgent


@pytest.mark.asyncio
async def test_analyst_agent_integration_with_sentiment_engine():
    """
    驗證 AnalystAgent 與 SentimentEngine 之間的集成。
    不直接 Mock SentimentEngine 的方法，而是 Mock 底層的數據服務，
    確保 AnalystAgent 能夠正確處理來自 SentimentEngine 的真實計算結果。
    """
    bot = MagicMock()
    # 防止 tasks.loop 在測試中啟動
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Mock 底層數據服務
    with patch(
        "cogs.analyst_agent.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch(
        "cogs.analyst_agent.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "cogs.analyst_agent.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch(
        "market_analysis.sentiment_engine.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
    ) as mock_exp, patch(
        "market_analysis.sentiment_engine.market_data_service.get_option_chain",
        new_callable=AsyncMock,
    ) as mock_chain, patch(
        "market_analysis.sentiment_engine.market_data_service.get_quote",
        new_callable=AsyncMock,
    ) as mock_quote_svc, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_httpx_get, patch(
        "cogs.analyst_agent.generate_analyst_report", new_callable=AsyncMock
    ) as mock_gen_report:
        # Mock AnalystAgent 的巨觀數據
        mock_macro.return_value = {"vix": 15.0}
        mock_quote.return_value = {"c": 500.0}

        # Mock 板塊歷史數據
        mock_hist.return_value = pd.DataFrame(
            {"Close": [100.0, 102.0], "Volume": [1000, 1100]},
            index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
        )

        # Mock SentimentEngine 使用的期權數據
        mock_exp.return_value = ["2024-06-21"]
        mock_quote_svc.return_value = {"c": 100.0}

        mock_chain_obj = MagicMock()
        # Skew 計算邏輯：Calls > 105, Puts < 95
        mock_chain_obj.calls = pd.DataFrame(
            {"strike": [105, 110], "impliedVolatility": [0.2, 0.22]}
        )
        mock_chain_obj.puts = pd.DataFrame(
            {"strike": [90, 95], "impliedVolatility": [0.25, 0.23]}
        )
        mock_chain.return_value = mock_chain_obj

        # Mock Polymarket
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_httpx_get.return_value = mock_resp

        # 執行報告生成
        await agent.run_sector_flow_report()

        # 驗證傳遞給 LLM 的原始數據
        assert mock_gen_report.called
        args, _ = mock_gen_report.call_args
        raw_data = args[1]

        # 驗證板塊數據中包含來自 SentimentEngine 的 Skew 計算結果
        # Skew = (0.25 - 0.22) * 100 = +3.0 (PutIV - CallIV)
        assert "sectors" in raw_data
        for sector in raw_data["sectors"]:
            assert "skew" in sector
            assert sector["skew"] == 3.0
            assert "skew_state" in sector
            assert sector["skew_state"] == "左偏 (Put 昂貴)"
