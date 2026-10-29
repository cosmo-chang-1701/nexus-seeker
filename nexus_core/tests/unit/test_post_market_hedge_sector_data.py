"""盤後報告：對沖歸因（真實部位）與板塊輪動缺值語意測試。"""

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.hedging import analyze_hedge_performance

_REAL_TRADES = [
    (1, "AAA", "call", 100.0, "2026-12-18", 2.0, 1, 0.0, 50.0, 0.0, 0.0, "SPECULATIVE"),
    (2, "SPY", "put", 500.0, "2026-12-18", 1.0, 2, 0.0, -30.0, 0.0, 0.0, "HEDGE"),
    (3, "BBB", "call", 50.0, "2026-12-18", 1.0, 1, 0.0, 10.0, 0.0, 0.0, "SPECULATIVE"),
]
_VIRTUAL_TRADES = [
    {
        "symbol": "CCC",
        "opt_type": "call",
        "strike": 10.0,
        "expiry": "2026-12-18",
        "entry_price": 1.0,
        "quantity": 1,
        "weighted_delta": 5.0,
        "trade_category": "SPECULATIVE",
    }
]
_OPT_PRICES = {"AAA": 3.0, "SPY": 0.5, "BBB": 0.0, "CCC": 1.5}


async def _fake_chain(symbol: str, *_args: Any) -> tuple[float, float, float, float]:
    return (_OPT_PRICES[symbol], 0.3, 0.0, 0.0)


def _hedge_patches(
    holdings: list[dict[str, Any]] | None = None,
    quotes: dict[str, float] | None = None,
) -> Any:
    quotes = quotes or {}

    async def _fake_quote(symbol: str, *_a: Any, **_k: Any) -> dict[str, float]:
        if symbol not in quotes:
            raise RuntimeError("no quote")
        return {"c": quotes[symbol]}

    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("database.portfolio.get_user_portfolio", return_value=_REAL_TRADES)
    )
    stack.enter_context(
        patch(
            "database.virtual_trading.get_open_virtual_trades",
            return_value=_VIRTUAL_TRADES,
        )
    )
    stack.enter_context(
        patch("database.holdings.get_user_holdings", return_value=holdings or [])
    )
    stack.enter_context(
        patch("market_analysis.portfolio.get_option_chain_mid_iv", _fake_chain)
    )
    stack.enter_context(patch("services.market_data_service.get_quote", _fake_quote))
    return stack


@pytest.mark.asyncio
async def test_default_call_characterization() -> None:
    with _hedge_patches():
        r = await analyze_hedge_performance(1)
    assert r["net_pnl"] == 50.0
    assert r["alpha_contribution"] == 150.0
    assert r["hedge_contribution"] == -100.0
    assert r["hedge_ratio"] == 0.4615
    assert r["effectiveness"] == 0.6667
    assert r["status"] == "UNDER_HEDGED"
    assert r["unpriced_count"] == 1
    assert r["scope"] == "real_and_virtual"


@pytest.mark.asyncio
async def test_exclude_virtual() -> None:
    with _hedge_patches():
        r = await analyze_hedge_performance(1, include_virtual=False)
    # 少了 CCC：alpha = 100（AAA），delta 60（含未報價 BBB）
    assert r["alpha_contribution"] == 100.0
    assert r["net_pnl"] == 0.0
    assert r["scope"] == "real_positions_only"
    assert r["unpriced_count"] == 1


