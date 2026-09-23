"""market_analysis/stream_bars.py — Alpaca 串流純計算層的單元測試。"""

from collections import deque
from datetime import datetime, timedelta, timezone

from market_analysis.stream_bars import (
    MinuteBar,
    SessionStats,
    StreamTier,
    aggregate_15m,
    apply_forward_fill,
    classify_symbol_tier,
    compute_technicals,
    rebuild_buffer,
)

OPEN = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)


def _bar(
    minute: int, close: float = 100.0, volume: float = 100.0, **kw: float
) -> MinuteBar:
    return MinuteBar(
        ts=OPEN + timedelta(minutes=minute),
        open=kw.get("open", close),
        high=kw.get("high", close),
        low=kw.get("low", close),
        close=close,
        volume=volume,
    )


def _buf() -> deque[MinuteBar]:
    return deque(maxlen=200)


# ---------------------------------------------------------------------------
# Forward Fill
# ---------------------------------------------------------------------------


def test_forward_fill_contiguous_bar_adds_no_synthetic() -> None:
    buf = _buf()
    apply_forward_fill(buf, _bar(0), OPEN, 30)
    added = apply_forward_fill(buf, _bar(1), OPEN, 30)
    assert len(buf) == 2
    assert [b.is_forward_filled for b in added] == [False]


def test_forward_fill_fills_gap_with_previous_close_and_zero_volume() -> None:
    buf = _buf()
    apply_forward_fill(buf, _bar(0, close=101.0), OPEN, 30)
    added = apply_forward_fill(buf, _bar(4, close=105.0), OPEN, 30)

    assert [b.ts for b in buf] == [OPEN + timedelta(minutes=m) for m in range(5)]
    synthetic = [b for b in added if b.is_forward_filled]
    assert len(synthetic) == 3
    assert all(b.close == b.open == b.high == b.low == 101.0 for b in synthetic)
    assert all(b.volume == 0.0 for b in synthetic)
    assert added[-1].close == 105.0 and not added[-1].is_forward_filled


def test_forward_fill_gap_exceeding_limit_is_not_filled() -> None:
    buf = _buf()
    apply_forward_fill(buf, _bar(0), OPEN, 30)
    apply_forward_fill(buf, _bar(45), OPEN, 30)
    assert len(buf) == 2
    assert not any(b.is_forward_filled for b in buf)


def test_forward_fill_never_creates_bars_before_session_open() -> None:
    buf = _buf()
    apply_forward_fill(buf, _bar(-3), None, 30)  # 盤前最後一根
    apply_forward_fill(buf, _bar(2), OPEN, 30)
    synthetic = [b for b in buf if b.is_forward_filled]
    assert [b.ts for b in synthetic] == [OPEN, OPEN + timedelta(minutes=1)]


def test_same_minute_replaces_last_bar() -> None:
    buf = _buf()
    apply_forward_fill(buf, _bar(0, close=100.0), OPEN, 30)
    apply_forward_fill(buf, _bar(0, close=100.5), OPEN, 30)
    assert len(buf) == 1
    assert buf[-1].close == 100.5


def test_older_bar_replaces_matching_minute_or_is_dropped() -> None:
    buf = _buf()
    for m in range(3):
        apply_forward_fill(buf, _bar(m), OPEN, 30)

    added = apply_forward_fill(buf, _bar(1, close=99.0), OPEN, 30)
    assert added and buf[1].close == 99.0
    assert len(buf) == 3

    buf2 = _buf()
    apply_forward_fill(buf2, _bar(5), OPEN, 30)
    assert apply_forward_fill(buf2, _bar(2), OPEN, 30) == []
    assert len(buf2) == 1


def test_rebuild_buffer_sorts_dedups_and_refills() -> None:
    bars = [_bar(3), _bar(0), _bar(3, close=103.0)]
    buf = rebuild_buffer(bars, 200, lambda ts: OPEN, 30)
    assert [b.ts.minute for b in buf] == [30, 31, 32, 33]
    assert sum(1 for b in buf if b.is_forward_filled) == 2


# ---------------------------------------------------------------------------
# Session stats / VWAP
# ---------------------------------------------------------------------------


def test_session_vwap_is_anchored_and_ignores_synthetic_bars() -> None:
    stats = SessionStats(session_date="2026-09-22")
    stats.add(_bar(0, close=10.0, volume=100.0))
    stats.add(_bar(1, close=20.0, volume=300.0))
    stats.add(
        MinuteBar(
            ts=OPEN + timedelta(minutes=2),
            open=20.0, high=20.0, low=20.0, close=20.0, volume=0.0,
            is_forward_filled=True,
        )
    )  # fmt: skip
    assert stats.vwap == (10.0 * 100 + 20.0 * 300) / 400
    assert stats.last_real_bar_end == OPEN + timedelta(minutes=2)
    assert stats.open == 10.0


