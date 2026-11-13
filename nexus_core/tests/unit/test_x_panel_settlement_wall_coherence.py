"""/x 結算日、CallWall、IV 比分母與 DTE 0 σ 的一致性（僅呈現，不改任何閘門）。"""

import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import cogs.embed_builders.portfolio_embeds as pe
from cogs.embed_builders.portfolio_embeds import (
    _dte0_time_years,
    create_tactical_symbol_embed,
)
from market_analysis.risk_engine import MacroContext, optimize_position_risk
from market_time import ny_tz
from models.quant import OptimizationResult
from tests.unit.test_x_panel_entry_coherence import _embed_text, _mu_case
from tests.unit.test_x_panel_review_fixes_a import _at, _data, _iv, _text


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _tomorrow_mp() -> list[dict[str, Any]]:
    tomorrow = (datetime.now(ny_tz).date() + timedelta(days=1)).isoformat()
    return [{"expiry": tomorrow, "max_pain": 1040.0, "distance_pct": 5.5}]


# --- 1 結算日 overlay --------------------------------------------------------


def test_negative_gamma_gravity_overlay_has_no_pin_claim() -> None:
    d = _mu_case(month_max_pains=_tomorrow_mp())
    d["gex_profile_data"]["net_gex"] = -5_000_000
    text = _plain(_embed_text(d))
    assert "結算風險: " in text
    assert "⚠️ 結算日風險：" in text and "現價偏離 +5.5%" in text
    assert "釘住效應最強" not in text
    assert "結算日引力" not in text


def test_positive_gamma_gravity_overlay_unchanged() -> None:
    text = _embed_text(_mu_case(month_max_pains=_tomorrow_mp()))
    assert "結算引力: \u001b[1;31m⚠️ 結算日引力：" in text
    assert "結算前 Gamma 釘住效應最強，避免追價新倉" in text


def test_negative_overlay_not_longer_than_positive() -> None:
    d_neg = _mu_case(month_max_pains=_tomorrow_mp())
    d_neg["gex_profile_data"]["net_gex"] = -5_000_000
    neg = _plain(_embed_text(d_neg))
    pos = _plain(_embed_text(_mu_case(month_max_pains=_tomorrow_mp())))
    n = [ln for ln in neg.splitlines() if "結算風險:" in ln][0]
    p = [ln for ln in pos.splitlines() if "結算引力:" in ln][0]
    assert len(n) <= len(p)


def test_deep_dive_reason_wording_neutral() -> None:
    src = (
        Path(pe.__file__).parent.parent / "unified_terminal" / "symbol_deep_dive.py"
    ).read_text(encoding="utf-8")
    assert "結算日引力" not in src
    assert "結算日 DTE {gravity['dte']} 偏離痛點" in src


def test_settlement_reason_still_halves_budget_and_no_gravity_word() -> None:
    kwargs: dict[str, Any] = dict(
        current_delta=0.0,
        unit_weighted_delta=0.16,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.4,
        strategy="STO",
        risk_limit=15.0,
    )
    base = optimize_position_risk(**kwargs)
    deg = optimize_position_risk(
        **kwargs, data_degraded_reasons=["結算日 DTE 0 偏離痛點 +5.0%"]
    )
    assert deg.suggested_contracts < base.suggested_contracts
    assert base.risk_budget_shares and deg.risk_budget_shares
    assert math.isclose(
        deg.risk_budget_shares / base.risk_budget_shares, 0.5, rel_tol=0.01
    )
    assert sum("倉位減半" in w for w in deg.warnings) == 1
    joined = "".join(deg.warnings)
    assert "結算日 DTE 0 偏離痛點" in joined and "結算日引力" not in joined


# --- 2 負淨 GEX CallWall ------------------------------------------------------


def _cw_case(cw_net: float, flip: bool = True) -> dict[str, Any]:
    prof = {
        "150.0": 80_000_000.0,
        "155.0": 90_000_000.0,
        "160.0": -2_000_000.0,
        "164.0": -3_000_000.0,
        "165.0": cw_net,
        "170.0": 60_000_000.0 if flip else -1_000_000.0,
    }
    return {
        "symbol": "SPCX",
        "price": 164.0,
        "atr_1d": 3.0,
        "atr_15m": 2.0,
        "gex_profile_data": {
            "spot": 164.0,
            "put_wall": 150.0,
            "call_wall": 165.0,
            "net_gex": 5_000_000.0,
            "gex_profile": prof,
        },
    }


