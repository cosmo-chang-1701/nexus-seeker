"""Unit tests for WTI crude oil price alert system."""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from pydantic import ValidationError

from database.notifications import (
    ALL_NOTIFICATION_KEYS,
    PRESET_PROFILES,
    _resolve_key,
)
from database.wti_config import (
    WtiAlertConfig,
    get_wti_config,
    save_wti_config,
)
from market_analysis.wti_analysis import (
    CorrelatedStockImpact,
    OilTrend,
    WtiAlertType,
    WtiAnalysisResult,
    WtiTechnicals,
    compute_oil_risk_weight,
    determine_oil_trend,
    analyze_wti,
)
from cogs.embed_builders.alert_embeds import create_wti_alert_embed
from cogs.trading.wti_monitor import WtiMonitorCog


# ============================================================================
# 1. WtiAlertConfig Model & Validation Tests
# ============================================================================


def test_wti_alert_config_defaults() -> None:
    """Test default values of WtiAlertConfig."""
    cfg = WtiAlertConfig()
    assert cfg.upper_price == 95.0
    assert cfg.lower_price == 65.0
    assert cfg.pct_change_threshold == 3.0


def test_wti_alert_config_custom_values() -> None:
    """Test custom values and rounding."""
    cfg = WtiAlertConfig(
        upper_price=88.456,
        lower_price=62.123,
        pct_change_threshold=2.555,
    )
    assert cfg.upper_price == 88.46
    assert cfg.lower_price == 62.12
    assert cfg.pct_change_threshold == 2.56


def test_wti_alert_config_none_prices() -> None:
    """Test that prices can be None (unbounded)."""
    cfg = WtiAlertConfig(upper_price=None, lower_price=None)
    assert cfg.upper_price is None
    assert cfg.lower_price is None


def test_wti_alert_config_validation_error() -> None:
    """Test validation boundaries."""
    with pytest.raises(ValidationError):
        WtiAlertConfig(pct_change_threshold=0.1)  # ge=0.5

    with pytest.raises(ValidationError):
        WtiAlertConfig(upper_price=300.0)  # le=250.0


@pytest.mark.asyncio
async def test_get_and_save_wti_config() -> None:
    """Test saving and getting WTI config from kv_cache."""
    test_uid = 999888
    cfg = WtiAlertConfig(upper_price=90.0, lower_price=60.0, pct_change_threshold=4.0)

    with patch(
        "database.wti_config.save_kv_cache", new_callable=AsyncMock
    ) as mock_save, patch("database.wti_config.get_kv_cache") as mock_get:
        mock_save.return_value = True
        mock_get.return_value = cfg.model_dump()

        saved = await save_wti_config(test_uid, cfg)
        assert saved is True
        mock_save.assert_called_once_with(f"wti_config_{test_uid}", cfg.model_dump())

        loaded = await get_wti_config(test_uid)
        assert loaded.upper_price == 90.0
        assert loaded.lower_price == 60.0
        assert loaded.pct_change_threshold == 4.0


# ============================================================================
# 2. WTI Analysis Engine Tests
# ============================================================================


def test_compute_oil_risk_weight() -> None:
    """Test oil price risk multiplier scaling."""
    assert compute_oil_risk_weight(70.0) == 1.0
    assert compute_oil_risk_weight(74.99) == 1.0
    assert compute_oil_risk_weight(75.0) == 0.9
    assert compute_oil_risk_weight(84.99) == 0.9
    assert compute_oil_risk_weight(85.0) == 0.7
    assert compute_oil_risk_weight(94.99) == 0.7
    assert compute_oil_risk_weight(95.0) == 0.5
    assert compute_oil_risk_weight(110.0) == 0.5


def test_determine_oil_trend() -> None:
    """Test trend classification under different MA and RSI combinations."""
    # Strong Bullish
    trend_sb = determine_oil_trend(
        price=90.0, rsi=65.0, ma20=85.0, ma50=80.0, ma200=75.0
    )
    assert trend_sb == OilTrend.STRONG_BULLISH

    # Bullish
    trend_b = determine_oil_trend(
        price=82.0, rsi=55.0, ma20=80.0, ma50=78.0, ma200=75.0
    )
    assert trend_b == OilTrend.BULLISH

    # Strong Bearish
    trend_sbr = determine_oil_trend(
        price=60.0, rsi=35.0, ma20=65.0, ma50=70.0, ma200=75.0
    )
    assert trend_sbr == OilTrend.STRONG_BEARISH

    # Bearish
    trend_br = determine_oil_trend(
        price=62.0, rsi=45.0, ma20=65.0, ma50=70.0, ma200=75.0
    )
    assert trend_br == OilTrend.BEARISH

    # Neutral
    trend_n = determine_oil_trend(
        price=75.0, rsi=50.0, ma20=76.0, ma50=74.0, ma200=75.0
    )
    assert trend_n == OilTrend.NEUTRAL


