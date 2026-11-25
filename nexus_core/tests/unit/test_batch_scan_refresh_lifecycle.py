"""批次雷達更新按鈕：重算判定、View 生命週期、換頁／錯誤保留與 GEX 即時抓取。"""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.unified_terminal import batch_scan_view as bsv
from cogs.unified_terminal.batch_scan import ScanOutcome
from cogs.unified_terminal.batch_scan_view import (
    BatchScanPaginatedView,
    BatchScanRefreshButton,
    BatchScanWarningButton,
)
from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_view import UnifiedRadarView

_IDLE_LABEL = "☑️ 暫無可更新項目"
_ALL_FRESH_LABEL = "✅ 資料皆在 30 分鐘內"


@pytest.fixture(autouse=True)
def live_gex() -> Any:
    """重算流程可能即時抓取 GEX，測試一律不連網。"""
    with patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
    ) as mocked:
        yield mocked


def _res(sym: str, age: Any = 10, **extra: Any) -> dict[str, Any]:
    return {"symbol": sym, "_freshness": {"radar_age_seconds": age}, **extra}


def _noted(sym: str, age: Any = 10) -> dict[str, Any]:
    """快照新鮮但 UOA 過期（非盤前 🕓）。"""
    return _res(sym, age, uoa_age_seconds=7200.0)


def _benign(sym: str) -> dict[str, Any]:
    """盤前良性過期（🌙）。"""
    return _res(sym, 10, uoa_age_seconds=7200.0, iv_metrics={"is_premarket": True})


def _gex_stale(sym: str, age: Any = 10) -> dict[str, Any]:
    return _res(sym, age, gex_metrics={"_is_stale_cache": True})


def _state() -> dict[str, Any]:
    return {"scope": "HOLDINGS", "quant_filters": [], "params": {}}


def _view(
    results: list[dict[str, Any]], cog: Any = None, pages: int = 1, **kw: Any
) -> BatchScanPaginatedView:
    return BatchScanPaginatedView(
        [discord.Embed(title=f"p{i}") for i in range(pages)],
        cog or MagicMock(),
        MagicMock(),
        total_items=len(results),
        scan_results=results,
        state=_state(),
        user_id=12345,
        **kw,
    )


def _btn(view: Any) -> BatchScanRefreshButton:
    return next(c for c in view.children if isinstance(c, BatchScanRefreshButton))


def _warn(view: Any) -> BatchScanWarningButton:
    return next(c for c in view.children if isinstance(c, BatchScanWarningButton))


def _interaction() -> Any:
    it = AsyncMock()
    it.user.id = 12345
    it.response = MagicMock()
    it.response.edit_message = AsyncMock()
    it.response.send_message = AsyncMock()
    it.response.defer = AsyncMock()
    it.followup.send = AsyncMock()
    it.edit_original_response = AsyncMock()
    return it


def _cog(outcome: Any = None, slow: Any = None) -> Any:
    cog = MagicMock()
    cog._fetch_sym_radar_data_slow = slow or AsyncMock()
    cog._run_unified_scan = AsyncMock(return_value=outcome or _outcome([_res("A")]))
    return cog


def _outcome(results: list[dict[str, Any]], pages: int = 1) -> ScanOutcome:
    return ScanOutcome(
        results=results,
        embeds=[discord.Embed(title=f"new{i}") for i in range(pages)],
    )


async def _click(view: Any, it: Any) -> Any:
    """點擊更新按鈕，回傳最後換上的 View。"""
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await _btn(view).callback(it)
    return it.edit_original_response.await_args.kwargs["view"]


# --- 判定 --------------------------------------------------------------------


def test_noted_row_counted_but_benign_premarket_is_not() -> None:
    view = _view([_noted("A"), _benign("B"), _res("C")])
    assert view.stale_symbols() == ["A"]
    assert _btn(view).label == "🔄 更新逾 30 分鐘標的 (1)"
    assert _btn(view).disabled is False


def test_only_benign_premarket_shows_idle_label() -> None:
    view = _view([_benign("B")])
    assert view.stale_symbols() == []
    assert _btn(view).label == _IDLE_LABEL
    assert _btn(view).disabled is True


