"""提領跑道服務測試（docs/risk_portfolio/05 階段二快照、階段三推播）。"""

import dataclasses
import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import database
from database.user_settings import UserContext
from database.withdrawal_runway import RunwaySnapshot
from market_analysis.downside_monitor import NavSnapshot
from market_analysis.macro_signals import Observation, available_date_for
from services import withdrawal_runway_service as svc
from services.withdrawal_runway_service import (
    compute_user_runway,
    cpi_at,
    cpi_for_anchor,
    is_snapshot_stale,
    next_withdrawal_date,
    parse_months,
    parse_target_weights,
    portfolio_beta,
    reminder_due,
    sellable_holdings,
)


def test_parse_months_falls_back_on_bad_input() -> None:
    assert parse_months("7,1") == [1, 7]
    assert parse_months("abc") == [1, 7]
    assert parse_months("0,13") == [1, 7]


def test_next_withdrawal_date_first_trading_day() -> None:
    # 2026-09-30 之後的下一個提領日：2027-01 的首個交易日（1/1 休市 → 1/4）
    target, month, days = next_withdrawal_date(date(2026, 9, 30), [1, 7])
    assert (target, month) == (date(2027, 1, 4), 1)
    assert days > 60


def test_next_withdrawal_date_in_withdrawal_month_after_first_day() -> None:
    # 2026-07-06 已過 7 月首個交易日 → 下一次是 2027-01
    target, month, _ = next_withdrawal_date(date(2026, 7, 6), [1, 7])
    assert (target.year, month) == (2027, 1)


def test_next_withdrawal_date_day_before_counts_one_day() -> None:
    target, month, days = next_withdrawal_date(date(2026, 12, 31), [1, 7])
    assert (target, month, days) == (date(2027, 1, 4), 1, 1)


def test_cpi_lookups_respect_release_lag() -> None:
    obs = [
        Observation(
            date(2026, 7, 1), 320.0, available_date_for("monthly_cpi", date(2026, 7, 1))
        ),
        Observation(
            date(2026, 8, 1), 321.0, available_date_for("monthly_cpi", date(2026, 8, 1))
        ),
    ]
    assert available_date_for("monthly_cpi", date(2026, 8, 1)) == date(2026, 9, 15)
    assert cpi_at(obs, date(2026, 9, 14)) == 320.0
    assert cpi_at(obs, date(2026, 9, 15)) == 321.0
    assert cpi_at(obs, date(2026, 7, 1)) is None
    assert cpi_for_anchor(obs, "2026-08") == 321.0
    assert cpi_for_anchor(obs, "2025-01") is None
    assert cpi_for_anchor(obs, "bad") is None


def test_portfolio_beta() -> None:
    rng = np.random.default_rng(0)
    mkt = rng.normal(0, 0.01, 120)
    port = 1.5 * mkt
    dates = [f"d{i}" for i in range(120)]
    beta = portfolio_beta(dates, port.tolist(), dict(zip(dates, mkt.tolist())))
    assert beta == pytest.approx(1.5, rel=1e-6)
    assert (
        portfolio_beta(dates[:30], port[:30].tolist(), dict(zip(dates, mkt.tolist())))
        is None
    )
    assert portfolio_beta(dates, port.tolist(), {}) is None


def _kwargs(**over: object) -> dict:
    base: dict = dict(
        user_id=1,
        today=date(2026, 9, 30),
        nav=100000.0,
        nav_date="2026-09-30",
        boxx_value=0.0,
        beta=1.3,
        withdrawal_amount=10000.0,
        months=[1, 7],
        cpi_anchor=300.0,
        cpi_now=300.0,
    )
    base.update(over)
    return base


def test_compute_user_runway_matches_spec_example() -> None:
    snap = compute_user_runway(**_kwargs())
    assert snap is not None
    assert snap.zero_years == pytest.approx(5.0)
    assert snap.stress_years <= snap.zero_years
    assert snap.stress_years == min(snap.gfc_years, snap.dotcom_years)
    assert snap.next_date == "2027-01-04"
    assert not snap.cpi_missing and not snap.beta_is_fallback


