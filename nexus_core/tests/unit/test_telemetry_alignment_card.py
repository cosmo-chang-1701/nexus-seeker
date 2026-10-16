"""遙測對齊快照卡片、/telemetry_alert 與盤中推播的行為測試（不連網）。"""

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.embed_builders._ansi_utils import _clean_ansi, _visual_len
from cogs.embed_builders._core import CURATED_PALETTE
from cogs.embed_builders.order_embeds.telemetry_alignment import (
    _build_telemetry_alignment_ansi_card,
    create_telemetry_alignment_embeds,
)

RED = "\u001b[1;31m"
GREEN = "\u001b[1;32m"
YELLOW = "\u001b[1;33m"


def _item(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "order_id": 7,
        "order_type": "LIMIT",
        "side": "BUY",
        "price_label": "掛單限價",
        "current_price": 100.0,
        "original_qty": 20,
        "suggested_price": 100.0,
        "suggested_qty": 20,
        "suggestion_changed": False,
        "is_size_down": False,
        "action": "NO_ALIGNMENT_NEEDED",
        "alignment_available": True,
        "live_price": 101.0,
        "prev_close": 100.5,
        "avg_cost": 95.0,
        "gain_loss_pct": 6.32,
        "holding_shares": 50,
        "holding_status": "持倉中",
        "holding_type_label": "1.00x 純現貨 (0 槓桿)",
        "put_wall": 95.0,
        "put_wall_is_stale": False,
        "max_pain_price": 101.0,
        "wall_dist_pct": 5.94,
        "wall_status": "上方緩衝",
        "skew_val": 1.2,
        "skew_pct": 50.0,
        "skew_status": "平穩",
        "iv_val": 35.0,
        "iv_rank": 40.0,
        "iv_status": "Normal",
        "iv_source": "LIVE_IV",
        "iv_event_loading_applied": False,
        "is_premarket": False,
        "proximity_pct": 0.99,
        "radar_status": "雷達鎖定中",
        "system_status_flag": "TELEMETRY ACTIVE",
        "system_instruction_directive": "通過實時防線，維持紀律掛單。",
        "alert_text": None,
    }
    base.update(kw)
    return base


def _card(**kw: Any) -> str:
    return _build_telemetry_alignment_ansi_card(_item(**kw))[0]


def _line(card: str, needle: str) -> str:
    return next(ln for ln in card.split("\n") if needle in ln)


def test_changed_suggestion_shows_price_qty_and_delta() -> None:
    card = _card(
        suggested_price=99.0,
        suggested_qty=20,
        suggestion_changed=True,
    )
    line = _clean_ansi(_line(card, "對齊建議"))
    assert "$99.00" in line and "20股" in line and "Δ-1.00%" in line


def test_unchanged_suggestion_and_missing_live_price() -> None:
    assert "維持原價原量" in _card()
    card = _card(
        live_price=None,
        alignment_available=False,
        proximity_pct=None,
        radar_status="現價缺失",
    )
    assert "暫停（現價缺失）" in card
    assert "當前現價: \u001b[1;37m--" in card


def test_radar_status_colours() -> None:
    locked = _line(_card(radar_status="FORTRESS RE-LOCKED"), "ID ")
    assert f"{RED}FORTRESS RE-LOCKED" in locked
    ok = _line(_card(radar_status="雷達鎖定中"), "ID ")
    assert f"{GREEN}雷達鎖定中" in ok
    off = _line(_card(radar_status="偏離擴大"), "ID ")
    assert f"{RED}偏離擴大" in off


def test_intraday_stored_iv_discloses_source_and_status_colours() -> None:
    card, suffix = _build_telemetry_alignment_ansi_card(
        _item(iv_source="STORED_IV", is_premarket=False)
    )
    assert "(前日收盤)" in card
    assert suffix == " [前日收盤]"
    hv, hv_suffix = _build_telemetry_alignment_ansi_card(
        _item(iv_source="HV_PROXY", is_premarket=False)
    )
    assert "(歷史波動率代理)" in hv and hv_suffix == " [HV代理]"

    unavailable = _card(iv_status="UNAVAILABLE", iv_source="UNAVAILABLE", iv_val=None)
    assert f"{RED}UNAVAILABLE" in unavailable
    accumulating = _card(iv_status="樣本累積 3/60", iv_rank=None)
    assert f"{YELLOW}樣本累積 3/60" in accumulating
    assert f"{GREEN}Normal" in _card()


