"""
tests/unit/test_advisory_mode.py

單元測試：階段 B — B&H 持倉顧問模式 (advisory_mode.py + 派發端三態解析)。

最高優先不變式：
  1. 預設 (portfolio_mode=COMMAND、advisory_only 缺省) 的行為與改動前逐位元相同。
  2. 顧問 SPOT 持倉永不產生 LIQUIDATE / REDUCE（也不外洩 dynamic_state_patch）。
     ⚠️ RolloverInstruction.action 是純 str，mypy 不會追蹤 "ADVISORY" 的未處理
     分支，這些測項是唯一防線。
"""

from typing import Any, Dict, List, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.advisory_mode import (
    build_advisory_instruction,
    is_advisory_asset,
)
from market_analysis.dynamic_rollover.models import RolloverInstruction
from market_analysis.room_threshold import resolve_effective_target


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


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


def _ins(**overrides: Any) -> RolloverInstruction:
    base: Dict[str, Any] = {
        "symbol": "NVDA",
        "action": "LIQUIDATE",
        "sell_ratio": 0.5,
        "target_core": "VOO",
        "reason": "唯一指令：立即市價全數賣出",
        "suggested_strategy": "STC 50%",
        "scenario": "SATELLITE_REBALANCE",
        "is_manual_override_required": True,
        "trigger_condition_text": "x",
        "cash_impact": "+$5,000",
        "limit_price": 100.0,
        "extreme_stop_loss": 90.0,
        "is_extreme_tick_breach": True,
        "extreme_breach_detail_block": "detail",
        "instrument_type": "SPOT",
        "exit_tier": "TP1",
        "asset_id": 42,
        "dynamic_state_patch": {"ratchet_stop": 95.0},
    }
    base.update(overrides)
    return cast(RolloverInstruction, base)


_METRICS: Dict[str, Any] = {
    "spot_price": 100.0,
    "call_wall": 100.0,
    "atr_14": 3.0,
}


# ---------------------------------------------------------------------------
# is_advisory_asset
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "asset,expected",
    [
        ({"advisory_only": True, "quantity": 10.0}, True),
        ({"advisory_only": False, "quantity": 10.0}, False),
        ({"quantity": 10.0}, False),
        ({"advisory_only": None, "quantity": 10.0}, False),
        # 嚴格 is True：truthy 但非 True 者不誤判（MagicMock 安全）
        ({"advisory_only": "ADVISORY", "quantity": 10.0}, False),
        ({"advisory_only": MagicMock(), "quantity": 10.0}, False),
        # 空頭現貨維持指令模式
        ({"advisory_only": True, "quantity": -10.0}, False),
    ],
)
def test_is_advisory_asset(asset: Dict[str, Any], expected: bool) -> None:
    assert is_advisory_asset(asset) is expected


# ---------------------------------------------------------------------------
# build_advisory_instruction：轉換規則
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["SL_STRUCTURAL", "EXTREME_TICK_BREACH"])
async def test_structure_tiers_become_advisory(tier: str) -> None:
    ins = _ins(exit_tier=tier, action="LIQUIDATE", sell_ratio=1.0)
    out = await build_advisory_instruction(
        ins, _asset(advisory_only=True), _METRICS, 92.0
    )
    assert out is not None
    assert out["action"] == "ADVISORY"
    assert out["sell_ratio"] == 0.0
    assert out["target_core"] == ""
    assert out["exit_tier"] == tier
    assert out["scenario"] == "SATELLITE_REBALANCE"
    assert out["is_extreme_tick_breach"] is False
    assert out["is_manual_override_required"] is False
    assert "唯一指令" not in out["reason"]
    plan = out["advisory_plan"]
    assert plan is not None and plan["kind"] == "STRUCTURE_FAILURE"
    assert plan["stop_loss"] == pytest.approx(92.0)


