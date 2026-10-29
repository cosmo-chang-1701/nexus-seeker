"""擠壓進場影子否決：原始值與旗標只寫前向紀錄，不得影響判定（欄位契約見規格）。"""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis import evaluation_recorder as rec
from market_analysis.squeeze_entry import shadow_vetoes as sv

_TODAY = date(2026, 10, 29)
_NAV = "market_analysis.squeeze_entry.shadow_vetoes.nearest_macro_event_tte_hours"


@pytest.fixture(autouse=True)
def _clean_buffer() -> Iterator[None]:
    rec._BUFFER.clear()
    yield
    rec._BUFFER.clear()


def _calc(
    spot: float = 100.0,
    radar: Optional[dict[str, Any]] = None,
    atr: Any = 2.0,
    stop: Any = 95.0,
    skew: Any = None,
    mev: Optional[float] = None,
    cal_ok: bool = True,
) -> dict[str, Any]:
    return sv.compute_shadow_vetoes(spot, radar, atr, stop, skew, mev, cal_ok, _TODAY)


def _radar(**gex: Any) -> dict[str, Any]:
    return {"gex_profile_data": dict(gex)}


# ------------------------------------------------------------- 原始值
def test_all_keys_present_for_empty_radar() -> None:
    out = _calc(radar={}, atr=None, stop=None, cal_ok=False)
    assert set(out) == set(sv.SHADOW_KEYS)
    assert out["sv_any"] is None
    assert out["sv_f_mev"] is None


def test_wall_values_hand_computed() -> None:
    radar = _radar(call_wall=104.0, put_wall=95.0)
    radar["overhead_neg_gex_swamp"] = (102.0, -5e6)
    out = _calc(radar=radar)
    assert out["sv_cw"] == 104.0 and out["sv_cw_atr"] == 2.0
    assert out["sv_sw"] == 102.0 and out["sv_sw_atr"] == 1.0
    # T = min(104, 102) = 102；L = 95 - 0.5*2 = 94；rr = 2/6
    assert out["sv_rr"] == 0.33
    assert out["sv_wstop_pct"] == 6.0
    assert out["sv_stop_pct"] == 5.0
    assert out["sv_f_ceil"] is True  # sw_atr 1.0 <= 1.5
    assert out["sv_f_rr"] is True
    assert out["sv_f_wide"] is False


def test_rr_pass_case() -> None:
    out = _calc(radar=_radar(call_wall=115.0, put_wall=95.0))
    assert out["sv_rr"] == 2.5  # 15 / 6
    assert out["sv_f_rr"] is False
    assert out["sv_f_ceil"] is False


def test_walls_on_wrong_side_are_null() -> None:
    out = _calc(radar=_radar(call_wall=100.0, put_wall=100.0))
    assert out["sv_cw"] is None and out["sv_pw"] is None
    out = _calc(radar=_radar(call_wall=90.0, put_wall=105.0))
    assert out["sv_cw"] is None and out["sv_pw"] is None
    assert out["sv_rr"] is None and out["sv_f_rr"] is None


def test_swamp_list_or_tuple() -> None:
    for raw in ((103.0, -1.0), [103.0, -1.0]):
        radar = _radar()
        radar["overhead_neg_gex_swamp"] = raw
        assert _calc(radar=radar)["sv_sw"] == 103.0
    for raw in ((0.0, 0.0), [0.0, 0.0]):
        radar = _radar()
        radar["overhead_neg_gex_swamp"] = raw
        out = _calc(radar=radar)
        assert out["sv_sw"] is None and out["sv_sw_atr"] is None
    # 泥淖在現價之下：視為無
    radar = _radar()
    radar["overhead_neg_gex_swamp"] = (99.0, -1.0)
    assert _calc(radar=radar)["sv_sw"] is None


def test_rr_needs_target_and_stop() -> None:
    assert _calc(radar=_radar(put_wall=95.0))["sv_rr"] is None
    out = _calc(radar=_radar(put_wall=95.0))
    assert out["sv_wstop_pct"] == 6.0
    assert _calc(radar=_radar(call_wall=110.0))["sv_wstop_pct"] is None
    assert (
        _calc(radar=_radar(call_wall=110.0, put_wall=95.0), atr=None)["sv_rr"] is None
    )


