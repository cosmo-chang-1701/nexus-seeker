"""WTI 警報卡片遙測真實性測試：30 分鐘基準、換月、持續突破、文案。"""

from datetime import datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import market_time
from cogs.embed_builders.alert_embeds import create_wti_alert_embed
from cogs.trading.wti_monitor import WtiMonitorCog
from database.wti_config import WtiAlertConfig
from market_analysis import wti_analysis
from market_analysis.wti_analysis import (
    CorrelatedStockImpact,
    WtiAlertType,
    WtiAnalysisResult,
    WtiTechnicals,
    analyze_wti,
    previous_session_close,
)


def _fields(embed: Any) -> dict[str, str]:
    return {str(f.name): str(f.value) for f in embed.fields}


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.wait_until_ready = AsyncMock()
    return bot


def _fake_datetime(hour: int) -> Any:
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return cls(2026, 10, 20, hour, 0, tzinfo=market_time.ny_tz)

    return _FakeDT


# ---------------------------------------------------------------- monitor
@pytest.mark.asyncio
async def test_quiet_hours_refresh_cache_without_notify(mock_bot: Any) -> None:
    cog = WtiMonitorCog(mock_bot)
    with patch("cogs.trading.wti_monitor.datetime", _fake_datetime(3)), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 97.6},
    ) as mock_quote, patch(
        "database.get_kv_cache_with_age", return_value=(97.0, 600.0)
    ), patch("database.save_kv_cache", new_callable=AsyncMock) as mock_save, patch(
        "cogs.trading.wti_monitor.notify", new_callable=AsyncMock
    ) as mock_notify, patch(
        "cogs.trading.wti_monitor.get_wti_config", new_callable=AsyncMock
    ) as mock_cfg:
        await cog.wti_oil_monitor.coro(cog)
        assert mock_quote.called
        mock_save.assert_any_call("macro_wti_prev_30m", 97.6)
        mock_save.assert_any_call("macro_wti", 97.6)
        mock_notify.assert_not_called()
        mock_cfg.assert_not_called()
    await cog.cog_unload()


async def _run_scan(
    cog: WtiMonitorCog, price: float, prev: Optional[float], age: Optional[float]
) -> AsyncMock:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": price},
    ), patch("database.get_all_user_ids", return_value=[1]), patch(
        "database.is_notification_enabled", return_value=True
    ), patch(
        "cogs.trading.wti_monitor.get_wti_config",
        new_callable=AsyncMock,
        return_value=WtiAlertConfig(
            upper_price=95.0, lower_price=None, pct_change_threshold=3.0
        ),
    ), patch("database.get_kv_cache", return_value=None), patch(
        "database.get_kv_cache_with_age", return_value=(prev, age)
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.get_user_watchlist", return_value=[]
    ), patch("cogs.trading.wti_monitor.notify", new_callable=AsyncMock), patch(
        "cogs.trading.wti_monitor.analyze_wti", new_callable=AsyncMock
    ) as mock_analyze:
        mock_analyze.return_value = WtiAnalysisResult(
            alert_type=WtiAlertType.UPPER_BREACH,
            technicals=WtiTechnicals(price=price),
        )
        await cog._evaluate_wti_alerts()
        return mock_analyze


@pytest.mark.asyncio
async def test_stale_baseline_gives_none_pct(mock_bot: Any) -> None:
    cog = WtiMonitorCog(mock_bot)
    m = await _run_scan(cog, 97.6, 95.0, 6.5 * 3600.0)
    kwargs = m.call_args.kwargs
    assert kwargs["pct_change_30min"] is None
    assert kwargs["prev_price_30m"] is None
    await cog.cog_unload()


@pytest.mark.asyncio
async def test_fresh_baseline_gives_pct(mock_bot: Any) -> None:
    cog = WtiMonitorCog(mock_bot)
    m = await _run_scan(cog, 95.81, 95.73, 1800.0)
    kwargs = m.call_args.kwargs
    assert kwargs["pct_change_30min"] == pytest.approx(0.0836, abs=0.001)
    assert kwargs["prev_price_30m"] == 95.73
    await cog.cog_unload()


