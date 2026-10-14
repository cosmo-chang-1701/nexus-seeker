"""PR-2：REGIME_CLASSIFIER／ENTRY_SQUEEZE 前向紀錄補上 Skew 欄位（純記錄，不改判定）。"""

import json
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis import evaluation_recorder as rec
from market_analysis.dynamic_rollover.regime_classifier import classify_dynamic_regime

_RADAR = {"skew_percentile": 91.5, "skew_percentile_source": "CANONICAL"}


@pytest.fixture(autouse=True)
def _clean_buffer() -> Iterator[None]:
    rec._BUFFER.clear()
    yield
    rec._BUFFER.clear()


def _features(row: dict[str, Any]) -> dict[str, Any]:
    feats = row["features_json"]
    parsed: dict[str, Any] = (
        json.loads(feats) if isinstance(feats, str) else dict(feats)
    )
    return parsed


# ---------------------------------------------------------------- 分類器
class TestRegimeSkewRecording:
    def test_radar_writes_both_skew_keys(self) -> None:
        with rec.evaluation_source("WATCHLIST_ADVISOR"):
            rec.record_regime_classification(
                "AAA",
                100.0,
                "REGIME_II_CHAOS_STANDASIDE",
                "x",
                {},
                candidate_radar=_RADAR,
            )
        feats = _features(rec._BUFFER[0])
        assert feats["skew_percentile"] == 91.5
        assert feats["skew_percentile_source"] == "CANONICAL"

    def test_no_radar_has_no_skew_keys(self) -> None:
        with rec.evaluation_source("WATCHLIST_ADVISOR"):
            rec.record_regime_classification(
                "AAA", 100.0, "REGIME_II_CHAOS_STANDASIDE", "x", {}
            )
        feats = _features(rec._BUFFER[0])
        assert "skew_percentile" not in feats
        assert "skew_percentile_source" not in feats

    @pytest.mark.asyncio
    async def test_classify_passes_radar_to_recorder_only(self) -> None:
        with rec.evaluation_source("WATCHLIST_ADVISOR"):
            await classify_dynamic_regime("AAA", 0.0, {}, candidate_radar=_RADAR)
        feats = _features(rec._BUFFER[0])
        assert feats["skew_percentile"] == 91.5
        assert feats["skew_percentile_source"] == "CANONICAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "gex",
        [{}, {"call_wall": 150.0, "put_wall": 90.0, "gex_profile": {}}],
    )
    async def test_classification_unchanged_with_or_without_radar(
        self, gex: dict[str, Any]
    ) -> None:
        """行為不變：有無 candidate_radar，(regime, reason) 完全相同，
        且 radar 不會被傳進 `_classify_dynamic_regime_impl`。"""
        impl = AsyncMock(
            wraps=__import__(
                "market_analysis.dynamic_rollover.regime_classifier",
                fromlist=["x"],
            )._classify_dynamic_regime_impl
        )
        with patch(
            "market_analysis.dynamic_rollover.regime_classifier."
            "_classify_dynamic_regime_impl",
            impl,
        ):
            r1, reason1, _ = await classify_dynamic_regime("AAA", 100.0, gex)
            r2, reason2, _ = await classify_dynamic_regime(
                "AAA", 100.0, gex, candidate_radar=_RADAR
            )
        assert (r1, reason1) == (r2, reason2)
        for call in impl.await_args_list:
            assert "candidate_radar" not in call.kwargs
            assert _RADAR not in call.args


# ---------------------------------------------------------------- 擠壓紀錄
class TestSqueezeSkewRecording:
    @staticmethod
    def _record(**kw: Any) -> dict[str, Any]:
        result = SimpleNamespace(
            status="ENTRY",
            tier=2,
            size_pct=1.0,
            squeeze_count=2,
            triggers=[],
            stop=9.0,
            reason="r",
        )
        with rec.evaluation_source("WATCHLIST_ADVISOR"):
            rec.record_squeeze_entry("AAA", 10.0, result, {}, **kw)
        return rec._BUFFER[0]

    def test_with_radar(self) -> None:
        row = self._record(candidate_radar=_RADAR)
        feats = _features(row)
        assert feats["skew_percentile"] == 91.5
        assert feats["skew_percentile_source"] == "CANONICAL"
        assert row["decision"] == 1 and row["conditions_mask"] == 2

    @pytest.mark.parametrize("radar", [None, {}])
    def test_without_radar_matches_legacy(self, radar: Any) -> None:
        row = self._record(candidate_radar=radar)
        feats = _features(row)
        assert "skew_percentile" not in feats
        assert "skew_percentile_source" not in feats
        assert row["decision"] == 1 and row["conditions_mask"] == 2


