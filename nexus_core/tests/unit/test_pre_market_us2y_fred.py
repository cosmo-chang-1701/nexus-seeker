"""盤前簡報 2Y 殖利率與 10Y−2Y 利差：改讀已儲存的 FRED DGS2／DGS10 觀測。"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from cogs.embed_builders.order_embeds.pre_market_briefing import (
    build_pre_market_briefing_embed,
)
from cogs.embed_builders.scan_embeds.macro_and_fomc import create_macro_scan_embed
from market_analysis.analyst_runners import macro_runner
from market_analysis.analyst_runners.macro_runner import (
    _FRED_YIELD_STALE_DAYS,
    _compute_fred_yields,
    build_macro_alerts,
    build_macro_healthy_status,
    fetch_fred_yields,
)
from market_analysis.macro_signals import FRED_SERIES, Observation

TODAY = date(2026, 11, 4)

FORBIDDEN = ("防禦型資產", "跨國流動性充裕", "跨國資產流動性充裕", "標準網格")


def _obs(d: str, v: float, avail: str | None = None) -> Observation:
    od = date.fromisoformat(d)
    return Observation(od, v, date.fromisoformat(avail) if avail else od)


def _text(embed: Any) -> str:
    return "\n".join(str(f.value) for f in embed.fields)


BASE: dict[str, Any] = {
    "vix": 16.0,
    "vix_change": 0.1,
    "dxy": 101.0,
    "tnx": 4.91,
    "tnx_change_bps": -2.0,
}


def test_real_dgs2_dgs10_shows_value_date_and_spread() -> None:
    fred = _compute_fred_yields(
        [_obs("2026-11-02", 4.59)], [_obs("2026-11-02", 4.91)], TODAY
    )
    assert fred["us2y"] == 4.59 and fred["us2y_date"] == "2026-11-02"
    assert fred["curve_spread"] == 0.32 and fred["curve_date"] == "2026-11-02"
    data = {**BASE, **fred}
    for embed in (
        build_pre_market_briefing_embed(data, build_macro_alerts(data)),
        create_macro_scan_embed(data, build_macro_alerts(data)),
    ):
        text = _text(embed)
        assert "4.59%" in text and "11/02" in text and "+0.32%" in text
        assert "降級" not in (embed.title or "")


def test_spread_uses_same_obs_date() -> None:
    fred = _compute_fred_yields(
        [_obs("2026-11-02", 4.59), _obs("2026-11-03", 4.60)],
        [_obs("2026-11-02", 4.91)],
        TODAY,
    )
    assert fred["us2y"] == 4.60 and fred["us2y_date"] == "2026-11-03"
    assert fred["curve_date"] == "2026-11-02" and fred["curve_spread"] == 0.32


def test_not_yet_available_observation_ignored() -> None:
    fred = _compute_fred_yields(
        [_obs("2026-11-03", 4.60, avail="2026-11-05")], [], TODAY
    )
    assert fred["us2y"] is None


def test_dgs10_missing_spread_dash_no_alert_not_degraded() -> None:
    fred = _compute_fred_yields([_obs("2026-11-02", 4.59)], [], TODAY)
    assert fred["curve_spread"] is None and fred["us2y"] == 4.59
    data = {**BASE, "tnx": 4.29, **fred}
    alerts = build_macro_alerts(data)
    assert not any("曲線" in a for a in alerts)
    embed = build_pre_market_briefing_embed(data, alerts)
    assert "降級" not in (embed.title or "")
    assert "利差 --%" in _text(embed)
    status = build_macro_healthy_status(data)
    assert "部分總經數據缺失" not in status
    assert "利差無資料（10Y 未入庫或與 2Y 無同日資料）" in status
    assert "利差無資料（10Y 未入庫或與 2Y 無同日資料）" in _text(embed)
    no2y = {**BASE, "us2y": None, "curve_spread": None}
    assert "2Y 無資料" in build_macro_healthy_status(no2y)


def test_stale_yields_become_none() -> None:
    old = "2026-10-20"
    fred = _compute_fred_yields([_obs(old, 4.5)], [_obs(old, 4.8)], TODAY)
    assert fred["us2y"] is None and fred["curve_spread"] is None
    assert _FRED_YIELD_STALE_DAYS == 5
    edge = _compute_fred_yields(
        [_obs("2026-10-30", 4.5)], [_obs("2026-10-30", 4.8)], TODAY
    )
    assert edge["us2y"] == 4.5  # 5 天整仍可用


@pytest.mark.asyncio
async def test_db_read_raising_yields_none_and_briefing_built() -> None:
    with patch(
        "database.macro_signal_log.load_observations", side_effect=RuntimeError("x")
    ):
        fred = await fetch_fred_yields()
    assert fred == {
        "us2y": None,
        "us2y_date": None,
        "curve_spread": None,
        "curve_date": None,
    }
    embed = build_pre_market_briefing_embed({**BASE, **fred}, [])
    assert "降級" not in (embed.title or "")
    assert "--%" in _text(embed)


def test_false_unrecession_alert_no_longer_fires() -> None:
    data = {**BASE, "tnx": 4.29, "tnx_change_bps": -3.0, "curve_spread": 0.32}
    assert not any("解除倒掛" in a for a in build_macro_alerts(data))


def test_curve_alerts_with_spread_and_date() -> None:
    near = build_macro_alerts(
        {
            **BASE,
            "tnx_change_bps": -3.0,
            "curve_spread": 0.05,
            "curve_date": "2026-11-02",
        }
    )
    assert any("解除倒掛" in a and "+0.05%" in a and "11/02" in a for a in near)
    deep = build_macro_alerts(
        {**BASE, "curve_spread": -0.5, "curve_date": "2026-11-02"}
    )
    assert any("深度倒掛" in a and "-0.50%" in a for a in deep)


def test_texts_have_no_exposure_advice() -> None:
    cases = [
        {"tnx": 4.8, "tnx_change_bps": 10.0, "curve_spread": -0.5},
        {"vix": 25.0, "vix_change": 3.0, "dxy": 106.0},
    ]
    blobs: list[str] = []
    for c in cases:
        blobs += build_macro_alerts({**BASE, **c})
    for dxy in (101.0, 104.0):
        for vix in (14.0, 17.0, 22.0):
            blobs.append(build_macro_healthy_status({**BASE, "dxy": dxy, "vix": vix}))
            blobs.append(
                _text(
                    build_pre_market_briefing_embed(
                        {**BASE, "dxy": dxy, "vix": vix}, []
                    )
                )
            )
    joined = "\n".join(blobs)
    assert not ("降低" in joined and "曝險" in joined)
    for word in FORBIDDEN:
        assert word not in joined


def test_dgs10_is_record_only() -> None:
    from services.macro_signal_service import fred_readings

    assert FRED_SERIES["DGS10"] == "daily"
    obs: dict[str, list[Observation]] = {
        "DGS2": [_obs("2026-11-02", 4.59)],
        "DFII10": [_obs("2026-11-02", 1.9)],
    }
    with_10 = {**obs, "DGS10": [_obs("2026-11-02", 4.91)]}
    assert fred_readings(with_10, TODAY) == fred_readings(obs, TODAY)


def test_no_tuple_fallback_left() -> None:
    import inspect

    assert "isinstance(macro_data, tuple)" not in inspect.getsource(macro_runner)


def test_spread_note_uses_curve_date_not_us2y_date() -> None:
    data = {
        **BASE,
        "us2y": 4.60,
        "us2y_date": "2026-11-03",
        "curve_spread": 0.32,
        "curve_date": "2026-11-02",
    }
    for embed in (
        build_pre_market_briefing_embed(data, []),
        create_macro_scan_embed(data, []),
    ):
        text = _text(embed)
        assert "11/03 利差" not in text
        assert "11/02 利差 +0.32%" in text
        assert "11/03" in text
