"""/x 量化雷達「靜默期避讓」(`avoid_silent_period`) 的單元測試。

語意：`silent_period_days` = N 時，未來 N 天內（含今日，美東日期）有財報或高影響
總經事件的標的即排除；拿不到事件日期時退回 `iv_data` 的布林值。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.unified_terminal.silent_period import (
    DEFAULT_SILENT_PERIOD_DAYS,
    SilentPeriodContext,
    _load_context_sync,
    is_in_silent_period,
    load_silent_period_context,
    normalize_silent_period_days,
)
from database.calendar_cache import replace_macro_month_events, save_earnings_cache
from database.connection import execute_write

# 2031-05-12 10:00 ET（遠期日期，避免與其他測試共用的記憶體資料庫互相干擾）
_NOW = datetime(2031, 5, 12, 14, 0, tzinfo=timezone.utc)
_TODAY = date(2031, 5, 12)


def _ctx(
    *,
    days: int = 5,
    macro: bool | None = False,
    earnings: dict[str, date | None] | None = None,
) -> SilentPeriodContext:
    return SilentPeriodContext(
        today=_TODAY,
        window_days=days,
        macro_event_in_window=macro,
        earnings_dates=earnings or {},
    )


def _result(sym: str, **iv_flags: bool) -> dict[str, Any]:
    return {"symbol": sym, "iv_data": dict(iv_flags) if iv_flags else None}


# --- 純判定邏輯 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(3, 3), ("7", 7), (-2, 0), (None, DEFAULT_SILENT_PERIOD_DAYS), ("x", 5)],
)
def test_normalize_silent_period_days(raw: Any, expected: int) -> None:
    assert normalize_silent_period_days(raw) == expected


@pytest.mark.parametrize(
    ("earn_date", "days", "expected"),
    [
        (date(2031, 5, 12), 5, True),  # 今日
        (date(2031, 5, 17), 5, True),  # 窗口最後一天
        (date(2031, 5, 18), 5, False),  # 超出窗口
        (date(2031, 5, 14), 1, False),  # 視窗縮短後不再命中
        (date(2031, 5, 14), 2, True),
    ],
)
def test_earnings_date_window(earn_date: date, days: int, expected: bool) -> None:
    ctx = _ctx(days=days, earnings={"AAPL": earn_date})
    # 事件日期已知時不採用布林值（即使 iv_data 標記有財報）
    assert is_in_silent_period(_result("aapl", has_earnings_event=True), ctx) is (
        expected
    )


def test_cached_no_earnings_is_definitive() -> None:
    """快取列存在但無財報日（例如 ETF）→ 確定無財報，不看布林值。"""
    ctx = _ctx(earnings={"SPY": None})
    assert not is_in_silent_period(_result("SPY", has_earnings_event=True), ctx)


@pytest.mark.parametrize("flag", [True, False])
def test_missing_earnings_cache_falls_back_to_flag(flag: bool) -> None:
    ctx = _ctx()
    assert is_in_silent_period(_result("NVDA", has_earnings_event=flag), ctx) is flag


@pytest.mark.parametrize("flag", [True, False])
def test_past_earnings_date_falls_back_to_flag(flag: bool) -> None:
    """快取的財報日已過（下一期未知）→ 退回布林值。"""
    ctx = _ctx(earnings={"AAPL": date(2031, 5, 1)})
    assert is_in_silent_period(_result("AAPL", has_earnings_event=flag), ctx) is flag


def test_macro_event_in_window_excludes_every_symbol() -> None:
    ctx = _ctx(macro=True, earnings={"SPY": None})
    assert is_in_silent_period(_result("SPY"), ctx)
    assert is_in_silent_period(_result("NVDA"), ctx)


def test_macro_known_absent_ignores_flag() -> None:
    ctx = _ctx(macro=False, earnings={"SPY": None})
    assert not is_in_silent_period(_result("SPY", has_macro_event=True), ctx)


@pytest.mark.parametrize("flag", [True, False])
def test_macro_unknown_falls_back_to_flag(flag: bool) -> None:
    ctx = _ctx(macro=None, earnings={"SPY": None})
    assert is_in_silent_period(_result("SPY", has_macro_event=flag), ctx) is flag


def test_flag_fallback_supports_object_iv_data() -> None:
    iv_data = MagicMock(has_earnings_event=True, has_macro_event=False)
    ctx = _ctx()
    assert is_in_silent_period({"symbol": "NVDA", "iv_data": iv_data}, ctx)


# --- 讀取 SQLite 快取 ---


def _clear_macro_months() -> None:
    months = ("2031-05", "2031-06")
    execute_write(
        "DELETE FROM economic_calendar_events WHERE month_key IN (?, ?)", months
    )
    execute_write(
        "DELETE FROM economic_calendar_month_cache WHERE month_key IN (?, ?)", months
    )


@pytest.fixture
def seeded_cache(db_conn: Any) -> Iterator[None]:
    _clear_macro_months()
    save_earnings_cache("ZSPA", "2031-05-15")  # 窗口內
    save_earnings_cache("ZSPB", "2031-05-30")  # 窗口外
    save_earnings_cache("ZSPC", None)  # 已查過、無財報
    save_earnings_cache("ZSPD", "not-a-date")  # 無法解析
    yield
    _clear_macro_months()


def _macro(time: str, event: str = "CPI 年增率") -> dict[str, Any]:
    return {"event": event, "time": time, "impact": "high", "country": "US"}


def test_load_context_reads_earnings_cache(seeded_cache: None) -> None:
    replace_macro_month_events("2031-05", [_macro("2031-05-28T12:30:00Z")])
    ctx = _load_context_sync(["zspa", "ZSPB", "ZSPC", "ZSPD", "ZSPE"], 5, _NOW)

    assert ctx.today == _TODAY
    assert ctx.earnings_dates == {
        "ZSPA": date(2031, 5, 15),
        "ZSPB": date(2031, 5, 30),
        "ZSPC": None,
    }
    # 無快取列 / 日期無法解析 → 不在 dict 中，交由布林值退路
    assert "ZSPD" not in ctx.earnings_dates
    assert "ZSPE" not in ctx.earnings_dates
    assert ctx.macro_event_in_window is False


def test_load_context_detects_macro_event_in_window(seeded_cache: None) -> None:
    replace_macro_month_events("2031-05", [_macro("2031-05-14T12:30:00Z")])
    assert _load_context_sync([], 5, _NOW).macro_event_in_window is True
    # 視窗縮為 1 天（5/12–5/13）即不包含 5/14 的事件
    assert _load_context_sync([], 1, _NOW).macro_event_in_window is False


def test_load_context_ignores_events_already_released_today(
    seeded_cache: None,
) -> None:
    # 今日 08:30 ET（12:30Z）已公布，早於現在 10:00 ET
    replace_macro_month_events("2031-05", [_macro("2031-05-12T12:30:00Z")])
    assert _load_context_sync([], 0, _NOW).macro_event_in_window is False


def test_load_context_window_end_uses_eastern_end_of_day(seeded_cache: None) -> None:
    # 5/17 22:00 ET = 5/18 02:00Z，仍屬美東 5/17（窗口最後一天）
    replace_macro_month_events("2031-05", [_macro("2031-05-18T02:00:00Z")])
    assert _load_context_sync([], 5, _NOW).macro_event_in_window is True
    assert _load_context_sync([], 4, _NOW).macro_event_in_window is False


def test_load_context_missing_month_cache_is_unknown(seeded_cache: None) -> None:
    # 2031-05 已快取，但 20 天窗口跨入尚未快取的 2031-06 → 無法確認，退回布林值
    replace_macro_month_events("2031-05", [_macro("2031-05-28T12:30:00Z")])
    assert _load_context_sync([], 20, _NOW).macro_event_in_window is None

    replace_macro_month_events("2031-06", [_macro("2031-06-11T12:30:00Z")])
    assert _load_context_sync([], 20, _NOW).macro_event_in_window is True


@pytest.mark.asyncio
async def test_load_silent_period_context_normalizes_inputs() -> None:
    with patch(
        "cogs.unified_terminal.silent_period._load_context_sync",
        return_value=_ctx(),
    ) as loader:
        naive_now = datetime(2031, 5, 12, 14, 0)
        await load_silent_period_context(["AAPL"], "3", now_utc=naive_now)
    symbols, days, now = loader.call_args.args
    assert symbols == ["AAPL"]
    assert days == 3
    assert now.tzinfo is not None


# --- 雷達批次掃描整合 ---


def _state(quant_filters: list[str], days: int) -> dict[str, Any]:
    return {
        "scope": "ALL",
        "quant_filters": quant_filters,
        "params": {
            "max_pain_threshold": 10.0,
            "abs_support_tolerance": 1.0,
            "silent_period_days": days,
        },
        "selected_tag": None,
    }


async def _run_scan(state: dict[str, Any], ctx: SilentPeriodContext) -> tuple[Any, Any]:
    from cogs.unified_terminal.cog import UnifiedTerminalCog

    cog = UnifiedTerminalCog(MagicMock())
    interaction = AsyncMock()
    interaction.user.id = 12345
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done.return_value = True
    interaction.followup.send = AsyncMock()

    async def fake_fetch(sym: str) -> dict[str, Any]:
        return {"symbol": sym, "iv_data": None}

    cog._fetch_sym_radar_data_fast = fake_fetch  # type: ignore[method-assign]

    def to_thread_side_effect(func: Any, *args: Any, **kwargs: Any) -> Any:
        if "get_user_portfolio" in func.__name__:
            return [(123, "AAPL"), (123, "TSLA")]
        return [{"symbol": "AAPL"}, {"symbol": "TSLA"}]

    with (
        patch(
            "cogs.unified_terminal.cog.asyncio.to_thread",
            side_effect=to_thread_side_effect,
        ),
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
        patch(
            "cogs.unified_terminal.silent_period.load_silent_period_context",
            new_callable=AsyncMock,
            return_value=ctx,
        ) as loader,
        patch("cogs.unified_terminal.batch_scan.build_radar_scan_embed") as builder,
        patch("cogs.unified_terminal.batch_scan.BatchScanPaginatedView") as view_cls,
    ):
        builder.return_value = discord.Embed(title="Radar Scan")
        view_cls.return_value = discord.ui.View()
        await cog.execute_unified_scan(interaction, state, 12345)
    return loader, builder


@pytest.mark.asyncio
async def test_batch_scan_excludes_symbols_in_silent_period() -> None:
    ctx = _ctx(days=3, earnings={"AAPL": date(2031, 5, 14), "TSLA": date(2031, 6, 20)})
    loader, builder = await _run_scan(_state(["avoid_silent_period"], 3), ctx)

    loader.assert_awaited_once()
    symbols, days = loader.await_args.args
    assert sorted(symbols) == ["AAPL", "TSLA"]
    assert days == 3
    filtered = builder.call_args.args[0]
    assert [r["symbol"] for r in filtered] == ["TSLA"]


@pytest.mark.asyncio
async def test_batch_scan_skips_silent_period_when_filter_off() -> None:
    ctx = _ctx(macro=True)
    loader, builder = await _run_scan(_state([], 3), ctx)
    loader.assert_not_awaited()
    filtered = builder.call_args.args[0]
    assert sorted(r["symbol"] for r in filtered) == ["AAPL", "TSLA"]
