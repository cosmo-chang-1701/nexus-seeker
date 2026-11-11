"""配置偏離提醒服務測試（docs/strategies/09 §7）。"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import database
from database.user_settings import UserContext
from database.withdrawal_runway import RunwaySnapshot
from market_analysis.downside_monitor import NavSnapshot
from services import allocation_drift_service as svc
from services.downside_risk_service import PortfolioExposure

UID = 6101
DAY1 = date(2026, 11, 2)
DAY2 = date(2026, 11, 3)
NEXT_MONTH = date(2026, 12, 1)


def _bot() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot._is_leader_instance = True
    return bot


class _World:
    """可變情境：以 patch 取代服務的各個載入函式。"""

    def __init__(self) -> None:
        self.shares: dict[str, float] = {"BOXX": 100.0}
        self.closes: dict[str, float] = {"BOXX": 100.0, "NVDA": 100.0}
        self.cash = 0.0
        self.target = 100.0
        self.withdrawal = 0.0
        self.nav_date: Optional[str] = None
        self.nav_date_override: Optional[str] = None  # 模擬 NAV 快照非當日
        self.runway: Optional[RunwaySnapshot] = None
        self.macro: list[tuple[str, str, Optional[str]]] = []

    def ctx(self, uid: int) -> UserContext:
        return UserContext(
            user_id=uid,
            capital=100000.0,
            risk_limit=2.0,
            total_weighted_delta=0.0,
            total_theta=0.0,
            total_gamma=0.0,
            withdrawal_amount=self.withdrawal,
            target_equity_pct=self.target,
        )

    def navs(self, uid: int, n: int) -> list[NavSnapshot]:
        assert self.nav_date is not None
        return [NavSnapshot(self.nav_date, 0.0, dict(self.shares), dict(self.closes))]

    def exposure(self, uid: int) -> PortfolioExposure:
        return PortfolioExposure(dict(self.shares), {}, 0.0, self.cash)

    async def runway_snap(self, uid: int) -> Optional[RunwaySnapshot]:
        return self.runway

    def regimes(self, before: date, limit: int) -> list[tuple[str, str, Optional[str]]]:
        return self.macro


def _runway(as_of: date, next_date: Optional[str], amount: float) -> RunwaySnapshot:
    fields: dict[str, Any] = {f.name: 0.0 for f in dataclasses.fields(RunwaySnapshot)}
    fields.update(
        user_id=UID,
        as_of=as_of.isoformat(),
        nav_date=as_of.isoformat(),
        capped=False,
        next_withdrawal=amount,
        beta_is_fallback=False,
        cpi_missing=False,
        next_date=next_date,
    )
    return RunwaySnapshot(**fields)


async def _run(world: _World, bot: Any, day: date) -> int:
    world.nav_date = world.nav_date_override or day.isoformat()
    with (
        patch.object(svc, "is_memory_safe", return_value=True),
        patch.object(svc, "get_users_with_positions", return_value=[UID]),
        patch.object(svc, "get_full_user_context", side_effect=world.ctx),
        patch.object(svc, "load_nav_snapshots", side_effect=world.navs),
        patch.object(svc, "load_portfolio_exposure", side_effect=world.exposure),
        patch.object(svc, "get_runway_snapshot", side_effect=world.runway_snap),
        patch.object(svc, "load_recent_regimes", side_effect=world.regimes),
    ):
        return await svc.run_allocation_drift_job(bot, day)


def _state() -> Any:
    return database.get_kv_cache(f"alloc_drift_state_{UID}")


def _embed(bot: Any, idx: int = -1) -> Any:
    return bot.queue_dm.await_args_list[idx].kwargs["embed"]


@pytest.mark.asyncio
async def test_crossing_then_monthly_then_back_inside(db_conn: Any) -> None:
    world, bot = _World(), _bot()
    await _run(world, bot, DAY1)
    bot.queue_dm.assert_awaited_once()
    embed = _embed(bot)
    assert embed.title == "⚖️ 配置偏離提醒"
    assert "已達 5 pp 門檻" in embed.description
    assert "移入股票" in "".join(f.value for f in embed.fields)
    assert _state() == {
        "outside": True,
        "last_sent": DAY1.isoformat(),
        "target_pct": 100.0,
    }

    await _run(world, bot, DAY2)  # 同月不再提醒
    bot.queue_dm.assert_awaited_once()

    await _run(world, bot, NEXT_MONTH)  # 新月份第一次
    assert bot.queue_dm.await_count == 2
    assert "每月提醒" in _embed(bot).description

    world.shares = {"NVDA": 100.0}  # 回到門檻內
    await _run(world, bot, date(2026, 12, 2))
    assert bot.queue_dm.await_count == 2
    assert _state()["outside"] is False


@pytest.mark.asyncio
async def test_disabled_target_and_fail_closed_cases(
    db_conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    world, bot = _World(), _bot()
    world.target = -1.0
    await _run(world, bot, DAY1)
    bot.queue_dm.assert_not_awaited()
    assert _state() is None

    # 先有超標狀態，再停用 → 旗標清除（日後重新啟用算新的一次超標）
    world.target = 100.0
    await _run(world, bot, DAY1)
    assert _state()["outside"] is True
    world.target = -1.0
    await _run(world, bot, DAY2)
    state = _state()
    assert state["outside"] is False and state["last_sent"] == DAY1.isoformat()
    assert state["target_pct"] is None
    assert bot.queue_dm.await_count == 1

    # 以下情境狀態皆不得改變
    world.target = 100.0
    await _run(world, bot, DAY2)  # 重新啟用 → CROSSING
    assert bot.queue_dm.await_count == 2
    before = _state()

    world.nav_date_override = "2026-01-01"  # NAV 非當日：移除日期防線就會送 MONTHLY
    assert await _run(world, bot, NEXT_MONTH) == 0
    assert bot.queue_dm.await_count == 2 and _state() == before
    world.nav_date_override = None

    world.shares = {"BOXX": 100.0, "XYZ": 10.0}  # 長倉缺收盤價
    await _run(world, bot, NEXT_MONTH)
    assert bot.queue_dm.await_count == 2 and _state() == before
    world.shares = {"BOXX": 100.0}

    world.withdrawal = 1000.0  # 有提領設定但無當日跑道快照
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=svc.logger.name):
        await _run(world, bot, NEXT_MONTH)
    assert bot.queue_dm.await_count == 2 and _state() == before
    # 缺 snap is None 防線時會 AttributeError 被逐戶 except 吞掉並記 ERROR
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("提領跑道快照非當日" in r.getMessage() for r in caplog.records)
    world.runway = _runway(DAY1, None, 0.0)  # 非當日快照
    await _run(world, bot, NEXT_MONTH)
    assert bot.queue_dm.await_count == 2 and _state() == before


@pytest.mark.asyncio
async def test_channel_off_does_not_advance_state(db_conn: Any) -> None:
    world, bot = _World(), _bot()
    database.set_user_notification_setting(UID, "risk_allocation_drift", False)
    await _run(world, bot, DAY1)
    bot.queue_dm.assert_not_awaited()
    assert _state() is None

    database.set_user_notification_setting(UID, "risk_allocation_drift", True)
    await _run(world, bot, DAY2)
    bot.queue_dm.assert_awaited_once()
    assert "已達 5 pp 門檻" in _embed(bot).description
    assert _state()["last_sent"] == DAY2.isoformat()


def _reserve_world(next_date: str) -> _World:
    world = _World()
    # 投資額 = 股票 + BOXX；BOXX = 12%，目標 100，其餘為股票
    world.shares = {"NVDA": 88.0, "BOXX": 12.0}
    world.closes = {"NVDA": 100.0, "BOXX": 100.0}
    world.withdrawal = 1200.0
    world.runway = _runway(DAY1, next_date, 1200.0)
    return world


@pytest.mark.asyncio
async def test_withdrawal_reserve_removes_drift(db_conn: Any) -> None:
    world, bot = _reserve_world("2026-12-01"), _bot()  # 12/1 提領，11 月在預留窗內
    await _run(world, bot, DAY1)
    bot.queue_dm.assert_not_awaited()
    assert _state()["outside"] is False

    world2, bot2 = _reserve_world("2027-02-01"), _bot()  # 三個月後 → 不預留
    await _run(world2, bot2, DAY1)
    bot2.queue_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_reserve_line_shown_when_reserved(db_conn: Any) -> None:
    world, bot = _reserve_world("2026-12-01"), _bot()
    world.shares = {"NVDA": 50.0, "BOXX": 50.0}  # 預留 1200 後仍偏離
    await _run(world, bot, DAY1)
    text = "".join(f.value for f in _embed(bot).fields)
    assert "已預留下次提領 $1,200（2026-12-01）" in text


@pytest.mark.asyncio
async def test_target_change_while_outside_is_new_crossing(db_conn: Any) -> None:
    world, bot = _World(), _bot()
    await _run(world, bot, DAY1)
    world.target = 60.0
    await _run(world, bot, DAY2)
    assert bot.queue_dm.await_count == 2
    assert "已達 5 pp 門檻" in _embed(bot).description


@pytest.mark.asyncio
async def test_worst_macro_does_not_block_and_is_noted(db_conn: Any) -> None:
    world, bot = _World(), _bot()
    world.macro = [(DAY1.isoformat(), "WORST", "WORST")]
    await _run(world, bot, DAY1)
    bot.queue_dm.assert_awaited_once()
    text = "".join(f.value for f in _embed(bot).fields)
    assert "WORST" in text and "固定比例不依總經調整" in text


@pytest.mark.asyncio
async def test_macro_note_without_same_day_data(db_conn: Any) -> None:
    world, bot = _World(), _bot()
    world.macro = [("2026-10-30", "GOOD", "GOOD")]  # 非當日 → 無資料
    await _run(world, bot, DAY1)
    assert "無資料" in "".join(f.value for f in _embed(bot).fields)


@pytest.mark.asyncio
async def test_non_leader_returns_zero(db_conn: Any) -> None:
    bot = _bot()
    bot._is_leader_instance = False
    assert await _run(_World(), bot, DAY1) == 0
    bot.queue_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_market_runs_drift_job_after_runway_job() -> None:
    from cogs.trading.after_market import AfterMarketCog

    order: list[str] = []

    def _job(name: str) -> AsyncMock:
        return AsyncMock(side_effect=lambda *a, **k: order.append(name))

    fake_self = SimpleNamespace(bot=MagicMock(_is_leader_instance=True))
    calendar = MagicMock()
    calendar.schedule.return_value = SimpleNamespace(empty=False)
    with (
        patch("cogs.trading.after_market.market_time.nyse_calendar", calendar),
        patch("cogs.trading.after_market.database.purge_old_cache", return_value=0),
        patch(
            "market_analysis.sentiment.canonical_history.snapshot_trading_day",
            _job("snapshot"),
        ),
        patch(
            "services.downside_risk_service.run_daily_downside_job", _job("downside")
        ),
        patch("services.macro_signal_service.run_macro_signal_job", _job("macro")),
        patch(
            "services.withdrawal_runway_service.run_withdrawal_runway_job",
            _job("runway"),
        ),
        patch(
            "services.allocation_drift_service.run_allocation_drift_job", _job("drift")
        ),
    ):
        await AfterMarketCog.dynamic_after_market_report.coro(fake_self)  # type: ignore[arg-type]
    assert order == ["snapshot", "downside", "macro", "runway", "drift"]
