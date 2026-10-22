"""/x 賣方判決呈現：Kelly 前提標題與名目、財報高波建議、擠壓封頂 R:R。"""

from typing import Any

import pytest

from cogs.embed_builders.portfolio_embeds import (
    _compact_notional,
    _kelly_verdict_header,
    _squeeze_cap_rr,
)
from market_analysis.risk_engine import OptimizationResult
from tests.unit.test_x_panel_embed_budget import _mrvl_data
from tests.unit.test_x_panel_gex_uoa_coherence import _render
from tests.unit.test_x_panel_timeframe_coherence import _render as _render_sq
from tests.unit.test_x_panel_timeframe_coherence import _spcx

_OLD_HEADER = " 賣 Put 曝險上限 (Kelly·16Δ，非現貨進場許可)"
_OLD_LIMIT_TAIL = "(β 加權 Delta 佔總資金 100.0%)"
_OLD_HIGH_IV_ADVICE = "建議縮小部位或使用期權賣方策略保護"
_OLD_SQ_HEADER = " 現貨擠壓進場 (Squeeze Entry · strategies/10)"


def _kelly_header(text: str) -> str:
    return next(ln for ln in text.splitlines() if "賣 Put 曝險上限" in ln)


# --- Kelly 判決標題 ----------------------------------------------------------


def test_header_chooser() -> None:
    assert "⛔前提未過" in _kelly_verdict_header(["✅", "❌", "⚠"])
    assert "⚠前提待確認" in _kelly_verdict_header(["✅", "—", "⚠"])
    assert _kelly_verdict_header(["✅", "—", "✅"]) == _OLD_HEADER


def test_negative_wall_gex_header_blocked() -> None:
    d = _mrvl_data()
    # PutWall 270 淨 GEX 轉負 → 牆淨GEX❌
    d["gex_profile_data"]["gex_profile"]["270.0"] = -50_000_000
    text, _ = _render(d)
    assert "牆淨GEX❌" in text
    assert "⛔前提未過" in _kelly_header(text)


def test_event_only_header_pending() -> None:
    text, _ = _render(_mrvl_data())
    assert "事件⚠" in text and "❌" not in text.split("賣方前提:")[1].splitlines()[0]
    assert _kelly_header(text).endswith("⚠前提待確認")


def test_all_pass_keeps_old_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import database.calendar_cache as cal

    monkeypatch.setattr(cal, "get_cached_earnings", lambda *_a, **_k: None)
    d = _mrvl_data()
    d["iv_rank"] = 20.0
    d["iv_data"].iv_source = "LIVE_IV"
    d["iv_data"].has_macro_event = False
    d["iv_data"].has_earnings_event = False
    text, _ = _render(d)
    row = text.split("賣方前提:")[1].splitlines()[0]
    assert "⚠" not in row and "❌" not in row
    assert _kelly_header(text) == _OLD_HEADER


# --- 名目 --------------------------------------------------------------------


def test_compact_notional() -> None:
    assert _compact_notional(327_040) == "$327K"
    assert _compact_notional(1_234_000) == "$1.2M"
    assert _compact_notional(12_400_000) == "$12M"
    assert _compact_notional(8_400) == "$8,400"
    assert _compact_notional(2_500_000_000) == "$2.5B"
    probes = (9_999.6, 999_600, 9_999_600, 999_600_000, 9_999_600_000, 1e12, 1e13, 1e18)
    assert all(len(_compact_notional(v)) <= 6 for v in probes)
    assert _compact_notional(999_600) == "$1.0M"
    assert _compact_notional(9_999.6) == "$10K"
    assert _compact_notional(float("nan")) == "—"
    assert _compact_notional(float("inf")) == "—"
    assert _compact_notional(1e13) == "$>10T"


def test_notional_on_limit_line() -> None:
    d = _mrvl_data()
    d["price"] = 116.8
    d["kelly_sizing"] = OptimizationResult(
        suggested_contracts=28, exposure_pct=0.8, warnings=[]
    )
    text, _ = _render(d)
    line = next(ln for ln in text.splitlines() if "建議上限" in ln)
    assert "名目≈$327K" in line and "βΔ佔資金 0.8%" in line


def test_zero_contracts_keeps_old_line() -> None:
    d = _mrvl_data()
    d["kelly_sizing"] = OptimizationResult(
        suggested_contracts=0, exposure_pct=0.0, warnings=[]
    )
    text, _ = _render(d)
    line = next(ln for ln in text.splitlines() if "建議上限" in ln)
    assert "0 口" in line and "(β 加權 Delta 佔總資金 0.0%)" in line
    assert "名目" not in line


