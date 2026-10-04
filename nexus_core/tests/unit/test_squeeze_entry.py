"""多時間框架擠壓進場：重採樣、已收盤截斷、壓力區與等級規則。"""

from dataclasses import replace
from datetime import date, datetime
from typing import Any, Dict

import numpy as np
import pandas as pd
import pytest

from market_analysis.squeeze_entry.resistance import (
    ResistanceContext,
    ResistanceZone,
    cluster_zones,
    detect_resistance,
)
from market_analysis.squeeze_entry.rules import (
    STATUS_ENTRY,
    STATUS_NO_DATA,
    STATUS_NONE,
    STATUS_PENDING_BREAKOUT,
    STATUS_VETOED,
    STATUS_WATCH,
    evaluate_squeeze_entry,
)
from market_analysis.squeeze_entry.timeframes import (
    TimeframeState,
    build_matrix,
    resample_65m,
    resample_three_day,
    resample_weekly,
    split_daily_confirmed,
    split_intraday_confirmed,
)


# ---------------------------------------------------------------------------
# 重採樣與已收盤截斷
# ---------------------------------------------------------------------------
def _daily(dates: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(dates)
    close = np.linspace(100, 100 + n, n)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=dates,
    )


def test_split_daily_confirmed_drops_today() -> None:
    df = _daily(pd.bdate_range("2026-09-28", "2026-10-02"))
    conf, live = split_daily_confirmed(df, date(2026, 10, 2))
    assert live is True
    assert conf.index[-1].date() == date(2026, 10, 1)

    conf2, live2 = split_daily_confirmed(df, date(2026, 10, 3))
    assert live2 is False
    assert len(conf2) == len(df)


def test_resample_weekly_friday_boundary_and_live_week() -> None:
    df = _daily(pd.bdate_range("2026-09-14", "2026-10-01"))  # 週一 ~ 週四
    weekly, live = resample_weekly(df, date(2026, 10, 1))
    assert live is True  # 本週（10/2 週五結束）尚未完成
    assert [ts.date() for ts in weekly.index] == [date(2026, 9, 18), date(2026, 9, 25)]
    wk = weekly.loc[pd.Timestamp("2026-09-25")]
    sub = df.iloc[5:10]  # 2026-09-21 ~ 2026-09-25
    assert wk["Open"] == sub["Open"].iloc[0]
    assert wk["High"] == sub["High"].max()
    assert wk["Close"] == sub["Close"].iloc[-1]
    assert wk["Volume"] == sub["Volume"].sum()

    # 週六執行：上週五那一根已完成
    weekly_sat, live_sat = resample_weekly(
        _daily(pd.bdate_range("2026-09-14", "2026-10-02")), date(2026, 10, 3)
    )
    assert live_sat is False
    assert weekly_sat.index[-1].date() == date(2026, 10, 2)


def _ordinals(dates: pd.DatetimeIndex, offset: int = 0) -> Dict[date, int]:
    return {ts.date(): i + offset for i, ts in enumerate(dates)}


def test_three_day_groups_are_anchor_stable() -> None:
    dates = pd.bdate_range("2026-09-01", "2026-09-30")
    ords = _ordinals(dates, offset=1)  # 序號 1 起算 → 第一組只有 2 天
    df = _daily(dates)
    out_a, _ = resample_three_day(df, ords, date(2026, 10, 5))
    # 多一天資料、視窗起點不變 → 既有分組邊界不得位移
    df_b = df.iloc[3:]
    out_b, _ = resample_three_day(df_b, ords, date(2026, 10, 5))
    common = out_a.index.intersection(out_b.index)
    assert len(common) >= 3
    pd.testing.assert_frame_equal(out_a.loc[common[1:]], out_b.loc[common[1:]])


def test_three_day_incomplete_last_group_is_live() -> None:
    dates = pd.bdate_range("2026-09-01", "2026-09-10")  # 8 個交易日
    ords = _ordinals(dates)
    out, live = resample_three_day(_daily(dates), ords, date(2026, 9, 11))
    assert live is True  # 8 = 3+3+2，最後一組未滿
    assert len(out) == 2


