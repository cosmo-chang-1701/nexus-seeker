"""報告路徑限制、決定性、離線模式與無網路保證。"""

import ast
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from calibration import pipeline, report
from calibration.config import CalibrationConfig
from calibration.data_store import CacheMissError, DataStore
from tests.unit.calibration_fixtures import FakeFetcher

_CALIBRATION_ROOT = Path(__file__).resolve().parents[2] / "calibration"


def test_output_path_must_stay_under_out_dir(tmp_path: Path) -> None:
    ok = report.resolve_output_dir(tmp_path, "20260101T000000Z")
    assert ok.parent == (tmp_path / "calibration").resolve()
    with pytest.raises(report.UnsafeOutputPathError):
        report.resolve_output_dir(tmp_path, "../../etc")


def test_calibration_package_never_writes_python_files_or_db() -> None:
    """只有 report.py 與 data_store.py 允許寫檔；整個套件不得出現 DB 寫入入口。"""
    allowed_writers = {"report.py", "data_store.py"}
    for path in _CALIBRATION_ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "write_text",
                "to_csv",
                "write_bytes",
            ):
                assert path.name in allowed_writers, f"{path.name} 寫檔"
            if isinstance(node, ast.Name) and node.id in (
                "execute_write",
                "execute_write_async",
                "execute_write_many",
                "execute_write_many_async",
            ):
                raise AssertionError(f"{path.name} 使用 DB 寫入入口")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("校準離線流程不得發動網路連線")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.mark.asyncio
async def test_offline_run_is_deterministic_and_network_free(
    tmp_path: Path, no_network: None
) -> None:
    symbols = ["AAA", "BBB"]
    cfg = CalibrationConfig(
        cache_dir=tmp_path / "cache",
        out_dir=tmp_path / "out",
        n_boot=100,
        min_events=20,
        min_dates=10,
    )
    store = DataStore(cfg.cache_dir)
    fetcher = FakeFetcher(symbols)
    await pipeline.fetch_all(cfg, store, fetcher, symbols)
    assert ("^VIX", "max", "1d") in fetcher.calls
    assert store.has("1h", "AAA") and store.has("1d", "^VIX")

    offline = CalibrationConfig(**{**cfg.__dict__, "offline": True})
    outputs = []
    for stamp in ("run1", "run2"):
        labeled, params, tables = pipeline.run_event_study(offline, store, symbols)
        results = report.build_results(
            offline, params, tables, data_coverage={}, generated_at="fixed"
        )
        target = report.write_report(
            offline, results, {"symbols": symbols}, stamp=stamp
        )
        outputs.append(
            json.loads((target / "results.json").read_text(encoding="utf-8"))
        )
        assert (
            (target / "report.md")
            .read_text(encoding="utf-8")
            .startswith("# 回測校準報告")
        )
    for out in outputs:
        out.pop("git_sha", None)
    assert outputs[0] == outputs[1]
    assert not labeled.empty
    names = {p["name"] for p in outputs[0]["parameters"]}
    assert "_REGIME_V_RSI_MAX" in names and "short_vix_multiplier.extreme" in names
    extreme = next(
        p
        for p in outputs[0]["parameters"]
        if p["name"] == "short_vix_multiplier.extreme"
    )
    assert extreme["proposed"] == 0.0


def test_offline_cache_miss_raises(tmp_path: Path) -> None:
    cfg = CalibrationConfig(cache_dir=tmp_path, out_dir=tmp_path, offline=True)
    with pytest.raises(CacheMissError):
        pipeline.run_event_study(cfg, DataStore(tmp_path), ["ZZZ"])


def test_store_localizes_naive_index_as_us_eastern(tmp_path: Path) -> None:
    import pandas as pd

    store = DataStore(tmp_path)
    df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex(["2026-03-02 00:00"]),  # 日線：naive 美東午夜
    )
    store.save("1d", "AAA", df)
    loaded = store.require("1d", "AAA")
    assert loaded.index[0] == pd.Timestamp("2026-03-02 05:00", tz="UTC")
    from calibration.features import et_dates

    assert str(et_dates(loaded.index)[0]) == "2026-03-02"


@pytest.mark.asyncio
async def test_hourly_fetch_falls_back_to_two_years(tmp_path: Path) -> None:
    """上市日早於 730 天的標的，yfinance 會拒絕 "729d" 的 1h 請求；須退回 "2y"。"""
    cfg = CalibrationConfig(cache_dir=tmp_path, out_dir=tmp_path)
    store = DataStore(tmp_path)
    fetcher = FakeFetcher(["AAA"], fail_periods={"729d"})
    await pipeline.fetch_all(cfg, store, fetcher, ["AAA"])
    assert ("AAA", "2y", "1h") in fetcher.calls
    assert store.has("1h", "AAA")