def test_limit_line_worst_case_not_longer() -> None:
    d = _mrvl_data()
    d["price"] = 99_999.0
    d["kelly_sizing"] = OptimizationResult(
        suggested_contracts=99_999, exposure_pct=100.0, warnings=[]
    )
    text, _ = _render(d)
    new = next(ln for ln in text.splitlines() if "建議上限" in ln)
    old = f" ├─ 建議上限: \u001b[1;36m99999 口\u001b[0m {_OLD_LIMIT_TAIL}"
    assert len(new) <= len(old)
    for h in (
        " 賣 Put 曝險上限 (Kelly·16Δ) ⛔前提未過",
        " 賣 Put 曝險上限 (Kelly·16Δ) ⚠前提待確認",
    ):
        assert len(h) < len(_OLD_HEADER)


# --- 高波提示 ----------------------------------------------------------------


def _high_iv(earnings: bool, **iv_kw: Any) -> str:
    from datetime import date, timedelta

    from tests.unit.test_iv_em_consistency import _embed_text, _iv, _psq

    kw: dict[str, Any] = {}
    if earnings:
        kw = dict(
            has_earnings_event=True,
            earnings_date=(date.today() + timedelta(days=5)).isoformat(),
        )
    return _embed_text(
        {
            "symbol": "SNDK",
            "quote": {"c": 1000.0},
            "iv_data": _iv(
                **{**dict(current_iv=1.04, straddle_implied_iv=1.04), **kw, **iv_kw}
            ),
            "psq_result": _psq(),
        }
    )


def test_high_iv_earnings_no_seller_advice() -> None:
    text = _high_iv(True)
    assert "極端高波環境" in text
    assert "賣方策略保護" not in text and "勿賣方收租" in text


def test_high_iv_ivr60_earnings_no_seller_advice() -> None:
    text = _high_iv(True, current_iv=0.5, straddle_implied_iv=0.5, iv_rank=60.0)
    assert "IVR 60%" in text
    assert "賣方策略保護" not in text and "勿賣方收租" in text


def test_high_iv_without_earnings_old_text() -> None:
    text = _high_iv(False)
    assert _OLD_HIGH_IV_ADVICE in text and "勿賣方收租" not in text


def test_earnings_advice_not_longer() -> None:
    assert len("財報跳空風險，縮小部位勿賣方收租") <= len(_OLD_HIGH_IV_ADVICE)


# --- 擠壓封頂 R:R ------------------------------------------------------------


def _sq(call_wall: float, status: str = "ENTRY") -> dict[str, Any]:
    d = _spcx()
    d["price"] = 100.0
    d["squeeze_eval"].result.status = status
    d["squeeze_eval"].result.stop = 95.0
    d["gex_profile_data"] = {
        "spot": 100.0,
        "put_wall": 90.0,
        "call_wall": call_wall,
        "net_gex": 5_000_000.0,
        "gex_profile": {
            "90.0": 80_000_000.0,
            "95.0": 60_000_000.0,
            "100.0": 1_000_000.0,
            str(call_wall): 30_000_000.0,
        },
    }
    return d


def _verdict(text: str) -> str:
    return next(ln for ln in text.splitlines() if "判定:" in ln)


def test_cap_rr_pure() -> None:
    assert _squeeze_cap_rr(102.0, 100.0, 95.0) == pytest.approx(0.4)
    assert _squeeze_cap_rr(None, 100.0, 95.0) is None
    assert _squeeze_cap_rr(102.0, 100.0, 105.0) is None
    assert _squeeze_cap_rr(99.0, 100.0, 95.0) is None


def test_squeeze_cap_tag_when_reward_below_risk() -> None:
    line = _verdict(_render_sq(_sq(102.0)))
    assert line.rstrip().endswith(" ⚠封頂R0.4")


def test_squeeze_no_tag_with_room() -> None:
    assert "⚠封頂" not in _verdict(_render_sq(_sq(120.0)))


def test_squeeze_hug_keeps_plain_tag() -> None:
    line = _verdict(_render_sq(_sq(100.05)))
    assert "⚠封頂" in line and "⚠封頂R" not in line


def test_squeeze_watch_no_tag() -> None:
    assert "⚠封頂" not in _verdict(_render_sq(_sq(102.0, "WATCH")))


def test_squeeze_header_shortened_offsets_tag() -> None:
    text = _render_sq(_sq(102.0))
    assert " 現貨擠壓進場 (strategies/10)" in text
    assert "Squeeze Entry · strategies/10" not in text
    new_header = " 現貨擠壓進場 (strategies/10)"
    assert len(_OLD_SQ_HEADER) - len(new_header) >= len(" ⚠封頂R0.4")
