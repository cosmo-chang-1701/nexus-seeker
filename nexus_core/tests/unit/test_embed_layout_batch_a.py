"""Phase 3a：量化欄位 ansi 樹狀化、取消 inline 網格、總長防線。"""

import re
from types import SimpleNamespace
from typing import Any

from cogs.embed_builders.alert_embeds.downside_alerts import (
    create_downside_snapshot_fields,
)
from cogs.embed_builders.alert_embeds.polymarket import (
    create_polymarket_prob_shift_embed,
)
from cogs.embed_builders import (
    create_scan_embed,
    create_strategic_dash_embed,
    create_tactical_hedge_embed,
    create_holdings_embed,
    create_trades_embed,
)
from cogs.embed_builders.scan_embeds.radar_panel import (
    build_unified_radar_panel_embed,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MAX_TOTAL = 5800


def _strip(text: str | None) -> str:
    return _ANSI_RE.sub("", text or "")


def _embed_len(d: dict[str, Any]) -> int:
    total = len(d.get("title") or "") + len(d.get("description") or "")
    total += len((d.get("footer") or {}).get("text") or "")
    for f in d.get("fields", []):
        total += len(f.get("name") or "") + len(f.get("value") or "")
    return total


def _assert_nothing_dropped(embed: Any) -> int:
    d = embed.to_dict()
    assert len(d["fields"]) == len(embed.fields), "NexusEmbed 靜默丟棄了尾端欄位"
    total = _embed_len(d)
    assert total <= _MAX_TOTAL, total
    return total


def _full_scan_data() -> dict[str, Any]:
    gap = SimpleNamespace(
        current_fill_status="GAP_HOLDING",
        is_support_confirmed=True,
        gap_size=3.25,
        gap_pct=1.9,
        gap_zone=(170.0, 173.25),
    )
    from market_analysis.gap_analysis import GapStatus

    gap.current_fill_status = GapStatus.GAP_HOLDING
    return {
        "symbol": "NVDA",
        "strategy": "STO_CALL",
        "stock_cost": 120.0,
        "strike": 190.0,
        "target_date": "2026-11-20",
        "price": 175.5,
        "beta": 1.7,
        "rsi": 61.2,
        "sma20": 168.4,
        "hv_rank": 55.0,
        "vrp": 0.04,
        "ts_ratio": 1.02,
        "ts_state": "⚠️ 短期偏貴",
        "v_skew": 1.08,
        "v_skew_state": "🌪️ 下行恐懼",
        "vix_vts_ratio": 0.92,
        "vix_regime": "🟢 正價差",
        "vix_z30": 0.8,
        "vix_z60": 0.3,
        "is_high_tail_risk": True,
        "pcr": 0.85,
        "skew": 6.1,
        "iv_rank": 48.0,
        "delta": -0.3,
        "weighted_delta": 40.0,
        "aroc": 32.5,
        "iv": 0.52,
        "alloc_pct": 0.1,
        "suggested_contracts": 3,
        "earnings_days": 5,
        "mmm_pct": 7.5,
        "safe_lower": 160.0,
        "safe_upper": 190.0,
        "expected_move": 12.0,
        "em_lower": 163.5,
        "em_upper": 187.5,
        "bid": 2.4,
        "ask": 2.6,
        "mid_price": 2.5,
        "spread_ratio": 8.0,
        "liq_status": "🟡 普通",
        "liq_msg": "價差偏寬",
        "trend": "BULLISH_STRONG",
        "ema_21": 169.0,
        "distance_from_21": 3.8,
        "ema_signals": [
            {"window": 21, "type": "TEST", "direction": "SUPPORT", "distance_pct": 1.2}
        ],
        "projected_exposure_pct": 22.5,
        "risk_limit": 15.0,
        "safe_qty": 0,
        "hedge_spy": 12.0,
        "spy_price": 640.0,
        "nro_warnings": ["VIX 資料不足，採保守策略"] * 3,
        "vix_spot": 21.5,
        "vix_tier_name": "摩拳擦掌",
        "vix_tier_emoji": "🟠",
        "vix_sto_delta_cap": 0.25,
        "vix_sizing_multiplier": 0.5,
        "gap_status": gap,
        "news_text": "新聞" * 400,
        "reddit_text": "討論" * 400,
        "ai_decision": "APPROVE",
        "ai_reasoning": "風險報酬比合理" * 20,
        "alert_reason": "IVR 突破 50 且 VRP 為正",
    }


def test_scan_embed_full_data_total_length_guard() -> None:
    embed = create_scan_embed(_full_scan_data())
    total = _assert_nothing_dropped(embed)
    print(f"scan embed total length = {total}")
    names = [f.name for f in embed.fields]
    for expected in ("📊 市場概覽", "🌪️ 波動率結構", "🧭 情緒指標", "🧩 部位與凱利"):
        assert expected in names
    # 不得再有 inline 網格或 ⠀ 撐寬欄名
    assert all(not f.inline for f in embed.fields)
    assert all("⠀" not in (f.name or "") for f in embed.fields)


def test_scan_embed_quant_fields_keep_every_value_in_ansi_tree() -> None:
    embed = create_scan_embed(_full_scan_data())
    by_name = {f.name: f.value for f in embed.fields}
    for key in ("📊 市場概覽", "🌪️ 波動率結構", "🧭 情緒指標", "🧩 部位與凱利"):
        assert by_name[key].startswith("```ansi\n")
        assert "├─" in by_name[key] and "└─" in by_name[key]
    overview = _strip(by_name["📊 市場概覽"])
    assert "$175.50 / 1.70" in overview and "61.20 / $168.40" in overview
    assert "55.0%" in overview
    vol = _strip(by_name["🌪️ 波動率結構"])
    for token in ("+4.00%", "1.02", "1.08", "0.92", "Z30:0.8", "Z60:0.3", "1/4 Kelly"):
        assert token in vol
    sent = _strip(by_name["🧭 情緒指標"])
    for token in ("0.85", "6.10", "48.0%"):
        assert token in sent
    perf = _strip(by_name["🧩 部位與凱利"])
    for token in ("-0.300", "-40.0", "32.5%", "52.0%", "3 口"):
        assert token in perf
    gap = _strip(by_name["📈 Gap & Fill 跳空監控"])
    assert "+1.90%" in gap and "$170.00 - $173.25" in gap
    risk = [f for f in embed.fields if "What-if" in (f.name or "")][0].value
    assert "```diff" not in risk and "```yaml" not in risk
    assert "\x1b[1;31m" in risk  # 過載 → 紅色


def test_risk_optimization_ansi_replaces_diff_and_yaml() -> None:
    from cogs.embed_builders._embed_helpers import _add_risk_optimization_fields
    from cogs.embed_builders._core import NexusEmbed

    ok = NexusEmbed(title="t")
    _add_risk_optimization_fields(ok, {"projected_exposure_pct": 5.0})
    val = ok.fields[0].value or ""
    assert val.startswith("```ansi") and "\x1b[1;32m" in val
    assert "符合資產組合平衡標準" in val and "yaml" not in val

    bad = NexusEmbed(title="t")
    _add_risk_optimization_fields(
        bad,
        {"projected_exposure_pct": 30.0, "suggested_contracts": 3, "safe_qty": 1},
    )
    joined = "\n".join(f.value or "" for f in bad.fields)
    assert "diff" not in joined
    assert "原始建議: 3 口" in joined and "安全成交: 1 口" in joined


def test_strategic_dash_full_data_total_length_guard() -> None:
    ctx = SimpleNamespace(
        total_theta=85.5,
        monthly_expense=4000.0,
        cash_reserve=25000.0,
        is_professional_mode=True,
        total_vanna=-120.0,
        total_weighted_delta=420.0,
        capital=150000.0,
    )
    runway = SimpleNamespace(
        stress_years=6.2,
        capped=False,
        gfc_years=6.2,
        dotcom_years=8.1,
        zero_years=14.3,
        boxx_payments=3,
        next_withdrawal=4000.0,
        next_date="2026-11-02",
        beta_is_fallback=True,
        beta=1.2,
        cpi_missing=True,
        nav_date="2026-10-14",
    )
    nav = {
        "nav": 182345.0,
        "missing_spot_symbols": ["AAA", "BBB", "CCC"],
        "missing_option_quotes": 4,
    }
    embed = create_strategic_dash_embed(
        ctx, {}, vix_spot=21.0, runway=runway, runway_stale=True,
        nav_data=nav, spy_price=640.0,
    )  # fmt: skip
    total = _assert_nothing_dropped(embed)
    print(f"strategic dash total length = {total}")
    assert all(not f.inline for f in embed.fields)
    assert all((f.value or "").startswith("```ansi") for f in embed.fields)
    text = _strip("\n".join(f.value or "" for f in embed.fields))
    for token in (
        "$182,345", "$25,000.00", "壓力跑道 6.2 年", "零報酬對照 14.3 年", "$85.50",
        "+420.0", "賣出 420 股 SPY", "對沖狀態", "RAM",
    ):  # fmt: skip
        assert token in text


def test_hedge_embed_ivr_ansi_tree() -> None:
    embed = create_tactical_hedge_embed("AAPL", 62.5, "Bear Put Spread")
    val = embed.fields[0].value or ""
    assert val.startswith("```ansi") and "├─" in val and "└─" in val
    text = _strip(val)
    assert "IV Rank: 62.5%" in text and "推薦防禦策略: Bear Put Spread" in text


def test_holdings_and_trades_summary_ansi() -> None:
    h = create_holdings_embed(
        [{"symbol": "AAA", "quantity": 10, "avg_cost": 100.0, "current_price": 110.0}],
        10000.0,
    )
    summ = next(f for f in h.fields if f.name == "🏁 財務摘要")
    assert (summ.value or "").startswith("```ansi") and not summ.inline
    t = _strip(summ.value)
    assert "$1,100.00" in t and "$100.00" in t and "11.0%" in t

    trade = {
        "id": 1, "symbol": "AAA", "opt_type": "put", "strike": 10,
        "expiry": "2099-01-01", "quantity": -1, "entry_price": 1.0,
        "current_price": 0.5, "unrealized_pnl": 50.0, "pnl_pct": 0.5,
        "quote_source": "ASK_HALF",
    }  # fmt: skip
    e = create_trades_embed(
        {"trades": [trade], "total_unrealized_pnl": 50.0, "missing_quote_count": 0},
        1000,
    )
    summ2 = next(f for f in e.fields if "財務摘要" in (f.name or ""))
    assert (summ2.value or "").startswith("```ansi")
    t2 = _strip(summ2.value)
    assert "$100.00" in t2 and "10.0%" in t2 and "$50.00" in t2 and "ask/2" in t2


def test_polymarket_prob_shift_single_ansi_field() -> None:
    embed = create_polymarket_prob_shift_embed("Fed cut", 0.30, 0.55)
    prob = next(f for f in embed.fields if f.name == "📊 機率變化")
    assert not prob.inline and (prob.value or "").startswith("```ansi")
    t = _strip(prob.value)
    assert "30.0% ➔ 55.0%" in t and "+25.0%" in t
    assert not any(f.name == "📐 Delta" for f in embed.fields)


def test_downside_fields_ansi_and_na_preserved() -> None:
    snap: Any = SimpleNamespace(
        n_obs=250, sortino_63=1.234, sortino_252=None, max_drawdown=-0.12,
        current_drawdown=-0.05, var_95=-0.02, cvar_95=-0.03,
    )  # fmt: skip
    fields = create_downside_snapshot_fields(snap, snap)
    assert len(fields) == 2
    for _name, value, inline in fields:
        assert not inline and value.startswith("```ansi") and "├─" in value
        t = _strip(value)
        assert "1.23" in t and "N/A" in t and "-12.00%" in t


def test_radar_panel_filters_and_params_not_inline() -> None:
    embed = build_unified_radar_panel_embed(
        {"quant_filters": ["tdp_mode"], "params": {"max_pain_threshold": 5}}
    )
    assert all(not f.inline for f in embed.fields)
