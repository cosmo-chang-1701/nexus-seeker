from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.transition_engine import (
    build_initial_dynamic_strategy_state,
    evaluate_transition_for_position,
    set_asset_dynamic_state,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


def _base_asset(**overrides: Any) -> Dict[str, Any]:
    asset: Dict[str, Any] = {
        "symbol": "NVDA",
        "asset_id": 42,
        "quantity": 100.0,
        "current_value": 5000.0,
        "avg_cost": 40.0,
        "instrument_type": "SPOT",
        "uoa": [],
        "dynamic_strategy_state": {},
    }
    asset.update(overrides)
    return asset


def _base_metrics(**overrides: Any) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "spot_price": 50.0,
        "price_15m_close": 50.0,
        "price_15m_open": 50.0,
        "call_wall": 60.0,
        "put_wall": 40.0,
        "gamma_flip": 45.0,
        "session_vwap": 48.0,
        "vwap_reclaim_with_volume": False,
    }
    metrics.update(overrides)
    return metrics


def test_build_initial_dynamic_strategy_state_shape() -> None:
    state = build_initial_dynamic_strategy_state("REGIME_I_LEFT_CATCH")
    assert state["entry_mode"] == "DYNAMIC"
    assert state["entry_regime"] == "REGIME_I_LEFT_CATCH"
    assert state["ratchet_applied"] is False
    assert state["pyramided"] is False
    assert state["pyramid_parent_asset_id"] is None
    assert state["lockout"] is False
    assert "entry_timestamp" in state


def test_set_asset_dynamic_state_merges_and_writes_back() -> None:
    mock_asset = MagicMock()
    mock_asset.metadata = {
        "dynamic_strategy_state": {
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": False,
        }
    }
    mock_manager = MagicMock()
    mock_manager.get_asset_by_id.return_value = mock_asset
    mock_manager.update_asset_metadata.return_value = True

    with patch("services.asset_manager.AssetManager", return_value=mock_manager):
        result = set_asset_dynamic_state(1, 42, pyramided=True)

    assert result is True
    mock_manager.update_asset_metadata.assert_called_once()
    call_args = mock_manager.update_asset_metadata.call_args[0]
    assert call_args[0] == 1
    assert call_args[1] == 42
    written_state = call_args[2]["dynamic_strategy_state"]
    assert written_state["entry_regime"] == "REGIME_I_LEFT_CATCH"
    assert written_state["pyramided"] is True


