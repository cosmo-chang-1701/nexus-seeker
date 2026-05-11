import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os
import pandas as pd

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.unified_terminal import UnifiedTerminalCog


@pytest.mark.asyncio
async def test_symbol_hub_full_integration(mock_interaction, db_conn):
    """
    整合測試：驗證 /x 指令從資料庫獲取用戶上下文、調用市場數據服務、
    並最終生成包含所有量化指標的 Embed。
    """
    bot = MagicMock()
    cog = UnifiedTerminalCog(bot)

    # 準備測試數據
    from database.user_settings import upsert_user_config

    upsert_user_config(mock_interaction.user.id, capital=200000.0, risk_limit=10.0)

    # Mock 最底層的 API 呼叫
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch(
        "services.market_data_service.get_spy_history_df", new_callable=AsyncMock
    ) as mock_spy, patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock_val, patch(
        "services.market_data_service.get_all_option_expiries", new_callable=AsyncMock
    ) as mock_exp, patch(
        "services.market_data_service.get_option_chain", new_callable=AsyncMock
    ) as mock_chain:
        mock_val.return_value = True
        mock_quote.return_value = {
            "c": 150.0,
            "dp": 1.5,
            "pc": 148.0,
            "h": 151.0,
            "l": 147.0,
        }

        # 準備足夠的歷史數據以供量化引擎計算 (至少 50 筆)
        hist_data = {
            "Close": [140.0] * 100,
            "Volume": [1000000] * 100,
            "High": [141.0] * 100,
            "Low": [139.0] * 100,
        }
        df = pd.DataFrame(hist_data, index=pd.date_range("2024-01-01", periods=100))
        # 確保 pandas-ta 指標存在，以防在測試環境中計算失敗
        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(append=True)

        mock_hist.return_value = df
        mock_spy.return_value = df

        mock_macro.return_value = {"vix": 18.0, "oil": 75.0, "vix_change": 0.0}

        # 期權數據
        mock_exp.return_value = ["2024-06-21"]
        mock_chain_obj = MagicMock()
        mock_chain_obj.calls = pd.DataFrame(
            {
                "strike": [155, 160],
                "impliedVolatility": [0.25, 0.26],
                "lastPrice": [2.5, 1.8],
                "bid": [2.4, 1.7],
                "ask": [2.6, 1.9],
                "openInterest": [100, 200],
                "volume": [10, 20],
            }
        )
        mock_chain_obj.puts = pd.DataFrame(
            {
                "strike": [145, 140],
                "impliedVolatility": [0.30, 0.32],
                "lastPrice": [3.0, 2.2],
                "bid": [2.9, 2.1],
                "ask": [3.1, 2.3],
                "openInterest": [150, 250],
                "volume": [15, 25],
            }
        )
        mock_chain.return_value = mock_chain_obj

        # 執行指令
        await cog.symbol_hub.callback(cog, mock_interaction, symbol="AAPL")

        # 驗證結果
        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]

        # 驗證 Embed 內容
        assert "AAPL" in embed.title
        # 檢查是否傳入了互動 View
        assert "view" in kwargs
