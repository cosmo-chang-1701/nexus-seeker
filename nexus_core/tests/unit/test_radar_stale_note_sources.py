"""雷達 🕓 註記逐來源顯示該來源自身年齡（GEX、UOA）或「MP 降級」。"""

import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.embed_builders._radar_freshness import assess_radar_row
from cogs.embed_builders.market_embeds import build_radar_scan_embed
from cogs.unified_terminal.cog import UnifiedTerminalCog
from tests.unit.test_radar_fast_path_freshness import _rows, _run_fast

# 圖例欄位 value（含 ansi 標記、不含 PutWall 代理註腳）的基準長度，不得放寬。
_LEGEND_VALUE_MAX_LEN = 310


def _scan_row(
    symbol: str = "TEST",
    *,
    mp: Optional[dict[str, Any]] = None,
    gex_stale: bool = False,
    premarket: bool = False,
    uoa_age: Optional[float] = None,
    gex_age: Optional[float] = None,
) -> dict[str, Any]:
    iv: dict[str, Any] = {"iv_rank": 40.0, "expected_move_weekly": 3.0}
    if premarket:
        iv["is_premarket"] = True
    row: dict[str, Any] = {
        "symbol": symbol,
        "quote": {"c": 100.0, "dp": 0.0},
        "iv_metrics": iv,
        "skew": 0.0,
        "max_pain": {
            "max_pain": 100.0,
            "distance_pct": 0.0,
            "is_stale": False,
            "calculation_mode": "OI",
            "is_degraded": False,
            "circuit_breaker_triggered": False,
            **(mp or {}),
        },
        "gex_metrics": {
            "put_wall": 80.0,
            "call_wall": 120.0,
            "net_gex": 1_000_000.0,
            "_is_stale_cache": gex_stale,
        },
    }
    if uoa_age is not None:
        row["uoa_age_seconds"] = uoa_age
    if gex_age is not None:
        row["_freshness"] = {"gex_age_seconds": gex_age}
    return row


