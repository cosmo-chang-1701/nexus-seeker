"""/x 面板 PR-A 審查修正（F1–F7）回歸測試。"""

from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from cogs.embed_builders.portfolio_embeds import (
    _session_phase,
    create_tactical_symbol_embed,
)
from market_time import ny_tz
from models.quant import IVMetrics

MRVL_QUOTE = {"c": 284.68, "d": -2.33, "dp": -0.8118, "pc": 287.01}


def _text(embed: Any) -> str:
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _iv(**kw: Any) -> IVMetrics:
    base: dict[str, Any] = dict(
        symbol="MRVL",
        current_iv=0.62,
        iv_source="LIVE_IV",
        is_premarket=False,
        event_loading_applied=False,
        straddle_implied_iv=0.6112,
        straddle_expiry="2026-10-16",
        straddle_dte=9,
        expected_move_weekly=24.0,
        reference_spot_price=284.68,
        hv_20=0.5271,
        term_structure_ratio=1.0327,
        iv_term_structure_status="Normal",
        iv_history_count=1,
    )
    base.update(kw)
    return IVMetrics(**base)


def _data(iv: Any, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "symbol": "MRVL",
        "price": 284.68,
        "quote": dict(MRVL_QUOTE),
        "iv_data": iv,
        "expected_move_context": {
            "reference_price": 284.68,
            "reference_label": "現價",
        },
    }
    d.update(extra)
    return d


def _psq() -> dict[str, Any]:
    return {
        "is_squeezing": False,
        "momentum": 0.1,
        "squeeze_level": "Release",
        "direction": "Neutral",
        "vix_momentum_label": "NORMAL",
    }


def _at(now: datetime, fn: Any) -> Any:
    with patch("cogs.embed_builders.portfolio_embeds.datetime") as dt:
        dt.now.return_value = now
        dt.strptime.side_effect = datetime.strptime
        return fn()


# --- F1 ---------------------------------------------------------------------


def test_f1_sigma_uses_headline_expiry_not_nearest_weekly() -> None:
    """週二且有週三到期：1σ 用頭條（週五）的 DTE，不用週三的 DTE 1。"""
    mps = [
        {"expiry": "2026-10-07", "max_pain": 280.0, "distance_pct": 1.0},
        {"expiry": "2026-10-09", "max_pain": 270.0, "distance_pct": 5.4},
    ]
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ny_tz)
    text = _text(
        _at(
            now,
            lambda: create_tactical_symbol_embed(
                _data(
                    _iv(),
                    max_pain=270.0,
                    month_max_pains=mps,
                    max_pain_expiry="2026-10-09",
                )
            ),
        )
    )
    # 頭條 DTE 3：1σ ≈ 15.8，價差 14.68 在範圍內 → 不判收斂機率低；
    # 若誤取週三 DTE 1，1σ ≈ 9.1 會錯判為「收斂機率低」。
    assert "收斂機率低" not in text


def test_f1_friday_rolls_to_next_friday() -> None:
    mps = [{"expiry": "2026-10-16", "max_pain": 270.0, "distance_pct": 5.4}]
    now = datetime(2026, 10, 9, 11, 0, tzinfo=ny_tz)
    text = _text(
        _at(
            now,
            lambda: create_tactical_symbol_embed(
                _data(
                    _iv(),
                    max_pain=270.0,
                    month_max_pains=mps,
                    max_pain_expiry="2026-10-16",
                )
            ),
        )
    )
    assert "±$24.1" in text  # DTE 7


def test_f1_legacy_cache_without_expiry_skips_sigma() -> None:
    mps = [{"expiry": "2026-10-09", "max_pain": 270.0, "distance_pct": 5.4}]
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ny_tz)
    text = _text(
        _at(
            now,
            lambda: create_tactical_symbol_embed(
                _data(_iv(), max_pain=270.0, month_max_pains=mps)
            ),
        )
    )
    assert "收斂機率低" not in text


def test_f7_sigma_title_only_when_a_row_has_sigma() -> None:
    mps = [{"expiry": "2026-10-09", "max_pain": 270.0, "distance_pct": 5.4}]
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ny_tz)
    no_st = _iv(straddle_implied_iv=None, straddle_dte=None, straddle_expiry=None)
    t1 = _text(
        _at(
            now,
            lambda: create_tactical_symbol_embed(
                _data(no_st, max_pain=270.0, month_max_pains=mps)
            ),
        )
    )
    assert "±結算前1σ" not in t1
    t2 = _text(
        _at(
            now,
            lambda: create_tactical_symbol_embed(
                _data(_iv(), max_pain=270.0, month_max_pains=mps)
            ),
        )
    )
    assert "±結算前1σ" in t2


