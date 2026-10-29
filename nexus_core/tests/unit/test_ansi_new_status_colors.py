"""盤後宏觀／相關性新狀態文字的 ANSI 配色測試。"""

from typing import Any, Dict, List

from cogs.embed_builders._ansi_utils import _format_macro_report_ansi
from market_analysis.report_formatter import (
    format_correlation_report,
    format_macro_risk_report,
)

GRAY = "\u001b[1;30m"
YELLOW = "\u001b[1;33m"
GREEN = "\u001b[1;32m"
RED = "\u001b[1;31m"


def _metrics(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "exposure_pct": 0.0,
        "net_exposure_dollars": 0.0,
        "total_beta_delta": 0.0,
        "total_gamma": 0.0,
        "gamma_threshold": 10.0,
        "theta_yield": 0.1,
        "total_theta": 5.0,
        "portfolio_heat": 10.0,
        "total_margin_used": 1000.0,
        "total_vega": 12.5,
        "total_vanna": 250.0,
    }
    base.update(overrides)
    return base


def _render(lines: List[str]) -> str:
    return _format_macro_report_ansi("\n".join(lines))


def _line_with(out: str, keyword: str) -> str:
    matched = [ln for ln in out.split("\n") if keyword in ln]
    assert matched, keyword
    return matched[0]


def test_no_theta_position_is_gray() -> None:
    out = _render(
        format_macro_risk_report(_metrics(total_theta=0.0, theta_yield=0.0), 500.0)
    )
    assert GRAY in _line_with(out, "無 Theta 部位")


def test_time_decay_is_yellow() -> None:
    out = _render(
        format_macro_risk_report(_metrics(total_theta=-20.0, theta_yield=-0.02), 500.0)
    )
    assert YELLOW in _line_with(out, "時間價值流失")


def test_correlation_insufficient_data_is_gray() -> None:
    out = _render(format_correlation_report(None, 3))
    assert GRAY in _line_with(out, "資料不足，未評估")


def test_existing_colors_unchanged() -> None:
    healthy = _render(format_macro_risk_report(_metrics(), 500.0))
    assert GREEN in _line_with(healthy, "現金流健康")
    low = _render(
        format_macro_risk_report(_metrics(total_theta=5.0, theta_yield=0.01), 500.0)
    )
    assert YELLOW in _line_with(low, "收益率過低")
    high = _render(
        format_macro_risk_report(_metrics(total_theta=5.0, theta_yield=0.5), 500.0)
    )
    assert RED in _line_with(high, "過度收租")


def test_beta_data_warning_stays_uncolored() -> None:
    text = "🔹 **Beta 資料品質:** 部分標的\n └─ Beta 資料不足 (暫以 1.0 估算)：AAA"
    out = _format_macro_report_ansi(text)
    assert "Beta 資料不足 (暫以 1.0 估算)：AAA" in out
    assert "\u001b[" not in _line_with(out, "Beta 資料不足")