def test_premarket_suffixes_follow_section_2_3() -> None:
    _, s1 = _build_telemetry_alignment_ansi_card(
        _item(is_premarket=True, iv_source="STORED_IV")
    )
    _, s2 = _build_telemetry_alignment_ansi_card(
        _item(is_premarket=True, iv_source="HV_PROXY")
    )
    _, s3 = _build_telemetry_alignment_ansi_card(
        _item(is_premarket=True, iv_source="UNAVAILABLE", iv_val=None)
    )
    assert s1 == " [盤前/前日收盤]"
    assert s2 == " [盤前/HV代理]"
    assert s3 == " [盤前數據未更新]"


def test_event_loading_tag() -> None:
    assert "(含事件加載)" in _card(iv_event_loading_applied=True)


def test_stale_wall_max_pain_and_missing_pnl() -> None:
    card = _card(put_wall_is_stale=True, gain_loss_pct=None)
    wall = _clean_ansi(_line(card, "做市商牆"))
    assert "GEX PutWall $95.00 [快取]" in wall
    assert "最大痛點 $101.00" in wall
    assert "[快取]" not in _card()
    assert "(損益: \u001b[1;30m--%" in card


def test_missing_values_render_dash_never_na() -> None:
    card = _card(avg_cost=None, put_wall=None, wall_dist_pct=None, max_pain_price=None)
    assert "N/A" not in card
    assert "平均成本 \u001b[1;33m--" in card
    assert "GEX PutWall \u001b[1;33m--" in card
    assert "最大痛點 \u001b[1;33m--" in card


def test_order_type_labels_and_short_holding() -> None:
    assert "停損限價" in _card(order_type="STOP_LIMIT")
    stop = _clean_ansi(_card(order_type="STOP"))
    assert "買入停損 " in stop and "停損限價" not in stop
    assert "買入限價" in _clean_ansi(_card(order_type="LIMIT"))
    short = _card(holding_status="空頭持倉", holding_shares=-100)
    assert f"{RED}空頭持倉" in short and "-100" in short


def test_size_down_quantity_is_highlighted() -> None:
    down = _card(suggested_price=99.0, suggested_qty=15, suggestion_changed=True,
                 is_size_down=True)  # fmt: skip
    plain = _card(suggested_price=99.0, suggested_qty=20, suggestion_changed=True)
    assert "\u001b[1;35m15\u001b[0m" in down
    assert "\u001b[1;35m" not in plain


def test_worst_case_card_fits_budget_and_keeps_fence() -> None:
    item = _item(
        symbol="GOOGL",
        order_id=1234567,
        order_type="STOP_LIMIT",
        current_price=1234.56,
        original_qty=99999,
        suggested_price=1200.12,
        suggested_qty=88888,
        suggestion_changed=True,
        is_size_down=True,
        live_price=1250.0,
        avg_cost=1111.11,
        gain_loss_pct=-123.45,
        holding_shares=-123456,
        holding_status="空頭持倉",
        holding_type_label="保證金槓桿 (推定)",
        put_wall=1100.0,
        put_wall_is_stale=True,
        max_pain_price=1300.0,
        wall_dist_pct=-12.34,
        wall_status="跌破支撐",
        skew_val=-12.34,
        skew_pct=99.99,
        skew_status="警告",
        iv_val=123.45,
        iv_rank=99.99,
        iv_status="樣本累積 59/60",
        iv_source="HV_PROXY",
        iv_event_loading_applied=True,
        proximity_pct=12.34,
        radar_status="FORTRESS RE-LOCKED",
        system_status_flag="FORTRESS RE-LOCKED",
        system_instruction_directive="；".join(
            f"第{i}項原因：價格逼近硬支撐且波動率異常放大，建議縮減部位"
            for i in range(8)
        ),
    )
    card, _ = _build_telemetry_alignment_ansi_card(item)
    assert len(card) <= 1020
    assert card.endswith("```")
    assert "…" in card
    [embed] = create_telemetry_alignment_embeds([item])
    assert len(embed.fields[0].value or "") <= 1024


