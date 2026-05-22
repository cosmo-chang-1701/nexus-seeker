import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
import pandas as pd
import sys
import os
import discord

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
        assert report.title == "📊 Nexus Seeker 收盤資金流向與板塊輪動報告"
        assert report.fields[0].name == "🌐 收盤市場快照"
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
    agent.run_sector_flow_report = AsyncMock(
        return_value=discord.Embed(title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告")
    )
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


@pytest.mark.asyncio
async def test_dispatch_report_sends_each_block_as_separate_message():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    embed = discord.Embed(title="📊 測試報告", description="摘要")
    embed.add_field(name="區塊一", value="內容一", inline=False)
    embed.add_field(name="區塊二", value="內容二", inline=False)

    with patch("database.get_all_user_ids", return_value=[1]), patch(
        "database.get_full_user_context",
        return_value=MagicMock(enable_analyst_agent=True),
    ):
        await agent.dispatch_report(embed)

    assert bot.queue_dm.await_count == 2
    first_embed = bot.queue_dm.await_args_list[0].kwargs["embed"]
    second_embed = bot.queue_dm.await_args_list[1].kwargs["embed"]
    assert first_embed.fields[0].name == "區塊一"
    assert second_embed.fields[0].name == "區塊二"
    assert second_embed.description is None


@pytest.mark.asyncio
async def test_run_next_day_strategy_success():
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    agent._fetch_macro_data = AsyncMock(return_value={"vix": 14.5})

    with patch(
        "cogs.analyst_agent.get_vix_term_structure", new_callable=AsyncMock
    ) as mock_vts, patch(
        "cogs.analyst_agent.SentimentEngine.calculate_skew", new_callable=AsyncMock
    ) as mock_skew:
        mock_vts.return_value = {
            "vts_ratio": 0.85,
            "vts_state": "Contango",
            "vix_front": 14.5,
            "vix_back": 17.06,
        }
        mock_skew.return_value = {"skew": 6.5, "state": "⚠️ 預警性對沖 (Put 昂貴)"}

        report = await agent.run_next_day_strategy()

        assert "14.50" in report
        assert "0.850" in report
        assert "Contango" in report
        assert "17.06" in report
        assert "6.5%" in report
        assert "⚠️ 預警性對沖" in report
        assert "⚠️ 市場處於休眠期 (Dormant)。強制拒絕所有 STO 訊號。" in report


@pytest.mark.asyncio
async def test_run_next_day_strategy_failure_fallbacks():
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    agent._fetch_macro_data = AsyncMock(return_value={"vix": 20.0})

    with patch(
        "cogs.analyst_agent.get_vix_term_structure", new_callable=AsyncMock
    ) as mock_vts, patch(
        "cogs.analyst_agent.SentimentEngine.calculate_skew", new_callable=AsyncMock
    ) as mock_skew:
        mock_vts.side_effect = Exception("VTS failed")
        mock_skew.side_effect = Exception("Skew failed")

        report = await agent.run_next_day_strategy()

        assert "20.00" in report
        assert "取得失敗 (Using Default)" in report


@pytest.mark.asyncio
async def test_run_premarket_earnings_sorting_and_filtering():
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Calculate dates dynamically relative to current NY time
    from market_time import ny_tz

    today = datetime.now(ny_tz).date()

    mock_watchlist = [(1, "SYM_A", 1), (1, "SYM_B", 1), (1, "SYM_C", 1)]
    mock_portfolio = [(1, 123, "SYM_D")]

    # SYM_D is 1 day away (valid, index 0 after sort)
    # SYM_A is 2 days away (valid, index 1 after sort)
    # SYM_C is 5 days away (valid, index 2 after sort)
    # SYM_B is 15 days away (invalid, filtered out)
    # SYM_E has no earnings info (invalid, filtered out)
    mock_earnings = {
        "SYM_A": MagicMock(date=(today + timedelta(days=2)).strftime("%Y-%m-%d")),
        "SYM_B": MagicMock(date=(today + timedelta(days=15)).strftime("%Y-%m-%d")),
        "SYM_C": MagicMock(date=(today + timedelta(days=5)).strftime("%Y-%m-%d")),
        "SYM_D": MagicMock(date=(today + timedelta(days=1)).strftime("%Y-%m-%d")),
        "SYM_E": None,
    }

    with patch(
        "cogs.analyst_agent.get_all_watchlist", return_value=mock_watchlist
    ) as mock_get_wl, patch(
        "cogs.analyst_agent.database.get_all_portfolio", return_value=mock_portfolio
    ) as mock_get_pf, patch(
        "services.calendar_service.calendar_service.get_symbol_earnings_batch",
        new_callable=AsyncMock,
    ) as mock_get_batch, patch(
        "cogs.analyst_agent.fetch_recent_news", new_callable=AsyncMock
    ) as mock_fetch_news, patch(
        "cogs.analyst_agent.get_reddit_context", new_callable=AsyncMock
    ) as mock_fetch_reddit, patch(
        "cogs.analyst_agent.generate_analyst_report", new_callable=AsyncMock
    ) as mock_gen_report, patch(
        "cogs.analyst_agent.create_earnings_report_embed"
    ) as mock_create_embed:
        mock_get_batch.return_value = mock_earnings
        mock_fetch_news.return_value = "News content"
        mock_fetch_reddit.return_value = "Reddit content"
        mock_gen_report.return_value = "Report generated"
        mock_create_embed.return_value = discord.Embed(
            title="📊 Nexus Seeker 盤前財報與估值調整"
        )

        # Execute
        await agent.run_premarket_earnings()

        # Assertions
        mock_get_wl.assert_called_once()
        mock_get_pf.assert_called_once()

        # Verify get_symbol_earnings_batch was called with all symbols (order doesn't matter since it is a set)
        called_symbols = mock_get_batch.call_args[0][0]
        assert set(called_symbols) == {"SYM_A", "SYM_B", "SYM_C", "SYM_D"}

        # Verify generate_analyst_report was called with sorted list (max 10) and only within 14 days
        args, kwargs = mock_gen_report.call_args
        raw_data = args[1]

        # Check analyzed_symbols count (total 4 unique symbols)
        assert raw_data["analyzed_symbols"] == 4

        # Check upcoming_earnings is filtered and sorted (top 10 closest)
        upcoming = list(raw_data["upcoming_earnings"].keys())
        assert len(upcoming) == 3
        # Should be ordered by closeness: SYM_D (1 day), SYM_A (2 days), SYM_C (5 days)
        assert upcoming == ["SYM_D", "SYM_A", "SYM_C"]

        # Check sentiment scan targets (top 2 closest: SYM_D, SYM_A)
        assert mock_fetch_news.call_count == 2
        assert mock_fetch_reddit.call_count == 2

        news_calls = [c[0][0] for c in mock_fetch_news.call_args_list]
        assert set(news_calls) == {"SYM_D", "SYM_A"}
