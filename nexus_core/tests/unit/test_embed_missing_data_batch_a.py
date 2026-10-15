"""R4 缺資料降級（batch a）：缺值顯示 `--`，不得以 0 冒充；以缺值做的判斷需跳過。"""

from types import SimpleNamespace
from typing import Any

import discord

from cogs.embed_builders import (
    create_psq_embed,
    create_quote_embed,
    create_squeeze_entry_embed,
    fmt_or_dash,
)
from cogs.embed_builders._ansi_utils import _to_float_or_none
from cogs.embed_builders._embed_helpers import (
    UOA_HEURISTIC_DISCLOSURE,
    UOA_OI_PREV_DAY_NOTE,
    _add_expected_move_fields,
    _add_liquidity_fields,
    _add_sentiment_fields,
    _add_volatility_fields,
    _parse_and_format_positions_table,
)
from cogs.embed_builders.alert_embeds.downside_alerts import (
    create_downside_snapshot_embed,
)
from cogs.embed_builders.alert_embeds.market_signal_alerts import (
    create_price_volume_alert_embed,
)
from cogs.embed_builders.alert_embeds.sentiment_feeds import (
    create_media_sentiment_embed,
)
from cogs.embed_builders.portfolio_embeds import (
    create_holdings_embed,
    create_strategic_dash_embed,
    create_tactical_hedge_embed,
    create_tactical_symbol_embed,
    create_trades_embed,
)
from cogs.embed_builders.scan_embeds.radar_panel import (
    build_unified_radar_panel_embed,
)
from cogs.embed_builders.scan_embeds.risk_stress_test import create_stress_test_embed


def _text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts.append(f"{f.name}\n{f.value}")
    return "\n".join(parts)


def _fields_text(embed: Any) -> str:
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def test_fmt_or_dash() -> None:
    assert fmt_or_dash(None) == "--"
    assert fmt_or_dash(float("nan")) == "--"
    assert fmt_or_dash("abc", placeholder="N/A") == "N/A"
    assert fmt_or_dash(1234.5, ",.2f", prefix="$") == "$1,234.50"
    assert fmt_or_dash(1.5, ".2f", suffix="%") == "1.50%"
    assert _to_float_or_none(float("inf")) is None


def test_x_panel_missing_quote_fields_show_dash() -> None:
    embed = create_tactical_symbol_embed(
        {"symbol": "ZZZ", "quote": {"c": 100.0, "dp": 1.0, "d": 1.0}}
    )
    text = _fields_text(embed)
    assert "開盤: \x1b[1;36m--\x1b[0m" in text
    assert "前收: \x1b[1;30m--\x1b[0m" in text
    assert "\x1b[1;31m0.00" not in text and "\x1b[1;32m0.00" not in text


def test_x_panel_no_quote_shows_dash_price() -> None:
    embed = create_tactical_symbol_embed({"symbol": "ZZZ", "quote": {}})
    text = _fields_text(embed)
    assert "$0.00" not in text.split("15m ATR")[0]
    assert "現價: \x1b[1;37m--\x1b[0m" in text


def test_x_panel_volume_profile_missing_hvn() -> None:
    embed = create_tactical_symbol_embed(
        {
            "symbol": "ZZZ",
            "quote": {
                "c": 100.0,
                "dp": 1.0,
                "d": 1.0,
                "o": 99,
                "h": 101,
                "l": 98,
                "pc": 99,
            },
            "volume_profile": {"hvn": None, "lvn": 95.0},
        }
    )
    text = _fields_text(embed)
    assert "(HVN): \x1b[1;35m--\x1b[0m" in text
    assert "$95.00" in text


def test_holdings_missing_quote_is_dash_and_excluded() -> None:
    embed = create_holdings_embed(
        [
            {"symbol": "AAA", "quantity": 10, "avg_cost": 100.0, "current_price": None},
            {
                "symbol": "BBB",
                "quantity": 10,
                "avg_cost": 100.0,
                "current_price": 110.0,
            },
        ],
        10000.0,
    )
    text = _fields_text(embed)
    assert "--%" in text
    assert "-100.0%" not in text
    assert "$1,100.00" in text  # 只計入 BBB
    assert "1 檔缺報價" in text


