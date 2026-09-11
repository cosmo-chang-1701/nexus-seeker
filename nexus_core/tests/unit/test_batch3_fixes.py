"""Unit tests for Batch 3 fixes:
- [ISSUE-4.4]: Net GEX Regime neutral deadband [-50k, 50k] and Heartbeat Block 4 GEX matrix inclusion of Regime & Flip.
- [ISS-12]: 15m multi-candle close confirmation and anti-washout buffer for PutWall breach (avoiding intraday wick false alarms).
"""

from unittest.mock import AsyncMock, patch
import pytest

from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext
from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed
from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.intraday_pipeline.evaluation import evaluate_watchlist_symbol


def test_portfolio_embed_net_gex_neutral_deadband() -> None:
    """ISSUE-4.4: 驗證 [-50k, +50k] 區間被歸類為 NEUTRAL_GAMMA (中性均衡)。"""
    test_cases = [
        (0.0, "⚖️ NEUTRAL_GAMMA (中性均衡)"),
        (25_000.0, "⚖️ NEUTRAL_GAMMA (中性均衡)"),
        (-30_000.0, "⚖️ NEUTRAL_GAMMA (中性均衡)"),
        (50_000.0, "⚖️ NEUTRAL_GAMMA (中性均衡)"),
        (-50_000.0, "⚖️ NEUTRAL_GAMMA (中性均衡)"),
        (100_000.0, "🟢 LONG_GAMMA (自穩定壓制波動)"),
        (-100_000.0, "🔴 SHORT_GAMMA (助漲助跌)"),
    ]

    for net_gex_val, expected_label in test_cases:
        data = {
            "symbol": "TEST",
            "price": 100.0,
            "quote": {
                "c": 100.0,
                "dp": 0.5,
                "d": 0.5,
                "o": 99.5,
                "h": 101.0,
                "l": 99.0,
                "pc": 99.5,
            },
            "rsi": 50.0,
            "bias_20": 0.0,
            "atr_14": 2.0,
            "gex_profile_data": {
                "spot": 100.0,
                "net_gex": net_gex_val,
                "call_wall": 110.0,
                "put_wall": 90.0,
                "gex_profile": {
                    90.0: -1_000_000.0,
                    100.0: 500_000.0,
                    110.0: 1_000_000.0,
                },
            },
        }
        embed = create_tactical_symbol_embed(data)
        found_label = False
        for f in embed.fields:
            if (
                f.name
                and "Gamma 曝險分布" in f.name
                and f.value
                and expected_label in f.value
            ):
                found_label = True
                break
        assert (
            found_label
        ), f"Expected {expected_label} in embed for net_gex={net_gex_val}"


def test_watchlist_signal_embed_includes_regime_and_gamma_flip() -> None:
    """ISSUE-4.4: 驗證 Heartbeat 2.0 Embed 在 Block 4 包含 Net GEX Regime 與 Gamma Flip。"""
    metrics = EnhancedWatchlistMetrics(
        symbol="AAPL",
        exchange="NASDAQ",
        current_price=150.0,
        beta=1.0,
        buy_zone_status="WATCH",
        buy_price_phase1=140.0,
        buy_price_phase2=135.0,
        buy_price_phase3=130.0,
        sell_zone_status="WATCH",
        sell_price_phase1=160.0,
        sell_price_phase2=165.0,
        sell_price_phase3=170.0,
        volume_poc=148.0,
        relative_strength_spy=1.0,
        option_skew_state="平穩",
    )
    symbol_gex = {
        "spot": 150.0,
        "net_gex": 150_000.0,
        "call_wall": 160.0,
        "put_wall": 140.0,
        "gex_profile": {140.0: -500_000.0, 150.0: 0.0, 160.0: 500_000.0},
    }
    embed = create_watchlist_signal_embed(
        symbol="AAPL",
        metrics=metrics,
        symbol_gex=symbol_gex,
        alert_level="green",
    )
    assert embed is not None
    gex_field = None
    for f in embed.fields:
        if f.name and "Gamma 曝險分布" in f.name:
            gex_field = f
            break
    assert gex_field is not None
    assert gex_field.value is not None
    assert "Net GEX Regime: +150K (🟢 LONG_GAMMA (自穩定壓制波動))" in gex_field.value
    assert "Gamma Flip:" in gex_field.value


