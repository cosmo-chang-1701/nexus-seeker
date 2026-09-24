"""services/downside_risk_service.py：曝險讀取、模擬報酬、盤中報酬、推播狀態與 NAV 快照。"""

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import database
from market_analysis.downside_monitor import DownsideSnapshot
from services import downside_risk_service as svc


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    svc.clear_series_cache()
    yield
    svc.clear_series_cache()


def _insert_asset(db_conn: Any, uid: int, sym: str, ctx: str, meta: dict) -> None:
    db_conn.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, ?, ?)",
        (uid, sym, ctx, json.dumps(meta)),
    )
    db_conn.commit()


def _closes(values: list[float], start: str = "2025-01-02") -> pd.Series:
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


# ---------------------------------------------------------------------------
# 曝險讀取
# ---------------------------------------------------------------------------


def test_load_exposure_stock_option_and_fallback_delta(db_conn: Any) -> None:
    database.upsert_user_config(4001, cash_reserve=5_000.0)
    _insert_asset(db_conn, 4001, "AAPL", "HOLDING", {"quantity": 10, "avg_cost": 150})
    # 有原始 Delta 的賣出 Call：-1 口 × 0.3 × 100 = -30 股
    _insert_asset(
        db_conn,
        4001,
        "AAPL",
        "TRADE",
        {
            "opt_type": "call",
            "strike": 200,
            "expiry": "2026-12-18",
            "entry_price": 2.0,
            "quantity": -1,
            "delta": 0.3,
        },
    )
    # 舊資料無 Delta 的買入 Put：以 -0.5 近似 → +2 口 × -0.5 × 100 = -100 股
    _insert_asset(
        db_conn,
        4001,
        "SPY",
        "TRADE",
        {
            "opt_type": "put",
            "strike": 500,
            "expiry": "2026-12-18",
            "entry_price": 5.0,
            "quantity": 2,
        },
    )
    exp = svc.load_portfolio_exposure(4001)
    assert exp.stock_shares == {"AAPL": 10.0}
    assert exp.option_delta_shares["AAPL"] == pytest.approx(-30.0)
    assert exp.option_delta_shares["SPY"] == pytest.approx(-100.0)
    assert exp.option_premium == pytest.approx(1 * 100 * 2.0 + 2 * 100 * 5.0)
    assert exp.cash == pytest.approx(5_000.0)
    assert exp.total_shares("AAPL") == pytest.approx(-20.0)


# ---------------------------------------------------------------------------
# 模擬報酬序列
# ---------------------------------------------------------------------------


def test_compute_return_series_fixed_weights() -> None:
    exp = svc.PortfolioExposure({"A": 10.0, "B": 10.0}, {}, 0.0, 1_000.0)
    closes = {
        "A": _closes([100.0, 110.0, 110.0]),
        "B": _closes([50.0, 50.0, 25.0]),
    }
    series = svc.compute_return_series(1, exp, closes)
    assert series is not None
    # NAV = 10×110 + 10×25 + 1000 = 2350；權重以最後收盤價計
    assert series.nav == pytest.approx(2350.0)
    w_a, w_b = 1100 / 2350, 250 / 2350
    assert series.weights == pytest.approx({"A": w_a, "B": w_b})
    assert list(series.returns) == pytest.approx([w_a * 0.10, w_b * -0.5])


def test_compute_return_series_drops_today_partial_bar() -> None:
    exp = svc.PortfolioExposure({"A": 1.0}, {}, 0.0, 0.0)
    closes = {"A": _closes([100.0, 101.0, 150.0], start="2026-03-02")}
    series = svc.compute_return_series(
        1, exp, closes, drop_on_or_after=date(2026, 3, 4)
    )
    assert series is not None
    assert series.last_closes["A"] == pytest.approx(101.0)
    assert series.dates == ["2026-03-03"]


def test_short_exposure_has_negative_weight() -> None:
    exp = svc.PortfolioExposure({"A": -10.0}, {}, 0.0, 1_000.0)
    series = svc.compute_return_series(1, exp, {"A": _closes([100.0, 90.0])})
    assert series is not None
    assert series.weights["A"] < 0
    assert series.returns[0] > 0  # 標的下跌、空頭部位獲利


def test_intraday_return_requires_price_coverage() -> None:
    exp = svc.PortfolioExposure({"A": 10.0, "B": 10.0}, {}, 0.0, 0.0)
    series = svc.compute_return_series(
        1, exp, {"A": _closes([100.0, 100.0]), "B": _closes([100.0, 100.0])}
    )
    assert series is not None
    assert svc.intraday_return(series, {"A": 90.0, "B": 90.0}) == pytest.approx(-0.10)
    # 只有一半權重有報價 → 覆蓋率 50% < 80%，不計算
    assert svc.intraday_return(series, {"A": 90.0}) is None


def test_extract_live_prices_from_radar_cache() -> None:
    radar = {"aapl": {"quote": {"c": 190.5}}, "BAD": None, "ZERO": {"quote": {"c": 0}}}
    assert svc.extract_live_prices(radar) == {"AAPL": 190.5}


# ---------------------------------------------------------------------------
# 推播狀態：只有實際送達才前進武裝狀態
# ---------------------------------------------------------------------------


