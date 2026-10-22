"""雷達／心跳／CRO 洞察的 PutWall 三分類（助跌／紙牆／實牆）與賣方標籤一致性。"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

import market_time
from cogs.embed_builders import market_embeds as market_embeds_mod
from cogs.embed_builders.market_embeds import (
    build_market_macro_overview_embed,
    build_radar_scan_embed,
)
from market_analysis.gex_wall_depth import thin_wall_threshold
from market_analysis.insights_engine import (
    InsightsEngine,
    RiskInsightsContext,
    classify_putwall_gex,
)

_THIN = thin_wall_threshold(None)


def _row(
    put_wall_gex: Optional[float] = 5_000_000.0,
    dte_er: Optional[int] = None,
    iv_rank: Optional[float] = 35.0,
    term_structure: Optional[float] = None,
    net_gex: float = 10_000_000.0,
    iv_extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    iv: dict[str, Any] = {
        "expected_move_weekly": 5.0,
        "expected_move_lower": 105.0,
        "expected_move_upper": 115.0,
    }
    if iv_rank is not None:
        iv["iv_rank"] = iv_rank
    if term_structure is not None:
        iv["term_structure_ratio"] = term_structure
    if iv_extra:
        iv.update(iv_extra)
    row: dict[str, Any] = {
        "symbol": "TEST",
        "quote": {"c": 110.0, "dp": 0.5},
        "iv_metrics": iv,
        "skew": 0.5,
        "skew_percentile": 40.0,
        "max_pain": {"max_pain": 110.0},
        "uoa": [],
        "gex_profile_data": {
            "put_wall": 108.0,
            "call_wall": 130.0,
            "net_gex": net_gex,
        },
        "psq_result": {
            "is_squeezing": False,
            "momentum": 1.5,
            "signal_direction": "🟢",
        },
    }
    if dte_er is not None:
        row["dte_er"] = dte_er
    if put_wall_gex is not None:
        row["put_wall_gex"] = put_wall_gex
    return row


def _render(row: dict[str, Any]) -> str:
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
        embeds = build_radar_scan_embed([row], "ALL", 12345)
    parts: list[str] = []
    for e in embeds:
        parts.append(str(e.description or ""))
        for f in e.fields:
            parts.append(str(f.value))
    return "\n".join(parts)


def test_classify_putwall_gex() -> None:
    assert classify_putwall_gex(None, None) is None
    assert classify_putwall_gex(-1.0, None) == "助跌"
    assert classify_putwall_gex(0.0, None) == "紙牆"
    assert classify_putwall_gex(_THIN - 1, None) == "紙牆"
    assert classify_putwall_gex(_THIN, None) == "實牆"


def test_radar_insight_negative_putwall_is_not_support() -> None:
    text = _render(_row(put_wall_gex=-20.8e6))
    assert "強大流動性支撐" not in text
    assert "助跌" in text


def test_radar_insight_positive_putwall_keeps_old_text() -> None:
    text = _render(_row(put_wall_gex=20.8e6))
    assert "強大流動性支撐" in text


def test_radar_small_negative_putwall_not_called_paper_wall() -> None:
    text = _render(_row(put_wall_gex=-300_000.0))
    assert "單薄" not in text
    assert "紙牆" not in text


def test_radar_small_positive_putwall_keeps_paper_wall_texts() -> None:
    assert 300_000.0 < _THIN
    text = _render(_row(put_wall_gex=300_000.0))
    assert "僅單薄 +300K GEX" in text


def _heartbeat_text(put_wall_gex: float) -> str:
    from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed
    from models.schemas import EnhancedWatchlistMetrics

    metrics = EnhancedWatchlistMetrics(
        symbol="NVDA",
        exchange="NASDAQ",
        current_price=120.0,
        buy_zone_status="🟢 買點支撐",
        buy_price_phase1=115.0,
        buy_price_phase2=110.0,
        buy_price_phase3=105.0,
        sell_zone_status="🟢 賣點壓力",
        sell_price_phase1=125.0,
        sell_price_phase2=130.0,
        sell_price_phase3=135.0,
        pe_ratio=40.0,
        rsi_14=55.0,
        atr_14=4.0,
        beta=1.5,
        ma20=118.0,
        ma50=112.0,
        ma200=100.0,
        iv_rank=35.0,
        iv_percentile=40.0,
        option_skew=1.8,
        skew_percentile=55.0,
        option_skew_state="正常",
        pcr=0.75,
        volume_poc=116.0,
        volume_lvn=112.5,
        gex_max_put_wall=110.0,
        gex_max_call_wall=130.0,
        vanna_sensitivity=0.08,
        relative_strength_spy=1.2,
        iv_source="LIVE_IV",
        is_premarket=False,
    )
    embed = create_watchlist_signal_embed(
        symbol="NVDA",
        metrics=metrics,
        symbol_gex={
            "call_wall": 130.0,
            "put_wall": 110.0,
            "call_wall_gex": 850_000.0,
            "put_wall_gex": put_wall_gex,
            "net_gex": 730_000.0,
        },
        alert_level="green",
    )
    assert embed is not None
    return "\n".join(f.value for f in embed.fields if f.value)


@pytest.mark.parametrize(
    "gex,tag",
    [(-20.8e6, "[助跌]"), (120_000.0, "[薄]"), (20.8e6, "[厚]")],
)
def test_heartbeat_putwall_tag(gex: float, tag: str) -> None:
    text = _heartbeat_text(gex)
    assert f"$110.00 {tag}" in text


def _ctx(put_wall_gex: Optional[float]) -> RiskInsightsContext:
    return RiskInsightsContext(
        symbol="TEST",
        current_price=110.0,
        put_wall=108.0,
        net_gex_status="POSITIVE_GAMMA",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=35.0,
        max_pain_deviation_pct=0.0,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        put_wall_gex=put_wall_gex,
    )


def test_cro_insight_negative_putwall_is_support_zone_label() -> None:
    dmp, status, _ = InsightsEngine.generate_cro_insight(_ctx(-20.8e6))
    assert dmp == "[⚠️ 助跌區]"
    assert status == "⚠️ PutWall 助跌區(淨GEX為負)"


def test_cro_insight_small_negative_is_not_paper_wall() -> None:
    dmp, status, _ = InsightsEngine.generate_cro_insight(_ctx(-1_000.0))
    assert "紙牆" not in (dmp or "") + (status or "")


def test_cro_insight_thin_positive_keeps_paper_wall_labels() -> None:
    dmp, status, _ = InsightsEngine.generate_cro_insight(_ctx(1_000.0))
    assert dmp == "[⚠️ 薄弱紙牆]"
    assert status == "⚠️ 薄弱紙牆(無做市商深度)"


def test_seller_label_backwardation_with_earnings() -> None:
    assert "⚠️財報事件期" in _render(_row(dte_er=5, term_structure=1.2))


def test_seller_label_backwardation_without_earnings() -> None:
    text = _render(_row(dte_er=40, term_structure=1.2))
    assert "⚠️總經事件防禦期" in text
    assert "⚠️財報事件期" not in text


def test_seller_label_neg_gamma_beats_earnings() -> None:
    text = _render(_row(dte_er=5, net_gex=-10_000_000.0))
    assert "🔴禁賣Put" in text
    assert "⚠️財報事件期" not in text


def test_seller_label_low_ivr() -> None:
    assert "🔴CSP 禁售" in _render(_row(dte_er=5, iv_rank=10.0))


def test_seller_label_ivr_missing_beats_earnings() -> None:
    text = _render(_row(dte_er=5, iv_rank=None))
    assert "⚪IVR 資料不足" in text
    assert "⚠️財報事件期" not in text


def test_seller_label_earnings_beats_negative_putwall() -> None:
    text = _render(_row(dte_er=5, put_wall_gex=-5e6))
    assert "⚠️財報事件期" in text
    assert "⚠️牆助跌" not in text


def test_pwall_cell_suffix_by_class() -> None:
    assert "$108.0(助)" in _render(_row(put_wall_gex=-5e6, dte_er=40))
    assert "$108.0(薄)" in _render(_row(put_wall_gex=300_000.0, dte_er=40))
    solid = _render(_row(put_wall_gex=20e6, dte_er=40))
    assert "$108.0(薄)" not in solid and "$108.0(助)" not in solid


def test_seller_label_earnings_only() -> None:
    assert "⚠️財報事件期" in _render(_row(dte_er=5))


def test_seller_label_earnings_via_iv_metrics_fallback() -> None:
    text = _render(_row(iv_extra={"has_earnings_event": True}))
    assert "⚠️財報事件期" in text


def test_seller_label_negative_putwall_only() -> None:
    text = _render(_row(dte_er=40, put_wall_gex=-5_000_000.0))
    assert "⚠️牆助跌" in text
    assert "🟢適宜賣方" not in text


def test_seller_label_clean() -> None:
    text = _render(_row(dte_er=40))
    assert "🟢適宜賣方" in text


def _macro(vix: float) -> dict[str, Any]:
    return {
        "spx": 1000.0,
        "vix": vix,
        "us10y": 5.146,
        "wti": 98.21,
        "gamma_flip_line": 5180.0,
        "spy_spot": 100.0,
        "spy_gamma_flip": 100.0,
    }


def _complacency(vix: float, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        market_embeds_mod,
        "_now_et",
        lambda: datetime(2026, 10, 15, 11, 0, tzinfo=market_time.ny_tz),
    )
    embed = build_market_macro_overview_embed(_macro(vix))
    return "\n".join(
        [str(embed.description or "")] + [f"{f.name}\n{f.value}" for f in embed.fields]
    )


def test_complacency_shown_at_vix_16_68(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "⚠ 低波自滿（參考）" in _complacency(16.68, monkeypatch)


def test_complacency_hidden_at_vix_20_5(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "低波自滿" not in _complacency(20.5, monkeypatch)