def _utc_str(age_seconds: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _field_values(embeds: list[Any], keyword: str) -> str:
    return "\n".join(
        str(f.value) for e in embeds for f in e.fields if keyword in (f.name or "")
    )


def _freshness_lines(row: dict[str, Any]) -> str:
    embeds = build_radar_scan_embed([row], "WATCHLIST", 12345)
    insights = _field_values(embeds, "即時聯動警示")
    return "\n".join(
        ln for ln in insights.splitlines() if ln.startswith(("• 🕓", "• 🌙"))
    )


# ── (h) 前綴與標題後綴（期望值於改動前的程式上即成立） ─────────────

_PREFIX_CASES: list[tuple[str, dict[str, Any], str, bool]] = [
    ("新鮮", _scan_row(), "", False),
    ("MP stale", _scan_row(mp={"is_stale": True}), "🕓", True),
    ("MP degraded", _scan_row(mp={"is_degraded": True}), "🕓", True),
    ("MP Volume", _scan_row(mp={"calculation_mode": "Volume"}), "🕓", True),
    ("僅 GEX", _scan_row(gex_stale=True), "🕓", True),
    ("僅 UOA", _scan_row(uoa_age=7200.0), "🕓", True),
    ("僅斷路器", _scan_row(mp={"circuit_breaker_triggered": True}), "", False),
    ("盤前僅 GEX", _scan_row(gex_stale=True, premarket=True), "🌙", False),
    (
        "盤前加 MP 降級",
        _scan_row(gex_stale=True, premarket=True, mp={"is_degraded": True}),
        "🕓",
        True,
    ),
    (
        "固定收益帶 MP 旗標",
        _scan_row(
            "BOXX",
            mp={
                "is_stale": True,
                "is_degraded": True,
                "calculation_mode": "Volume",
                "circuit_breaker_triggered": True,
            },
        ),
        "",
        False,
    ),
    (
        "缺鍵",
        {"symbol": "BARE", "quote": {"c": 100.0}, "max_pain": {}},
        "",
        False,
    ),
]


@pytest.mark.parametrize(
    ("row", "prefix", "degraded"),
    [case[1:] for case in _PREFIX_CASES],
    ids=[case[0] for case in _PREFIX_CASES],
)
def test_h_row_prefix_and_title_suffix(
    row: dict[str, Any], prefix: str, degraded: bool
) -> None:
    embeds = build_radar_scan_embed([row], "WATCHLIST", 12345)
    sym = row["symbol"]
    table = _field_values(embeds, "核心 AI")
    cell = next(
        ln.split("|")[1].strip()
        for ln in table.splitlines()
        if ln.startswith("|") and sym in ln.split("|")[1]
    )
    marks = "".join(m for m in ("🕓", "🌙") if m in cell)
    assert marks == prefix
    assert ("[快取/降級]" in str(embeds[0].title)) is degraded


# ── (a)–(d) 逐來源年齡與判定 ──────────────────────────────────


def test_a_gex_only_stale_shows_gex_age_not_mp_age() -> None:
    row = _scan_row(gex_stale=True, gex_age=2400.0, mp={"updated_at": _utc_str(3600.0)})
    line = _freshness_lines(row)
    assert "TEST GEX 40分鐘前" in line
    assert "1小時前" not in line


def test_a2_gex_stale_without_age_shows_unknown() -> None:
    row = _scan_row(gex_stale=True, mp={"updated_at": _utc_str(3600.0)})
    line = _freshness_lines(row)
    assert "TEST GEX 時間不明" in line
    assert "1小時前" not in line


def test_b_mp_stale_shows_degraded_without_age() -> None:
    row = _scan_row(mp={"is_stale": True, "updated_at": _utc_str(3600.0)})
    line = _freshness_lines(row)
    assert "TEST MP 降級" in line
    assert "小時前" not in line
    assert "分鐘前" not in line


def test_c_uoa_age_labelled() -> None:
    line = _freshness_lines(_scan_row(uoa_age=7200.0))
    assert "TEST UOA 2小時前" in line


def test_c2_multiple_sources_joined_in_order() -> None:
    row = _scan_row(
        gex_stale=True, gex_age=3120.0, uoa_age=7200.0, mp={"is_degraded": True}
    )
    assert assess_radar_row(row).compact_note() == ("GEX 52分鐘前/UOA 2小時前/MP 降級")


def test_d_assess_fresh_and_benign_premarket() -> None:
    fresh = assess_radar_row(_scan_row())
    assert fresh.is_stale is False
    assert fresh.is_benign_premarket is False

    benign = assess_radar_row(_scan_row(gex_stale=True, premarket=True))
    assert benign.is_stale is True
    assert benign.is_benign_premarket is True

    breaker = assess_radar_row(_scan_row(mp={"circuit_breaker_triggered": True}))
    assert breaker.is_stale is False
    assert breaker.is_genuine_degradation is True


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"symbol": None, "gex_metrics": None, "max_pain": None, "_freshness": None},
        {"gex_metrics": {}, "max_pain": 100.0, "iv_metrics": {}},
    ],
)
def test_d2_assess_tolerates_missing_keys(row: dict[str, Any]) -> None:
    assert assess_radar_row(row).is_stale is False


# ── (e) 快速路徑 GEX 年齡 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_e_fast_path_gex_age_from_kv_data_timestamp() -> None:
    now = time.time()
    envelope = {
        "data": {"put_wall": 140.0, "call_wall": 160.0, "net_gex": 5.0},
        "timestamp": now,
        "data_timestamp": now - 2700.0,
    }
    data = await _run_fast(_rows(None, extra={"gex_metrics_NVDA": envelope}), {})
    assert data["_freshness"]["gex_age_seconds"] == pytest.approx(2700.0, abs=5)
    assert data["gex_metrics"]["_is_stale_cache"] is True


@pytest.mark.asyncio
async def test_e2_fast_path_gex_age_from_snapshot_fallback() -> None:
    data = await _run_fast(
        _rows({"put_wall_strike": 140.0}, radar_age_s=7200.0),
        {},
    )
    assert data["_freshness"]["gex_age_seconds"] == pytest.approx(7200.0, abs=5)


