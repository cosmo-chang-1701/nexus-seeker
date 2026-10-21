"""雷達 fast path 的 Max Pain／新鮮度真實性，與 slow path 的 force_refresh。"""

import threading
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_data import _KvSnapshot

_SQZ = {"momentum": 1.0, "direction": "🟢", "is_squeezing": False}


def _rows(
    radar: Optional[dict[str, Any]] = None,
    radar_age_s: Optional[float] = 60.0,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, tuple[Any, Optional[datetime]]]:
    now = datetime.now(timezone.utc)
    rows: dict[str, tuple[Any, Optional[datetime]]] = {
        "uoa_NVDA": ([{"strike": 100}], now),
    }
    if radar is not None:
        rows["radar_terminal_NVDA"] = (
            radar,
            now - timedelta(seconds=radar_age_s) if radar_age_s is not None else None,
        )
    for k, v in (extra or {}).items():
        rows[k] = (v, now)
    return rows


async def _run_fast(
    rows: dict[str, tuple[Any, Optional[datetime]]],
    market_cache: Optional[dict[str, Any]] = None,
    session_fresh: Any = None,
    history_side_effects: Optional[dict[str, Any]] = None,
) -> Any:
    cog = UnifiedTerminalCog(MagicMock())
    cog._async_revalidate_market_cache = AsyncMock()  # type: ignore[method-assign]
    he = history_side_effects or {}
    with ExitStack() as st:
        st.enter_context(
            patch(
                "services.market_data_service.get_quote",
                return_value={"c": 150.0, "volume": 1000},
            )
        )
        st.enter_context(
            patch(
                "cogs.unified_terminal.radar_data._load_symbol_caches",
                return_value=(_KvSnapshot(rows), market_cache, dict(_SQZ)),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_last_stored_iv",
                side_effect=he.get("iv", lambda s: None),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_last_stored_sentiment",
                side_effect=he.get("sent", lambda s, i: None),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_indicator_percentile_detail",
                side_effect=he.get("pct", lambda s, i, v: None),
            )
        )
        if session_fresh is not None:
            st.enter_context(
                patch("database.cache.session_fresh_value", side_effect=session_fresh)
            )
        return await cog._fetch_sym_radar_data_fast_raw("NVDA")


@pytest.mark.asyncio
async def test_a_no_known_expiry_yields_no_month_max_pain() -> None:
    data = await _run_fast(
        _rows({"mp_near": 120.0, "mp_far": 130.0}),
        {"max_pain": 120.0, "expiry": "WEEKLY"},
    )
    assert data["month_max_pains"] == []
    assert data["max_pain"]["expiry"] is None


@pytest.mark.asyncio
async def test_b_known_expiry_yields_single_entry_without_far() -> None:
    data = await _run_fast(
        _rows({"mp_near": 120.0, "mp_far": 130.0, "mp_near_expiry": "2026-10-23"}),
        {"max_pain": 120.0, "expiry": "WEEKLY"},
    )
    assert data["month_max_pains"] == [{"expiry": "2026-10-23", "max_pain": 120.0}]
    assert data["max_pain"]["expiry"] == "2026-10-23"


@pytest.mark.asyncio
async def test_c_market_cache_max_pain_preferred_over_snapshot() -> None:
    data = await _run_fast(
        _rows({"mp_near": 120.0, "mp_near_expiry": "2026-10-23"}),
        {"max_pain": 150.0, "expiry": "WEEKLY"},
    )
    assert data["max_pain"]["max_pain"] == 150.0
    # 數值與快照不同，不得沿用快照的到期日
    assert data["max_pain"]["expiry"] is None
    assert data["month_max_pains"] == []


@pytest.mark.asyncio
async def test_c2_market_cache_real_expiry_is_used() -> None:
    data = await _run_fast(
        _rows({"mp_near": 120.0}), {"max_pain": 150.0, "expiry": "2026-10-30"}
    )
    assert data["month_max_pains"] == [{"expiry": "2026-10-30", "max_pain": 150.0}]


@pytest.mark.asyncio
async def test_d_uoa_fallback_carries_snapshot_age() -> None:
    rows = _rows({"uoa": [{"strike": 1}]}, radar_age_s=7200.0)
    del rows["uoa_NVDA"]
    data = await _run_fast(rows, {})
    assert data["uoa_age_seconds"] == pytest.approx(7200.0, abs=5)
    assert data["_freshness"]["uoa_age_seconds"] == pytest.approx(7200.0, abs=5)
    assert data["_freshness"]["is_any_stale"] is True


