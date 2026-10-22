"""批次量化雷達：更新逾 1 小時標的按鈕、進階過濾修正、失敗標的與返回面板還原。"""

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.embed_builder import build_radar_scan_embed
from cogs.unified_terminal.batch_scan_view import (
    RADAR_REFRESH_STALE_SECONDS,
    BatchScanPaginatedView,
    BatchScanRefreshButton,
    BatchScanWarningButton,
)
from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_view import UnifiedRadarView
from services.rate_gate import _is_interactive_request


def _res(sym: str, age: Any, **extra: Any) -> dict[str, Any]:
    return {"symbol": sym, "_freshness": {"radar_age_seconds": age}, **extra}


def _state(filters: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "scope": "HOLDINGS",
        "quant_filters": filters or [],
        "params": {"max_pain_threshold": 10.0},
        "selected_tag": None,
        **extra,
    }


def _assets(*syms: str) -> list[Any]:
    out = []
    for s in syms:
        a = MagicMock()
        a.symbol = s
        out.append(a)
    return out


def _interaction() -> Any:
    it = AsyncMock()
    it.user.id = 12345
    it.response = MagicMock()
    it.response.defer = AsyncMock()
    it.response.edit_message = AsyncMock()
    it.response.send_message = AsyncMock()
    it.response.is_done.return_value = False
    it.followup.send = AsyncMock()
    it.edit_original_response = AsyncMock()
    return it


def _cog() -> UnifiedTerminalCog:
    return UnifiedTerminalCog(MagicMock())


def _pager(results: list[dict[str, Any]], cog: Any = None, **kw: Any) -> Any:
    return BatchScanPaginatedView(
        [discord.Embed(title="p1")],
        cog or MagicMock(),
        MagicMock(),
        total_items=len(results),
        scan_results=results,
        state=_state(),
        user_id=12345,
        **kw,
    )


def _refresh_btn(view: Any) -> Any:
    btns = [c for c in view.children if isinstance(c, BatchScanRefreshButton)]
    return btns[0] if btns else None


# --- #10 按鈕呈現 -----------------------------------------------------------


def test_refresh_button_counts_stale() -> None:
    view = _pager([_res("A", None), _res("B", 7200), _res("C", 600)])
    btn = _refresh_btn(view)
    assert btn.label == "🔄 更新逾 1 小時標的 (2)"
    assert btn.disabled is False
    assert btn.row == 1


def test_refresh_button_disabled_when_all_fresh() -> None:
    view = _pager([_res("A", 10), _res("B", RADAR_REFRESH_STALE_SECONDS)])
    btn = _refresh_btn(view)
    assert btn.disabled is True
    assert btn.label == "✅ 資料皆在 1 小時內"


def test_no_refresh_button_without_scan_results() -> None:
    view = BatchScanPaginatedView([discord.Embed(title="p")], MagicMock(), MagicMock())
    assert _refresh_btn(view) is None
    assert len(view.children) == 4


# --- #10 點擊流程 -----------------------------------------------------------


def _refresh_cog(slow: Any, seen: list[Any] | None = None) -> Any:
    cog = _cog()
    cog._fetch_sym_radar_data_slow = slow  # type: ignore[method-assign]

    async def fast(sym: str) -> dict[str, Any]:
        if seen is not None:
            seen.append(_is_interactive_request.get())
        return _res(sym, 5, quote={"c": 1.0})

    cog._fetch_sym_radar_data_fast = fast  # type: ignore[method-assign]
    return cog


async def _click(view: Any, interaction: Any) -> None:
    with (
        patch("services.llm_service.is_memory_safe", return_value=True),
        patch("services.asset_manager.AssetManager.get_assets") as ga,
        patch("cogs.unified_terminal.batch_scan.build_radar_scan_embed") as bld,
    ):
        ga.return_value = _assets("A", "B", "C")
        bld.return_value = [discord.Embed(title="new")]
        await _refresh_btn(view).callback(interaction)


