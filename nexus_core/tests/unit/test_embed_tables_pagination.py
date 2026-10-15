"""Phase 4：動態欄寬表格、樹狀符號、分頁上限的行為測試。"""

from __future__ import annotations

import re
from typing import Any

import discord

from cogs.embed_builders import _render_aligned_table
from cogs.embed_builders._ansi_utils import _clean_ansi, _visual_len
from cogs.embed_builders.market_embeds import (
    build_calendar_embed,
    create_iv_risk_scan_embed,
)
from cogs.embed_builders.order_embeds.active_orders import (
    _build_active_order_ansi_card,
    create_active_orders_embed,
)
from cogs.embed_builders.order_embeds.telemetry_alignment import (
    create_telemetry_alignment_embeds,
)
from cogs.embed_builders.portfolio_embeds import (
    _ENTRY_RULES_DETAIL_LEFT,
    _ENTRY_RULES_DETAIL_SHORT,
    create_holdings_embed,
    create_trades_embed,
)
from cogs.embed_builders.rollover_embeds import create_rollover_history_embed
from cogs.embed_builders.scan_embeds.earnings_and_sector_flow import (
    create_earnings_report_embed,
    create_sector_flow_report_embed,
)
from cogs.embed_builders.scan_embeds.covered_call import create_cc_recovery_embed
from cogs.embed_builders.squeeze_entry_embeds import _matrix_lines
from cogs.embed_builders.watchlist_embeds import create_watchlist_embed

RED = "\u001b[1;31m"
RST = "\u001b[0m"


def _vlens(lines: list[str]) -> list[int]:
    return [_visual_len(_clean_ansi(ln)) for ln in lines]


# ── _render_aligned_table ────────────────────────────────────────────────────


def test_render_aligned_table_cjk_emoji_ansi() -> None:
    lines = _render_aligned_table(
        ["標的", "狀態", "值"],
        [
            ["GOOGL", "🔥 熱門", f"{RED}1.5{RST}"],
            ["A", "冷", "10.25"],
        ],
        ["left", "left", "right"],
        sep=" | ",
    )
    header, divider, *rows = lines
    assert divider == "-" * _visual_len(header)
    # 所有列的「去色後視覺寬度」一致 -> 欄位對齊
    assert len(set(_vlens([header, *rows]))) == 1
    # ANSI 色碼保留在輸出中
    assert RED in rows[0] and RST in rows[0]
    # 5 碼代號不會把後面欄位擠歪
    assert rows[0].startswith("GOOGL | ")
    assert rows[1].startswith("A     | ")


def test_render_aligned_table_default_aligns_and_short_rows() -> None:
    lines = _render_aligned_table(["a", "b"], [["x"]])
    assert len(lines) == 3
    assert lines[1] == "-" * _visual_len(lines[0])


# ── R6：holdings / trades 以 GOOGL 驗證 ───────────────────────────────────────


def _table_lines(embed: discord.Embed) -> list[str]:
    out: list[str] = []
    for f in embed.fields:
        v = f.value or ""
        if v.startswith("```ansi") and " | " in v:
            out.extend(v.split("\n")[1:-1])
    return out


def _pipe_positions(line: str, bar: str = "|") -> list[int]:
    plain = _clean_ansi(line)
    pos, w = [], 0
    for ch in plain:
        if ch == bar:
            pos.append(w)
        w += _visual_len(ch)
    return pos


def test_holdings_table_aligned_with_googl() -> None:
    embed = create_holdings_embed(
        [
            {
                "symbol": "GOOGL",
                "quantity": 10,
                "avg_cost": 100.0,
                "current_price": 110.0,
            },
            {"symbol": "A", "quantity": 5, "avg_cost": 50.0, "current_price": 45.0},
        ],
        10000.0,
    )
    lines = _table_lines(embed)
    header, divider, *rows = lines
    assert len(rows) == 2
    assert divider == "-" * _visual_len(header)
    assert all(_pipe_positions(r) == _pipe_positions(header) for r in rows)
    assert all(len(f.value or "") <= 1024 for f in embed.fields)
    assert "GOOGL" in rows[1]