@pytest.mark.asyncio
async def test_command_only_fields_are_cleared() -> None:
    out = await build_advisory_instruction(
        _ins(exit_tier="SL_STRUCTURAL"),
        _asset(advisory_only=True),
        _METRICS,
        92.0,
    )
    assert out is not None
    for key in (
        "suggested_strategy",
        "trigger_condition_text",
        "cash_impact",
        "limit_price",
        "extreme_stop_loss",
        "extreme_breach_detail_block",
        "dynamic_state_patch",
        "asset_id",
    ):
        assert key not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["TP1", "TP2", "TP3"])
async def test_tp_tiers_fold_into_target_reached(tier: str) -> None:
    with patch(
        "market_analysis.atr_utils.fetch_high_60d",
        new_callable=AsyncMock,
        return_value=0.0,
    ):
        out = await build_advisory_instruction(
            _ins(exit_tier=tier),
            _asset(advisory_only=True),
            _METRICS,
            92.0,
        )
    assert out is not None
    assert out["action"] == "ADVISORY"
    assert out["sell_ratio"] == 0.0
    assert out["target_core"] == ""
    plan = out["advisory_plan"]
    assert plan is not None and plan["kind"] == "TARGET_REACHED"
    expected = resolve_effective_target(100.0, 100.0, 0.0, 3.0).target
    assert plan["target"] == pytest.approx(expected)
    assert "不建議減碼" in out["reason"]


@pytest.mark.asyncio
async def test_tp_target_uses_blue_sky_ceiling_when_near_high() -> None:
    with patch(
        "market_analysis.atr_utils.fetch_high_60d",
        new_callable=AsyncMock,
        return_value=100.0,
    ):
        out = await build_advisory_instruction(
            _ins(exit_tier="TP1"),
            _asset(advisory_only=True),
            _METRICS,
            92.0,
        )
    assert out is not None
    plan = out["advisory_plan"]
    assert plan is not None
    expected = resolve_effective_target(100.0, 100.0, 100.0, 3.0)
    assert plan["target"] == pytest.approx(expected.target)
    assert plan["is_blue_sky"] == expected.is_blue_sky


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"exit_tier": "SL_REGIME_FLIP"},
        {"exit_tier": "SL_WHALE_PUT"},
        {"exit_tier": None, "action": "REDUCE"},  # 比例控管
        {"exit_tier": "SL_TRAILING_BREAKEVEN", "action": "HOLD"},
        {"exit_tier": "TP1_TREND_EXEMPT", "action": "HOLD"},
        {"exit_tier": "SL_STRUCTURAL", "action": "HOLD"},  # 淨額化為 0
        {"exit_tier": "TP1", "action": "HOLD"},  # 淨額化為 0
        {"exit_tier": None, "action": "HOLD"},  # 灰帶
    ],
)
async def test_noise_instructions_are_dropped(overrides: Dict[str, Any]) -> None:
    out = await build_advisory_instruction(
        _ins(**overrides),
        _asset(advisory_only=True),
        _METRICS,
        92.0,
    )
    assert out is None


# ---------------------------------------------------------------------------
# 端到端：check_satellite_rebalancing
# ---------------------------------------------------------------------------
async def _run(
    engine: DynamicRolloverEngine, portfolio: List[Dict[str, Any]]
) -> List[Any]:
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
        return await engine.check_satellite_rebalancing(1, portfolio, 100_000.0)


def _assert_no_commands(instructions: List[Any]) -> None:
    for ins in instructions:
        assert ins["action"] not in ("LIQUIDATE", "REDUCE"), ins
        if ins["scenario"] == "SATELLITE_REBALANCE":
            assert ins["action"] == "ADVISORY", ins
            assert ins["sell_ratio"] == 0.0
            assert ins["target_core"] == ""
            assert not ins.get("dynamic_state_patch")


