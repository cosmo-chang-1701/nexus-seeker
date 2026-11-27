"""總經乾跑前向報告的 SPY 回檔檢核（§5.15）：定義、視窗邊界、輸出與 CLI 接線。"""

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

from calibration import __main__ as cli
from calibration import macro_forward_report as mfr
from calibration.macro_forward_report import (
    DRAWDOWN_NO_RECORD,
    DRAWDOWN_PEAK_LOOKBACK,
    DRAWDOWN_PRICE_UNAVAILABLE,
    PriceSeries,
    build_report,
    render_markdown,
    spy_drawdown,
)

START = date(2025, 1, 1)


def _prices(values: list[float], start: date = START) -> PriceSeries:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def _day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def _check(prices: PriceSeries, first: int, last: int) -> dict[str, Any]:
    result = spy_drawdown(prices, _day(first), _day(last))
    assert isinstance(result, dict)
    return result


def _regimes(first: int, last: int) -> list[dict[str, Any]]:
    return [
        {"trading_date": _day(i), "confirmed_state": "CAUTION"}
        for i in range(first, last + 1)
    ]


def test_spy_drawdown_reaches_threshold_exactly_ten_pct() -> None:
    result = _check(_prices([100.0, 95.0, 90.0, 92.0, 89.0]), 0, 4)
    assert result["reached"] is True
    assert result["first_breach_date"] == _day(2)
    assert result["max_drawdown"] == pytest.approx(0.11)
    assert result["max_drawdown_date"] == _day(4)
    assert result["n_days"] == 5
    assert result["symbol"] == "SPY"
    exact = _check(_prices([100.0, 90.0]), 0, 1)
    assert exact["reached"] is True and exact["first_breach_date"] == _day(1)


def test_spy_drawdown_below_threshold() -> None:
    result = _check(_prices([100.0, 91.0, 99.0]), 0, 2)
    assert result["reached"] is False
    assert result["first_breach_date"] is None
    assert result["max_drawdown"] == pytest.approx(0.09)
    assert result["max_drawdown_date"] == _day(1)
    rising = _check(_prices([100.0, 101.0, 102.0]), 0, 2)
    assert rising["max_drawdown"] == 0.0 and rising["reached"] is False


def test_spy_drawdown_peak_lookback_and_window() -> None:
    n = DRAWDOWN_PEAK_LOOKBACK
    # 紀錄首日（索引 n-1）往前第 n 筆（含當日）的高點仍採用
    inside = _prices([100.0] + [90.0] * (n - 1))
    used = _check(inside, n - 1, n - 1)
    assert used["reached"] is True
    assert used["max_drawdown"] == pytest.approx(0.1)
    # 再早一筆就落在視窗外
    outside = _prices([100.0] + [90.0] * n)
    dropped = _check(outside, n, n)
    assert dropped["reached"] is False
    assert dropped["max_drawdown"] == 0.0
    # 紀錄期間前後的低點不計
    around = _prices([100.0, 50.0, 100.0, 95.0, 100.0, 40.0])
    window = _check(around, 2, 4)
    assert window["reached"] is False
    assert window["max_drawdown"] == pytest.approx(0.05)
    assert window["n_days"] == 3


def test_spy_drawdown_unavailable_and_no_record() -> None:
    assert spy_drawdown(None, _day(0), _day(1)) == DRAWDOWN_PRICE_UNAVAILABLE
    assert spy_drawdown([], _day(0), _day(1)) == DRAWDOWN_PRICE_UNAVAILABLE
    assert (
        spy_drawdown(_prices([100.0]), _day(5), _day(6)) == DRAWDOWN_PRICE_UNAVAILABLE
    )
    empty = build_report([], [], {}, [], [], spy_prices=_prices([100.0]))
    assert empty["spy_drawdown"] == DRAWDOWN_NO_RECORD
    assert f"- {DRAWDOWN_NO_RECORD}" in render_markdown(empty)


def test_build_report_backward_compatible() -> None:
    result = build_report(_regimes(0, 1), [], {}, [], [])
    assert result["spy_drawdown"] == DRAWDOWN_PRICE_UNAVAILABLE
    md = render_markdown(result)
    assert "SPY 回檔檢核" in md and f"- {DRAWDOWN_PRICE_UNAVAILABLE}" in md
    del result["spy_drawdown"]  # 舊 result 沒有該鍵
    assert "SPY 回檔檢核" not in render_markdown(result)


def test_markdown_shows_spy_drawdown_section() -> None:
    hit = render_markdown(
        build_report(_regimes(0, 1), [], {}, [], [], spy_prices=_prices([100.0, 88.0]))
    )
    assert "## SPY 回檔檢核（§5.15）" in hit
    assert "前 252 個交易日（含當日）最高收盤的跌幅；≥ 10% 視為一次回檔" in hit
    assert f"- 記錄期間最大回落：12.0%（{_day(1)}）" in hit
    assert f"- ≥ 10% 回檔：已出現（首次 {_day(1)}）" in hit
    assert hit.index("SPY 回檔檢核") < hit.index("## 各指標亮起時段")
    miss = render_markdown(
        build_report(_regimes(0, 1), [], {}, [], [], spy_prices=_prices([100.0, 95.0]))
    )
    assert f"- 記錄期間最大回落：5.0%（{_day(1)}）" in miss
    assert "- ≥ 10% 回檔：尚未出現" in miss


def test_cli_loads_spy_and_passes_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _prices([100.0, 90.0])
    requested: list[str] = []
    captured: dict[str, Any] = {}

    async def _load_prices(symbols: Sequence[str]) -> dict[str, PriceSeries]:
        requested.extend(symbols)
        return {s: spy if s == "SPY" else [] for s in symbols}

    def _load_logs(
        snapshot_db: Optional[Path],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _regimes(0, 1), []

    def _build_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return build_report(*args, **kwargs)

    def _write_report(out_dir: Path, result: dict[str, Any]) -> Path:
        captured["result"] = result
        return out_dir / "report.json"

    monkeypatch.setattr(mfr, "load_prices", _load_prices)
    monkeypatch.setattr(mfr, "load_logs", _load_logs)
    monkeypatch.setattr(mfr, "build_report", _build_report)
    monkeypatch.setattr(mfr, "write_report", _write_report)
    args = cli._parse(
        [
            "macro-forward-report",
            "--out",
            str(tmp_path / "o"),
            "--cache-dir",
            str(tmp_path / "c"),
        ]
    )
    assert asyncio.run(cli._main(args)) == 0
    assert "SPY" in requested
    assert captured["spy_prices"] is spy
    assert captured["result"]["spy_drawdown"]["reached"] is True
