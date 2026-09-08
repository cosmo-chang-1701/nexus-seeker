"""
tests/unit/test_portfolio_monitor.py

單元測試：cogs/trading/portfolio_monitor.py 的期權部位併入邏輯
(PortfolioMonitorCog._build_option_asset_entry / _build_symbol_metrics)。
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading.portfolio_monitor import PortfolioMonitorCog


_SAMPLE_METRICS = {
    "spot_price": 100.0,
    "price_15m_close": 99.5,
    "ivr": 40.0,
    "ivr_drop": 5.0,
    "max_pain": 98.0,
    "put_wall": 95.0,
    "call_wall": 110.0,
    "is_uoa_sweep": True,
    "gamma_flip": 97.0,
    "sqz_mom": 3.2,
    "skew": -0.1,
    "atr_14": 2.0,
    "atr_15m": 2.5,
    "hvn": 96.0,
    "lvn": 93.0,
    "dte": 30,
}


def test_build_option_asset_entry_success_path() -> None:
    """成功路徑：正確組裝 current_value (quantity*mid*100)、instrument_type
    恆為 OPTIONS_CONTRACT、asset_class 恆為 SATELLITE、bid/ask 帶入、量化欄位
    重用同一標的的 metrics 快照。"""
    r_data = {
        "gex_profile_data": {"put_wall": 95.0, "call_wall": 110.0},
        "psq_result": {"squeeze_level": "High", "signal_direction": "Long"},
    }
    entry = PortfolioMonitorCog._build_option_asset_entry(
        "AAPL", 2.0, 5.0, 4.9, 5.1, _SAMPLE_METRICS, r_data
    )

    assert entry["symbol"] == "AAPL"
    assert entry["asset_class"] == "SATELLITE"
    assert entry["instrument_type"] == "OPTIONS_CONTRACT"
    assert entry["quantity"] == 2.0
    assert entry["current_value"] == pytest.approx(2.0 * 5.0 * 100.0)
    assert entry["max_allocation_pct"] == 0.3
    assert entry["spot_price"] == 100.0
    assert entry["put_wall"] == 95.0
    assert entry["call_wall"] == 110.0
    assert entry["ivr"] == 40.0
    assert entry["bid"] == 4.9
    assert entry["ask"] == 5.1
    # atr_15m 必須直接沿用 metrics 提供的真實 15m ATR，不得別名/退回 atr_14
    # (Task 1.1: 消除 atr_15m == atr_14 的資料鏈假造問題)。
    assert entry["atr_14"] == 2.0
    assert entry["atr_15m"] == 2.5
    assert entry["atr_15m"] != entry["atr_14"]
    assert entry["gex_profile_data"] == {"put_wall": 95.0, "call_wall": 110.0}
    assert entry["psq_result"] == {
        "squeeze_level": "High",
        "signal_direction": "Long",
    }


def test_build_option_asset_entry_degrades_avg_cost_and_acquired_at() -> None:
    """期權部位無法從既有資料推導單筆成本基礎，avg_cost/acquired_at 必須
    明確降級為 0.0/None (而非沿用現貨的 h.get('avg_cost')/h.get('acquired_at')
    語意，那對期權合約不成立)。"""
    entry = PortfolioMonitorCog._build_option_asset_entry(
        "NVDA", 1.0, 10.0, 9.5, 10.5, _SAMPLE_METRICS, None
    )
    assert entry["avg_cost"] == 0.0
    assert entry["acquired_at"] is None
    assert entry["boxx_allocation_pct"] is None
    # 無雷達資料時，gex_profile_data/psq_result 優雅降級為空 dict
    assert entry["gex_profile_data"] == {}
    assert entry["psq_result"] == {}


def test_build_option_asset_entry_atr_15m_defaults_to_zero_not_atr_14() -> None:
    """metrics 缺失 atr_15m 時 (例如上游抓取失敗)，必須優雅降級為 0.0，
    嚴禁再退回 metrics['atr_14'] 別名充當 15m ATR (Task 1.1 消除的假造行為)。
    """
    metrics_without_atr_15m: dict[str, Any] = {
        k: v for k, v in _SAMPLE_METRICS.items() if k != "atr_15m"
    }
    entry = PortfolioMonitorCog._build_option_asset_entry(
        "AAPL", 2.0, 5.0, 4.9, 5.1, metrics_without_atr_15m, None
    )
    assert entry["atr_14"] == 2.0
    assert entry["atr_15m"] == 0.0


@pytest.mark.asyncio
async def test_build_symbol_metrics_returns_fallback_when_no_radar_data() -> None:
    """r_data 缺失 (None) 時，_build_symbol_metrics 應回傳全零的降級快照，
    而非拋出例外。"""
    bot = MagicMock()
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = bot

    metrics = await cog._build_symbol_metrics("AAPL", None)
    assert metrics["spot_price"] == 0.0
    assert metrics["dte"] == 99
    assert metrics["is_uoa_sweep"] is False


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache")
@patch("database.cache.get_kv_cache", return_value=None)
async def test_build_symbol_metrics_parses_radar_data(
    mock_get_kv: Any, mock_save_kv: Any
) -> None:
    """r_data 存在時，_build_symbol_metrics 應正確解析 spot/IVR/GEX/PSQ 等
    欄位 (現貨與期權部位共用同一份計算結果的關鍵前提)。"""
    bot = MagicMock()
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = bot

    r_data = {
        "quote": {"c": 150.0},
        "iv_metrics": {"iv_rank": 55.0},
        "atr_14": 3.0,
        "atr_15m": 1.2,
        "gex_profile_data": {
            "put_wall": 145.0,
            "call_wall": 160.0,
            "gamma_flip": 148.0,
        },
        "max_pain": {"max_pain": 149.0},
        "uoa": [{"type": "CALL"}],
        "psq_result": {"momentum_value": 7.5},
        "skew": -0.2,
        "vp_data": {"hvn": 148.5, "lvn": 143.0},
        "nearest_dte": 14,
    }
    metrics = await cog._build_symbol_metrics("AAPL", r_data)
    assert metrics["spot_price"] == 150.0
    assert metrics["ivr"] == 55.0
    assert metrics["put_wall"] == 145.0
    assert metrics["call_wall"] == 160.0
    assert metrics["gamma_flip"] == 148.0
    assert metrics["max_pain"] == 149.0
    assert metrics["is_uoa_sweep"] is True
    assert metrics["sqz_mom"] == 7.5
    assert metrics["skew"] == -0.2
    assert metrics["hvn"] == 148.5
    assert metrics["lvn"] == 143.0
    assert metrics["dte"] == 14
    # atr_15m 必須是真實的 15m ATR (獨立於 atr_14)，不得別名 (Task 1.1)。
    assert metrics["atr_14"] == 3.0
    assert metrics["atr_15m"] == 1.2
    assert metrics["atr_15m"] != metrics["atr_14"]


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache")
@patch("database.cache.get_kv_cache", return_value=None)
async def test_build_symbol_metrics_atr_15m_defaults_to_zero_not_atr_14(
    mock_get_kv: Any, mock_save_kv: Any
) -> None:
    """r_data 缺失 atr_15m 欄位時 (radar_data.py 尚未回填/抓取失敗)，
    _build_symbol_metrics 必須降級為 0.0，嚴禁退回 r_data['atr_14'] 別名。"""
    bot = MagicMock()
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = bot

    r_data = {
        "quote": {"c": 150.0},
        "atr_14": 3.0,
        # 刻意不提供 atr_15m
    }
    metrics = await cog._build_symbol_metrics("AAPL", r_data)
    assert metrics["atr_14"] == 3.0
    assert metrics["atr_15m"] == 0.0


# ----------------------------------------------------------------------
# price_15m_close：必須是真正的 15m 已收盤 K 棒收盤價，而非現價
# ----------------------------------------------------------------------


def _make_confirmed_bar(close: float, volume: float = 1000.0) -> Any:
    from datetime import datetime

    from market_analysis.price_volume_alert import Confirmed15mBar

    return Confirmed15mBar(
        symbol="AAPL",
        bar_time=datetime(2024, 1, 2, 10, 0),
        close=close,
        volume=volume,
        avg_volume=1000.0,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
    )


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache")
@patch("database.cache.get_kv_cache", return_value=None)
async def test_price_15m_close_uses_confirmed_bar_not_spot(
    mock_get_kv: Any, mock_save_kv: Any
) -> None:
    """price_15m_close 必須取自已收盤的 15m 實體 K 棒，而非即時現價。

    此欄位是微觀結構出場決策矩陣 SPOT 軌道 (15m 實體收盤過濾) 與 Transition
    Engine 路徑 2/3 的判定依據；若誤填現價，盤中任何一次插針都會被當成實體
    破位，防洗盤設計形同虛設。OPTIONS 快速通道另行使用 spot_price，不受影響。
    """
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = MagicMock()

    r_data = {"quote": {"c": 150.0}, "atr_15m": 1.2}

    with (
        patch(
            "market_analysis.price_volume_alert.get_confirmed_15m_bar",
            new_callable=AsyncMock,
            return_value=_make_confirmed_bar(close=147.5),
        ),
        patch(
            "market_analysis.vwap_utils.fetch_session_vwap",
            new_callable=AsyncMock,
            return_value=149.0,
        ),
    ):
        metrics = await cog._build_symbol_metrics("AAPL", r_data)

    assert metrics["spot_price"] == 150.0
    assert metrics["price_15m_close"] == 147.5
    assert metrics["price_15m_close"] != metrics["spot_price"]


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache")
@patch("database.cache.get_kv_cache", return_value=None)
async def test_price_15m_close_falls_back_to_spot_when_no_confirmed_bar(
    mock_get_kv: Any, mock_save_kv: Any
) -> None:
    """尚無可用的已收盤 15m K 棒 (開盤初期/抓取失敗) 時，退回現價以確保判定
    不會失去輸入 (等同此欄位過去的行為)。"""
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = MagicMock()

    r_data = {"quote": {"c": 150.0}}

    with (
        patch(
            "market_analysis.price_volume_alert.get_confirmed_15m_bar",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.vwap_utils.fetch_session_vwap",
            new_callable=AsyncMock,
            return_value=149.0,
        ),
    ):
        metrics = await cog._build_symbol_metrics("AAPL", r_data)

    assert metrics["price_15m_close"] == 150.0


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache")
@patch("database.cache.get_kv_cache", return_value=None)
async def test_price_15m_close_survives_session_vwap_failure(
    mock_get_kv: Any, mock_save_kv: Any
) -> None:
    """Session VWAP 抓取失敗不得連帶讓 price_15m_close 降級退回現價——兩者是
    彼此獨立的資料源，各自擁有獨立的 try 區塊。"""
    cog = PortfolioMonitorCog.__new__(PortfolioMonitorCog)
    cog.bot = MagicMock()

    r_data = {"quote": {"c": 150.0}}

    with (
        patch(
            "market_analysis.price_volume_alert.get_confirmed_15m_bar",
            new_callable=AsyncMock,
            return_value=_make_confirmed_bar(close=147.5),
        ),
        patch(
            "market_analysis.vwap_utils.fetch_session_vwap",
            new_callable=AsyncMock,
            side_effect=RuntimeError("VWAP 抓取失敗"),
        ),
    ):
        metrics = await cog._build_symbol_metrics("AAPL", r_data)

    assert metrics["price_15m_close"] == 147.5
    # VWAP 相關欄位本身仍 fail-safe 降級
    assert metrics["session_vwap"] == 0.0
    assert metrics["vwap_loss_with_volume"] is False
    assert metrics["vwap_reclaim_with_volume"] is False
