import pytest
from unittest.mock import AsyncMock, patch
from services.llm_service import generate_analyst_report


@pytest.mark.asyncio
async def test_generate_analyst_report_prompt_and_constraints():
    # Mock OpenAI client beta.chat.completions.parse
    with patch(
        "services.llm_service.client.beta.chat.completions.parse",
        new_callable=AsyncMock,
    ) as mock_parse:
        # Set up a mock parsed result
        mock_parsed_obj = AsyncMock()
        mock_parsed_obj.choices = [
            AsyncMock(
                message=AsyncMock(
                    parsed=AsyncMock(
                        report_content="1. 📊 多空大盤交叉驗證解讀\n測試內容\n2. ⚠️ 潛在陷阱與風險提示\n測試內容\n3. 🛡️ 高勝率交易策略推薦\n測試內容"
                    )
                )
            )
        ]
        mock_parse.return_value = mock_parsed_obj

        raw_data = {"test": 123}
        await generate_analyst_report("test_report", raw_data)

        # Ensure it was called
        mock_parse.assert_called_once()

        # Verify system prompt content passed to OpenAI
        kwargs = mock_parse.call_args[1]
        messages = kwargs["messages"]
        system_msg = next(msg for msg in messages if msg["role"] == "system")
        system_content = system_msg["content"]

        # Check for language constraint & Taiwanese terminology
        assert "Traditional Chinese" in system_content or "繁體中文" in system_content
        assert "選擇權" in system_content
        assert "履約價" in system_content
        assert "權利金" in system_content
        assert "價差期權/價差策略" in system_content
        assert "隱含波動率" in system_content
        assert "乖離率" in system_content

        # Check for required formatting headers
        assert "📊 多空大盤交叉驗證解讀" in system_content
        assert "⚠️ 潛在陷阱與風險提示" in system_content
        assert "🛡️ 高勝率交易策略推薦" in system_content

        # Check for mathematical cross-validation rules
        assert "IV Bubble Validation" in system_content
        assert "Market Divergence Validation" in system_content
        assert "IV Rank > 90%" in system_content
        assert "days_to_earnings > 20" in system_content
        assert "Option Skew" in system_content
        assert "Put/Call Ratio (PCR) > 1.5" in system_content