@pytest.mark.asyncio
async def test_default_is_bit_identical_when_advisory_key_absent(
    engine: DynamicRolloverEngine,
) -> None:
    """未帶 advisory_only 與顯式 False 的輸出必須逐位元相同，且維持指令語意。"""
    plain = await _run(engine, [_asset(spot_price=100.0, call_wall=100.0)])
    explicit_false = await _run(
        engine, [_asset(spot_price=100.0, call_wall=100.0, advisory_only=False)]
    )
    assert plain == explicit_false
    assert len(plain) == 1
    assert plain[0]["action"] in ("LIQUIDATE", "REDUCE")
    assert plain[0]["action"] != "ADVISORY"


@pytest.mark.asyncio
async def test_advisory_tp1_becomes_target_reached(
    engine: DynamicRolloverEngine,
) -> None:
    out = await _run(
        engine, [_asset(spot_price=100.0, call_wall=100.0, advisory_only=True)]
    )
    assert len(out) == 1
    _assert_no_commands(out)
    assert out[0]["advisory_plan"]["kind"] == "TARGET_REACHED"


@pytest.mark.asyncio
async def test_advisory_trend_exempt_hold_is_dropped_without_state_patch(
    engine: DynamicRolloverEngine,
) -> None:
    """牆遷移 1%~3% 灰帶 (TP1_TREND_EXEMPT HOLD + 棘輪 patch)：顧問持倉丟棄，
    且不得外洩 dynamic_state_patch。"""
    portfolio = [
        _asset(
            spot_price=99.6,
            call_wall=100.0,
            previous_call_wall=98.0,
            put_wall=99.3,
            avg_cost=90.0,
            advisory_only=True,
        )
    ]
    out = await _run(engine, portfolio)
    assert out == []
    # 對照組：指令模式同資料仍會產出帶 patch 的 HOLD
    cmd = await _run(engine, [{**portfolio[0], "advisory_only": False}])
    assert len(cmd) == 1 and cmd[0]["action"] == "HOLD"
    assert cmd[0].get("dynamic_state_patch")


@pytest.mark.asyncio
async def test_advisory_proportion_control_is_dropped(
    engine: DynamicRolloverEngine,
) -> None:
    """超過 max_allocation_pct 且無 TP/SL 觸發：指令模式 REDUCE，顧問模式無輸出。"""
    base = _asset(
        spot_price=100.0,
        call_wall=130.0,
        put_wall=90.0,
        current_value=50_000.0,
        max_allocation_pct=0.3,
    )
    cmd = await _run(engine, [base])
    assert len(cmd) == 1 and cmd[0]["action"] in ("REDUCE", "LIQUIDATE")
    out = await _run(engine, [{**base, "advisory_only": True}])
    _assert_no_commands(out)
    assert out == []


@pytest.mark.asyncio
async def test_advisory_never_touches_options_or_shorts(
    engine: DynamicRolloverEngine,
) -> None:
    """空頭現貨即使被標 advisory_only 也維持指令模式。"""
    short = _asset(
        quantity=-100.0,
        current_value=-10_000.0,
        spot_price=100.0,
        put_wall=100.0,
        call_wall=120.0,
        advisory_only=True,
    )
    with patch(
        "market_analysis.dynamic_rollover.anti_washout.build_advisory_instruction",
        new_callable=AsyncMock,
    ) as mock_build:
        await _run(engine, [short])
    mock_build.assert_not_called()


@pytest.mark.asyncio
async def test_pyramid_add_stays_command_in_advisory_mode(
    engine: DynamicRolloverEngine,
) -> None:
    """鎖定決定：加碼是新增曝險，與 B&H 相容，顧問模式下不轉換也不丟棄。"""
    fake_add = [
        {
            "symbol": "NVDA",
            "action": "OPEN_PYRAMID",
            "sell_ratio": 0.0,
            "target_core": "NVDA",
            "reason": "pyramid",
            "scenario": "PYRAMID_ADD",
        }
    ]
    with patch(
        "market_analysis.dynamic_rollover.anti_washout.evaluate_pyramid_add_impl",
        new_callable=AsyncMock,
        return_value=fake_add,
    ):
        out = await _run(
            engine,
            [_asset(spot_price=110.0, call_wall=150.0, advisory_only=True)],
        )
    assert any(i["action"] == "OPEN_PYRAMID" for i in out)
    assert all(i["action"] != "ADVISORY" for i in out if i["scenario"] == "PYRAMID_ADD")