def test_trades_table_aligned_with_googl_and_cjk() -> None:
    def trade(i: int, sym: str, qty: int, cur: Any) -> dict[str, Any]:
        return {
            "id": i, "symbol": sym, "opt_type": "put", "strike": 150, "expiry": "2099-01-01",
            "quantity": qty, "entry_price": 1.5, "current_price": cur,
            "unrealized_pnl": None if cur is None else 12.0,
            "pnl_pct": None if cur is None else 0.1,
        }  # fmt: skip

    embed = create_trades_embed(
        {
            "trades": [trade(1, "GOOGL", -1, 1.2), trade(2, "A", 2, None)],
            "missing_quote_count": 1,
        },
        1000.0,
    )
    header, divider, *rows = _table_lines(embed)
    assert len(rows) == 2
    assert divider == "-" * _visual_len(header)
    assert all(_pipe_positions(r) == _pipe_positions(header) for r in rows)
    assert "報價缺失" in rows[1]


# ── R6：其餘動態欄寬 ──────────────────────────────────────────────────────────


def test_rollover_history_table_pyramid_label_and_ansi() -> None:
    embed = create_rollover_history_embed(
        [
            {
                "created_at": "2026-10-14 09:30:00", "symbol": "GOOGL",
                "scenario": "PYRAMID_ADD", "action": "加碼", "sell_ratio": 0.25,
            },
            {
                "created_at": "2026-10-13 09:30:00", "symbol": "A",
                "scenario": "MARGIN_DEFENSE", "action": "減碼", "sell_ratio": 1.0,
            },
        ]
    )  # fmt: skip
    value = embed.fields[0].value or ""
    assert value.startswith("```ansi\n") and value.endswith("\n```")
    assert "金字塔加碼" in value
    assert "PYRAMID_ADD" not in value
    header, _div, *rows = value.split("\n")[1:-1]
    assert all(_pipe_positions(r) == _pipe_positions(header) for r in rows)


def test_rollover_history_many_records_keep_closing_fence() -> None:
    records = [
        {
            "created_at": "2026-10-14 09:30:00", "symbol": f"SYM{i}",
            "scenario": "SHORT_ENTRY", "action": "做空", "sell_ratio": 0.5,
        }
        for i in range(60)
    ]  # fmt: skip
    embed = create_rollover_history_embed(records)
    assert len(embed.fields) > 1
    for f in embed.fields:
        assert len(f.value or "") <= 1024
        assert (f.value or "").startswith("```ansi\n")
        assert (f.value or "").endswith("\n```")


def test_squeeze_matrix_level_column_aligned() -> None:
    from types import SimpleNamespace

    def st(level: str) -> Any:
        return SimpleNamespace(
            green_dot=False, green_dot_bars_ago=0, turbo=False,
            squeeze_level=level, momentum_color="Red",
            preview_squeeze_level=None, preview_momentum_color=None,
        )  # fmt: skip

    lines = _matrix_lines({"W": st("High"), "D": st("Release"), "65m": st("Mid")})
    body = [ln for ln in lines if "動能" in ln]
    assert len(body) == 3
    assert len({_pipe_positions(ln, "│")[0] for ln in body}) == 1
    assert len({_pipe_positions(ln, "│")[1] for ln in body}) == 1


def test_squeeze_matrix_preview_uses_full_connector() -> None:
    from types import SimpleNamespace

    s = SimpleNamespace(
        green_dot=False, green_dot_bars_ago=0, turbo=False,
        squeeze_level="High", momentum_color="Red",
        preview_squeeze_level="Mid", preview_momentum_color="Red",
    )  # fmt: skip
    text = "\n".join(_matrix_lines({"D": s}))
    assert "└─ 盤中未收盤預覽" in text