def _dd_snapshot(dd: float) -> DownsideSnapshot:
    return DownsideSnapshot(
        n_obs=252,
        sortino_63=0.5,
        sortino_252=0.5,
        max_drawdown=dd,
        current_drawdown=dd,
        var_95=0.01,
        cvar_95=0.01,
        cvar_short=0.01,
        cvar_short_baseline=0.01,
    )


@pytest.mark.asyncio
async def test_drawdown_state_not_advanced_when_channel_disabled(db_conn: Any) -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    database.set_user_notification_setting(4101, "risk_portfolio_downside", False)
    await svc._evaluate_and_notify(
        bot, 4101, _dd_snapshot(0.16), include_cvar=False, today_str="20260923"
    )
    bot.queue_dm.assert_not_awaited()
    assert database.get_kv_cache("downside_state_dd_4101") is None

    database.set_user_notification_setting(4101, "risk_portfolio_downside", True)
    await svc._evaluate_and_notify(
        bot, 4101, _dd_snapshot(0.16), include_cvar=False, today_str="20260923"
    )
    bot.queue_dm.assert_awaited_once()
    assert database.get_kv_cache("downside_state_dd_4101") == pytest.approx(0.15)

    # 同日再次檢查：武裝狀態已前進、不重發
    await svc._evaluate_and_notify(
        bot, 4101, _dd_snapshot(0.17), include_cvar=False, today_str="20260923"
    )
    bot.queue_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_cvar_alerts_on_entry_only(db_conn: Any) -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    database.upsert_user_config(4102, risk_limit=15.0)
    breach = DownsideSnapshot(
        n_obs=252,
        sortino_63=0.1,
        sortino_252=0.1,
        max_drawdown=0.05,
        current_drawdown=0.0,
        var_95=0.02,
        cvar_95=0.05,
        cvar_short=0.05,
        cvar_short_baseline=0.05,
    )
    await svc._evaluate_and_notify(
        bot, 4102, breach, include_cvar=True, today_str="20260922"
    )
    await svc._evaluate_and_notify(
        bot, 4102, breach, include_cvar=True, today_str="20260923"
    )
    # 持續超出預算：只在進入時推一次，隔天不重複
    assert bot.queue_dm.await_count == 1
    assert database.get_kv_cache("downside_state_cvar_4102") == ["BUDGET"]

    # 條件解除後狀態清空，下次再進入會再推
    await svc._evaluate_and_notify(
        bot, 4102, _dd_snapshot(0.0), include_cvar=True, today_str="20260924"
    )
    assert database.get_kv_cache("downside_state_cvar_4102") == []


# ---------------------------------------------------------------------------
# 收盤任務：寫入 NAV 快照
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_job_writes_nav_snapshot(db_conn: Any) -> None:
    database.upsert_user_config(4201, cash_reserve=1_000.0)
    _insert_asset(db_conn, 4201, "AAA", "HOLDING", {"quantity": 10, "avg_cost": 100})
    rng = np.random.default_rng(11)
    prices = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 120)))
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    with (
        patch.object(
            svc, "_fetch_closes", new=AsyncMock(return_value={"AAA": _closes(prices)})
        ),
        patch("services.llm_service.is_memory_safe", return_value=True),
        patch("market_time.is_market_open", return_value=False),
    ):
        await svc.run_daily_downside_job(bot, date(2026, 9, 23))

    rows = svc.load_nav_snapshots(4201)
    assert len(rows) == 1
    assert rows[0].date == "2026-09-23"
    assert rows[0].shares == {"AAA": 10.0}
    assert rows[0].nav == pytest.approx(10 * prices[-1] + 1_000.0)
    assert svc.get_cached_series(4201) is not None


@pytest.mark.asyncio
async def test_intraday_check_uses_cache_only() -> None:
    """盤中檢查只走快取；沒有快取的使用者不觸發任何歷史抓取。"""
    bot = MagicMock()
    bot._is_leader_instance = True
    with (
        patch.object(svc, "_fetch_closes", new=AsyncMock()) as fetch,
        patch("services.llm_service.is_memory_safe", return_value=True),
    ):
        await svc.run_intraday_downside_checks(bot, {"AAA": 100.0})
    fetch.assert_not_awaited()


def test_v082_migration_exports_required_attributes() -> None:
    from database.migrations import v082_add_portfolio_nav_daily as m

    assert m.version == 82
    assert m.description
    assert "portfolio_nav_daily" in m.sql


def test_snapshot_fields_render_without_data() -> None:
    from cogs.embed_builders.alert_embeds.downside_alerts import (
        create_downside_snapshot_fields,
        create_portfolio_downside_alert_embed,
    )

    assert "無法計算" in create_downside_snapshot_fields(None)[0][1]
    fields = create_downside_snapshot_fields(_dd_snapshot(0.12), _dd_snapshot(0.05))
    assert len(fields) == 2
    assert "Sortino" in fields[0][1]
    embed = create_portfolio_downside_alert_embed(
        "DRAWDOWN", _dd_snapshot(0.16), tier=0.15
    )
    assert "-15%" in (embed.title or "")
    assert all(len(f.value or "") <= 1024 for f in embed.fields)
