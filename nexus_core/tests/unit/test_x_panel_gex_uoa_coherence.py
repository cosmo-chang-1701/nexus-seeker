"""PR-B：/x GEX／UOA／風控呈現一致性（MRVL golden）。"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd

from cogs.embed_builders._embed_helpers import get_sqz_status_display
from cogs.embed_builders.portfolio_embeds import (
    _infer_gex_expiry,
    create_tactical_symbol_embed,
)
from market_time import ny_tz
from tests.unit.test_x_panel_embed_budget import _mrvl_data

_NOW = datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "_FakeDT":
        return _NOW  # type: ignore[return-value]


def _render(data: dict[str, Any]) -> tuple[str, list[str]]:
    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        embed = create_tactical_symbol_embed(data)
    names = [f.name or "" for f in embed.fields]
    text = "\n".join((f.name or "") + "\n" + (f.value or "") for f in embed.fields)
    return text, names


def _gex_present(names: list[str]) -> bool:
    return any("🧲 Gamma 曝險分布" in n for n in names)


def test_infer_gex_expiry() -> None:
    mps = [{"expiry": "2026-10-16"}, {"expiry": "2026-10-09"}]
    assert _infer_gex_expiry(mps, _NOW) == "2026-10-09"
    # 到期當日 16:00 ET 後已結算，跳到下一檔
    after = datetime(2026, 10, 9, 17, 0, tzinfo=ny_tz)
    assert _infer_gex_expiry(mps, after) == "2026-10-16"
    assert _infer_gex_expiry(None, _NOW) is None


def test_kelly_header_and_seller_prerequisites() -> None:
    text, names = _render(_mrvl_data())
    assert _gex_present(names)
    assert "賣 Put 曝險上限 (Kelly·16Δ，非現貨進場許可)" in text
    # MRVL：IVR 未知⚠、最近到期 DTE2 不在引力窗口✅、PutWall 270 淨 GEX 正✅、事件⚠
    assert "賣方前提: IVR⚠ 引力✅ 牆淨GEX✅ 局部Γ" in text
    assert "事件⚠" in text


def test_seller_prerequisites_negative_wall_and_low_ivr() -> None:
    d = _mrvl_data()
    d["iv_rank"] = 5.0
    d["gex_profile_data"]["gex_profile"]["270.0"] = -50_000_000
    d["month_max_pains"] = [
        {"expiry": "2026-10-08", "max_pain": 270.0, "distance_pct": 5.44}
    ]
    d["iv_data"] = d["iv_data"].model_copy(update={"has_macro_event": False})
    text, names = _render(d)
    assert _gex_present(names)
    assert "IVR❌ 引力❌ 牆淨GEX❌" in text
    assert "事件✅" in text


def test_seller_prerequisites_without_gex_shows_dash() -> None:
    d = _mrvl_data()
    d["gex_profile_data"] = None
    d["iv_rank"] = 40.0
    text, _ = _render(d)
    assert "IVR✅" in text
    assert "牆淨GEX— 局部Γ—" in text


def test_divergence_line_tags_and_gex_title() -> None:
    text, names = _render(_mrvl_data())
    assert _gex_present(names)
    assert "Net GEX Regime (10-09 到期)" in text
    assert "大單 $257.50 10-16(DTE9) STO PUT 4,128口" in text
    assert "〔全鏈掃描，表外〕〔跨到期〕" in text


def test_bto_flow_warning_on_heatmap() -> None:
    text, names = _render(_mrvl_data())
    assert _gex_present(names)
    line = next(ln for ln in text.splitlines() if "290.00 |" in ln)
    assert "⚠買7.0k" in line
    assert "今日買入未計入前日OI" in text


def test_uoa_spread_intent_is_tentative() -> None:
    from market_analysis.uoa_telemetry import annotate_spread_structures

    entries: list[dict[str, Any]] = [
        {
            "expiry": "2026-10-09", "type": "CALL", "strike": 280.0,
            "volume": 1000, "action": "🟢 買入開倉 (BTO - Ask)", "intent": "x",
        },
        {
            "expiry": "2026-10-09", "type": "CALL", "strike": 285.0,
            "volume": 1000, "action": "🔴 賣出開倉 (STO - Bid)", "intent": "y，z",
        },
    ]  # fmt: skip
    annotate_spread_structures(entries)
    assert (
        "疑似" in entries[1]["intent"]
        and "賣出腿（日累積配對）" in entries[1]["intent"]
    )
    assert (
        "疑似" in entries[0]["intent"]
        and "買入腿（日累積配對）" in entries[0]["intent"]
    )
    assert entries[0]["spread_role"] == "LONG_LEG"
    assert entries[1]["spread_role"] == "SHORT_LEG"


def test_tod_median_resists_outlier() -> None:
    from market_analysis.price_volume_alert import (
        compute_time_of_day_avg_volume,
        compute_time_of_day_median_volume,
    )

    idx = pd.DatetimeIndex(
        [f"2026-10-0{d} 15:45" for d in (1, 2, 5, 6)] + ["2026-10-07 15:45"]
    )
    df = pd.DataFrame({"Volume": [100, 100, 100, 1000, 500]}, index=idx)
    med, n = compute_time_of_day_median_volume(df)
    avg, _ = compute_time_of_day_avg_volume(df)
    assert (med, n) == (100.0, 4)
    assert avg == 325.0


def test_sqz_display_default_unchanged_and_decel() -> None:
    base = get_sqz_status_display(True, 3.1, "Long")
    assert base == get_sqz_status_display(True, 3.1, "Long", momentum_color=None)
    assert base == get_sqz_status_display(True, 3.1, "Long", momentum_color="LightBlue")
    assert "多頭推進" in base[0]
    t1, _ = get_sqz_status_display(True, 3.1, "Long", momentum_color="DarkBlue")
    assert "多頭減速 / 擠壓中" in t1
    t2, _ = get_sqz_status_display(False, 3.1, "Long", momentum_color="DarkBlue")
    assert "多頭減速 / 無擠壓" in t2
    t3, _ = get_sqz_status_display(False, -2.0, "Short", momentum_color="Golden")
    assert "空頭減速" in t3


def _matrix(m65: float, m15: float) -> SimpleNamespace:
    mk = lambda v: SimpleNamespace(momentum_value=v)  # noqa: E731
    return SimpleNamespace(
        result=None, matrix={"D": mk(34.4), "65m": mk(m65), "15m": mk(m15)}
    )


def _sqz_data() -> dict[str, Any]:
    d = _mrvl_data()
    d["psq_result"] = {
        "is_squeezing": False, "momentum": 35.69, "squeeze_level": "Release",
        "direction": "Long", "vix_momentum_label": "NORMAL",
        "momentum_color": "DarkBlue",
    }  # fmt: skip
    return d


def test_sqz_block_no_duplicate_mom_line_and_intraday_turn() -> None:
    d = _sqz_data()
    d["squeeze_eval"] = _matrix(-1.0, 1.0)
    text, _ = _render(d)
    assert "動能數值 (SQZ MOM)" not in text
    assert "多頭減速" in text
    assert "⚠ 日內轉弱: 65m/15m 動能<0" in text


def test_sqz_block_intraday_turn_below_gamma_flip() -> None:
    d = _sqz_data()
    d["squeeze_eval"] = _matrix(1.0, 1.0)
    d["price"] = 200.0
    d["quote"] = {"c": 200.0, "d": 0.0, "dp": 0.0, "pc": 200.0}
    d["gex_profile_data"]["gex_profile"] = {
        "190.0": -5e8, "195.0": -4e8, "200.0": -1e8, "205.0": 3e8, "210.0": 6e8,
    }  # fmt: skip
    d["gex_profile_data"]["spot"] = 200.0
    d["gex_profile_data"]["call_wall"] = 210.0
    d["gex_profile_data"]["put_wall"] = 190.0
    text, names = _render(d)
    assert _gex_present(names)
    assert "⚠ 日內轉弱: 跌破 Gamma Flip" in text


def test_sqz_block_no_turn_when_aligned() -> None:
    d = _sqz_data()
    d["squeeze_eval"] = _matrix(1.0, 1.0)
    text, _ = _render(d)
    assert "日內轉弱" not in text


def test_skew_sign_note() -> None:
    text, _ = _render(_mrvl_data())
    assert "Skew (P−C):" in text and "Call溢價" in text
