"""get_macro_overview_data 的 SPX/SPY 衍生：用實測比值、不寫回 KV、標記衍生。"""

from typing import Iterator
from unittest.mock import MagicMock

import pytest

import config
from tests.unit.macro_overview_helpers import run_macro_overview
from tests.unit.network_guard import NetworkGuard


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


def _saved_keys(save_mock: MagicMock) -> list[str]:
    return [c.args[0] for c in save_mock.call_args_list]


@pytest.mark.asyncio
async def test_spy_derived_from_kv_ratio_not_written_back() -> None:
    data, save = await run_macro_overview(
        {"^SPX": {"c": 7744.06}}, {"macro_spx_spy_ratio": 10.0335}
    )
    assert data["spy_spot"] == pytest.approx(771.82, abs=0.01)
    assert data["spy_spot_is_derived"] is True
    assert data["spx_is_derived"] is False
    assert "macro_spy_spot" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_both_live_writes_ratio_and_flags_false() -> None:
    data, save = await run_macro_overview(
        {"^SPX": {"c": 7744.06}, "SPY": {"c": 771.79}}, {}
    )
    ratio_calls = [c for c in save.call_args_list if c.args[0] == "macro_spx_spy_ratio"]
    assert len(ratio_calls) == 1
    assert ratio_calls[0].args[1] == pytest.approx(10.0338, abs=0.0005)
    assert data["spx_is_derived"] is False
    assert data["spy_spot_is_derived"] is False


@pytest.mark.asyncio
async def test_no_ratio_kv_falls_back_to_ten() -> None:
    data, save = await run_macro_overview({"SPY": {"c": 762.5}}, {})
    assert data["spx"] == 7625.0
    assert data["spx_is_derived"] is True
    assert "macro_spx" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_out_of_range_ratio_kv_ignored() -> None:
    data, _ = await run_macro_overview(
        {"^SPX": {"c": 7700.0}}, {"macro_spx_spy_ratio": 11.5}
    )
    assert data["spy_spot"] == 770.0
    assert data["spy_spot_is_derived"] is True


@pytest.mark.asyncio
async def test_ratio_not_written_when_one_side_from_kv_fallback() -> None:
    _, save = await run_macro_overview(
        {"^SPX": {"c": 7744.06}}, {"macro_spy_spot": 771.0}
    )
    assert "macro_spx_spy_ratio" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_out_of_range_live_ratio_not_written() -> None:
    _, save = await run_macro_overview({"^SPX": {"c": 7000.0}, "SPY": {"c": 771.0}}, {})
    assert "macro_spx_spy_ratio" not in _saved_keys(save)


@pytest.mark.asyncio
async def test_run_macro_overview_offline_with_edge_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使配置 TUNNEL_URL，run_macro_overview 也不應觸發對外總經日曆連線。"""
    monkeypatch.setattr(config, "TUNNEL_URL", "http://edge.invalid")
    data, _ = await run_macro_overview({"^SPX": {"c": 7744.06}}, {})
    assert data["cpi_nfp_calendar"] == "近期無重大數據"
