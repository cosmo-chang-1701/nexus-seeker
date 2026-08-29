from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.terminal import TerminalCog
from market_analysis.dynamic_rollover import FundamentalThesisResult


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_sys_health_uses_builder(mock_interaction: Any):  # type: ignore
    bot = MagicMock()
    bot.polymarket_service = SimpleNamespace(
        _market_cache={1: 1}, _order_books={1: 1, 2: 2}
    )
    cog = TerminalCog(bot)
    embed = object()

    with (
        patch("psutil.virtual_memory") as mock_mem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.cpu_percent", return_value=10.0),
        patch("psutil.Process") as mock_process,
        patch("services.market_data_service._sma_cache", {1: 1, 2: 2}),
        patch("services.market_data_service._ema_cache", {1: 1}),
        patch(
            "cogs.terminal.system.create_system_health_embed", return_value=embed
        ) as mock_builder,
    ):
        mock_mem.return_value.percent = 50.0
        mock_mem.return_value.available = 512 * 1024 * 1024
        mock_disk.return_value.percent = 40.0
        mock_disk.return_value.free = 10 * 1024 * 1024 * 1024
        mock_process.return_value.memory_info.return_value.rss = 256 * 1024 * 1024

        await cog.sys_health.callback(cog, mock_interaction)  # type: ignore

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["sma_cache_size"] == 2
    assert kwargs["poly_cache_size"] == 1
    assert kwargs["orderbook_size"] == 2
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)


@pytest.mark.asyncio
async def test_promote_watch_uses_builder(mock_interaction: Any):  # type: ignore
    bot = MagicMock()
    cog = TerminalCog(bot)
    embed = object()

    with (
        patch(
            "services.market_data_service.validate_symbol",
            new=AsyncMock(return_value=True),
        ),
        patch("services.asset_manager.AssetManager") as mock_manager_cls,
        patch("market_analysis.portfolio.refresh_portfolio_greeks", new=AsyncMock()),
        patch(
            "cogs.terminal.watchlist.create_asset_promotion_embed", return_value=embed
        ) as mock_builder,
    ):
        mock_manager_cls.return_value.promote_to_trade.return_value = True
        await cog.promote_watch.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol="aapl",
            opt_type="call",
            strike=150.0,
            expiry="2026-06-19",
            price=5.5,
            qty=2,
        )

    mock_builder.assert_called_once_with(
        symbol="AAPL",
        expiry="2026-06-19",
        strike=150.0,
        opt_type="call",
        quantity=2,
        price=5.5,
    )
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)


@pytest.mark.asyncio
async def test_transition_sim_uses_builder(mock_interaction: Any):  # type: ignore
    bot = MagicMock()
    cog = TerminalCog(bot)
    embed = object()
    result = SimpleNamespace(
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        projected_aroc=18.0,
        capital_efficiency_gain=2.7,
    )

    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0}),
        ),
        patch(
            "market_analysis.pro_management.simulate_pro_transition",
            return_value=result,
        ),
        patch(
            "cogs.terminal.analysis.create_transition_simulation_embed",
            return_value=embed,
        ) as mock_builder,
    ):
        await cog.transition_sim.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol="nvda",
            current_option_pnl=2500.0,
            target_cc_strike=110.0,
            target_cc_premium=2.5,
        )

    mock_builder.assert_called_once_with(
        symbol="NVDA",
        current_price=100.0,
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        target_cc_strike=110.0,
        target_cc_premium=2.5,
        projected_aroc=18.0,
        capital_efficiency_gain=2.7,
    )
    mock_interaction.followup.send.assert_called_once_with(embed=embed)