@pytest.mark.asyncio
async def test_e3_fast_path_gex_age_unknown() -> None:
    legacy = {
        "data": {"put_wall": 140.0, "net_gex": 5.0, "_is_stale_cache": True},
        "data_timestamp": time.time(),
    }
    data = await _run_fast(_rows(None, extra={"gex_metrics_NVDA": legacy}), {})
    assert data["_freshness"]["gex_age_seconds"] is None

    no_source = await _run_fast(_rows(None), {})
    assert no_source["_freshness"]["gex_age_seconds"] is None


# ── (f) 慢速路徑 ─────────────────────────────────────────────


async def _run_slow_with_expiries(
    gex_data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], AsyncMock, MagicMock]:
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

    far_expiry = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    se = "market_analysis.sentiment_engine.SentimentEngine."
    returns: dict[str, Any] = {
        "services.market_data_service.get_quote": {"c": 100.0, "volume": 1000},
        "services.market_data_service.get_history_df": df,
        "services.market_data_service.get_all_option_expiries": [far_expiry],
        "database.squeeze_cache.get_squeeze_cache": None,
        "market_analysis.psq_engine.analyze_psq": None,
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics": gex_data,
        "market_analysis.atr_utils.fetch_atr_15m": None,
        se + "detect_uoa_with_physical_caps": ([], []),
        se + "calculate_pcr": {},
        se + "calculate_skew": {"skew": None},
        se + "fetch_and_calculate_iv_metrics": None,
        se + "get_expected_move": {},
        "database.market_cache.get_market_cache": {},
        "database.cache.get_kv_cache_with_age": (None, 0.0),
        "database.cache.get_kv_cache_session_fresh": None,
    }
    with ExitStack() as st:
        for target, value in returns.items():
            st.enter_context(patch(target, return_value=value))
        calc_mp = st.enter_context(
            patch(
                "market_analysis.sentiment.max_pain.calculate_max_pain",
                new=AsyncMock(return_value={"max_pain": 130.0}),
            )
        )
        unified_mp = st.enter_context(
            patch(se + "get_unified_max_pain", return_value={"max_pain": 100.0})
        )
        st.enter_context(
            patch("database.market_cache.save_wall_levels", new=AsyncMock())
        )
        st.enter_context(patch("database.cache.save_kv_cache", side_effect=_save_kv))
        result = await cog._fetch_sym_radar_data_slow_raw("NVDA")
    return result, saved, calc_mp, unified_mp


@pytest.mark.asyncio
async def test_f_slow_path_skips_far_max_pain() -> None:
    result, saved, calc_mp, unified_mp = await _run_slow_with_expiries(
        {"put_wall": 90.0, "call_wall": 110.0}
    )
    calc_mp.assert_not_called()
    for call in unified_mp.call_args_list:
        assert call.args == ("NVDA",)
        assert "expiry" not in call.kwargs
    assert "mp_far" not in saved["radar_terminal_NVDA"]
    assert "nearest_dte" not in result
    assert result["_freshness"]["gex_age_seconds"] is None


@pytest.mark.asyncio
async def test_f2_slow_path_gex_age_from_fallback_data_ts() -> None:
    result, _, _, _ = await _run_slow_with_expiries(
        {
            "put_wall": 90.0,
            "call_wall": 110.0,
            "_is_stale_cache": True,
            "_data_ts": time.time() - 3000.0,
        }
    )
    assert result["_freshness"]["gex_age_seconds"] == pytest.approx(3000.0, abs=5)


# ── (g) 圖例 ─────────────────────────────────────────────────


def test_g_legend_direction_and_length() -> None:
    embeds = build_radar_scan_embed([_scan_row()], "WATCHLIST", 12345)
    legend = _field_values(embeds, "雷達圖例")
    assert "上方" in legend
    assert "下方即時" not in legend
    assert "🌙" in legend
    assert "依來源標示年齡；盤後 GEX 常無新資料" in legend
    assert "PutWall 後綴" not in legend
    assert len(legend) <= _LEGEND_VALUE_MAX_LEN
