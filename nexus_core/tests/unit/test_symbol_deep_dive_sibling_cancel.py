"""深度分析子任務收尾：任一失敗或外層被取消時，兄弟任務全部取消並取回例外。"""

import asyncio
import gc
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin
from tests.unit.network_guard import NetworkGuard

_MODULE = "cogs.unified_terminal.symbol_deep_dive"
_MARKET = "services.market_data_service"
_SENTIMENT = "market_analysis.sentiment_engine.SentimentEngine"
# 被測程式有缺陷（卡住）時的保險上限；正常情況全程在記憶體內瞬間完成。
_CALL_TIMEOUT_S = 0.8


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


class _DD(SymbolDeepDiveMixin):
    def __init__(self) -> None:
        self.bot = MagicMock()
        self.bot.polymarket_service = None


class _Blocker:
    """永遠阻塞的 fake：記錄開始與取消，取消後照原樣往上拋。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("阻塞 fake 不應自行返回")


def _expiry() -> str:
    return (date.today() + timedelta(days=15)).strftime("%Y-%m-%d")


def _defaults() -> dict[str, tuple[str, Any]]:
    """名稱 → (patch 目標, 成功路徑回傳值)。"""
    return {
        "expiries": (f"{_MARKET}.get_all_option_expiries", [_expiry()]),
        "spy": (f"{_MARKET}.get_spy_history_df", pd.DataFrame({"Close": [500.0]})),
        "macro": (f"{_MARKET}.get_macro_environment", {"vix": 15.0}),
        "quote": (f"{_MARKET}.get_quote", {"c": 120.0}),
        "history": (f"{_MARKET}.get_history_df", pd.DataFrame({"Close": [105.0]})),
        "gex": (
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            {"put_wall": 100.0},
        ),
        "volume_profile": (f"{_MODULE}._throttled_volume_profile", {"hvn": 110.0}),
        "atr_15m": (f"{_MODULE}.fetch_atr_15m", 1.5),
        "session_stats": (f"{_MODULE}.fetch_session_stats", MagicMock(vwap=119.5)),
        "bar_15m": (f"{_MODULE}.get_confirmed_15m_bar", {"close": 120.5}),
        "catalysts": (
            "services.calendar_service.calendar_service.get_symbol_catalysts",
            ["財報"],
        ),
        "reddit": (
            "services.reddit_service.get_reddit_details",
            ("看多情緒高漲", ["貼文"]),
        ),
        "ddp": (
            "market_analysis.ddp_inspector.DDPInspector.inspect_symbol",
            {"is_ddp": False},
        ),
        "skew": (f"{_SENTIMENT}.calculate_skew", {"skew": 1.0}),
        "pcr": (f"{_SENTIMENT}.calculate_pcr", {"pcr": 0.9}),
        "uoa": (f"{_SENTIMENT}.detect_uoa_with_physical_caps", (["uoa"], [130.0])),
        "max_pain": (f"{_SENTIMENT}.calculate_max_pain", {"max_pain": 115.0}),
        "iv": (f"{_SENTIMENT}.fetch_and_calculate_iv_metrics", {"iv_rank": 35.0}),
        "unified_max_pain": (
            f"{_SENTIMENT}.get_unified_max_pain",
            {"max_pain": 116.0, "distance_pct": 1.25},
        ),
    }


@contextmanager
def _patched(**fakes: Any) -> Iterator[None]:
    """把深度分析的全部相依換成離線 fake；`fakes` 可依名稱覆寫個別項目。

    `_evaluate_squeeze_for_panel` 只有在傳入 `squeeze` 時才替換，否則走真實實作
    （它會等待 quote 任務）。
    """
    defaults = _defaults()
    unknown = set(fakes) - set(defaults) - {"squeeze"}
    assert not unknown, f"未知的 fake 名稱: {unknown}"
    with pytest.MonkeyPatch.context() as mp:
        for name, (target, value) in defaults.items():
            mp.setattr(target, fakes.get(name, AsyncMock(return_value=value)))
        if "squeeze" in fakes:
            mp.setattr(f"{_MODULE}._evaluate_squeeze_for_panel", fakes["squeeze"])
        mp.setattr(f"{_MODULE}.last_chain_oi_missing", MagicMock(return_value=True))
        yield


async def _fetch(dd: _DD) -> dict[str, Any]:
    async with asyncio.timeout(_CALL_TIMEOUT_S):
        return await dd._fetch_single_symbol_data_raw("NVDA")


def _leftover(before: "set[asyncio.Task[Any]]") -> "set[asyncio.Task[Any]]":
    return asyncio.all_tasks() - before


async def _drain(before: "set[asyncio.Task[Any]]") -> None:
    """測試失敗時清掉殘留任務，避免污染後續測試。"""
    tasks = _leftover(before)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=_CALL_TIMEOUT_S)


def _fails_on_cancel(
    started: asyncio.Event, error: RuntimeError, raised: list[BaseException]
) -> Any:
    """阻塞 fake：被取消時改拋 `error`，並記錄到 `raised`。"""

    async def _fake(*args: Any, **kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raised.append(error)
            raise error

    return _fake


@pytest.mark.asyncio
async def test_sibling_failure_cancels_pending_and_reraises() -> None:
    boom = RuntimeError("SPY 歷史抓取失敗")
    reddit, expiries, quote = _Blocker(), _Blocker(), _Blocker()

    async def _spy_fails(*args: Any, **kwargs: Any) -> Any:
        for blocker in (reddit, expiries, quote):
            await blocker.started.wait()
        raise boom

    before = asyncio.all_tasks()
    try:
        with _patched(
            spy=_spy_fails, reddit=reddit, expiries=expiries, quote=quote
        ):  # 擠壓評估走真實實作，卡在等待 quote
            with pytest.raises(RuntimeError) as excinfo:
                await _fetch(_DD())

            assert excinfo.value is boom
            assert reddit.cancelled, "reddit 任務未被取消"
            assert expiries.cancelled, "到期日任務未被取消"
            assert quote.cancelled, "quote 任務未被取消"
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"
    finally:
        await _drain(before)


@pytest.mark.asyncio
async def test_multiple_failures_no_unretrieved_exception() -> None:
    first = RuntimeError("第一個失敗：SPY 歷史")
    second = RuntimeError("第二個失敗：GEX 於收尾時拋錯")
    third = RuntimeError("第三個失敗：到期日於收尾時拋錯")
    gex_started, expiries_started = asyncio.Event(), asyncio.Event()
    raised_on_cancel: list[BaseException] = []

    async def _spy_fails(*args: Any, **kwargs: Any) -> Any:
        await gex_started.wait()
        await expiries_started.wait()
        raise first

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    captured: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    before = asyncio.all_tasks()
    try:
        with _patched(
            spy=_spy_fails,
            gex=_fails_on_cancel(gex_started, second, raised_on_cancel),
            # 到期日任務不在 gather 清單內，另行涵蓋其收尾路徑。
            expiries=_fails_on_cancel(expiries_started, third, raised_on_cancel),
            squeeze=AsyncMock(return_value=None),
        ):
            caught: BaseException | None = None
            try:
                await _fetch(_DD())
            except RuntimeError as exc:
                caught = exc

            assert caught is first, f"拋出的不是第一個失敗的原例外: {caught!r}"
            assert set(raised_on_cancel) == {second, third}
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"

        # traceback 會經由框架持有子任務；先斷開引用，GC 才能回收並觸發回報。
        caught = None
        for error in (first, second, third):
            error.__traceback__ = None
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

        unretrieved = [
            c for c in captured if "exception was never retrieved" in c["message"]
        ]
        assert not unretrieved, f"有未取回的例外: {unretrieved}"
    finally:
        await _drain(before)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_outer_cancel_cancels_all_siblings() -> None:
    names = ("expiries", "spy", "quote", "reddit", "gex", "uoa", "iv")
    blockers = {name: _Blocker() for name in names}

    before = asyncio.all_tasks()
    try:
        # 情境一：所有阻塞 fake 都已開始後取消外層任務。
        with _patched(**blockers):
            outer = asyncio.create_task(_DD()._fetch_single_symbol_data_raw("NVDA"))
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                for blocker in blockers.values():
                    await blocker.started.wait()
            outer.cancel()
            done, _ = await asyncio.wait({outer}, timeout=_CALL_TIMEOUT_S)

            assert outer in done and outer.cancelled()
            not_cancelled = [n for n, b in blockers.items() if not b.cancelled]
            assert not not_cancelled, f"未被取消的任務: {not_cancelled}"
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"

        # 情境二：子任務都還沒開始執行就取消。此時沒有任何任務在等待到期日任務，
        # 取消無法經由 gather 傳到它，必須由收尾邏輯處理。
        expiries = _Blocker()
        with _patched(expiries=expiries, squeeze=AsyncMock(return_value=None)):
            outer = asyncio.create_task(_DD()._fetch_single_symbol_data_raw("NVDA"))
            await asyncio.sleep(0)
            assert not expiries.started.is_set()
            outer.cancel()
            done, _ = await asyncio.wait({outer}, timeout=_CALL_TIMEOUT_S)

            assert outer in done and outer.cancelled()
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"
    finally:
        await _drain(before)


@pytest.mark.asyncio
async def test_setup_failure_cancels_created_tasks() -> None:
    boom = RuntimeError("建立任務中途失敗")

    before = asyncio.all_tasks()
    try:
        with _patched(session_stats=MagicMock(side_effect=boom)):
            with pytest.raises(RuntimeError) as excinfo:
                await _fetch(_DD())

            assert excinfo.value is boom
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"
    finally:
        await _drain(before)


@pytest.mark.asyncio
async def test_setup_failure_child_raising_on_cancel_is_retrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """建立任務中途失敗時，被取消才拋錯的子任務例外由收尾回呼取回並記錄。

    此時 gather 尚未建立，回呼是唯一的取回者。預設的惰性任務工廠下，所有子任務
    建立完之前沒有任何 `await`，子任務尚未開始執行就被取消，不可能在取消時拋錯；
    改用 `asyncio.eager_task_factory`，子任務在 `create_task` 內就先跑到第一個
    `await`，才能重現這條路徑。
    """
    boom = RuntimeError("建立任務中途失敗")
    second = RuntimeError("GEX 於收尾時拋錯")
    gex_started = asyncio.Event()
    raised_on_cancel: list[BaseException] = []

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()
    captured: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    before = asyncio.all_tasks()
    try:
        loop.set_task_factory(asyncio.eager_task_factory)
        with (
            caplog.at_level(logging.DEBUG, logger=_MODULE),
            _patched(
                gex=_fails_on_cancel(gex_started, second, raised_on_cancel),
                session_stats=MagicMock(side_effect=boom),
                squeeze=AsyncMock(return_value=None),
            ),
        ):
            caught: BaseException | None = None
            try:
                await _fetch(_DD())
            except RuntimeError as exc:
                caught = exc

            assert caught is boom, f"拋出的不是建立任務時的原例外: {caught!r}"
            assert gex_started.is_set(), "GEX 任務未在建立時開始執行"
            assert raised_on_cancel == [second]
            assert not _leftover(before), f"仍有殘留任務: {_leftover(before)}"
        loop.set_task_factory(previous_factory)

        # traceback 會經由框架持有子任務；先斷開引用，GC 才能回收並觸發回報。
        caught = None
        for error in (boom, second):
            error.__traceback__ = None
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

        unretrieved = [
            c
            for c in captured
            if "exception was never retrieved" in str(c.get("message"))
        ]
        assert not unretrieved, f"GC_UNRETRIEVED={len(unretrieved)}"
        messages = [record.getMessage() for record in caplog.records]
        assert any(repr(second) in m for m in messages), f"未記錄子任務例外: {messages}"
    finally:
        loop.set_task_factory(previous_factory)
        loop.set_exception_handler(previous_handler)
        await _drain(before)


@pytest.mark.asyncio
async def test_success_path_unchanged() -> None:
    defaults = _defaults()
    squeeze_eval = object()
    dd = _DD()
    dd.bot.polymarket_service = MagicMock()
    dd.bot.polymarket_service.get_market_snapshot = AsyncMock(return_value=["盤口"])

    before = asyncio.all_tasks()
    with _patched(
        # 沿用同一組回傳物件，才能以身分比對確認原樣傳遞。
        **{
            name: AsyncMock(return_value=value) for name, (_, value) in defaults.items()
        },
        squeeze=AsyncMock(return_value=squeeze_eval),
    ):
        raw = await _fetch(dd)

    assert not _leftover(before)
    assert set(raw) == {
        "df_spy",
        "macro_raw",
        "quote",
        "skew_data",
        "pcr_data",
        "uoa_data",
        "uoa_oi_missing",
        "sto_physical_cap_strikes",
        "max_pain_data",
        "iv_metrics",
        "reddit_text",
        "reddit_posts",
        "poly_markets",
        "ddp_report",
        "ddp_reason",
        "squeeze_eval",
        "df_hist_1d",
        "df_hist_fetched_at",
        "month_max_pains",
        "option_expiries",
        "gex_profile_data",
        "volume_profile",
        "atr_15m",
        "session_vwap",
        "session_stats",
        "bar_15m",
        "catalysts",
    }
    # fake 回傳的物件原樣傳遞（以身分比對）
    for key, name in (
        ("df_spy", "spy"),
        ("macro_raw", "macro"),
        ("quote", "quote"),
        ("skew_data", "skew"),
        ("pcr_data", "pcr"),
        ("max_pain_data", "max_pain"),
        ("iv_metrics", "iv"),
        ("ddp_report", "ddp"),
        ("df_hist_1d", "history"),
        ("gex_profile_data", "gex"),
        ("volume_profile", "volume_profile"),
        ("atr_15m", "atr_15m"),
        ("session_stats", "session_stats"),
        ("bar_15m", "bar_15m"),
        ("catalysts", "catalysts"),
    ):
        assert raw[key] is defaults[name][1], key
    assert raw["uoa_data"] == ["uoa"]
    assert raw["sto_physical_cap_strikes"] == [130.0]
    assert raw["uoa_oi_missing"] is True
    assert raw["reddit_text"] == "看多情緒高漲"
    assert raw["reddit_posts"] == ["貼文"]
    assert raw["poly_markets"] == ["盤口"]
    assert raw["ddp_reason"] is None
    assert raw["squeeze_eval"] is squeeze_eval
    assert isinstance(raw["df_hist_fetched_at"], datetime)
    assert raw["session_vwap"] == 119.5
    assert raw["option_expiries"] == defaults["expiries"][1]
    assert raw["month_max_pains"] == [
        {
            "expiry": defaults["expiries"][1][0],
            "max_pain": 116.0,
            "distance_pct": 1.25,
            "is_degraded": False,
            "calculation_mode": "OI",
        }
    ]
