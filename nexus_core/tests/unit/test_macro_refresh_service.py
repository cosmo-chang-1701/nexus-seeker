"""services/macro_refresh_service.py 共用刷新流程的單元測試。

`/force_macro_update` 的呈現與 GEX / FedWatch / 日曆 / CPI 各步驟成敗已於
`test_force_macro_update.py` 覆蓋；本檔聚焦：
- `include_vts_and_core=True`（CLI 使用）時 VTS 與核心總經指標的判定與寫入。
- `MacroRefreshResult` 的彙總屬性。
- `update_cpi_deviation()` 以回傳值表示成敗的契約。
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from services.calendar_service import calendar_service
from services.macro_refresh_service import (
    STEP_CALENDAR,
    STEP_CORE,
    STEP_CPI,
    STEP_FEDWATCH,
    STEP_GEX,
    STEP_LIQUIDITY,
    STEP_VTS,
    MacroRefreshResult,
    RefreshStep,
    refresh_macro_data,
)

_VALID_VTS: dict[str, Any] = {
    "vts_ratio": 1.05,
    "is_valid": True,
    "vts_state": "CONTANGO",
}


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
                return_value={"ted_spread": 0.21},
            )
        )
        self.vts = stack.enter_context(
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value=dict(_VALID_VTS),
            )
        )
        self.core = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_core_macro_metrics",
                new_callable=AsyncMock,
                return_value={"rrp": 420.5},
            )
        )
        self.invalidate_core = stack.enter_context(
            patch(
                "market_analysis.index_microstructure.invalidate_core_macro_metrics_cache"
            )
        )
        self.save_single = stack.enter_context(
            patch(
                "database.cache.save_kv_cache",
                new_callable=AsyncMock,
                return_value=True,
            )
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
        self.cpi = stack.enter_context(
            patch.object(
                calendar_service,
                "update_cpi_deviation",
                new_callable=AsyncMock,
                return_value=True,
            )
        )


@pytest.fixture
def env() -> Iterator[_Env]:
    with ExitStack() as stack:
        yield _Env(stack)


@pytest.mark.asyncio
async def test_default_refresh_steps(env: _Env) -> None:
    result = await refresh_macro_data()
    assert [s.name for s in result.steps] == [
        STEP_GEX,
        STEP_LIQUIDITY,
        STEP_CALENDAR,
        STEP_FEDWATCH,
        STEP_CPI,
    ]
    assert result.all_ok
    assert result.gex is not None
    assert result.gex.source == "macro"
    assert result.ted_spread == 0.21
    env.vts.assert_not_awaited()
    env.core.assert_not_awaited()
    env.cpi.assert_awaited_once()


@pytest.mark.asyncio
async def test_include_vts_and_core_writes_valid_vts(env: _Env) -> None:
    result = await refresh_macro_data(include_vts_and_core=True)
    assert result.all_ok
    assert result.vts_ratio == 1.05
    assert result.core_metrics == {"rrp": 420.5}
    env.invalidate_core.assert_called_once()
    env.save_single.assert_any_await("macro_vts_ratio", 1.05)
    step = result.step(STEP_CORE)
    assert step is not None and step.ok and "RRP: 420.5" in step.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vts",
    [
        {"vts_ratio": 1.05},  # 缺 is_valid
        {**_VALID_VTS, "vts_ratio": 4.2},  # 超出 [0.5, 3.0]
        {**_VALID_VTS, "vts_state": "UNKNOWN"},
    ],
)
async def test_invalid_vts_is_not_written(env: _Env, vts: dict[str, Any]) -> None:
    env.vts.return_value = vts
    result = await refresh_macro_data(include_vts_and_core=True)
    step = result.step(STEP_VTS)
    assert step is not None and not step.ok
    assert result.vts_ratio is None
    assert all(
        call.args[0] != "macro_vts_ratio" for call in env.save_single.await_args_list
    )


@pytest.mark.asyncio
async def test_core_fallback_is_reported_as_failure(env: _Env) -> None:
    env.core.return_value = {"rrp": 420.5, "_is_fallback": True}
    result = await refresh_macro_data(include_vts_and_core=True)
    step = result.step(STEP_CORE)
    assert step is not None and not step.ok
    assert result.core_metrics is None
    assert not result.all_ok


@pytest.mark.asyncio
async def test_gex_block_exception_does_not_abort_later_steps(env: _Env) -> None:
    with patch(
        "market_analysis.index_microstructure.invalidate_market_regime_cache",
        side_effect=RuntimeError("boom"),
    ):
        result = await refresh_macro_data()
    gex_step = result.step(STEP_GEX)
    assert gex_step is not None and not gex_step.ok
    assert "boom" in gex_step.message
    for name in (STEP_CALENDAR, STEP_FEDWATCH, STEP_CPI):
        step = result.step(name)
        assert step is not None and step.ok


def test_result_aggregation_properties() -> None:
    result = MacroRefreshResult(
        steps=[
            RefreshStep("A", True, "ok"),
            RefreshStep("B", False, "bad"),
        ]
    )
    assert not result.all_ok
    assert [s.name for s in result.succeeded] == ["A"]
    assert [s.name for s in result.failed] == ["B"]
    assert result.step("C") is None


# --- update_cpi_deviation() 回傳值契約 ---


def _cpi_patches(stack: ExitStack, row: Any) -> None:
    stack.enter_context(
        patch.object(
            calendar_service, "prefetch_monthly_macro_cache", new_callable=AsyncMock
        )
    )
    stack.enter_context(
        patch.object(
            calendar_service, "_ensure_macro_month_cached", new_callable=AsyncMock
        )
    )
    stack.enter_context(
        patch(
            "database.calendar_cache.get_latest_released_economic_event",
            return_value=row,
        )
    )
    stack.enter_context(patch("database.cache.save_kv_cache", new_callable=AsyncMock))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"actual_value": 3.2, "consensus_value": "3.1"}, True),
        ({"actual_value": 99.0, "consensus_value": "3.1"}, False),
        (None, False),
    ],
)
async def test_update_cpi_deviation_returns_success_flag(
    row: Any, expected: bool
) -> None:
    with ExitStack() as stack:
        _cpi_patches(stack, row)
        assert await calendar_service.update_cpi_deviation() is expected
