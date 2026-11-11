"""/x 面板揭露：快取 GEX 年齡與現價偏離、GEX 完全暫缺、UOA 的 OI 未就緒空狀態。"""

import logging
import time
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from tests.unit.test_x_panel_daily_stop_ref import _base

_GEX = "🧲 Gamma 曝險分布"


def _gex_field(embed: Any) -> Any:
    return next(f for f in embed.fields if _GEX in (f.name or ""))


def _uoa_field(embed: Any) -> Any:
    return next(f for f in embed.fields if "異常活動" in (f.name or ""))


def _stale(d: dict[str, Any], snap_spot: float, age_s: float | None) -> dict[str, Any]:
    g = d["gex_profile_data"]
    g["_is_stale_cache"] = True
    g["spot"] = snap_spot
    if age_s is not None:
        g["_data_ts"] = time.time() - age_s
    return d


def test_stale_with_data_ts_shows_age_in_field_name() -> None:
    e = create_tactical_symbol_embed(_stale(_base(), 154.0, 10 * 3600))
    name = _gex_field(e).name
    assert "更新於10小時前" in name
    assert "快取 / API 降級" in name


def test_stale_without_data_ts_keeps_legacy_suffix() -> None:
    e = create_tactical_symbol_embed(_stale(_base(), 154.0, None))
    name = _gex_field(e).name
    assert name.endswith(" [快取 / API 降級]")
    assert "更新於" not in name


def test_large_drift_adds_line_with_reference_only_note() -> None:
    # 快照現價 195.0 → 即時 154.0（約 −21%），ATR₁D 2.5（約 1.6%）
    e = create_tactical_symbol_embed(_stale(_base(), 195.0, 3600))
    value = _gex_field(e).value
    assert "快照現價 $195.00 → 即時 $154.00" in value
    assert "僅供參考" in value
    assert value.rstrip("\u200b\n ").endswith("```")


def test_drift_above_one_percent_but_within_atr_has_no_reference_note() -> None:
    # 偏離約 1.3%，ATR₁D 2.5/154 約 1.6%
    e = create_tactical_symbol_embed(_stale(_base(), 152.0, 3600))
    value = _gex_field(e).value
    assert "快照現價" in value
    assert "僅供參考" not in value


def test_small_drift_has_no_drift_line() -> None:
    e = create_tactical_symbol_embed(_stale(_base(), 154.5, 3600))
    assert "快照現價" not in _gex_field(e).value


def test_fresh_gex_has_no_age_or_drift() -> None:
    d = _base()
    d["gex_profile_data"]["spot"] = 195.0
    e = create_tactical_symbol_embed(d)
    f = _gex_field(e)
    assert "更新於" not in f.name and "降級" not in f.name
    assert "快照現價" not in f.value


def test_empty_gex_profile_shows_missing_field() -> None:
    d = _base()
    d["gex_profile_data"] = None
    e = create_tactical_symbol_embed(d)
    assert "GEX 資料暫缺" in _gex_field(e).value
    d2 = _base()
    d2["gex_profile_data"] = {"gex_profile": {}}
    assert "GEX 資料暫缺" in _gex_field(create_tactical_symbol_embed(d2)).value


def test_present_profile_has_no_missing_field() -> None:
    e = create_tactical_symbol_embed(_base())
    assert "GEX 資料暫缺" not in _gex_field(e).value


def test_empty_uoa_oi_missing_shows_not_ready_text() -> None:
    e = create_tactical_symbol_embed(_base(uoa=[], uoa_oi_missing=True))
    v = _uoa_field(e).value
    assert "期權鏈未平倉量尚未就緒" in v
    assert "目前無顯著異常活動" not in v


def test_empty_uoa_oi_ready_or_unknown_shows_no_activity() -> None:
    for flag in (False, None):
        e = create_tactical_symbol_embed(_base(uoa=[], uoa_oi_missing=flag))
        assert "目前無顯著異常活動" in _uoa_field(e).value


def test_uoa_render_failure_logs_and_adds_line(caplog: Any) -> None:
    row = {"strike": 160.0, "type": "CALL", "volume": 1}
    with (
        patch(
            "cogs.embed_builders.portfolio_embeds._format_uoa_field",
            side_effect=RuntimeError("x"),
        ),
        caplog.at_level(logging.WARNING, logger="cogs.embed_builders.portfolio_embeds"),
    ):
        e = create_tactical_symbol_embed(_base(uoa=[row]))
    assert "UOA 表格渲染失敗" in _uoa_field(e).value
    assert any("UOA 表格渲染失敗" in r.getMessage() for r in caplog.records)


def test_worst_case_stale_empty_uoa_stays_within_budget() -> None:
    from datetime import datetime

    from market_time import ny_tz
    from tests.unit.test_x_panel_timeframe_coherence import (
        _render_embed,
        _total_chars,
        _worst_case_data,
    )

    now = datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz)
    fresh = _render_embed(_worst_case_data(), now)
    d = _worst_case_data()
    live = float((d.get("quote") or {}).get("c") or d["price"])
    g = d["gex_profile_data"]
    g["_is_stale_cache"] = True
    g["_data_ts"] = time.time() - 10 * 3600
    g["spot"] = round(live / 0.79, 2)
    d["uoa"] = []
    d["uoa_oi_missing"] = True
    stale = _render_embed(d, now)
    assert _total_chars(stale) <= 5800, _total_chars(stale)
    assert len(stale.to_dict()["fields"]) == len(stale.fields), "欄位被靜默 pop"
    assert [f.name.split(" [")[0].split(" (更新")[0] for f in stale.fields] == [
        f.name for f in fresh.fields
    ]
    assert "更新於10小時前" in _gex_field(stale).name
    assert "快照現價" in _gex_field(stale).value