@pytest.mark.asyncio
async def test_click_refreshes_only_stale_and_edits_in_place() -> None:
    slow = AsyncMock()
    seen: list[Any] = []
    cog = _refresh_cog(slow, seen)
    view = _pager([_res("A", None), _res("B", 7200), _res("C", 600)], cog)
    it = _interaction()

    await _click(view, it)

    awaited = sorted(c.args[0] for c in slow.await_args_list)
    assert awaited == ["A", "B"]
    assert all(c.kwargs == {"force_refresh": True} for c in slow.await_args_list)
    it.response.edit_message.assert_awaited_once()
    it.edit_original_response.assert_awaited_once()
    kwargs = it.edit_original_response.await_args.kwargs
    assert kwargs["embed"].title == "new"
    assert isinstance(kwargs["view"], BatchScanPaginatedView)
    assert kwargs["view"] is not view
    it.followup.send.assert_not_called()
    # 重新掃描的 fast fetch 在互動 context 內執行
    assert seen and all(seen)


@pytest.mark.asyncio
async def test_slow_fetch_failure_still_rescans_and_counts_stale() -> None:
    slow = AsyncMock(side_effect=RuntimeError("boom"))
    cog = _cog()
    cog._fetch_sym_radar_data_slow = slow  # type: ignore[method-assign]

    async def fast(sym: str) -> dict[str, Any]:
        return _res(sym, 9999, quote={"c": 1.0})

    cog._fetch_sym_radar_data_fast = fast  # type: ignore[method-assign]
    view = _pager([_res("A", 9999)], cog)
    it = _interaction()

    await _click(view, it)

    slow.assert_awaited_once()
    it.followup.send.assert_not_called()
    new_view = it.edit_original_response.await_args.kwargs["view"]
    assert _refresh_btn(new_view).label == "🔄 更新逾 1 小時標的 (3)"
    assert _refresh_btn(new_view).disabled is False


@pytest.mark.asyncio
async def test_memory_gate_blocks_refresh() -> None:
    slow = AsyncMock()
    cog = _refresh_cog(slow)
    view = _pager([_res("A", None)], cog)
    it = _interaction()
    with patch("services.llm_service.is_memory_safe", return_value=False):
        await _refresh_btn(view).callback(it)
    slow.assert_not_called()
    it.response.send_message.assert_awaited_once()
    assert it.response.send_message.await_args.kwargs["ephemeral"] is True
    it.response.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_error_restores_fresh_pager_and_followup() -> None:
    cog = _refresh_cog(AsyncMock())
    cog._run_unified_scan = AsyncMock(side_effect=RuntimeError("x"))
    view = _pager([_res("A", None)], cog)
    it = _interaction()
    with patch("services.llm_service.is_memory_safe", return_value=True):
        await _refresh_btn(view).callback(it)
    new_view = it.edit_original_response.await_args.kwargs["view"]
    assert new_view is not view
    assert _refresh_btn(new_view).disabled is False
    it.followup.send.assert_awaited_once()
    assert it.followup.send.await_args.kwargs["ephemeral"] is True


# --- #5 進階過濾 ------------------------------------------------------------


async def _scan(state: dict[str, Any], results: dict[str, dict[str, Any]]) -> Any:
    cog = _cog()

    async def fast(sym: str) -> Any:
        return results[sym]

    cog._fetch_sym_radar_data_fast = fast  # type: ignore[method-assign]
    it = _interaction()
    with (
        patch(
            "services.asset_manager.AssetManager.get_assets",
            return_value=_assets(*results),
        ),
        patch("cogs.unified_terminal.batch_scan.build_radar_scan_embed") as bld,
    ):
        bld.return_value = [discord.Embed(title="scan")]
        await cog.execute_unified_scan(it, state, 12345)
    return bld, it


_NEG_UOA = [{"trade_type": "BLOCK", "delta": 50.0, "dte": 30}]


