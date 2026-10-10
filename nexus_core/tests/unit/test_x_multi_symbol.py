"""/x symbol 多標的（逗號分隔）批次查詢的測試。

涵蓋：代號清單解析、單檔回歸（含 followup wait=True）、多檔分派與輸入閘門、
`_run_multi_symbol_hub` 的組頁／逾時／記憶體閘門、`SymbolBatchHubView` 換頁行為。
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.unified_terminal import symbol_deep_dive
from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.symbol_deep_dive import parse_symbol_list
from cogs.unified_terminal.symbol_view import (
    SymbolBatchHubView,
    SymbolHubPage,
    SymbolHubView,
)


@pytest.fixture
def interaction() -> Any:
    inter = MagicMock()
    inter.user.id = 4242
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.response.is_done.return_value = True
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    return inter


@pytest.fixture
def cog() -> Any:
    bot = MagicMock(spec=[])  # 無 memory_manager，跳過預熱 hook
    return UnifiedTerminalCog(bot)


def _page(sym: str, ok: bool = True) -> SymbolHubPage:
    return SymbolHubPage(
        sym,
        {"symbol": sym, "marker": f"data-{sym}"} if ok else None,
        discord.Embed(title=f"page-{sym}", description=None if ok else f"error {sym}"),
    )


def _view(syms: list[str], start: int = 0, fail: tuple[str, ...] = ()) -> Any:
    pages = [_page(s, ok=s not in fail) for s in syms]
    return SymbolBatchHubView(pages, 4242, MagicMock(), start_index=start)


def _btn(view: Any, custom_id: str) -> Any:
    for child in view.children:
        if getattr(child, "custom_id", None) == custom_id:
            return child
    raise AssertionError(custom_id)


# ---------------------------------------------------------------------------
# 1. parse_symbol_list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NVDA,SPCX,TSLA,BE", ["NVDA", "SPCX", "TSLA", "BE"]),
        (" nvda , $tsla ,", ["NVDA", "TSLA"]),
        ("NVDA，TSLA", ["NVDA", "TSLA"]),
        ("NVDA TSLA", ["NVDA", "TSLA"]),
        ("NVDA;TSLA", ["NVDA", "TSLA"]),
        ("NVDA；TSLA", ["NVDA", "TSLA"]),
        ("NVDA、TSLA", ["NVDA", "TSLA"]),
        ("NVDA,TSLA,nvda,$TSLA,BE", ["NVDA", "TSLA", "BE"]),
        (",,", []),
        ("$", []),
        ("brk.b,NVDA", ["BRK.B", "NVDA"]),
    ],
)
def test_parse_symbol_list(raw: str, expected: list[str]) -> None:
    assert parse_symbol_list(raw) == expected


# ---------------------------------------------------------------------------
# 2. 單檔回歸
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["NVDA", "NVDA,"])
async def test_single_symbol_goes_to_single_hub(
    cog: Any, interaction: Any, raw: str
) -> None:
    cog._run_single_symbol_hub = AsyncMock()
    cog._run_multi_symbol_hub = AsyncMock()
    await cog.symbol_hub.callback(cog, interaction, symbol=raw)
    cog._run_single_symbol_hub.assert_awaited_once_with(interaction, "NVDA", 4242)
    cog._run_multi_symbol_hub.assert_not_called()


@pytest.mark.asyncio
async def test_empty_list_rejected_without_validate(cog: Any, interaction: Any) -> None:
    with patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock_validate:
        await cog.symbol_hub.callback(cog, interaction, symbol=",,")
    mock_validate.assert_not_called()
    _, kwargs = interaction.followup.send.call_args
    assert "請輸入有效的股票代號" in (kwargs["embed"].description or "")


@pytest.mark.asyncio
async def test_single_symbol_followup_uses_wait_true(
    cog: Any, interaction: Any
) -> None:
    cog._process_symbol_hub_data = AsyncMock(return_value={"symbol": "NVDA"})
    with patch(
        "services.market_data_service.validate_symbol",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "services.single_flight.SingleFlightManager.run",
        new_callable=AsyncMock,
        return_value={},
    ), patch(
        "cogs.unified_terminal.symbol_deep_dive.create_tactical_symbol_embed",
        return_value=discord.Embed(title="ok"),
    ):
        await cog.symbol_hub.callback(cog, interaction, symbol="NVDA")

    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs["wait"] is True
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], SymbolHubView)


# ---------------------------------------------------------------------------
# 3. 多檔分派
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_symbol_dispatch(cog: Any, interaction: Any) -> None:
    cog._run_single_symbol_hub = AsyncMock()
    cog._run_multi_symbol_hub = AsyncMock()
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog.symbol_hub.callback(cog, interaction, symbol="nvda,tsla")
    cog._run_multi_symbol_hub.assert_awaited_once_with(
        interaction, ["NVDA", "TSLA"], 4242
    )
    cog._run_single_symbol_hub.assert_not_called()


@pytest.mark.asyncio
async def test_multi_symbol_over_limit_rejected(cog: Any, interaction: Any) -> None:
    cog._run_multi_symbol_hub = AsyncMock()
    raw = ",".join(f"S{i}" for i in range(11))
    with patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock_validate:
        await cog.symbol_hub.callback(cog, interaction, symbol=raw)
    mock_validate.assert_not_called()
    cog._run_multi_symbol_hub.assert_not_called()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs["embed"].title.endswith("輸入錯誤")
    assert "最多查詢 10 檔" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_multi_symbol_memory_gate(cog: Any, interaction: Any) -> None:
    cog._run_multi_symbol_hub = AsyncMock()
    with patch("services.llm_service.is_memory_safe", return_value=False):
        await cog.symbol_hub.callback(cog, interaction, symbol="NVDA,TSLA")
    cog._run_multi_symbol_hub.assert_not_called()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs["embed"].title.endswith("資源不足")


@pytest.mark.asyncio
async def test_symbol_with_scan_type_still_rejected(cog: Any, interaction: Any) -> None:
    cog._run_multi_symbol_hub = AsyncMock()
    cog._run_single_symbol_hub = AsyncMock()
    scan = MagicMock()
    scan.value = "HOLDINGS"
    await cog.symbol_hub.callback(cog, interaction, symbol="NVDA,TSLA", scan_type=scan)
    cog._run_multi_symbol_hub.assert_not_called()
    cog._run_single_symbol_hub.assert_not_called()
    _, kwargs = interaction.followup.send.call_args
    assert "只能擇一" in kwargs["embed"].description


# ---------------------------------------------------------------------------
# 4. _run_multi_symbol_hub
# ---------------------------------------------------------------------------


def _patch_builder(
    cog: Any, failing: tuple[str, ...] = (), slow: tuple[str, ...] = ()
) -> None:
    async def _build(sym: str, user_id: int) -> SymbolHubPage:
        if sym in slow:
            await asyncio.sleep(5)
        return _page(sym, ok=sym not in failing)

    cog._build_symbol_hub_page = AsyncMock(side_effect=_build)


@pytest.mark.asyncio
async def test_multi_hub_builds_view_in_input_order(cog: Any, interaction: Any) -> None:
    _patch_builder(cog)
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "SPCX", "TSLA"], 4242)

    interaction.followup.send.assert_not_called()
    _, kwargs = interaction.edit_original_response.call_args
    view = kwargs["view"]
    assert isinstance(view, SymbolBatchHubView)
    assert kwargs["content"] is None
    assert [p.symbol for p in view.pages] == ["NVDA", "SPCX", "TSLA"]
    assert view.last_interaction is interaction


@pytest.mark.asyncio
async def test_multi_hub_reports_progress(cog: Any, interaction: Any) -> None:
    async def _build(sym: str, user_id: int) -> SymbolHubPage:
        if sym == "TSLA":
            await asyncio.sleep(0.05)
        return _page(sym)

    cog._build_symbol_hub_page = AsyncMock(side_effect=_build)
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "TSLA"], 4242)
    contents = [
        c.kwargs.get("content")
        for c in interaction.edit_original_response.call_args_list
    ]
    # 進度在主協程依序送出；最後一則為結果（content=None）
    assert any(c and "分析中 1/2" in c for c in contents)
    assert contents[-1] is None


@pytest.mark.asyncio
async def test_multi_hub_start_index_is_first_success(
    cog: Any, interaction: Any
) -> None:
    _patch_builder(cog, failing=("NVDA", "SPCX"))
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "SPCX", "TSLA"], 4242)
    _, kwargs = interaction.edit_original_response.call_args
    view = kwargs["view"]
    assert view.index == 2
    assert view.symbol == "TSLA"


@pytest.mark.asyncio
async def test_multi_hub_all_failed(cog: Any, interaction: Any) -> None:
    _patch_builder(cog, failing=("NVDA", "TSLA"))
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "TSLA"], 4242)
    _, kwargs = interaction.edit_original_response.call_args
    assert "view" not in kwargs
    assert kwargs["content"] is None
    assert kwargs["embed"].title.endswith("載入失敗")
    desc = kwargs["embed"].description
    # 逐檔列出各自的失敗原因
    assert "• NVDA：" in desc and "• TSLA：" in desc
    assert "error NVDA" in desc and "error TSLA" in desc


@pytest.mark.asyncio
async def test_multi_hub_task_exception_is_error_not_timeout(
    cog: Any, interaction: Any
) -> None:
    async def _build(sym: str, user_id: int) -> SymbolHubPage:
        if sym == "SPCX":
            raise RuntimeError("boom")
        return _page(sym)

    cog._build_symbol_hub_page = AsyncMock(side_effect=_build)
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "SPCX", "TSLA"], 4242)
    _, kwargs = interaction.edit_original_response.call_args
    view = kwargs["view"]
    assert [p.base_data is not None for p in view.pages] == [True, False, True]
    desc = view.pages[1].embed.description or ""
    assert "逾時" not in desc
    assert "載入 `SPCX` 資料時發生錯誤" in desc
    assert "boom" not in desc


@pytest.mark.asyncio
async def test_multi_hub_batch_deadline_cancels_pending(
    cog: Any, interaction: Any
) -> None:
    _patch_builder(cog, slow=("SPCX",))
    with patch("services.llm_service.is_memory_safe", return_value=True), patch.object(
        symbol_deep_dive, "_BATCH_DEADLINE_S", 0.05
    ):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "SPCX"], 4242)
    _, kwargs = interaction.edit_original_response.call_args
    view = kwargs["view"]
    assert view.pages[0].base_data is not None
    assert view.pages[1].base_data is None
    assert "逾時" in (view.pages[1].embed.description or "")


@pytest.mark.asyncio
async def test_multi_hub_per_symbol_memory_gate(cog: Any, interaction: Any) -> None:
    _patch_builder(cog)
    with patch("services.llm_service.is_memory_safe", side_effect=[True, False]):
        await cog._run_multi_symbol_hub(interaction, ["NVDA", "TSLA"], 4242)
    _, kwargs = interaction.edit_original_response.call_args
    view = kwargs["view"]
    assert view.pages[0].base_data is not None
    assert view.pages[1].base_data is None
    assert "記憶體水位過高" in (view.pages[1].embed.description or "")
    # 被略過的標的不應實際抓取
    assert cog._build_symbol_hub_page.await_count == 1


# ---------------------------------------------------------------------------
# 5. SymbolBatchHubView
# ---------------------------------------------------------------------------


def test_view_keeps_inherited_buttons() -> None:
    view = _view(["NVDA", "TSLA"])
    ids = [getattr(c, "custom_id", None) for c in view.children]
    assert ids[0] == "btn_home"
    for cid in ("btn_media", "btn_refresh", "btn_hedge", "btn_entry_rules"):
        assert cid in ids
    assert {"btn_batch_prev", "btn_batch_pos", "btn_batch_next"} <= set(ids)


@pytest.mark.asyncio
async def test_view_navigation_switches_symbol_and_data(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA", "BE"])
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_symbol_embed",
        return_value=discord.Embed(title="rendered"),
    ) as mock_render:
        await view._on_next(interaction)
    assert view.symbol == "TSLA"
    assert view.base_data["marker"] == "data-TSLA"
    mock_render.assert_called_once_with(view.base_data)
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["view"] is view
    assert kwargs["embed"].title == "rendered"
    assert view.last_interaction is interaction

    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_symbol_embed",
        return_value=discord.Embed(title="r2"),
    ):
        await view._on_prev(interaction)
    assert view.symbol == "NVDA"


def test_view_boundary_buttons_and_label() -> None:
    view = _view(["NVDA", "SPCX", "TSLA"])
    assert _btn(view, "btn_batch_prev").disabled is True
    assert _btn(view, "btn_batch_next").disabled is False
    assert _btn(view, "btn_batch_pos").disabled is True
    assert _btn(view, "btn_batch_pos").label == "1/3 · NVDA"

    last = _view(["NVDA", "SPCX", "TSLA"], start=2)
    assert _btn(last, "btn_batch_prev").disabled is False
    assert _btn(last, "btn_batch_next").disabled is True
    assert _btn(last, "btn_batch_pos").label == "3/3 · TSLA"


def test_view_error_page_disables_function_buttons() -> None:
    view = _view(["NVDA", "SPCX", "TSLA"], start=1, fail=("SPCX",))
    assert _btn(view, "btn_batch_pos").label == "⚠️ 2/3 · SPCX"
    for cid in ("btn_home", "btn_media", "btn_refresh", "btn_hedge", "btn_entry_rules"):
        assert _btn(view, cid).disabled is True
    assert _btn(view, "btn_batch_prev").disabled is False
    assert _btn(view, "btn_batch_next").disabled is False

    ok = _view(["NVDA", "SPCX"], fail=("SPCX",))
    assert _btn(ok, "btn_home").disabled is False


@pytest.mark.asyncio
async def test_view_leaving_error_page_does_not_write_back_placeholder(
    interaction: Any,
) -> None:
    view = _view(["NVDA", "SPCX", "TSLA"], start=1, fail=("SPCX",))
    assert view.base_data == {}
    await view._on_next(interaction)
    assert view.pages[1].base_data is None
    assert view.pages[1].symbol == "SPCX"
    assert view.symbol == "TSLA"


@pytest.mark.asyncio
async def test_view_refresh_data_survives_navigation(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    refreshed = {"symbol": "NVDA", "marker": "refreshed"}
    view.base_data = refreshed  # 模擬即時整理寫入新資料
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_symbol_embed",
        return_value=discord.Embed(title="x"),
    ):
        await view._on_next(interaction)
        await view._on_prev(interaction)
    assert view.base_data is refreshed
    assert view.pages[0].base_data is refreshed


@pytest.mark.asyncio
async def test_view_busy_blocks_navigation(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    view._busy = True
    await view._on_next(interaction)
    assert view.index == 0
    assert view.symbol == "NVDA"
    interaction.response.defer.assert_awaited_once()
    interaction.response.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_view_render_failure_falls_back_to_cached_embed(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_symbol_embed",
        side_effect=RuntimeError("boom"),
    ):
        await view._on_next(interaction)
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["embed"] is view.pages[1].embed


@pytest.mark.asyncio
async def test_view_function_button_sets_and_clears_busy(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    seen: list[bool] = []

    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_hedge_embed",
        side_effect=lambda *a, **k: discord.Embed(title="hedge"),
    ):
        # 在 loading 期間 _busy 為 True
        orig = view._set_loading

        async def _spy(i: Any) -> Any:
            r = await orig(i)
            seen.append(view._busy)
            return r

        view._set_loading = _spy
        await view.btn_hedge.callback(interaction)

    assert seen == [True]
    assert view._busy is False
    assert view.last_interaction is interaction


@pytest.mark.asyncio
async def test_view_button_failure_keeps_embed(interaction: Any) -> None:
    """按鈕失敗（embed=None）時不得清空原畫面。"""
    view = _view(["NVDA"])
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_hedge_embed",
        side_effect=RuntimeError("x"),
    ):
        await view.btn_hedge.callback(interaction)
    _, kwargs = interaction.edit_original_response.call_args
    assert "embed" not in kwargs


@pytest.mark.asyncio
async def test_view_on_timeout_removes_buttons(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    view.last_interaction = interaction
    await view.on_timeout()
    interaction.edit_original_response.assert_awaited_once_with(view=None)

    view2 = _view(["NVDA"])
    await view2.on_timeout()  # 無 last_interaction 不應拋例外


def _component_interaction(interaction: Any, custom_id: str) -> Any:
    interaction.data = {"custom_id": custom_id, "component_type": 2}
    return interaction


@pytest.mark.asyncio
async def test_view_interaction_check_marks_busy_before_callback(
    interaction: Any,
) -> None:
    """功能按鈕在派送 callback 前即標記忙碌，關閉 defer→_set_loading 間的換頁空窗。"""
    view = _view(["NVDA", "TSLA"])
    ok = await view.interaction_check(_component_interaction(interaction, "btn_hedge"))
    assert ok is True
    assert view._busy is True

    # 空窗期間按「下一檔」不得換頁
    await view._on_next(interaction)
    assert view.index == 0
    assert view.symbol == "NVDA"


@pytest.mark.asyncio
async def test_view_interaction_check_rejects_second_feature_click(
    interaction: Any,
) -> None:
    view = _view(["NVDA", "TSLA"])
    view._busy = True
    ok = await view.interaction_check(
        _component_interaction(interaction, "btn_refresh")
    )
    assert ok is False
    interaction.response.defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_view_interaction_check_allows_navigation(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    ok = await view.interaction_check(
        _component_interaction(interaction, "btn_batch_next")
    )
    assert ok is True
    assert view._busy is False


@pytest.mark.asyncio
async def test_view_on_error_clears_busy(interaction: Any) -> None:
    view = _view(["NVDA", "TSLA"])
    view._busy = True
    await view.on_error(interaction, RuntimeError("x"), view.btn_hedge)
    assert view._busy is False
