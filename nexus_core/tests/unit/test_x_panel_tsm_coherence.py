"""/x 面板 TSM 報告四項一致性：RVOL 狀態基準、結算窗標籤、UOA 現價核對、IVR 樣本註記。"""

import copy
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.uoa_telemetry import reconcile_breached_levels
from tests.unit.test_x_panel_embed_budget import _mrvl_data
from tests.unit.test_x_panel_gex_uoa_coherence import _render

_PUT_OK = "賣方看守地板（貸方價差，定義風險承接）"
_CALL_OK = "賣方看跌上限（貸方價差，定義風險封頂）"


def _row(typ: str, strike: float, intent: str) -> dict[str, Any]:
    return {
        "expiry": "2026-10-16", "strike": strike, "type": typ, "volume": 1000,
        "oi": 2000, "ratio": 0.5, "ratio_str": "0.50x", "trade_type": "SWEEP",
        "trade_type_inferred": True, "action": "🔴 賣出開倉 (STO - Bid)",
        "trade_price": 1.0, "bid_price": 0.9, "ask_price": 1.1,
        "intent": intent, "notional_value": 100000.0, "level_breached": False,
    }  # fmt: skip


def _uoa_data(row: dict[str, Any], price: float) -> dict[str, Any]:
    d = _mrvl_data()
    d["uoa"] = [row]
    d["quote"] = {"c": price, "d": 0.0, "dp": 0.0, "pc": price}
    return d


def _uoa_text(row: dict[str, Any], price: float) -> str:
    text, _ = _render(_uoa_data(row, price))
    return text


def test_put_credit_breached_by_panel_price() -> None:
    row = _row("PUT", 415.0, f"平價 PUT，{_PUT_OK}")
    assert "賣方地板已破" in _uoa_text(row, 410.0)
    text = _uoa_text(row, 410.0)
    assert "賣方看守地板" not in text
    assert "賣方看守地板" in _uoa_text(row, 420.0)


def test_call_credit_mirror() -> None:
    row = _row("CALL", 300.0, f"平價 CALL，{_CALL_OK}")
    assert "賣方上限已破" in _uoa_text(row, 310.0)
    assert "賣方看跌上限" in _uoa_text(row, 290.0)


def test_reconcile_pure_and_length_not_longer() -> None:
    rows = [_row("PUT", 415.0, f"平價 PUT，{_PUT_OK}，基準註記")]
    snap = copy.deepcopy(rows)
    out = reconcile_breached_levels(rows, 410.0)
    assert rows == snap
    assert out[0] is not rows[0]
    assert out[0]["level_breached"] is True
    assert out[0]["intent"].endswith("基準註記")
    assert len(out[0]["intent"]) <= len(rows[0]["intent"])
    assert reconcile_breached_levels(rows, 0.0) == snap
    assert reconcile_breached_levels(rows, 420.0) == snap


def test_reconcile_single_leg_floor_clause() -> None:
    rows = [_row("PUT", 415.0, "價外，強力構築下行支撐地板")]
    out = reconcile_breached_levels(rows, 410.0)
    assert "現價已跌破、地板失效" in out[0]["intent"]
    assert out[0]["level_breached"] is True


def test_reconcile_skips_non_sto() -> None:
    row = _row("PUT", 415.0, f"平價 PUT，{_PUT_OK}")
    row["action"] = "🟢 買入開倉 (BTO - Ask)"
    assert reconcile_breached_levels([row], 410.0)[0]["intent"] == row["intent"]


def _rvol_text(**extra: Any) -> str:
    d: dict[str, Any] = {
        "symbol": "TSM",
        "bar_15m": {
            "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.5,
            "volume": 3_660_000, "avg_volume": 1_000_000,
        },
        "rvol_15m": 3.66,
    }  # fmt: skip
    d.update(extra)
    embed = create_tactical_symbol_embed(d)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def test_rvol_status_prefix_follows_basis() -> None:
    with_tod = _rvol_text(rvol_15m_tod=1.14, tod_sample_count=4)
    assert "(同時段: ❌ 缺乏放量代償 < 1.5x" in with_tod
    assert "(狀態: ❌" not in with_tod
    without = _rvol_text()
    assert "(狀態: 🟢 放量 >= 1.5x)" in without
    assert "(同時段: " not in without


def test_settlement_window_label() -> None:
    text, _ = _render(_mrvl_data())
    assert "結算窗✅" in text and "引力✅" not in text
    d = _mrvl_data()
    d["circuit_breaker_triggered"] = True
    text, _ = _render(d)
    assert "結算窗—" in text and "引力—" not in text


def _ivr_text(count: int, sessions: int) -> str:
    from types import SimpleNamespace

    d = _mrvl_data()
    iv = d["iv_data"]
    assert hasattr(iv, "__dict__")
    d["iv_rank"] = None
    d["iv_data"] = SimpleNamespace(
        **{**vars(iv), "iv_history_count": count, "iv_history_required": 60}
    )
    with patch("market_time.count_nyse_sessions_after", return_value=sessions):
        embed = create_tactical_symbol_embed(d)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def test_ivr_sample_note() -> None:
    text = _ivr_text(1, 12)
    assert "樣本累積中 1/60 日，10/01 母體重建，非每日取樣；" in text
    assert "母體重置" not in text
    text = _ivr_text(12, 12)
    assert "樣本累積中 12/60 日，10/01 母體重建；" in text
    assert "非每日取樣" not in text
    assert "母體重置" not in text
