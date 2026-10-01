"""/force_macro_update 管理員指令的回歸測試。

涵蓋：
- 大盤 GEX 有效性改依 fetch_gex_metrics() 的回傳形態（空 dict / `_is_stale_cache`）
  判斷，不再以 510/515 常數比對。
- SPY 即時備援：拒絕 `_is_stale_cache` 的過期個股快取、以單一交易批次寫入。
- 錯誤訊息不重複、FedWatch / 總經日曆依實際回傳值回報成敗。
- 總經日曆必須先於 FedWatch 刷新（日曆整月覆寫會清空 fedwatch_probability）。
- 相關服務函式的回傳值契約（save_kv_cache_many、update_fedwatch_probability、
  prefetch_monthly_macro_cache、fetch_liquidity_metrics 的 `_is_fallback`）。
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading.admin_commands import AdminCommandsCog
from config import DISCORD_ADMIN_USER_ID
from services.calendar_service import calendar_service

_LIVE_LIQ: dict[str, Any] = {"ted_spread": 0.21, "sofr_90": 5.3}
_SPY_LIVE_GEX: dict[str, Any] = {
    "spot": 505.0,
    "put_wall": 495.0,
    "gex_profile": {"490": -1_000_000.0, "500": 2_000_000.0, "510": 3_000_000.0},
}


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = DISCORD_ADMIN_USER_ID
    interaction.user.name = "admin"
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class _Env:
    def __init__(self, stack: ExitStack) -> None:
        self.gex = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_gex_metrics",
                new_callable=AsyncMock,
                return_value={"spy_spot": 600.0, "gamma_flip": 590.0},
            )
        )
        self.liq = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_liquidity_metrics",
                new_callable=AsyncMock,
                return_value=dict(_LIVE_LIQ),
            )
        )
        self.spy = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                new_callable=AsyncMock,
                return_value=dict(_SPY_LIVE_GEX),
            )
        )
        self.save_many = stack.enter_context(
            patch(
                "database.cache.save_kv_cache_many",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        self.save_single = stack.enter_context(
            patch("database.cache.save_kv_cache", new_callable=AsyncMock)
        )
        self.calendar = stack.enter_context(
            patch.object(
                calendar_service,
                "prefetch_monthly_macro_cache",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        self.fedwatch = stack.enter_context(
            patch.object(
                calendar_service,
                "update_fedwatch_probability",
                new_callable=AsyncMock,
                return_value=True,
            )
        )


@pytest.fixture
def env() -> Iterator[_Env]:
    with ExitStack() as stack:
        yield _Env(stack)


async def _run() -> tuple[str, str]:
    cog = AdminCommandsCog(MagicMock())
    interaction = _make_interaction()
    await cog.force_macro_update.callback(cog, interaction)  # type: ignore
    interaction.response.defer.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    return str(embed.title), str(embed.description)


@pytest.mark.asyncio
async def test_all_success_reports_every_component(env: _Env) -> None:
    title, desc = await _run()
    assert "系統控制" in title
    assert "SPY: $600.00 / Gamma Flip: 590.00 / TED Spread: 0.21" in desc
    assert "**FedWatch**: 最新利率定價已寫入資料庫" in desc
    assert "**總經日曆**: 已重新抓取並寫入快取" in desc
    # 英文殘留文案已移除（使用者可見文字須為繁中）
    assert "Edge Scraper" not in desc
    assert "Calendar" not in desc
    env.spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_gex_equal_to_legacy_sentinel_is_not_treated_as_default(
    env: _Env,
) -> None:
    """fetch_gex_metrics(allow_empty=True) 不會回傳 510/515 靜態常數；
    即時數據剛好等於舊哨兵值時不應被誤判為預設值而觸發 SPY 備援。"""
    env.gex.return_value = {"spy_spot": 510.0, "gamma_flip": 515.0}
    title, desc = await _run()
    assert "系統控制" in title
    assert "SPY: $510.00 / Gamma Flip: 515.00" in desc
    env.spy.assert_not_awaited()
    env.save_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_macro_cache_is_tagged_without_spy_fallback(env: _Env) -> None:
    env.gex.return_value = {
        "spy_spot": 600.0,
        "gamma_flip": 590.0,
        "_is_stale_cache": True,
    }
    _, desc = await _run()
    assert "使用快取資料" in desc
    env.spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_gex_uses_spy_fallback_with_single_batch_write(
    env: _Env,
) -> None:
    env.gex.return_value = {}
    title, desc = await _run()

    assert "系統控制" in title
    assert "SPY: $505.00 / Gamma Flip: 500.00" in desc
    env.spy.assert_awaited_once_with("SPY", force_live=True)
    env.save_many.assert_awaited_once()
    env.save_single.assert_not_awaited()
    save_call = env.save_many.await_args
    assert save_call is not None
    items = save_call.args[0]
    assert items["macro_spy_spot"] == 505.0
    assert items["macro_spy_gamma_flip"] == 500.0
    assert items["macro_gamma_flip_line"] == 5000.0
    assert items["macro_gex_is_fallback"] == 0
    assert items["macro_gex_metrics_cache"]["data"] == {
        "spy_spot": 505.0,
        "gamma_flip": 500.0,
        "put_wall": 495.0,
    }


@pytest.mark.asyncio
async def test_gex_exception_also_triggers_spy_fallback(env: _Env) -> None:
    env.gex.side_effect = RuntimeError("boom")
    _, desc = await _run()
    assert "SPY: $505.00" in desc
    env.save_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_spy_fallback_rejects_stale_symbol_cache(env: _Env) -> None:
    """force_live 抓取失敗時回傳的過期個股快取不可當作即時數據寫回大盤快取。"""
    env.gex.return_value = {}
    env.spy.return_value = {**_SPY_LIVE_GEX, "_is_stale_cache": True}
    title, desc = await _run()

    assert "更新部分失敗" in title
    assert "GEX 更新失敗" in desc
    env.save_many.assert_not_awaited()
    env.save_single.assert_not_awaited()


@pytest.mark.asyncio
async def test_spy_fallback_write_failure_is_reported(env: _Env) -> None:
    env.gex.return_value = {}
    env.save_many.return_value = False
    title, desc = await _run()
    assert "更新部分失敗" in title
    assert "GEX 更新失敗" in desc
    assert "**GEX**" not in desc


@pytest.mark.asyncio
async def test_gex_failure_message_is_not_duplicated(env: _Env) -> None:
    env.gex.return_value = {}
    env.spy.side_effect = RuntimeError("scrape down")
    _, desc = await _run()
    assert desc.count("GEX 更新失敗") == 1
    assert "**GEX**" not in desc
    assert "獲取數據失敗" not in desc
    # 其餘成功項目仍列出
    assert "**FedWatch**" in desc
    assert "**總經日曆**" in desc


@pytest.mark.asyncio
async def test_liquidity_fallback_is_not_shown_as_live_value(env: _Env) -> None:
    env.liq.return_value = {"ted_spread": 0.15, "_is_fallback": True}
    title, desc = await _run()
    assert "更新部分失敗" in title
    assert "流動性指標 (TED Spread) 更新失敗" in desc
    assert "TED Spread: 0.15" not in desc
    assert "SPY: $600.00 / Gamma Flip: 590.00" in desc


@pytest.mark.asyncio
async def test_fedwatch_false_return_is_reported_as_failure(env: _Env) -> None:
    env.fedwatch.return_value = False
    title, desc = await _run()
    assert "更新部分失敗" in title
    assert "FedWatch 更新失敗" in desc
    assert "已寫入資料庫" not in desc


@pytest.mark.asyncio
async def test_calendar_false_return_is_reported_as_failure(env: _Env) -> None:
    env.calendar.return_value = False
    title, desc = await _run()
    assert "更新部分失敗" in title
    assert "總經日曆更新失敗" in desc
    assert "已重新抓取" not in desc


@pytest.mark.asyncio
async def test_calendar_refresh_runs_before_fedwatch(env: _Env) -> None:
    """日曆強制刷新會整月 DELETE + INSERT（fedwatch_probability 為 NULL），
    必須先刷新日曆再寫入 FedWatch，否則剛寫入的定價會被清空。"""
    order: list[str] = []

    def _calendar(**_: Any) -> bool:
        order.append("calendar")
        return True

    def _fedwatch() -> bool:
        order.append("fedwatch")
        return True

    env.calendar.side_effect = _calendar
    env.fedwatch.side_effect = _fedwatch
    await _run()
    assert order == ["calendar", "fedwatch"]
    env.calendar.assert_awaited_once_with(months_ahead=1, force_fetch=True)


@pytest.mark.asyncio
async def test_non_admin_is_rejected_before_defer(env: _Env) -> None:
    cog = AdminCommandsCog(MagicMock())
    interaction = _make_interaction()
    interaction.user.id = DISCORD_ADMIN_USER_ID + 1
    await cog.force_macro_update.callback(cog, interaction)  # type: ignore
    interaction.response.send_message.assert_awaited_once()
    interaction.response.defer.assert_not_awaited()
    env.gex.assert_not_awaited()


# --- 服務層回傳值契約 ---


@pytest.mark.asyncio
async def test_save_kv_cache_many_writes_in_single_batch() -> None:
    from database.cache import save_kv_cache_many

    with patch(
        "database.cache.execute_write_many_async", new_callable=AsyncMock
    ) as mock_many:
        ok = await save_kv_cache_many({"a": 1, "b": {"x": 2}})

    assert ok is True
    mock_many.assert_awaited_once()
    many_call = mock_many.await_args
    assert many_call is not None
    statements = many_call.args[0]
    assert len(statements) == 1
    _sql, rows, is_many = statements[0]
    assert is_many is True
    assert rows == [("a", "1"), ("b", '{"x": 2}')]


@pytest.mark.asyncio
async def test_save_kv_cache_many_returns_false_on_failure() -> None:
    from database.cache import save_kv_cache_many

    with patch(
        "database.cache.execute_write_many_async",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db locked"),
    ):
        assert await save_kv_cache_many({"a": 1}) is False


@pytest.mark.asyncio
async def test_save_kv_cache_many_empty_is_noop() -> None:
    from database.cache import save_kv_cache_many

    with patch(
        "database.cache.execute_write_many_async", new_callable=AsyncMock
    ) as mock_many:
        assert await save_kv_cache_many({}) is True
    mock_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_fedwatch_probability_returns_success_flag() -> None:
    import config

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {
        "status": "success",
        "data": {"probability": 0.6, "prob_hike": 0.0, "prob_cut": 40.0},
    }
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.return_value = {
        "status": "success",
        "data": {"probability": 1.0, "prob_hike": 100.0, "prob_cut": 0.0},
    }

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("database.connection.execute_write_async", new_callable=AsyncMock),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value = ok_resp
        assert await calendar_service.update_fedwatch_probability() is True

        mock_get.return_value = bad_resp
        assert await calendar_service.update_fedwatch_probability() is False

        mock_get.side_effect = RuntimeError("network down")
        assert await calendar_service.update_fedwatch_probability() is False

    with (
        patch.object(config, "TUNNEL_URL", ""),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
    ):
        assert await calendar_service.update_fedwatch_probability() is False


@pytest.mark.asyncio
async def test_prefetch_monthly_macro_cache_returns_all_months_success() -> None:
    with patch.object(
        calendar_service, "_ensure_macro_month_cached", new_callable=AsyncMock
    ) as mock_ensure:
        mock_ensure.return_value = (True, False)
        assert (
            await calendar_service.prefetch_monthly_macro_cache(
                months_ahead=1, force_fetch=True
            )
            is True
        )

        mock_ensure.side_effect = [(True, False), (False, True)]
        assert (
            await calendar_service.prefetch_monthly_macro_cache(
                months_ahead=1, force_fetch=True
            )
            is False
        )


@pytest.mark.asyncio
async def test_fetch_liquidity_metrics_marks_fallback() -> None:
    import config
    from market_analysis.index_microstructure import fetch_liquidity_metrics

    with (
        patch.object(config, "TUNNEL_URL", ""),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
    ):
        result = await fetch_liquidity_metrics()
    assert result["_is_fallback"] is True
    assert result["ted_spread"] == 0.15

    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("down"),
        ),
    ):
        result = await fetch_liquidity_metrics()
    assert result["_is_fallback"] is True