def test_compute_user_runway_inflation_and_flags() -> None:
    snap = compute_user_runway(**_kwargs(cpi_now=330.0, beta=None))
    assert snap is not None
    assert snap.next_withdrawal == pytest.approx(11000.0)
    assert snap.beta_is_fallback and snap.beta == 1.3
    missing = compute_user_runway(**_kwargs(cpi_anchor=None))
    assert missing is not None and missing.cpi_missing
    assert missing.next_withdrawal == pytest.approx(10000.0)


def test_compute_user_runway_skips_disabled_or_empty() -> None:
    assert compute_user_runway(**_kwargs(withdrawal_amount=0.0)) is None
    assert compute_user_runway(**_kwargs(nav=0.0)) is None


def test_boxx_reduces_dotcom_exposure() -> None:
    plain = compute_user_runway(**_kwargs())
    hedged = compute_user_runway(**_kwargs(boxx_value=50000.0))
    assert plain is not None and hedged is not None
    assert hedged.boxx_payments == 5
    assert hedged.dotcom_years >= plain.dotcom_years


def _snap(nav_date: str) -> RunwaySnapshot:
    snap = compute_user_runway(**_kwargs(nav_date=nav_date))
    assert snap is not None
    return snap


def test_snapshot_stale_after_five_trading_days() -> None:
    assert not is_snapshot_stale(_snap("2026-09-30"), date(2026, 10, 7))  # 5 個交易日
    assert is_snapshot_stale(_snap("2026-09-30"), date(2026, 10, 8))  # 6 個交易日
    assert not is_snapshot_stale(_snap("2026-09-30"), date(2026, 9, 30))


# ---------------------------------------------------------------------------
# 階段三：推播
# ---------------------------------------------------------------------------


def test_reminder_due_pre_window_and_catch_up() -> None:
    target = date(2027, 1, 4)  # 2027-01 首個交易日（1/1 休市）
    assert reminder_due(date(2026, 12, 14), [1, 7], None) is None
    assert reminder_due(date(2026, 12, 15), [1, 7], None) == ("PRE", target)
    # 當天任務被跳過 → 隔天補發；已送過則同視窗不再提醒
    assert reminder_due(date(2026, 12, 16), [1, 7], None) == ("PRE", target)
    assert reminder_due(date(2026, 12, 16), [1, 7], "PRE_2027-01-04") is None
    assert reminder_due(date(2026, 12, 31), [1, 7], "PRE_2027-01-04") is None


def test_reminder_due_day_window() -> None:
    target = date(2027, 1, 4)
    assert reminder_due(target, [1, 7], "PRE_2027-01-04") == ("DAY", target)
    assert reminder_due(target, [1, 7], "DAY_2027-01-04") is None
    assert reminder_due(date(2027, 1, 11), [1, 7], "PRE_2027-01-04") == ("DAY", target)
    assert reminder_due(date(2027, 1, 12), [1, 7], "PRE_2027-01-04") is None


def test_reminder_due_next_month_in_cycle() -> None:
    assert reminder_due(date(2027, 6, 15), [1, 7], "DAY_2027-01-04") == (
        "PRE",
        date(2027, 7, 1),
    )
    assert reminder_due(date(2027, 6, 15), [7], None) == ("PRE", date(2027, 7, 1))
    assert reminder_due(date(2027, 6, 14), [7], None) is None


def test_parse_target_weights() -> None:
    assert parse_target_weights(None) is None
    assert parse_target_weights("bad") is None
    assert parse_target_weights("[1, 2]") is None
    assert parse_target_weights(json.dumps({"nvda": 2, "x": -1})) == {"NVDA": 2.0}
    assert parse_target_weights(json.dumps({"x": 0})) is None


def test_resolve_target_weights_priority_and_mixing() -> None:
    from services.withdrawal_runway_service import resolve_target_weights

    syms = {"VOO": 50_000.0, "NVDA": 30_000.0, "AMD": 20_000.0}
    # 手動覆寫最優先
    assert resolve_target_weights({"NVDA": 1.0}, {"VOO": 0.5}, syms) == {"NVDA": 1.0}
    # 全無目標 → None（等權）
    assert resolve_target_weights(None, {}, syms) is None
    # 部分設定：VOO 40%，其餘兩檔平分剩餘 60%
    w = resolve_target_weights(None, {"VOO": 0.4}, syms)
    assert w == pytest.approx({"VOO": 0.4, "NVDA": 0.3, "AMD": 0.3})
    # 全部有目標且總和超過 100% → 正規化
    w = resolve_target_weights(None, {"VOO": 0.8, "NVDA": 0.4, "AMD": 0.3}, syms)
    assert w == pytest.approx({"VOO": 0.8 / 1.5, "NVDA": 0.4 / 1.5, "AMD": 0.3 / 1.5})
    # 不在可賣清單者（如 BOXX 或已清空）忽略
    assert resolve_target_weights(None, {"QQQ": 0.5}, syms) is None


