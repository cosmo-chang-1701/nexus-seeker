"""/x 與 /dash 的四項呈現矛盾修正（sar/x-panel-dash-coherence）。

B1 弱 PutWall 不得稱進場甜蜜點；B2 近到期／背離 IV 不得算 IV/HV20；
B3 0 口原因顯示數字；B4 /dash 安全提領紅線對照可動用現金。
"""

import re
from types import SimpleNamespace
from typing import Any

import pytest

from cogs.embed_builders.portfolio_embeds import (
    create_strategic_dash_embed,
    create_tactical_symbol_embed,
)
from market_analysis.payout_coverage import payout_coverage, short_put_collateral
from market_analysis.risk_engine import optimize_position_risk
from models.quant import IVMetrics, OptimizationResult


def _embed_text(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    parts = [str(embed.description or "")]
    for field in embed.fields:
        parts.append(str(field.name))
        parts.append(str(field.value))
    return "\n".join(parts)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ── B1 ─────────────────────────────────────────────────────────────────


def _wall_case(net_at_wall: float, call_wall: float = 1250.0) -> dict[str, Any]:
    return {
        "symbol": "MU",
        "price": 1097.39,
        "gex_profile_data": {
            "put_wall": 1050.0,
            "call_wall": call_wall,
            "net_gex": 49_097_538_000,
            "gex_profile": {
                "1050.0": net_at_wall,
                f"{call_wall}": 900_000_000,
            },
        },
        "atr_15m": 9.68,
        "atr_14": 40.0,
    }


@pytest.mark.parametrize("net, kind", [(-138_988_000, "助跌區"), (1_000, "紙牆")])
def test_b1_weak_putwall_never_sweet_spot(net: float, kind: str) -> None:
    desc = _embed_text(_wall_case(net))
    assert "進場甜蜜點" not in desc
    assert desc.count("✅ 停損距離合格") == 1
    assert f"⚠ PutWall 為{kind}，支撐性待校準" in desc


def test_b1_weak_and_upside_short_single_qualified_line() -> None:
    desc = _embed_text(_wall_case(-138_988_000, call_wall=1100.0))
    assert "進場甜蜜點" not in desc
    assert desc.count("✅ 停損距離合格") == 1
    assert "支撐性待校準" not in desc
    assert "❌ 上檔空間" in desc
    assert "非進場點" in desc


def test_b1_strong_putwall_keeps_sweet_spot() -> None:
    desc = _embed_text(_wall_case(5_000_000_000))
    assert "✅ 進場甜蜜點" in desc
    assert "支撐性待校準" not in desc


# ── B2 ─────────────────────────────────────────────────────────────────


def _iv_data(**kw: Any) -> IVMetrics:
    base: dict[str, Any] = dict(
        symbol="GOOGL",
        current_iv=0.086,
        iv_source="LIVE_IV",
        is_premarket=False,
        current_iv_expiry="2026-10-16",
        current_iv_dte=1,
        straddle_implied_iv=0.324,
        straddle_expiry="2026-10-22",
        straddle_dte=7,
        hv_20=0.205,
        iv_history_count=1,
    )
    base.update(kw)
    return IVMetrics(**base)


def _iv_text(iv: IVMetrics) -> str:
    return _plain(
        _embed_text(
            {
                "symbol": "GOOGL",
                "price": 180.0,
                "quote": {"c": 180.0, "d": 1.0, "dp": 0.5, "pc": 179.0},
                "iv_data": iv,
                "expected_move_context": {
                    "reference_price": 180.0,
                    "reference_label": "現價",
                },
            }
        )
    )


def test_b2_near_expiry_uses_straddle_hv_ratio() -> None:
    text = _iv_text(_iv_data())
    assert "近到期取樣，可能失真" in text
    assert "跨式 IV/HV20: 32.4% / 20.5%" in text
    assert "含結算 Gamma 偏高" not in text
    assert " └─ IV/HV20:" not in text


def test_b2_diverged_dte5_marks_divergence() -> None:
    text = _iv_text(
        _iv_data(current_iv=0.13, current_iv_dte=5, current_iv_expiry="2026-10-20")
    )
    assert "，與跨式背離" in text
    assert "跨式 IV/HV20" in text


def test_b2_not_diverged_keeps_current_iv_ratio() -> None:
    text = _iv_text(
        _iv_data(current_iv=0.30, current_iv_dte=5, current_iv_expiry="2026-10-20")
    )
    assert "與跨式背離" not in text
    assert " └─ IV/HV20: 30.0% / 20.5%" in text
    assert "跨式 IV/HV20" not in text


# ── B3 ─────────────────────────────────────────────────────────────────


def test_b3_optimize_returns_presentation_fields() -> None:
    res = optimize_position_risk(
        current_delta=10.0,
        unit_weighted_delta=100.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="STO_PUT",
    )
    assert res.unit_adj_delta is not None and res.unit_adj_delta > 0
    assert res.risk_budget_shares is not None


def _kelly_text(res: OptimizationResult) -> str:
    return _plain(
        _embed_text(
            {
                "symbol": "MU",
                "price": 100.0,
                "kelly_sizing": res,
                "kelly_beta": 1.2,
            }
        )
    )


def test_b3_zero_contract_line_shows_numbers() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=85.37,
        risk_budget_shares=30.0,
    )
    assert (
        "0 口原因: 單口 ≈85.4 股 SPY 等值（含 IV 調整）> 可用額度 ≈30.0 股"
        in _kelly_text(res)
    )