def test_description_counts_changed_only() -> None:
    items = [
        _item(order_id=1, suggestion_changed=True, suggested_price=99.0),
        _item(order_id=2),
        _item(order_id=3),
    ]
    [embed] = create_telemetry_alignment_embeds(items)
    assert "共 `3` 筆委託單，其中 `1` 筆有調整建議。" in (embed.description or "")
    assert "需要進行遙測對齊" not in (embed.description or "")


def test_title_aggregates_deduplicated_tags() -> None:
    items = [
        _item(order_id=1, is_premarket=True, iv_source="STORED_IV"),
        _item(order_id=2, is_premarket=True, iv_source="STORED_IV"),
        _item(order_id=3, live_price=None, alignment_available=False),
    ]
    [embed] = create_telemetry_alignment_embeds(items)
    title = embed.title or ""
    assert title.count("[盤前/前日收盤]") == 1
    assert "[降級模式]" in title
    assert "[盤前/前日收盤]" in (embed.fields[0].name or "")


@pytest.mark.parametrize(
    "items, expected",
    [
        (
            [_item(suggestion_changed=True, suggested_qty=10, is_size_down=True)],
            0x8E44AD,
        ),
        ([_item(suggestion_changed=True, suggested_price=99.0)], 0xF39C12),
        ([_item()], 0x3498DB),
        (
            [_item(live_price=None, alignment_available=False)],
            0x99AAB5,
        ),
    ],
)
def test_embed_colour_follows_palette_rule(
    items: list[dict[str, Any]], expected: int
) -> None:
    [embed] = create_telemetry_alignment_embeds(items)
    assert embed.colour is not None
    assert embed.colour.value == expected
    assert expected in CURATED_PALETTE


def test_no_na_and_dividers_not_longer_than_content() -> None:
    card = _card(avg_cost=None, put_wall=None)
    assert "N/A" not in card
    lines = [_clean_ansi(ln) for ln in card.split("\n")[1:-1]]
    dividers = [ln for ln in lines if re.fullmatch(r" -+", ln)]
    content = [ln for ln in lines if ln.strip() and ln not in dividers]
    longest = max(_visual_len(ln) for ln in content)
    for d in dividers:
        assert len(d.strip()) <= longest


# ---------------------------------------------------------------- 入口測試


def _orders() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "user_id": 42,
            "symbol": "AAPL",
            "quantity": 10.0,
            "order_type": "LIMIT",
            "limit_price": 100.0,
            "side": "BUY",
        }
    ]


def _interaction() -> Any:
    interaction = AsyncMock()
    interaction.user.id = 42
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_telemetry_alert_zero_items_sends_one_ephemeral_info() -> None:
    from cogs.order_ui import OrderUICog

    cog = OrderUICog(MagicMock())
    interaction = _interaction()
    with (
        patch("cogs.order_ui.get_user_active_orders", return_value=_orders()),
        patch("database.get_user_holdings", return_value=[]),
        patch("database.get_user_portfolio", return_value=[]),
        patch(
            "services.order_telemetry_service.collect_macro_event_dates",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "services.order_telemetry_service.build_telemetry_alignment_items",
            new=AsyncMock(return_value=([], False)),
        ),
    ):
        await cog.telemetry_alert.callback(cog, interaction)  # type: ignore
    assert interaction.followup.send.await_count == 1
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "移動停損單不適用" in (kwargs["embed"].description or "")


@pytest.mark.asyncio
async def test_telemetry_alert_survives_calendar_failure() -> None:
    from cogs.order_ui import OrderUICog

    cog = OrderUICog(MagicMock())
    interaction = _interaction()
    built = AsyncMock(return_value=([_item(order_id=1)], False))
    with (
        patch("cogs.order_ui.get_user_active_orders", return_value=_orders()),
        patch("database.get_user_holdings", return_value=[]),
        patch("database.get_user_portfolio", return_value=[]),
        patch(
            "services.calendar_service.calendar_service.get_high_impact_events",
            new=AsyncMock(side_effect=RuntimeError("calendar down")),
        ),
        patch(
            "services.order_telemetry_service.build_telemetry_alignment_items",
            new=built,
        ),
    ):
        await cog.telemetry_alert.callback(cog, interaction)  # type: ignore
    assert interaction.followup.send.await_count == 1
    assert built.await_args is not None
    assert built.await_args.kwargs["macro_event_dates"] == set()


