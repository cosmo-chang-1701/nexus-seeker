"""/market 總經面板標示：Flip 三態、TED 改名、RRP 小基數、低波自滿（僅呈現）。"""

from typing import Any
from unittest.mock import patch

from cogs.embed_builders.market_embeds import (
    build_market_macro_overview_embed,
    build_radar_scan_embed,
)


def _text(embed: Any) -> str:
    parts = [str(embed.description or "")]
    for f in embed.fields:
        parts.append(str(f.name))
        parts.append(str(f.value))
    return "\n".join(parts)


def _macro(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spx": 5150.0,
        "vix": 18.0,
        "us10y": 4.25,
        "gamma_flip_line": 5180.0,
        "spy_spot": 100.0,
        "spy_gamma_flip": 100.0,
        "wti": 75.0,
        "rrp": 420.5,
        "rrp_change_30d": 5.0,
        "fed_balance": 7.25,
        "cpi_nfp_calendar": "近期無重大數據",
        "fear_greed": 48.0,
        "uer": 4.0,
        "sahm_rule": 0.1,
        "payout_threshold": 13000.0,
        "short_gamma_critical": False,
        "recession_warning": False,
        "fedwatch_probability": 0.3,
        "fedwatch_is_fallback": False,
        "fedwatch_details": {},
        "escape_win_status": "NEUTRAL",
        "gex_is_fallback": False,
        "gex_is_expired": False,
    }
    base.update(over)
    return base


def _render(**over: Any) -> str:
    return _text(build_market_macro_overview_embed(_macro(**over)))


def test_flip_buffer_negative_shows_negative_gamma_zone() -> None:
    text = _render(spy_spot=99.7, spy_gamma_flip=100.0)
    assert "負 Gamma 區（低於 Flip -0.30%）" in text
    assert "NORMAL (網格步長正常)" not in text
    assert "(SPY 緩衝 -0.30%)" in text


def test_flip_buffer_knife_edge() -> None:
    text = _render(spy_spot=100.07, spy_gamma_flip=100.0)
    assert "臨界（緩衝 +0.07%）" in text
    assert "NORMAL (網格步長正常)" not in text


def test_flip_buffer_comfortable_stays_normal() -> None:
    text = _render(spy_spot=102.0, spy_gamma_flip=100.0)
    assert "NORMAL (網格步長正常)" in text
    assert "臨界" not in text
    assert "(SPY 緩衝 +2.00%)" in text


def test_flip_buffer_hidden_when_expired_or_missing() -> None:
    expired = _render(spy_spot=99.0, spy_gamma_flip=100.0, gex_is_expired=True)
    assert "SPY 緩衝" not in expired
    assert "負 Gamma 區" not in expired
    missing = _render(spy_spot=None, spy_gamma_flip=100.0)
    assert "SPY 緩衝" not in missing


def test_critical_keeps_existing_label() -> None:
    text = _render(spy_spot=99.0, spy_gamma_flip=100.0, short_gamma_critical=True)
    assert "CRITICAL (網格步長 1.5x 已生效)" in text
    assert "負 Gamma 區" not in text


def test_ted_label_renamed_radar() -> None:
    scan = [
        {
            "symbol": "SPY",
            "quote": {"c": 500.0, "dp": 0.5},
            "iv_metrics": {"iv_rank": 20.0, "expected_move_weekly": 5.0},
            "max_pain": {"max_pain": 500.0},
        }
    ]
    with patch("database.cache.get_kv_cache") as mock_kv:
        mock_kv.side_effect = lambda k: "0.03" if k == "macro_ted_spread" else None
        embeds = build_radar_scan_embed(scan, "ALL", 12345)
    text = "\n".join(_text(e) for e in embeds)
    assert "CP−T-Bill 利差 (TED 代理)" in text
    assert "TED Spread (流動性指標)" not in text


def test_rrp_small_base_hides_percentage() -> None:
    text = _render(rrp=2.3, rrp_change_30d=477.0)
    assert "477" not in text
    assert "小基數不計%" in text
    assert "+1.9B" in text  # 2.3 - 2.3/5.77


def test_rrp_small_base_total_drain_omits_bracket() -> None:
    text = _render(rrp=0.3, rrp_change_30d=-100.0)
    assert "小基數" not in text


def test_rrp_large_base_keeps_percentage() -> None:
    text = _render(rrp=420.5, rrp_change_30d=5.0)
    assert "30天變動" in text and "+5.0%" in text


def test_complacency_fires_on_low_vix_high_yield() -> None:
    text = _render(vix=15.4, us10y=5.23, wti=91.0)
    assert "低波自滿（參考）：VIX 15.4 未反映" in text
    assert "10Y 5.23%" in text and "WTI $91" in text


def test_complacency_fires_on_hike_probability() -> None:
    text = _render(vix=14.0, fedwatch_details={"prob_hike": 18.8})
    assert "升息機率 19%" in text


def test_complacency_not_fired() -> None:
    assert "低波自滿" not in _render(vix=18.0, us10y=5.23, wti=91.0)
    assert "低波自滿" not in _render(vix=14.0, us10y=4.2, wti=70.0)
    stale = _render(
        vix=14.0,
        fedwatch_details={"prob_hike": 30.0},
        fedwatch_is_stale=True,
    )
    assert "低波自滿" not in stale