def test_resample_65m_six_bars_per_day() -> None:
    idx = pd.date_range("2026-10-01 09:30", "2026-10-01 15:55", freq="5min")
    df = pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10.0},
        index=idx,
    )
    out = resample_65m(df)
    assert len(out) == 6
    assert out.index[0] == pd.Timestamp("2026-10-01 09:30")
    assert out.index[1] == pd.Timestamp("2026-10-01 10:35")
    assert out.index[-1] == pd.Timestamp("2026-10-01 14:55")
    assert out["Volume"].iloc[0] == 130.0  # 13 根 5m


def test_resample_65m_drops_extended_hours() -> None:
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2026-10-01 08:00"), pd.Timestamp("2026-10-01 09:30")]
    )
    df = pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx
    )
    assert len(resample_65m(df)) == 1


def test_split_intraday_confirmed() -> None:
    idx = pd.date_range("2026-10-01 09:30", periods=4, freq="15min")
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    conf, live = split_intraday_confirmed(df, 15, datetime(2026, 10, 1, 10, 20))
    assert live is True and len(conf) == 3  # 10:15 那一根尚未收盤
    conf2, live2 = split_intraday_confirmed(df, 15, datetime(2026, 10, 1, 10, 30))
    assert live2 is False and len(conf2) == 4


def test_build_matrix_missing_data_is_absent() -> None:
    m = build_matrix(None, None, None, datetime(2026, 10, 1, 12, 0), {})
    assert m == {}


# ---------------------------------------------------------------------------
# 壓力區
# ---------------------------------------------------------------------------
def test_cluster_zones_groups_within_half_atr() -> None:
    zones = cluster_zones([290.0, 293.3, 292.0, 310.0], atr_1d=8.0)
    assert zones == [ResistanceZone(290.0, 293.3, 3), ResistanceZone(310.0, 310.0, 1)]


def _ranging_daily(n: int = 60, cap: float = 293.3) -> pd.DataFrame:
    """在 270–cap 區間震盪、多次觸及上緣的日線。"""
    t = np.arange(n)
    close = 280 + 10 * np.sin(t / 3.0)
    high = np.minimum(close + 4, cap)
    high[::6] = cap  # 每 6 根觸頂一次
    low = close - 4
    idx = pd.bdate_range("2026-06-01", periods=n)
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close}, index=idx
    )


def test_detect_resistance_approaching_zone() -> None:
    df = _ranging_daily()
    ctx = detect_resistance(df, spot=291.0, atr_1d=6.0)
    assert ctx.overhead is not None
    assert ctx.overhead.top == pytest.approx(293.3)
    assert ctx.is_approaching is True
    assert ctx.broken is None

    far = detect_resistance(df, spot=270.0, atr_1d=6.0)
    assert far.is_approaching is False


def test_detect_resistance_daily_breakout() -> None:
    df = _ranging_daily()
    breakout = df.copy()
    breakout.iloc[-1, breakout.columns.get_loc("Close")] = 298.0
    breakout.iloc[-1, breakout.columns.get_loc("High")] = 299.0
    ctx = detect_resistance(breakout, spot=298.5, atr_1d=6.0)
    assert ctx.broken is not None
    assert ctx.broken.top == pytest.approx(293.3)

    # 現價又跌回區內 → 不算突破
    failed = detect_resistance(breakout, spot=292.0, atr_1d=6.0)
    assert failed.broken is None


def test_detect_resistance_65m_breakout() -> None:
    df = _ranging_daily()
    df.iloc[-1, df.columns.get_loc("Close")] = 290.0
    ctx = detect_resistance(df, spot=295.0, atr_1d=6.0, last_65m_close=294.5)
    assert ctx.broken is not None
    assert ctx.broken.top == pytest.approx(293.3)


def test_detect_resistance_insufficient_data() -> None:
    ctx = detect_resistance(_ranging_daily(10), spot=291.0, atr_1d=6.0)
    assert ctx.overhead is None and ctx.broken is None


