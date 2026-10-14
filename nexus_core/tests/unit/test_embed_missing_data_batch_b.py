"""R4/R5 缺資料降級（batch b）：缺值顯示 `--`，不得以 0 冒充；以缺值做的判斷需跳過。"""

from types import SimpleNamespace
from typing import Any

import discord

from cogs.embed_builders._ansi_utils import _positive_float_or_none
from cogs.embed_builders.hedge_embeds import (
    create_hedge_alert_embed,
    create_proactive_event_alert_embed,
)
from cogs.embed_builders.market_embeds import (
    build_market_macro_overview_embed,
    build_radar_scan_embed,
    create_iv_risk_scan_embed,
    create_max_pain_embed,
)
from cogs.embed_builders.order_embeds.active_orders import (
    _build_active_order_ansi_card,
)
from cogs.embed_builders.order_embeds.post_market_intelligence import (
    build_post_market_intelligence_embed,
)
from cogs.embed_builders.order_embeds.pre_market_briefing import (
    build_pre_market_briefing_embed,
)
from cogs.embed_builders.order_embeds.telemetry_alignment import (
    create_telemetry_alignment_embeds,
)
from cogs.embed_builders.report_embeds import (
    build_hedge_analysis_field,
    build_scan_report,
    create_ddp_embed,
)
from cogs.embed_builders.rollover_embeds import (
    create_short_entry_embed,
    create_transition_pyramid_embed,
)
from cogs.embed_builders.scan_embeds.covered_call import create_cc_recovery_embed
from cogs.embed_builders.scan_embeds.earnings_and_sector_flow import (
    create_sector_flow_report_embed,
)
from cogs.embed_builders.scan_embeds.macro_and_fomc import (
    create_fomc_escape_window_embed,
    create_macro_scan_embed,
)
from cogs.embed_builders.scan_embeds.sentiment_scan import (
    create_sentiment_scan_embed,
)
from cogs.embed_builders._embed_helpers import (
    UOA_HEURISTIC_DISCLOSURE,
    UOA_OI_PREV_DAY_NOTE,
)
from market_analysis.uoa_telemetry import UOATradeResult, generate_uoa_ascii_table


def _text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts.append(f"{f.name}\n{f.value}")
    return "\n".join(parts)


# ---------------------------------------------------------------- sentiment_scan


def _iv(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "current_iv": 0.3,
        "iv_rank": 40.0,
        "iv_percentile": 50.0,
        "expected_move_weekly": 5.0,
        "iv_status": "Normal",
        "is_premarket": False,
        "iv_source": "LIVE_IV",
    }
    base.update(kw)
    return base


def test_sentiment_iv_unavailable_shows_dash_and_title_suffix() -> None:
    embed = create_sentiment_scan_embed(
        "AAA",
        {"skew": 1.0, "state": "x"},
        {},
        [],
        {},
        iv_data=_iv(current_iv=0.0, iv_source="UNAVAILABLE"),
    )
    assert "[降級模式]" in (embed.title or "")
    text = _text(embed)
    assert "0.0%" not in text
    assert "--%" in text


def test_sentiment_iv_live_has_no_degraded_suffix() -> None:
    embed = create_sentiment_scan_embed("AAA", {"skew": 1.0}, {}, [], {}, _iv())
    assert "[降級模式]" not in (embed.title or "")
    assert "30.0%" in _text(embed)


def test_sentiment_skew_missing_shows_dash() -> None:
    embed = create_sentiment_scan_embed("AAA", {}, {}, [], {}, _iv())
    text = _text(embed)
    assert "Option Skew" in text
    skew_line = next(ln for ln in text.splitlines() if "Option Skew" in ln)
    assert "--%" in skew_line and "0.00%" not in skew_line


def test_sentiment_uoa_discloses_heuristic_and_legacy_premium_dash() -> None:
    uoa = [
        {
            "expiry": "2099-01-01",
            "strike": 100.0,
            "type": "CALL",
            "volume": 5000,
            "oi": 100,
            "ratio": 50.0,
            "trade_type": "SWEEP",
            "symbol": "AAA",
        }
    ]
    embed = create_sentiment_scan_embed("AAA", {"skew": 1.0}, {}, uoa, {}, _iv())
    text = _text(embed)
    assert UOA_HEURISTIC_DISCLOSURE in text
    assert UOA_OI_PREV_DAY_NOTE in text
    assert "$0.0k" not in text