def test_trades_title_suffix_when_quotes_missing() -> None:
    trade = {
        "id": 1, "symbol": "AAA", "opt_type": "put", "strike": 10, "expiry": "2099-01-01",
        "quantity": -1, "entry_price": 1.0, "current_price": None,
        "unrealized_pnl": None, "pnl_pct": None,
    }  # fmt: skip
    embed = create_trades_embed(
        {"trades": [trade], "total_unrealized_pnl": 0.0, "missing_quote_count": 1}, 1000
    )
    assert "[部分報價缺失/降級模式]" in (embed.title or "")
    ok = create_trades_embed({"trades": [trade], "missing_quote_count": 0}, 1000)
    assert "降級模式" not in (ok.title or "")


def _ctx() -> Any:
    return SimpleNamespace(
        total_theta=1.0, monthly_expense=100.0, cash_reserve=10.0,
        is_professional_mode=True, total_vanna=0.0, total_weighted_delta=0.0,
        capital=1000.0,
    )  # fmt: skip


def test_strategic_dash_degraded() -> None:
    embed = create_strategic_dash_embed(
        _ctx(), {}, vix_spot=None, nav_data={"nav": None}, spy_price=None
    )
    assert "[降級模式]" in (embed.title or "")
    text = _fields_text(embed)
    assert "總資產 (NAV): \x1b[1;37m-- (" in text
    assert "BoundedCache" not in text
    assert "$0" not in text.split("總資產")[1].split("\n")[0]
    full = create_strategic_dash_embed(
        _ctx(), {}, vix_spot=18.0, nav_data={"nav": 5000.0}, spy_price=500.0
    )
    assert "降級模式" not in (full.title or "")


def test_tactical_hedge_title_suffix_when_ivr_missing() -> None:
    assert "[資料不足/降級模式]" in (
        create_tactical_hedge_embed("AAA", None, "x").title or ""
    )
    assert "降級模式" not in (create_tactical_hedge_embed("AAA", 40.0, "x").title or "")


def test_squeeze_no_data_title_suffix() -> None:
    ev = SimpleNamespace(
        result=SimpleNamespace(status="NO_DATA"), matrix={}, resistance=None
    )
    embed = create_squeeze_entry_embed("AAA", ev, passed=False, reason="x")
    assert "[資料不足/降級模式]" in (embed.title or "")
    ev2 = SimpleNamespace(
        result=SimpleNamespace(status="NONE"), matrix={}, resistance=None
    )
    assert "降級模式" not in (
        create_squeeze_entry_embed("AAA", ev2, passed=False, reason="x").title or ""
    )


def test_radar_panel_missing_params_no_none_text() -> None:
    embed = build_unified_radar_panel_embed({"params": {}})
    text = _text(embed)
    assert "None" not in text
    assert "--%" in text


def test_stress_test_missing_keys_degrade_and_no_safe_verdict() -> None:
    embed = create_stress_test_embed({})
    text = _text(embed)
    assert "[降級模式]" in (embed.title or "")
    assert "$0.00" not in text
    assert "--" in text
    assert "充裕" not in text
    ok = create_stress_test_embed(
        {
            "is_critical": False, "total_deficit": 100.0, "cash_reserve": 500.0,
            "boxx_shares": 10.0, "boxx_cash": 1000.0, "net_deficit": 1400.0,
            "gtc_buy_orders_count": 3,
        }
    )  # fmt: skip
    assert "降級模式" not in (ok.title or "")
    assert "充裕" in _text(ok)


def _vol_data(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "vrp": 0.05, "ts_ratio": 1.0, "ts_state": "x", "v_skew": 1.0,
        "vix_vts_ratio": 0.9, "vix_regime": "R", "vix_z30": 1.0, "vix_z60": 1.0,
    }  # fmt: skip
    d.update(over)
    return d


def test_volatility_fields_missing_show_dash() -> None:
    embed = discord.Embed()
    _add_volatility_fields(
        embed,
        _vol_data(vrp=None, vix_vts_ratio=None, vix_z30=None, vix_z60=None),
        "STO_PUT",
    )
    text = _fields_text(embed)
    assert "VRP 溢酬         : \x1b[1;37m--%" in text
    assert "Z30:\x1b[1;37m--\x1b[0m Z60:\x1b[1;37m--\x1b[0m" in text
    assert "收斂" not in text and "擴張" not in text
    assert "0.00" not in text.split("VIX 期限結構")[1].split("尾部")[0]


def test_sentiment_skew_missing_dash() -> None:
    embed = discord.Embed()
    _add_sentiment_fields(embed, {"pcr": None, "skew": None, "iv_rank": None})
    text = _fields_text(embed)
    assert "平衡" not in text
    assert "\x1b[1;37m--\x1b[0m 資料不足" in text