def test_wall_stop_above_spot_gives_null_rr() -> None:
    # atr 為負視為缺值，不得產生盈虧比
    out = sv.compute_shadow_vetoes(
        100.0,
        _radar(call_wall=110.0, put_wall=99.0),
        -1.0,
        None,
        None,
        None,
        True,
        _TODAY,
    )
    assert out["sv_rr"] is None


def test_stop_pct_null_when_stop_missing_or_above() -> None:
    assert _calc(stop=None)["sv_stop_pct"] is None
    assert _calc(stop=100.0)["sv_stop_pct"] is None
    assert _calc(stop=120.0)["sv_stop_pct"] is None
    out = _calc(stop=85.0)
    assert out["sv_stop_pct"] == 15.0 and out["sv_f_wide"] is True


def test_wide_from_wall_stop() -> None:
    out = _calc(radar=_radar(call_wall=130.0, put_wall=80.0), stop=99.0)
    # L = 79 -> 21%
    assert out["sv_wstop_pct"] == 21.0
    assert out["sv_f_wide"] is True


def test_ngex_sign() -> None:
    assert _calc(radar=_radar(net_gex=5e6))["sv_ngex"] == 1
    assert _calc(radar=_radar(net_gex=-5e6))["sv_ngex"] == -1
    assert _calc(radar=_radar(net_gex=0))["sv_ngex"] is None
    assert _calc(radar=_radar())["sv_ngex"] is None


def test_gex_stale_flag() -> None:
    assert _calc(radar=_radar(_is_stale_cache=True))["sv_gex_stale"] is True
    assert _calc(radar=_radar(_is_stale_cache=False))["sv_gex_stale"] is False
    assert _calc(radar={})["sv_gex_stale"] is None


def test_mp_dist_recomputed() -> None:
    mp = {"max_pain": 95.0, "expiry": "2026-10-30", "distance_pct": 0.0}
    out = _calc(radar={"max_pain": mp})
    assert out["sv_mp_dist"] == 5.26  # (100-95)/95
    # radar 的 distance_pct 為 0.0 但 MP 無效 -> None
    for bad in (
        {"max_pain": 0.0, "expiry": "2026-10-30", "distance_pct": 0.0},
        {"max_pain": None, "expiry": "2026-10-30", "distance_pct": 0.0},
        {"max_pain": 95.0, "expiry": "2026-10-30", "is_stale": True},
    ):
        assert _calc(radar={"max_pain": bad})["sv_mp_dist"] is None
    assert _calc(spot=0.0, radar={"max_pain": mp})["sv_mp_dist"] is None


def test_zero_values_kept() -> None:
    mp = {"max_pain": 100.0, "expiry": "2026-10-29"}
    out = _calc(radar={"max_pain": mp}, mev=0.0)
    assert out["sv_mp_dte"] == 0
    assert out["sv_mp_dist"] == 0.0
    assert out["sv_mev_h"] == 0.0
    assert out["sv_f_mev"] is True


def test_mp_dte_et_days() -> None:
    assert _calc(radar={"max_pain": {"expiry": "2026-10-31"}})["sv_mp_dte"] == 2
    assert _calc(radar={"max_pain": {"expiry": "2026-10-28"}})["sv_mp_dte"] == -1
    assert _calc(radar={"max_pain": {"expiry": "garbage"}})["sv_mp_dte"] is None
    assert _calc(radar={"max_pain": {}})["sv_mp_dte"] is None


@pytest.mark.parametrize(
    "radar",
    [
        {"gex_profile_data": "x"},
        {"gex_profile_data": {"call_wall": "a", "put_wall": [], "net_gex": {}}},
        {"overhead_neg_gex_swamp": "abc"},
        {"overhead_neg_gex_swamp": (1.0,)},
        {"overhead_neg_gex_swamp": ("a", "b")},
        {"max_pain": "x"},
        {"max_pain": {"max_pain": "a", "expiry": 5}},
        {"gex_profile_data": {"call_wall": float("nan"), "put_wall": float("inf")}},
        None,
    ],
)
def test_bad_radar_types_do_not_raise(radar: Any) -> None:
    out = sv.compute_shadow_vetoes(100.0, radar, 2.0, None, "x", None, True, _TODAY)
    assert set(out) == set(sv.SHADOW_KEYS)
    assert out["sv_cw"] is None and out["sv_pw"] is None
    assert out["sv_mp_dist"] is None


