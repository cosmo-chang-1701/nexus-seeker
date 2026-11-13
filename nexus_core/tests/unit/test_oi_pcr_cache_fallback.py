"""OI PCR 快取降級：即時 OI 缺失時，僅供呈現的 oi_pcr_cached* 鍵（oi_pcr 不變）。"""

from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.sentiment.options_flow import _pcr_thresholds, calculate_pcr

_OF = "market_analysis.sentiment.options_flow"
_CACHED_KEYS = ("oi_pcr_cached", "oi_pcr_cached_state", "oi_pcr_cached_age_seconds")


class _Chain:
    def __init__(self, cv: float, pv: float, coi: float, poi: float) -> None:
        self.calls = pd.DataFrame({"volume": [cv], "openInterest": [coi]})
        self.puts = pd.DataFrame({"volume": [pv], "openInterest": [poi]})


async def _run(
    chain: Any,
    stored: Tuple[Optional[float], Optional[float]] = (None, None),
    symbol: str = "AAPL",
    expiries_exc: Optional[Exception] = None,
    expiries: Optional[list] = None,
) -> Tuple[Dict[str, Any], AsyncMock, MagicMock]:
    exp_kwargs: Dict[str, Any] = (
        {"side_effect": expiries_exc}
        if expiries_exc
        else {"return_value": ["2099-01-15"] if expiries is None else expiries}
    )
    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        **exp_kwargs,
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ), patch(f"{_OF}.get_last_stored_sentiment", return_value=None), patch(
        f"{_OF}.get_last_stored_sentiment_with_age", return_value=stored
    ) as mock_age, patch(
        f"{_OF}.save_sentiment_history", new_callable=AsyncMock
    ) as mock_save:
        res = await calculate_pcr(symbol)
    return res, mock_save, mock_age


@pytest.mark.asyncio
async def test_live_success_persists_oi_pcr() -> None:
    res, mock_save, mock_age = await _run(_Chain(1000.0, 1200.0, 500.0, 750.0))
    assert res["oi_pcr"] == 1.5
    mock_save.assert_any_await("AAPL", "OI_PCR", 1.5)
    mock_save.assert_any_await("AAPL", "PCR", 1.2)
    assert not any(k in res for k in _CACHED_KEYS)
    mock_age.assert_not_called()


@pytest.mark.asyncio
async def test_all_zero_uses_cache_presentation_only() -> None:
    res, _, _ = await _run(_Chain(0.0, 0.0, 0.0, 0.0), stored=(1.5, 36000.0))
    assert res["oi_pcr"] is None
    assert res["oi_pcr_state"] == "N/A"
    assert res["oi_pcr_cached"] == 1.5
    assert res["oi_pcr_cached_state"] == "🐻 結構防禦/偏向空頭"
    assert res["oi_pcr_cached_age_seconds"] == 36000.0


@pytest.mark.asyncio
async def test_index_etf_cached_state_uses_etf_thresholds() -> None:
    res, _, _ = await _run(
        _Chain(0.0, 0.0, 0.0, 0.0), stored=(1.3, 3600.0), symbol="SPY"
    )
    assert res["oi_pcr_cached_state"] == "結構平衡"


@pytest.mark.asyncio
async def test_stale_cache_not_attached() -> None:
    # get_last_stored_sentiment_with_age 超過 max_age 時回 (None, age)
    res, _, _ = await _run(_Chain(0.0, 0.0, 0.0, 0.0), stored=(None, 400000.0))
    assert res["oi_pcr"] is None
    assert not any(k in res for k in _CACHED_KEYS)


@pytest.mark.asyncio
async def test_lookup_uses_96h_cap() -> None:
    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(f"{_OF}.get_last_stored_sentiment", return_value=None), patch(
        f"{_OF}.get_last_stored_sentiment_with_age", return_value=(None, None)
    ) as mock_get:
        await calculate_pcr("AAPL")
    mock_get.assert_called_once_with("AAPL", "OI_PCR", max_age_seconds=96 * 3600)


@pytest.mark.asyncio
async def test_no_expiries_uses_cache() -> None:
    res, _, _ = await _run(None, stored=(0.8, 100.0), expiries=[])
    assert res["oi_pcr"] is None
    assert res["oi_pcr_cached"] == 0.8
    assert res["oi_pcr_cached_state"] == "🐂 結構看漲/偏向多頭"


@pytest.mark.asyncio
async def test_exception_path_uses_cache() -> None:
    res, _, _ = await _run(None, stored=(1.0, 100.0), expiries_exc=RuntimeError("boom"))
    assert res["state"] == "ERROR"
    assert res["oi_pcr"] is None
    assert res["oi_pcr_cached"] == 1.0


@pytest.mark.asyncio
async def test_volume_zero_with_live_oi_keeps_live_oi() -> None:
    res, mock_save, mock_age = await _run(
        _Chain(0.0, 0.0, 2000.0, 3000.0), stored=(0.5, 100.0)
    )
    assert res["oi_pcr"] == 1.5
    assert not any(k in res for k in _CACHED_KEYS)
    mock_save.assert_awaited_once_with("AAPL", "OI_PCR", 1.5)
    mock_age.assert_not_called()


@pytest.mark.asyncio
async def test_live_volume_with_zero_oi_uses_cache() -> None:
    # 有成交量、OI 兩邊皆 0：正常路徑（成交量 PCR 有定義），但無即時 OI，應附快取
    res, _, _ = await _run(_Chain(10.0, 0.0, 0.0, 0.0), stored=(1.2, 100.0))
    assert res["oi_pcr"] is None
    assert res["oi_pcr_cached"] == 1.2


@pytest.mark.asyncio
async def test_one_sided_volume_with_zero_oi_uses_cache() -> None:
    # Call 成交量為 0、Put 有量（單邊）；OI 兩邊皆 0：走成交量單邊分支，附 OI 快取
    res, _, _ = await _run(_Chain(0.0, 10.0, 0.0, 0.0), stored=(2.0, 100.0))
    assert res["oi_pcr"] is None
    assert res["oi_pcr_cached"] == 2.0
    assert res["volume_pcr"] is None
    assert "Call 成交量為 0" in res["state"]


@pytest.mark.asyncio
async def test_one_sided_oi_not_replaced_by_cache() -> None:
    res, mock_save, mock_age = await _run(
        _Chain(10.0, 10.0, 0.0, 100.0), stored=(0.5, 100.0)
    )
    assert res["oi_pcr"] is None
    assert "Call 未平倉量為 0" in res["oi_pcr_state"]
    assert not any(k in res for k in _CACHED_KEYS)
    assert all(c.args[1] != "OI_PCR" for c in mock_save.await_args_list)
    mock_age.assert_not_called()


def test_pcr_thresholds_index_vs_single() -> None:
    assert _pcr_thresholds(True) == (1.20, 1.40)
    assert _pcr_thresholds(False) == (0.90, 1.10)
