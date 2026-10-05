"""單元測試：央行淨流動性與體制分類服務 (liquidity_service.py)。"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from market_analysis.macro_signals import Observation
from services.liquidity_service import (
    _find_prior_obs,
    compute_net_liquidity_series,
    run_liquidity_pipeline,
)


def test_find_prior_obs() -> None:
    """測試在指定目標日期周遭尋找最貼近的歷史觀測值。"""
    t0 = date(2026, 1, 10)
    obs = [
        Observation(date(2026, 1, 1), 10.0, date(2026, 1, 2)),
        Observation(date(2026, 1, 8), 12.0, date(2026, 1, 9)),
        Observation(date(2026, 1, 15), 14.0, date(2026, 1, 16)),
    ]

    # 目標日 2026-01-10，差距最近的是 2026-01-08 (2天)，應回傳 12.0
    val = _find_prior_obs(obs, t0, window_days=5)
    assert val == 12.0

    # 若超出容許視窗，應回傳 None
    val_none = _find_prior_obs(obs, date(2026, 2, 20), window_days=5)
    assert val_none is None


def test_compute_net_liquidity_series() -> None:
    """測試 WALCL、WTREGEN、RRPONTSYD 對齊計算淨流動性。"""
    d1 = date(2026, 1, 7)
    d2 = date(2026, 1, 14)

    walcl_obs = [
        Observation(d1, 7_000_000.0, d1 + timedelta(days=2)),
        Observation(d2, 7_100_000.0, d2 + timedelta(days=2)),
    ]
    wtregen_obs = [
        Observation(d1, 800_000.0, d1 + timedelta(days=2)),
        Observation(d2, 850_000.0, d2 + timedelta(days=2)),
    ]
    rrp_obs = [
        Observation(d1, 200.0, d1 + timedelta(days=1)),
        Observation(d2, 220.0, d2 + timedelta(days=1)),
    ]

    series = compute_net_liquidity_series(walcl_obs, wtregen_obs, rrp_obs)
    assert len(series) == 2
    # d1: 7000 - 800 - 200 = 6000
    assert pytest.approx(series[0].value, 1e-4) == 6000.0
    # d2: 7100 - 850 - 220 = 6030
    assert pytest.approx(series[1].value, 1e-4) == 6030.0


@pytest.mark.asyncio
async def test_run_liquidity_pipeline_mocked() -> None:
    """測試流動性管線執行與各指標整合計算。"""
    today = date(2026, 10, 5)

    def mock_fetch(sid: str, t: date, kind: str | None = None) -> list[Observation]:
        # 提供充足長度之觀測值
        avail = today - timedelta(days=1)
        if sid == "NFCI":
            return [Observation(today - timedelta(days=7), -0.58, avail)]
        if sid == "ANFCI":
            return [Observation(today - timedelta(days=7), -0.60, avail)]
        if sid == "DGS10":
            return [Observation(today - timedelta(days=1), 4.15, avail)]
        if sid == "WALCL":
            return [
                Observation(today - timedelta(days=98), 6_800_000.0, avail),
                Observation(today - timedelta(days=7), 7_000_000.0, avail),
            ]
        if sid == "WTREGEN":
            return [
                Observation(today - timedelta(days=98), 800_000.0, avail),
                Observation(today - timedelta(days=7), 800_000.0, avail),
            ]
        if sid == "RRPONTSYD":
            return [
                Observation(today - timedelta(days=98), 200.0, avail),
                Observation(today - timedelta(days=7), 200.0, avail),
            ]
        if sid == "WRESBAL":
            return [
                Observation(today - timedelta(days=98), 3_000_000.0, avail),
                Observation(today - timedelta(days=7), 3_150_000.0, avail),
            ]
        return []

    with patch(
        "services.liquidity_service.fetch_fred_series", side_effect=mock_fetch
    ), patch(
        "services.liquidity_service.store_observations", new_callable=AsyncMock
    ), patch(
        "services.liquidity_service.save_liquidity_regime", new_callable=AsyncMock
    ) as mock_save:
        reading = await run_liquidity_pipeline(today)

        assert reading.trading_date == today
        assert reading.nfci == -0.58
        assert reading.anfci == -0.60
        assert reading.us10y == 4.15
        assert reading.net_liquidity_bn == 6000.0
        # 6000 vs 5800 -> +3.448%
        assert reading.net_liquidity_chg_13w_pct is not None
        assert reading.net_liquidity_chg_13w_pct > 0.0
        # NFCI <= -0.5 且 13w >= 0 -> EASY
        assert reading.regime == "EASY"
        assert reading.equity_risk_premium is not None
        mock_save.assert_awaited_once()


def test_compute_net_liquidity_series_unsorted_and_available_date() -> None:
    """測試 WTREGEN 順序混亂時能正確對齊，且複合可用日取所有組件的最大可用日。"""
    d1 = date(2026, 1, 7)
    d2 = date(2026, 1, 14)

    walcl_obs = [
        Observation(d1, 7_000_000.0, d1 + timedelta(days=2)),
        Observation(d2, 7_100_000.0, d2 + timedelta(days=2)),
    ]
    # 故意倒序傳入 WTREGEN
    wtregen_obs = [
        Observation(d2, 850_000.0, d2 + timedelta(days=3)),
        Observation(d1, 800_000.0, d1 + timedelta(days=4)),
    ]
    rrp_obs = [
        Observation(d1, 200.0, d1 + timedelta(days=1)),
        Observation(d2, 220.0, d2 + timedelta(days=5)),
    ]

    series = compute_net_liquidity_series(walcl_obs, wtregen_obs, rrp_obs)
    assert len(series) == 2
    # 對齊 d1: 7000 - 800 - 200 = 6000
    assert pytest.approx(series[0].value, 1e-4) == 6000.0
    # available_date 應為 max(d1+2, d1+4, d1+1) = d1+4
    assert series[0].available_date == d1 + timedelta(days=4)

    # 對齊 d2: 7100 - 850 - 220 = 6030
    assert pytest.approx(series[1].value, 1e-4) == 6030.0
    # available_date 應為 max(d2+2, d2+3, d2+5) = d2+5
    assert series[1].available_date == d2 + timedelta(days=5)


@pytest.mark.asyncio
async def test_run_liquidity_pipeline_missing_history_is_unknown() -> None:
    """測試歷史樣本不足以計算 13w 變更率時，淨流動性季增率為 None 且體制降級為 UNKNOWN。"""
    today = date(2026, 10, 5)

    def mock_fetch_short(
        sid: str, t: date, kind: str | None = None
    ) -> list[Observation]:
        avail = today - timedelta(days=1)
        # 僅提供當週資料，無 13 週前基期
        if sid == "NFCI":
            return [Observation(today - timedelta(days=7), -0.65, avail)]
        if sid == "WALCL":
            return [Observation(today - timedelta(days=7), 7_000_000.0, avail)]
        if sid == "WTREGEN":
            return [Observation(today - timedelta(days=7), 800_000.0, avail)]
        if sid == "RRPONTSYD":
            return [Observation(today - timedelta(days=7), 200.0, avail)]
        return []

    with patch(
        "services.liquidity_service.fetch_fred_series", side_effect=mock_fetch_short
    ), patch(
        "services.liquidity_service.store_observations", new_callable=AsyncMock
    ), patch(
        "services.liquidity_service.save_liquidity_regime", new_callable=AsyncMock
    ):
        reading = await run_liquidity_pipeline(today)
        # 季增率必須為 None，不得被強制降級或短路為 0.0
        assert reading.net_liquidity_chg_13w_pct is None
        # 關鍵指標缺失時，體制必須判定為 UNKNOWN (而非因 0.0 >= 0 誤判為 EASY)
        assert reading.regime == "UNKNOWN"
