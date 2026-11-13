"""/x 與擠壓推播的五項資料異常防護（僅呈現，不改任何判定值）。"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from cogs.embed_builders.squeeze_entry_embeds import create_squeeze_entry_embed
from market_analysis.risk_engine import OptimizationResult
from tests.unit.test_x_panel_timeframe_coherence import (
    _spcx,
    _tf,
)


def _render(d: dict[str, Any]) -> str:
    """同 _render_base，但讓 patch 後的 datetime 仍認得真實 datetime（bar 時間）。"""
    from unittest.mock import patch

    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
    from market_time import ny_tz

    fixed = datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)

    class _Meta(type):
        def __instancecheck__(cls, inst: object) -> bool:
            return isinstance(inst, datetime)

    class _FakeDT(datetime, metaclass=_Meta):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return fixed  # type: ignore[return-value]

    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        embed = create_tactical_symbol_embed(d)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _plain(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# --- A 停損方向 ---------------------------------------------------------------


def test_a_stop_above_price_flagged() -> None:
    d = _spcx(price=160.9)
    d["squeeze_eval"].result.stop = 190.0
    text = _plain(_render(d))
    assert "參考停損: $190.00 (⚠ 高於現價 +18.09%，錨點已失守)" in text
    assert "↓-" not in text
    stop_line = next(ln for ln in text.splitlines() if "參考停損" in ln)
    assert "超寬" not in stop_line


def test_a_stop_below_price_unchanged() -> None:
    text = _plain(_render(_spcx()))
    assert "參考停損: $135.20 (↓15.97%) ⚠ 超寬" in text


def _ev() -> SimpleNamespace:
    return SimpleNamespace(
        result=SimpleNamespace(
            status="ENTRY", tier=3, size_pct=5, triggers=[], reason="T3"
        ),
        matrix={},
        resistance=None,
    )


def _push_text(entry: float, stop: float) -> str:
    embed = create_squeeze_entry_embed(
        "NVDA", _ev(), passed=True, reason="x", entry_price=entry, stop_loss=stop
    )
    return "\n".join(f.value or "" for f in embed.fields)


def test_a_push_stop_above_entry() -> None:
    text = _push_text(166.19, 196.33)
    assert "（⚠ 高於進場價，錨點已失守）" in text
    assert "較低者" not in text


def test_a_push_stop_below_entry_unchanged() -> None:
    text = _push_text(166.19, 150.0)
    assert "較低者" in text and "錨點已失守" not in text


# --- B Kelly 0 口原因 ---------------------------------------------------------


def _kelly_render(unit: float | None) -> str:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=31.3,
        risk_budget_shares=10.0,
    )
    d: dict[str, Any] = _spcx(kelly_sizing=res, kelly_beta=1.78)
    if unit is not None:
        d["kelly_unit_weighted_delta"] = unit
    return _plain(_render(d))


def test_b_zero_lot_reason_shows_iv_ratio() -> None:
    text = _kelly_render(6.3)
    assert (
        "0 口原因: 單口 ≈6.3 股×IV比4.97≈31.3 股 SPY 等值 > 可用額度 ≈10.0 股" in text
    )


def test_b_zero_lot_reason_without_unit_keeps_old_wording() -> None:
    text = _kelly_render(None)
    assert "單口 ≈31.3 股 SPY 等值（含 IV 調整）> 可用額度 ≈10.0 股" in text


# --- C 前一交易日 RVOL --------------------------------------------------------


def _bar_dt(day: int) -> datetime:
    from market_time import ny_tz

    return datetime(2026, 10, day, 15, 45, tzinfo=ny_tz)


def test_c_prior_session_bar_not_live_verdict() -> None:
    d = _spcx(bar_15m_time=_bar_dt(6), rvol_15m=1.83, rvol_15m_tod=1.38)
    text = _plain(_render(d))
    assert "@10-06 15:45 [前一交易日]" in text
    assert (
        "量比 (RVOL_15m，前一交易日): 1.83x｜同時段 1.38x (⏸ 非即時，不判量能)" in text
    )
    assert "即時量比" not in text


def test_c_prior_session_without_tod() -> None:
    d = _spcx(bar_15m_time=_bar_dt(6), rvol_15m=1.83)
    text = _plain(_render(d))
    assert "量比 (RVOL_15m，前一交易日): 1.83x (⏸ 非即時，不判量能)" in text


def test_c_same_day_bar_keeps_live_line() -> None:
    d = _spcx(bar_15m_time=_bar_dt(7), rvol_15m=1.83)
    text = _plain(_render(d))
    assert "即時量比 (RVOL_15m): 1.83x" in text
    assert "前一交易日): " not in text


# --- D 估值 ---------------------------------------------------------------------


def test_d_tdp_bearish_momentum_warns() -> None:
    d = _spcx(tdp_activated=True)
    d["squeeze_eval"].matrix = {"D": _tf(-26.69, "Red")}
    raw = _render(d)
    text = _plain(raw)
    assert "DDP/TDP 估值: ⚠️ TDP 估值三擊" in text
    assert "（D 動能 -26.69，慎防接刀）" in text
    assert "DDP 估值雙擊" not in text


def test_d_tdp_positive_momentum_unchanged() -> None:
    d = _spcx(tdp_activated=True)
    d["squeeze_eval"].matrix = {"D": _tf(5.0, "Green")}
    text = _plain(_render(d))
    assert "DDP/TDP 估值: ✨ TDP 估值三擊" in text
    assert "慎防接刀" not in text


def test_d_tdpq_bearish_uses_yellow() -> None:
    d = _spcx(tdpq_activated=True)
    d["squeeze_eval"].matrix = {"D": _tf(0.0, "Red")}
    raw = _render(d)
    assert "\u001b[1;33m⚠️ TDPQ" in raw


# --- E Reddit 缺失 --------------------------------------------------------------


def _sync_base(reddit: str) -> dict[str, Any]:
    return _spcx(
        skew=4.5,
        skew_percentile=55.0,
        pcr={"volume_pcr": 0.85},
        reddit_sentiment_score=reddit,
    )


def test_e_reddit_down_not_green_sync() -> None:
    raw = _render(_sync_base("⚠️ 抓取失敗 (邊緣節點異常)"))
    assert "同步（Reddit 缺失，散戶背離未判定）" in _plain(raw)
    assert "\u001b[1;30m同步（Reddit 缺失，散戶背離未判定）" in raw
    assert "\u001b[1;32m同步" not in raw


def test_e_reddit_normal_stays_sync() -> None:
    raw = _render(_sync_base("⚖️ 中性"))
    assert "\u001b[1;32m同步\u001b[0m" in raw
    assert "Reddit 缺失" not in raw