def test_negative_callwall_is_labelled_breakout_zone() -> None:
    text = _plain(_embed_text(_cw_case(-30_000_000.0)))
    assert "CallWall: $165.00〔淨GEX" in text and "突破轉助漲區" in text
    assert "⚠️ 上檔助漲" in text and "🚧 上檔壓力" not in text
    assert "📌 貼牆" not in text
    assert "📌 釘住效應" not in text
    assert "深度:" not in text.split("上檔助漲")[1].split("Net GEX Regime")[0]


def test_negative_callwall_flip_pointer_when_flip_above() -> None:
    with patch.object(pe, "estimate_symbol_gamma_flip", return_value=170.0):
        text = _plain(_embed_text(_cw_case(-30_000_000.0)))
    assert "突破轉助漲區；Flip $170.00〕" in text


def _sq_case(cw_net: float, atr_15m: float = 0.8) -> dict[str, Any]:
    from types import SimpleNamespace

    from market_analysis.squeeze_entry import rules as sq

    d = _cw_case(cw_net)
    d["atr_15m"] = atr_15m
    d["quote"] = {"c": 164.0, "d": 0.0, "dp": 0.0, "pc": 164.0}
    result = SimpleNamespace(
        status=sq.STATUS_ENTRY,
        tier=3,
        size_pct=2.5,
        stop=160.0,
        triggers=["D Green Dot"],
        reason="ok",
        resistance_warning=None,
    )
    d["squeeze_eval"] = SimpleNamespace(result=result, matrix={})
    return d


def _run_spy(d: dict[str, Any], flip: float) -> tuple[list[Any], str]:
    seen: list[Any] = []
    real = pe._squeeze_cap_rr

    def spy(cw: Any, price: float, stop: Any) -> Any:
        seen.append(cw)
        return real(cw, price, stop)

    with (
        patch.object(pe, "_squeeze_cap_rr", spy),
        patch.object(pe, "estimate_symbol_gamma_flip", return_value=flip),
    ):
        text = _plain(_embed_text(d))
    return seen, text


def test_negative_callwall_squeeze_cap_uses_flip_above_price() -> None:
    seen, text = _run_spy(_sq_case(-30_000_000.0), 170.0)
    assert seen == [170.0]
    assert "判定: 🟢 可進場" in text


def test_negative_callwall_flip_between_price_and_wall_still_caps() -> None:
    seen, text = _run_spy(_sq_case(-30_000_000.0), 164.5)
    assert seen == [164.5]
    assert "突破轉助漲區〕" in text  # Flip < CallWall：不附 Flip 指標
    assert "；Flip" not in text


def test_negative_callwall_no_flip_or_below_price_no_cap() -> None:
    for flip in (0.0, 150.0):
        seen, text = _run_spy(_sq_case(-30_000_000.0), flip)
        assert seen == [None], flip
        assert "⚠封頂" not in text


def test_positive_callwall_hug_keeps_squeeze_cap_tag() -> None:
    seen, text = _run_spy(_sq_case(40_000_000.0, atr_15m=2.0), 170.0)
    assert " ⚠封頂" in text and "📌 貼牆" in text


def test_positive_callwall_unchanged() -> None:
    text = _plain(_embed_text(_cw_case(40_000_000.0)))
    assert "🚧 上檔壓力" in text and "助漲" not in text
    assert "深度:" in text
    assert "📌 貼牆" in text  # 距 0.61% < 貼牆帶


# --- 4a IV 比分母 -------------------------------------------------------------


def test_optimization_result_iv_ratio_fields_vix_vs_default() -> None:
    kwargs: dict[str, Any] = dict(
        current_delta=0.0,
        unit_weighted_delta=0.16,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.935,
        strategy="STO",
        risk_limit=15.0,
    )
    dflt = optimize_position_risk(**kwargs)
    assert dflt.iv_ratio_ref_source == "預設"
    assert dflt.iv_ratio_ref_iv == 0.16 and dflt.iv_ratio_stock_iv == 0.935
    macro = MacroContext(vix=18.7, oil_price=70.0, vix_change=0.0)
    with_vix = optimize_position_risk(**kwargs, macro_data=macro)
    assert with_vix.iv_ratio_ref_source == "VIX"
    assert with_vix.iv_ratio_ref_iv is not None
    assert math.isclose(with_vix.iv_ratio_ref_iv, 0.187)
    # 新欄位不影響 sizing：unit_adj_delta 恰為 unit_delta × IV 比
    assert dflt.unit_adj_delta is not None and with_vix.unit_adj_delta is not None
    assert math.isclose(dflt.unit_adj_delta, round(0.16 * 0.935 / 0.16, 2))
    assert math.isclose(with_vix.unit_adj_delta, round(0.16 * 0.935 / 0.187, 2))
    unk = optimize_position_risk(**kwargs, stock_iv_unknown=True)
    assert unk.iv_ratio_stock_iv is None


