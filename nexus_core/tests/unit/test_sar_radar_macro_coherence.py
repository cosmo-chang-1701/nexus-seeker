"""雷達／即時洞察一致性：負 Gamma 否決 Max Pain 磁吸，與「禁賣Put」標示。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.market_embeds import build_radar_scan_embed
from market_analysis.insight_generator import compute_realtime_insights


def _scan(put_wall: float) -> list[dict[str, Any]]:
    return [
        {
            "symbol": "AAOI",
            "quote": {"c": 106.29, "dp": -1.5},
            "iv_metrics": {
                "iv_rank": 35.0,
                "expected_move_weekly": 5.0,
                "expected_move_lower": 108.0,
                "expected_move_upper": 118.0,
            },
            "dte_er": 5,
            "skew": 0.5,
            "skew_percentile": 60.0,
            "max_pain": {"max_pain": 120.0},
            "uoa": [],
            "gex_metrics": {"put_wall": put_wall, "call_wall": 0.0},
            "psq_result": {
                "is_squeezing": False,
                "momentum": -1.2,
                "signal_direction": "🔴",
            },
        }
    ]


def _render(put_wall: float, orders: list[dict[str, Any]]) -> str:
    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=orders),
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
        embeds = build_radar_scan_embed(_scan(put_wall), "ALL", 12345)
    parts: list[str] = []
    for e in embeds:
        parts.append(str(e.description or ""))
        for f in e.fields:
            parts.append(str(f.value))
    return "\n".join(parts)


_BUY_ORDER: dict[str, Any] = {
    "id": 7,
    "symbol": "AAOI",
    "side": "BUY",
    "limit_price": 100.0,
}


def test_radar_neg_gamma_suppresses_magnet_without_order() -> None:
    text = _render(put_wall=110.0, orders=[])
    for banned in ("多頭磁吸引力", "限價捕獵", "捕獸夾"):
        assert banned not in text


def test_radar_neg_gamma_with_order_asks_reassessment() -> None:
    text = _render(put_wall=110.0, orders=[_BUY_ORDER])
    assert (
        "負 Gamma 環境 Max Pain 磁吸不成立，既有限價買單 ID: 7 ($100.00) 建議重新評估"
        in text
    )
    for banned in ("多頭磁吸引力", "限價捕獵", "捕獸夾"):
        assert banned not in text


def test_radar_positive_gamma_keeps_magnet_advice() -> None:
    # put_wall 低於現價且無 net_gex 負值 -> 非負 Gamma
    text = _render(put_wall=90.0, orders=[])
    assert "多頭磁吸引力" in text


def test_radar_label_is_sell_put_ban() -> None:
    text = _render(put_wall=110.0, orders=[])
    assert "🔴禁賣Put" in text
    assert "賣方禁售" not in text


def _insight(**over: Any) -> str:
    data: dict[str, Any] = {
        "symbol": "AAOI",
        "spot": 100.0,
        "max_pain": 120.0,
        "put_wall": 50.0,
        "gex_status": "NEGATIVE_GAMMA_ZONE",
    }
    data.update(over)
    return compute_realtime_insights(data)


def test_insight_neg_non_crisis_has_no_bullish_advice() -> None:
    text = _insight()
    assert "磁吸效應不成立" in text
    assert "Covered Call" in text
    for banned in ("CSP", "Long Call", "Bull Put Spread", "逢低吸納", "向上磁吸"):
        assert banned not in text


def test_insight_neg_low_ivr_has_no_long_call() -> None:
    text = _insight(iv_rank=5.0, skew_percentile=80.0)
    for banned in ("CSP", "Long Call", "Debit Spread", "Bull Put Spread"):
        assert banned not in text


def test_insight_positive_gamma_unchanged() -> None:
    text = _insight(gex_status="POSITIVE_GAMMA")
    assert "向上磁吸效應" in text
    assert "磁吸效應不成立" not in text
    assert "逢低吸納" in _insight(gex_status="POSITIVE_GAMMA", max_pain=105.0)
