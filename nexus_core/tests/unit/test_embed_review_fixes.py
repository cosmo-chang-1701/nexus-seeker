"""PR #80 code-review 修正回歸測試。"""

from __future__ import annotations

import discord

from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _display_width,
    _render_aligned_table,
    _visual_len,
)
from cogs.embed_builders._embed_helpers import (
    _add_ansi_table_fields,
    _parse_and_format_positions_table,
    no_title_suffix,
    split_embed_by_fields,
)


# 1. macro healthy status / degraded
def test_macro_embed_us2y_zero_or_none_is_degraded() -> None:
    from cogs.embed_builders.scan_embeds.macro_and_fomc import create_macro_scan_embed

    base = {
        "vix": 16.0,
        "dxy": 101.0,
        "tnx": 4.2,
        "vix_change": 0.1,
        "tnx_change_bps": 1.0,
    }
    for us2y in (0.0, None):
        embed = create_macro_scan_embed({**base, "us2y": us2y})
        assert "[降級模式]" in (embed.title or "")
        text = "\n".join(str(f.value) for f in embed.fields)
        assert "曲線平穩" not in text


def test_build_macro_healthy_status_never_crashes_on_missing() -> None:
    from market_analysis.analyst_runners.macro_runner import (
        build_macro_healthy_status,
    )

    out = build_macro_healthy_status(
        {
            "vix": 16.0,
            "vix_change": None,
            "dxy": 101.0,
            "tnx": 4.2,
            "tnx_change_bps": None,
            "us2y": None,
        }
    )
    assert "曲線平穩" not in out and "缺失" in out
    assert "變動" not in out
    out0 = build_macro_healthy_status(
        {
            "vix": 16.0,
            "vix_change": 0.2,
            "dxy": 101.0,
            "tnx": 4.2,
            "tnx_change_bps": 2.0,
            "us2y": 0.0,
        }
    )
    assert "曲線平穩" not in out0
    full = build_macro_healthy_status(
        {
            "vix": 16.0,
            "vix_change": 0.2,
            "dxy": 101.0,
            "tnx": 4.2,
            "tnx_change_bps": 2.0,
            "us2y": 3.9,
        }
    )
    assert "利差 +0.30%" in full and "全數合規" in full


# 2. ASK_HALF positions
def test_positions_table_handles_ask_half_price_note() -> None:
    from market_analysis.report_formatter import format_position_report

    row = format_position_report(
        symbol="AAPL",
        expiry="2026-12-18",
        strike=200.0,
        opt_type="call",
        cc_tag="",
        entry_price=1.0,
        current_price=2.0,
        pnl_pct=1.0,
        dte=30,
        spx_weighted_delta=0.1,
        status="HOLD",
        quantity=1.0,
        price_note=" (ask/2 估)",
    )
    out = _parse_and_format_positions_table([row])
    assert "缺現價" not in out
    assert "+$100.00" in out
    assert "$2.00*" in out and "ask/2" in out


# 3. whale alert summary
def test_whale_alert_ai_summary_not_truncated() -> None:
    from cogs.embed_builders.hedge_embeds import create_polymarket_whale_alert_embed

    summary = "分析" * 1250
    embed = create_polymarket_whale_alert_embed(
        intent_emoji="🐳",
        intent_label="看多",
        market_question="Q?",
        usd_value=1e6,
        dynamic_threshold=5e5,
        win_rate=80.0,
        is_high_conviction=True,
        is_bullish=True,
        summary=summary,
        event_slug="slug",
        uoa_correlation=None,
    )
    assert summary in (embed.description or "")
    assert "前往市場" in (embed.description or "")
    assert any("預測性對沖" in (f.name or "") for f in embed.fields)
    assert len(embed.description or "") <= 4096


# 4. IV risk scan ordering
def test_iv_risk_scan_sorts_high_risk_first_and_lists_omitted() -> None:
    from cogs.embed_builders.market_embeds import create_iv_risk_scan_embed

    results = [
        {"symbol": f"S{i}", "iv_rank": 80 + i, "tte_hours": 10, "strategy": "x"}
        for i in range(12)
    ]
    results.append({"symbol": "RISKY", "iv_rank": 81, "is_high_risk_vol": True})
    results.append({"symbol": "NOIVR"})
    embed = create_iv_risk_scan_embed(results)
    names = [str(f.name) for f in embed.fields]
    assert len(names) == 10
    assert names[0].startswith("🚨 RISKY")
    assert "S11" in names[1]
    desc = embed.description or ""
    assert "另有 4 檔未列出" in desc and "NOIVR" in desc