def test_uoa_table_zero_trade_price_is_dash_real_price_is_formatted() -> None:
    def trade(price: float) -> UOATradeResult:
        return UOATradeResult(
            expiry="2099-01-01",
            strike_price=100.0,
            option_type="CALL",
            trade_price=price,
            bid_price=0.0,
            ask_price=0.0,
            volume=1000,
            open_interest=10,
            ratio=100.0,
            ratio_str="100x",
            action="BTO",
            intent="x",
            symbol="AAA",
        )

    assert "$0.0k" not in generate_uoa_ascii_table([trade(0.0)])
    assert "$500.0k" in generate_uoa_ascii_table([trade(5.0)])


# ---------------------------------------------------- earnings_and_sector_flow


def test_sector_flow_missing_spy_vix_pct_dash_and_suffix() -> None:
    embed = create_sector_flow_report_embed(
        "t",
        "report",
        {
            "vix": None,
            "spy_price": None,
            "sectors": [
                {"symbol": "XLK", "name": "Tech", "pct_change": None, "rel_vol": None}
            ],
        },
    )
    assert "[降級模式]" in (embed.title or "")
    text = _text(embed)
    assert "$0.00" not in text
    assert "SPY 現價：** `--`" in text.replace("**", "**")
    assert "+0.00%" not in text
    assert "--%" in text


def test_sector_flow_full_data_no_suffix() -> None:
    embed = create_sector_flow_report_embed(
        "t", "report", {"vix": 19.2, "spy_price": 528.5, "sectors": []}
    )
    assert "[降級模式]" not in (embed.title or "")
    assert "$528.50" in _text(embed)


# -------------------------------------------------------------------- covered_call


def test_cc_recovery_fallback_iv_missing_and_present() -> None:
    missing = create_cc_recovery_embed(
        {"symbol": "AAA", "current_price": 10.0, "recommendations": []}
    )
    assert "--%" in _text(missing)
    assert "0.00%" not in _text(missing)
    present = create_cc_recovery_embed(
        {
            "symbol": "AAA",
            "current_price": 10.0,
            "recommendations": [],
            "fallback_iv": 0.35,
        }
    )
    assert "35.00% (HV 代理/備援)" in _text(present)


# ------------------------------------------------------------------ macro_and_fomc


def test_positive_float_or_none() -> None:
    assert _positive_float_or_none(0.0) is None
    assert _positive_float_or_none(None) is None
    assert _positive_float_or_none("x") is None
    assert _positive_float_or_none(3) == 3.0


def test_macro_scan_missing_values_dash_degraded_no_healthy_verdict() -> None:
    embed = create_macro_scan_embed({"dxy": None, "tnx": 4.0, "us2y": 3.5, "vix": None})
    assert "[降級模式]" in (embed.title or "")
    text = _text(embed)
    assert "0.00" not in text
    assert "--" in text
    assert "全數合規" not in text
    assert "無法判定" in text


def test_macro_scan_full_data_healthy() -> None:
    embed = create_macro_scan_embed(
        {
            "dxy": 100.0,
            "tnx": 4.0,
            "tnx_change_bps": 1.0,
            "us2y": 3.5,
            "vix": 14.0,
            "vix_change": -0.5,
        }
    )
    assert "[降級模式]" not in (embed.title or "")
    assert "100.00" in _text(embed)


def test_fomc_prob_stale_title_suffix() -> None:
    kwargs: dict[str, Any] = dict(
        direction="持平",
        shift_days=0,
        adjusted_start="a",
        adjusted_end="b",
        reason="r",
    )
    stale = create_fomc_escape_window_embed(None, prob_stale=True, **kwargs)
    assert "[資料過期]" in (stale.title or "")
    fresh = create_fomc_escape_window_embed(0.5, **kwargs)
    assert "[資料過期]" not in (fresh.title or "")


# ------------------------------------------------------------------ active_orders