def test_recently_refreshed_skips_noted_row_but_not_old_snapshot() -> None:
    now = time.monotonic()
    view = _view(
        [_noted("A"), _noted("B", 7200), _noted("C")],
        recently_refreshed={
            "A": now,
            "B": now,
            "C": now - bsv.RADAR_REFRESH_STALE_SECONDS - 1,
        },
    )
    assert view.stale_symbols() == ["B", "C"]


@pytest.mark.asyncio
async def test_failed_symbols_listed_first_and_refreshed() -> None:
    cog = _cog()
    view = _view([_res("B", 7200), _res("A", 7200)], cog, failed_symbols=["X", "A"])
    assert view.stale_symbols() == ["X", "A", "B"]
    await _click(view, _interaction())
    awaited = [c.args[0] for c in cog._fetch_sym_radar_data_slow.await_args_list]
    assert sorted(awaited) == ["A", "B", "X"]


@pytest.mark.asyncio
async def test_count_grows_with_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view([_res("A", 25 * 60), _res("B")], pages=2)
    assert _btn(view).label == _ALL_FRESH_LABEL
    later = time.monotonic() + 600
    monkeypatch.setattr(bsv, "time", SimpleNamespace(monotonic=lambda: later))
    await view.btn_next.callback(_interaction())
    assert view.stale_symbols() == ["A"]
    assert _btn(view).label == "🔄 更新逾 30 分鐘標的 (1)"
    assert _btn(view).disabled is False


# --- 換頁、錯誤與回報 --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("new_pages,expected", [(3, 2), (2, 1)])
async def test_success_keeps_page_and_clamps(new_pages: int, expected: int) -> None:
    cog = _cog(_outcome([_res("A")], pages=new_pages))
    view = _view([_res("A", None)], cog, pages=3)
    view.current_page = 2
    it = _interaction()
    new_view = await _click(view, it)
    assert new_view.current_page == expected
    embed = it.edit_original_response.await_args.kwargs["embed"]
    assert embed.title == f"new{expected}"
    assert new_view.btn_next.disabled is True
    assert new_view.last_interaction is it
    it.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_error_embed_keeps_panel_without_refresh_button() -> None:
    error = discord.Embed(title="無結果")
    cog = _cog(ScanOutcome(error_embed=error))
    view = _view([_res("A", None)], cog)
    it = _interaction()
    new_view = await _click(view, it)
    assert new_view is not None
    assert new_view is not view
    assert it.edit_original_response.await_args.kwargs["embed"] is error
    labels = [getattr(c, "label", None) for c in new_view.children]
    assert "🔄 返回控制面板" in labels
    assert not any(isinstance(c, BatchScanRefreshButton) for c in new_view.children)
    assert new_view.state == view.state
    assert new_view.last_interaction is it


@pytest.mark.asyncio
async def test_failed_and_timed_out_symbols_in_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bsv, "_REFRESH_BUDGET_SECONDS", 0.05)
    release = asyncio.Event()

    async def slow(sym: str, force_refresh: bool = False) -> None:
        if sym == "A":
            raise RuntimeError("boom")
        if sym == "B":
            await release.wait()

    rows = [_res(s, None) for s in ("A", "B", "C")]
    cog = _cog(_outcome([_res("A")]), slow)
    view = _view(rows, cog)
    it = _interaction()
    new_view = await _click(view, it)
    release.set()
    await asyncio.sleep(0)

    embed = it.edit_original_response.await_args.kwargs["embed"]
    assert "⚠️ 更新失敗: A, B" in embed.footer.text
    assert new_view.refresh_failed_symbols == ["A", "B"]
    assert set(new_view.recently_refreshed) == {"C"}
    it.followup.send.assert_not_called()


def test_footer_caps_refresh_failed_list() -> None:
    view = _view([_res("A")], refresh_failed_symbols=[f"S{i}" for i in range(12)])
    text = view.embeds[0].footer.text
    assert text is not None
    assert "⚠️ 更新失敗: S0" in text
    assert "S10" not in text
    assert "等 12 檔" in text