# ---------------------------------------------------------------- entry_advisor
class TestEntryAdvisorPassesRadar:
    @pytest.mark.asyncio
    async def test_squeeze_long_forwards_radar_to_recorder(self) -> None:
        from market_analysis.intraday_pipeline import entry_advisor
        from market_analysis.squeeze_entry import SqueezeEvaluation
        from market_analysis.squeeze_entry.rules import SqueezeEntryResult

        result = SqueezeEntryResult("WATCH", None, 0.0, None, "r")
        evaluate = AsyncMock(return_value=SqueezeEvaluation(result, {}, None))
        with (
            patch(
                "market_analysis.squeeze_entry.vetoes.resolve_long_entry_vetoes",
                AsyncMock(return_value=([], None)),
            ),
            patch("market_analysis.squeeze_entry.evaluate_symbol", evaluate),
            patch.object(rec, "record_squeeze_entry") as recorder,
        ):
            await entry_advisor._evaluate_squeeze_long(
                "DYNAMIC", "AAA", 10.0, None, None, radar=_RADAR
            )
            await entry_advisor._evaluate_squeeze_long("DYNAMIC", "AAA", 10.0)
        assert recorder.call_args_list[0].kwargs["candidate_radar"] == _RADAR
        assert recorder.call_args_list[1].kwargs["candidate_radar"] is None

    @pytest.mark.asyncio
    async def test_evaluate_uncached_passes_radar_on_both_squeeze_paths(self) -> None:
        from market_analysis.dynamic_rollover.models import (
            DynamicRegime,
            TradingStrategyMode,
        )
        from market_analysis.intraday_pipeline import entry_advisor

        sentinel = SimpleNamespace(passed=False)
        squeeze = AsyncMock(return_value=sentinel)
        classify = AsyncMock(
            return_value=(
                DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
                "休眠",
                SimpleNamespace(df_15m=None),
            )
        )
        radar = dict(_RADAR)
        with (
            patch.object(entry_advisor, "_evaluate_squeeze_long", squeeze),
            patch(
                "market_analysis.dynamic_rollover.regime_classifier."
                "classify_dynamic_regime",
                classify,
            ),
        ):
            await entry_advisor._evaluate_uncached(
                TradingStrategyMode.DYNAMIC.value, "AAA", radar, 10.0, None
            )
            await entry_advisor._evaluate_uncached(
                TradingStrategyMode.RIGHT_SIDE.value, "AAA", radar, 10.0, None
            )
        assert classify.await_args is not None
        assert classify.await_args.kwargs["candidate_radar"] is radar
        for call in squeeze.await_args_list:
            assert call.kwargs["radar"] is radar


# ---------------------------------------------------------------- 報表
def test_threshold_studies_split_skew_by_evaluator() -> None:
    from calibration.forward_log import build_threshold_studies

    def feat(pct: float, src: str) -> str:
        return json.dumps({"skew_percentile": pct, "skew_percentile_source": src})

    entries = pd.DataFrame(
        [
            {
                "evaluator": "REGIME_CLASSIFIER",
                "features_json": feat(99, "CANONICAL"),
                "win": 1,
                "outcome": 1,
            },
            {
                "evaluator": "ENTRY_SQUEEZE",
                "features_json": feat(50, "INTRADAY_FALLBACK"),
                "win": 0,
                "outcome": -1,
            },
            {
                "evaluator": "ENTRY_SQUEEZE",
                "features_json": feat(50, "INTRADAY_FALLBACK"),
                "win": 1,
                "outcome": 1,
            },
            {
                "evaluator": "ENTRY_RIGHT",
                "features_json": feat(10, "CANONICAL"),
                "win": 0,
                "outcome": 0,
            },
        ]
    )
    studies = build_threshold_studies(entries)
    by_ev = studies["skew_percentile_by_evaluator"]
    assert by_ev["REGIME_CLASSIFIER"]["CANONICAL"][0]["n"] == 1
    assert by_ev["ENTRY_SQUEEZE"]["INTRADAY_FALLBACK"][0]["n"] == 2
    assert by_ev["其他"]["CANONICAL"][0]["bucket"] == "<15"
    # 既有「依母體來源」總表維持不變
    assert set(studies["skew_percentile"]) == {"CANONICAL", "INTRADAY_FALLBACK"}


def test_threshold_studies_without_evaluator_column_does_not_fail() -> None:
    from calibration.forward_log import build_threshold_studies

    entries = pd.DataFrame(
        [
            {
                "features_json": json.dumps({"skew_percentile": 50.0}),
                "win": 1,
                "outcome": 1,
            }
        ]
    )
    studies = build_threshold_studies(entries)
    assert isinstance(studies["skew_percentile_by_evaluator"], str)