@pytest.mark.asyncio
async def test_include_holdings_long_short_and_unpriced() -> None:
    holdings = [
        {"symbol": "LNG", "quantity": 10, "avg_cost": 100.0, "weighted_delta": 10.0},
        {"symbol": "SHT", "quantity": -5, "avg_cost": 50.0, "weighted_delta": -5.0},
        {"symbol": "ZER", "quantity": 0, "avg_cost": 1.0, "weighted_delta": 0.0},
        {"symbol": "NOQ", "quantity": 3, "avg_cost": 1.0, "weighted_delta": 3.0},
    ]
    quotes = {"LNG": 110.0, "SHT": 40.0, "ZER": 9.0}
    with _hedge_patches(holdings, quotes):
        r = await analyze_hedge_performance(
            1, include_virtual=False, include_holdings=True
        )
    # alpha = 100(AAA) + 100(LNG) + 50(SHT) = 250
    assert r["alpha_contribution"] == 250.0
    assert r["hedge_contribution"] == -100.0
    assert r["net_pnl"] == 150.0
    # 未報價：BBB 選擇權 + NOQ 持股；ZER 數量 0 略過
    assert r["unpriced_count"] == 2
    # alpha delta = 50+10 + 10 - 5 = 65；NOQ 的 delta 不計
    assert r["hedge_ratio"] == round(30.0 / 65.0, 4)


