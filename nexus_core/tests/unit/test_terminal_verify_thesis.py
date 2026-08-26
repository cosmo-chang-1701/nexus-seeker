"""Unit tests for cogs.terminal's /verify_thesis flow — verifies form_type
and sections (from the SEC edge scraper) are correctly threaded from
`get_fundamental_context` through to `DynamicRolloverEngine.evaluate_fundamental_thesis`,
and that the news_context (manual, no SEC filing) path stays a no-op for
these new optional fields."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.terminal import TerminalCog
from market_analysis.dynamic_rollover import FundamentalThesisResult


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