def test_earnings_report_tables_closed_and_aligned() -> None:
    upcoming = {f"SYM{i}": [{"date": "2026-10-20"}] for i in range(80)}
    upcoming["GOOGL"] = [{"date": "2026-10-21"}]
    embed = create_earnings_report_embed(
        "EARNINGS", "## 摘要\n內容", {"upcoming_earnings": upcoming}
    )
    earn = [f for f in embed.fields if (f.name or "").startswith("📅")]
    assert len(earn) > 1  # 超過 1024 時切成多塊而非截斷
    for f in earn:
        v = f.value or ""
        assert len(v) <= 1024
        assert v.startswith("```ansi\n") and v.endswith("\n```")
    all_text = "\n".join(f.value or "" for f in earn)
    assert "GOOGL" in all_text


def test_sector_flow_table_aligned() -> None:
    sectors = [
        {"symbol": "XLK", "name": "科技", "pct_change": 1.2, "rel_vol": 1.1, "skew": 2.0, "uoa_count": 3},
        {"symbol": "GOOGL", "name": "Communication", "pct_change": -2.5, "rel_vol": 0.9, "skew": -1.0, "uoa_count": 0},
    ]  # fmt: skip
    embed = create_sector_flow_report_embed(
        "CLOSE", "## 摘要\n內容", {"vix": 20.0, "spy_price": 500.0, "sectors": sectors}
    )
    f = next(x for x in embed.fields if "板塊輪動" in (x.name or ""))
    v = f.value or ""
    assert v.startswith("```ansi\n") and v.endswith("\n```")
    header, _div, *rows = v.split("\n")[1:-1]
    assert all(_pipe_positions(r) == _pipe_positions(header) for r in rows)


def test_cc_recovery_table_aligned_and_notes_kept() -> None:
    recs = [
        {"expiration": "2026-11-20", "strike": 1234.5, "delta": 0.123, "premium": 9.5,
         "annualized_yield": 12.34, "premium_source": "ASK_HALF", "has_earnings_risk": True},
        {"expiration": "2026-12-18", "strike": 99.0, "delta": 0.1, "premium": 120.25,
         "annualized_yield": 8.0},
    ]  # fmt: skip
    embed = create_cc_recovery_embed(
        {"symbol": "GOOGL", "current_price": 100.0, "recommendations": recs}
    )
    table_field = next(f for f in embed.fields if "推薦 Covered Call" in (f.name or ""))
    v = table_field.value or ""
    assert v.endswith("\n```")
    header, _div, *rows = v.split("\n")[1:-1]
    assert all(_pipe_positions(r) == _pipe_positions(header) for r in rows)
    notes = "\n".join(f.value or "" for f in embed.fields)
    assert "ask/2" in notes and "IV Crush" in notes


# ── R2：樹狀符號 ──────────────────────────────────────────────────────────────


def test_entry_rules_detail_uses_tree_glyphs() -> None:
    for lines in (_ENTRY_RULES_DETAIL_LEFT, _ENTRY_RULES_DETAIL_SHORT):
        text = "\n".join(lines)
        assert "•" not in text
        assert " ├─ " in text and " └─ " in text
        assert lines[0] == "```ansi" and lines[-1] == "```"
        # 每個「條件N」區塊的最後一項是 └─
        dividers = [ln for ln in lines if ln.strip() and set(ln.strip()) == {"-"}]
        assert len(dividers) == 1
        header = [ln for ln in lines[1 : lines.index(dividers[0])]]
        assert len(dividers[0].strip()) == max(_visual_len(h) for h in header)


def test_active_order_card_tree_glyphs_last_is_corner() -> None:
    card = _build_active_order_ansi_card(
        {
            "id": 1, "symbol": "GOOGL", "quantity": 10, "order_type": "LIMIT",
            "validity": "DAY", "limit_price": 100.0, "side": "BUY",
        }
    )  # fmt: skip
    tree = [ln for ln in card.split("\n") if "─ " in ln and ("├─" in ln or "└─" in ln)]
    assert len(tree) == 5
    assert all("├─" in ln for ln in tree[:-1])
    assert "└─" in tree[-1]


# ── R7：分頁 ──────────────────────────────────────────────────────────────────


