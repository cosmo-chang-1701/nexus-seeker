"""波動率優勢偵測：正典 IV 來源、fail-closed、財報判定、Theta 與 DTE 日期。"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from cogs.embed_builders._ansi_utils import _visual_len
from cogs.embed_builders.report_embeds import create_volatility_embed
from market_analysis.volatility_inspector import VolatilityInspector
from models.quant import IVMetrics

REPORT_KEYS = {
    "symbol",
    "price",
    "iv_current",
    "hv_current",
    "iv_rank",
    "is_opportunity",
    "is_high_risk_vol",
    "tte_hours",
    "strategy",
    "trigger_logic",
    "trend",
    "psq_signal",
    "runway_impact",
    "iv",
    "iv_p",
    "hv",
    "status",
    "days_to_earnings",
    "stop_loss",
    "daily_theta",
    "iv_source",
    "iv_history_count",
}


def _df() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 150.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 252)))
    dates = pd.date_range(end="2026-05-21", periods=252, freq="D")
    return pd.DataFrame(
        {
            "Close": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Volume": [1000] * 252,
        },
        index=dates,
    )


def _metrics(**kw: Any) -> IVMetrics:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "current_iv": 0.20,
        "iv_rank": 20.0,
        "iv_source": "LIVE_IV",
        "hv_20": 0.40,
        "iv_history_count": 70,
        "reference_spot_price": 175.0,
    }
    base.update(kw)
    return IVMetrics(**base)


def _earnings(tte: float, released: bool = False) -> Any:
    return SimpleNamespace(
        tte_hours=tte,
        is_released=released,
        is_imminent=0 < tte <= 24,
    )


def _ctx() -> Any:
    ctx = MagicMock()
    ctx.cash_reserve = 10000.0
    ctx.monthly_expense = 3000.0
    return ctx


def _patches(
    stack: ExitStack,
    metrics: IVMetrics,
    earnings: Any,
    expiries: list[str] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {}

    def p(name: str, target: str, **kw: Any) -> None:
        m[name] = stack.enter_context(patch(target, **kw))

    p("hist", "services.market_data_service.get_history_df", new_callable=AsyncMock)
    m["hist"].return_value = _df()
    p(
        "iv",
        "market_analysis.sentiment.SentimentEngine.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
    )
    m["iv"].return_value = metrics
    p(
        "earn",
        "services.calendar_service.calendar_service.get_symbol_earnings",
        new_callable=AsyncMock,
    )
    m["earn"].return_value = earnings
    p(
        "ema",
        "market_analysis.volatility_inspector.evaluate_ema_trend",
        new_callable=AsyncMock,
    )
    m["ema"].return_value = {"trend": "BULLISH_STRONG"}
    p("psq", "market_analysis.volatility_inspector.analyze_psq")
    m["psq"].return_value = SimpleNamespace(signal_direction="Long")
    p(
        "gex",
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
    )
    m["gex"].return_value = {"put_wall": 0.0}
    p(
        "exp",
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
    )
    m["exp"].return_value = expiries if expiries is not None else ["2099-12-31"]
    p(
        "sleep",
        "market_analysis.volatility_inspector.asyncio.sleep",
        new_callable=AsyncMock,
    )
    p("yf", "yfinance.Ticker")
    return m


@pytest.mark.asyncio
async def test_iv_rank_unknown_fails_closed() -> None:
    with ExitStack() as st:
        m = _patches(st, _metrics(iv_rank=None), _earnings(500.0))
        assert await VolatilityInspector().inspect_symbol("AAPL", _ctx()) is None
        m["ema"].assert_not_awaited()
        m["gex"].assert_not_awaited()
        m["exp"].assert_not_awaited()
        m["yf"].assert_not_called()


@pytest.mark.asyncio
async def test_non_live_iv_fails_closed() -> None:
    with ExitStack() as st:
        m = _patches(st, _metrics(iv_source="STORED_IV"), _earnings(500.0))
        assert await VolatilityInspector().inspect_symbol("AAPL", _ctx()) is None
        m["ema"].assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_opportunity_values_no_yahoo_info() -> None:
    with ExitStack() as st:
        m = _patches(st, _metrics(), _earnings(500.0))
        r = await VolatilityInspector().inspect_symbol("AAPL", _ctx())
        assert r is not None
        assert REPORT_KEYS <= set(r)
        assert r["is_opportunity"] is True
        assert r["is_high_risk_vol"] is False
        assert r["iv_rank"] == 20.0 and r["iv_p"] == 20.0
        assert r["iv_current"] == 20.0 and r["iv"] == 20.0
        assert r["hv_current"] == 40.0 and r["hv"] == 40.0
        assert r["price"] == 175.0
        assert r["iv_source"] == "LIVE_IV"
        assert r["iv_history_count"] == 70
        assert r["strategy"] == "單邊 Call (BTO)"
        m["yf"].assert_not_called()


@pytest.mark.asyncio
async def test_imminent_unreleased_high_risk_defensive() -> None:
    with ExitStack() as st:
        _patches(
            st,
            _metrics(iv_rank=85.0, current_iv=0.8),
            _earnings(12.0, released=False),
        )
        r = await VolatilityInspector().inspect_symbol("AAPL", _ctx())
        assert r is not None
        assert r["is_high_risk_vol"] is True
        assert r["strategy"] == "風險中性 (Risk Neutral / Defensive)"
        assert "高風險波動率事件" in r["trigger_logic"]


@pytest.mark.asyncio
async def test_released_earnings_no_high_risk() -> None:
    with ExitStack() as st:
        _patches(
            st,
            _metrics(iv_rank=85.0, current_iv=0.8),
            _earnings(-3.0, released=True),
        )
        assert await VolatilityInspector().inspect_symbol("AAPL", _ctx()) is None


@pytest.mark.asyncio
async def test_theta_per_contract_reference() -> None:
    with ExitStack() as st:
        _patches(
            st,
            _metrics(current_iv=0.30, reference_spot_price=100.0),
            _earnings(500.0),
        )
        r = await VolatilityInspector().inspect_symbol("AAPL", _ctx())
        assert r is not None
        assert r["daily_theta"] == pytest.approx(6.27, abs=0.02)


@pytest.mark.asyncio
async def test_dte_uses_eastern_date() -> None:
    frozen = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)  # 美東仍為 10/21

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    assert frozen.astimezone(ZoneInfo("America/New_York")).date().day == 21
    with ExitStack() as st:
        # 到期日 10/21：以美東日期 DTE=0 (<=1 觸發額外停損緩衝)；以 UTC 日期則為 -1 被忽略 (DTE=99)
        m = _patches(st, _metrics(), _earnings(500.0), expiries=["2026-10-21"])
        st.enter_context(
            patch("market_analysis.volatility_inspector.datetime", _FakeDT)
        )
        m["gex"].return_value = {"put_wall": 100.0}
        with patch("pandas_ta.atr") as atr:
            atr.return_value = pd.Series([2.0])
            r = await VolatilityInspector().inspect_symbol("AAPL", _ctx())
        assert r is not None
        # base = 100 - 1.5*2 = 97；DTE<=1 再扣 3 => 94
        assert r["stop_loss"] == pytest.approx(94.0)


def test_volatility_embed_first_column_labels_fit() -> None:
    report = {
        "symbol": "AAPL",
        "price": 175.0,
        "iv": 20.0,
        "iv_p": 20.0,
        "hv": 40.0,
        "status": "波動率極低",
        "strategy": "單邊 Call (BTO)",
        "trigger_logic": "x",
        "days_to_earnings": 20.0,
        "stop_loss": 160.0,
        "daily_theta": 6.27,
        "runway_impact": 0.5,
    }
    embed = create_volatility_embed(report)
    widths = {0: 20, 2: 22}
    texts = [f.value or "" for f in embed.fields]
    joined = "\n".join(texts)
    assert "HV20 (20 日)" in joined
    assert "IV / IV Rank" in joined
    assert "Theta/日(30DTE每口)" in joined
    for idx, w in widths.items():
        rows = (embed.fields[idx].value or "").splitlines()[3:-1]
        for row in rows:
            label = row.split(" | ")[0].rstrip()
            assert _visual_len(label) <= w, label
