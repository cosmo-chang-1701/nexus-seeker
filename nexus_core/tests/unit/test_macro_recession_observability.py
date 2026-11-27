"""經濟衰退警告三態判定、觸發歸因、快取時效與 `/market`／CC 閘門一致性的測試。"""

from contextlib import ExitStack
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
from cogs.unified_terminal.utils import get_macro_overview_data
from market_analysis.trading_orchestration import (
    MACRO_SAHM_CACHE_MAX_AGE_SECONDS,
    MACRO_US10Y_CACHE_MAX_AGE_SECONDS,
    evaluate_recession_warning,
    is_covered_call_unlock_allowed,
)
from scripts.audit_macro_market_live import _expected_recession_for_audit
from tests.unit.network_guard import NetworkGuard

_DAY = 86400.0


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


def _embed_text(embed: Any) -> str:
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _overview_patches(
    quotes: dict[str, Any],
    kv: dict[str, Any],
    ages: dict[str, float],
    core: dict[str, Any] | None = None,
) -> list[Any]:
    async def _quote(sym: str) -> Any:
        val = quotes.get(sym)
        if isinstance(val, Exception):
            raise val
        return val

    return [
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch("services.market_data_service.get_quote", side_effect=_quote),
        patch("database.get_kv_cache", side_effect=lambda k: kv.get(k)),
        patch("database.save_kv_cache", new_callable=AsyncMock),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "database.cache.get_kv_cache_many",
            side_effect=lambda keys: {
                k: (kv.get(k), ages[k]) for k in keys if k in ages
            },
        ),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value=core if core is not None else {"_is_fallback": True},
        ),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "services.calendar_service.calendar_service.get_latest_fedwatch_info",
            return_value=(None, True, {}, False),
        ),
        patch(
            "cogs.unified_terminal.utils.get_credit_spreads",
            new_callable=AsyncMock,
            return_value={"hy": None, "ig": None},
        ),
        patch(
            "market_analysis.trading_orchestration.get_safety_payout_threshold",
            return_value=13000.0,
        ),
    ]


async def _run_overview(
    quotes: dict[str, Any],
    kv: dict[str, Any],
    ages: dict[str, float],
    core: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(p) for p in _overview_patches(quotes, kv, ages, core)
        ]
        data = await get_macro_overview_data(9001)
    return data, mocks[8]


# ---------------------------------------------------------------------------
# 1. 純三值判定 evaluate_recession_warning
# ---------------------------------------------------------------------------


def test_evaluate_recession_warning_rate_vix_trigger_with_zero_sahm() -> None:
    status = evaluate_recession_warning(sahm_rule=0.0, us10y=4.94, vix=23.81)
    assert status.state == "WARNING"
    assert status.is_warning is True
    assert status.cc_unlock_allowed is False
    assert status.triggers == ("US10Y 4.94% > 4.5% & VIX 23.81 > 20",)
    assert status.missing == ()


def test_evaluate_recession_warning_sahm_trigger() -> None:
    status = evaluate_recession_warning(sahm_rule=0.50, us10y=4.20, vix=15.0)
    assert status.state == "WARNING"
    assert status.triggers == ("薩姆規則 0.50 ≥ 0.50",)
    assert status.missing == ()


def test_evaluate_recession_warning_both_triggers() -> None:
    status = evaluate_recession_warning(sahm_rule=0.62, us10y=4.94, vix=23.81)
    assert status.state == "WARNING"
    assert status.triggers == (
        "薩姆規則 0.62 ≥ 0.50",
        "US10Y 4.94% > 4.5% & VIX 23.81 > 20",
    )
    assert status.missing == ()