# ---------------------------------------------------------------------------
# 其餘兩個情境跳過顧問持倉；MARGIN_DEFENSE 不受影響
# ---------------------------------------------------------------------------
def test_opportunity_cost_and_macro_trim_import_the_skip() -> None:
    """兩個情境的迴圈皆以 is_advisory_asset 跳過顧問持倉（原始碼層級守衛）。"""
    import inspect

    from market_analysis.dynamic_rollover import (
        macro_top_escape_defense,
        opportunity_cost,
    )

    assert "is_advisory_asset(asset)" in inspect.getsource(opportunity_cost)
    assert "is_advisory_asset(asset)" in inspect.getsource(macro_top_escape_defense)


def test_margin_defense_does_not_use_advisory_skip() -> None:
    """MARGIN_DEFENSE 是帳戶生存線，不得受顧問模式影響。"""
    import inspect

    from market_analysis.dynamic_rollover import margin_defense

    assert "advisory" not in inspect.getsource(margin_defense).lower()


# ---------------------------------------------------------------------------
# migration v079 / user settings
# ---------------------------------------------------------------------------
def test_v079_module_exports_required_attributes() -> None:
    from database.core import get_migrations
    from database.migrations import v079_add_portfolio_mode as m

    assert m.version == 79
    assert m.description
    assert "portfolio_mode" in m.sql and "'COMMAND'" in m.sql
    assert any(x["version"] == 79 for x in get_migrations())


def test_upsert_portfolio_mode_whitelist_and_default(db_conn: Any) -> None:
    import database

    database.upsert_user_config(1001, capital=10_000.0)
    assert database.get_full_user_context(1001).portfolio_mode == "COMMAND"
    database.upsert_user_config(1001, portfolio_mode="ADVISORY")
    assert database.get_full_user_context(1001).portfolio_mode == "ADVISORY"
    database.upsert_user_config(1001, portfolio_mode="garbage")
    assert database.get_full_user_context(1001).portfolio_mode == "COMMAND"


# ---------------------------------------------------------------------------
# /edit_holding advisory_mode 三態 → metadata
# ---------------------------------------------------------------------------
def _choice(value: str) -> MagicMock:
    c = MagicMock()
    c.value = value
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [("ADVISORY", True), ("COMMAND", False), ("FOLLOW", None)],
)
async def test_edit_holding_advisory_mode_writes_tristate(
    mode: str, expected: Any
) -> None:
    from cogs.terminal import holdings

    interaction = MagicMock()
    interaction.user.id = 7
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    manager = MagicMock()
    manager.update_asset_metadata_by_symbol.return_value = True
    with patch("services.asset_manager.AssetManager", return_value=manager), patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ):
        # 只傳 advisory_mode：不得被「未提供任何參數」擋下
        await holdings.edit_holding_impl(
            interaction, "nvda", advisory_mode=_choice(mode)
        )

    interaction.response.send_message.assert_not_called()
    updates = manager.update_asset_metadata_by_symbol.call_args.args[3]
    assert "advisory_only" in updates
    assert updates["advisory_only"] is expected


def test_holdings_flatten_exposes_advisory_only_tristate() -> None:
    """None / True / False 三態經 metadata JSON 往返後不得互相混淆。"""
    import json

    for stored, expected in [(None, None), (True, True), (False, False)]:
        meta = json.loads(json.dumps({"advisory_only": stored}))
        assert meta.get("advisory_only") is expected
    assert json.loads("{}").get("advisory_only") is None
