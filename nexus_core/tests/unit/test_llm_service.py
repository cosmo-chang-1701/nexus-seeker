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


@pytest.mark.asyncio
async def test_generate_watchlist_roundup_commentary():
    from services.llm_service import generate_watchlist_roundup_commentary

    # Test case 1: Empty symbols
    res_1 = await generate_watchlist_roundup_commentary({"symbols": []})
    assert "無可用" in res_1

    # Test case 2: Red and Yellow items, and Events
    raw_data_2 = {
        "symbols": [
            {
                "symbol": "AAPL",
                "alert_level": "red",
                "option_skew": 6.5,
                "option_skew_state": "左偏",
                "iv_rank": 70.0,
                "scenario": "hard-hedge",
                "event_risk_summary": "財報倒數 2 天",
            },
            {
                "symbol": "NVDA",
                "alert_level": "yellow",
                "option_skew": 1.0,
                "option_skew_state": "平穩",
                "iv_rank": 68.0,
                "scenario": "premium-harvest",
                "event_risk_summary": "無重大事件",
            },
        ]
    }
    res_2 = await generate_watchlist_roundup_commentary(raw_data_2)
    assert "🔴 **高危警報" in res_2
    assert "AAPL" in res_2
    assert "🟡 **收租機會" in res_2
    assert "NVDA" in res_2
    assert "🗓️ **重大事件追蹤**" in res_2
    assert "AAPL(財報倒數 2 天)" in res_2

    # Test case 3: All green
    raw_data_3 = {
        "symbols": [
            {
                "symbol": "MSFT",
                "alert_level": "green",
                "option_skew": 0.5,
                "option_skew_state": "平穩",
                "iv_rank": 40.0,
                "scenario": "wait",
                "event_risk_summary": "無重大事件",
            }
        ]
    }
    res_3 = await generate_watchlist_roundup_commentary(raw_data_3)
    assert "🟢 **全局安全**" in res_3
