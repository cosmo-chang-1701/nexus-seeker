"""/market「流動性與總經指標」的 CP−T-Bill 利差（TED 代理）列（僅呈現）。"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
from cogs.unified_terminal.utils import get_macro_overview_data

_LABEL = "CP−T-Bill 利差 (TED 代理)"
_CYAN = "\u001b[1;36m"
_RED = "\u001b[1;31m"
_YELLOW = "\u001b[1;33m"
_RESET = "\u001b[0m"
_ALERT = "⚠️ 流動性警戒"
_FALLBACK_TAG = "[即時抓取失敗，沿用快取]"
_DAY = 86400.0


def _field(**macro: Any) -> str:
    embed = build_market_macro_overview_embed(macro)
    return str(
        next(f for f in embed.fields if "流動性與總經" in str(f.name)).value or ""
    )


def _ted_line(**macro: Any) -> str:
    return next(ln for ln in _field(**macro).splitlines() if _LABEL in ln)


@pytest.mark.parametrize(
    "value,shown", [(0.21, "0.21"), (0.0, "0.00"), (-0.07, "-0.07"), (0.5, "0.50")]
)
def test_ted_normal_values_are_cyan_without_alert(value: float, shown: str) -> None:
    line = _ted_line(ted_spread=value)
    assert line == f" ├─ {_LABEL}: {_CYAN}{shown}{_RESET}"


def test_ted_above_threshold_is_red_with_alert() -> None:
    line = _ted_line(ted_spread=0.65)
    assert line == f" ├─ {_LABEL}: {_RED}0.65{_RESET} {_RED}{_ALERT}{_RESET}"


@pytest.mark.parametrize(
    "bad", [None, "INVALID_STR", float("nan"), float("inf"), float("-inf"), True, False]
)
def test_ted_missing_or_invalid_shows_failure(bad: Any) -> None:
    # 過期／備援旗標不得讓缺值列出現快取標示
    line = _ted_line(
        ted_spread=bad,
        ted_is_expired=True,
        ted_is_fallback=True,
        ted_cache_age_seconds=9 * _DAY,
    )
    assert line == f" ├─ {_LABEL}: {_RED}獲取數據失敗{_RESET}"


def test_ted_missing_key_shows_failure() -> None:
    assert "獲取數據失敗" in _ted_line()


@pytest.mark.parametrize("is_fallback", [False, True])
def test_ted_expired_tags_cache_age(is_fallback: bool) -> None:
    line = _ted_line(
        ted_spread=0.21,
        ted_is_expired=True,
        ted_is_fallback=is_fallback,
        ted_cache_age_seconds=24 * _DAY,
    )
    assert line.endswith(f" {_YELLOW}[24.0 天前快取]{_RESET}")
    assert _FALLBACK_TAG not in line


def test_ted_fallback_not_expired_is_tagged() -> None:
    line = _ted_line(
        ted_spread=0.65, ted_is_fallback=True, ted_cache_age_seconds=3600.0
    )
    assert _ALERT in line
    assert line.endswith(f" {_YELLOW}{_FALLBACK_TAG}{_RESET}")
    assert "天前快取" not in line


def test_ted_row_follows_credit_spreads_in_tree() -> None:
    lines = _field(ted_spread=0.21).splitlines()
    idx = next(i for i, ln in enumerate(lines) if _LABEL in ln)
    assert "IG OAS" in lines[idx - 1]
    assert "FedWatch" in lines[idx + 1]


def test_ted_field_length_worst_case() -> None:
    spread = {"bps": 1999, "change_1m_bps": -1999, "obs_date": "2020-01-01"}
    value = _field(
        wti=199.99,
        rrp=2500.0,
        rrp_change_30d=12345.6,
        fed_balance=99.99,
        fear_greed=100.0,
        uer=14.7,
        sahm_rule=-10.55,
        sahm_rule_date="2026-09-01",
        credit_spreads={"hy": spread, "ig": spread},
        core_is_expired=True,
        core_cache_age_seconds=999 * _DAY,
        fedwatch_probability=1.0,
        fedwatch_is_fallback=True,
        fedwatch_details={
            "meeting_date": "2026-12-09",
            "prob_maintain": 100.0,
            "prob_hike": 100.0,
            "prob_cut": 100.0,
            "prob_hike_25": 100.0,
            "prob_hike_50": 100.0,
            "ladder_saturated": True,
            "source": "CBOT ZQ",
        },
        cpi_actual=-10.1,
        cpi_expected=10.1,
        cpi_is_fallback=True,
        ted_spread=99.99,
        ted_is_fallback=True,
    )
    assert len(value) <= 1024
    # 未被 NexusEmbed 截斷：TED 標示與末尾資料源說明都完整
    assert _FALLBACK_TAG in value
    assert value.endswith("可能有落差。\n```")


def _overview_patches(kv: dict[str, Any], ages: dict[str, float]) -> list[Any]:
    return [
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch(
            "cogs.unified_terminal.utils.get_credit_spreads",
            new_callable=AsyncMock,
            return_value={"hy": None, "ig": None},
        ),
        patch("database.get_kv_cache", side_effect=kv.get),
        patch("database.save_kv_cache", new_callable=AsyncMock),
        patch(
            "database.cache.get_kv_cache_many",
            side_effect=lambda keys: {
                k: (kv.get(k), ages[k]) for k in keys if k in ages
            },
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            side_effect=Exception("offline"),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch(
            "services.calendar_service.calendar_service.get_latest_fedwatch_info",
            return_value=(None, True, {}, False),
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("offline"),
        ),
    ]


async def _overview(kv: dict[str, Any], ages: dict[str, float]) -> dict[str, Any]:
    with ExitStack() as stack:
        for p in _overview_patches(kv, ages):
            stack.enter_context(p)
        return await get_macro_overview_data(5150)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age,flag,expired,fallback",
    [
        (600.0, 0, False, False),
        (600.0, 1, False, True),
        (3 * _DAY, 0, False, False),
        (3 * _DAY + 1, 1, True, True),
        (24 * _DAY, 0, True, False),
        (600.0, None, False, False),
    ],
)
async def test_overview_data_ted_keys(
    age: float, flag: int | None, expired: bool, fallback: bool
) -> None:
    kv: dict[str, Any] = {"macro_ted_spread": -0.07}
    if flag is not None:
        kv["macro_liquidity_is_fallback"] = flag
    data = await _overview(kv, {"macro_ted_spread": age})
    assert data["ted_spread"] == -0.07
    assert data["ted_cache_age_seconds"] == age
    assert data["ted_is_expired"] is expired
    assert data["ted_is_fallback"] is fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "INVALID_STR", "nan", True])
async def test_overview_data_ted_missing_or_invalid(raw: Any) -> None:
    kv: dict[str, Any] = {"macro_liquidity_is_fallback": 1}
    ages: dict[str, float] = {}
    if raw is not None:
        kv["macro_ted_spread"] = raw
        ages["macro_ted_spread"] = 24 * _DAY
    data = await _overview(kv, ages)
    assert data["ted_spread"] is None
    assert data["ted_is_expired"] is False
    assert data["ted_is_fallback"] is True
    line = next(
        ln
        for f in build_market_macro_overview_embed(data).fields
        for ln in str(f.value).splitlines()
        if _LABEL in ln
    )
    assert "獲取數據失敗" in line