def test_evaluate_recession_warning_normal_and_missing_specificity() -> None:
    # US10Y <= 4.5 即排除 (US10Y > 4.5 & VIX > 20)，即使 VIX 未知仍可判 NORMAL
    normal = evaluate_recession_warning(sahm_rule=-0.1, us10y=4.1, vix=None)
    assert normal.state == "NORMAL"
    assert normal.cc_unlock_allowed is True
    assert normal.missing == ()

    # 只缺阻礙判定分支的欄位
    miss_sahm = evaluate_recession_warning(sahm_rule=None, us10y=4.1, vix=15.0)
    assert miss_sahm.state == "UNKNOWN"
    assert miss_sahm.cc_unlock_allowed is False
    assert miss_sahm.missing == ("薩姆規則",)

    miss_vix = evaluate_recession_warning(sahm_rule=0.2, us10y=4.94, vix=None)
    assert miss_vix.state == "UNKNOWN"
    assert miss_vix.missing == ("VIX",)

    miss_us10y = evaluate_recession_warning(sahm_rule=0.2, us10y=None, vix=23.81)
    assert miss_us10y.state == "UNKNOWN"
    assert miss_us10y.missing == ("US10Y",)


# ---------------------------------------------------------------------------
# 2. `/market` 面板歸因、三態與 CC 閘門一致性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_overview_displays_rate_vix_trigger_attribution() -> None:
    quotes = {
        "^SPX": {"c": 5900.0},
        "SPY": {"c": 588.0},
        "^VIX": {"c": 23.81},
        "^TNX": {"c": 4.94},
        "CL=F": {"c": 72.0},
    }
    kv = {
        "macro_rrp": 0.2,
        "macro_fed_balance": 6.8,
        "macro_fear_greed": 45.0,
        "macro_uer": 4.1,
        "macro_sahm_rule": 0.0,
    }
    ages = {k: 300.0 for k in kv}

    data, _ = await _run_overview(quotes, kv, ages)
    assert data["recession_warning"] is True
    assert data["recession_state"] == "WARNING"
    assert data["recession_triggers"] == ["US10Y 4.94% > 4.5% & VIX 23.81 > 20"]
    assert data["recession_missing"] == []

    embed = build_market_macro_overview_embed(data)
    text = _embed_text(embed)
    assert "🚨 WARNING (CC 開倉阻斷已生效：US10Y 4.94% > 4.5% & VIX 23.81 > 20)" in text
    assert int(getattr(embed.color, "value")) == 0xE74C3C


@pytest.mark.asyncio
async def test_market_overview_both_triggers_and_legacy_fallback() -> None:
    embed = build_market_macro_overview_embed(
        {
            "spx": 5900.0,
            "vix": 23.81,
            "us10y": 4.94,
            "sahm_rule": 0.62,
            "recession_warning": True,
            "recession_state": "WARNING",
            "recession_triggers": [
                "薩姆規則 0.62 ≥ 0.50",
                "US10Y 4.94% > 4.5% & VIX 23.81 > 20",
            ],
        }
    )
    text = _embed_text(embed)
    assert (
        "🚨 WARNING (CC 開倉阻斷已生效：薩姆規則 0.62 ≥ 0.50；US10Y 4.94% > 4.5% & VIX 23.81 > 20)"
        in text
    )

    # 舊格式 caller 只傳 recession_warning=True 且無 triggers 時，從數值補推；全無數值則降回純文字
    legacy_derived = _embed_text(
        build_market_macro_overview_embed(
            {"sahm_rule": 0.55, "us10y": 4.1, "vix": 15.0, "recession_warning": True}
        )
    )
    assert "🚨 WARNING (CC 開倉阻斷已生效：薩姆規則 0.55 ≥ 0.50)" in legacy_derived

    legacy_plain = _embed_text(
        build_market_macro_overview_embed({"recession_warning": True})
    )
    assert "🚨 WARNING (CC 開倉阻斷已生效)" in legacy_plain