# ---------------------------------------------------------------------------
# 等級規則
# ---------------------------------------------------------------------------
def _st(tf: str, **kw: object) -> TimeframeState:
    base = TimeframeState(
        timeframe=tf,
        squeeze_level="Release",
        is_squeezing=False,
        momentum_value=1.0,
        momentum_color="DarkBlue",
        green_dot=False,
        green_dot_bars_ago=None,
        turbo=False,
        squeeze_range_low=None,
        sma_20=100.0,
        last_close=105.0,
        bar_ts="2026-10-01",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _matrix(**overrides: Any) -> Dict[str, TimeframeState]:
    m = {tf: _st(tf) for tf in ("W", "3D", "D", "65m", "15m", "5m")}
    for tf, kw in overrides.items():
        m[tf] = replace(m[tf], **kw)
    return m


_SQ = {"is_squeezing": True, "squeeze_level": "Mid"}
_NO_RES = ResistanceContext(
    atr_1d=4.0, overhead=None, is_approaching=False, broken=None
)


def test_t1_crml_daily_squeeze_with_5m_trigger() -> None:
    m = _matrix(D=_SQ, **{"5m": {"green_dot": True, "green_dot_bars_ago": 0}})
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.status == STATUS_ENTRY and r.tier == 1 and r.size_pct == 1.0
    assert "5m Green Dot" in r.triggers


def test_t2_smci_multi_timeframe_squeeze() -> None:
    m = _matrix(
        W=_SQ,
        D={**_SQ, "momentum_color": "LightBlue"},
        **{"65m": _SQ},
    )
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.status == STATUS_ENTRY and r.tier == 2 and r.size_pct == 1.5


def test_t2_requires_daily_rising_momentum() -> None:
    m = _matrix(W=_SQ, D=_SQ, **{"65m": _SQ})  # D 深藍（動能 > 0 但下降）
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.tier is None and r.status == STATUS_WATCH


def test_t3_nasa_daily_green_dot() -> None:
    m = _matrix(
        D={"green_dot": True, "green_dot_bars_ago": 1, "momentum_color": "LightBlue"}
    )
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.status == STATUS_ENTRY and r.tier == 3 and r.size_pct == 2.5


def test_t3_three_day_turbo_needs_multi_squeeze() -> None:
    lone = evaluate_squeeze_entry(_matrix(**{"3D": {"turbo": True}}), _NO_RES)
    assert lone.tier is None
    m = _matrix(W=_SQ, D=_SQ, **{"3D": {**_SQ, "turbo": True}})
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.tier == 3 and "3D Turbo" in r.triggers


def test_bearish_preconditions_block_all_tiers() -> None:
    m = _matrix(D={"green_dot": True, "momentum_value": -0.5})
    assert evaluate_squeeze_entry(m, _NO_RES).status == STATUS_NONE
    m2 = _matrix(D={"green_dot": True}, W={"momentum_color": "Red"})
    assert evaluate_squeeze_entry(m2, _NO_RES).status == STATUS_NONE


def test_missing_daily_or_weekly_fails_closed() -> None:
    m = _matrix()
    del m["W"]
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.status == STATUS_NO_DATA and not r.passed


def test_be_pending_breakout_at_resistance() -> None:
    m = _matrix(W=_SQ, D=_SQ, **{"3D": {**_SQ, "turbo": True}})
    zone = ResistanceZone(292.0, 293.3, 3)
    res = ResistanceContext(atr_1d=6.0, overhead=zone, is_approaching=True, broken=None)
    r = evaluate_squeeze_entry(m, res)
    assert r.status == STATUS_PENDING_BREAKOUT and not r.passed
    assert r.size_pct is None and "293.30" in r.reason


def test_lower_breakout_does_not_exempt_next_overhead_zone() -> None:
    """SMCI 情境：剛突破較低的壓力區，但又頂到上方下一個壓力區 → 仍待突破。"""
    lower = ResistanceZone(41.0, 42.0, 2)
    upper = ResistanceZone(43.76, 44.59, 2)
    res = ResistanceContext(
        atr_1d=2.0, overhead=upper, is_approaching=True, broken=lower
    )
    m = _matrix(W=_SQ, D={**_SQ, "momentum_color": "LightBlue"}, **{"65m": _SQ})
    r = evaluate_squeeze_entry(m, res)
    assert r.status == STATUS_PENDING_BREAKOUT and r.size_pct is None
    assert "44.59" in r.reason


def test_breakout_acts_as_trigger_and_lifts_pending() -> None:
    zone = ResistanceZone(292.0, 293.3, 3)
    res = ResistanceContext(
        atr_1d=6.0, overhead=None, is_approaching=False, broken=zone
    )
    m = _matrix(D=_SQ)
    r = evaluate_squeeze_entry(m, res)
    assert r.status == STATUS_ENTRY and r.tier == 1 and "壓力區突破" in r.triggers


def test_hard_veto_and_downgrade() -> None:
    m = _matrix(D={"green_dot": True})
    v = evaluate_squeeze_entry(m, _NO_RES, hard_vetoes=["財報 2 天內"])
    assert v.status == STATUS_VETOED and v.size_pct is None

    dg = evaluate_squeeze_entry(m, _NO_RES, downgrade_reason="逃頂警戒")
    assert dg.status == STATUS_ENTRY and dg.tier == 2 and dg.size_pct == 1.5

    m1 = _matrix(D=_SQ, **{"15m": {"turbo": True}})
    dg1 = evaluate_squeeze_entry(m1, _NO_RES, downgrade_reason="逃頂警戒")
    assert dg1.status == STATUS_WATCH and dg1.tier is None


def test_reference_stop_uses_daily_range_and_sma() -> None:
    m = _matrix(D={"green_dot": True, "squeeze_range_low": 98.0, "sma_20": 101.0})
    r = evaluate_squeeze_entry(m, _NO_RES)
    assert r.stop == pytest.approx(98.0 - 0.5 * 4.0)

    m2 = _matrix(D={"green_dot": True, "squeeze_range_low": None, "sma_20": 101.0})
    assert evaluate_squeeze_entry(m2, _NO_RES).stop == pytest.approx(99.0)

    no_atr = ResistanceContext(
        atr_1d=0.0, overhead=None, is_approaching=False, broken=None
    )
    assert evaluate_squeeze_entry(m2, no_atr).stop is None


def test_build_matrix_all_six_timeframes() -> None:
    rng = np.random.default_rng(1)
    d_idx = pd.bdate_range("2024-10-01", "2026-10-01")
    d_close = 100 + np.cumsum(rng.normal(0, 1, len(d_idx)))
    df_d = pd.DataFrame(
        {
            "Open": d_close,
            "High": d_close + 1,
            "Low": d_close - 1,
            "Close": d_close,
            "Volume": 1e6,
        },
        index=d_idx,
    )
    days = pd.bdate_range("2026-09-01", "2026-10-01")
    m5_idx = pd.DatetimeIndex(
        [
            ts
            for day in days
            for ts in pd.date_range(
                f"{day.date()} 09:30", f"{day.date()} 15:55", freq="5min"
            )
        ]
    )
    m5_close = 100 + np.cumsum(rng.normal(0, 0.1, len(m5_idx)))
    df_5m = pd.DataFrame(
        {
            "Open": m5_close,
            "High": m5_close + 0.1,
            "Low": m5_close - 0.1,
            "Close": m5_close,
            "Volume": 1e4,
        },
        index=m5_idx,
    )
    df_15m = df_5m.groupby(df_5m.index.floor("15min")).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    ords = {ts.date(): i for i, ts in enumerate(d_idx)}
    now = datetime(2026, 10, 1, 12, 7)
    m = build_matrix(df_d, df_15m, df_5m, now, ords)
    assert set(m) == {"W", "3D", "D", "65m", "15m", "5m"}
    # 只用已收盤 K 棒：D 不含今日、15m 最後一根為 11:45、5m 為 12:00
    assert m["D"].bar_ts.startswith("2026-09-30")
    assert m["15m"].bar_ts == "2026-10-01 11:45:00"
    assert m["5m"].bar_ts == "2026-10-01 12:00:00"
    assert m["65m"].bar_ts == "2026-10-01 10:35:00"
    assert m["D"].preview_squeeze_level is not None


# ---------------------------------------------------------------------------
# 否決與降級
# ---------------------------------------------------------------------------
async def _run_vetoes(c5: Any, vts: float, tier: str) -> Any:
    from unittest.mock import AsyncMock, patch

    from market_analysis.squeeze_entry import vetoes as mod

    with (
        patch(
            "market_analysis.dynamic_rollover.opportunity_cost."
            "_confirm_entry_condition5_macro_earnings_gate",
            c5,
        ),
        patch(
            "services.market_data_service.get_vix_term_structure",
            AsyncMock(return_value={"vts_ratio": vts, "is_valid": True}),
        ),
        patch.object(mod, "compute_macro_escape_tier", AsyncMock(return_value=tier)),
    ):
        return await mod.resolve_long_entry_vetoes("NVDA")


async def test_vetoes_clean_environment() -> None:
    from unittest.mock import AsyncMock

    vetoes, downgrade = await _run_vetoes(
        AsyncMock(return_value=(True, None)), 0.9, "NORMAL"
    )
    assert vetoes == [] and downgrade is None


async def test_vetoes_earnings_backwardation_and_escape_tier() -> None:
    async def _c5(symbol: str, prior: bool, reasons: list) -> Any:
        reasons.append("條件五❌：即將於 2 天內發布財報，避開高波事件風險")
        return False, 2

    vetoes, downgrade = await _run_vetoes(_c5, 1.15, "WATCH")
    assert vetoes[0] == "即將於 2 天內發布財報，避開高波事件風險"
    assert any("深度倒掛" in v for v in vetoes)
    assert downgrade == "逃頂警戒 WATCH"


async def test_macro_escape_tier_fails_closed_to_unknown() -> None:
    from unittest.mock import AsyncMock, patch

    from market_analysis.squeeze_entry.vetoes import compute_macro_escape_tier

    with patch(
        "market_analysis.index_microstructure.get_market_regime",
        AsyncMock(side_effect=RuntimeError("net")),
    ):
        assert await compute_macro_escape_tier() == "UNKNOWN"


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------
def test_squeeze_entry_embed_renders_matrix_verdict_and_sizing() -> None:
    from cogs.embed_builders.squeeze_entry_embeds import create_squeeze_entry_embed
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    m = _matrix(
        D={"green_dot": True, "green_dot_bars_ago": 1, "momentum_color": "LightBlue"},
        **{
            "3D": {
                "turbo": True,
                "preview_squeeze_level": "Mid",
                "preview_momentum_color": "LightBlue",
            }
        },
    )
    del m["5m"]
    zone = ResistanceZone(292.0, 293.3, 3)
    res = ResistanceContext(atr_1d=6.0, overhead=zone, is_approaching=True, broken=None)
    result = SqueezeEntryResult(
        STATUS_ENTRY, 3, 2.5, 280.0, "✅ T3：D Green Dot", ["D Green Dot"], 2
    )
    embed = create_squeeze_entry_embed(
        "BE",
        SqueezeEvaluation(result, m, res),
        passed=True,
        reason=result.reason,
        entry_price=290.0,
        stop_loss=280.0,
    )
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert "Green Dot" in text and "（1 根前）" in text
    assert "Turbo" in text and "盤中未收盤預覽" in text
    assert " 5m  │ 資料不足" in text
    assert "T3（2.5%）" in text and "總資產 2.5%" in text
    assert "衝擊中" in text and "$293.30" in text
    assert "$280.00" in text


def test_squeeze_entry_embed_without_levels_has_no_price_field() -> None:
    from cogs.embed_builders.squeeze_entry_embeds import create_squeeze_entry_embed
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    result = SqueezeEntryResult(STATUS_NO_DATA, None, None, None, "⛔ 缺少 D/W")
    embed = create_squeeze_entry_embed(
        "CRML", SqueezeEvaluation(result, {}, None), passed=False, reason=result.reason
    )
    names = [f.name for f in embed.fields]
    assert "💰 價位與部位建議" not in names