@pytest.mark.asyncio
async def test_rescan_error_carries_refresh_results_and_scan_age() -> None:
    async def slow(sym: str, force_refresh: bool = False) -> None:
        if sym == "B":
            raise RuntimeError("boom")

    cog = _cog(slow=slow)
    cog._run_unified_scan = AsyncMock(side_effect=RuntimeError("x"))
    now = time.monotonic()
    view = _view(
        [_noted("A"), _noted("B")],
        cog,
        recently_refreshed={"OLD": now, "GONE": now - 7200},
    )
    view._created_at -= 100
    it = _interaction()
    new_view = await _click(view, it)
    assert set(new_view.recently_refreshed) == {"A", "OLD"}
    assert new_view.refresh_failed_symbols == ["B"]
    assert new_view._created_at == view._created_at
    assert new_view.stale_symbols() == ["B"]
    assert new_view.last_interaction is it
    it.followup.send.assert_awaited_once()


# --- 生命週期 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_view_finished_after_refresh_click() -> None:
    view = _view([_res("A", None)], _cog())
    new_view = await _click(view, _interaction())
    assert view.is_finished() is True
    assert new_view.is_finished() is False


@pytest.mark.asyncio
async def test_on_timeout_prefers_last_interaction() -> None:
    view = _view([_res("A")])
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    it = _interaction()
    await view.btn_next.callback(it)
    await view.on_timeout()
    it.edit_original_response.assert_awaited_once_with(view=None)
    assert view.last_interaction is it
    view.message.edit.assert_not_awaited()
    assert view.children == []


@pytest.mark.asyncio
async def test_on_timeout_falls_back_to_message_and_swallows_error() -> None:
    view = _view([_res("A")])
    message = MagicMock()
    message.edit = AsyncMock(side_effect=RuntimeError("gone"))
    view.message = message
    await view.on_timeout()
    message.edit.assert_awaited_once_with(view=None)


@pytest.mark.asyncio
async def test_initial_scan_stores_message_on_view() -> None:
    cog = UnifiedTerminalCog(MagicMock())
    cog._run_unified_scan = AsyncMock(  # type: ignore[method-assign]
        return_value=_outcome([_res("A")])
    )
    it = _interaction()
    sent = MagicMock()
    it.followup.send = AsyncMock(return_value=sent)
    await cog.execute_unified_scan(it, _state(), 12345)
    assert it.followup.send.await_args.kwargs["view"].message is sent


@pytest.mark.asyncio
async def test_return_panel_stops_pager_so_it_never_edits_again() -> None:
    view = BatchScanPaginatedView(
        [discord.Embed(title="p")], MagicMock(), MagicMock(), timeout=0.02
    )
    message = MagicMock()
    message.edit = AsyncMock()
    view.message = message
    view._start_listening_from_store(MagicMock())
    it = _interaction()
    await view.btn_return_panel.callback(it)
    assert view.is_finished() is True
    await asyncio.sleep(0.05)
    message.edit.assert_not_awaited()
    it.edit_original_response.assert_not_awaited()


async def _run_warning(view: BatchScanPaginatedView, during: Any) -> Any:
    async def hub(*args: Any) -> None:
        await during()

    view.cog._run_warning_symbols_hub = hub
    it = _interaction()
    embed = discord.Embed(title="x")
    embed.add_field(name="💡 即時聯動警示", value="```ansi\nAAPL\n• x\n```")
    it.message = MagicMock()
    it.message.embeds = [embed]
    await _warn(view).callback(it)
    return it


@pytest.mark.asyncio
async def test_warning_button_does_not_reenable_finished_view() -> None:
    view = _view([_res("A", None)], pages=2)
    children = list(view.children)

    async def expire() -> None:
        view.stop()

    it = await _run_warning(view, expire)
    assert all(getattr(c, "disabled") for c in children)
    it.edit_original_response.assert_awaited_once_with(view=None)
    assert view.last_interaction is it


@pytest.mark.asyncio
async def test_warning_button_leaves_replaced_message_alone() -> None:
    view = _view([_res("A", None)])

    async def go_back() -> None:
        await view.btn_return_panel.callback(_interaction())

    it = await _run_warning(view, go_back)
    it.edit_original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_panel_edit_failure_keeps_pager_alive() -> None:
    view = _view([_res("A")])
    message = MagicMock()
    message.edit = AsyncMock()
    view.message = message
    view._start_listening_from_store(MagicMock())
    it = _interaction()
    it.response.edit_message = AsyncMock(side_effect=RuntimeError("discord down"))
    with pytest.raises(RuntimeError):
        await view.btn_return_panel.callback(it)
    assert view.is_finished() is False
    assert view.superseded is False
    await view.on_timeout()
    message.edit.assert_awaited_once_with(view=None)
    view.stop()


