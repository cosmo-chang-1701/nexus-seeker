"""Phase 3b：report/market/rollover/hedge embed 的 ansi 樹狀化、取消 inline 網格、總長防線。"""

import re
from types import SimpleNamespace
from typing import Any

import discord

from cogs.embed_builders.hedge_embeds import (
    create_event_impact_embed,
    create_hedge_alert_embed,
    create_hedge_settlement_embed,
    create_memory_alert_embed,
    create_option_defense_alert_embed,
    create_polymarket_whale_alert_embed,
)
from cogs.embed_builders.market_embeds import (
    create_max_pain_embed,
    create_transition_simulation_embed,
)
from cogs.embed_builders.report_embeds import (
    build_hedge_analysis_field,
    build_scan_report,
    create_ddp_embed,
    create_rehedge_embed,
    create_transition_suggestion_embed,
)
from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
    create_short_entry_embed,
    create_transition_pyramid_embed,
)
from cogs.embed_builders.scan_embeds.macro_and_fomc import (
    create_fomc_escape_window_embed,
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


def _assert_layout(embed: discord.Embed) -> None:
    assert all(not f.inline for f in embed.fields), "不得有 inline=True 欄位"
    for f in embed.fields:
        value = f.value or ""
        if "```" in value:
            assert value.startswith("```ansi\n"), f"非 ansi 區塊: {f.name}"


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {str(f.name): _strip(f.value) for f in embed.fields}


# ----------------------------------------------------------------- 總長防線


def test_hedge_alert_full_data_within_budget() -> None:
    embed = create_hedge_alert_embed(
        vix=31.25,
        stage_move=3,
        tier_name="Aggressive",
        tier_emoji="🔥",
        color_hex=0xFFAA00,
        total_beta_delta=-1250.5,
        adjusted_delta=-1380.2,
        total_vega=-632.55,
        hedge_quantity=1380,
        instruction_text="賣出 1380 股 SPY (市價單)\n或買入 14 口 SPY 30DTE Put",
        narration="VIX 急升導致隱含 Delta 擴張，需先降曝險。" * 40,
        alert_id=77,
        poly_snapshot=[
            {
                "question": f"Will the Federal Reserve cut rates in meeting #{i}?",
                "odds_distribution": [
                    {"outcome": "Yes", "odds": 0.62},
                    {"outcome": "No", "odds": 0.38},
                ],
            }
            for i in range(5)
        ],
    )
    total = _assert_nothing_dropped(embed)
    print("hedge_alert total", total)
    _assert_layout(embed)
    # AI 敘述為 LLM 文字，其餘量化欄位皆為 ansi
    ansi_names = {
        "📊 風險指標",
        "🌐 [快取快照] Polymarket 即時機率",
        "🛡️ 對沖建議指令",
        "📈 預期效果",
    }
    for f in embed.fields:
        if f.name in ansi_names:
            assert (f.value or "").startswith("```ansi\n")
    fields = _fields(embed)
    risk = fields["📊 風險指標"]
    assert "即時 VIX     : 31.25" in risk
    assert "淨 Delta     : -1250.5" in risk
    assert "調整後 Delta : -1380.2" in risk
    assert "Vega 脆弱性  : -632.55" in risk
    assert " ├─ " in risk and " └─ " in risk
    assert "Yes: 62%" in fields["🌐 [快取快照] Polymarket 即時機率"]
    assert "賣出 1380 股 SPY" in fields["🛡️ 對沖建議指令"]


def _full_scan_result() -> dict[str, Any]:
    return {
        "symbol": "NVDA",
        "strategy": "SELL PUT",
        "strike": 170.0,
        "target_date": "2026-11-20",
        "ai_decision": "APPROVE",
        "delta": -0.2314,
        "theta": 0.1523,
        "gamma": 0.012345,
        "iv": 0.4567,
        "safe_qty": 3,
        "projected_exposure_pct": 12.34,
        "risk_limit": 15.0,
        "macro_vix": 21.5,
        "macro_oil": 78.4,
        "spy_price": 612.3,
        "ema_signals": [
            {
                "window": w,
                "type": t,
                "direction": d,
                "distance_pct": 1.23,
            }
            for w, t, d in [
                (8, "CROSSOVER", "BULLISH"),
                (21, "TEST", "SUPPORT"),
                (50, "TEST", "RESISTANCE"),
                (100, "CROSSOVER", "BEARISH"),
                (200, "TEST", "SUPPORT"),
            ]
        ],
    }


def test_scan_report_full_data_within_budget_and_ema_ansi() -> None:
    embed = build_scan_report(_full_scan_result())
    total = _assert_nothing_dropped(embed)
    print("scan_report total", total)
    _assert_layout(embed)
    ema = [f for f in embed.fields if f.name == "📈 趨勢與指標動態"]
    assert len(ema) == 1
    assert (ema[0].value or "").startswith("```ansi\n")
    plain = _strip(ema[0].value)
    assert "EMA 8 強勢突破" in plain and "EMA 200 回測支撐" in plain
    assert plain.count(" ├─ ") == 4 and plain.count(" └─ ") == 1


def test_scan_report_empty_ema_still_ansi() -> None:
    result = _full_scan_result()
    result["ema_signals"] = []
    embed = build_scan_report(result)
    assert "暫無關鍵 EMA 觸碰訊號" in _fields(embed)["📈 趨勢與指標動態"]


# --------------------------------------------------------------- report_embeds


def test_transition_suggestion_ansi() -> None:
    res = SimpleNamespace(
        adjusted_cost_basis=92.5,
        capital_efficiency_gain=7.5,
        projected_aroc=18.2,
        cc_strike=110,
    )
    embed = create_transition_suggestion_embed(
        {
            "symbol": "NVDA",
            "pnl_pct": 1.5,
            "pnl_usd": 2500.0,
            "transition_result": res,
            "stock_price": 100.0,
        }
    )
    _assert_layout(embed)
    f = _fields(embed)
    assert "獲利率: +150.0% / $2,500.00" in f["📈 目前獲利狀況"]
    assert "調整後持股成本: $92.50" in f["🧬 演進後預期效益"]
    assert "(@$110)" in f["🧬 演進後預期效益"]


def test_rehedge_ansi_and_emoji_names() -> None:
    embed = create_rehedge_embed({"suggested_spy_qty": 120, "reason": "趨勢轉弱"})
    _assert_layout(embed)
    f = _fields(embed)
    assert "趨勢轉弱" in f["🚨 觸發原因"]
    assert "賣出 (Short) 120 股 SPY" in f["🛠️ 建議動作"]
    for fld in embed.fields:
        assert (fld.value or "").startswith("```ansi\n")


def test_ddp_logic_field_ansi() -> None:
    embed = create_ddp_embed(
        {
            "symbol": "AAA",
            "current_pe": 20.0,
            "pe_mean_3y": 30.0,
            "eps_growth": 0.25,
            "rev_accel": True,
            "confidence_score": 80,
            "forward_pe": 15.5,
            "has_margin_bonus": True,
            "rvol": 1.8,
        }
    )
    _assert_layout(embed)
    logic = _fields(embed)["🧐 量化篩選邏輯"]
    assert "25.0%" in logic and "15.50" in logic and "1.80x" in logic
    assert "(+營業利益率升)" in logic and "(動能點火)" in logic


def test_hedge_analysis_field_ansi_and_placeholders() -> None:
    embed = discord.Embed()
    build_hedge_analysis_field(
        embed,
        {
            "status": "OPTIMAL",
            "effectiveness": 0.85,
            "alpha_contribution": 350.0,
            "hedge_contribution": -200.0,
            "hedge_ratio": 0.57,
            "net_pnl": 150.0,
        },
    )
    assert embed.fields[0].value.startswith("```ansi\n")  # type: ignore
    text = _strip(embed.fields[0].value)
    assert "個股 Alpha 損益 : $350.00" in text
    assert "對沖有效性 (ES) : 85.00% (🎯 精準)" in text
    sparse = discord.Embed()
    build_hedge_analysis_field(sparse, {"status": "UNKNOWN"})
    assert "--" in _strip(sparse.fields[0].value)
    assert "$0.00" not in _strip(sparse.fields[0].value)


# ---------------------------------------------------------------- market_embeds


def test_max_pain_single_ansi_field() -> None:
    embed = create_max_pain_embed(
        "AAA",
        {
            "max_pain": 100.0,
            "current_price": 104.0,
            "expiry": "2099-01-01",
            "distance_pct": 4.0,
        },
    )
    _assert_layout(embed)
    f = _fields(embed)["📍 最大痛點概覽"]
    assert "到期日        : 2099-01-01" in f
    assert "$100.00" in f and "$104.00" in f
    assert "現價高於痛點 4.00%" in f


def test_max_pain_circuit_breaker_keeps_notice() -> None:
    embed = create_max_pain_embed(
        "AAA", {"circuit_breaker_triggered": True, "expiry": "2099-01-01"}
    )
    _assert_layout(embed)
    assert "已觸發自動斷路" in _fields(embed)["📍 最大痛點概覽"]


def test_transition_simulation_ansi() -> None:
    embed = create_transition_simulation_embed(
        symbol="NVDA",
        current_price=100.0,
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        target_cc_strike=110.0,
        target_cc_premium=2.5,
        projected_aroc=12.0,
        capital_efficiency_gain=2.7,
    )
    _assert_layout(embed)
    f = _fields(embed)
    assert "$2,500.00" in f["📊 轉軌模擬概覽"]
    assert "$7,500.00" in f["🚀 資本重分配路線圖 (Roadmap)"]
    assert "低於效率門檻" in f["📊 資本效率評估"]
    assert "2.70%" in f["📊 資本效率評估"]


# -------------------------------------------------------------- rollover_embeds


def test_short_entry_and_pyramid_use_ansi_trees() -> None:
    plan = {
        "entry_price": 92.0,
        "stop_price": 97.5,
        "target_price": 80.0,
        "reward_risk_ratio": 2.18,
        "risk_budget_usd": 250.0,
        "share_qty": 45,
        "notional_usd": 4140.0,
        "vix_spot": 22.0,
        "short_vix_multiplier": 0.5,
    }
    short = create_short_entry_embed("TSLA", "r", plan, "Long Put", "REGIME_X")
    _assert_layout(short)
    for fld in short.fields:
        assert (fld.value or "").startswith("```ansi\n")
    assert "進場 (限價放空)：$92.00" in _fields(short)["📐 價位"]

    pyramid = create_transition_pyramid_embed(
        "AAA", "r", "s", scenario="PYRAMID_ADD", pyramid_add_plan=plan
    )
    _assert_layout(pyramid)
    assert (pyramid.fields[-1].value or "").startswith("```ansi\n")
    assert pyramid.fields[-1].name == "⚖️ 倉位"


def test_dynamic_rollover_fields_not_inline() -> None:
    embed = create_dynamic_rollover_embed(
        rollover_type="再平衡",
        sell_symbol="AAA",
        sell_ratio=1.0,
        buy_symbol="VOO",
        reason="r",
        suggested_strategy="Buy Shares",
        suggested_price="$100",
        strike="N/A",
        expiry="N/A",
    )
    assert all(not f.inline for f in embed.fields)


# ----------------------------------------------------------------- hedge_embeds


def test_event_impact_and_settlement_layout() -> None:
    embed = create_event_impact_embed("SPY", 5.0, 100.0, 12.5, 140.0, 40.0, 5000.0)
    _assert_layout(embed)
    f = _fields(embed)
    assert "預期 Hidden Delta       : 140.00" in f["📊 Greeks 偏移模擬"]
    assert "$5,000.00" in f["📊 Greeks 偏移模擬"]
    assert "危險" in f["🚦 風險狀態判定"]
    _assert_layout(create_hedge_settlement_embed(1, "SPY", 5))


def test_memory_alert_optional_rows() -> None:
    embed = create_memory_alert_embed(91.2, 0.0, 1, 2)
    _assert_layout(embed)
    text = _fields(embed)["🖥️ 記憶體概況"]
    assert "Swap" not in text and "RSS" not in text
    full = create_memory_alert_embed(91.2, 512.4, 1, 2, swap_usage=40.0)
    assert "Swap 占用 : 40.0%" in _fields(full)["🖥️ 記憶體概況"]


def test_option_defense_layout() -> None:
    embed = create_option_defense_alert_embed(
        is_live=True,
        symbol="TSLA",
        status_icon="✅",
        action_taken="已平倉",
        pnl=-12.5,
        exposure_pct=9.5,
        exit_reason="Delta 接近 1.0\nDTE < 3",
        regime="Balanced",
        target_delta=120.0,
        hedge={"action": "賣出 10 股 SPY", "gap": 10},
    )
    _assert_layout(embed)
    f = _fields(embed)
    assert "$-12.50" in f["📋 處置結果"] and "9.50%" in f["📋 處置結果"]
    assert "DTE < 3" in f["🔎 觸發原因 / 指標"]
    assert "120.0 Delta" in f["🧠 系統自主位階判定"]
    assert "賣出 10 股 SPY (缺口: 10)" in f["🛡️ 自動對沖決策"]


def test_whale_alert_description_split_into_emoji_fields() -> None:
    embed = create_polymarket_whale_alert_embed(
        intent_emoji="🟢",
        intent_label="強力看多",
        market_question="Will NVDA beat earnings?",
        usd_value=65000.0,
        dynamic_threshold=10000.0,
        win_rate=78.0,
        is_high_conviction=True,
        is_bullish=True,
        summary="市場預期財報後仍有延續動能。",
        event_slug="nvda-earnings",
        uoa_correlation={
            "uoa": {
                "symbol": "NVDA",
                "expiry": "2026-06-19",
                "strike": 150,
                "type": "C",
            },
            "classification": {
                "classification": "方向性押注",
                "confidence": 0.88,
                "explanation": "同步觀察到買權放量。",
            },
        },
    )
    _assert_layout(embed)
    # AI 總結改放 description（避免 field 1024 上限截斷），其餘維持 3 個 emoji 欄位
    assert len(embed.fields) == 3
    assert all(
        str(f.name)[0] != " " and not str(f.name).isascii() for f in embed.fields
    )
    assert "nvda-earnings" in (embed.description or "")
    assert "市場預期財報後仍有延續動能。" in (embed.description or "")
    assert "強力看多" in (embed.description or "")
    f = _fields(embed)
    assert "$65,000.00" in f["📊 交易概況"] and "6.50x" in f["📊 交易概況"]
    assert "方向性押注" in f["🔍 UOA 關聯偵測 (NVDA)"]
    _assert_nothing_dropped(embed)


# -------------------------------------------------------------------- scan_embeds


def test_fomc_escape_window_fields_not_inline() -> None:
    embed = create_fomc_escape_window_embed(
        prob=0.4,
        direction="延後",
        shift_days=3,
        adjusted_start="2026-11-01",
        adjusted_end="2026-11-10",
        reason="r",
        tactical_directive="減碼",
    )
    assert all(not f.inline for f in embed.fields)