@pytest.mark.asyncio
async def test_tdp_only_not_rejected_by_momentum_or_uoa() -> None:
    row = {
        "symbol": "AAA",
        "quote": {"c": 90.0},
        "ma20": 100.0,
        "max_pain": {"max_pain": 100.0},
        "volume_poc": 100.0,
        "psq_result": {"is_squeezing": False, "momentum_value": -6.0},
        "uoa": _NEG_UOA,
    }
    bld, _ = await _scan(_state(["tdp_mode"]), {"AAA": row})
    assert [r["symbol"] for r in bld.call_args.args[0]] == ["AAA"]


@pytest.mark.asyncio
async def test_squeeze_only_not_rejected_by_negative_uoa() -> None:
    row = {
        "symbol": "BBB",
        "quote": {"c": 90.0},
        "psq_result": {"is_squeezing": True, "momentum_value": 2.0},
        "uoa": _NEG_UOA,
    }
    bld, _ = await _scan(_state(["squeeze_mode"]), {"BBB": row})
    assert [r["symbol"] for r in bld.call_args.args[0]] == ["BBB"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mp", [None, {"max_pain": 0.0}, {}])
async def test_tdp_not_failed_by_missing_max_pain(mp: Any) -> None:
    row = {
        "symbol": "CCC",
        "quote": {"c": 90.0},
        "ma20": 100.0,
        "volume_poc": 100.0,
        "psq_result": {},
        "max_pain": mp,
    }
    bld, _ = await _scan(_state(["tdp_mode"]), {"CCC": row})
    assert [r["symbol"] for r in bld.call_args.args[0]] == ["CCC"]


@pytest.mark.asyncio
async def test_no_advanced_filter_skips_evaluation() -> None:
    row = {"symbol": "DDD", "quote": {"c": 1.0}}
    with patch("market_analysis.intraday_pipeline.evaluate_advanced_filters") as ev:
        bld, _ = await _scan(_state([]), {"DDD": row})
    ev.assert_not_called()
    assert len(bld.call_args.args[0]) == 1


# --- #7 抓取失敗 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_symbol_listed_in_footer() -> None:
    cog = _cog()

    async def fast(sym: str) -> Any:
        if sym == "BAD":
            raise RuntimeError("fetch failed")
        return _res(sym, 5, quote={"c": 1.0})

    cog._fetch_sym_radar_data_fast = fast  # type: ignore[method-assign]
    it = _interaction()
    with patch(
        "services.asset_manager.AssetManager.get_assets",
        return_value=_assets("OK", "BAD"),
    ):
        with patch(
            "cogs.unified_terminal.batch_scan.build_radar_scan_embed",
            return_value=[discord.Embed(title="scan")],
        ):
            await cog.execute_unified_scan(it, _state(), 12345)
    embed = it.followup.send.await_args.kwargs["embed"]
    assert embed.footer.text is not None
    assert "⚠️ 抓取失敗: BAD" in embed.footer.text


@pytest.mark.asyncio
async def test_all_failed_message_says_fetch_failed() -> None:
    cog = _cog()
    cog._fetch_sym_radar_data_fast = AsyncMock(side_effect=RuntimeError("x"))  # type: ignore[method-assign]
    it = _interaction()
    with patch(
        "services.asset_manager.AssetManager.get_assets", return_value=_assets("A")
    ):
        await cog.execute_unified_scan(it, _state(), 12345)
    embed = it.followup.send.await_args.kwargs["embed"]
    assert "抓取失敗" in (embed.title or "") + (embed.description or "")


def test_footer_unchanged_without_failures() -> None:
    view = _pager([_res("A", 1)])
    assert view.embeds[0].footer.text.endswith("頁次: 1/1 ｜ 📊 總項目: 1")


def test_footer_caps_failed_list() -> None:
    view = _pager([_res("A", 1)], failed_symbols=[f"S{i}" for i in range(25)])
    text = view.embeds[0].footer.text
    assert "等 25 檔" in text
    assert len(text) < 2048


# --- #8 警示按鈕不重新啟用邊界按鈕 ------------------------------------------


@pytest.mark.asyncio
async def test_warning_button_keeps_prev_disabled_on_first_page() -> None:
    cog = MagicMock()
    cog._run_warning_symbols_hub = AsyncMock()
    embeds = [discord.Embed(title="1"), discord.Embed(title="2")]
    view = BatchScanPaginatedView(embeds, cog, MagicMock(), total_items=2)
    it = _interaction()
    it.message = MagicMock()
    it.message.embeds = [discord.Embed(title="x")]
    warn = [c for c in view.children if isinstance(c, BatchScanWarningButton)][0]
    await warn.callback(it)
    assert view.btn_prev.disabled is True
    assert view.btn_next.disabled is False


# --- #9 返回控制面板還原 ----------------------------------------------------


@pytest.mark.asyncio
async def test_return_panel_restores_state_copy() -> None:
    state: dict[str, Any] = {
        "scope": "ALL",
        "quant_filters": ["tdp_mode", "squeeze_mode"],
        "params": {"max_pain_threshold": 7.0, "silent_period_days": 3},
        "selected_tag": "AI",
    }
    view = BatchScanPaginatedView(
        [discord.Embed(title="p")],
        MagicMock(),
        MagicMock(),
        scan_results=[_res("A", 1)],
        state=state,
        user_id=12345,
    )
    it = _interaction()
    await view.btn_return_panel.callback(it)
    new = it.response.edit_message.await_args.kwargs["view"]
    assert isinstance(new, UnifiedRadarView)
    got = new.get_state_dict()
    assert got["scope"] == "ALL"
    assert got["params"] == state["params"]
    assert got["selected_tag"] == "AI"
    assert set(got["quant_filters"]) == {"tdp_mode", "squeeze_mode"}
    assert [o.value for o in new.scope_select.options if o.default] == ["ALL"]
    assert {o.value for o in new.filter_select.options if o.default} == {
        "tdp_mode",
        "squeeze_mode",
    }
    new.params["max_pain_threshold"] = 99.0
    new.quant_filters.add("uoa_mode")
    assert state["params"]["max_pain_threshold"] == 7.0
    assert "uoa_mode" not in state["quant_filters"]


# --- #6 off-loop ------------------------------------------------------------


def test_embed_builder_loads_user_context_once() -> None:
    rows = [
        {"symbol": f"S{i}", "quote": {"c": 100.0}, "max_pain": {"max_pain": 100.0}}
        for i in range(3)
    ]
    ctx = MagicMock()
    ctx.can_trade_spreads = True
    ctx.cash_reserve_protection = False
    with (
        patch("database.get_full_user_context", return_value=ctx) as gfc,
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch("database.cache.get_kv_cache", return_value=None),
        patch("database.cache.get_kv_cache_with_age", return_value=(None, None)),
    ):
        build_radar_scan_embed(rows, "WATCHLIST", 1)
    assert gfc.call_count == 1


@pytest.mark.asyncio
async def test_embed_build_runs_off_main_thread() -> None:
    on_main: list[bool] = []

    def fake_builder(*args: Any, **kwargs: Any) -> list[discord.Embed]:
        on_main.append(threading.current_thread() is threading.main_thread())
        return [discord.Embed(title="x")]

    cog = _cog()
    cog._fetch_sym_radar_data_fast = AsyncMock(  # type: ignore[method-assign]
        return_value=_res("A", 1, quote={"c": 1.0})
    )
    it = _interaction()
    with (
        patch(
            "services.asset_manager.AssetManager.get_assets",
            return_value=_assets("A"),
        ),
        patch(
            "cogs.unified_terminal.batch_scan.build_radar_scan_embed",
            side_effect=fake_builder,
        ),
    ):
        await cog.execute_unified_scan(it, _state(), 12345)
    assert on_main == [False]