@pytest.mark.asyncio
async def test_double_click_refreshes_only_once() -> None:
    cog = _cog()
    view = _view([_res("A", None)], cog)
    button = _btn(view)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_edit(**kwargs: Any) -> None:
        entered.set()
        await release.wait()

    first = _interaction()
    first.response.edit_message = AsyncMock(side_effect=blocked_edit)
    second = _interaction()
    with patch("services.llm_service.is_memory_safe", return_value=True):
        task = asyncio.create_task(button.callback(first))
        await entered.wait()
        await button.callback(second)
        release.set()
        await task

    second.response.defer.assert_awaited_once_with()
    second.response.edit_message.assert_not_awaited()
    second.edit_original_response.assert_not_awaited()
    first.response.edit_message.assert_awaited_once()
    first.edit_original_response.assert_awaited_once()
    cog._fetch_sym_radar_data_slow.assert_awaited_once()
    cog._run_unified_scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_on_finished_view_only_defers() -> None:
    cog = _cog()
    view = _view([_res("A", None)], cog)
    button = _btn(view)
    view.stop()
    it = _interaction()
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await button.callback(it)
    it.response.defer.assert_awaited_once_with()
    it.response.edit_message.assert_not_awaited()
    cog._fetch_sym_radar_data_slow.assert_not_awaited()
    cog._run_unified_scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_edit_failure_allows_retry() -> None:
    cog = _cog()
    view = _view([_res("A", None)], cog, pages=2)
    button = _btn(view)
    before = [getattr(c, "disabled") for c in view.children]
    label = button.label
    failing = _interaction()
    failing.response.edit_message = AsyncMock(side_effect=RuntimeError("x"))
    with patch("services.llm_service.is_memory_safe", return_value=True):
        with pytest.raises(RuntimeError):
            await button.callback(failing)

    assert view.refresh_in_progress is False
    assert view.is_finished() is False
    assert [getattr(c, "disabled") for c in view.children] == before
    assert button.label == label
    cog._fetch_sym_radar_data_slow.assert_not_awaited()

    retry = _interaction()
    new_view = await _click(view, retry)
    assert new_view is not view
    retry.response.defer.assert_not_awaited()
    cog._fetch_sym_radar_data_slow.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_refresh_task_counted_as_failed() -> None:
    async def slow(sym: str, force_refresh: bool = False) -> None:
        if sym == "B":
            raise asyncio.CancelledError

    cog = _cog(slow=slow)
    view = _view([_res("A", None), _res("B", None)], cog)
    it = _interaction()
    new_view = await _click(view, it)
    assert new_view.refresh_failed_symbols == ["B"]
    assert set(new_view.recently_refreshed) == {"A"}
    cog._run_unified_scan.assert_awaited_once()
    embed = it.edit_original_response.await_args.kwargs["embed"]
    assert "⚠️ 更新失敗: B" in embed.footer.text
    it.followup.send.assert_not_called()


# --- 按鈕交錯 ----------------------------------------------------------------