# --- F2 ---------------------------------------------------------------------


def test_f2_scale_corrected_label_uses_straddle_tenor() -> None:
    iv = _iv(
        iv_scale_corrected=True,
        current_iv_expiry="2026-10-16",
        current_iv_dte=9,
    )
    text = _text(create_tactical_symbol_embed(_data(iv)))
    assert "跨式反推 10-16 (DTE 9)" in text
    assert "±20% OI 加權" not in text
    assert "含結算 Gamma" not in text


# --- F3 / F4 / F5 -----------------------------------------------------------


def test_f3_intraday_hv_proxy_not_labeled_oi_weighted() -> None:
    iv = _iv(iv_source="HV_PROXY", current_iv=0.5)
    text = _text(create_tactical_symbol_embed(_data(iv)))
    assert "OI 加權" not in text
    assert "30D 歷史實現波動率代理（即時 IV 不可用）" in text


def test_f4_stored_iv_without_loading_shows_iv_hv20() -> None:
    iv = _iv(iv_source="STORED_IV", event_loading_applied=False)
    assert "IV/HV20:" in _text(create_tactical_symbol_embed(_data(iv)))


def test_f4_stored_iv_with_loading_hides_iv_hv20() -> None:
    iv = _iv(iv_source="STORED_IV", event_loading_applied=True)
    text = _text(create_tactical_symbol_embed(_data(iv))).replace("跨式 IV/HV20:", "")
    assert "IV/HV20:" not in text


def test_f4_hv_proxy_hides_iv_hv20() -> None:
    iv = _iv(iv_source="HV_PROXY")
    text = _text(create_tactical_symbol_embed(_data(iv))).replace("跨式 IV/HV20:", "")
    assert "IV/HV20:" not in text


@pytest.mark.parametrize(
    "now,open_,expected",
    [
        (datetime(2026, 10, 7, 17, 0, tzinfo=ny_tz), False, "盤後"),
        (datetime(2026, 10, 10, 1, 0, tzinfo=ny_tz), False, "休市"),
        (datetime(2026, 11, 27, 14, 0, tzinfo=ny_tz), False, "盤後"),  # 提前收盤日
        (datetime(2026, 10, 7, 8, 0, tzinfo=ny_tz), False, "盤前"),
        (datetime(2026, 10, 7, 11, 0, tzinfo=ny_tz), True, "盤中"),
    ],
)
def test_f5_session_phase(now: datetime, open_: bool, expected: str) -> None:
    with patch("market_time.is_market_open", return_value=open_):
        assert _session_phase(now) == expected


def test_f5_after_close_stored_iv_copy_not_previous_day() -> None:
    iv = _iv(iv_source="STORED_IV", is_premarket=True)
    now = datetime(2026, 10, 7, 17, 0, tzinfo=ny_tz)
    with patch("market_time.is_market_open", return_value=False):
        text = _text(_at(now, lambda: create_tactical_symbol_embed(_data(iv))))
    assert "當日盤中 IV 快取（已收盤）" in text
    assert "前日" not in text


def test_f5_premarket_stored_iv_keeps_previous_day_copy() -> None:
    iv = _iv(iv_source="STORED_IV", is_premarket=True)
    now = datetime(2026, 10, 7, 8, 0, tzinfo=ny_tz)
    with patch("market_time.is_market_open", return_value=False):
        text = _text(_at(now, lambda: create_tactical_symbol_embed(_data(iv))))
    assert "前日收盤 IV" in text


# --- F6 ---------------------------------------------------------------------


def test_f6_short_dte_straddle_not_extreme_vol() -> None:
    iv = _iv(straddle_implied_iv=0.82, straddle_dte=4, iv_source="HV_PROXY")
    text = _text(create_tactical_symbol_embed(_data(iv, psq_result=_psq())))
    assert "極端高波" not in text


def test_f6_long_dte_straddle_extreme_vol() -> None:
    iv = _iv(straddle_implied_iv=0.85, straddle_dte=9, iv_source="HV_PROXY")
    text = _text(create_tactical_symbol_embed(_data(iv, psq_result=_psq())))
    assert "極端高波環境 (跨式 IV 85%)" in text


# --- F7 ---------------------------------------------------------------------


def test_f7_no_iv_data_is_safe() -> None:
    create_tactical_symbol_embed(_data(None, psq_result=_psq()))