def test_active_order_missing_prices_dash() -> None:
    card = _build_active_order_ansi_card(
        {
            "id": 1,
            "symbol": "AAA",
            "order_type": "STOP_LIMIT",
            "quantity": 1,
            "validity": "DAY",
            "limit_price": None,
        }
    )
    assert "$0.00" not in card
    assert "限價: \u001b[1;32m--" in card
    assert "停損價: \u001b[1;31m--" in card


# ---------------------------------------------------------- telemetry_alignment


def _tele_item(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "AAA",
        "order_id": 7,
        "current_price": 145.0,  # 限價
        "original_qty": 10.0,
        "live_price": 146.5,
        "avg_cost": 140.0,
        "gain_loss_pct": 4.0,
        "put_wall": 130.0,
        "wall_dist_pct": 10.0,
        "skew_val": 2.0,
        "skew_pct": 50.0,
        "iv_val": 35.0,
        "iv_rank": 25.0,
        "iv_status": "Normal",
        "proximity_pct": 1.0,
        "is_premarket": False,
        "iv_source": "LIVE_IV",
        "side": "BUY",
        "order_type": "LIMIT",
    }
    base.update(kw)
    return base


def test_telemetry_missing_live_price_does_not_show_limit_as_spot() -> None:
    [embed] = create_telemetry_alignment_embeds([_tele_item(live_price=None)])
    text = _text(embed)
    assert "當前現價: \u001b[1;37m--" in text
    assert "當前現價: \u001b[1;37m$145.00" not in text
    assert "$145.00" in text  # 限價仍在委託雷達顯示
    assert "[降級模式]" in (embed.title or "")
    assert "[數據未更新/降級模式]" in (embed.fields[0].name or "")


def test_telemetry_live_price_present_shows_spot_no_suffix() -> None:
    [embed] = create_telemetry_alignment_embeds([_tele_item()])
    assert "當前現價: \u001b[1;37m$146.50" in _text(embed)
    assert "降級" not in (embed.title or "")


# --------------------------------------------------------------- pre_market_briefing


def test_pre_market_briefing_missing_macro_dash_and_degraded_title() -> None:
    embed = build_pre_market_briefing_embed({"dxy": None, "vix": None})
    assert "[盤前數據未更新/降級模式]" in (embed.title or "")
    assert "總經平穩" not in (embed.title or "")
    text = _text(embed)
    assert "VIX 0.00" not in text
    assert "指標全數合規" not in text
    assert "--" in text


def test_pre_market_briefing_full_macro_keeps_stable_title() -> None:
    embed = build_pre_market_briefing_embed(
        {
            "dxy": 100.0,
            "tnx": 4.0,
            "tnx_change_bps": 1.0,
            "us2y": 3.5,
            "vix": 14.0,
            "vix_change": -0.5,
        }
    )
    assert "總經平穩" in (embed.title or "")
    assert "降級" not in (embed.title or "")


# --------------------------------------------------------- post_market_intelligence


def test_post_market_sector_missing_change_not_leader() -> None:
    sectors: list[dict[str, Any]] = [
        {"symbol": "XLK", "name": "Tech", "pct_change": 1.2, "rel_vol": 1.0},
        {"symbol": "XLE", "name": "Energy", "pct_change": None, "rel_vol": None},
    ]
    embeds = build_post_market_intelligence_embed([], sectors_data=sectors)
    sector_text = "\n".join(
        f.value or "" for e in embeds for f in e.fields if "板塊輪動" in (f.name or "")
    )
    lead, _, rest = sector_text.partition("資料缺失")
    assert "XLK" in lead and "XLE" not in lead
    assert "XLE" in rest and "--%" in rest
    assert "+0.00%" not in sector_text


# ----------------------------------------------------------------- report_embeds


def test_scan_report_missing_greeks_and_footer_dash() -> None:
    embed = build_scan_report({"symbol": "AAA"})
    text = _text(embed)
    assert "+0.000" not in text
    assert "--" in text
    footer = embed.footer.text or ""
    assert "VIX --" in footer and "WTI --" in footer
    assert "$0.0" not in footer
    assert "⚪" in footer