def test_breach_embed_without_baseline_shows_dashes() -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(price=97.6),
        threshold_value=95.0,
        pct_change_30min=None,
    )
    trig = _fields(create_wti_alert_embed(analysis))["🚨 觸發事件與即時遙測"]
    assert "--" in trig
    assert "無 30 分鐘基準" in trig
    assert "+0.00%" not in trig


# ---------------------------------------------------------- technicals
def _intraday() -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [
            "2026-10-16 16:00",
            "2026-10-16 16:30",
            "2026-10-16 18:00",
            "2026-10-16 18:30",
        ]
    )
    return pd.DataFrame({"Close": [98.5, 99.02, 98.7, 98.9]}, index=idx)


def test_previous_session_close() -> None:
    assert previous_session_close(_intraday()) == pytest.approx(99.02)
    idx = pd.date_range("2026-10-16 10:00", periods=5, freq="30min")
    flat = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)
    assert previous_session_close(flat) is None
    assert previous_session_close(None) is None


def test_previous_session_close_ignores_missing_midsession_bar() -> None:
    idx = pd.DatetimeIndex(
        ["2026-10-16 10:00", "2026-10-16 10:30", "2026-10-16 11:30", "2026-10-16 12:00"]
    )
    gap_only = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    assert previous_session_close(gap_only) is None
    idx2 = pd.DatetimeIndex(
        [
            "2026-10-16 10:00",
            "2026-10-16 10:30",
            "2026-10-16 11:30",
            "2026-10-16 16:30",
            "2026-10-16 18:00",
            "2026-10-16 18:30",
        ]
    )
    both = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 99.02, 98.0, 98.5]}, index=idx2)
    assert previous_session_close(both) == pytest.approx(99.02)


def _daily(values: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(values), freq="D")
    return pd.DataFrame({"Close": values, "High": values, "Low": values}, index=idx)


async def _technicals(daily: pd.DataFrame, intra: Optional[pd.DataFrame]) -> Any:
    async def fake(sym: str, period: str, interval: str) -> Any:
        return daily if interval == "1d" else intra

    with patch("services.market_data_service.get_history_df", new=fake):
        return await wti_analysis._compute_wti_technicals(97.60)


@pytest.mark.asyncio
async def test_contract_roll_daily_and_weekly() -> None:
    closes = [95.0] * 60 + [100.0, 101.0, 101.5, 97.97, 101.41, 97.60]
    t = await _technicals(_daily(closes), _intraday())
    assert t.daily_change_pct == pytest.approx(-1.43, abs=0.01)
    assert t.is_contract_roll is True
    assert t.weekly_change_pct is None


@pytest.mark.asyncio
async def test_no_roll_weekly_uses_iloc_minus_6() -> None:
    closes = [95.0] * 60 + [90.0, 99.0, 99.1, 99.2, 99.02, 97.60]
    t = await _technicals(_daily(closes), _intraday())
    assert t.is_contract_roll is False
    assert t.weekly_change_pct == pytest.approx((97.60 - 90.0) / 90.0 * 100, abs=0.01)


@pytest.mark.asyncio
async def test_daily_falls_back_without_intraday() -> None:
    closes = [95.0] * 60 + [90.0, 99.0, 99.1, 99.2, 100.0, 97.60]
    t = await _technicals(_daily(closes), None)
    assert t.daily_change_pct == pytest.approx(-2.4, abs=0.01)
    assert t.is_contract_roll is False


def test_fetch_failure_technicals_render_dashes() -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(price=97.0),
        threshold_value=95.0,
        pct_change_30min=0.5,
    )
    tech = _fields(create_wti_alert_embed(analysis))["📊 技術結構與量化指標"]
    assert "日 \u001b[1;30m--" in tech
    assert "+0.00%" not in tech