# 5. display width
def test_display_width_aligns_rows_with_symbols() -> None:
    cells = ["A→B", "X—Y", "Δ1", "●", "±2", "⚠️", "台積電", "GOOGL"]
    rows = [[c, "v"] for c in cells]
    lines = _render_aligned_table(["名稱", "值"], rows, sep=" | ")
    first = lines[2]
    sep_cols = {_display_width(ln.split(" | ")[0]) for ln in lines[2:]}
    assert len(sep_cols) == 1
    assert {_display_width(ln) for ln in lines[2:]} == {_display_width(first)}
    assert _display_width(lines[1]) == _display_width(lines[0])


def test_display_width_values_and_visual_len_unchanged() -> None:
    assert _display_width("→—Δ●±") == 5
    assert _display_width("⚠️") == 1
    assert _display_width("台積電") == 6
    assert _display_width("\x1b[1;31mAB\x1b[0m") == 2
    assert _display_width("😀") == 2
    assert _visual_len("→") == 2  # 舊行為不變（/x 面板預算依賴）
    assert _clean_ansi("\x1b[0mA") == "A"


# 6. after_market logging uses names
def test_after_market_field_lookup_by_name() -> None:
    from cogs.embed_builders.report_embeds import create_portfolio_report_embed

    embed = create_portfolio_report_embed(
        [
            "🔹 **AAPL** ｜ `2026-12-18` ｜ `$200` **CALL**\n├─ 💰 成本: `$1.00` ｜ 📈 現價: `$2.00`\n└─ 🎯 動作: HOLD\n"
        ],
        {},
        None,
        False,
    )
    names = [str(f.name) for f in embed.fields]
    assert any(n.startswith("📊 持倉明細") for n in names)
    pos_val = next(
        str(f.value) for f in embed.fields if str(f.name).startswith("📊 持倉明細")
    )
    assert "AAPL" in pos_val


# 7. squeeze no-data row alignment
def test_squeeze_matrix_no_data_rows_align() -> None:
    from cogs.embed_builders.squeeze_entry_embeds import _matrix_lines
    from tests.unit.test_squeeze_entry import _matrix

    m = _matrix(D={"is_squeezing": True, "squeeze_level": "Mid"})
    del m["5m"]
    del m["15m"]
    lines = [ln for ln in _matrix_lines(m) if "│" in ln and "└─" not in ln]
    nodata = [ln for ln in lines if "資料不足" in ln]
    assert nodata
    cols = {_display_width(_clean_ansi(ln.split("│")[0])) for ln in lines}
    assert len(cols) == 1


# 8. split title suffix
def test_split_embed_by_fields_title_suffix_option() -> None:
    def _big() -> discord.Embed:
        e = discord.Embed(title="T")
        for i in range(8):
            e.add_field(name=f"f{i}", value="x" * 900, inline=False)
        return e

    default = split_embed_by_fields(_big())
    assert len(default) > 1 and default[0].title == f"T (1/{len(default)})"
    plain = split_embed_by_fields(_big(), title_suffix=no_title_suffix)
    assert [e.title for e in plain] == ["T"] * len(plain)


def test_active_orders_titles_use_page_label_only() -> None:
    from cogs.embed_builders.order_embeds.active_orders import (
        create_active_orders_embed,
    )

    orders = [
        {
            "id": i,
            "symbol": "AAPL",
            "quantity": 1,
            "entry_price": 1.0,
            "option_type": "call",
            "strike": 100.0,
            "expiry": "2026-12-18",
            "order_type": "LIMIT",
            "validity": "GTC",
            "side": "BUY",
            "limit_price": 1.0,
        }
        for i in range(25)
    ]
    embeds = create_active_orders_embed(orders)
    assert len(embeds) > 1
    for i, e in enumerate(embeds, start=1):
        assert (e.title or "").endswith(f"(第 {i}/{len(embeds)} 頁)")
        assert "(1/" not in (e.title or "") and f" ({i}/{len(embeds)}) " not in (
            e.title or ""
        )


# 10. shared table-field helper
def test_add_ansi_table_fields_names_chunks() -> None:
    embed = discord.Embed()
    lines = [f"row{i} " + "x" * 80 for i in range(40)]
    _add_ansi_table_fields(embed, "表", "hdr", "---", lines)
    assert len(embed.fields) > 1
    assert embed.fields[0].name == f"表 (1/{len(embed.fields)})"
    one = discord.Embed()
    _add_ansi_table_fields(one, "表", "hdr", "---", ["a"])
    assert one.fields[0].name == "表"


def test_execution_decision_uses_tree_glyphs() -> None:
    from cogs.embed_builders.alert_embeds.execution_decision import (
        create_execution_decision_embed,
    )
    from models.execution import ExecutionDecision

    d = ExecutionDecision(
        decision_type="STANDBY",
        trigger_reason="t",
        grid_params=None,
        position_sizing=None,
        exit_strategy=None,
    )
    embed = create_execution_decision_embed(d)
    v = str(embed.fields[1].value)
    assert "├─ 決策類型" in v and "└─ 路由模組" in v