def _kelly_text(res: OptimizationResult) -> str:
    from tests.unit.test_x_panel_data_anomaly_guards import _render
    from tests.unit.test_x_panel_timeframe_coherence import _spcx

    d: dict[str, Any] = _spcx(kelly_sizing=res, kelly_beta=1.78)
    d["kelly_unit_weighted_delta"] = 6.3
    return _plain(_render(d))


def test_zero_lot_reason_discloses_iv_denominator() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=31.3,
        risk_budget_shares=10.0,
        iv_ratio_stock_iv=0.935,
        iv_ratio_ref_iv=0.187,
        iv_ratio_ref_source="VIX",
    )
    text = _kelly_text(res)
    assert "×IV比4.97(93.5%÷VIX18.7%)≈31.3 股 SPY 等值" in text


def test_zero_lot_reason_high_iv_uses_integer_percent() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=31.3,
        risk_budget_shares=10.0,
        iv_ratio_stock_iv=1.234,
        iv_ratio_ref_iv=0.187,
        iv_ratio_ref_source="VIX",
    )
    text = _kelly_text(res)
    assert "(123%÷VIX18.7%)" in text
    assert "123.4%" not in text


def test_zero_lot_reason_default_source_label() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=31.3,
        risk_budget_shares=10.0,
        iv_ratio_stock_iv=0.935,
        iv_ratio_ref_iv=0.16,
        iv_ratio_ref_source="預設",
    )
    text = _kelly_text(res)
    assert "(93.5%÷預設16.0%)" in text


def test_zero_lot_reason_without_fields_keeps_old_text() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=31.3,
        risk_budget_shares=10.0,
    )
    assert "×IV比4.97≈31.3 股 SPY 等值" in _kelly_text(res)


# --- 5 DTE 0 σ ----------------------------------------------------------------


def test_dte0_time_years_phases() -> None:
    pre = _dte0_time_years(datetime(2026, 10, 7, 8, 0, tzinfo=ny_tz))
    mid = _dte0_time_years(datetime(2026, 10, 7, 12, 45, tzinfo=ny_tz))
    after = _dte0_time_years(datetime(2026, 10, 7, 16, 0, tzinfo=ny_tz))
    assert pre is not None and math.isclose(pre, 1 / 252)
    assert mid is not None and math.isclose(mid, (195 / 390) / 252)
    assert after is None


def _dte0_text(now: datetime) -> str:
    mps = [{"expiry": "2026-10-07", "max_pain": 160.0, "distance_pct": -3.6}]
    iv = _iv(straddle_implied_iv=0.937, straddle_dte=0, straddle_expiry="2026-10-07")
    d = _data(iv, max_pain=160.0, month_max_pains=mps, max_pain_expiry="2026-10-07")
    d["price"] = 166.0
    d["quote"] = {"c": 166.0, "d": 0.0, "dp": 0.0, "pc": 166.0}
    return _text(_at(now, lambda: create_tactical_symbol_embed(d)))


def test_dte0_sigma_premarket_matches_formula() -> None:
    text = _dte0_text(datetime(2026, 10, 7, 8, 0, tzinfo=ny_tz))
    assert f"±${0.937 * 166.0 * math.sqrt(1 / 252):.1f}" in text  # ±$9.8


def test_dte0_sigma_midsession_smaller_and_after_close_none() -> None:
    mid = _dte0_text(datetime(2026, 10, 7, 12, 45, tzinfo=ny_tz))
    assert f"±${0.937 * 166.0 * math.sqrt(0.5 / 252):.1f}" in mid
    after = _dte0_text(datetime(2026, 10, 7, 16, 30, tzinfo=ny_tz))
    assert "結算前1σ" not in after


# --- 3 OI PCR 快取顯示 ---------------------------------------------------------


def test_oi_pcr_cached_value_rendered() -> None:
    d = _mu_case(
        pcr={
            "volume_pcr": 0.8,
            "oi_pcr": None,
            "oi_pcr_cached": 0.85,
            "oi_pcr_cached_state": "⚖️ 籌碼結構中性",
            "oi_pcr_cached_age_seconds": 7300.0,
        }
    )
    text = _plain(_embed_text(d))
    assert "0.85" in text and "[快取 2h前]" in text
    assert "N/A (結構缺失)" not in text


def test_oi_pcr_without_cache_still_missing() -> None:
    d = _mu_case(pcr={"volume_pcr": 0.8, "oi_pcr": None})
    text = _plain(_embed_text(d))
    assert "[快取" not in text
