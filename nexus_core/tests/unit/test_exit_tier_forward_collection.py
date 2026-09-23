"""
tests/unit/test_exit_tier_forward_collection.py

出場分層前向蒐集 (handoff §5.4 SL 分層檢討的資料來源)。

不變式：
  1. 只在 evaluation_source() 脈絡內記錄；記錄器永不影響交易路徑。
  2. 每個分層 × 部位方向各自一個 evaluator（去重鍵不含 user_id）。
  3. direction 是「訊號押注方向」：平倉類分層與部位相反、抬停損類與部位相同。
  4. 記錄在顧問模式轉換之前——被顧問模式丟棄的分層也要記錄，且 advisory=True。
  5. 加上記錄點後，指令輸出與未記錄時逐位元相同。
"""

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from calibration.config import CalibrationConfig
from calibration.forward_log import build_exit_tier_breakdown, build_forward_report
from calibration.report import render_markdown
from market_analysis import evaluation_recorder as rec
from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.outcome_labeling import TOUCH_DOWN, TOUCH_UP


def _metrics(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "spot_price": 100.0,
        "call_wall": 110.0,
        "put_wall": 95.0,
        "net_gex": -1.0,
        "session_vwap": 101.0,
        "atr_15m": 0.8,
        "avg_cost": 105.0,
        "ratchet_stop": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 記錄器
class TestRecordExitSignal:
    def test_no_source_means_no_record(self) -> None:
        rec.record_exit_signal("AAA", "SL_STRUCTURAL", "LONG", _metrics())
        assert rec.pending_count() == 0

    def test_long_liquidation_tier_bets_short(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_exit_signal(
                "aaa", "SL_STRUCTURAL", "LONG", _metrics(), stop_level=99.5
            )
        row = rec._BUFFER[0]
        assert row["evaluator"] == "EXIT_SL_STRUCTURAL"
        assert row["direction"] == "SHORT"
        assert row["sub_mode"] == "SL_STRUCTURAL"
        assert row["decision"] == 1
        assert row["symbol"] == "AAA"
        assert row["spot"] == 100.0
        # 停損價不得進 stop_price 欄 (labeler 的 plan_outcome 會誤讀)
        assert row.get("stop_price") is None
        features = json.loads(row["features_json"])
        assert features["stop_level"] == 99.5
        assert features["advisory"] is False
        assert features["position_side"] == "LONG"

    @pytest.mark.parametrize("tier", ["SL_TRAILING_BREAKEVEN", "TP1_TREND_EXEMPT"])
    def test_hold_tiers_bet_with_position(self, tier: str) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_exit_signal("AAA", tier, "LONG", _metrics())
        assert rec._BUFFER[0]["direction"] == "LONG"

    def test_short_position_gets_own_evaluator_and_mirrored_direction(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_exit_signal("AAA", "SL_STRUCTURAL", "SHORT", _metrics())
        row = rec._BUFFER[0]
        assert row["evaluator"] == "EXIT_SL_STRUCTURAL_SHORT"
        assert row["direction"] == "LONG"

    def test_distinct_tiers_on_same_bar_do_not_collide(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_exit_signal("AAA", "SL_STRUCTURAL", "LONG", _metrics())
            rec.record_exit_signal("AAA", "SL_TRAILING_BREAKEVEN", "LONG", _metrics())
        keys = {
            (r["symbol"], r["evaluator"], r["source"], r["bar_ts"]) for r in rec._BUFFER
        }
        assert len(keys) == 2

    def test_bad_metrics_never_raise(self) -> None:
        with rec.evaluation_source("PORTFOLIO_MONITOR"):
            rec.record_exit_signal("AAA", "SL_STRUCTURAL", "LONG", None)  # type: ignore[arg-type]
        assert rec.pending_count() == 0


# ---------------------------------------------------------------- 端到端掛點
def _asset(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "symbol": "NVDA",
        "asset_id": 42,
        "asset_class": "SATELLITE",
        "quantity": 100.0,
        "current_value": 10_000.0,
        "avg_cost": 90.0,
        "spot_price": 100.0,
        "call_wall": 100.0,
        "previous_call_wall": 0.0,
        "put_wall": 95.0,
        "session_vwap": 97.0,
        "gex_profile_data": {"net_gex": 500_000.0},
        "dynamic_strategy_state": {},
    }
    base.update(overrides)
    return base


async def _run(portfolio: List[Dict[str, Any]], source: bool = True) -> List[Any]:
    engine = DynamicRolloverEngine()
    with patch(
        "market_analysis.dynamic_rollover.get_full_user_context",
        return_value=MagicMock(risk_appetite="DEFENSIVE"),
    ), patch(
        "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "market_analysis.atr_utils.fetch_high_60d",
        new_callable=AsyncMock,
        return_value=0.0,
    ):
        if source:
            with rec.evaluation_source("PORTFOLIO_MONITOR"):
                return await engine.check_satellite_rebalancing(1, portfolio, 100_000.0)
        return await engine.check_satellite_rebalancing(1, portfolio, 100_000.0)


_TREND_EXEMPT = dict(
    spot_price=99.6,
    call_wall=100.0,
    previous_call_wall=98.0,
    put_wall=99.3,
    avg_cost=90.0,
)


@pytest.mark.asyncio
async def test_tp1_tier_is_recorded() -> None:
    await _run([_asset(spot_price=100.0, call_wall=100.0)])
    evaluators = [r["evaluator"] for r in rec._BUFFER]
    assert evaluators == ["EXIT_TP1"]
    assert rec._BUFFER[0]["direction"] == "SHORT"


@pytest.mark.asyncio
async def test_advisory_dropped_tier_is_still_recorded() -> None:
    """顧問模式會丟棄 TP1 趨勢豁免的 HOLD，但原始訊號仍須記錄且標記 advisory。"""
    out = await _run([_asset(**_TREND_EXEMPT, advisory_only=True)])
    assert out == []
    assert [r["evaluator"] for r in rec._BUFFER] == ["EXIT_TP1_TREND_EXEMPT"]
    assert json.loads(rec._BUFFER[0]["features_json"])["advisory"] is True


@pytest.mark.asyncio
async def test_no_tier_means_no_record() -> None:
    """未觸發任何分層 (灰階 HOLD) 不記錄。"""
    await _run([_asset(spot_price=96.0, call_wall=110.0, put_wall=90.0)])
    assert all(not r["evaluator"].startswith("EXIT_") for r in rec._BUFFER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        dict(spot_price=100.0, call_wall=100.0),
        _TREND_EXEMPT,
    ],
)
async def test_recording_does_not_change_instructions(
    overrides: Dict[str, Any],
) -> None:
    with_source = await _run([_asset(**overrides)], source=True)
    without_source = await _run([_asset(**overrides)], source=False)
    assert with_source == without_source


# ---------------------------------------------------------------- 前向報告
def _labeled(evaluator: str, direction: str, touch: int, advisory: bool) -> dict:
    return {
        "evaluator": evaluator,
        "decision": 1,
        "direction": direction,
        "first_touch_k15": touch,
        "bar_ts": "2026-09-22T10:00:00-04:00",
        "features_json": json.dumps({"advisory": advisory}),
        "fwd_ret_5d": 0.01,
        "spot": 100.0,
        "next_negative_node": None,
        "atr_1d": 2.0,
        "resistance_wall": None,
        "atr_15m": 0.5,
        "put_wall": None,
        "conditions_mask": None,
        "conditions_evaluated_mask": None,
    }


def test_exit_breakdown_splits_advisory_and_counts_washout() -> None:
    rows = pd.DataFrame(
        [
            # 多頭 SL 平倉 (direction=SHORT)：先觸下緣＝出場正確、先觸上緣＝洗盤
            _labeled("EXIT_SL_STRUCTURAL", "SHORT", TOUCH_DOWN, False),
            _labeled("EXIT_SL_STRUCTURAL", "SHORT", TOUCH_UP, False),
            _labeled("EXIT_SL_STRUCTURAL", "SHORT", TOUCH_UP, False),
            _labeled("EXIT_SL_STRUCTURAL", "SHORT", TOUCH_UP, True),
            _labeled("ENTRY_RIGHT", "LONG", TOUCH_UP, False),
        ]
    )
    report = build_forward_report(rows, CalibrationConfig())
    tiers = {(e["evaluator"], e["advisory"]): e for e in report["exit_tiers"]}
    assert set(tiers) == {
        ("EXIT_SL_STRUCTURAL", False),
        ("EXIT_SL_STRUCTURAL", True),
    }
    cmd = tiers[("EXIT_SL_STRUCTURAL", False)]
    assert cmd["n"] == 3
    assert cmd["correct_rate"] == pytest.approx(1 / 3)
    assert cmd["washout_rate"] == pytest.approx(2 / 3)
    assert cmd["status"].startswith("資料累積中")
    assert tiers[("EXIT_SL_STRUCTURAL", True)]["washout_rate"] == 1.0


def test_exit_breakdown_empty_without_exit_rows() -> None:
    assert build_exit_tier_breakdown(pd.DataFrame()) == []


def test_markdown_renders_exit_section() -> None:
    forward = {
        "status": "OK",
        "total_labeled": 1,
        "sections": [],
        "exit_tiers": [
            {
                "evaluator": "EXIT_SL_REGIME_FLIP",
                "advisory": False,
                "n": 1,
                "correct_rate": 0.0,
                "washout_rate": 1.0,
                "timeout_rate": 0.0,
                "median_fwd_ret_5d": None,
                "status": "資料累積中 (1/100)",
            }
        ],
    }
    results = {
        "generated_at": "2026-09-23T00:00:00Z",
        "config": {"seed": 7},
        "parameters": [],
        "event_tables": [],
        "forward_collection": forward,
    }
    text = render_markdown(results)
    assert "出場分層洗盤率" in text
    assert "`EXIT_SL_REGIME_FLIP`" in text