@pytest.mark.asyncio
async def test_live_vix_failure_with_fresh_kv_cache_is_unknown_and_aligned_with_cc_gate() -> (
    None
):
    """即時 ^VIX 失敗且 macro_vix KV 仍在 3 天內：面板顯示 [快取] 但不納入判定，
    `/market` 顯示 ⚪ 未知（金色邊框）且與 is_covered_call_unlock_allowed() 同步阻斷。"""
    quotes = {
        "^SPX": {"c": 5900.0},
        "SPY": {"c": 588.0},
        "^VIX": {},
        "^TNX": {"c": 4.94},
        "CL=F": {"c": 72.0},
    }
    kv = {
        "macro_vix": 16.38,
        "macro_rrp": 0.2,
        "macro_fed_balance": 6.8,
        "macro_fear_greed": 45.0,
        "macro_uer": 4.1,
        "macro_sahm_rule": 0.0,
    }
    ages = {k: 300.0 for k in kv}

    data, _ = await _run_overview(quotes, kv, ages)
    assert data["vix"] == 16.38
    assert data["vix_is_cache"] is True
    assert data["vix_is_expired"] is False
    assert data["recession_warning"] is False
    assert data["recession_state"] == "UNKNOWN"
    assert data["recession_missing"] == ["VIX"]

    embed = build_market_macro_overview_embed(data)
    text = _embed_text(embed)
    assert "16.38\u001b[0m \u001b[1;33m[快取]\u001b[0m" in text
    assert "⚪ 未知 (資料不足，CC 開倉保守阻斷：VIX)" in text
    assert "🟢 NORMAL (CC 開倉正常)" not in text
    assert int(getattr(embed.color, "value")) == 0xF39C12

    with (
        patch(
            "database.get_kv_cache_fresh",
            side_effect=lambda k, *_a: kv.get(k),
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 4.94},
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await is_covered_call_unlock_allowed() is False


@pytest.mark.asyncio
async def test_expired_us10y_and_sahm_fail_closed_in_overview_and_cc_gate() -> None:
    """macro_us10y 逾 3 天、macro_sahm_rule 逾 45 天皆標記過期並轉為 UNKNOWN / False；
    10 天內的月度 macro_sahm_rule 仍屬有效，可判 NORMAL / True。"""
    # (A) US10Y 4 天前快取 + 即時 VIX 23.81 + Sahm 0.0 -> UNKNOWN (缺 US10Y)
    quotes = {
        "^SPX": {"c": 5900.0},
        "SPY": {"c": 588.0},
        "^VIX": {"c": 23.81},
        "^TNX": {},
        "CL=F": {"c": 72.0},
    }
    kv = {
        "macro_us10y": 4.20,
        "macro_rrp": 0.2,
        "macro_fed_balance": 6.8,
        "macro_fear_greed": 45.0,
        "macro_uer": 4.1,
        "macro_sahm_rule": 0.0,
    }
    ages = {
        "macro_us10y": 4 * _DAY,
        "macro_rrp": 300.0,
        "macro_fed_balance": 300.0,
        "macro_fear_greed": 300.0,
        "macro_uer": 300.0,
        "macro_sahm_rule": 10 * _DAY,
    }

    data, _ = await _run_overview(quotes, kv, ages)
    assert data["us10y_is_cache"] is True
    assert data["us10y_is_expired"] is True
    assert data["sahm_is_expired"] is False
    assert data["recession_state"] == "UNKNOWN"
    assert data["recession_missing"] == ["US10Y"]
    text = _embed_text(build_market_macro_overview_embed(data))
    assert "[4.0 天前快取・不納入判定]" in text
    assert "⚪ 未知 (資料不足，CC 開倉保守阻斷：US10Y)" in text

    # (B) Sahm 48 天前 (> 45d) 且重抓失敗 + 安全 US10Y/VIX -> UNKNOWN (缺薩姆規則)
    quotes_live_tnx = {**quotes, "^TNX": {"c": 4.10}, "^VIX": {"c": 15.0}}
    ages_stale_sahm = {**ages, "macro_sahm_rule": 48 * _DAY}
    data_stale_sahm, _ = await _run_overview(quotes_live_tnx, kv, ages_stale_sahm)
    assert data_stale_sahm["sahm_is_expired"] is True
    assert data_stale_sahm["recession_state"] == "UNKNOWN"
    assert data_stale_sahm["recession_missing"] == ["薩姆規則"]

    # (C) is_covered_call_unlock_allowed 使用真實期限比較模擬過期
    def _fresh_with_age(key: str, max_age: float) -> Any:
        if ages_stale_sahm.get(key, 0.0) > max_age:
            return None
        return kv.get(key)

    with (
        patch("database.get_kv_cache_fresh", side_effect=_fresh_with_age),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=15.0,
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={"_is_fallback": True},
        ),
    ):
        # Sahm 48d (>45d) -> False
        assert await is_covered_call_unlock_allowed() is False

        # Sahm 10d (<=45d) + US10Y 2d (<=3d) -> True
        ages_stale_sahm["macro_sahm_rule"] = 10 * _DAY
        ages_stale_sahm["macro_us10y"] = 2 * _DAY
        assert MACRO_SAHM_CACHE_MAX_AGE_SECONDS == 45 * _DAY
        assert MACRO_US10Y_CACHE_MAX_AGE_SECONDS == 3 * _DAY
        assert await is_covered_call_unlock_allowed() is True


