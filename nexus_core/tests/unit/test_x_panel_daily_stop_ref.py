"""/x 日線參考停損（PR-C 審查修正 C-F1）：ATR₁D 取值與輸出條件。"""

from typing import Any

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.room_threshold import (
    _DAILY_NOISE_STOP_ATR_1D_MULT,
    is_valid_daily_atr,
)


def _text(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _base(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 154.0,
        "atr_1d": 2.5,
        "atr_15m": 0.5,
        "gex_profile_data": {
            "spot": 154.0,
            "put_wall": 150.0,
            "call_wall": 160.0,
            "net_gex": 5_000_000.0,
            "gex_profile": {
                "150.0": 2_000_000.0,
                "154.0": 1_000_000.0,
                "160.0": 3_000_000.0,
            },
        },
    }
    d.update(extra)
    return d


def test_constant_and_validity_helper() -> None:
    assert _DAILY_NOISE_STOP_ATR_1D_MULT == 0.25
    assert is_valid_daily_atr(2.5)
    assert not is_valid_daily_atr(0.01)
    assert not is_valid_daily_atr(0.0)
    assert not is_valid_daily_atr(float("nan"))


def test_normal_case_appended_to_stop_line_with_correct_value() -> None:
    text = _text(_base())
    # 150 - 0.25 * 2.5 = 149.375 → 149.38；(154 - 149.375) / 154 = 3.00%
    assert " ｜日線參考 $149.38 (↓3.00%)" in text
    stop_line = next(ln for ln in text.splitlines() if "結構停損" in ln)
    assert "日線參考" in stop_line  # 同一行尾註，不另起一行


def test_atr_1d_preferred_then_atr_14_fallback() -> None:
    d = _base()
    d.pop("atr_1d")
    d["atr_14"] = 2.5
    assert "日線參考 $149.38" in _text(d)
    d2 = _base(atr_1d=4.0, atr_14=2.5)
    assert "日線參考 $149.00" in _text(d2)  # 150 - 0.25 * 4.0


def test_placeholder_atr_is_not_used() -> None:
    d = _base(atr_1d=0.01)
    assert "日線參考" not in _text(d)
    d2 = _base(atr_1d=0.01, atr_14=0.01)
    assert "日線參考" not in _text(d2)


def test_putwall_above_price_not_output() -> None:
    d = _base(price=148.0)
    d["gex_profile_data"]["spot"] = 148.0
    assert "日線參考" not in _text(d)


def test_putwall_degrade_branch_not_output() -> None:
    d = {
        "symbol": "X",
        "price": 100.0,
        "atr_1d": 2.0,
        "gex_profile_data": {
            "put_wall": 101.0,
            "gex_profile": {"100.0": 1_000_000, "101.0": 2_000_000},
        },
        "atr_15m": 0.2,
    }
    text = _text(d)
    assert "PutWall異常降級" in text
    assert "日線參考" not in text


def test_net_gex_support_zone_branch_not_output() -> None:
    d = {
        "symbol": "MU",
        "price": 1097.39,
        "atr_1d": 14.0,
        "gex_profile_data": {
            "put_wall": 1050.0,
            "gex_profile": {
                "1050.0": -138_988_000,
                "1070.0": 200_000_000,
                "1100.0": -1_000_000,
            },
        },
        "atr_15m": 9.70,
    }
    text = _text(d)
    assert "實為助跌區" in text
    assert "日線參考" not in text