@pytest.mark.asyncio
async def test_analyze_wti_pipeline() -> None:
    """Test full analyze_wti pipeline with mocked market data & calendar."""
    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        new_callable=AsyncMock,
    ) as mock_events:
        mock_hist.return_value = None  # Graceful fallback on empty history
        mock_quote.side_effect = (
            lambda sym: {"c": 85.50, "dp": 1.25}
            if sym == "XLE"
            else {"c": 115.0, "dp": -0.5}
        )
        mock_events.return_value = [
            {"date": "2026-08-20", "event": "OPEC+ Ministerial Meeting"},
            {"date": "2026-08-21", "event": "FOMC Rate Decision"},
        ]

        result = await analyze_wti(
            price=96.50,
            alert_type=WtiAlertType.UPPER_BREACH,
            threshold_value=95.0,
            pct_change_30min=1.85,
            user_watchlist=["XLE"],
            user_holdings=["XOM"],
        )

        assert result.alert_type == WtiAlertType.UPPER_BREACH
        assert result.trigger_price == 96.50
        assert result.threshold_value == 95.0
        assert result.oil_risk_weight == 0.5  # price >= 95.0
        assert len(result.correlated_impacts) > 0
        # Check holdings and watchlist flags
        xle_impact = next(
            (x for x in result.correlated_impacts if x.symbol == "XLE"), None
        )
        assert xle_impact is not None
        assert xle_impact.is_in_watchlist is True

        # Check geopolitical events
        assert len(result.geopolitical_events) == 1
        assert "OPEC+" in result.geopolitical_events[0]


# ============================================================================
# 3. WTI Embed Builder Tests
# ============================================================================


def test_create_wti_alert_embed_upper_breach() -> None:
    """Test create_wti_alert_embed for UPPER_BREACH."""
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(
            price=96.50,
            rsi_14=68.5,
            ma_20=91.20,
            ma_50=86.40,
            ma_200=78.00,
            atr_14=2.15,
            daily_change_pct=3.20,
            weekly_change_pct=5.80,
            trend=OilTrend.STRONG_BULLISH,
        ),
        correlated_impacts=[
            CorrelatedStockImpact(
                symbol="XLE", price=88.50, daily_change_pct=2.45, is_in_watchlist=True
            ),
            CorrelatedStockImpact(
                symbol="XOM", price=118.20, daily_change_pct=1.80, is_in_holdings=True
            ),
        ],
        geopolitical_events=["📅 2026-08-20: OPEC+ Output Policy Review"],
        oil_risk_weight=0.5,
        trigger_price=96.50,
        threshold_value=95.0,
        pct_change_30min=1.5,
    )

    embed = create_wti_alert_embed(analysis)
    assert embed.title == "🚀 WTI 原油突破上限警戒"

    fields: Dict[str, str] = {
        str(f.name): str(f.value)
        for f in embed.fields
        if f.name is not None and f.value is not None
    }
    # 1. Verify all sections are field-based with ```ansi
    for fname, fval in fields.items():
        assert fval.startswith(
            "```ansi\n"
        ), f"Field {fname} value must start with ```ansi"
        assert fval.endswith("\n```"), f"Field {fname} value must end with ```"

    # 2. Check field contents
    assert "🚨 觸發事件與即時遙測" in fields
    assert "$96.50" in fields["🚨 觸發事件與即時遙測"]
    assert "$95.00" in fields["🚨 觸發事件與即時遙測"]
    assert "突破上限" in fields["🚨 觸發事件與即時遙測"]

    assert "📊 技術結構與量化指標" in fields
    assert "RSI(14)" in fields["📊 技術結構與量化指標"]
    assert "68.5" in fields["📊 技術結構與量化指標"]

    assert "⛽ 能源板塊關聯股衝擊" in fields
    assert "XLE" in fields["⛽ 能源板塊關聯股衝擊"]
    assert "XOM" in fields["⛽ 能源板塊關聯股衝擊"]
    assert "[WATCH]" in fields["⛽ 能源板塊關聯股衝擊"]
    assert "[HOLDING]" in fields["⛽ 能源板塊關聯股衝擊"]

    assert "🛡️ 投資組合風險與總經事件" in fields
    assert "0.50x" in fields["🛡️ 投資組合風險與總經事件"]
    assert "OPEC+" in fields["🛡️ 投資組合風險與總經事件"]


