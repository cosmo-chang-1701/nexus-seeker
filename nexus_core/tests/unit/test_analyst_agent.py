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
        "market_analysis.analyst_runners.sector_runner.get_macro_environment",
        new_callable=AsyncMock,
    ) as mock_macro, patch(
        "market_analysis.analyst_runners.sector_runner.get_quote",
        new_callable=AsyncMock,
    ) as mock_quote, patch(
        "market_analysis.analyst_runners.sector_runner.get_history_df",
        new_callable=AsyncMock,
    ) as mock_hist, patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as mock_skew, patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
    ) as mock_uoa, patch(
        "market_analysis.analyst_runners.sector_runner.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
    ) as mock_max_pain, patch(
        "market_analysis.analyst_runners.sector_runner.generate_analyst_report",
        new_callable=AsyncMock,
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
    agent.dispatch_post_market_intelligence = AsyncMock()

    # Mock timing functions to avoid sleeping
    with patch("cogs.analyst_agent.get_next_market_target_time") as mock_target, patch(
        "cogs.analyst_agent.get_sleep_seconds"
    ) as mock_sleep, patch("asyncio.sleep", new_callable=AsyncMock) as mock_async_sleep:
        mock_target.return_value = "sometime"
        mock_sleep.return_value = 0.1

        # We need to break the while True loop in post_market_loop
        # Let's mock the while True by making it run once then raising an error we catch
        mock_async_sleep.side_effect = [None, Exception("Stop loop")]

        try:
            await agent.post_market_loop()
        except Exception as e:
            if str(e) != "Stop loop":
                raise e

        # Verify dispatch_post_market_intelligence was called
        agent.dispatch_post_market_intelligence.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_report_sends_each_block_as_separate_message():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    embed = discord.Embed(title="📊 測試報告", description="摘要")
    embed.add_field(name="區塊一", value="內容一", inline=False)
    embed.add_field(name="區塊二", value="內容二", inline=False)

    mock_split = MagicMock(
        return_value=[
            discord.Embed(title="📊 測試報告 (1/2)", description="摘要").add_field(
                name="區塊一", value="內容一", inline=False
            ),
            discord.Embed(title="📊 測試報告 (2/2)").add_field(
                name="區塊二", value="內容二", inline=False
            ),
        ]
    )

    with patch("database.get_all_user_ids", return_value=[1]), patch(
        "database.get_full_user_context",
        return_value=MagicMock(enable_analyst_agent=True),
    ), patch("cogs.analyst_agent.split_embed_by_fields", mock_split):
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
        "market_analysis.analyst_runners.strategy_runner.get_vix_term_structure",
        new_callable=AsyncMock,
    ) as mock_vts, patch(
        "market_analysis.analyst_runners.strategy_runner.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
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
        "market_analysis.analyst_runners.strategy_runner.get_vix_term_structure",
        new_callable=AsyncMock,
    ) as mock_vts, patch(
        "market_analysis.analyst_runners.strategy_runner.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
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
        "market_analysis.analyst_runners.earnings_runner.get_all_watchlist",
        return_value=mock_watchlist,
    ) as mock_get_wl, patch(
        "market_analysis.analyst_runners.earnings_runner.database.get_all_portfolio",
        return_value=mock_portfolio,
    ) as mock_get_pf, patch(
        "services.calendar_service.calendar_service.get_symbol_earnings_batch",
        new_callable=AsyncMock,
    ) as mock_get_batch, patch(
        "market_analysis.analyst_runners.earnings_runner.fetch_recent_news",
        new_callable=AsyncMock,
    ) as mock_fetch_news, patch(
        "market_analysis.analyst_runners.earnings_runner.get_reddit_context",
        new_callable=AsyncMock,
    ) as mock_fetch_reddit, patch(
        "market_analysis.analyst_runners.earnings_runner.generate_analyst_report",
        new_callable=AsyncMock,
    ) as mock_gen_report, patch(
        "market_analysis.analyst_runners.earnings_runner.create_earnings_report_embed"
    ) as mock_create_embed, patch(
        "market_analysis.analyst_runners.earnings_runner.evaluate_watchlist_symbol",
        new_callable=AsyncMock,
    ) as mock_eval_symbol, patch(
        "market_analysis.analyst_runners.earnings_runner.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
    ) as mock_calc_pcr, patch(
        "market_analysis.analyst_runners.earnings_runner.market_data_service.get_company_profile",
        new_callable=AsyncMock,
    ) as mock_get_profile:
        mock_get_batch.return_value = mock_earnings
        mock_fetch_news.return_value = "News content"
        mock_fetch_reddit.return_value = "Reddit content"
        mock_gen_report.return_value = "Report generated"
        mock_create_embed.return_value = discord.Embed(
            title="📊 Nexus Seeker 盤前財報與估值調整"
        )

        # Define mock metrics for SYM_A to test successful data enrichment
        mock_eval = MagicMock()
        mock_eval.metrics = MagicMock(
            current_price=150.0,
            rsi_14=62.0,
            pe_ratio=25.0,
            bias_ma20=0.05,
            iv_rank=85.0,
            option_skew=-0.02,
            option_skew_state="Bullish",
            beta=1.1,
            relative_strength_spy=1.05,
            buy_zone_status="Buy Zone",
            sell_zone_status="Hold",
        )

        async def mock_eval_side_effect(symbol):
            if symbol == "SYM_A":
                return mock_eval
            return None

        mock_eval_symbol.side_effect = mock_eval_side_effect
        mock_calc_pcr.return_value = {"pcr": 1.2, "state": "偏向空頭"}
        mock_get_profile.return_value = {"finnhubIndustry": "Semiconductors"}

        # Execute
        await agent.run_premarket_earnings()

        # Assertions
        mock_get_wl.assert_called_once()
        mock_get_pf.assert_called_once()

        # Verify get_symbol_earnings_batch was called with all symbols (order doesn't matter since it is a set)
        called_symbols = mock_get_batch.call_args[0][0]
        assert set(called_symbols) == {"SYM_A", "SYM_B", "SYM_C", "SYM_D"}

        # Verify evaluate_watchlist_symbol and calculate_pcr were called in parallel ONLY for top deep scan symbols (days_left <= 2: SYM_D, SYM_A)
        assert mock_eval_symbol.call_count == 2
        eval_calls = [c[0][0] for c in mock_eval_symbol.call_args_list]
        assert set(eval_calls) == {"SYM_D", "SYM_A"}

        assert mock_calc_pcr.call_count == 2

        # Verify get_company_profile was called for all 3 sorted symbols (deep + light scan)
        assert mock_get_profile.call_count == 3

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

        # Check data enrichment for SYM_A (successful deep scan fetch)
        a_metrics = raw_data["upcoming_earnings"]["SYM_A"][0]
        assert a_metrics["current_price"] == 150.0
        assert a_metrics["rsi_14"] == 62.0
        assert a_metrics["pe_ratio"] == 25.0
        assert a_metrics["bias_ma20"] == 0.05
        assert a_metrics["iv_rank"] == 85.0
        assert a_metrics["option_skew"] == -0.02
        assert a_metrics["pcr"] == 1.2
        assert a_metrics["sector"] == "Semiconductors"

        # Check data enrichment for SYM_D (graceful deep scan fallback)
        d_metrics = raw_data["upcoming_earnings"]["SYM_D"][0]
        assert d_metrics["current_price"] == 0.0
        assert d_metrics["rsi_14"] == 50.0
        assert d_metrics["pe_ratio"] is None
        assert d_metrics["bias_ma20"] == 0.0
        assert d_metrics["pcr"] == 1.2
        assert d_metrics["sector"] == "Semiconductors"

        # Check data enrichment for SYM_C (successful light scan)
        c_metrics = raw_data["upcoming_earnings"]["SYM_C"][0]
        assert c_metrics["current_price"] == 0.0
        assert c_metrics["rsi_14"] == 50.0
        assert c_metrics["pe_ratio"] is None
        assert c_metrics["bias_ma20"] == 0.0
        assert c_metrics["pcr"] == 0.0  # PCR skipped for light scan
        assert c_metrics["sector"] == "Semiconductors"

        # Check sentiment scan targets (top 2 closest: SYM_D, SYM_A)
        assert mock_fetch_news.call_count == 2

        # Reddit tunnel 呼叫需由 /settings 啟用；預設關閉時，不應觸發本地 Tunnel I/O
        assert mock_fetch_reddit.call_count == 0

        news_calls = [c[0][0] for c in mock_fetch_news.call_args_list]
        assert set(news_calls) == {"SYM_D", "SYM_A"}


