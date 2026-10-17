"""負 Gamma（淨 GEX<0）不得再亮超跌磁吸／磁吸回升；/market Flip 行顯示 SPY Flip 與估算標示。"""

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

from cogs.embed_builders.market_embeds import (
    build_market_macro_overview_embed,
    build_radar_scan_embed,
)


def _scan(price: float, net_gex: float) -> List[Dict[str, Any]]:
    # max_pain=100、put_wall=50：現價恆高於 PutWall，單靠 net_gex 決定負 Gamma
    return [
        {
            "symbol": "XYZ",
            "quote": {"c": price, "dp": -1.0},
            "iv_metrics": {"iv_rank": 35.0, "expected_move_weekly": 5.0},
            "dte_er": 5,
            "skew": 0.5,
            "skew_percentile": 60.0,
            "max_pain": {"max_pain": 100.0},
            "uoa": [],
            "gex_metrics": {"put_wall": 50.0, "call_wall": 0.0, "net_gex": net_gex},
            "psq_result": {"is_squeezing": False, "momentum": 0.0},
        }
    ]


def _render(price: float, net_gex: float) -> str:
    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds = build_radar_scan_embed(_scan(price, net_gex), "ALL", 12345)
    parts: List[str] = []
    for e in embeds:
        parts.append(str(e.description or ""))
        for f in e.fields:
            parts.append(str(f.value))
    return "\n".join(parts)


def test_neg_gamma_deep_oversold_no_magnet_label() -> None:
    text = _render(price=90.0, net_gex=-5e8)  # dist -10%，現價 > PutWall
    assert "超跌磁吸" not in text
    assert "磁吸回升" not in text
    assert "🟢 超跌" not in text
    assert "勿抄底" in text


def test_neg_gamma_mild_dip_no_rebound_label() -> None:
    text = _render(price=97.0, net_gex=-5e8)  # dist -3%
    assert "磁吸回升" not in text
    assert "超跌磁吸" not in text
    assert "⚠️ XYZ" in text


def test_pos_gamma_control_keeps_magnet_labels() -> None:
    deep = _render(price=90.0, net_gex=5e8)
    assert "超跌磁吸" in deep
    assert "負Gamma" not in deep
    mild = _render(price=97.0, net_gex=5e8)
    assert "負Gamma" not in mild


def _macro(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "spx": 1000.0,
        "vix": 16.0,
        "vts_ratio": 0.9,
        "us10y": 4.2,
        "gamma_flip_line": 5180.0,
        "spy_spot": 99.0,
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


def _macro_text(**over: Any) -> str:
    embed = build_market_macro_overview_embed(_macro(**over))
    parts: List[str] = [str(embed.description or "")]
    for f in embed.fields:
        parts.append(str(f.value))
    return "\n".join(parts)


def test_flip_line_shows_spy_flip_and_buffer() -> None:
    text = _macro_text()
    assert "SPY Flip $100.00，緩衝 -1.00%" in text
    assert "≈" not in text


def test_flip_line_marks_derived_spot_as_estimate() -> None:
    text = _macro_text(spy_spot_is_derived=True)
    assert "緩衝 ≈-1.00%" in text
    assert "低於 Flip ≈-1.00%" in text


def test_flip_line_hidden_when_missing() -> None:
    text = _macro_text(spy_spot=None)
    assert "SPY Flip $" not in text


def _scan_dte0(price: float, net_gex: float) -> List[Dict[str, Any]]:
    from datetime import date

    rows = _scan(price, net_gex)
    # 到期日為今日（DTE 0），痛點高於現價 10% 以上
    rows[0]["month_max_pains"] = [
        {"expiry": date.today().isoformat(), "max_pain": price * 1.12}
    ]
    return rows


def _render_rows(rows: List[Dict[str, Any]]) -> str:
    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
        patch("market_time.days_to_expiry_et", return_value=0),
    ):
        embeds = build_radar_scan_embed(rows, "ALL", 12345)
    parts: List[str] = []
    for e in embeds:
        parts.append(str(e.description or ""))
        for f in e.fields:
            parts.append(str(f.value))
    return "\n".join(parts)


def test_neg_gamma_dte0_no_upward_expiry_magnet() -> None:
    text = _render_rows(_scan_dte0(100.0, -5e8))
    assert "向上末日磁吸" not in text
    assert "向上磁吸力" not in text


def test_pos_gamma_dte0_keeps_upward_expiry_magnet() -> None:
    text = _render_rows(_scan_dte0(100.0, 5e8))
    assert "向上磁吸力" in text
