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
                "date": "2026-10-05",
                "win": 0,
                "outcome": -1,
            },
            {
                "evaluator": "ENTRY_SQUEEZE",
                "features_json": feat(50, "INTRADAY_FALLBACK"),
                "date": "2026-10-06",
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
    # 交易日數欄沿用 #76 的 _bucket_rows（有效樣本約等於交易日數）
    assert by_ev["ENTRY_SQUEEZE"]["INTRADAY_FALLBACK"][0]["n_dates"] == 2
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


def _dedupe_entries(with_keys: bool = True) -> pd.DataFrame:
    feat = json.dumps({"skew_percentile": 50.0, "skew_percentile_source": "CANONICAL"})
    rows = []
    for ev, win, outcome in (
        ("ENTRY_SQUEEZE", 0, -1),
        ("REGIME_CLASSIFIER", 1, 1),
    ):
        row: dict[str, Any] = {
            "evaluator": ev,
            "features_json": feat,
            "win": win,
            "outcome": outcome,
            "date": "2026-10-05",
        }
        if with_keys:
            row.update(
                symbol="AAPL", bar_ts="2026-10-05T10:00", source="WATCHLIST_ADVISOR"
            )
        rows.append(row)
    return pd.DataFrame(rows)


def test_threshold_studies_dedupe_pooled_skew_counts_bar_once() -> None:
    from calibration.forward_log import build_threshold_studies

    studies = build_threshold_studies(_dedupe_entries())
    row = studies["skew_percentile"]["CANONICAL"][0]
    assert row["n"] == 1
    # 保留 REGIME_CLASSIFIER 那一列 (win=1, outcome=1)，而非先出現的 SQUEEZE 列
    assert row["win_rate"] == 1.0
    assert row["adverse_rate"] == 0.0
    by_ev = studies["skew_percentile_by_evaluator"]
    assert by_ev["REGIME_CLASSIFIER"]["CANONICAL"][0]["n"] == 1
    assert by_ev["ENTRY_SQUEEZE"]["CANONICAL"][0]["n"] == 1


def test_threshold_studies_dedupe_without_key_columns_keeps_all_rows() -> None:
    from calibration.forward_log import build_threshold_studies

    studies = build_threshold_studies(_dedupe_entries(with_keys=False))
    assert studies["skew_percentile"]["CANONICAL"][0]["n"] == 2


def test_threshold_studies_dedupe_multi_bar_and_distinct_log_source() -> None:
    from calibration.forward_log import build_threshold_studies

    feat = json.dumps({"skew_percentile": 50.0, "skew_percentile_source": "CANONICAL"})

    def _row(
        evaluator: str, bar_ts: str, source: str, win: int, outcome: int
    ) -> dict[str, Any]:
        return {
            "evaluator": evaluator,
            "symbol": "AAPL",
            "bar_ts": bar_ts,
            "source": source,
            "features_json": feat,
            "win": win,
            "outcome": outcome,
            "date": "2026-10-05",
        }

    rows = [
        # 兩根不同 K 棒，各被 REGIME 與 SQUEEZE 記錄，每根只計 1
        _row("REGIME_CLASSIFIER", "2026-10-05T10:00", "WATCHLIST_ADVISOR", 1, 1),
        _row("ENTRY_SQUEEZE", "2026-10-05T10:00", "WATCHLIST_ADVISOR", 0, -1),
        _row("REGIME_CLASSIFIER", "2026-10-05T11:00", "WATCHLIST_ADVISOR", 1, 1),
        _row("ENTRY_SQUEEZE", "2026-10-05T11:00", "WATCHLIST_ADVISOR", 0, -1),
        # 同 symbol／bar_ts 但 log 來源不同 (SCAN)，視為另一筆紀錄，另算 1
        _row("ENTRY_SQUEEZE", "2026-10-05T10:00", "SCAN", 0, -1),
    ]
    studies = build_threshold_studies(pd.DataFrame(rows))
    bins = studies["skew_percentile"]["CANONICAL"]
    assert len(bins) == 1  # 三者 skew 皆為 50，同一區間
    row = bins[0]
    assert row["n"] == 3
    # 保留的是兩根 K 棒的 REGIME 列 (win=1) 與 SCAN 列 (win=0)：win 2/3、adverse 1/3
    assert row["win_rate"] == pytest.approx(2 / 3)
    assert row["adverse_rate"] == pytest.approx(1 / 3)


def test_threshold_studies_dedupe_without_evaluator_keeps_first_row() -> None:
    from calibration.forward_log import build_threshold_studies

    feat = json.dumps({"skew_percentile": 50.0, "skew_percentile_source": "CANONICAL"})
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "bar_ts": "2026-10-05T10:00",
        "source": "WATCHLIST_ADVISOR",
        "features_json": feat,
        "date": "2026-10-05",
    }
    # 無 evaluator 欄：同一根 K 棒兩列，應保留原順序第一列 (win=0)
    rows = [
        {**base, "win": 0, "outcome": -1},
        {**base, "win": 1, "outcome": 1},
    ]
    studies = build_threshold_studies(pd.DataFrame(rows))
    row = studies["skew_percentile"]["CANONICAL"][0]
    assert row["n"] == 1
    assert row["win_rate"] == 0.0
    assert row["adverse_rate"] == 1.0