@pytest.mark.asyncio
async def test_dispatch_post_market_intelligence_runway_fallback():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Mock gather_sector_rotation_data
    agent.gather_sector_rotation_data = AsyncMock(
        return_value={
            "vix": 15.0,
            "vix_tier_name": "Low",
            "spy_price": 500.0,
            "sectors": [],
            "poly_events": [],
            "spy_max_pain": {"max_pain": 500.0},
        }
    )

    user_ctx = MagicMock()
    user_ctx.enable_analyst_agent = True
    user_ctx.capital = 100000.0
    user_ctx.cash_reserve = 60000.0
    user_ctx.monthly_expense = 3000.0
    user_ctx.total_theta = 0.0  # daily theta

    with patch("database.purge_old_cache", return_value=0), patch(
        "services.trading_service.TradingService.get_after_market_report_data",
        new_callable=AsyncMock,
    ) as mock_get_data, patch("database.get_all_user_ids", return_value=[12345]), patch(
        "database.is_notification_enabled", return_value=True
    ), patch("database.get_full_user_context", return_value=user_ctx), patch(
        "cogs.analyst_agent.generate_analyst_report", new_callable=AsyncMock
    ) as mock_gen_report, patch("psutil.virtual_memory") as mock_vmem:
        # Empty dict from ts.get_after_market_report_data
        mock_get_data.return_value = {}

        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_vmem.return_value = mock_mem

        mock_gen_report.return_value = "Mocked AI Commentary"

        await agent.dispatch_post_market_intelligence()

        # Check that it fetched empty and fallback was applied
        args, kwargs = mock_gen_report.call_args
        raw_data = args[1]
        # Check runway calculation fallback is 600.0
        assert raw_data["aggregate_risk_metrics"]["avg_financial_runway_days"] == 600.0

        # Check queue_dm was called with the build embed containing "600"
        assert bot.queue_dm.await_count == 1
        sent_embed = bot.queue_dm.await_args.kwargs["embed"]
        # The fields should include the survival runway field with "600"
        runway_field = next(f for f in sent_embed.fields if "財務生存跑道" in f.name)
        assert "600.0" in runway_field.value