def test_roll_marker_rendered() -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(
            price=97.0, daily_change_pct=-1.4, is_contract_roll=True
        ),
        threshold_value=95.0,
    )
    tech = _fields(create_wti_alert_embed(analysis))["📊 技術結構與量化指標"]
    assert "[換月]" in tech


# ------------------------------------------------------ continuing titles
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alert,prev,threshold,title",
    [
        (WtiAlertType.UPPER_BREACH, 96.0, 95.0, "🛢️ WTI 原油持續高於上限"),
        (WtiAlertType.UPPER_BREACH, 94.0, 95.0, "🚀 WTI 原油突破上限警戒"),
        (WtiAlertType.UPPER_BREACH, None, 95.0, "🚀 WTI 原油突破上限警戒"),
        (WtiAlertType.LOWER_BREACH, 64.0, 65.0, "🛢️ WTI 原油持續低於下限"),
        (WtiAlertType.LOWER_BREACH, 66.0, 65.0, "📉 WTI 原油跌破下限警戒"),
        (WtiAlertType.LOWER_BREACH, None, 65.0, "📉 WTI 原油跌破下限警戒"),
    ],
)
async def test_breach_continuing_titles(
    alert: WtiAlertType, prev: Optional[float], threshold: float, title: str
) -> None:
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await analyze_wti(
            price=97.0, alert_type=alert, threshold_value=threshold, prev_price_30m=prev
        )
    embed = create_wti_alert_embed(result)
    assert embed.title == title
    trig = _fields(embed)["🚨 觸發事件與即時遙測"]
    if "持續" in title:
        assert "持續高於上限" in trig or "持續低於下限" in trig
    else:
        assert "突破上限" in trig or "跌破下限" in trig


@pytest.mark.asyncio
async def test_pct_alert_never_continuing() -> None:
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await analyze_wti(
            price=97.0,
            alert_type=WtiAlertType.PCT_SURGE,
            threshold_value=3.0,
            pct_change_30min=3.5,
            prev_price_30m=96.0,
        )
    assert result.breach_is_continuing is False


# ------------------------------------------------- sector header / text
@pytest.mark.parametrize(
    "open_,expected",
    [(False, "最近收盤"), (True, "現價 ─ 日漲跌")],
)
def test_sector_header_session_aware(open_: bool, expected: str) -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(price=97.0),
        correlated_impacts=[CorrelatedStockImpact("XLE", 88.0, 1.0)],
        equity_session_open=open_,
    )
    sector = _fields(create_wti_alert_embed(analysis))["⛽ 能源板塊關聯股衝擊"]
    assert expected in sector


def test_events_empty_text_is_honest() -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH, technicals=WtiTechnicals(price=97.0)
    )
    risk = _fields(create_wti_alert_embed(analysis))["🛡️ 投資組合風險與總經事件"]
    assert "總經日曆未收錄" in risk
    assert "近期無高影響力" not in risk


@pytest.mark.asyncio
async def test_event_time_converted_to_et() -> None:
    events = [{"time": "2026-10-21T14:30:00Z", "event": "EIA Crude Inventory"}]
    with patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        new_callable=AsyncMock,
        return_value=events,
    ):
        out = await wti_analysis._scan_geopolitical_events()
    assert out and "10/21 10:30 ET" in out[0]


@pytest.mark.parametrize("weight", [1.0, 0.9, 0.7, 0.5])
def test_directive_uses_portfolio_wording(weight: float) -> None:
    analysis = WtiAnalysisResult(
        alert_type=WtiAlertType.UPPER_BREACH,
        technicals=WtiTechnicals(price=97.0),
        oil_risk_weight=weight,
    )
    risk = _fields(create_wti_alert_embed(analysis))["🛡️ 投資組合風險與總經事件"]
    assert "全組合風險限額" in risk
    assert "賣方" not in risk