@asynccontextmanager
async def _edit_in_flight(
    callback: Any, error: Optional[Exception] = None
) -> AsyncIterator[Any]:
    """讓 callback 停在第一次 edit_message；離開區塊時放行（可指定該次編輯失敗）。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_edit(**kwargs: Any) -> None:
        entered.set()
        await release.wait()
        if error is not None:
            raise error

    it = _interaction()
    it.response.edit_message = AsyncMock(side_effect=blocked_edit)
    with patch("services.llm_service.is_memory_safe", return_value=True):
        task = asyncio.create_task(callback(it))
        await entered.wait()
        try:
            yield it
        finally:
            release.set()
            if error is None:
                await task
            else:
                with pytest.raises(type(error)):
                    await task


def _assert_only_deferred(it: Any) -> None:
    it.response.defer.assert_awaited_once_with()
    it.response.edit_message.assert_not_awaited()
    it.response.send_message.assert_not_awaited()
    it.edit_original_response.assert_not_awaited()
    it.followup.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("button_name", ["btn_prev", "btn_next"])
async def test_pager_ignored_during_return_panel(button_name: str) -> None:
    view = _view([_res("A")], pages=3)
    view.current_page = 1
    view._update_button_states()
    async with _edit_in_flight(view.btn_return_panel.callback):
        click = _interaction()
        await getattr(view, button_name).callback(click)
        _assert_only_deferred(click)
        assert view.current_page == 1
        # 連點返回面板同樣只回應互動
        again = _interaction()
        await view.btn_return_panel.callback(again)
        _assert_only_deferred(again)
    assert view.is_finished() is True


@pytest.mark.asyncio
async def test_refresh_ignored_during_return_panel() -> None:
    cog = _cog()
    view = _view([_res("A", None)], cog)
    async with _edit_in_flight(view.btn_return_panel.callback):
        click = _interaction()
        await _btn(view).callback(click)
        _assert_only_deferred(click)
        assert view.refresh_in_progress is False
    cog._fetch_sym_radar_data_slow.assert_not_awaited()
    cog._run_unified_scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_warning_ignored_during_return_panel() -> None:
    cog = _cog()
    cog._run_warning_symbols_hub = AsyncMock()
    view = _view([_res("A", None)], cog, pages=2)
    async with _edit_in_flight(view.btn_return_panel.callback):
        before = [getattr(c, "disabled") for c in view.children]
        click = _interaction()
        click.message = MagicMock()
        click.message.embeds = [discord.Embed(title="x")]
        await _warn(view).callback(click)
        _assert_only_deferred(click)
        assert [getattr(c, "disabled") for c in view.children] == before
    cog._run_warning_symbols_hub.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_panel_during_refresh_is_ignored() -> None:
    cog = _cog()
    view = _view([_res("A", None)], cog)
    async with _edit_in_flight(_btn(view).callback) as first:
        click = _interaction()
        await view.btn_return_panel.callback(click)
        _assert_only_deferred(click)
        assert view.returning_to_panel is False
    first.edit_original_response.assert_awaited_once()
    cog._fetch_sym_radar_data_slow.assert_awaited_once()
    cog._run_unified_scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_return_panel_edit_failure_restores_pager() -> None:
    view = _view([_res("A")], pages=2)
    async with _edit_in_flight(
        view.btn_return_panel.callback, error=RuntimeError("discord down")
    ):
        pass
    assert view.returning_to_panel is False
    assert view.is_finished() is False
    click = _interaction()
    await view.btn_next.callback(click)
    click.response.defer.assert_not_awaited()
    click.response.edit_message.assert_awaited_once_with(
        embed=view.embeds[1], view=view
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("button_name", ["btn_prev", "btn_next"])
async def test_pager_ignored_during_refresh_first_edit(button_name: str) -> None:
    view = _view([_res("A", None)], _cog(_outcome([_res("A")], pages=3)), pages=3)
    view.current_page = 1
    view._update_button_states()
    async with _edit_in_flight(_btn(view).callback):
        click = _interaction()
        await getattr(view, button_name).callback(click)
        _assert_only_deferred(click)
        assert view.current_page == 1
        assert all(getattr(c, "disabled") for c in view.children)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["return_panel", "refresh"])
@pytest.mark.parametrize("fails", [False, True])
async def test_warning_finally_waits_for_pending_transition(
    transition: str, fails: bool
) -> None:
    """警示分析在換畫面編輯在途時結束：成功則不得再編輯訊息，失敗才恢復按鈕。"""
    view = _view([_res("A", None)], _cog(), pages=2)
    callback = (
        view.btn_return_panel.callback
        if transition == "return_panel"
        else _btn(view).callback
    )
    hub_entered = asyncio.Event()
    hub_release = asyncio.Event()

    async def during() -> None:
        hub_entered.set()
        await hub_release.wait()

    warning = asyncio.create_task(_run_warning(view, during))
    await hub_entered.wait()
    error = RuntimeError("discord down") if fails else None
    async with _edit_in_flight(callback, error=error):
        hub_release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert warning.done() is False

    it = await warning
    if fails:
        it.edit_original_response.assert_awaited_once_with(view=view)
        assert view.btn_next.disabled is False
        assert _warn(view).disabled is False
        assert view.btn_return_panel.disabled is False
    else:
        it.edit_original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_settled_survives_pre_try_error() -> None:
    view = _view([_res("A", None)], _cog())
    with (
        patch("services.llm_service.is_memory_safe", return_value=True),
        patch(
            "services.market_data_service.mark_interactive_request",
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(RuntimeError):
            await _btn(view).callback(_interaction())
    assert view.transition_settled.is_set() is True


# --- 控制面板逾時 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_radar_panel_timeout_removes_buttons_via_last_interaction() -> None:
    panel = UnifiedRadarView(MagicMock(), 12345)
    message = MagicMock()
    message.edit = AsyncMock()
    panel.message = message
    it = _interaction()
    it.response.is_done = MagicMock(return_value=False)
    await panel.update_state_message(it)
    stored: Any = panel.last_interaction
    assert stored is it
    await panel.on_timeout()
    it.edit_original_response.assert_awaited_once_with(view=None)
    message.edit.assert_not_awaited()
    assert panel.children == []


@pytest.mark.asyncio
async def test_radar_panel_timeout_falls_back_to_message() -> None:
    panel = UnifiedRadarView(MagicMock(), 12345)
    message = MagicMock()
    message.edit = AsyncMock(side_effect=RuntimeError("gone"))
    panel.message = message
    await panel.on_timeout()
    message.edit.assert_awaited_once_with(view=None)

    orphan = UnifiedRadarView(MagicMock(), 12345)
    await orphan.on_timeout()
    assert orphan.children == []


@pytest.mark.asyncio
async def test_return_panel_hands_interaction_to_panel() -> None:
    it = _interaction()
    await _view([_res("A")]).btn_return_panel.callback(it)
    panel = it.response.edit_message.await_args.kwargs["view"]
    assert panel.last_interaction is it

    failing = _interaction()
    failing.response.edit_message = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError):
        await _view([_res("A")]).btn_return_panel.callback(failing)
    orphan = failing.response.edit_message.await_args.kwargs["view"]
    assert orphan.last_interaction is None
    assert orphan.message is None


@pytest.mark.asyncio
async def test_panel_open_stores_message_on_view() -> None:
    cog = UnifiedTerminalCog(MagicMock())
    it = _interaction()
    sent = MagicMock()
    it.followup.send = AsyncMock(return_value=sent)
    callback: Any = cog.symbol_hub.callback
    await callback(cog, it, symbol=None, scan_type=None, tag=None, squeeze=None)
    kwargs = it.followup.send.await_args.kwargs
    assert kwargs["wait"] is True
    assert kwargs["view"].message is sent


# --- GEX 即時抓取 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_gex_stale_rows_fetched_live_before_slow_path(live_gex: Any) -> None:
    calls: list[tuple[str, str]] = []

    async def gex(sym: str, force_live: bool = False) -> dict[str, Any]:
        assert force_live is True
        await asyncio.sleep(0)
        calls.append(("gex", sym))
        if sym == "BOOM":
            raise RuntimeError("edge down")
        return {}

    async def slow(sym: str, force_refresh: bool = False) -> None:
        calls.append(("slow", sym))

    live_gex.side_effect = gex
    aged = _res("AGED", 7200)
    aged["_freshness"]["gex_age_seconds"] = float(bsv.RADAR_REFRESH_STALE_SECONDS)
    fresh = _res("FRESH", 7200)
    fresh["_freshness"]["gex_age_seconds"] = 10.0
    cog = _cog(slow=slow)
    view = _view(
        [_gex_stale("NOTE"), aged, _gex_stale("BOOM"), fresh],
        cog,
        failed_symbols=["MISS"],
    )
    new_view = await _click(view, _interaction())

    assert sorted(s for kind, s in calls if kind == "gex") == ["AGED", "BOOM", "NOTE"]
    for sym in ("AGED", "BOOM", "NOTE"):
        assert calls.index(("gex", sym)) < calls.index(("slow", sym))
    assert ("slow", "FRESH") in calls
    assert ("slow", "MISS") in calls
    # GEX 抓取失敗不算更新失敗
    assert new_view.refresh_failed_symbols == []


@pytest.mark.asyncio
async def test_only_gex_stale_after_refresh_shows_idle_label() -> None:
    cog = _cog(_outcome([_gex_stale("A")]))
    view = _view([_gex_stale("A")], cog)
    new_view = await _click(view, _interaction())
    assert new_view.stale_symbols() == []
    assert _btn(new_view).label == _IDLE_LABEL
    assert _btn(new_view).disabled is True
