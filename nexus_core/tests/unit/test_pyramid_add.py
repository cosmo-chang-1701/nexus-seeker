"""PYRAMID_ADD 順勢金字塔加碼 (pyramid_add.py) 單元測試。

涵蓋 handoff.md §3.2 列出的八項條件逐項 pass/fail，以及最高優先的條件二
不變式（`ratchet_stop >= avg_cost`，放寬即退化為攤平）、次數上限、冷卻、
曝險降量、空頭部位排除、狀態延後提交等回歸測項。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.constants import (
    _PYRAMID_MAX_ADDS,
    resolve_risk_profile,
)
from market_analysis.dynamic_rollover.pyramid_add import evaluate_pyramid_add_impl

_PROFILE = resolve_risk_profile("DEFENSIVE")  # max_satellite_budget_pct = 0.15


async def _normal_tier() -> str:
    return "NORMAL"


async def _critical_tier() -> str:
    return "CRITICAL"


def _asset(**overrides: Any) -> Dict[str, Any]:
    asset: Dict[str, Any] = {
        "symbol": "NVDA",
        "asset_id": 42,
        "quantity": 100.0,
        "current_value": 1_000.0,
        "avg_cost": 100.0,
        "dynamic_strategy_state": {"ratchet_stop": 100.0, "pyramid_count": 0},
    }
    asset.update(overrides)
    return asset


def _metrics(**overrides: Any) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "spot_price": 110.0,
        "call_wall": 120.0,
        "put_wall": 108.0,
        "atr_15m": 1.0,
        "atr_14": 2.0,
        "session_vwap": 105.0,
        "gamma_flip": 104.0,
        "net_gex": 500_000.0,
    }
    metrics.update(overrides)
    return metrics


async def _evaluate(
    asset: Dict[str, Any],
    metrics: Dict[str, Any],
    *,
    capital: float = 100_000.0,
    risk_limit_pct: float = 15.0,
    vix_spot: float = 20.0,
    resolve_macro_tier: Any = _normal_tier,
    high_60d: float = 0.0,
) -> list:
    with patch(
        "market_analysis.atr_utils.fetch_high_60d",
        new_callable=AsyncMock,
        return_value=high_60d,
    ):
        return await evaluate_pyramid_add_impl(
            engine=None,
            user_id=1,
            asset=asset,
            metrics=metrics,
            profile=_PROFILE,
            capital=capital,
            risk_limit_pct=risk_limit_pct,
            vix_spot=vix_spot,
            resolve_macro_tier=resolve_macro_tier,
        )


@pytest.mark.asyncio
async def test_all_conditions_pass_produces_instruction() -> None:
    """基準情境：八項條件全數通過，產出帶正確欄位的 OPEN_PYRAMID 指令。"""
    instructions = await _evaluate(_asset(), _metrics())
    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "OPEN_PYRAMID"
    assert ins["scenario"] == "PYRAMID_ADD"
    assert ins["asset_id"] == 42
    assert ins["dynamic_state_patch"]["pyramid_count"] == 1
    assert "last_pyramid_at" in ins["dynamic_state_patch"]
    plan = ins["pyramid_add_plan"]
    assert plan["share_qty"] >= 1
    assert plan["stop_price"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_condition1_profit_threshold_not_met() -> None:
    """條件一：獲利未達 3% 門檻 -> 不加碼。"""
    instructions = await _evaluate(_asset(), _metrics(spot_price=101.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition2_ratchet_stop_below_avg_cost_never_produces_instruction() -> (
    None
):
    """最高優先測項：ratchet_stop < avg_cost 時絕對不得產生指令，即使其餘
    七項條件全數通過。放寬此不變式即退化為盲目攤平。"""
    asset = _asset(dynamic_strategy_state={"ratchet_stop": 90.0, "pyramid_count": 0})
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_condition2_missing_ratchet_stop_never_produces_instruction() -> None:
    """ratchet_stop 完全缺失 (0.0) 同樣視為未滿足條件二。"""
    asset = _asset(dynamic_strategy_state={})
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_when_net_gex_non_positive() -> None:
    """條件三：NetGEX <= 0 -> 不加碼（承擔新曝險的閘門，fail-closed）。"""
    instructions = await _evaluate(_asset(), _metrics(net_gex=-100.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_when_net_gex_missing() -> None:
    """條件三：NetGEX 資料缺失 (None) -> fail-closed 不加碼。"""
    metrics = _metrics()
    metrics["net_gex"] = None
    instructions = await _evaluate(_asset(), metrics)
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_when_spot_below_vwap() -> None:
    instructions = await _evaluate(_asset(), _metrics(session_vwap=115.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_when_spot_below_gamma_flip() -> None:
    instructions = await _evaluate(_asset(), _metrics(gamma_flip=115.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition4_fails_when_room_insufficient() -> None:
    """條件四：上方空間不足動態門檻 -> 不加碼。call_wall 貼近現價、且提供
    遠高於現價的 60 日高點停用晴空萬里擴展，確保天花板就是裸 call_wall。"""
    instructions = await _evaluate(
        _asset(), _metrics(call_wall=112.0), high_60d=1_000.0
    )
    assert instructions == []


@pytest.mark.asyncio
async def test_condition5_max_adds_reached_rejects_third_add() -> None:
    """條件五：第 3 次加碼必須被拒絕（pyramid_count 已達 _PYRAMID_MAX_ADDS=2）。"""
    asset = _asset(
        dynamic_strategy_state={
            "ratchet_stop": 100.0,
            "pyramid_count": _PYRAMID_MAX_ADDS,
        }
    )
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_condition6_cooldown_blocks_second_trigger_within_window() -> None:
    """條件六：距上次加碼未滿 8 根 15m bar (2 小時) -> 不加碼。"""
    recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    asset = _asset(
        dynamic_strategy_state={
            "ratchet_stop": 100.0,
            "pyramid_count": 0,
            "last_pyramid_at": recent,
        }
    )
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_condition6_cooldown_elapsed_allows_trigger() -> None:
    """冷卻已滿（超過 2 小時）-> 允許加碼。"""
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    asset = _asset(
        dynamic_strategy_state={
            "ratchet_stop": 100.0,
            "pyramid_count": 0,
            "last_pyramid_at": old,
        }
    )
    instructions = await _evaluate(asset, _metrics())
    assert len(instructions) == 1


@pytest.mark.asyncio
async def test_condition7_exposure_cap_downsizes_instead_of_rejecting() -> None:
    """條件七：加碼後總曝險超過 max_satellite_budget_pct 時降量而非直接拒絕。"""
    # capital=100_000, DEFENSIVE max_satellite_budget_pct=0.15 -> 預算 $15,000
    # current_value=$14,500 -> 剩餘預算僅 $500，遠低於無約束下的建議股數。
    asset = _asset(current_value=14_500.0)
    instructions = await _evaluate(asset, _metrics())
    assert len(instructions) == 1
    plan = instructions[0]["pyramid_add_plan"]
    assert plan["binding_constraint"] == "EXPOSURE_CAP"
    assert 0 < plan["share_qty"] < 50


@pytest.mark.asyncio
async def test_condition7_exposure_cap_rejects_when_no_budget_remains() -> None:
    """已無剩餘預算時，降量後不足 1 股 -> 直接拒絕。"""
    asset = _asset(current_value=15_000.0)  # 已達 max_satellite_budget_pct 上限
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_condition8_macro_tier_not_normal_rejects() -> None:
    """條件八：宏觀逃頂 tier 非 NORMAL -> 不加碼。"""
    instructions = await _evaluate(
        _asset(), _metrics(), resolve_macro_tier=_critical_tier
    )
    assert instructions == []


@pytest.mark.asyncio
async def test_short_position_never_enters_pyramid_add_path() -> None:
    """空頭部位 (quantity < 0) 完全不進入本路徑。"""
    asset = _asset(quantity=-100.0)
    instructions = await _evaluate(asset, _metrics())
    assert instructions == []


@pytest.mark.asyncio
async def test_state_patch_not_committed_inside_evaluator() -> None:
    """狀態延後提交：評估函式本身絕不呼叫 set_asset_dynamic_state，只把增量
    附在 dynamic_state_patch 上，由派發端在確認送達後才提交。"""
    with patch(
        "market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state"
    ) as mock_set_state:
        instructions = await _evaluate(_asset(), _metrics())
    assert len(instructions) == 1
    mock_set_state.assert_not_called()
    assert instructions[0]["dynamic_state_patch"] == {
        "pyramid_count": 1,
        "last_pyramid_at": instructions[0]["dynamic_state_patch"]["last_pyramid_at"],
    }


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "services.market_data_service.get_vix_term_structure",
    new_callable=AsyncMock,
    return_value={"vts_ratio": 0.5, "is_valid": True},
)
@patch(
    "market_analysis.index_microstructure.fetch_core_macro_metrics",
    new_callable=AsyncMock,
    return_value={"fear_greed": 40.0},
)
@patch(
    "market_analysis.atr_utils.fetch_high_60d",
    new_callable=AsyncMock,
    return_value=0.0,
)
async def test_end_to_end_via_check_satellite_rebalancing(
    _mock_high_60d: AsyncMock,
    _mock_fear_greed: AsyncMock,
    _mock_vts: AsyncMock,
    _mock_regime: AsyncMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
) -> None:
    """端到端：`check_satellite_rebalancing` 正確解析 capital/risk_limit、把
    VIX 即時值與記憶化的宏觀逃頂 tier 解析器一路傳進 `evaluate_pyramid_add_impl`，
    對符合條件的多頭 SATELLITE 部位產出 PYRAMID_ADD 指令。"""
    mock_get_user.return_value = MagicMock(
        risk_appetite="DEFENSIVE", capital=100_000.0, risk_limit=15.0
    )
    engine = DynamicRolloverEngine()
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_id": 42,
            "asset_class": "SATELLITE",
            "quantity": 100.0,
            "current_value": 1_000.0,
            "avg_cost": 100.0,
            "spot_price": 110.0,
            "call_wall": 120.0,
            "put_wall": 108.0,
            "atr_15m": 1.0,
            "atr_14": 2.0,
            "session_vwap": 105.0,
            "gamma_flip": 104.0,
            "gex_profile_data": {"net_gex": 500_000.0},
            "dynamic_strategy_state": {"ratchet_stop": 100.0, "pyramid_count": 0},
        },
    ]

    instructions = await engine.check_satellite_rebalancing(
        1, portfolio, 100_000.0, 20.0
    )

    pyramid_instructions = [
        ins for ins in instructions if ins.get("scenario") == "PYRAMID_ADD"
    ]
    assert len(pyramid_instructions) == 1
    assert pyramid_instructions[0]["action"] == "OPEN_PYRAMID"
    assert pyramid_instructions[0]["asset_id"] == 42