def _telemetry_item(i: int) -> dict[str, Any]:
    return {
        "order_id": i, "symbol": "AAPL", "order_type": "LIMIT", "side": "BUY",
        "quantity": 10, "limit_price": 100.0, "live_price": 101.0,
        "suggested_price": 99.0, "suggested_quantity": 10,
        "is_size_down": False,
    }  # fmt: skip


def test_telemetry_pages_of_ten() -> None:
    for n, pages in ((10, 1), (11, 2), (21, 3)):
        embeds = create_telemetry_alignment_embeds(
            [_telemetry_item(i) for i in range(n)]
        )
        # 每 10 筆一個分頁；單頁過長時 split_embed_by_fields 可能再細切，故為下界
        assert len(embeds) >= pages
        assert all(len(e.fields) <= 10 for e in embeds)
        assert sum(len(e.fields) for e in embeds) == n
        if len(embeds) > 1:
            assert f"(第 1/{len(embeds)} 頁)" in (embeds[0].title or "")


def _orders(n: int) -> list[dict[str, Any]]:
    return [
        {"id": i + 1, "symbol": f"SYM{i}", "quantity": 1.0, "order_type": "MARKET",
         "validity": "DAY"}
        for i in range(n)
    ]  # fmt: skip


def test_active_orders_ten_per_embed_with_page_titles() -> None:
    assert len(create_active_orders_embed(_orders(10))) == 1
    single_title = create_active_orders_embed(_orders(10))[0].title or ""
    assert "頁)" not in single_title
    embeds = create_active_orders_embed(_orders(25))
    assert len(embeds) == 3
    assert [len(e.fields) for e in embeds] == [10, 10, 5]
    for i, e in enumerate(embeds, start=1):
        assert (e.title or "").endswith(f"(第 {i}/3 頁)")
    # 欄位編號跨頁連續
    assert "#11" in (embeds[1].fields[0].name or "")


def test_iv_risk_scan_caps_at_ten_with_remainder_line() -> None:
    res = [
        {"symbol": f"S{i}", "iv_rank": 90.0, "tte_hours": 5.0, "strategy": "x"}
        for i in range(13)
    ]
    embed = create_iv_risk_scan_embed(res)
    assert len(embed.fields) == 10
    assert "…另有 3 檔未列出" in (embed.description or "")
    exact = create_iv_risk_scan_embed(res[:10])
    assert len(exact.fields) == 10
    assert "未列出" not in (exact.description or "")


def test_calendar_earnings_cap_ten_with_remainder_line() -> None:
    from types import SimpleNamespace

    evs = [
        SimpleNamespace(symbol=f"S{i}", tte_hours=10.0, date="2026-10-20")
        for i in range(12)
    ]
    embed = build_calendar_embed(
        macro_events=[], earnings_events=evs, fedwatch_prob=None
    )
    earn = next(f for f in embed.fields if "財報" in (f.name or ""))
    assert (earn.value or "").count("財報發布") == 10
    assert "…另有 2 檔未列出" in (earn.value or "")
    assert len(earn.value or "") <= 1024


def test_watchlist_embed_page_in_title_not_footer() -> None:
    page = [("GOOGL", "核心"), ("A", "")]
    embed = create_watchlist_embed(page, 2, 3, 120)
    assert "(第 2/3 頁)" in (embed.title or "")
    assert "頁次" not in (embed.footer.text or "")
    assert "總項目: 120" in (embed.footer.text or "")
    single = create_watchlist_embed(page, 1, 1, 2)
    assert "頁)" not in (single.title or "")


def test_watchlist_embed_table_aligned_googl() -> None:
    page = [("GOOGL", "核心"), ("A", ""), ("台積電", "x")]
    embed = create_watchlist_embed(page, 1, 1, 3)
    block = re.search(r"```ansi\n(.*?)\n```", embed.description or "", re.S)
    assert block is not None
    header, divider, *rows = block.group(1).split("\n")
    assert len(rows) == 3
    assert divider == "-" * _visual_len(header)
    # 價格欄起點一致（無快取時為 "-- (尚未預熱)"）
    starts = {_visual_len(r[: r.index("-- (")]) for r in rows}
    assert starts == {_visual_len(header[: header.index("快取價")])}
