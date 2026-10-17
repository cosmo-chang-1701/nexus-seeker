"""
tests/unit/test_macro_sahm_display.py

薩姆規則面板顯示 (依門檻上色、附門檻與 FRED 觀測月份) 與
`macro_sahm_rule` / `macro_sahm_rule_date` 成對寫入的回歸測試。
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders.market_embeds import _format_sahm_rule

GREEN = "\u001b[1;32m"
YELLOW = "\u001b[1;33m"
RED = "\u001b[1;31m"


@pytest.mark.parametrize(
    "value, color, text",
    [
        (0.0, GREEN, "0.00"),
        (-0.07, GREEN, "-0.07"),
        (0.29, GREEN, "0.29"),
        (0.30, YELLOW, "0.30"),
        (0.49, YELLOW, "0.49"),
        (0.50, RED, "0.50"),
        (0.62, RED, "0.62"),
    ],
)
def test_sahm_colour_bands(value: float, color: str, text: str) -> None:
    out = _format_sahm_rule(value, "2026-09")
    assert f"{color}{text}\u001b[0m / 0.50・2026-09" in out
    assert out.startswith(" (薩姆規則值: ") and out.endswith(")")


def test_sahm_month_suffix_optional() -> None:
    out = _format_sahm_rule(0.55, None)
    assert f"{RED}0.55" in out
    assert "・" not in out
    assert _format_sahm_rule(0.55, "") == out
    assert out.endswith("\u001b[0m / 0.50)")


def test_sahm_none_or_invalid_is_empty() -> None:
    assert _format_sahm_rule(None, "2026-09") == ""
    assert _format_sahm_rule(float("nan"), "2026-09") == ""
    assert _format_sahm_rule(float("inf"), "2026-09") == ""
    assert _format_sahm_rule("abc", "2026-09") == ""


def test_sahm_no_negative_zero() -> None:
    out = _format_sahm_rule(-0.001, "2026-09")
    assert "-0.00" not in out
    assert f"{GREEN}0.00" in out


def _mock_http_client(payload: dict[str, Any]) -> MagicMock:
    res = MagicMock()
    res.status_code = 200
    res.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=res)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _run_core(data: dict[str, Any]) -> tuple[AsyncMock, AsyncMock]:
    import config
    from market_analysis import index_microstructure as im

    save = AsyncMock()
    save_many = AsyncMock(return_value=True)
    client = _mock_http_client({"status": "success", "data": data})
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", save),
        patch("database.cache.save_kv_cache_many", save_many),
        patch("httpx.AsyncClient", return_value=client),
    ):
        await im._fetch_core_macro_metrics_uncached()
    return save, save_many


@pytest.mark.asyncio
async def test_core_kv_writes_sahm_pair() -> None:
    save, save_many = await _run_core(
        {"rrp": 100.0, "sahm_rule": 0.0, "sahm_rule_date": "2026-09"}
    )
    save_many.assert_awaited_once_with(
        {"macro_sahm_rule": 0.0, "macro_sahm_rule_date": "2026-09"}
    )
    assert "macro_sahm_rule" not in {c.args[0] for c in save.call_args_list}


@pytest.mark.asyncio
async def test_core_kv_writes_none_date_when_edge_omits_it() -> None:
    _save, save_many = await _run_core({"rrp": 100.0, "sahm_rule": 0.12})
    save_many.assert_awaited_once_with(
        {"macro_sahm_rule": 0.12, "macro_sahm_rule_date": None}
    )


@pytest.mark.asyncio
async def test_core_kv_skips_both_when_sahm_missing() -> None:
    save, save_many = await _run_core(
        {"rrp": 100.0, "sahm_rule": None, "sahm_rule_date": "2026-09"}
    )
    save_many.assert_not_awaited()
    keys = {c.args[0] for c in save.call_args_list}
    assert "macro_sahm_rule" not in keys and "macro_sahm_rule_date" not in keys