# ------------------------------------------------------------- 旗標三值
def test_flag_ceil_three_values() -> None:
    assert _calc(radar=_radar(call_wall=102.0))["sv_f_ceil"] is True  # 1.0 atr
    assert _calc(radar=_radar(call_wall=103.0))["sv_f_ceil"] is True  # 1.5 邊界
    assert _calc(radar=_radar(call_wall=110.0))["sv_f_ceil"] is False
    assert _calc(radar={})["sv_f_ceil"] is None


def test_flag_skew_three_values() -> None:
    assert _calc(skew=80.0)["sv_f_skew"] is True
    assert _calc(skew=79.9)["sv_f_skew"] is False
    assert _calc(skew=None)["sv_f_skew"] is None
    assert _calc(skew=0.0)["sv_f_skew"] is False


def test_flag_mp_three_values() -> None:
    gex = {"net_gex": 1e6}
    base = {"max_pain": 95.0, "expiry": "2026-10-30"}
    assert _calc(radar={"gex_profile_data": gex, "max_pain": base})["sv_f_mp"] is True
    far = {**base, "expiry": "2026-11-20"}
    assert _calc(radar={"gex_profile_data": gex, "max_pain": far})["sv_f_mp"] is False
    neg = {"net_gex": -1e6}
    assert _calc(radar={"gex_profile_data": neg, "max_pain": base})["sv_f_mp"] is False
    near = {**base, "max_pain": 99.0}
    assert _calc(radar={"gex_profile_data": gex, "max_pain": near})["sv_f_mp"] is False
    assert _calc(radar={"max_pain": base})["sv_f_mp"] is None
    assert _calc(radar={"gex_profile_data": gex})["sv_f_mp"] is None


def test_flag_wide_three_values() -> None:
    assert _calc(stop=85.0)["sv_f_wide"] is True
    assert _calc(stop=95.0)["sv_f_wide"] is False
    assert _calc(stop=None)["sv_f_wide"] is None


def test_flag_mev_values() -> None:
    assert _calc(mev=10.0)["sv_f_mev"] is True
    assert _calc(mev=-2.0)["sv_f_mev"] is True
    assert _calc(mev=48.0)["sv_f_mev"] is True
    assert _calc(mev=48.5)["sv_f_mev"] is False
    assert _calc(mev=None, cal_ok=True)["sv_f_mev"] is False
    assert _calc(mev=None, cal_ok=False)["sv_f_mev"] is None
    assert _calc(mev=5.0, cal_ok=False)["sv_f_mev"] is None


def test_sv_any_three_values() -> None:
    # 全 false：需要六旗標皆有值
    radar = {
        "gex_profile_data": {"call_wall": 120.0, "put_wall": 95.0, "net_gex": -1.0},
        "max_pain": {"max_pain": 99.0, "expiry": "2026-11-30"},
    }
    out = _calc(radar=radar, skew=10.0, mev=None, cal_ok=True)
    assert [out[k] for k in sv._FLAG_KEYS] == [False] * 6
    assert out["sv_any"] is False
    out = _calc(radar=radar, skew=90.0)
    assert out["sv_any"] is True
    # 有 true 也有 null -> true
    assert _calc(radar={}, skew=90.0)["sv_any"] is True
    # 無 true 但有 null -> null
    assert _calc(radar={}, skew=10.0, stop=None, cal_ok=False)["sv_any"] is None


# ------------------------------------------------------------- 日曆輔助
def _cal_events(*events: Any) -> Any:
    from services.calendar_service import EconomicEventList

    lst = EconomicEventList()
    lst.extend(events)
    return lst


def _patch_calendar(result: Any) -> Any:
    return patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        AsyncMock(return_value=result),
    )


@pytest.mark.asyncio
async def test_calendar_plain_list_is_failure() -> None:
    with _patch_calendar([]):
        ok, tte = await sv.nearest_macro_event_tte_hours()
    assert (ok, tte) == (False, None)
    assert _calc(mev=tte, cal_ok=ok)["sv_f_mev"] is None
    with _patch_calendar(_cal_events()):
        ok, tte = await sv.nearest_macro_event_tte_hours()
    assert (ok, tte) == (True, None)
    assert _calc(mev=tte, cal_ok=ok)["sv_f_mev"] is False


@pytest.mark.asyncio
async def test_calendar_exception_is_failure() -> None:
    with patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert await sv.nearest_macro_event_tte_hours() == (False, None)


