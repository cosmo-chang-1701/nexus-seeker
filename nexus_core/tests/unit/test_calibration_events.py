"""校準事件偵測器：掃描器複製版與原函式一致、代理事件無前視。"""

import itertools

import numpy as np
import pandas as pd
import pytest

from calibration.events import (
    apply_default_thresholds,
    detect_regime_proxy_events,
    detect_scanner_events,
    first_per_session,
    random_control_events,
    scanner_signal,
)
from calibration.features import daily_features, hourly_features, prior_session_lookup
from market_analysis.strategy.indicators import _determine_strategy_signal
from tests.unit.calibration_fixtures import synthetic_daily, synthetic_hourly


def test_scanner_replica_matches_production_on_full_grid() -> None:
    """全格點比對：複製版分支必須與 production `_determine_strategy_signal` 一致。"""
    grid = itertools.product(
        [100.0],
        [20.0, 34.9, 35.0, 42.0, 50.0, 57.0, 65.0, 65.1, 80.0],
        [0.0, 29.9, 30.0, 49.9, 50.0, 90.0],
        [95.0, 100.0, 105.0],
        [-1.0, 0.0, 1.0],
    )
    for price, rsi, hv, sma, macd in grid:
        expected = _determine_strategy_signal(
            {
                "price": price,
                "rsi": rsi,
                "hv_rank": hv,
                "sma20": sma,
                "macd_hist": macd,
            },
            ivr=0.0,
        )[0]
        assert scanner_signal(price, rsi, hv, sma, macd) == expected


def test_prior_session_features_have_no_lookahead() -> None:
    daily = synthetic_daily(n_days=120)
    feat = daily_features(daily)
    lookup = prior_session_lookup(feat)
    d = feat["date"].iloc[50]
    assert lookup.loc[d, "low10"] == pytest.approx(feat["low10"].iloc[49])
    assert lookup.loc[d, "atr14"] == pytest.approx(feat["atr14"].iloc[49])


def test_scanner_events_enter_next_open_and_start_runs_only() -> None:
    daily = synthetic_daily()
    feat = daily_features(daily)
    events = detect_scanner_events("AAA", feat, vix_daily=None)
    assert not events.empty
    assert set(events["side"]) <= {"LONG", "SHORT"}
    for ev in events.head(20).itertuples():
        pos = feat.index.get_loc(ev.signal_ts)
        assert ev.entry_price == pytest.approx(feat["open"].iloc[pos + 1])
    # 連續訊號只取第一天：同類型事件不會出現在相鄰交易日
    for etype, grp in events.groupby("event_type"):
        positions = sorted(feat.index.get_indexer(grp["signal_ts"]))
        assert all(b - a > 1 for a, b in zip(positions, positions[1:]))


def test_regime_v_proxy_triggers_on_constructed_breakdown() -> None:
    daily = synthetic_daily(n_days=400, drift=0.0)
    hourly = synthetic_hourly(daily, n_days=80)
    last_day = hourly.index[-7:]
    # 最後一天第 4 根：放量實體陰線，收盤遠低於前 60 日低點以下 2×ATR 的距離之上
    dfeat = daily_features(daily)
    prev = prior_session_lookup(dfeat).iloc[-1]
    target_close = float(prev["low10"]) * 0.97
    ts = last_day[3]
    hourly.loc[ts, ["Open", "High", "Low", "Close"]] = [
        target_close * 1.02,
        target_close * 1.025,
        target_close * 0.995,
        target_close,
    ]
    hourly.loc[ts, "Volume"] = 5_000_000.0
    hfeat = hourly_features(hourly, dfeat)
    # 強制 RSI 條件與次級節點空間成立，只驗證組合邏輯
    hfeat.loc[ts, "rsi"] = 30.0
    hfeat.loc[ts, "low60_prev"] = target_close - 3.0 * float(
        hfeat.loc[ts, "atr14_prev"]
    )
    events = detect_regime_proxy_events("AAA", hfeat, vix_daily=None)
    v = events[events["event_type"] == "REGIME_V_PROXY"]
    assert ts in set(v["signal_ts"])
    assert (v["side"] == "SHORT").all()


def test_default_thresholds_and_session_dedup() -> None:
    base = {
        "symbol": "AAA",
        "side": "SHORT",
        "entry_price": 1.0,
        "atr_1d": 1.0,
        "vix_prev": np.nan,
        "atr_15m_equiv": 1.0,
        "next_node_space_atr": 3.0,
        "room_atr": 1.0,
        "bar_interval": "1h",
        "event_type": "REGIME_V_PROXY",
    }
    events = pd.DataFrame(
        [
            dict(
                base,
                signal_ts=pd.Timestamp("2026-03-02 15:30", tz="UTC"),
                date="2026-03-02",
                rsi=50.0,
            ),
            dict(
                base,
                signal_ts=pd.Timestamp("2026-03-02 16:30", tz="UTC"),
                date="2026-03-02",
                rsi=40.0,
            ),
            dict(
                base,
                signal_ts=pd.Timestamp("2026-03-02 17:30", tz="UTC"),
                date="2026-03-02",
                rsi=38.0,
            ),
        ]
    )
    kept = apply_default_thresholds(events)
    assert len(kept) == 1
    assert kept.iloc[0]["rsi"] == 40.0
    assert len(first_per_session(events)) == 1


def test_random_control_is_deterministic() -> None:
    daily = synthetic_daily(n_days=400)
    hfeat = hourly_features(synthetic_hourly(daily, n_days=60), daily_features(daily))
    matched = pd.DataFrame({"side": ["SHORT", "SHORT", "LONG"]})
    a = random_control_events("AAA", hfeat, matched, None, seed=7)
    b = random_control_events("AAA", hfeat, matched, None, seed=7)
    assert list(a["signal_ts"]) == list(b["signal_ts"])
    assert sorted(a["side"]) == ["LONG", "SHORT", "SHORT"]