@pytest.mark.asyncio
async def test_run_fomc_escape_window_analysis_dynamic_period_labels():
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        agent = AnalystAgent(bot)

    # Mock user context with custom October window
    user_ctx = MagicMock()
    user_ctx.escape_window_start = "10-15"  # 15th is "中旬"
    user_ctx.escape_window_end = "10-25"  # 25th is "下旬"

    # Case 1: Hawkish (prob = 0.85 > 0.70)
    with patch("sqlite3.connect") as mock_conn, patch(
        "database.user_settings.get_full_user_context", return_value=user_ctx
    ), patch("cogs.embed_builder.create_fomc_escape_window_embed") as mock_create_embed:
        # Mock DB select to return prob = 0.85
        mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
        mock_cursor.fetchone.return_value = {"fedwatch_probability": 0.85}

        await agent.run_fomc_escape_window_analysis(12345)

        # Verify dynamic labels for adjusted dates
        # start: 15 + 5 = 20 -> "中旬"
        # end: 25 + 5 = 30 -> "下旬"
        mock_create_embed.assert_called_once()
        kwargs = mock_create_embed.call_args.kwargs
        assert kwargs["direction"] == "後推"
        assert "10月中旬" in kwargs["adjusted_start"]
        assert "10月下旬" in kwargs["adjusted_end"]
        assert "10月中旬至10月下旬" in kwargs["reason"]

    # Case 2: Dovish (prob = 0.45 <= 0.70)
    with patch("sqlite3.connect") as mock_conn, patch(
        "database.user_settings.get_full_user_context", return_value=user_ctx
    ), patch("cogs.embed_builder.create_fomc_escape_window_embed") as mock_create_embed:
        # Mock DB select to return prob = 0.45
        mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
        mock_cursor.fetchone.return_value = {"fedwatch_probability": 0.45}

        await agent.run_fomc_escape_window_analysis(12345)

        # Verify dynamic labels for adjusted dates
        # start: 15 - 5 = 10 -> "上旬"
        # end: 25 - 5 = 20 -> "中旬"
        mock_create_embed.assert_called_once()
        kwargs = mock_create_embed.call_args.kwargs
        assert kwargs["direction"] == "前移"
        assert "10月上旬" in kwargs["adjusted_start"]
        assert "10月中旬" in kwargs["adjusted_end"]
        assert "10月中旬至10月下旬" in kwargs["reason"]