# ---------------------------------------------------------------------------
# 3. F-M4：core_missing 含 sahm_rule 與部分 FRED 失敗不歸零
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_missing_triggers_self_heal_when_only_sahm_is_none() -> None:
    quotes = {
        "^SPX": {"c": 5900.0},
        "SPY": {"c": 588.0},
        "^VIX": {"c": 16.0},
        "^TNX": {"c": 4.1},
        "CL=F": {"c": 72.0},
    }
    kv = {
        "macro_rrp": 0.2,
        "macro_fed_balance": 6.8,
        "macro_fear_greed": 45.0,
        "macro_uer": 4.1,
        "macro_sahm_rule": None,
    }
    ages = {
        "macro_rrp": 300.0,
        "macro_fed_balance": 300.0,
        "macro_fear_greed": 300.0,
        "macro_uer": 300.0,
    }
    live_core = {
        "rrp": 0.2,
        "fed_balance": 6.8,
        "fear_greed": 45.0,
        "uer": 4.1,
        "sahm_rule": 0.12,
        "sahm_rule_date": "2026-10",
    }

    data, mock_fetch = await _run_overview(quotes, kv, ages, core=live_core)
    mock_fetch.assert_awaited_once()
    assert data["sahm_rule"] == 0.12
    assert data["sahm_rule_date"] == "2026-10"
    assert data["recession_state"] == "NORMAL"


