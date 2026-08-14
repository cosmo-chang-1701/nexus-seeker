from typing import Any
import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from database.squeeze_cache import save_squeeze_cache, get_squeeze_cache
from market_analysis.sentiment.uoa_detector import detect_uoa
from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.embed_builders.market_embeds import build_radar_scan_embed


@pytest.mark.asyncio
async def test_get_squeeze_cache_ttl_and_fallback() -> None:
    """驗證 squeeze_cache 的 TTL 與過期回退判定。"""
    sym = "TEST_SQZ_TTL"
    # 儲存初始快取
    save_squeeze_cache(sym, True, 15.5, "🟢")

    # 1. 正常 30 分鐘內讀取
    cache_fresh = get_squeeze_cache(sym, max_age_minutes=30)
    assert cache_fresh is not None
    assert cache_fresh["momentum"] == 15.5
    assert cache_fresh["direction"] == "🟢"
    assert cache_fresh["is_squeezing"] is True
    assert cache_fresh["is_expired"] is False

    # 2. 模擬設定 max_age_minutes=0 (即刻過期)
    cache_expired_with_fallback = get_squeeze_cache(
        sym, max_age_minutes=0, fallback_to_latest=True
    )
    assert cache_expired_with_fallback is not None
    assert cache_expired_with_fallback["momentum"] == 15.5
    assert cache_expired_with_fallback["is_expired"] is True

    # 3. 不允許 fallback_to_latest 時應返回 None
    cache_no_fallback = get_squeeze_cache(
        sym, max_age_minutes=0, fallback_to_latest=False
    )
    assert cache_no_fallback is None


@pytest.mark.asyncio
async def test_detect_uoa_whale_blocks() -> None:
    """驗證 detect_uoa 雙軌機制：既能偵測 Sweep 異動，亦能捕獲大型權值股巨額名義價值的 Whale Block。"""
    expiries = ["2026-08-28"]
    quote = {"c": 200.0}

    # NVDA/AAPL 類型的權值股：未平倉量 10,000，成交量 2,000 (雖然只有 0.2x OI，但名義價值 = 2000 * 5.0 * 100 = 1,000,000 美元)
    calls_df = pd.DataFrame(
        [
            {
                "strike": 210.0,
                "volume": 2000.0,
                "openInterest": 10000.0,  # 0.2x OI (無法達標 5x OI，但名義價值高達 $1M)
                "lastPrice": 5.0,
                "bid": 4.9,
                "ask": 5.1,
                "impliedVolatility": 0.35,
            },
            {
                "strike": 220.0,
                "volume": 500.0,
                "openInterest": 50.0,  # 10x OI (Sweep 異動)
                "lastPrice": 1.5,
                "bid": 1.4,
                "ask": 1.6,
                "impliedVolatility": 0.35,
            },
        ]
    )
    puts_df = pd.DataFrame([])

    class MockChain:
        def __init__(self, calls: Any, puts: Any):
            self.calls = calls
            self.puts = puts

    mock_chain = MockChain(calls_df, puts_df)

    with patch(
        "services.market_data_service.get_all_option_expiries", return_value=expiries
    ), patch(
        "services.market_data_service.get_option_chain", return_value=mock_chain
    ), patch("services.market_data_service.get_quote", return_value=quote):
        uoa_res = await detect_uoa("NVDA")
        assert len(uoa_res) == 2
        # 依成交量降序
        assert uoa_res[0]["strike"] == 210.0
        assert uoa_res[0]["volume"] == 2000.0
        assert uoa_res[1]["strike"] == 220.0
        assert uoa_res[1]["volume"] == 500.0