def test_b3_zero_contract_line_budget_exhausted() -> None:
    res = OptimizationResult(
        suggested_contracts=0,
        exposure_pct=0.0,
        unit_adj_delta=85.0,
        risk_budget_shares=-5.0,
    )
    assert "0 口原因: 組合既有 β 加權 Delta 已用盡可用額度" in _kelly_text(res)


def test_b3_zero_contract_line_falls_back_without_fields() -> None:
    res = OptimizationResult(suggested_contracts=0, exposure_pct=0.0)
    assert "0 口原因: 單口 β 加權 Delta 已超過可用風險額度" in _kelly_text(res)


# ── B4 ─────────────────────────────────────────────────────────────────


def _put(sym: str, strike: float, qty: int, exp: str = "2026-11-06") -> dict[str, Any]:
    return {
        "symbol": sym,
        "opt_type": "Put",
        "strike": strike,
        "expiry": exp,
        "quantity": qty,
    }


def test_collateral_naked() -> None:
    assert short_put_collateral([_put("SPCX", 145.0, -2)]) == 29000.0


def test_collateral_spread_covered_and_partial() -> None:
    full = short_put_collateral([_put("A", 100.0, -2), _put("A", 95.0, 2)])
    assert full == pytest.approx(1000.0)
    partial = short_put_collateral([_put("A", 100.0, -2), _put("A", 95.0, 1)])
    assert partial == pytest.approx(500.0 + 10000.0)
    # 較高履約價的 long 不構成價差；不同到期亦不涵蓋
    assert short_put_collateral([_put("A", 100.0, -1), _put("A", 105.0, 1)]) == 10000.0
    assert (
        short_put_collateral([_put("A", 100.0, -1), _put("A", 95.0, 1, "2026-12-18")])
        == 10000.0
    )


def test_collateral_ignores_calls_and_bad_rows() -> None:
    trades: list[dict[str, Any]] = [
        {**_put("A", 50.0, -1), "opt_type": "Call"},
        {**_put("A", 50.0, -1), "strike": None},
        {**_put("A", 50.0, -1), "quantity": "x"},
        {**_put("A", 50.0, 0)},
        {**_put("B", 30.0, -1), "opt_type": "p"},
    ]
    assert short_put_collateral(trades) == 3000.0


def test_payout_coverage_math() -> None:
    assert payout_coverage(47011.63, 35000.0, 16500.0) == pytest.approx(
        (12011.63, -4488.37)
    )


def _dash(cash: float, pnl: dict[str, Any]) -> str:
    ctx = SimpleNamespace(
        total_theta=10.0,
        monthly_expense=4000.0,
        cash_reserve=cash,
        is_professional_mode=True,
        total_vanna=0.0,
        total_weighted_delta=0.0,
        capital=100000.0,
    )
    embed = create_strategic_dash_embed(ctx, pnl, vix_spot=18.0)
    return _plain("\n".join(str(f.value) for f in embed.fields))


def test_dash_real_case_below_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "market_analysis.trading_orchestration.get_safety_payout_threshold",
        lambda: 16500.0,
    )
    pnl = {
        "trades": [
            _put("SPCX", 145.0, -2),
            _put("GEO", 30.0, -2, "2026-11-20"),
        ]
    }
    text = _dash(47011.63, pnl)
    assert "安全提領紅線: $16,500" in text
    assert "可動用現金 $12,012" in text
    assert "Short Put 擔保金 $35,000" in text
    assert "❌ 低於紅線 $4,488：擔保金解鎖前暫停新 CSP" in text


def test_dash_above_red_line_and_no_puts() -> None:
    text = _dash(60000.0, {})
    assert "擔保金 $0" in text
    assert "✅ 高於紅線 $" in text


def test_dash_cash_unset() -> None:
    text = _dash(0.0, {"trades": [_put("A", 100.0, -1)]})
    assert "可動用現金 --（未設定現金儲備）" in text
    assert "✅ 高於" not in text and "❌ 低於" not in text