def test_resolve_target_weights_full_targets_keep_unset_at_current_share() -> None:
    """目標已占滿 100% 時，未設目標的持股以目前占比為權重，不會被當成全額超配
    而在提領時優先賣光（審查發現：舊規則給 0 權重）。"""
    from market_analysis.withdrawal_runway import plan_withdrawal
    from services.withdrawal_runway_service import resolve_target_weights

    syms = {"VOO": 50_000.0, "NVDA": 30_000.0, "AMD": 20_000.0}
    w = resolve_target_weights(None, {"VOO": 0.7, "NVDA": 0.3}, syms)
    assert w is not None
    assert w["AMD"] == pytest.approx(0.2)
    assert w["VOO"] == pytest.approx(0.7 * 0.8)
    assert w["NVDA"] == pytest.approx(0.3 * 0.8)

    plan = plan_withdrawal(10_000.0, 0.0, syms, w)
    # AMD 只按市值比例分攤，而不是整筆 10,000 都由它賣出
    assert plan.sells.get("AMD", 0.0) < 10_000.0 * 0.5


def test_sellable_holdings_excludes_boxx_shorts_and_missing_prices() -> None:
    out = sellable_holdings(
        {"NVDA": 10, "BOXX": 50, "TSLA": -5, "AMD": 3},
        {"NVDA": 100.0, "BOXX": 110.0, "TSLA": 200.0},
    )
    assert out == {"NVDA": 1000.0}


def _bot() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    return bot


def _stress_snap(years: float, **over: Any) -> RunwaySnapshot:
    snap = dataclasses.replace(
        _snap("2026-09-30"), stress_years=years, capped=years >= 10.0
    )
    return dataclasses.replace(snap, **over) if over else snap


@pytest.mark.asyncio
async def test_runway_warning_fires_once_and_rearms(db_conn: Any) -> None:
    uid = 5101
    bot = _bot()
    key = "runway_state_tiers_5101"
    database.set_user_notification_setting(uid, "risk_withdrawal_runway", True)

    # 無狀態 = 全部武裝：上線即低於 3 年 → 推 3 年級
    await svc._notify_warnings(
        bot, uid, _stress_snap(2.3), stale=False, today_str="20260930"
    )
    bot.queue_dm.assert_awaited_once()
    assert "3 年" in bot.queue_dm.await_args.kwargs["embed"].title
    assert database.get_kv_cache(key) == [2, 1]

    # 隔天仍在 2.3 年：3 年級已解除、2 年級未跌破 → 不重發
    await svc._notify_warnings(
        bot, uid, _stress_snap(2.3), stale=False, today_str="20261001"
    )
    bot.queue_dm.assert_awaited_once()

    # 跌破 1 年（同時跌破 2 年）→ 只以最嚴重一級推播一則
    await svc._notify_warnings(
        bot, uid, _stress_snap(0.8), stale=False, today_str="20261002"
    )
    assert bot.queue_dm.await_count == 2
    assert "1 年" in bot.queue_dm.await_args.kwargs["embed"].title
    assert database.get_kv_cache(key) == []

    # 回升到 3.5 年 → 全部重新武裝（不推播）
    await svc._notify_warnings(
        bot, uid, _stress_snap(3.5), stale=False, today_str="20261005"
    )
    assert bot.queue_dm.await_count == 2
    assert database.get_kv_cache(key) == [3, 2, 1]