@pytest.mark.asyncio
async def test_intraday_pipeline_putwall_breach_wick_filtering() -> None:
    """ISS-12: 驗證即時下影線刺穿但未獲 15m 實體收盤確認時，過濾假破位不發出枯竭預警。"""
    metrics = EnhancedWatchlistMetrics(
        symbol="NVDA",
        exchange="NASDAQ",
        current_price=98.0,  # Below PutWall (100.0), but inside buffer (100 - 1.5 * 2.0 = 97.0)
        beta=1.2,
        buy_zone_status="WATCH",
        buy_price_phase1=90.0,
        buy_price_phase2=85.0,
        buy_price_phase3=80.0,
        sell_zone_status="WATCH",
        sell_price_phase1=110.0,
        sell_price_phase2=115.0,
        sell_price_phase3=120.0,
        volume_poc=105.0,
        relative_strength_spy=1.0,
        option_skew_state="平穩",
        atr_14=10.2,  # 10.2 / 5.099 = 2.0 (atr_15m) -> buffer = 3.0 -> anti_washout = 97.0
        atr_15m=2.0,
    )
    event_context = WatchlistEventContext(
        risk_mode="normal",
        summary="無重大事件",
    )

    # Case A: is_gamma_cliff_confirmed returns False -> filtered as wick
    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new=AsyncMock(return_value=metrics),
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new=AsyncMock(return_value=event_context),
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new=AsyncMock(
            return_value={
                "spot": 98.0,
                "net_gex": 100_000.0,
                "call_wall": 110.0,
                "put_wall": 100.0,
                "gex_profile": {},
            }
        ),
    ), patch(
        "market_analysis.gamma_cliff_confirmation.is_gamma_cliff_confirmed",
        new=AsyncMock(return_value=False),
    ) as mock_cliff:
        eval_res = await evaluate_watchlist_symbol("NVDA")
        assert eval_res is not None
        assert "流動性枯竭預警" not in eval_res.tactical.action_guideline
        mock_cliff.assert_called_once_with("NVDA", 100.0)

    # Case B: is_gamma_cliff_confirmed returns True -> triggers warning
    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new=AsyncMock(return_value=metrics),
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new=AsyncMock(return_value=event_context),
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new=AsyncMock(
            return_value={
                "spot": 98.0,
                "net_gex": 100_000.0,
                "call_wall": 110.0,
                "put_wall": 100.0,
                "gex_profile": {},
            }
        ),
    ), patch(
        "market_analysis.gamma_cliff_confirmation.is_gamma_cliff_confirmed",
        new=AsyncMock(return_value=True),
    ):
        eval_res = await evaluate_watchlist_symbol("NVDA")
        assert eval_res is not None
        assert "流動性枯竭預警" in eval_res.tactical.action_guideline
        assert "實體貫穿確認跌破 Put Wall" in eval_res.tactical.action_guideline

    # Case C: spot is deep below anti-washout line (95.0 < 97.0) -> triggers warning without needing cliff check
    metrics_deep = metrics.model_copy(update={"current_price": 95.0})
    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new=AsyncMock(return_value=metrics_deep),
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new=AsyncMock(return_value=event_context),
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new=AsyncMock(
            return_value={
                "spot": 95.0,
                "net_gex": 100_000.0,
                "call_wall": 110.0,
                "put_wall": 100.0,
                "gex_profile": {},
            }
        ),
    ):
        eval_res = await evaluate_watchlist_symbol("NVDA")
        assert eval_res is not None
        assert "流動性枯竭預警" in eval_res.tactical.action_guideline