@pytest.mark.asyncio
async def test_e_walls_from_old_snapshot_are_stale() -> None:
    data = await _run_fast(
        _rows(
            {"put_wall_strike": 140.0, "call_wall_strike": 160.0, "net_gex": 5.0},
            radar_age_s=7200.0,
        ),
        {},
    )
    assert data["gex_metrics"]["put_wall"] == 140.0
    assert data["gex_metrics"]["_is_stale_cache"] is True
    assert data["gex_profile_data"]["_is_stale_cache"] is True


@pytest.mark.asyncio
async def test_e2_walls_from_fresh_snapshot_not_stale() -> None:
    data = await _run_fast(_rows({"put_wall_strike": 140.0}, radar_age_s=60.0), {})
    assert data["gex_metrics"]["_is_stale_cache"] is False


@pytest.mark.asyncio
async def test_f_stale_snapshot_hvn_not_used() -> None:
    def _sf(value: Any, updated_at: Any) -> Any:
        # 快照（dict）視為非當日有效；volume_poc 鍵（數值）有效
        return None if isinstance(value, dict) else value

    data = await _run_fast(
        _rows({"hvn_price": 111.0}, extra={"volume_poc_NVDA": 123.0}),
        {},
        session_fresh=_sf,
    )
    assert data["volume_poc"] == 123.0
    assert data["vp_data"]["hvn"] == 123.0


@pytest.mark.asyncio
async def test_f2_fresh_snapshot_hvn_used() -> None:
    data = await _run_fast(
        _rows({"hvn_price": 111.0}, extra={"volume_poc_NVDA": 123.0}),
        {},
        session_fresh=lambda v, t: v,
    )
    assert data["volume_poc"] == 111.0


@pytest.mark.asyncio
async def test_g_radar_age_seconds_exposed() -> None:
    data = await _run_fast(_rows({"mp_near": 1.0}, radar_age_s=900.0), {})
    assert data["_freshness"]["radar_age_seconds"] == pytest.approx(900.0, abs=5)
    assert data["_freshness"]["oldest_age_seconds"] >= 890

    missing = await _run_fast(_rows(None), {})
    assert missing["_freshness"]["radar_age_seconds"] is None

    no_ts = await _run_fast(_rows({"mp_near": 1.0}, radar_age_s=None), {})
    assert no_ts["_freshness"]["radar_age_seconds"] is None


@pytest.mark.asyncio
async def test_j_history_reads_run_off_event_loop() -> None:
    on_loop: list[bool] = []

    def _rec(ret: Any) -> Any:
        def _f(*a: Any) -> Any:
            on_loop.append(threading.current_thread() is threading.main_thread())
            return ret

        return _f

    await _run_fast(
        _rows({"mp_near": 1.0}),
        {},
        history_side_effects={
            "iv": _rec(0.3),
            "sent": _rec(-0.2),
            "pct": _rec(None),
        },
    )
    # last_iv、skew、skew percentile、PCR 共四次
    assert len(on_loop) == 4
    assert not any(on_loop)


# ── slow path ────────────────────────────────────────────────────