def test_set_asset_dynamic_state_missing_asset_returns_false() -> None:
    mock_manager = MagicMock()
    mock_manager.get_asset_by_id.return_value = None

    with patch("services.asset_manager.AssetManager", return_value=mock_manager):
        result = set_asset_dynamic_state(1, 999, lockout=True)

    assert result is False
    mock_manager.update_asset_metadata.assert_not_called()


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state")
async def test_path1_left_evolves_to_right_pyramid_and_ratchet(
    mock_set_state: MagicMock, engine: DynamicRolloverEngine
) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": False,
            "lockout": False,
        }
    )
    metrics = _base_metrics(
        price_15m_close=50.0,
        gamma_flip=45.0,
        call_wall=60.0,
        put_wall=40.0,
        session_vwap=48.0,
        vwap_reclaim_with_volume=True,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 2
    hold_ins, pyramid_ins = instructions
    assert hold_ins["action"] == "HOLD"
    assert hold_ins["scenario"] == "TRANSITION_ENGINE"
    assert hold_ins["exit_tier"] == "TRANSITION_RATCHET"
    assert hold_ins["entry_regime"] == "REGIME_I_LEFT_CATCH"
    assert pyramid_ins["action"] == "OPEN_PYRAMID"
    assert pyramid_ins["exit_tier"] == "TRANSITION_PYRAMID"
    mock_set_state.assert_called_once_with(1, 42, ratchet_applied=True, pyramided=True)


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state")
async def test_path1_does_not_refire_once_pyramided(
    mock_set_state: MagicMock, engine: DynamicRolloverEngine
) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": True,
            "lockout": False,
        }
    )
    # 站上 VWAP/Gamma Flip 條件仍成立，但 pyramided 已為 True 應跳過路徑1；
    # call_wall 空間足夠 (>3.5%) 也不觸發路徑4，故整體應回傳空 list。
    metrics = _base_metrics(
        price_15m_close=50.0,
        gamma_flip=45.0,
        call_wall=60.0,
        put_wall=40.0,
        session_vwap=48.0,
        vwap_reclaim_with_volume=True,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert instructions == []
    mock_set_state.assert_not_called()


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state")
async def test_path2_left_hard_stop_on_put_wall_breach_with_chasing_put(
    mock_set_state: MagicMock, engine: DynamicRolloverEngine
) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": False,
            "lockout": False,
        }
    )
    # put_wall=40, price_15m_close=39.0 -> breach_pct = (40-39)/40 = 2.5% >= 1.5%
    metrics = _base_metrics(
        price_15m_close=39.0,
        put_wall=40.0,
        gamma_flip=45.0,
        session_vwap=48.0,
        call_wall=60.0,
        vwap_reclaim_with_volume=False,
    )
    uoa_list = [
        {
            "type": "PUT",
            "action": "BTO",
            "ratio": 1.5,
            "notional_value": 300_000.0,
            "strike": 35.0,
        }
    ]

    instructions = await evaluate_transition_for_position(
        engine, 1, asset, metrics, uoa_list
    )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "BOXX"
    assert ins["exit_tier"] == "TRANSITION_LEFT_HARD_STOP"
    mock_set_state.assert_called_once_with(1, 42, lockout=True)


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state")
async def test_path2_fires_without_chasing_put_when_bearish_body_confirms(
    mock_set_state: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """規格衝突解析回歸鎖定：路徑 2 以 Regime I 矩陣「風險防線」欄為準——
    15m 實體陰線跌破 Put Wall 下緣 >1.5% 即觸發硬停損，**不**以追空 PUT BTO
    印花為前提。

    先前實作採用切換路徑欄的較嚴格版本 (破牆 AND 追空 PUT BTO)，會讓緊貼
    Put Wall 的 Regime I 部位在無巨鯨印花的真實結構失效時完全失去停損保護。
    """
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": False,
            "lockout": False,
        }
    )
    # 實體陰線 (open 40.5 > close 39.0)，破位幅度 (40-39)/40 = 2.5% >= 1.5%
    metrics = _base_metrics(
        price_15m_close=39.0,
        price_15m_open=40.5,
        put_wall=40.0,
        gamma_flip=45.0,
        session_vwap=48.0,
        call_wall=60.0,
        vwap_reclaim_with_volume=False,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "LIQUIDATE"
    assert ins["exit_tier"] == "TRANSITION_LEFT_HARD_STOP"
    # 無追空印花時不得出現該升級敘述
    assert "PUT BTO 追擊" not in ins["reason"]
    mock_set_state.assert_called_once_with(1, 42, lockout=True)


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.transition_engine.set_asset_dynamic_state")
async def test_path2_does_not_fire_without_bearish_body(
    mock_set_state: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """破位幅度達標但該根 15m K 棒為陽線 (收復中) 時不觸發硬停損——「實體陰線」
    是矩陣風險防線明訂的確認條件，用於過濾下影線獵殺與盤中假破位。"""
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": False,
            "lockout": False,
        }
    )
    # 實體陽線 (open 38.0 < close 39.0)：雖仍在 Put Wall 之下，但屬收復中
    metrics = _base_metrics(
        price_15m_close=39.0,
        price_15m_open=38.0,
        put_wall=40.0,
        gamma_flip=45.0,
        session_vwap=48.0,
        call_wall=60.0,
        vwap_reclaim_with_volume=False,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert instructions == []
    mock_set_state.assert_not_called()


@pytest.mark.asyncio
async def test_path3_fires_on_entry_bar_low_break_even_above_vwap(
    engine: DynamicRolloverEngine,
) -> None:
    """Regime III 風險防線為「跌破 Session VWAP **或**下破進場 K 棒低點」的 OR
    關係。此處收盤仍站在 VWAP 之上，但已下破標記當下記錄的進場 K 棒低點，
    仍須觸發假突破防禦性平倉。"""
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
            "entry_bar_low": 52.0,
            "pyramided": False,
            "lockout": False,
        }
    )
    # 收盤 51.0 > VWAP 48.0 (未跌破 VWAP)，但 < entry_bar_low 52.0
    metrics = _base_metrics(
        price_15m_close=51.0,
        session_vwap=48.0,
        call_wall=60.0,
        put_wall=40.0,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "LIQUIDATE"
    assert ins["exit_tier"] == "TRANSITION_FALSE_BREAKOUT"
    assert "下破進場 K 棒低點" in ins["reason"]
    assert "跌破 Session VWAP" not in ins["reason"]


@pytest.mark.asyncio
async def test_path3_ignores_entry_bar_low_when_not_recorded(
    engine: DynamicRolloverEngine,
) -> None:
    """未記錄 entry_bar_low (標記當下抓取失敗，或標記早於本功能上線) 時，該子
    條件自動略過，退回僅以 Session VWAP 判定——不得因欄位缺失而誤觸發。"""
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
            "pyramided": False,
            "lockout": False,
        }
    )
    metrics = _base_metrics(
        price_15m_close=51.0,
        session_vwap=48.0,
        call_wall=60.0,
        put_wall=40.0,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert instructions == []


@pytest.mark.asyncio
async def test_build_dynamic_strategy_state_captures_entry_bar_low() -> None:
    """標記入口組裝函式應擷取當下已收盤 15m K 棒的低點並寫入 state。"""
    from market_analysis.dynamic_rollover.transition_engine import (
        build_dynamic_strategy_state_for_symbol,
    )

    bar = MagicMock()
    bar.low = 123.45
    with patch(
        "market_analysis.price_volume_alert.get_confirmed_15m_bar",
        new_callable=AsyncMock,
        return_value=bar,
    ):
        state = await build_dynamic_strategy_state_for_symbol(
            "NVDA", "REGIME_III_RIGHT_MOMENTUM"
        )

    assert state["entry_bar_low"] == 123.45
    assert state["entry_regime"] == "REGIME_III_RIGHT_MOMENTUM"


@pytest.mark.asyncio
async def test_build_dynamic_strategy_state_leaves_entry_bar_low_none_on_failure() -> (
    None
):
    """抓取失敗時留空而非猜測——路徑 3 會自動退回僅以 Session VWAP 判定。"""
    from market_analysis.dynamic_rollover.transition_engine import (
        build_dynamic_strategy_state_for_symbol,
    )

    with patch(
        "market_analysis.price_volume_alert.get_confirmed_15m_bar",
        new_callable=AsyncMock,
        side_effect=RuntimeError("K 棒抓取失敗"),
    ):
        state = await build_dynamic_strategy_state_for_symbol(
            "NVDA", "REGIME_I_LEFT_CATCH"
        )

    assert state["entry_bar_low"] is None


@pytest.mark.asyncio
async def test_path3_right_false_breakout_defensive_close(
    engine: DynamicRolloverEngine,
) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
            "pyramided": False,
            "lockout": False,
        }
    )
    metrics = _base_metrics(
        price_15m_close=47.0,
        session_vwap=48.0,
        call_wall=60.0,
        put_wall=40.0,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "VOO"
    assert ins["exit_tier"] == "TRANSITION_FALSE_BREAKOUT"


@pytest.mark.asyncio
async def test_path4_call_wall_room_exhausted_take_profit(
    engine: DynamicRolloverEngine,
) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
            "pyramided": False,
            "lockout": False,
        }
    )
    # call_wall=60, spot=58.5 -> room_pct = (60-58.5)/58.5 = 2.56% < 3.5%
    metrics = _base_metrics(
        spot_price=58.5,
        price_15m_close=58.5,
        call_wall=60.0,
        session_vwap=48.0,
        put_wall=40.0,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "LIQUIDATE"
    assert ins["target_core"] == "VOO"
    assert ins["exit_tier"] == "TRANSITION_TAKE_PROFIT"


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.transition_engine.detect_uoa_sto_call_physical_cap"
)
async def test_path4_fires_on_sto_call_physical_cap_even_with_room(
    mock_detect_cap: MagicMock, engine: DynamicRolloverEngine
) -> None:
    mock_detect_cap.return_value = (True, 62.0)
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_I_LEFT_CATCH",
            "pyramided": True,  # 已加碼過，路徑1不應再觸發
            "lockout": False,
        }
    )
    # call_wall 空間充足 (10%)，但機構 STO Call 封頂仍應觸發路徑4
    metrics = _base_metrics(
        spot_price=55.0,
        price_15m_close=55.0,
        call_wall=61.0,
        session_vwap=48.0,
        put_wall=40.0,
        gamma_flip=45.0,
        vwap_reclaim_with_volume=False,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert len(instructions) == 1
    assert instructions[0]["exit_tier"] == "TRANSITION_TAKE_PROFIT"
    assert "STO Call" in instructions[0]["reason"]


@pytest.mark.asyncio
async def test_no_path_fires_returns_empty_list(engine: DynamicRolloverEngine) -> None:
    asset = _base_asset(
        dynamic_strategy_state={
            "entry_mode": "DYNAMIC",
            "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
            "pyramided": False,
            "lockout": False,
        }
    )
    # 站上 VWAP，Call Wall 空間充足，無任何路徑條件成立
    metrics = _base_metrics(
        price_15m_close=52.0,
        session_vwap=48.0,
        call_wall=60.0,
        put_wall=40.0,
    )

    instructions = await evaluate_transition_for_position(engine, 1, asset, metrics, [])

    assert instructions == []


# ----------------------------------------------------------------------
# 協調測試：check_satellite_rebalancing_impl 對已標記/未標記資產的分流
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_tagged_asset_skips_generic_ladder_via_transition_engine(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """有 dynamic_strategy_state 標記且未 lockout 的資產，即使同時符合常規
    max_allocation_pct 超額再平衡 (REDUCE) 的觸發條件，也應改由 Transition
    Engine 接管、完全跳過通用出場矩陣與常規比例控管，不產生 REDUCE 指令。"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
            "asset_id": 42,
            "dynamic_strategy_state": {
                "entry_mode": "DYNAMIC",
                "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
                "pyramided": False,
                "lockout": False,
            },
            # 無任何路徑觸發條件的中性 metrics 快照 (站上 VWAP、Call Wall 空間充足)
            "session_vwap": 100.0,
            "vwap_reclaim_with_volume": False,
        },
    ]
    total_val = 10000.0

    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)

    # Transition Engine 對此中性快照回傳空 list，且不會落入常規 REDUCE 分支
    # (即使 current_alloc 45% 明顯超過 target 20%)。
    assert instructions == []


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_untagged_asset_regression_unaffected_by_transition_engine(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """未標記 dynamic_strategy_state 的資產行為完全不受 Transition Engine 影響
    ——與既有 test_check_satellite_rebalancing 的 REDUCE 案例逐位元對位，
    作為回歸鎖定。"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    total_val = 10000.0

    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)

    assert len(instructions) == 1
    assert instructions[0]["symbol"] == "NVDA"
    assert instructions[0]["action"] == "REDUCE"
    assert instructions[0]["target_core"] == "VOO"
    assert instructions[0]["sell_ratio"] == 0.56
    assert instructions[0]["scenario"] == "SATELLITE_REBALANCE"


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_locked_out_tagged_asset_falls_through_to_generic_ladder(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """dynamic_strategy_state.lockout=True 的資產不應再被 Transition Engine
    接管 (路徑2已執行過清倉，不該重複評估)，而是落回既有通用邏輯 (此案例中
    即常規 REDUCE 再平衡)。"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
            "dynamic_strategy_state": {
                "entry_mode": "DYNAMIC",
                "entry_regime": "REGIME_I_LEFT_CATCH",
                "pyramided": False,
                "lockout": True,
            },
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    total_val = 10000.0

    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)

    assert len(instructions) == 1
    assert instructions[0]["symbol"] == "NVDA"
    assert instructions[0]["action"] == "REDUCE"


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_tagged_asset_still_protected_by_track2_extreme_stop(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """軌道二極端瞬時停損 (黑天鵝最後防線) 必須對已標記 dynamic_strategy_state
    的部位同樣生效，不得因交給 Transition Engine 接管而被跳過。

    Transition Engine 的四條切換路徑都無法涵蓋黑天鵝跳空——路徑 2 除了破牆還
    額外要求同時偵測到追空 PUT BTO 印花、路徑 3 依賴 Session VWAP 抓取成功，
    兩者在極端行情下都可能落空。若在掛載點一併攔截，已標記部位反而會比未標記
    部位保護更薄。

    沿用 test_dynamic_rollover.py 既有的極端瞬時停損數值組合：put_wall=100.0、
    atr_15m=2.0 → extreme_stop_loss = 100 - 3.0*2 = 94.0，spot(90) < 94 觸發。
    此處刻意讓四條路徑全部不成立 (Regime III 部位但現價站在 session_vwap 之上、
    call_wall=0 使路徑 4 不評估)，因此若軌道二被跳過，結果會是空 list。
    """
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "XYZ",
            "asset_class": "SATELLITE",
            "quantity": 100.0,
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
            "spot_price": 90.0,
            "price_15m_close": 90.0,
            "put_wall": 100.0,
            "call_wall": 0.0,
            "gamma_flip": 0.0,
            "hvn": 0.0,
            "atr_14": 0.0,
            "atr_15m": 2.0,
            "ivr": 25.0,
            "sqz_mom": -1.0,
            "skew": 0.0,
            "max_pain": 0.0,
            "is_uoa_sweep": False,
            "gex_profile_data": {},
            "asset_id": 42,
            "dynamic_strategy_state": {
                "entry_mode": "DYNAMIC",
                "entry_regime": "REGIME_III_RIGHT_MOMENTUM",
                "pyramided": False,
                "lockout": False,
            },
            # 現價站上 VWAP：Transition Engine 路徑 3 (假突破防禦性平倉) 不成立
            "session_vwap": 80.0,
            "vwap_reclaim_with_volume": False,
        },
    ]

    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["is_extreme_tick_breach"] is True
    assert ins["extreme_stop_loss"] == 94.0
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    # 必須由既有通用矩陣的極端瞬時停損分支產出，而非 Transition Engine
    assert ins["scenario"] != "TRANSITION_ENGINE"