def test_threshold_studies_dedupe_regime_nan_skew_keeps_squeeze_row() -> None:
    from calibration.forward_log import build_threshold_studies

    base: dict[str, Any] = {
        "symbol": "AAPL",
        "bar_ts": "2026-10-05T10:00",
        "source": "WATCHLIST_ADVISOR",
        "date": "2026-10-05",
    }
    # REGIME 列無 skew (會被 dropna 剔除)，SQUEEZE 列有 skew；去重須在 dropna 之後
    rows = [
        {
            **base,
            "evaluator": "REGIME_CLASSIFIER",
            "features_json": json.dumps({}),
            "win": 0,
            "outcome": 1,
        },
        {
            **base,
            "evaluator": "ENTRY_SQUEEZE",
            "features_json": json.dumps(
                {"skew_percentile": 50.0, "skew_percentile_source": "CANONICAL"}
            ),
            "win": 1,
            "outcome": -1,
        },
    ]
    studies = build_threshold_studies(pd.DataFrame(rows))
    row = studies["skew_percentile"]["CANONICAL"][0]
    assert row["n"] == 1
    assert row["win_rate"] == 1.0
    assert row["adverse_rate"] == 1.0


# ---------------------------------------------------------------- /x (SYMBOL_VIEW)
@pytest.mark.asyncio
async def test_hub_data_skew_source_comes_from_same_result_as_percentile() -> None:
    from unittest.mock import MagicMock

    from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin

    class _DD(SymbolDeepDiveMixin):
        def __init__(self) -> None:
            self.bot = MagicMock()

    def raw(skew: dict[str, Any]) -> dict[str, Any]:
        return {
            "df_spy": pd.DataFrame(), "macro_raw": {"vix": 18.0},
            "quote": {"c": 115.0, "dp": 0.5}, "skew_data": skew, "pcr_data": {},
            "uoa_data": [], "sto_physical_cap_strikes": [], "max_pain_data": {},
            "iv_metrics": {}, "reddit_text": "", "poly_markets": [], "ddp_report": {},
            "df_hist_1d": pd.DataFrame(), "month_max_pains": [],
            "gex_profile_data": None, "volume_profile": None, "atr_15m": 0.0,
            "session_vwap": 0.0, "bar_15m": None, "catalysts": [],
        }  # fmt: skip

    results: list[dict[str, Any]] = []
    skews: list[dict[str, Any]] = [
        {"skew": 0.1, "skew_percentile": 77.0, "skew_percentile_source": "CANONICAL"},
        {},
    ]
    for skew in skews:
        with (
            patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
            patch("market_math.analyze_symbol", new_callable=AsyncMock) as m,
            patch(
                "cogs.unified_terminal.utils.find_matching_polymarket_odds",
                new_callable=AsyncMock, return_value=[],
            ),
            patch(
                "cogs.unified_terminal.utils.calculate_polymarket_weighted_odds",
                new_callable=AsyncMock, return_value=None,
            ),
            patch("database.get_full_user_context", return_value=MagicMock()),
        ):  # fmt: skip
            m.return_value = {"symbol": "TEST", "price": 115.0}
            results.append(await _DD()._process_symbol_hub_data("TEST", 1, raw(skew)))
    assert results[0]["skew_percentile"] == 77.0
    assert results[0]["skew_percentile_source"] == "CANONICAL"
    assert results[1]["skew_percentile"] is None
    assert results[1]["skew_percentile_source"] is None


@pytest.mark.asyncio
async def test_symbol_view_radar_reaches_recorder() -> None:
    """SYMBOL_VIEW 路徑：base_data 經 _evaluate_squeeze_long 到達 recorder。"""
    from market_analysis.intraday_pipeline import entry_advisor
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    base_data = {"symbol": "AAPL", **_RADAR}
    result = SqueezeEntryResult("WATCH", None, 0.0, None, "r")
    with (
        patch(
            "market_analysis.squeeze_entry.vetoes.resolve_long_entry_vetoes",
            AsyncMock(return_value=([], None)),
        ),
        patch(
            "market_analysis.squeeze_entry.evaluate_symbol",
            AsyncMock(return_value=SqueezeEvaluation(result, {}, None)),
        ),
        rec.evaluation_source("SYMBOL_VIEW"),
    ):
        await entry_advisor._evaluate_squeeze_long(
            "RIGHT_SIDE", "AAPL", 10.0, radar=base_data
        )
    row = rec._BUFFER[0]
    assert row["source"] == "SYMBOL_VIEW"
    assert _features(row)["skew_percentile"] == 91.5
    assert _features(row)["skew_percentile_source"] == "CANONICAL"