@pytest.mark.asyncio
async def test_partial_fred_failure_sets_fallback_flag_and_preserves_sahm_age() -> None:
    import config
    from market_analysis import index_microstructure as im

    def _http_client(payload: dict[str, Any]) -> MagicMock:
        res = MagicMock()
        res.status_code = 200
        res.json = MagicMock(return_value=payload)
        client = AsyncMock()
        client.get = AsyncMock(return_value=res)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    save_partial = AsyncMock()
    save_many_partial = AsyncMock(return_value=True)
    partial_data: dict[str, Any] = {
        "rrp": 0.2,
        "fed_balance": 6.8,
        "fear_greed": 45.0,
        "uer": 4.1,
        "sahm_rule": None,
    }
    partial_payload: dict[str, Any] = {
        "status": "success",
        "data": partial_data,
    }
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", save_partial),
        patch("database.cache.save_kv_cache_many", save_many_partial),
        patch("httpx.AsyncClient", return_value=_http_client(partial_payload)),
    ):
        await im._fetch_core_macro_metrics_uncached()

    saved_partial = {c.args[0]: c.args[1] for c in save_partial.call_args_list}
    assert saved_partial["macro_core_is_fallback"] == 1
    save_many_partial.assert_not_awaited()

    save_full = AsyncMock()
    save_many_full = AsyncMock(return_value=True)
    full_payload: dict[str, Any] = {
        "status": "success",
        "data": {
            **partial_data,
            "sahm_rule": 0.1,
            "sahm_rule_date": "2026-10",
        },
    }
    with (
        patch.object(config, "TUNNEL_URL", "http://mock-tunnel"),
        patch("database.cache.save_kv_cache", save_full),
        patch("database.cache.save_kv_cache_many", save_many_full),
        patch("httpx.AsyncClient", return_value=_http_client(full_payload)),
    ):
        await im._fetch_core_macro_metrics_uncached()

    saved_full = {c.args[0]: c.args[1] for c in save_full.call_args_list}
    assert saved_full["macro_core_is_fallback"] == 0

    # get_macro_overview_data 遇到部分刷新成功（sahm_rule 仍為 None）時，不得將 sahm_cache_age 歸零
    quotes = {
        "^SPX": {"c": 5900.0},
        "SPY": {"c": 588.0},
        "^VIX": {"c": 16.0},
        "^TNX": {"c": 4.1},
        "CL=F": {"c": 72.0},
    }
    kv = {
        "macro_rrp": 0.2,
        "macro_fed_balance": 6.8,
        "macro_fear_greed": 45.0,
        "macro_uer": 4.1,
        "macro_sahm_rule": 0.1,
    }
    ages = {
        "macro_rrp": 4 * _DAY,
        "macro_fed_balance": 4 * _DAY,
        "macro_fear_greed": 4 * _DAY,
        "macro_uer": 4 * _DAY,
        "macro_sahm_rule": 48 * _DAY,
    }
    data, _ = await _run_overview(quotes, kv, ages, core=partial_data)
    assert data["sahm_cache_age_seconds"] == 48 * _DAY
    assert data["sahm_is_expired"] is True
    assert data["core_is_expired"] is True
    assert data["recession_state"] == "UNKNOWN"
    assert data["recession_missing"] == ["薩姆規則"]


# ---------------------------------------------------------------------------
# 4. Battery 6 稽查邏輯與 evaluate_recession_warning 一致性
# ---------------------------------------------------------------------------


def test_audit_battery6_matches_expiry_gated_recession_logic() -> None:
    # VIX 來自快取 -> Battery 6 預期 UNKNOWN（即使數值 > 20）
    st_vix_cache = _expected_recession_for_audit(
        {
            "sahm_rule": 0.0,
            "us10y": 4.94,
            "vix": 23.81,
            "vix_is_cache": True,
            "us10y_is_expired": False,
            "sahm_is_expired": False,
        }
    )
    assert st_vix_cache.state == "UNKNOWN"
    assert st_vix_cache.is_warning is False

    # US10Y 過期 -> Battery 6 預期 UNKNOWN
    st_us10y_exp = _expected_recession_for_audit(
        {
            "sahm_rule": 0.0,
            "us10y": 4.94,
            "vix": 23.81,
            "vix_is_cache": False,
            "us10y_is_expired": True,
            "sahm_is_expired": False,
        }
    )
    assert st_us10y_exp.state == "UNKNOWN"

    # 均新鮮且觸發 -> Battery 6 預期 WARNING 與完整歸因
    st_warn = _expected_recession_for_audit(
        {
            "sahm_rule": 0.0,
            "us10y": 4.94,
            "vix": 23.81,
            "vix_is_cache": False,
            "us10y_is_expired": False,
            "sahm_is_expired": False,
        }
    )
    assert st_warn.state == "WARNING"
    assert st_warn.is_warning is True
    assert st_warn.triggers == ("US10Y 4.94% > 4.5% & VIX 23.81 > 20",)