def test_scan_report_full_data_unchanged() -> None:
    embed = build_scan_report(
        {
            "symbol": "AAA",
            "delta": 0.2,
            "theta": -0.01,
            "gamma": 0.001,
            "iv": 0.35,
            "projected_exposure_pct": 20.0,
            "risk_limit": 15.0,
            "macro_vix": 30.0,
            "macro_oil": 80.0,
            "spy_price": 500.0,
        }
    )
    assert "+0.200" in _text(embed)
    footer = embed.footer.text or ""
    assert "VIX 30 🔴" in footer and "SPY: $500.0" in footer


def test_ddp_nonpositive_pe_shows_na() -> None:
    embed = create_ddp_embed(
        {
            "symbol": "AAA",
            "current_pe": -5.0,
            "pe_mean_3y": 20.0,
            "eps_growth": 0.1,
            "rev_accel": True,
            "confidence_score": 80,
            "forward_pe": 10.0,
        }
    )
    text = _text(embed)
    assert "N/A" in text
    assert "+0.0%" not in text


def test_hedge_analysis_field_missing_values_dash() -> None:
    embed = discord.Embed()
    build_hedge_analysis_field(embed, {"status": "OPTIMAL"})
    value = embed.fields[0].value or ""
    assert "$0.00" not in value and "0.00%" not in value
    assert "--" in value and "無法判定" in value


# ------------------------------------------------------------------ market_embeds


def _radar_row(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "ZZZ",
        "quote": {"c": 100.0, "dp": 1.0},
        "iv_metrics": {"iv_rank": 50.0, "expected_move_weekly": 5.0},
        "skew": 1.0,
        "max_pain": {"max_pain": 100.0},
        "psq_result": {"momentum": 1.5, "direction": "Long"},
    }
    row.update(kw)
    return row


def _md_row(embed: discord.Embed, sym: str) -> str:
    for f in embed.fields:
        for ln in (f.value or "").splitlines():
            if ln.startswith("| ") and f" {sym} |" in ln:
                return ln
    raise AssertionError("row not found")


def test_radar_missing_quote_and_sqz_dash_and_title_suffix() -> None:
    [embed] = build_radar_scan_embed([_radar_row(quote={}, psq_result=None)], "ALL", 1)
    cells = [c.strip() for c in _md_row(embed, "ZZZ").split("|")]
    assert cells[2] == "--"  # 現價
    assert cells[5] == "--"  # SQZ
    assert "$0.00" not in _md_row(embed, "ZZZ")
    assert (embed.title or "").endswith("[快取/降級]")


def test_radar_complete_row_no_suffix_and_sqz_shown() -> None:
    [embed] = build_radar_scan_embed(
        [_radar_row(gex_metrics={"put_wall": 95.0})], "ALL", 1
    )
    cells = [c.strip() for c in _md_row(embed, "ZZZ").split("|")]
    assert cells[2].startswith("$100.00")
    assert cells[5] != "--"
    assert "降級" not in (embed.title or "")
    assert "Max Pain×0.9" not in _text(embed)


def test_radar_proxy_put_wall_marked_with_legend() -> None:
    [embed] = build_radar_scan_embed([_radar_row()], "ALL", 1)
    row = _md_row(embed, "ZZZ")
    assert "$90.0*" in row
    assert "Max Pain×0.9" in _text(embed)


def test_radar_real_put_wall_not_marked() -> None:
    [embed] = build_radar_scan_embed(
        [_radar_row(gex_metrics={"put_wall": 95.0, "call_wall": 110.0})], "ALL", 1
    )
    assert "$95.0*" not in _md_row(embed, "ZZZ")
    assert "Max Pain×0.9" not in _text(embed)


def test_max_pain_embed_missing_distance_dash() -> None:
    embed = create_max_pain_embed(
        "AAA", {"max_pain": 100.0, "current_price": 100.0, "expiry": "2099-01-01"}
    )
    text = _text(embed)
    assert "`--%`" in text
    assert "貼近最大痛點" not in text


def test_iv_risk_scan_missing_dash() -> None:
    embed = create_iv_risk_scan_embed([{"symbol": "AAA", "strategy": "x"}])
    f = embed.fields[0]
    assert "IVR: --%" in (f.name or "")
    assert "TTE: `--`h" in (f.value or "")


