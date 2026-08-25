from typing import Any

from models.schemas import EnhancedWatchlistMetrics
from risk_engine.nro import WatchlistRiskController


def _build_metrics(**overrides: Any) -> EnhancedWatchlistMetrics:
    payload: dict[str, Any] = dict(
        symbol="TEST",
        exchange="NASDAQ",
        current_price=100.0,
        buy_zone_status="Wait",
        buy_price_phase1=105.0,
        buy_price_phase2=95.0,
        buy_price_phase3=85.0,
        sell_zone_status="Wait",
        sell_price_phase1=110.0,
        sell_price_phase2=120.0,
        sell_price_phase3=130.0,
        rsi_14=50.0,
        atr_14=2.0,
        beta=1.0,
        ma20=100.0,
        ma50=100.0,
        ma200=100.0,
        iv_rank=70.0,
        iv_percentile=70.0,
        option_skew=0.0,
        skew_percentile=50.0,
        option_skew_state="正常",
        pcr=1.0,
        volume_poc=100.0,
        gex_max_put_wall=100.0,
        vanna_sensitivity=0.1,
        relative_strength_spy=1.0,
        squeeze_momentum=None,
    )
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def test_process_metrics_premium_harvest_without_backwardation() -> None:
    """基準案例：無 Backwardation 時，premium-harvest 情境維持原行為。"""
    metrics = _build_metrics(current_price=100.0, iv_rank=70.0)
    plan = WatchlistRiskController.process_metrics(metrics)
    assert plan.scenario == "premium-harvest"
    assert plan.alert_level == "yellow"


def test_process_metrics_backwardation_downgrades_premium_harvest_to_wait() -> None:
    """Item 2：IV Backwardation 時，premium-harvest（左側限價接刀/CSP 收租）
    須全面降級為 wait，禁止左側限價接刀。"""
    metrics = _build_metrics(
        current_price=100.0, iv_rank=70.0, iv_term_structure_status="Backwardation"
    )
    plan = WatchlistRiskController.process_metrics(metrics)
    assert plan.scenario == "wait"
    assert plan.alert_level == "red"
    assert "總經事件防禦期" in plan.sddm_route
    assert "Backwardation" in plan.action_guideline


def test_process_metrics_backwardation_appends_warning_for_hard_hedge() -> None:
    """hard-hedge 情境本就是最防禦姿態，Backwardation 僅附加警語，不覆寫 scenario。"""
    metrics = _build_metrics(
        current_price=80.0,  # <= buy_price_phase2 (95.0) -> hard-hedge
        iv_term_structure_status="Backwardation",
    )
    plan = WatchlistRiskController.process_metrics(metrics)
    assert plan.scenario == "hard-hedge"
    assert "Backwardation" in plan.action_guideline