async def _run_slow(
    mp_data: dict[str, Any],
    force_refresh: bool = False,
    squeeze_row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cog = UnifiedTerminalCog(MagicMock())
    cog._async_revalidate_market_cache = AsyncMock()  # type: ignore[method-assign]
    df = pd.DataFrame(
        {
            "Close": [100.0] * 30,
            "High": [105.0] * 30,
            "Low": [95.0] * 30,
            "Volume": [1000.0] * 30,
        }
    )
    saved: dict[str, Any] = {}

    async def _save_kv(k: str, v: Any) -> None:
        saved[k] = v

    se = "market_analysis.sentiment_engine.SentimentEngine."
    with ExitStack() as st:
        mocks: dict[str, Any] = {}
        st.enter_context(
            patch(
                "services.market_data_service.get_quote",
                return_value={"c": 100.0, "volume": 1000},
            )
        )
        st.enter_context(
            patch("services.market_data_service.get_history_df", return_value=df)
        )
        st.enter_context(
            patch(
                "services.market_data_service.get_all_option_expiries",
                return_value=[],
            )
        )
        st.enter_context(
            patch("database.squeeze_cache.get_squeeze_cache", return_value=squeeze_row)
        )
        mocks["save_sqz"] = st.enter_context(
            patch("database.squeeze_cache.save_squeeze_cache", new=AsyncMock())
        )
        mocks["psq"] = st.enter_context(
            patch("market_analysis.psq_engine.analyze_psq", return_value=None)
        )
        st.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
                return_value={"put_wall": 90.0, "call_wall": 110.0},
            )
        )
        st.enter_context(
            patch("market_analysis.atr_utils.fetch_atr_15m", return_value=None)
        )
        st.enter_context(
            patch(se + "detect_uoa_with_physical_caps", return_value=([], []))
        )
        st.enter_context(patch(se + "calculate_pcr", return_value={}))
        st.enter_context(patch(se + "calculate_skew", return_value={"skew": None}))
        mocks["iv"] = st.enter_context(
            patch(se + "fetch_and_calculate_iv_metrics", return_value=None)
        )
        st.enter_context(patch(se + "get_expected_move", return_value={}))
        mocks["mp"] = st.enter_context(
            patch(se + "get_unified_max_pain", return_value=mp_data)
        )
        mocks["save_mc"] = st.enter_context(
            patch("database.market_cache.save_market_cache", new=AsyncMock())
        )
        st.enter_context(
            patch("database.market_cache.get_market_cache", return_value={})
        )
        st.enter_context(patch("database.cache.save_kv_cache", side_effect=_save_kv))
        st.enter_context(
            patch("database.cache.get_kv_cache_with_age", return_value=(None, 0.0))
        )
        st.enter_context(
            patch("database.cache.get_kv_cache_session_fresh", return_value=None)
        )
        await cog._fetch_sym_radar_data_slow_raw("NVDA", force_refresh=force_refresh)
    return {"saved": saved, **mocks}


@pytest.mark.asyncio
async def test_h_slow_path_forwards_max_pain_flags_and_expiry_in_snapshot() -> None:
    r = await _run_slow(
        {
            "max_pain": 100.0,
            "expiry": "2026-10-23",
            "is_stale": True,
            "is_degraded": True,
            "calculation_mode": "Volume",
        }
    )
    kwargs = r["save_mc"].call_args.kwargs
    assert kwargs["is_stale"] == 1
    assert kwargs["is_degraded"] == 1
    assert kwargs["calculation_mode"] == "Volume"
    assert kwargs["circuit_breaker_triggered"] == 0
    assert "expiry" not in kwargs
    assert r["saved"]["radar_terminal_NVDA"]["mp_near_expiry"] == "2026-10-23"


@pytest.mark.asyncio
async def test_i_slow_path_weekly_expiry_stored_as_none() -> None:
    r = await _run_slow({"max_pain": 100.0, "expiry": "WEEKLY"})
    assert r["saved"]["radar_terminal_NVDA"]["mp_near_expiry"] is None
    kwargs = r["save_mc"].call_args.kwargs
    assert kwargs["is_stale"] == 0
    assert kwargs["calculation_mode"] == "OI"


@pytest.mark.asyncio
async def test_k_force_refresh_forces_recompute() -> None:
    sqz = {"momentum": 1.0, "direction": "🟢", "is_squeezing": False}
    forced = await _run_slow({"max_pain": 100.0}, force_refresh=True, squeeze_row=sqz)
    forced["mp"].assert_awaited_once_with("NVDA", force_refresh=True)
    forced["iv"].assert_awaited_once_with("NVDA", force_refresh=True)
    forced["psq"].assert_called()

    default = await _run_slow({"max_pain": 100.0}, squeeze_row=sqz)
    default["mp"].assert_awaited_once_with("NVDA")
    default["iv"].assert_awaited_once_with("NVDA")
    default["psq"].assert_not_called()


@pytest.mark.asyncio
async def test_k2_force_refresh_uses_distinct_single_flight_key() -> None:
    cog = UnifiedTerminalCog(MagicMock())
    keys: list[str] = []

    async def _run(key: str, fn: Any, *a: Any, **kw: Any) -> str:
        keys.append(key)
        return "ok"

    with patch("services.single_flight.SingleFlightManager.run", side_effect=_run):
        await cog._fetch_sym_radar_data_slow("NVDA")
        await cog._fetch_sym_radar_data_slow("NVDA", force_refresh=True)
    assert len(set(keys)) == 2