def test_create_wti_alert_embed_pct_surge() -> None:
    """Test create_wti_alert_embed for PCT_SURGE."""
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.PCT_SURGE,
        technicals=WtiTechnicals(price=82.00, trend=OilTrend.BULLISH),
        oil_risk_weight=0.9,
        trigger_price=82.00,
        threshold_value=3.0,
        pct_change_30min=3.85,
    )

    embed = create_wti_alert_embed(analysis)
    assert embed.title == "⚡ WTI 原油劇烈飆漲"

    fields: Dict[str, str] = {
        str(f.name): str(f.value)
        for f in embed.fields
        if f.name is not None and f.value is not None
    }
    assert "🚨 觸發事件與即時遙測" in fields
    assert "+3.85%" in fields["🚨 觸發事件與即時遙測"]
    assert "±3.0%" in fields["🚨 觸發事件與即時遙測"]
    assert "劇烈飆漲" in fields["🚨 觸發事件與即時遙測"]


# ============================================================================
# 4. WtiMonitorCog Loop & Dispatching Tests
# ============================================================================


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_wti_monitor_alert_dispatch(mock_bot: Any) -> None:
    """Test that WtiMonitorCog evaluates conditions and sends alerts via queue_dm."""
    cog = WtiMonitorCog(mock_bot)

    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch("database.get_all_user_ids", return_value=[1001]), patch(
        "database.is_notification_enabled", return_value=True
    ), patch(
        "database.wti_config.get_wti_config", new_callable=AsyncMock
    ) as mock_cfg, patch("database.get_kv_cache", return_value=None), patch(
        "database.save_kv_cache", new_callable=AsyncMock
    ) as mock_save, patch(
        "market_analysis.wti_analysis.analyze_wti", new_callable=AsyncMock
    ) as mock_analyze:
        # CL=F price triggers upper breach
        mock_quote.return_value = {"c": 98.0, "dp": 3.5}
        mock_cfg.return_value = WtiAlertConfig(
            upper_price=95.0, lower_price=65.0, pct_change_threshold=3.0
        )
        mock_analyze.return_value = WtiAnalysisResult(
            alert_type=WtiAlertType.UPPER_BREACH,
            technicals=WtiTechnicals(price=98.0),
            oil_risk_weight=0.5,
            trigger_price=98.0,
            threshold_value=95.0,
        )

        await cog._evaluate_wti_alerts()

        # Bot queue_dm should be called with embed
        mock_bot.queue_dm.assert_called_once()
        call_uid = mock_bot.queue_dm.call_args[0][0]
        assert call_uid == 1001
        embed_sent = mock_bot.queue_dm.call_args[1]["embed"]
        assert "WTI 原油突破上限警戒" in embed_sent.title
        assert mock_save.called

    await cog.cog_unload()


# ============================================================================
# 5. Database Notification Settings & Alias Resolution Tests
# ============================================================================


def test_notification_keys_and_presets() -> None:
    """Test alpha_wti_oil key presence and preset behavior."""
    assert "alpha_wti_oil" in ALL_NOTIFICATION_KEYS
    assert PRESET_PROFILES["all_on"]["alpha_wti_oil"] is True
    assert PRESET_PROFILES["all_off"]["alpha_wti_oil"] is False
    # 全天候情報，不受盤中/Alpha 雜訊降噪邏輯影響，focus 與 mute_intraday 皆維持開啟
    assert PRESET_PROFILES["focus"]["alpha_wti_oil"] is True
    assert PRESET_PROFILES["mute_intraday"]["alpha_wti_oil"] is True


def test_legacy_alias_resolution() -> None:
    """Test alias resolution for WTI oil alerts."""
    assert _resolve_key("wti_oil_alert") == "alpha_wti_oil"
    assert _resolve_key("oil_alert") == "alpha_wti_oil"
