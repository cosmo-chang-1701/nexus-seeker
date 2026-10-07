"""/x 面板 PR-B：Kelly 單位、擠壓矩陣、IV 狀態文字、DDP 原因、Polymarket 池量。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.embed_builders._embed_helpers import (
    format_psq_matrix_lines,
    macro_iv_status_text,
)
from cogs.embed_builders.portfolio_embeds import (
    _parse_poly_bullish_pct,
    create_tactical_symbol_embed,
)


def _text(embed: Any) -> str:
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def test_kelly_unit_weighted_delta_matches_nro_definition() -> None:
    from market_analysis.risk_engine import optimize_position_risk, build_macro_context

    unit_wd = 0.16 * 1.0 * (168.0 / 775.0) * 100.0
    assert unit_wd == pytest.approx(3.47, abs=0.01)
    macro = build_macro_context({"vix": 15.71, "oil": 89.5, "vix_change": 0.0})
    kwargs: dict[str, Any] = dict(
        current_delta=0.0,
        user_capital=100000.0,
        spy_price=775.0,
        stock_iv=0.448,
        strategy="STO",
        macro_data=macro,
        risk_limit=15.0,
        vix_spot=15.71,
        pcr=1.22,
        skew=-1.17,
        data_degraded_reasons=["IV Rank 缺失"],
    )
    old = optimize_position_risk(unit_weighted_delta=0.16, **kwargs)
    new = optimize_position_risk(unit_weighted_delta=unit_wd, **kwargs)
    assert old.suggested_contracts > new.suggested_contracts
    assert new.suggested_contracts <= 1


def _state(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        squeeze_level="Release",
        momentum_color="LightBlue",
        momentum_value=6.04,
        green_dot=False,
        green_dot_bars_ago=None,
        turbo=False,
        bar_ts="2026-10-07 11:00:00",
        preview_squeeze_level=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_format_psq_matrix_lines_flags_and_missing() -> None:
    matrix = {
        "D": _state(green_dot=True, green_dot_bars_ago=1, bar_ts="2026-10-06 00:00:00"),
        "15m": _state(turbo=True, preview_squeeze_level="Mid"),
    }
    lines = format_psq_matrix_lines(matrix)
    text = "\n".join(lines)
    assert len(lines) == 7  # 標題＋6 個框架
    assert "GD 1 根前" in text and "10-06" in text
    assert "Turbo" in text and "11:00" in text and "成型中" in text
    assert "W   │ —（資料不足）" in text
    assert lines[-1].startswith(" └─")
    assert format_psq_matrix_lines(None) == []


def test_macro_iv_status_text_branches() -> None:
    assert "1.4x" in macro_iv_status_text("STORED_IV", True)
    assert "即時 IV 已含事件定價" in macro_iv_status_text("LIVE_IV", False)
    assert "已校正" not in macro_iv_status_text("LIVE_IV", False)
    assert "快取 IV" in macro_iv_status_text("STORED_IV", False)


def _iv(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        current_iv=0.448,
        iv_rank=None,
        iv_percentile=None,
        expected_move_weekly=10.39,
        iv_status="Normal",
        is_premarket=False,
        iv_source="LIVE_IV",
        has_earnings_event=False,
        has_macro_event=True,
        event_loading_applied=False,
        iv_history_count=5,
        iv_history_required=60,
        hv_20=0.70,
        straddle_expiry="2026-10-16",
        straddle_dte=9,
        straddle_implied_iv=0.446,
        iv_term_structure_status="Contango",
        term_structure_ratio=0.95,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_iv_field_shows_accumulation_hv_and_em_label() -> None:
    data = {
        "symbol": "SPCX",
        "price": 168.19,
        "iv_data": _iv(),
        "iv_rank": None,
        "expected_move_context": {
            "reference_price": 168.19,
            "reference_label": "現價",
            "expected_move_lower": 157.8,
            "expected_move_upper": 178.58,
        },
    }
    text = _text(create_tactical_symbol_embed(data))
    assert "樣本累積中 5/60 日" in text
    assert "IV/HV20" in text
    assert "7 日 1σ（10-16 跨式 ×√(7/9)）" in text
    assert "現價 $168.19" in text
    assert "快取波動率已校正" not in text
    assert "即時 IV 已含事件定價" in text


def test_ddp_reason_and_skew_samples_displayed() -> None:
    data = {
        "symbol": "SPCX",
        "price": 168.19,
        "is_ddp": False,
        "ddp_reason": "資料不足（季報未滿 5 季，新上市）",
        "skew": -1.17,
        "skew_percentile": None,
        "skew_sample_size": 1,
    }
    text = _text(create_tactical_symbol_embed(data))
    assert "不符合（資料不足（季報未滿 5 季，新上市））" in text
    assert "樣本 1/20" in text


def test_squeeze_matrix_and_entry_block_rendered() -> None:
    from market_analysis.squeeze_entry import rules as sq

    result = SimpleNamespace(
        status=sq.STATUS_ENTRY,
        tier=3,
        size_pct=2.5,
        stop=155.0,
        triggers=["D Green Dot"],
        reason="ok",
        resistance_warning=None,
    )
    ev = SimpleNamespace(result=result, matrix={"D": _state()})
    data = {
        "symbol": "SPCX",
        "price": 168.0,
        "squeeze_eval": ev,
        "psq_result": SimpleNamespace(
            is_squeezing=False,
            momentum_value=7.2,
            squeeze_level="Release",
            signal_direction="Long",
            vix_momentum_label="NORMAL",
            green_dot_bars_ago=1,
        ),
    }
    text = _text(create_tactical_symbol_embed(data))
    assert "Green Dot: 1 根前" in text
    assert "多時間框架 (已收盤 K 棒)" in text
    assert "T3 → 建議部位 2.5%" in text
    assert "參考停損: $155.00" in text


@pytest.mark.asyncio
async def test_polymarket_low_pool_not_interpreted() -> None:
    from cogs.unified_terminal.utils import calculate_polymarket_weighted_odds

    markets = [
        {
            "question": "Will NVDA close above $200?",
            "volumeNum": 5_000.0,
            "tokens": [{"outcome": "Yes", "price": 0.8}],
        }
    ]
    with patch(
        "cogs.unified_terminal.utils._get_matched_poly_markets",
        new_callable=AsyncMock,
        return_value=markets,
    ):
        summary = await calculate_polymarket_weighted_odds("NVDA", markets)
    assert "池量不足" in summary and "巨鯨" not in summary
    assert _parse_poly_bullish_pct(summary) is None


@pytest.mark.asyncio
async def test_ddp_inspector_records_reason_for_new_listing() -> None:
    from market_analysis.ddp_inspector import DDPInspector

    q_inc = pd.DataFrame(
        {"c0": [1.0, 100.0], "c1": [1.0, 90.0], "c2": [1.0, 80.0]},
        index=["Diluted EPS", "Total Revenue"],
    )
    with patch(
        "market_analysis.ddp_inspector.market_data_service.call_yf",
        new_callable=AsyncMock,
        return_value=({"sector": "Industrials"}, q_inc),
    ):
        insp = DDPInspector(MagicMock())
        assert await insp.inspect_symbol("SPCX") is None
    assert insp.last_fail_reason["SPCX"] == "資料不足（季報未滿 5 季，新上市）"


def test_rvol_wording_by_candle_direction() -> None:
    from datetime import datetime

    from market_analysis.price_volume_alert import Confirmed15mBar

    def render(open_: float, close: float) -> str:
        bar = Confirmed15mBar(
            symbol="SPY",
            bar_time=datetime.now(),
            open=open_,
            high=max(open_, close) + 1,
            low=min(open_, close) - 1,
            close=close,
            volume=800000.0,
            avg_volume=1000000.0,
        )
        return _text(create_tactical_symbol_embed({"symbol": "SPY", "bar_15m": bar}))

    assert "縮量回檔" in render(550.0, 547.5)
    assert "缺乏放量代償" in render(547.5, 550.0)


def test_select_straddle_expiry_prefers_dte_nearest_seven() -> None:
    from datetime import date

    from market_analysis.sentiment.iv_metrics import _select_straddle_expiry

    expiries = ["2026-10-08", "2026-10-09", "2026-10-16", "2026-10-30"]
    assert _select_straddle_expiry(expiries, date(2026, 10, 7)) == (9, "2026-10-16")
    assert _select_straddle_expiry(["2026-10-07"], date(2026, 10, 7)) is None


def test_catalyst_calendar_prioritises_key_events_and_merges_cpi() -> None:
    def ev(name: str, time: str, tte: float) -> SimpleNamespace:
        return SimpleNamespace(event=name, time=time, tte_hours=tte)

    cats = [
        ev("成屋銷售", "2026-10-13T14:00:00Z", 100.0),
        ev("密大消費者信心指數", "2026-10-09T14:00:00Z", 46.0),
        ev("核心 CPI 月增率", "2026-10-14T12:30:00Z", 150.0),
        ev("CPI 月增率", "2026-10-14T12:30:00Z", 150.0),
        ev("CPI 年增率", "2026-10-14T12:30:00Z", 150.0),
        ev("FOMC Minutes", "2026-10-07T18:00:00Z", 2.7),
    ]
    data = {"symbol": "SPCX", "price": 168.0, "iv_data": _iv(), "catalysts": cats}
    text = _text(create_tactical_symbol_embed(data))
    assert "FOMC Minutes" in text
    assert "CPI 等 3 項" in text
    assert "成屋銷售" not in text
    assert "已省略" in text