@pytest.mark.asyncio
async def test_stale_tte_uses_event_time() -> None:
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    past = (now - timedelta(hours=1)).isoformat()
    too_old = (now - timedelta(hours=10)).isoformat()
    later = (now + timedelta(hours=30)).isoformat()
    evs = [
        # tte_hours 為凍結的假值，必須被忽略
        SimpleNamespace(time=too_old, tte_hours=0.5),
        SimpleNamespace(time=later, tte_hours=-99.0),
        SimpleNamespace(time=soon, tte_hours=999.0),
        SimpleNamespace(time=past, tte_hours=999.0),
        SimpleNamespace(time="not-a-time", tte_hours=0.0),
    ]
    with _patch_calendar(_cal_events(*evs)):
        ok, tte = await sv.nearest_macro_event_tte_hours()
    assert ok is True
    assert tte is not None and -1.05 < tte < -0.95  # 過去 1 小時的事件（>= -2）


@pytest.mark.asyncio
async def test_stale_tte_picks_smallest_nonnegative_side() -> None:
    now = datetime.now(timezone.utc)
    evs = [
        SimpleNamespace(time=(now + timedelta(hours=30)).isoformat(), tte_hours=1.0),
        SimpleNamespace(time=(now + timedelta(hours=5)).isoformat(), tte_hours=70.0),
    ]
    with _patch_calendar(_cal_events(*evs)):
        ok, tte = await sv.nearest_macro_event_tte_hours()
    assert ok and tte is not None and 4.9 < tte < 5.1


# ------------------------------------------------------------- 紀錄器
_RADAR_FULL: dict[str, Any] = {
    "skew_percentile": 85.0,
    "skew_percentile_source": "CANONICAL",
    "gex_profile_data": {
        "call_wall": 104.0,
        "put_wall": 95.0,
        "net_gex": 1e6,
        "_is_stale_cache": False,
    },
    "overhead_neg_gex_swamp": (102.0, -5e6),
    "max_pain": {"max_pain": 95.0, "expiry": "2026-10-30"},
}


