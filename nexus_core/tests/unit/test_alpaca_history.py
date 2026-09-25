"""calibration/alpaca_history.py：常規時段 1h 聚合、成交量分割調整、區間取代。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from calibration.alpaca_history import (
    _credentials,
    aggregate_rth_hourly,
    build_hourly,
    compare_with_cache,
    replace_range,
)
from calibration.data_store import DataStore


def _bars_30m(
    day: str, n: int = 13, start: str = "09:30", price: float = 100.0
) -> pd.DataFrame:
    idx = pd.date_range(
        f"{day} {start}", periods=n, freq="30min", tz="America/New_York"
    )
    df = pd.DataFrame(
        {
            "o": [price + i for i in range(n)],
            "h": [price + i + 0.5 for i in range(n)],
            "l": [price + i - 0.5 for i in range(n)],
            "c": [price + i + 0.25 for i in range(n)],
            "v": [1000.0 * (i + 1) for i in range(n)],
        },
        index=idx.tz_convert("UTC"),
    )
    return df


def test_aggregates_to_yahoo_style_hourly_buckets() -> None:
    """13 根 30 分 K → 7 根 1h，開始時間 09:30／10:30／…／15:30，最後一根只含 15:30–16:00。"""
    out = aggregate_rth_hourly(_bars_30m("2022-03-01"))
    et = out.index.tz_convert("America/New_York")
    assert list(et.strftime("%H:%M")) == [
        "09:30",
        "10:30",
        "11:30",
        "12:30",
        "13:30",
        "14:30",
        "15:30",
    ]
    first = out.iloc[0]
    assert first["Open"] == 100.0 and first["Close"] == 101.25
    assert first["High"] == 101.5 and first["Low"] == 99.5
    assert first["Volume"] == 1000.0 + 2000.0
    assert out.iloc[-1]["Volume"] == 13000.0


def test_extended_hours_are_dropped() -> None:
    pre = _bars_30m("2022-03-01", n=4, start="07:30")
    rth = _bars_30m("2022-03-01", n=2)
    out = aggregate_rth_hourly(pd.concat([pre, rth]).sort_index())
    assert len(out) == 1
    assert out.iloc[0]["Volume"] == 3000.0


def test_half_day_keeps_partial_last_bucket() -> None:
    out = aggregate_rth_hourly(_bars_30m("2022-11-25", n=7))  # 09:30–13:00
    assert len(out) == 4
    assert out.index.tz_convert("America/New_York")[-1].strftime("%H:%M") == "12:30"


def test_volume_is_split_adjusted_from_raw_times_price_factor() -> None:
    """成交量 = raw 量 × (raw 收盤 / split 收盤)；不採用 Alpaca 調整後的量。"""
    raw = _bars_30m("2022-08-24", n=2, price=900.0)
    split = raw.assign(c=raw["c"] / 3.0)
    adj_all = split.assign(v=raw["v"] * 9.0)  # 模擬 Alpaca 錯誤調整的量
    out = build_hourly({"all": adj_all, "raw": raw, "split": split})
    assert out.iloc[0]["Volume"] == pytest.approx((1000.0 + 2000.0) * 3.0)
    assert out.iloc[0]["Close"] == pytest.approx(split["c"].iloc[-1])


def test_replace_range_keeps_bars_outside_range(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    yahoo = pd.concat(
        [
            aggregate_rth_hourly(_bars_30m("2022-03-01", price=10.0)),
            aggregate_rth_hourly(_bars_30m("2022-03-02", price=10.0)),
            aggregate_rth_hourly(_bars_30m("2022-03-03", price=10.0)),
        ]
    )
    store.save("1h", "XYZ", yahoo)
    alpaca = aggregate_rth_hourly(_bars_30m("2022-03-02", price=50.0))
    total = replace_range(store, "XYZ", alpaca, "2022-03-02", "2022-03-02")
    loaded = store.load("1h", "XYZ")
    assert loaded is not None and total == len(loaded) == 21
    days = loaded.index.tz_convert("America/New_York").strftime("%Y-%m-%d")
    mid = loaded[days == "2022-03-02"]
    assert float(mid["Open"].iloc[0]) == 50.0
    assert float(loaded[days == "2022-03-01"]["Open"].iloc[0]) == 10.0


def test_compare_with_cache_reports_price_and_volume_ratio(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    base = aggregate_rth_hourly(_bars_30m("2022-03-01"))
    store.save("1h", "XYZ", base)
    other = base.assign(Close=base["Close"] * 1.001, Volume=base["Volume"] * 2.0)
    res = compare_with_cache(store, "XYZ", other)
    assert res is not None
    assert res["bars"] == 7
    assert res["close_diff_median_pct"] == pytest.approx(0.1, rel=1e-3)
    assert res["volume_ratio_median"] == pytest.approx(2.0)
    # 逐根量比中位數偏離 1 超過 10% → 不及格
    assert res["passed"] is False


def test_compare_with_cache_passes_identical_volume(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    days = [
        f"2022-03-{d:02d}"
        for d in range(1, 11)
        if pd.Timestamp(f"2022-03-{d:02d}").weekday() < 5
    ]
    base = pd.concat([aggregate_rth_hourly(_bars_30m(d)) for d in days])
    store.save("1h", "XYZ", base)
    res = compare_with_cache(store, "XYZ", base)
    assert res is not None and res["passed"] is True
    assert res["vol_ratio_flip_1.15"] == 0.0


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        _credentials()
