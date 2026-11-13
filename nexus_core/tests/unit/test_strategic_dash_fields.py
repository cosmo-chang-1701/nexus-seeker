"""/dash 戰略看板欄位：體制避險列、Beta-Delta 名目、Theta 措辭、現金等價物提領檢查。"""

import re
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from cogs.embed_builders.portfolio_embeds import create_strategic_dash_embed

_RUNWAY = "🏁 提領跑道 (Withdrawal Runway)"
_NRO = "🛡️ 組合風險精算 (NRO Integrity)"


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _ctx(
    delta: float = 0.0,
    theta: float = 0.0,
    expense: float = 0.0,
    cash: float = 0.0,
) -> Any:
    return SimpleNamespace(
        total_theta=theta,
        monthly_expense=expense,
        cash_reserve=cash,
        is_professional_mode=True,
        total_vanna=0.0,
        total_weighted_delta=delta,
        capital=100000.0,
    )


def _build(ctx: Any, **kw: Any) -> Any:
    kw.setdefault("vix_spot", 20.0)
    with patch(
        "market_analysis.trading_orchestration.get_safety_payout_threshold",
        lambda: 13000.0,
    ):
        return create_strategic_dash_embed(ctx, {}, **kw)


def _field(embed: Any, name: str) -> str:
    for f in embed.fields:
        if f.name == name:
            return _plain(str(f.value))
    raise AssertionError(name)


def test_only_two_fields_no_system_health() -> None:
    embed = _build(_ctx(), spy_price=670.0)
    assert [f.name for f in embed.fields] == [_RUNWAY, _NRO]
    assert all("RAM" not in str(f.value) for f in embed.fields)
    assert all("系統健康" not in f.name for f in embed.fields)


def test_beta_delta_notional_and_nav_pct() -> None:
    embed = _build(_ctx(209.0), spy_price=670.0, nav_data={"nav": 100000.0})
    nro = _field(embed, _NRO)
    assert "$140,030" in nro and "NAV 140%" in nro


def test_beta_delta_spy_unknown_and_nav_unknown() -> None:
    nro = _field(_build(_ctx(209.0), spy_price=None), _NRO)
    assert "SPY 現價未知，無法換算名目" in nro and "$" not in nro.split("\n")[0]
    nro2 = _field(_build(_ctx(209.0), spy_price=670.0), _NRO)
    assert "$140,030" in nro2 and "NAV" not in nro2
    nro3 = _field(_build(_ctx(209.0), spy_price=670.0, nav_data={"nav": 0.0}), _NRO)
    assert "NAV" not in nro3


@pytest.mark.parametrize("delta", [40.0, 420.0, -420.0])
def test_no_spy_trade_advice(delta: float) -> None:
    nro = _field(_build(_ctx(delta), spy_price=670.0), _NRO)
    for bad in ("賣出", "買入", "股 SPY 以對沖", "對沖狀態", "建議對沖位"):
        assert bad not in nro


@pytest.mark.parametrize(
    "regime,expected",
    [
        ("NORMAL", "體制避險: 大盤 NORMAL｜第二級未觸發"),
        (
            "SHORT_GAMMA_CRITICAL",
            "⚠️ 體制避險: 大盤 SHORT_GAMMA_CRITICAL｜第二級條件成立",
        ),
        (
            "SYSTEMIC_LIQUIDITY_CRISIS",
            "⚠️ 體制避險: 大盤 SYSTEMIC_LIQUIDITY_CRISIS｜第二級條件成立",
        ),
        ("UNKNOWN", "體制避險: 大盤體制未知（資料不足）"),
        (None, "體制避險: 大盤體制未知（資料不足）"),
    ],
)
def test_regime_rows(regime: Optional[str], expected: str) -> None:
    nro = _field(_build(_ctx(), spy_price=670.0, market_regime=regime), _NRO)
    assert expected in nro