def _result(**kw: Any) -> Any:
    base: dict[str, Any] = dict(
        status="ENTRY",
        tier=2,
        size_pct=1.0,
        squeeze_count=2,
        triggers=[],
        stop=95.0,
        reason="r",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _features(row: dict[str, Any]) -> dict[str, Any]:
    feats = row["features_json"]
    assert feats is not None
    parsed: dict[str, Any] = (
        json.loads(feats) if isinstance(feats, str) else dict(feats)
    )
    return parsed


def test_all_sv_keys_written() -> None:
    shadow = _calc(radar=_RADAR_FULL, skew=85.0, mev=3.0)
    with rec.evaluation_source("WATCHLIST_ADVISOR"):
        rec.record_squeeze_entry(
            "AAA",
            100.0,
            _result(),
            {},
            candidate_radar=_RADAR_FULL,
            shadow_vetoes=shadow,
        )
    feats = _features(rec._BUFFER[0])
    for key in sv.SHADOW_KEYS:
        assert key in feats, key
        assert feats[key] == shadow[key]


def test_record_without_shadow_has_no_sv_keys() -> None:
    with rec.evaluation_source("WATCHLIST_ADVISOR"):
        rec.record_squeeze_entry("AAA", 100.0, _result(), {})
    assert not any(k.startswith("sv_") for k in _features(rec._BUFFER[0]))


def test_features_json_within_max() -> None:
    tf = SimpleNamespace(
        squeeze_level=3,
        momentum_color="Light Blue",
        green_dot=True,
        turbo=True,
        momentum=-123456.789012,
    )
    matrix = {k: tf for k in ("1d", "4h", "65m", "15m", "5m", "1m")}
    shadow: dict[str, Any] = {k: None for k in sv.SHADOW_KEYS}
    for k in sv.SHADOW_KEYS:
        if k.endswith(("_atr", "_pct", "_dist", "_rr")) or k in (
            "sv_cw",
            "sv_sw",
            "sv_pw",
            "sv_mev_h",
        ):
            shadow[k] = -123456.78
        elif k.startswith("sv_f_") or k in ("sv_any", "sv_gex_stale"):
            shadow[k] = False
        else:
            shadow[k] = -1
    resistance = SimpleNamespace(
        atr_1d=123.456789,
        overhead=SimpleNamespace(bottom=12345.67, top=12345.67),
        broken=SimpleNamespace(top=12345.67),
        is_approaching=True,
    )
    radar = {"skew_percentile": 99.123456, "skew_percentile_source": "CANONICAL"}
    with rec.evaluation_source("WATCHLIST_ADVISOR"):
        rec.record_squeeze_entry(
            "AAA",
            12345.67,
            _result(triggers=["D Green Dot", "4h Green Dot", "65m Turbo"]),
            matrix,
            resistance,
            candidate_radar=radar,
            shadow_vetoes=shadow,
        )
    row = rec._BUFFER[0]
    assert row["features_json"] is not None
    assert len(row["features_json"]) <= rec._FEATURES_JSON_MAX
    assert "sv_any" in _features(row)


# ------------------------------------------------------------- entry_advisor
async def _run_squeeze(
    radar: Optional[dict[str, Any]], *, active: bool, side_effect: Any = None
) -> Any:
    from market_analysis.intraday_pipeline import entry_advisor
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.resistance import (
        ResistanceContext,
        ResistanceZone,
    )
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    res = ResistanceContext(
        atr_1d=2.0,
        overhead=ResistanceZone(108.0, 109.0, 2),
        is_approaching=False,
        broken=None,
    )
    result = SqueezeEntryResult("ENTRY", 2, 1.0, 95.0, "✅ T2：r", ["x"], 2, None)
    evaluate = AsyncMock(return_value=SqueezeEvaluation(result, {}, res))
    stack = [
        patch(
            "market_analysis.squeeze_entry.vetoes.resolve_long_entry_vetoes",
            AsyncMock(return_value=([], None)),
        ),
        patch("market_analysis.squeeze_entry.evaluate_symbol", evaluate),
    ]
    if side_effect is not None:
        stack.append(patch.object(sv, "compute_shadow_vetoes", side_effect=side_effect))
    token = rec.set_evaluation_source("WATCHLIST_ADVISOR" if active else None)
    try:
        for p in stack:
            p.start()
        return await entry_advisor._evaluate_squeeze_long(
            "DYNAMIC", "AAA", 100.0, radar=radar
        )
    finally:
        for p in reversed(stack):
            p.stop()
        rec.reset_evaluation_source(token)


def _advice_tuple(a: Any) -> tuple[Any, ...]:
    return (
        a.passed,
        a.reason,
        a.entry_price,
        a.stop_loss,
        a.target,
        a.rr_ratio,
        a.tier,
        a.size_pct,
    )


@pytest.mark.asyncio
async def test_no_calendar_when_inactive() -> None:
    mock = AsyncMock(return_value=(True, 3.0))
    with patch(_NAV, mock):
        await _run_squeeze(_RADAR_FULL, active=False)
    mock.assert_not_awaited()
    assert len(rec._BUFFER) == 0


@pytest.mark.asyncio
async def test_shadow_passed_when_active_and_advice_unchanged() -> None:
    mock = AsyncMock(return_value=(True, 3.0))
    with patch(_NAV, mock):
        with_shadow = await _run_squeeze(_RADAR_FULL, active=True)
    mock.assert_awaited_once()
    feats = _features(rec._BUFFER[0])
    assert feats["sv_mev_h"] == 3.0 and feats["sv_f_mev"] is True
    assert feats["sv_cw"] == 104.0 and feats["sv_stop_pct"] == 5.0
    assert feats["sv_f_skew"] is True
    rec._BUFFER.clear()
    with patch(_NAV, AsyncMock(return_value=(True, 3.0))):
        without = await _run_squeeze(_RADAR_FULL, active=False)
    assert _advice_tuple(with_shadow) == _advice_tuple(without)


@pytest.mark.asyncio
async def test_shadow_with_empty_radar_still_records() -> None:
    with patch(_NAV, AsyncMock(return_value=(False, None))):
        await _run_squeeze({}, active=True)
    feats = _features(rec._BUFFER[0])
    assert feats["sv_f_mev"] is None and feats["sv_cw"] is None
    assert feats["sv_stop_pct"] == 5.0


@pytest.mark.asyncio
async def test_shadow_exception_keeps_advice() -> None:
    with patch(_NAV, AsyncMock(return_value=(True, None))):
        baseline = await _run_squeeze(_RADAR_FULL, active=False)
        broken = await _run_squeeze(
            _RADAR_FULL, active=True, side_effect=RuntimeError("boom")
        )
    assert _advice_tuple(broken) == _advice_tuple(baseline)
    assert len(rec._BUFFER) == 1
    assert not any(k.startswith("sv_") for k in _features(rec._BUFFER[0]))