def test_session_remove_reverts_vwap_contribution() -> None:
    stats = SessionStats(session_date="2026-09-22")
    old = _bar(0, close=10.0, volume=100.0)
    stats.add(old)
    stats.add(_bar(1, close=20.0, volume=100.0))
    stats.remove(old)
    stats.add(_bar(0, close=12.0, volume=100.0))
    assert stats.vwap == (12.0 * 100 + 20.0 * 100) / 200
    assert stats.volume_sum == 200.0


def test_session_high_low_track_extremes() -> None:
    stats = SessionStats(session_date="2026-09-22")
    stats.add(_bar(0, close=100.0, high=101.0, low=99.0))
    stats.add(_bar(1, close=103.0, high=104.0, low=102.0))
    assert (stats.high, stats.low, stats.last_close) == (104.0, 99.0, 103.0)


# ---------------------------------------------------------------------------
# 15 分 K 聚合
# ---------------------------------------------------------------------------


def test_aggregate_15m_uses_only_real_bars_in_window() -> None:
    bars = [
        _bar(0, close=100.0, open=99.0, high=100.5, low=98.5, volume=10.0),
        _bar(7, close=101.0, high=102.0, low=100.0, volume=20.0),
        _bar(14, close=100.8, volume=30.0),
        _bar(15, close=500.0, volume=999.0),  # 下一個時窗
    ]
    agg = aggregate_15m(bars, OPEN, prev_close=None)
    assert agg is not None
    assert (agg.open, agg.high, agg.low, agg.close, agg.volume) == (
        99.0, 102.0, 98.5, 100.8, 60.0,
    )  # fmt: skip


def test_aggregate_15m_empty_window_uses_prev_close() -> None:
    agg = aggregate_15m([], OPEN, prev_close=42.0)
    assert agg is not None
    assert agg.close == 42.0 and agg.volume == 0.0
    assert aggregate_15m([], OPEN, prev_close=None) is None


# ---------------------------------------------------------------------------
# 技術指標與分級
# ---------------------------------------------------------------------------


def test_compute_technicals_matches_hand_calculation() -> None:
    closes = [float(100 + (i % 5)) for i in range(60)]
    bars = [_bar(i, close=c) for i, c in enumerate(closes)]
    tech = compute_technicals(bars, "AAPL", StreamTier.LARGE_CAP_US)
    assert tech is not None
    assert tech.sma_20 == round(sum(closes[-20:]) / 20, 4)
    assert tech.sma_50 == round(sum(closes[-50:]) / 50, 4)

    k = 2 / 10
    ema = sum(closes[:9]) / 9
    for c in closes[9:]:
        ema = c * k + ema * (1 - k)
    assert tech.ema_9 == round(ema, 4)
    assert tech.rsi_14 is not None and 0.0 <= tech.rsi_14 <= 100.0
    assert tech.bars_count == 60 and tech.ffill_count == 0


def test_compute_technicals_insufficient_bars_returns_none_fields() -> None:
    tech = compute_technicals([_bar(0), _bar(1)], "X", StreamTier.SMALL_MID_CAP_US)
    assert tech is not None
    assert tech.ema_9 is None and tech.sma_20 is None and tech.rsi_14 is None
    assert compute_technicals([], "X", StreamTier.SMALL_MID_CAP_US) is None


def test_volume_spike_ratio_counts_synthetic_zero_volume() -> None:
    bars = [_bar(0, volume=100.0)]
    bars += [
        MinuteBar(ts=OPEN + timedelta(minutes=m), open=100, high=100, low=100,
                  close=100, volume=0.0, is_forward_filled=True)
        for m in range(1, 20)
    ]  # fmt: skip
    bars.append(_bar(20, volume=100.0))
    tech = compute_technicals(bars, "X", StreamTier.SMALL_MID_CAP_US)
    assert tech is not None
    assert tech.volume_spike_ratio == 20.0  # 100 / (100 / 20)


def test_classify_symbol_tier_whitelist_only() -> None:
    wl = {"AAPL", "SPY"}
    assert classify_symbol_tier(" aapl ", wl) == StreamTier.LARGE_CAP_US
    assert classify_symbol_tier("SOL", wl) == StreamTier.SMALL_MID_CAP_US
    assert classify_symbol_tier("LINK", wl) == StreamTier.SMALL_MID_CAP_US
