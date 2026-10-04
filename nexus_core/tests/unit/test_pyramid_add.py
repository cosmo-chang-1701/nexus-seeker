"""PYRAMID_ADD 順勢金字塔加碼 (pyramid_add.py) 單元測試。

涵蓋八項條件逐項 pass/fail，以及最高優先的條件二不變式（擠壓參考停損
>= avg_cost，放寬即退化為攤平）、次數上限、冷卻、曝險降量、空頭部位排除、
狀態延後提交，以及顧問模式現貨持倉（無 ratchet_stop）可走到評估等回歸測項。
條件二～四的多時間框架擠壓判定以 `squeeze_entry.evaluate_symbol` 注入。
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.constants import (
    _PYRAMID_MAX_ADDS,
    resolve_risk_profile,
)
from market_analysis.dynamic_rollover.pyramid_add import evaluate_pyramid_add_impl
from market_analysis.squeeze_entry import SqueezeEvaluation
from market_analysis.squeeze_entry.resistance import ResistanceContext, ResistanceZone
from market_analysis.squeeze_entry.rules import SqueezeEntryResult
from market_analysis.squeeze_entry.timeframes import TimeframeState

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
        # 顧問模式現貨持倉的實際狀態：ratchet_stop 從未寫入
        "dynamic_strategy_state": {"pyramid_count": 0},
        "advisory_only": True,
    }
    asset.update(overrides)
    return asset


def _metrics(**overrides: Any) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {"spot_price": 110.0}
    metrics.update(overrides)
    return metrics


def _tf(tf: str, **kw: Any) -> TimeframeState:
    base = TimeframeState(
        timeframe=tf,
        squeeze_level="Release",
        is_squeezing=False,
        momentum_value=1.0,
        momentum_color="LightBlue",
        green_dot=False,
        green_dot_bars_ago=None,
        turbo=False,
        squeeze_range_low=None,
        sma_20=104.0,
        last_close=110.0,
        bar_ts="2026-10-01",
    )
    return replace(base, **kw)


def _squeeze(
    *,
    stop: Optional[float] = 101.0,
    d_mom: float = 1.0,
    green: tuple = ("D",),
    broken: Optional[ResistanceZone] = None,
    approaching: Optional[ResistanceZone] = None,
) -> SqueezeEvaluation:
    matrix = {
        tf: _tf(tf, green_dot=tf in green, momentum_value=d_mom if tf == "D" else 1.0)
        for tf in ("W", "3D", "D", "65m", "15m", "5m")
    }
    res = ResistanceContext(
        atr_1d=2.0,
        overhead=approaching,
        is_approaching=approaching is not None,
        broken=broken,
    )
    result = SqueezeEntryResult("WATCH", None, None, stop, "x")
    return SqueezeEvaluation(result, matrix, res)


async def _evaluate(
    asset: Dict[str, Any],
    metrics: Dict[str, Any],
    *,
    capital: float = 100_000.0,
    risk_limit_pct: float = 15.0,
    vix_spot: float = 20.0,
    resolve_macro_tier: Any = _normal_tier,
    squeeze: Optional[SqueezeEvaluation] = None,
) -> list:
    with patch(
        "market_analysis.squeeze_entry.evaluate_symbol",
        new_callable=AsyncMock,
        return_value=squeeze if squeeze is not None else _squeeze(),
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
    assert plan["stop_price"] == pytest.approx(101.0)
    assert "D Green Dot" in ins["reason"]


@pytest.mark.asyncio
async def test_condition1_profit_threshold_not_met() -> None:
    """條件一：獲利未達 3% 門檻 -> 不加碼。"""
    instructions = await _evaluate(_asset(), _metrics(spot_price=101.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition2_stop_below_avg_cost_never_produces_instruction() -> None:
    """最高優先測項：擠壓參考停損 < avg_cost 時絕對不得產生指令，即使其餘
    條件全數通過（放寬即退化為攤平）。"""
    instructions = await _evaluate(_asset(), _metrics(), squeeze=_squeeze(stop=99.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition2_missing_stop_never_produces_instruction() -> None:
    """參考停損算不出來（None）同樣視為未滿足條件二（fail-closed）。"""
    instructions = await _evaluate(_asset(), _metrics(), squeeze=_squeeze(stop=None))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition2_ignores_legacy_ratchet_stop() -> None:
    """舊的 ratchet_stop 即使 >= 成本，也不能取代擠壓參考停損。"""
    asset = _asset(dynamic_strategy_state={"ratchet_stop": 105.0, "pyramid_count": 0})
    instructions = await _evaluate(asset, _metrics(), squeeze=_squeeze(stop=95.0))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_when_daily_momentum_non_positive() -> None:
    instructions = await _evaluate(_asset(), _metrics(), squeeze=_squeeze(d_mom=-0.1))
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_fails_without_continuation_signal() -> None:
    """只有 15m/5m 的 Green Dot 不算（需 65m 以上），也沒有壓力區突破。"""
    instructions = await _evaluate(
        _asset(), _metrics(), squeeze=_squeeze(green=("15m", "5m"))
    )
    assert instructions == []


@pytest.mark.asyncio
async def test_condition3_resistance_breakout_counts_as_continuation() -> None:
    zone = ResistanceZone(107.0, 108.0, 2)
    instructions = await _evaluate(
        _asset(), _metrics(), squeeze=_squeeze(green=(), broken=zone)
    )
    assert len(instructions) == 1
    assert "站上壓力區 $108.00" in instructions[0]["reason"]


@pytest.mark.asyncio
async def test_condition4_fails_when_pressing_unbroken_resistance() -> None:
    zone = ResistanceZone(110.5, 111.0, 3)
    instructions = await _evaluate(
        _asset(), _metrics(), squeeze=_squeeze(approaching=zone)
    )
    assert instructions == []


@pytest.mark.asyncio
async def test_condition4_lower_breakout_does_not_exempt_next_zone() -> None:
    lower = ResistanceZone(105.0, 106.0, 2)
    upper = ResistanceZone(110.5, 111.0, 3)
    instructions = await _evaluate(
        _asset(), _metrics(), squeeze=_squeeze(broken=lower, approaching=upper)
    )
    assert instructions == []


@pytest.mark.asyncio
async def test_squeeze_fetch_skipped_when_cheap_conditions_fail() -> None:
    """條件一／五／六未通過時不得發動多時間框架 K 線抓取。"""
    with patch(
        "market_analysis.squeeze_entry.evaluate_symbol", new_callable=AsyncMock
    ) as mock_eval:
        await evaluate_pyramid_add_impl(
            engine=None,
            user_id=1,
            asset=_asset(),
            metrics=_metrics(spot_price=101.0),
            profile=_PROFILE,
            capital=100_000.0,
            risk_limit_pct=15.0,
            vix_spot=20.0,
            resolve_macro_tier=_normal_tier,
        )
    mock_eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_condition5_max_adds_reached_rejects_third_add() -> None:
    """條件五：第 3 次加碼必須被拒絕（pyramid_count 已達 _PYRAMID_MAX_ADDS=2）。"""
    asset = _asset(
        dynamic_strategy_state={
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
    "market_analysis.squeeze_entry.evaluate_symbol",
    new_callable=AsyncMock,
    return_value=_squeeze(),
)
@patch(
    "database.cache.get_kv_cache",
    # 條件八的宏觀逃頂評分需要 FedWatch 鷹派分數；未知 (None) 時評分回傳
    # UNKNOWN 而 fail-closed，故端到端測試須提供已知值。
    side_effect=lambda key, *a, **k: 0.5
    if key == "macro_fedwatch_probability"
    else None,
)
async def test_end_to_end_via_check_satellite_rebalancing(
    _mock_kv: MagicMock,
    _mock_squeeze: AsyncMock,
    _mock_fear_greed: AsyncMock,
    _mock_vts: AsyncMock,
    _mock_regime: AsyncMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
) -> None:
    """端到端：`check_satellite_rebalancing` 正確解析 capital/risk_limit、把
    VIX 即時值與記憶化的宏觀逃頂 tier 解析器一路傳進 `evaluate_pyramid_add_impl`，
    對符合條件的多頭 SATELLITE 部位產出 PYRAMID_ADD 指令——包含顧問模式
    （advisory_only=True、從未寫入 ratchet_stop）的 B&H 現貨持倉。"""
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
            "dynamic_strategy_state": {"pyramid_count": 0},
            "advisory_only": True,
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


@pytest.mark.asyncio
async def test_low_vix_does_not_zero_out_add_size() -> None:
    """VIX < 15（賣方階梯乘數為 0 的區間）仍須產出加碼股數：VIX 不再調整倉位。"""
    low = await _evaluate(_asset(), _metrics(), vix_spot=12.0)
    normal = await _evaluate(_asset(), _metrics(), vix_spot=20.0)
    assert len(low) == 1 and len(normal) == 1
    low_plan = low[0]["pyramid_add_plan"]
    assert low_plan["share_qty"] == normal[0]["pyramid_add_plan"]["share_qty"] > 0
    assert low_plan["vix_multiplier"] == 1.0
