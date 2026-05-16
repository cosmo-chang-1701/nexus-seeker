import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import pandas as pd
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.analyst_agent import AnalystAgent, SECTORS


@pytest.mark.asyncio
async def test_run_sector_flow_report():
    # Mock bot
    bot = MagicMock()

    # Patch loops to prevent them from starting and failing due to missing discord environment
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Mock dependencies
    with patch(
        "cogs.analyst_agent.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch(
        "cogs.analyst_agent.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "cogs.analyst_agent.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch(
        "cogs.analyst_agent.SentimentEngine.calculate_skew", new_callable=AsyncMock
    ) as mock_skew, patch(
        "cogs.analyst_agent.SentimentEngine.detect_uoa", new_callable=AsyncMock
    ) as mock_uoa, patch(
        "cogs.analyst_agent.SentimentEngine.calculate_max_pain", new_callable=AsyncMock
    ) as mock_max_pain, patch(
        "cogs.analyst_agent.generate_analyst_report", new_callable=AsyncMock
    ) as mock_gen_report, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_httpx_get:
        # Set up mock returns
        mock_macro.return_value = {"vix": 20.0}
        mock_quote.return_value = {"c": 500.0}

        # Mock history DF for sectors
        df = pd.DataFrame(
            {"Close": [100.0, 105.0], "Volume": [1000, 1200]},
            index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
        )
        mock_hist.return_value = df

        mock_skew.return_value = {"skew": 5.0, "state": "WARNING"}
        mock_uoa.return_value = [{"symbol": "XLK"}]
        mock_max_pain.return_value = {"max_pain": 490.0}

        # Mock Polymarket response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "question": "Test?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.6", "0.4"],
            }
        ]
        mock_httpx_get.return_value = mock_resp

        mock_gen_report.return_value = "Generated Report Content"

        # Execute
        report = await agent.run_sector_flow_report()

        # Verify
        assert report == "Generated Report Content"
        mock_gen_report.assert_called_once()

        # Check that it called get_history_df for all sectors
        assert mock_hist.call_count >= len(SECTORS)

        # Verify the structure of raw_data passed to generate_analyst_report
        args, kwargs = mock_gen_report.call_args
        raw_data = args[1]
        assert raw_data["vix"] == 20.0
        assert raw_data["spy_price"] == 500.0
        assert len(raw_data["sectors"]) == len(SECTORS)
        assert raw_data["sectors"][0]["symbol"] in SECTORS
        assert "pct_change" in raw_data["sectors"][0]
        assert "rel_vol" in raw_data["sectors"][0]
        assert raw_data["spy_max_pain"]["max_pain"] == 490.0


@pytest.mark.asyncio
async def test_post_market_loop_triggers_sector_report():
    # Mock bot
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Mock methods called in post_market_loop
    agent.run_postmarket_summary = AsyncMock(return_value="Summary")
    agent.run_sector_flow_report = AsyncMock(return_value="Sector Report")
    agent.run_next_day_strategy = AsyncMock(return_value="Next Day")
    agent.dispatch_report = AsyncMock()

    # Mock timing functions to avoid sleeping
    with patch("cogs.analyst_agent.get_next_market_target_time") as mock_target, patch(
        "cogs.analyst_agent.get_sleep_seconds"
    ) as mock_sleep, patch("asyncio.sleep", new_callable=AsyncMock) as mock_async_sleep:
        mock_target.return_value = "sometime"
        mock_sleep.return_value = 0.1

        # We need to break the while True loop in post_market_loop
        # We can use side_effect to raise an exception or just let it run once if we can mock the loop differently
        # But post_market_loop is a @tasks.loop(count=1) with a while True inside.

        # Let's mock the while True by making it run once then raising an error we catch
        mock_async_sleep.side_effect = [None, Exception("Stop loop")]

        try:
            await agent.post_market_loop()
        except Exception as e:
            if str(e) != "Stop loop":
                raise e

        # Verify run_sector_flow_report was called
        agent.run_sector_flow_report.assert_called_once()
        # Verify dispatch_report was called for sector report and next day strategy
        assert agent.dispatch_report.call_count == 2

        # 檢查第一個調用 (Sector Report)
        first_call_args = agent.dispatch_report.call_args_list[0][0][0]
        assert first_call_args.title == "📊 Nexus Seeker 收盤資金流向與板塊輪動報告"

        # 檢查第二個調用 (Next Day Strategy)
        second_call_args = agent.dispatch_report.call_args_list[1][0][0]
        assert second_call_args.title == "🎯 Nexus Seeker 次日策略制定"