def test_theta_rows() -> None:
    pos = _field(_build(_ctx(theta=30.0, expense=3000.0)), _RUNWAY)
    assert "期權 Theta: 每日 +$30.00（淨賣方收租） 覆蓋期權熔斷月支出 30.0%" in pos
    pos2 = _field(_build(_ctx(theta=30.0, expense=0.0)), _RUNWAY)
    assert "期權 Theta: 每日 +$30.00（淨賣方收租）" in pos2 and "覆蓋" not in pos2
    neg = _field(_build(_ctx(theta=-12.5, expense=3000.0)), _RUNWAY)
    assert "期權 Theta: 每日 −$12.50（淨買方時間價值流失）" in neg
    assert "%" not in neg and "收租" not in neg
    zero = _field(_build(_ctx(theta=0.0)), _RUNWAY)
    assert "期權 Theta: 無期權部位" in zero


def _nav(ce: float) -> Dict[str, Any]:
    return {"nav": 50000.0, "cash_equivalent_value": ce}


def test_payout_counts_cash_equivalents() -> None:
    text = _field(_build(_ctx(cash=5000.0), nav_data=_nav(20000.0)), _RUNWAY)
    assert "可動用現金 $25,000" in text
    assert "現金等價物 $20,000" in text
    assert "✅ 高於紅線 $12,000" in text


def test_payout_no_cash_equivalent_omits_part() -> None:
    text = _field(_build(_ctx(cash=5000.0), nav_data=_nav(0.0)), _RUNWAY)
    assert "現金等價物" not in text
    assert "❌ 低於紅線 $8,000" in text


def test_payout_unset_rules() -> None:
    unset = _field(_build(_ctx(cash=0.0), nav_data=_nav(0.0)), _RUNWAY)
    assert "可動用現金 --（未設定現金儲備）" in unset
    only_eq = _field(_build(_ctx(cash=0.0), nav_data=_nav(20000.0)), _RUNWAY)
    assert "未設定現金儲備" not in only_eq and "可動用現金 $20,000" in only_eq
    no_nav = _field(_build(_ctx(cash=5000.0), nav_data=None), _RUNWAY)
    assert "可動用現金 $5,000" in no_nav and "現金等價物" not in no_nav


def test_nav_line_has_no_mode_text() -> None:
    text = _field(_build(_ctx(), nav_data={"nav": 1234.0}), _RUNWAY)
    assert "實戰模式" not in text and "觀戰模式" not in text
    assert "總資產 (NAV): $1,234 (按市價)" in text
    none_text = _field(_build(_ctx(), nav_data=None), _RUNWAY)
    assert "總資產 (NAV): --" in none_text
    assert "模式" not in none_text


@pytest.mark.asyncio
async def test_get_market_nav_cash_equivalent_value() -> None:
    from services.trading_service import TradingService

    def _h(sym: str, qty: float) -> MagicMock:
        h = MagicMock()
        h.symbol = sym
        h.metadata = {"quantity": qty}
        return h

    prices = {"BOXX": 110.0, "SGOV": 100.0, "BIL": 0.0, "SHV": 110.0, "AAPL": 200.0}

    async def _quote(sym: str, *a: Any, **k: Any) -> Dict[str, Any]:
        return {"c": prices.get(sym, 0.0)}

    holdings = [
        _h("BOXX", 100),
        _h("SGOV", 10),
        _h("BIL", 50),
        _h("SHV", -5),
        _h("AAPL", 10),
    ]
    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=holdings),
        patch("services.market_data_service.get_quote", side_effect=_quote),
    ):
        nav = await TradingService(MagicMock()).get_market_nav(
            1, 1000.0, {"total_option_market_value": 0.0, "missing_quote_count": 0}
        )
    # BOXX 11,000 + SGOV 1,000；BIL 無報價、SHV 為空頭皆不計入
    assert nav["cash_equivalent_value"] == pytest.approx(12000.0)
    assert nav["spot_market_value"] == pytest.approx(12000.0 + 2000.0 - 550.0)
