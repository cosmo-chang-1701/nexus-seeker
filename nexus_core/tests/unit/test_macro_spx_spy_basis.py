"""get_macro_overview_data 的 SPX/SPY 衍生：用實測比值、不寫回 KV、標記衍生。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run(
    quotes: dict[str, Any], kv: dict[str, Any]
) -> tuple[dict[str, Any], MagicMock]:
    from cogs.unified_terminal.utils import get_macro_overview_data

    save_mock = AsyncMock()

    async def _quote(symbol: str) -> Any:
        return quotes.get(symbol)

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("database.get_kv_cache", side_effect=lambda k, *a, **kw: kv.get(k)),
        patch("database.save_kv_cache", save_mock),
        patch("services.market_data_service.get_quote", side_effect=_quote),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
    ):
        data = await get_macro_overview_data(1)
        # 讓 create_task 寫入有機會執行
        import asyncio

        await asyncio.sleep(0)
    return data, save_mock


def _saved_keys(save_mock: MagicMock) -> list[str]:
    return [c.args[0] for c in save_mock.call_args_list]


@pytest.mark.asyncio
async def test_spy_derived_from_kv_ratio_not_written_back() -> None:
    data, save = await _run({"^SPX": {"c": 7744.06}}, {"macro_spx_spy_ratio": 10.0335})
    assert data["spy_spot"] == pytest.approx(771.82, abs=0.01)
    assert data["spy_spot_is_derived"] is True
    assert data["spx_is_derived"] is False
    assert "macro_spy_spot" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_both_live_writes_ratio_and_flags_false() -> None:
    data, save = await _run({"^SPX": {"c": 7744.06}, "SPY": {"c": 771.79}}, {})
    ratio_calls = [c for c in save.call_args_list if c.args[0] == "macro_spx_spy_ratio"]
    assert len(ratio_calls) == 1
    assert ratio_calls[0].args[1] == pytest.approx(10.0338, abs=0.0005)
    assert data["spx_is_derived"] is False
    assert data["spy_spot_is_derived"] is False


@pytest.mark.asyncio
async def test_no_ratio_kv_falls_back_to_ten() -> None:
    data, save = await _run({"SPY": {"c": 762.5}}, {})
    assert data["spx"] == 7625.0
    assert data["spx_is_derived"] is True
    assert "macro_spx" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_out_of_range_ratio_kv_ignored() -> None:
    data, _ = await _run({"^SPX": {"c": 7700.0}}, {"macro_spx_spy_ratio": 11.5})
    assert data["spy_spot"] == 770.0
    assert data["spy_spot_is_derived"] is True


@pytest.mark.asyncio
async def test_ratio_not_written_when_one_side_from_kv_fallback() -> None:
    _, save = await _run({"^SPX": {"c": 7744.06}}, {"macro_spy_spot": 771.0})
    assert "macro_spx_spy_ratio" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_out_of_range_live_ratio_not_written() -> None:
    _, save = await _run({"^SPX": {"c": 7000.0}, "SPY": {"c": 771.0}}, {})
    assert "macro_spx_spy_ratio" not in _saved_keys(save)