@pytest.mark.asyncio
async def test_holdings_quote_exception_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    holdings = [
        {"symbol": "BOOM", "quantity": 3, "avg_cost": 1.0, "weighted_delta": 3.0}
    ]
    with _hedge_patches(holdings, {}):
        with caplog.at_level(logging.WARNING, logger="market_analysis.hedging"):
            r = await analyze_hedge_performance(
                1, include_virtual=False, include_holdings=True
            )
    assert r["unpriced_count"] == 2
    assert r["uncosted_count"] == 0
    assert any(
        rec.levelno == logging.WARNING and "BOOM" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_holdings_missing_cost_excluded_from_pnl_keeps_delta() -> None:
    holdings = [
        {"symbol": "NOC", "quantity": 100, "avg_cost": None, "weighted_delta": -20.0},
        {"symbol": "ZRC", "quantity": 100, "avg_cost": 0, "weighted_delta": -10.0},
    ]
    with _hedge_patches(holdings, {"NOC": 500.0, "ZRC": 500.0}):
        r = await analyze_hedge_performance(
            1, include_virtual=False, include_holdings=True
        )
    assert r["uncosted_count"] == 2
    assert r["unpriced_count"] == 1  # 僅 BBB 選擇權
    assert r["alpha_contribution"] == 100.0
    # alpha delta = 50 + 10 - 20 - 10 = 30；hedge delta = -30
    assert r["hedge_ratio"] == 1.0
    assert r["status"] == "OPTIMAL"


@pytest.mark.asyncio
async def test_holdings_normal_cost_unchanged() -> None:
    holdings = [
        {"symbol": "LNG", "quantity": 10, "avg_cost": 100.0, "weighted_delta": 10.0}
    ]
    with _hedge_patches(holdings, {"LNG": 110.0}):
        r = await analyze_hedge_performance(
            1, include_virtual=False, include_holdings=True
        )
    assert r["alpha_contribution"] == 200.0
    assert r["uncosted_count"] == 0
    assert r["unpriced_count"] == 1


@pytest.mark.asyncio
async def test_holdings_nonpositive_quote_is_unpriced() -> None:
    holdings = [
        {"symbol": "NEG", "quantity": 5, "avg_cost": 10.0, "weighted_delta": 5.0}
    ]
    with _hedge_patches(holdings, {"NEG": 0.0}):
        r = await analyze_hedge_performance(
            1, include_virtual=False, include_holdings=True
        )
    assert r["unpriced_count"] == 2
    assert r["uncosted_count"] == 0


@pytest.mark.asyncio
async def test_holdings_not_loaded_by_default() -> None:
    holdings = [
        {"symbol": "LNG", "quantity": 10, "avg_cost": 100.0, "weighted_delta": 10.0}
    ]
    with _hedge_patches(holdings, {"LNG": 110.0}):
        r = await analyze_hedge_performance(1)
    assert r["alpha_contribution"] == 150.0
    assert r["uncosted_count"] == 0


def _hist(rows: int, last_vol: float = 200.0) -> pd.DataFrame:
    vols = [100.0] * (rows - 1) + [last_vol]
    return pd.DataFrame({"Close": [100.0 + i for i in range(rows)], "Volume": vols})


async def _gather(history: Any, skew: Any, uoa: Any) -> dict[str, Any]:
    from market_analysis.analyst_runners import sector_runner as sr

    with (
        patch.object(sr, "get_macro_environment", AsyncMock(return_value={"vix": 20})),
        patch.object(sr, "get_quote", AsyncMock(return_value={"c": 500})),
        patch.object(sr, "get_history_df", history),
        patch.object(sr.SentimentEngine, "calculate_skew", skew),
        patch.object(sr.SentimentEngine, "detect_uoa", uoa),
        patch.object(
            sr.SentimentEngine, "calculate_max_pain", AsyncMock(return_value={})
        ),
        patch.object(sr, "_fetch_poly_events", AsyncMock(return_value=[])),
    ):
        return await sr.gather_sector_rotation_data(object())


@pytest.mark.asyncio
async def test_sector_skew_uoa_exception_none() -> None:
    data = await _gather(
        AsyncMock(return_value=_hist(30)),
        AsyncMock(side_effect=RuntimeError("x")),
        AsyncMock(side_effect=RuntimeError("y")),
    )
    row = data["sectors"][0]
    assert row["skew"] is None
    assert row["skew_state"] == "N/A"
    assert row["uoa_count"] is None
    assert row["pct_change"] is not None


@pytest.mark.asyncio
async def test_sector_failed_fetch_returns_none_row() -> None:
    data = await _gather(
        AsyncMock(side_effect=RuntimeError("boom")),
        AsyncMock(return_value={"skew": 1.0, "state": "x"}),
        AsyncMock(return_value=[]),
    )
    assert len(data["sectors"]) == 11
    for row in data["sectors"]:
        assert row["pct_change"] is None
        assert row["rel_vol"] is None
        assert row["uoa_count"] is None
        assert row["skew"] is None
        assert row["skew_state"] == "N/A"


@pytest.mark.asyncio
async def test_sector_empty_frame_returns_none_row() -> None:
    data = await _gather(
        AsyncMock(return_value=pd.DataFrame()),
        AsyncMock(return_value={"skew": 1.0, "state": "x"}),
        AsyncMock(return_value=[]),
    )
    assert all(r["pct_change"] is None for r in data["sectors"])
    assert len(data["sectors"]) == 11


@pytest.mark.asyncio
async def test_sector_rel_vol_excludes_today() -> None:
    data = await _gather(
        AsyncMock(return_value=_hist(30, 200.0)),
        AsyncMock(return_value={"skew": 1.0, "state": "x"}),
        AsyncMock(return_value=[1, 2]),
    )
    row = data["sectors"][0]
    assert row["rel_vol"] == 2.0
    assert row["uoa_count"] == 2


@pytest.mark.asyncio
async def test_sector_rel_vol_none_when_short() -> None:
    data = await _gather(
        AsyncMock(return_value=_hist(5)),
        AsyncMock(return_value={"skew": 1.0, "state": "x"}),
        AsyncMock(return_value=[]),
    )
    row = data["sectors"][0]
    assert row["rel_vol"] is None
    assert row["pct_change"] is not None


def test_sector_flow_embed_renders_none_row() -> None:
    from cogs.embed_builders.scan_embeds.earnings_and_sector_flow import (
        create_sector_flow_report_embed,
    )

    raw = {
        "vix": 20,
        "vix_tier_name": "x",
        "spy_price": 500,
        "sectors": [
            {
                "symbol": "XLK",
                "name": "Technology",
                "pct_change": None,
                "rel_vol": None,
                "skew": None,
                "skew_state": "N/A",
                "uoa_count": None,
            },
            {
                "symbol": "XLE",
                "name": "Energy",
                "pct_change": 1.2,
                "rel_vol": 1.0,
                "skew": 0.5,
                "skew_state": "x",
                "uoa_count": 3,
            },
        ],
        "poly_events": [],
        "spy_max_pain": {},
    }
    embed = create_sector_flow_report_embed("t", "report", raw)
    assert "操作錯誤" not in (embed.title or "")
    assert any("XLK" in (f.value or "") for f in embed.fields)