def test_expected_move_missing_skips_safety_verdict() -> None:
    for em in (None, 0, 0.0):
        embed = discord.Embed()
        _add_expected_move_fields(
            embed,
            {
                "strike": 100,
                "bid": 1.0,
                "expected_move": em,
                "em_lower": 0.0,
                "em_upper": 0.0,
            },
            "STO_PUT",
            False,
        )
        text = _fields_text(embed)
        assert "防線已建構" not in text
        assert "無法判定" in text
        assert "--" in text
    ok = discord.Embed()
    _add_expected_move_fields(
        ok,
        {
            "strike": 90,
            "bid": 1.0,
            "expected_move": 10.0,
            "em_lower": 95.0,
            "em_upper": 105.0,
        },
        "STO_PUT",
        False,
    )
    assert "防線已建構於預期暴跌區間外" in _fields_text(ok)


def test_liquidity_missing_bid_ask_dash() -> None:
    embed = discord.Embed()
    _add_liquidity_fields(embed, {"bid": None, "ask": None})
    text = _fields_text(embed)
    assert "**Bid:** `--`" in text
    assert "0.00" not in text
    assert "`--`" in text.split("Limit")[1]


def test_positions_table_missing_price_excluded_from_total() -> None:
    item_ok = "🔹 **AAA** `2099-01-01` `$10` **PUT**\n方向: `BTO` 數量: `1` 成本: `$1.00` 現價: `$2.00`"
    item_missing = (
        "🔹 **BBB** `2099-01-01` `$10` **PUT**\n方向: `BTO` 數量: `1` 成本: `$1.00`"
    )
    out = _parse_and_format_positions_table([item_ok, item_missing])
    assert "-100.00%" not in out
    assert "**-- (--%)**" in out
    assert "現價 `--`" in out
    assert "**+$100.00**" in out  # 只計入 AAA
    assert "1 筆缺現價" in out


def test_psq_price_missing_dash() -> None:
    psq = SimpleNamespace(
        is_squeezing=False, is_breakout_long=False, is_breakout_short=False,
        momentum_value=0.1, sma_distance_pct=1.0, sma_20=10.0, is_near_support=True,
    )  # fmt: skip
    embed = create_psq_embed({"symbol": "AAA", "psq_result": psq, "price": None})
    assert "`--`" in (embed.description or "")
    assert "$0.00" not in (embed.description or "")


def test_quote_embed_closed_market() -> None:
    embed = create_quote_embed("AAA", {"c": 0, "dp": None, "h": 0, "l": 0, "pc": 0})
    assert "[封盤中/數據未更新]" in (embed.title or "")
    text = _fields_text(embed)
    assert "$0" not in text
    assert "--" in text
    live = create_quote_embed(
        "AAA", {"c": 10.0, "dp": 1.0, "h": 11.0, "l": 9.0, "pc": 9.5}
    )
    assert "封盤" not in (live.title or "")


def test_downside_snapshot_unavailable_title_suffix() -> None:
    embed = create_downside_snapshot_embed(
        None, None, unavailable_reason="記憶體水位過高"
    )
    assert "[降級模式]" in (embed.title or "")
    assert "降級模式" not in (create_downside_snapshot_embed(None, None).title or "")


def test_price_volume_alert_zero_avg_volume_dash() -> None:
    from datetime import datetime

    from database.price_volume_watch import WatchDirection

    watch = SimpleNamespace(
        symbol="AAA", direction=WatchDirection.ABOVE, volume_multiplier=1.5,
        target_price=10.0,
    )  # fmt: skip
    bar = SimpleNamespace(
        volume=1000.0, avg_volume=0.0, close=11.0, bar_time=datetime(2026, 1, 2, 10, 0),
    )  # fmt: skip
    embed = create_price_volume_alert_embed(watch, bar)
    text = _fields_text(embed)
    assert "放大 \x1b[1;33m--x" in text or "--x" in text
    assert "0.00x" not in text


def test_media_sentiment_discloses_keyword_heuristic() -> None:
    embed = create_media_sentiment_embed("AAA")
    assert "關鍵字啟發式" in _fields_text(embed)


def test_uoa_disclosure_constants_shared() -> None:
    assert "啟發式" in UOA_HEURISTIC_DISCLOSURE
    assert "前一交易日" in UOA_OI_PREV_DAY_NOTE