@pytest.mark.asyncio
async def test_fetch_sym_radar_data_fast_sqz_self_healing() -> None:
    """驗證 _fetch_sym_radar_data_fast_raw 在 Squeeze Cache 未命中或過期時，自動執行自癒計算。"""
    bot = MagicMock()
    cog = UnifiedTerminalCog(bot)
    sym = "TEST_SELF_HEAL"

    # 構造 6 個月的 mock K 線
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=80)
    df_hist = pd.DataFrame(
        {
            "Open": [100.0 + i * 0.5 for i in range(80)],
            "High": [102.0 + i * 0.5 for i in range(80)],
            "Low": [99.0 + i * 0.5 for i in range(80)],
            "Close": [101.0 + i * 0.5 for i in range(80)],
            "Volume": [1000000 for _ in range(80)],
        },
        index=dates,
    )

    with patch(
        "services.market_data_service.get_quote",
        return_value={"c": 140.0, "volume": 1200000},
    ), patch("database.squeeze_cache.get_squeeze_cache", return_value=None), patch(
        "services.market_data_service.get_history_df", return_value=df_hist
    ), patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.market_cache.get_market_cache", return_value={"max_pain": 135.0}
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa", return_value=[]
    ):
        result = await cog._fetch_sym_radar_data_fast_raw(sym)
        assert result is not None
        psq = result.get("psq_result", {})
        assert "momentum" in psq
        assert psq["momentum"] != 0.0 or psq["signal_direction"] in ("🟢", "🔴", "⚪")


@pytest.mark.asyncio
async def test_fetch_sym_radar_data_fast_uoa_self_healing() -> None:
    """驗證 _fetch_sym_radar_data_fast_raw 在 UOA 快取未命中時，自動觸發 detect_uoa 並寫回快取。"""
    bot = MagicMock()
    cog = UnifiedTerminalCog(bot)
    sym = "TEST_UOA_HEAL"

    mock_uoa = [
        {
            "symbol": sym,
            "expiry": "2026-08-28",
            "strike": 150.0,
            "type": "CALL",
            "volume": 2500,
            "oi": 300,
            "action": "🟢 買入開倉 (BTO - Ask)",
            "trade_type": "SWEEP",
        }
    ]

    with patch(
        "services.market_data_service.get_quote",
        return_value={"c": 145.0, "volume": 500000},
    ), patch("database.cache.get_kv_cache", return_value=None), patch(
        "database.squeeze_cache.get_squeeze_cache",
        return_value={"momentum": 5.2, "direction": "🟢", "is_squeezing": False},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        return_value=mock_uoa,
    ), patch("database.cache.save_kv_cache", new_callable=AsyncMock) as mock_save_kv:
        result = await cog._fetch_sym_radar_data_fast_raw(sym)
        assert len(result["uoa"]) == 1
        assert result["uoa"][0]["strike"] == 150.0
        mock_save_kv.assert_called()


def test_build_radar_scan_embed_top_uoa_clean_action() -> None:
    """驗證 build_radar_scan_embed 能正確解析並輸出精簡版 Top UOA action 標籤。"""
    scan_results = [
        {
            "symbol": "AAPL",
            "quote": {"c": 225.0, "dp": 1.2},
            "iv_metrics": {"iv_rank": 35.0, "expected_move_weekly": 5.0},
            "max_pain": {"max_pain": 220.0},
            "gex_metrics": {"put_wall": 215.0, "call_wall": 230.0, "net_gex": 500000.0},
            "gex_profile_data": {
                "put_wall": 215.0,
                "call_wall": 230.0,
                "net_gex": 500000.0,
            },
            "psq_result": {"momentum": 8.5, "direction": "🟢", "is_squeezing": False},
            "uoa": [
                {
                    "symbol": "AAPL",
                    "expiry": "2026-08-28",
                    "strike": 230.0,
                    "type": "CALL",
                    "volume": 15000,
                    "action": "🟢 買入開倉 (BTO - Ask)",
                }
            ],
            "skew": 1.5,
            "skew_percentile": 45.0,
        }
    ]

    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        return_value=(None, None, None),
    ):
        embeds = build_radar_scan_embed(scan_results, "WATCHLIST", 12345)

    assert len(embeds) == 1
    # 驗證 field 中的表格內容
    table_field = next(f for f in embeds[0].fields if "核心 AI" in (f.name or ""))
    assert table_field is not None
    assert table_field.value is not None
    # 應包含精簡版 BTO 格式
    assert "🔥 08/28 $230.0C (BTO 15k)" in table_field.value
    # 應包含 SQZ 向量 🟢+8.5
    assert "🟢+8.5" in table_field.value