@pytest.mark.asyncio
async def test_telemetry_alert_pages_in_place_and_excludes_unavailable() -> None:
    from cogs.order_ui import OrderUICog
    from cogs.order_views import ApplyTelemetryView

    cog = OrderUICog(MagicMock())
    interaction = _interaction()
    items = [
        _item(
            order_id=i,
            suggestion_changed=True,
            suggested_price=99.0,
            **({"alignment_available": False, "live_price": None} if i == 3 else {}),
        )
        for i in range(1, 13)
    ]
    with (
        patch("cogs.order_ui.get_user_active_orders", return_value=_orders()),
        patch("database.get_user_holdings", return_value=[]),
        patch("database.get_user_portfolio", return_value=[]),
        patch(
            "services.order_telemetry_service.collect_macro_event_dates",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "services.order_telemetry_service.build_telemetry_alignment_items",
            new=AsyncMock(return_value=(items, False)),
        ),
    ):
        await cog.telemetry_alert.callback(cog, interaction)  # type: ignore

    assert interaction.followup.send.await_count == 1
    kwargs = interaction.followup.send.call_args.kwargs
    view = kwargs["view"]
    assert isinstance(view, ApplyTelemetryView)
    assert 3 not in view.suggestions and len(view.suggestions) == 11
    assert len(view.embeds) >= 2
    assert sum(len(e.fields) for e in view.embeds) == 12
    assert kwargs["embed"] is view.embeds[0]

    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    apply_buttons = [b for b in buttons if "一鍵套用" in (b.label or "")]
    assert len(apply_buttons) == 1 and apply_buttons[0].row == 1
    assert view.btn_prev.disabled is True and view.btn_next.disabled is False

    click = AsyncMock()
    await view.btn_next.callback(click)
    click.response.edit_message.assert_awaited_once()
    assert click.response.edit_message.call_args.kwargs["embed"] is view.embeds[1]
    assert view.btn_prev.disabled is False
    assert view.btn_next.disabled is (len(view.embeds) == 2)
    for _ in range(len(view.embeds) - 2):
        await view.btn_next.callback(AsyncMock())
    assert view.btn_next.disabled is True

    for _ in range(len(view.embeds) - 1):
        click2 = AsyncMock()
        await view.btn_prev.callback(click2)
    assert click2.response.edit_message.call_args.kwargs["embed"] is view.embeds[0]
    assert view.btn_prev.disabled is True


@pytest.mark.asyncio
async def test_scheduled_push_skips_unavailable_and_unchanged() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    items = [
        _item(order_id=1, suggestion_changed=True, suggested_price=99.0),
        _item(order_id=2),
        _item(
            order_id=3,
            suggestion_changed=True,
            suggested_price=99.0,
            alignment_available=False,
            live_price=None,
        ),
    ]
    with (
        patch("database.orders.get_all_active_orders", return_value=[
            {"id": 1, "user_id": 1, "symbol": "AAPL", "quantity": 10.0,
             "order_type": "LIMIT", "limit_price": 100.0, "side": "BUY"}
        ]),
        patch("database.is_notification_enabled", return_value=True),
        patch("database.get_user_holdings", return_value=[]),
        patch("database.get_user_portfolio", return_value=[]),
        patch(
            "services.order_telemetry_service.collect_macro_event_dates",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "services.order_telemetry_service.build_telemetry_alignment_items",
            new=AsyncMock(return_value=(items, False)),
        ),
        patch(
            "cogs.trading.telemetry.create_telemetry_alignment_embeds",
            return_value=[MagicMock()],
        ) as builder,
    ):  # fmt: skip
        from cogs.trading.telemetry import _dispatch_order_telemetry_alignment_alert

        await _dispatch_order_telemetry_alignment_alert(bot)

    sent = builder.call_args.args[0]
    assert [i["order_id"] for i in sent] == [1]