@pytest.mark.asyncio
async def test_runway_warning_not_advanced_when_disabled_or_stale(
    db_conn: Any,
) -> None:
    uid = 5102
    bot = _bot()
    key = "runway_state_tiers_5102"
    database.set_user_notification_setting(uid, "risk_withdrawal_runway", False)
    await svc._notify_warnings(
        bot, uid, _stress_snap(2.3), stale=False, today_str="20260930"
    )
    bot.queue_dm.assert_not_awaited()
    assert database.get_kv_cache(key) is None

    database.set_user_notification_setting(uid, "risk_withdrawal_runway", True)
    await svc._notify_warnings(
        bot, uid, _stress_snap(2.3), stale=True, today_str="20260930"
    )
    bot.queue_dm.assert_not_awaited()
    assert database.get_kv_cache(key) is None


def _ctx(uid: int, **over: Any) -> UserContext:
    base: dict[str, Any] = dict(
        user_id=uid,
        capital=100000.0,
        risk_limit=2.0,
        total_weighted_delta=0.0,
        total_theta=0.0,
        total_gamma=0.0,
        withdrawal_amount=10000.0,
        withdrawal_months="1,7",
    )
    base.update(over)
    return UserContext(**base)


def _insert_holding(db_conn: Any, uid: int, sym: str, qty: float) -> None:
    db_conn.execute(
        "INSERT INTO assets (user_id, symbol, context_type, metadata) VALUES (?, ?, 'HOLDING', ?)",
        (uid, sym, json.dumps({"quantity": qty})),
    )
    db_conn.commit()


@pytest.mark.asyncio
async def test_withdrawal_reminder_sell_list_and_state(db_conn: Any) -> None:
    uid = 5103
    bot = _bot()
    database.set_user_notification_setting(uid, "risk_withdrawal_runway", True)
    _insert_holding(db_conn, uid, "NVDA", 100)
    _insert_holding(db_conn, uid, "BOXX", 50)
    _insert_holding(db_conn, uid, "TSLA", -10)
    nav = NavSnapshot(
        date="2026-12-15",
        nav=15500.0,
        shares={"NVDA": 100, "BOXX": 50, "TSLA": -10},
        closes={"NVDA": 100.0, "BOXX": 110.0, "TSLA": 200.0},
    )
    snap = _stress_snap(2.0, boxx_value=5500.0, next_withdrawal=10000.0)

    await svc._notify_reminder(
        bot,
        uid,
        snap,
        _ctx(uid),
        nav,
        today=date(2026, 12, 15),
        today_str="20261215",
    )
    bot.queue_dm.assert_awaited_once()
    embed = bot.queue_dm.await_args.kwargs["embed"]
    assert "前置提醒" in embed.title
    sells = next(f.value for f in embed.fields if "賣出清單" in f.name)
    assert "BOXX：`$5,500`" in sells
    assert "NVDA：`$4,500`" in sells
    assert "TSLA" not in sells
    assert "年度再平衡" in embed.description
    assert database.get_kv_cache("runway_state_remind_5103") == "PRE_2027-01-04"

    # 同視窗隔天不再提醒
    await svc._notify_reminder(
        bot,
        uid,
        snap,
        _ctx(uid),
        nav,
        today=date(2026, 12, 16),
        today_str="20261216",
    )
    bot.queue_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_withdrawal_reminder_shortfall_and_disabled_channel(
    db_conn: Any,
) -> None:
    uid = 5104
    bot = _bot()
    _insert_holding(db_conn, uid, "NVDA", 10)
    nav = NavSnapshot(
        date="2027-06-15", nav=1000.0, shares={"NVDA": 10}, closes={"NVDA": 100.0}
    )
    snap = _stress_snap(0.1, boxx_value=0.0, next_withdrawal=10000.0)
    kwargs: dict[str, Any] = dict(today=date(2027, 6, 15), today_str="20270615")

    database.set_user_notification_setting(uid, "risk_withdrawal_runway", False)
    await svc._notify_reminder(bot, uid, snap, _ctx(uid), nav, **kwargs)
    bot.queue_dm.assert_not_awaited()
    assert database.get_kv_cache("runway_state_remind_5104") is None

    database.set_user_notification_setting(uid, "risk_withdrawal_runway", True)
    await svc._notify_reminder(bot, uid, snap, _ctx(uid), nav, **kwargs)
    embed = bot.queue_dm.await_args.kwargs["embed"]
    sells = next(f.value for f in embed.fields if "賣出清單" in f.name)
    assert "差額 `$9,000`" in sells
    assert "年度再平衡" not in (embed.description or "")
