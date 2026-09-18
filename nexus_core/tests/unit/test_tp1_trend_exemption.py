"""TP1 趨勢豁免 (anti_washout.py::_evaluate_microstructure_tp_ladder) 單元測試。

涵蓋 handoff.md §3.1 列出的七項測試，並額外補上探索過程中發現、handoff.md
本身未列出的三個既有架構缺口的回歸測試：

1. `previous_call_wall` 過去從未被 `check_satellite_rebalancing_impl` 寫入
   metrics dict，導致 TP2 牆體遷移分支與本次新增的 TP1 豁免在生產路徑上
   恆為死碼（見 `test_previous_call_wall_is_wired_into_metrics`）。
2. 棘輪停損必須透過 `dynamic_state_patch` + `asset_id` 延後提交，才能在下一輪
   被 `_compute_anti_washout_stop` 讀取（見
   `test_trend_exemption_persists_ratchet_stop_via_dynamic_state_patch`）。
3. 外層閘門（`check_satellite_rebalancing_impl` 的 `tp_tier is not None or ...`
   判斷式）必須把 TP1 豁免本身也視為需要處理的訊號，否則牆遷移 1%~3% 的灰帶
   會讓報告產生流程被整段跳過（見同一測試，斷言確實有指令產出）。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


def _tp1_metrics(**overrides: object) -> dict:
    """基準 metrics：現價已達 Call Wall 99.5% 觸發 TP1，且牆體正快速上移、
    NetGEX 為正、現價站穩 VWAP——四項條件同時成立即應豁免。"""
    base: dict = {
        "spot_price": 100.0,
        "call_wall": 100.0,
        "previous_call_wall": 98.0,  # (100-98)/98 ≈ 2.04% >= 1% 門檻
        "net_gex": 500_000.0,
        "session_vwap": 97.0,
        "avg_cost": 90.0,
        "atr_15m": 1.0,
        "dte": 30,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# handoff.md §3.1 七項測試
# --------------------------------------------------------------------------
def test_exempts_when_wall_migrating_positive_gex_above_vwap(
    engine: DynamicRolloverEngine,
) -> None:
    """牆上移 2% + NetGEX>0 + Spot>VWAP → 豁免（不觸發 TP1，改為棘輪停損）。"""
    tier, ratio, reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(), anchor_base=95.0
    )
    assert tier is None
    assert ratio == 0.0
    assert "TP1-趨勢豁免" in reason
    assert new_stop == pytest.approx(95.0)  # max(avg_cost=90, anchor_base=95)


def test_no_exemption_when_net_gex_non_positive(
    engine: DynamicRolloverEngine,
) -> None:
    """牆上移 2% 但 NetGEX<=0（做市商邏輯已消亡）→ 不豁免，正常 TP1。"""
    tier, ratio, _reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(net_gex=-100.0)
    )
    assert tier == "TP1"
    assert ratio == pytest.approx(0.5)
    assert new_stop is None


def test_no_exemption_when_spot_below_vwap(engine: DynamicRolloverEngine) -> None:
    """牆上移 2% 但 Spot<VWAP → 不豁免，正常 TP1。"""
    tier, _ratio, _reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(session_vwap=101.0)
    )
    assert tier == "TP1"
    assert new_stop is None


def test_no_exemption_when_wall_unchanged(engine: DynamicRolloverEngine) -> None:
    """牆未移動 (previous_call_wall == call_wall) → 不豁免，正常 TP1。"""
    tier, _ratio, _reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(previous_call_wall=100.0)
    )
    assert tier == "TP1"
    assert new_stop is None


def test_no_exemption_when_previous_call_wall_missing(
    engine: DynamicRolloverEngine,
) -> None:
    """previous_call_wall == 0（資料缺失）→ fail-safe 不豁免，正常 TP1。"""
    tier, _ratio, _reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(previous_call_wall=0.0)
    )
    assert tier == "TP1"
    assert new_stop is None


def test_tp2_priority_unaffected_by_exemption_logic(
    engine: DynamicRolloverEngine,
) -> None:
    """牆上移 5%（> 3% TP2 門檻）→ TP2 優先序仍高於 TP1，豁免邏輯不得干擾 TP2。"""
    tier, ratio, reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(previous_call_wall=95.0, call_wall=100.0, spot_price=100.0)
    )
    assert tier == "TP2"
    assert ratio == pytest.approx(0.3)
    assert new_stop is None
    assert "TP2-空間擴展" in reason


def test_short_position_does_not_enter_exemption_path(
    engine: DynamicRolloverEngine,
) -> None:
    """空頭部位 → 完全不進入本路徑（分流至鏡像版，回傳型別仍為 4 元組）。"""
    metrics = _tp1_metrics(
        position_side="SHORT",
        quantity=-100.0,
        put_wall=100.0,
        spot_price=99.6,  # 觸及 Put Wall 的 99.5%~100.5% 範圍
    )
    result = engine._evaluate_microstructure_tp_ladder(metrics)
    assert len(result) == 4
    tier, _ratio, _reason, new_stop = result
    # 空頭鏡像版完全沒有豁免分支：不是 TP1 就是 None，new_stop 恆為 None。
    assert new_stop is None


# --------------------------------------------------------------------------
# 額外回歸測試：fail-safe（anchor_base 與 avg_cost 皆不可得）
# --------------------------------------------------------------------------
def test_no_exemption_when_no_safe_stop_available(
    engine: DynamicRolloverEngine,
) -> None:
    """avg_cost 與 anchor_base 皆不可得時，無法安全抬停損 → fail-safe 退回
    正常 TP1 減碼，不得在無停損保護下豁免。"""
    tier, ratio, _reason, new_stop = engine._evaluate_microstructure_tp_ladder(
        _tp1_metrics(avg_cost=0.0), anchor_base=0.0
    )
    assert tier == "TP1"
    assert ratio == pytest.approx(0.5)
    assert new_stop is None


# --------------------------------------------------------------------------
# 探索發現的既有架構缺口回歸測試（handoff.md 未列出）
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_trend_exemption_persists_ratchet_stop_via_dynamic_state_patch(
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """端到端：`check_satellite_rebalancing_impl` 在牆遷移 1%~3% 灰帶（TP1 已
    豁免、SL-動態保本 50% 進度門檻尚未達標）仍須產出指令，且該指令帶有正確的
    `asset_id` 與 `dynamic_state_patch`，供派發端延後提交棘輪停損。"""
    mock_get_user.return_value = MagicMock(risk_appetite="DEFENSIVE")
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_id": 42,
            "asset_class": "SATELLITE",
            "quantity": 100.0,
            "current_value": 10_000.0,
            "avg_cost": 90.0,
            # spot 貼近但未觸及 call_wall (99.6 < 100)，且錨點 (put_wall=99.3)
            # 與 spot 距離很近——刻意讓 SL-動態保本的 50% 進度門檻不達標
            # ((99.6-99.3)/(100-99.3) ≈ 43%)，確保落在「TP1 已豁免、SL-動態
            # 保本尚未觸發」的 1%~3% 灰帶，這正是外層閘門 (finding 4) 修正前
            # 會被整段跳過、指令永遠傳不到派發端的情境。
            "spot_price": 99.6,
            "call_wall": 100.0,
            # 若 check_satellite_rebalancing_impl 忘記把此欄位接進 metrics
            # （曾經的既有缺陷），TP1 豁免與 TP2 牆體遷移分支都會恆為死碼，
            # 本測試會因 action 落回 REDUCE/HOLD(灰階) 而非帶棘輪停損的 HOLD
            # 失敗。
            "previous_call_wall": 98.0,
            "put_wall": 99.3,
            "session_vwap": 97.0,
            "gex_profile_data": {"net_gex": 500_000.0},
            "dynamic_strategy_state": {},
        },
    ]

    instructions = await engine.check_satellite_rebalancing(1, portfolio, 100_000.0)

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["action"] == "HOLD"
    assert ins["exit_tier"] == "TP1_TREND_EXEMPT"
    assert ins["asset_id"] == 42
    assert ins["dynamic_state_patch"] is not None
    assert ins["dynamic_state_patch"]["ratchet_stop"] > 0.0