def test_macro_overview_degraded_title_and_missing_probs_dash() -> None:
    base: dict[str, Any] = {
        "fedwatch_probability": 0.5,
        "fedwatch_details": {"prob_maintain": 80.0},
    }
    ok = build_market_macro_overview_embed(dict(base))
    assert "降級" not in (ok.title or "")
    assert "--%" in _text(ok)
    degraded = build_market_macro_overview_embed({**base, "is_degraded": True})
    assert "[降級模式]" in (degraded.title or "")
    stale = build_market_macro_overview_embed(
        {**base, "is_degraded": True, "served_stale_cache": True}
    )
    assert "[快取/降級]" in (stale.title or "")


# ------------------------------------------------------------------ rollover_embeds


def test_short_entry_missing_plan_values_dash() -> None:
    embed = create_short_entry_embed("AAA", "r", {"sub_mode": "x"})
    text = _text(embed)
    assert "$0.00" not in text
    assert "R:R：`N/A`" in text
    assert "`--` 股" in text
    assert "0.00x" not in text


def test_short_entry_full_plan_formats() -> None:
    embed = create_short_entry_embed(
        "AAA",
        "r",
        {
            "entry_price": 10.0,
            "stop_price": 11.0,
            "target_price": 8.0,
            "reward_risk_ratio": 2.0,
            "share_qty": 100,
            "risk_budget_usd": 100.0,
            "notional_usd": 1000.0,
            "short_vix_multiplier": 0.5,
        },
    )
    text = _text(embed)
    assert "`$10.00`" in text and "R:R：`2.00`" in text and "`100` 股" in text


def test_pyramid_add_missing_plan_values_dash() -> None:
    embed = create_transition_pyramid_embed(
        "AAA", "r", "s", scenario="PYRAMID_ADD", pyramid_add_plan={"vix_spot": None}
    )
    text = _text(embed)
    assert "$0.00" not in text
    assert "`--` 股" in text
    assert "第 `--` 次加碼" in text


# --------------------------------------------------------------------- hedge_embeds


def test_proactive_event_missing_tte_is_neutral_not_red() -> None:
    [embed] = create_proactive_event_alert_embed([{"name": "CPI", "tte_hours": None}])
    value = embed.fields[0].value or ""
    assert "剩餘時間: \u001b[1;30m-- 小時" in value
    assert "\u001b[1;31m0.0" not in value


def test_proactive_event_real_tte_still_red_when_urgent() -> None:
    [embed] = create_proactive_event_alert_embed([{"name": "CPI", "tte_hours": 3.0}])
    assert "\u001b[1;31m3.0 小時" in (embed.fields[0].value or "")


def test_hedge_alert_missing_odds_dash() -> None:
    embed = create_hedge_alert_embed(
        tier_emoji="x",
        tier_name="t",
        stage_move=1,
        color_hex=0x3498DB,
        vix=20.0,
        total_beta_delta=1.0,
        adjusted_delta=1.0,
        total_vega=1.0,
        instruction_text="i",
        hedge_quantity=1,
        narration="n",
        alert_id=1,
        poly_snapshot=[
            {
                "question": "q",
                "odds_distribution": [{"outcome": "Yes", "odds": None}],
            }
        ],
    )
    text = _text(embed)
    assert "Yes: `--%`" in text
    assert "`0%`" not in text


# ------------------------------------------------------------------------ watchlist


def test_watchlist_signal_missing_holding_values_dash() -> None:
    from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed

    embed = create_watchlist_signal_embed(
        symbol="AAA",
        has_position=True,
        holding_quantity=10.0,
        holding_avg_cost=None,
        holding_pnl_pct=None,
        max_pain_data={"max_pain": 100.0, "distance_pct": None},
    )
    assert embed is not None
    text = _text(embed)
    assert "平均成本: -- ｜ 當前損益: --%" in text
    assert "$0.00" not in text
    assert "(當前價差: --%)" in text


def test_watchlist_signal_option_plan_missing_leg_values() -> None:
    from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed

    plan = SimpleNamespace(
        strategy_name="S",
        legs=[SimpleNamespace(action="BUY", opt_type="CALL", expiry="2099-01-01")],
        suggested_contracts=1,
        max_risk_amount=None,
    )
    embed = create_watchlist_signal_embed(symbol="AAA", option_plan=plan)
    assert embed is not None
    text = _text(embed)
    assert "$0.00" not in text
    assert "風險配額: --" in text