@pytest.mark.asyncio
async def test_execute_verify_thesis_logic_passes_form_type_and_sections_to_engine(
    mock_interaction: Any,
) -> None:
    bot = MagicMock()
    cog = TerminalCog(bot)

    result = FundamentalThesisResult(is_broken=False, confidence=0.5, reasoning="ok")

    with patch(
        "market_analysis.dynamic_rollover.DynamicRolloverEngine.evaluate_fundamental_thesis",
        new_callable=AsyncMock,
        return_value=result,
    ) as mock_eval:
        await cog._execute_verify_thesis_logic(
            mock_interaction,
            "AMD",
            "combined text",
            "https://sec.gov/doc",
            form_type="10-Q",
            sections={"quarterly_financials": "Q3 rev $1B"},
        )

    mock_eval.assert_awaited_once_with(
        "AMD",
        "combined text",
        form_type="10-Q",
        sections={"quarterly_financials": "Q3 rev $1B"},
    )


@pytest.mark.asyncio
async def test_verify_thesis_news_context_path_passes_news_form_type(
    mock_interaction: Any,
) -> None:
    bot = MagicMock()
    cog = TerminalCog(bot)
    cog._execute_verify_thesis_logic = AsyncMock()  # type: ignore[method-assign]

    await cog.verify_thesis.callback(
        cog,  # type: ignore
        mock_interaction,
        "AMD",
        news_context="最新法說會摘要",
    )

    cog._execute_verify_thesis_logic.assert_awaited_once()
    call = cog._execute_verify_thesis_logic.call_args
    assert call.kwargs.get("form_type", "") == "NEWS"
    assert call.kwargs.get("sections") is None


@pytest.mark.asyncio
async def test_evaluate_fundamental_thesis_impl_news_prompt_construction() -> None:
    from market_analysis.dynamic_rollover.fundamental_thesis import (
        evaluate_fundamental_thesis_impl,
    )

    mock_client = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock()
    mock_parse_result = MagicMock()
    mock_parse_result.choices = [
        MagicMock(
            message=MagicMock(
                parsed=FundamentalThesisResult(
                    is_broken=False, confidence=0.7, reasoning="測試新聞分析"
                )
            )
        )
    ]
    mock_client.beta.chat.completions.parse.return_value = mock_parse_result

    with patch("database.market_cache.save_fundamental_cache") as mock_save_cache:
        result = await evaluate_fundamental_thesis_impl(
            client=mock_client,
            is_memory_safe=lambda: True,
            llm_model_name="test-model",
            symbol="NVDA",
            fundamental_text="[使用者補充新聞/資訊]:\n測試突發消息",
            form_type="NEWS",
        )

    assert result is not None
    assert result.is_broken is False
    assert result.confidence == 0.7
    mock_save_cache.assert_called_once_with("NVDA", False, 0.7, "測試新聞分析")

    call_args = mock_client.beta.chat.completions.parse.call_args
    messages = call_args.kwargs["messages"]
    system_msg = messages[0]["content"]
    user_msg = messages[1]["content"]

    assert (
        "### 📰 Context: Real-Time News / Breaking Event / Conference Call Notes"
        in system_msg
    )
    assert (
        "Please analyze the following breaking news / event context for NVDA"
        in user_msg
    )


@pytest.mark.asyncio
async def test_verify_thesis_single_report_path_threads_fundamental_data_fields(
    mock_interaction: Any,
) -> None:
    bot = MagicMock()
    cog = TerminalCog(bot)
    cog._execute_verify_thesis_logic = AsyncMock()  # type: ignore[method-assign]

    fundamental_data = {
        "text": "SEC 財報段落",
        "source_url": "https://sec.gov/doc",
        "form_type": "10-K",
        "sections": {"forward_guidance": "guidance cut"},
    }

    with (
        patch(
            "services.fundamental_service.get_fundamental_reports_list",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "services.fundamental_service.get_fundamental_context",
            new_callable=AsyncMock,
            return_value=fundamental_data,
        ),
    ):
        await cog.verify_thesis.callback(
            cog,  # type: ignore
            mock_interaction,
            "AMD",
            news_context=None,
        )

    cog._execute_verify_thesis_logic.assert_awaited_once()
    call = cog._execute_verify_thesis_logic.call_args
    assert call.kwargs["form_type"] == "10-K"
    assert call.kwargs["sections"] == {"forward_guidance": "guidance cut"}
