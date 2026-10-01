"""
tests/unit/test_data_accuracy_golden_l.py

數據正確性修正 L1–L13 (低嚴重度) 的 golden 值測試。
每個測試寫出具體輸入與手算期望值，並在註解列出手算過程。
"""

from datetime import datetime, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


# ===========================================================================
# L1：無風險利率統一 (定價用 r 與 config.RISK_FREE_RATE 一致)
# ===========================================================================
def test_l1_calibration_microstructure_uses_config_risk_free_rate() -> None:
    import config
    from calibration import microstructure

    assert microstructure._RISK_FREE_RATE == config.RISK_FREE_RATE == 0.042


# ===========================================================================
# L3：analyze_symbol 的股息率取實際值 (ETF 不再寫死 1.5%)
# ===========================================================================
async def _analyze_dividend_passed(strict_value: Optional[float]) -> float:
    from market_analysis import strategy

    captured: dict[str, float] = {}

    async def _opt_chain(*args: Any, **_k: Any) -> tuple:
        captured["q"] = args[-1]
        return None, None

    dates = pd.date_range(end="2026-08-19", periods=60, freq="D")
    df = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(60)], "Volume": [1000] * 60},
        index=dates,
    )
    expiry = (datetime.now().date() + timedelta(days=40)).strftime("%Y-%m-%d")
    indicators = {
        "price": 105.0,
        "rsi": 55.0,
        "sma20": 100.0,
        "macd_hist": 0.5,
        "hv_current": 0.3,
        "hv_rank": 40.0,
    }
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "services.market_data_service.is_etf",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df,
    ), patch(
        "services.market_data_service.get_dividend_yield_strict",
        new_callable=AsyncMock,
        return_value=strict_value,
    ), patch(
        "market_analysis.strategy._calculate_technical_indicators",
        return_value=indicators,
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[expiry],
    ), patch(
        "market_analysis.strategy._fetch_opt_chain_and_best_contract",
        new_callable=AsyncMock,
        side_effect=_opt_chain,
    ), patch(
        "market_analysis.strategy.evaluate_ema_trend",
        new_callable=AsyncMock,
        return_value={
            "trend": "N",
            "ema_8": 1.0,
            "ema_21": 1.0,
            "distance_from_21": 0.0,
        },
    ), patch(
        "market_analysis.strategy._calculate_mmm",
        new_callable=AsyncMock,
        return_value=(0.0, 0.0, 0.0, -1),
    ), patch(
        "market_analysis.strategy._calculate_term_structure",
        new_callable=AsyncMock,
        return_value=(1.0, "平滑 (Flat)"),
    ):
        result = await strategy.analyze_symbol("QQQ", vix_spot=15.0)
    assert result is None  # best_contract=None 提前 return
    return captured["q"]


@pytest.mark.asyncio
async def test_l3_etf_dividend_yield_uses_actual_value() -> None:
    # ETF 實際近 12 個月殖利率 0.6% → 選約 BSM 收到 0.006 (舊版一律 0.015)
    assert await _analyze_dividend_passed(0.006) == 0.006


@pytest.mark.asyncio
async def test_l3_unknown_dividend_yield_falls_back_to_zero() -> None:
    assert await _analyze_dividend_passed(None) == 0.0


# ===========================================================================
# L4：帳戶全空時資金不以 $100,000 冒充
# ===========================================================================
def test_l4_empty_account_capital_floors_to_one() -> None:
    from unittest.mock import MagicMock

    from database.user_settings import calculate_auto_capital

    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = []  # 無持倉
    conn.cursor.return_value.fetchone.return_value = (0.0,)  # 現金儲備 0
    # 舊版回傳 100000.0；現在落到 1.0 下限，部位建議歸零
    assert calculate_auto_capital(0, conn=conn) == 1.0


def test_l4_cash_only_account_capital_is_cash_reserve() -> None:
    from unittest.mock import MagicMock

    from database.user_settings import calculate_auto_capital

    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = []
    conn.cursor.return_value.fetchone.return_value = (25_000.0,)
    assert calculate_auto_capital(0, conn=conn) == 25_000.0


# ===========================================================================
# L6：PCR 分母為 0 不以哨兵值寫入 sentiment_history
# ===========================================================================
def _pcr_chain(call_vol: float, put_vol: float, call_oi: float, put_oi: float) -> Any:
    class _Chain:
        calls = pd.DataFrame({"volume": [call_vol], "openInterest": [call_oi]})
        puts = pd.DataFrame({"volume": [put_vol], "openInterest": [put_oi]})

    return _Chain()


async def _run_pcr(chain: Any, last_stored: Optional[float]) -> tuple[dict, AsyncMock]:
    from market_analysis.sentiment_engine import SentimentEngine

    with patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=["2026-06-19"],
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ), patch(
        "market_analysis.sentiment.options_flow.get_last_stored_sentiment",
        return_value=last_stored,
    ), patch(
        "market_analysis.sentiment.options_flow.save_sentiment_history",
        new_callable=AsyncMock,
    ) as mock_save:
        res = await SentimentEngine.calculate_pcr("AAPL")
    return res, mock_save


@pytest.mark.asyncio
async def test_l6_premarket_zero_volume_does_not_store_balanced_sentinel() -> None:
    # 盤前：成交量 0/0、OI put 3000 / call 2000 → oi_pcr = 1.5
    res, mock_save = await _run_pcr(_pcr_chain(0.0, 0.0, 2000.0, 3000.0), None)
    assert res["volume_pcr"] is None  # 舊版 1.0 (平衡) 並寫入歷史
    assert res["oi_pcr"] == 1.5
    assert res["oi_pcr_state"] == "🐻 結構防禦/偏向空頭"
    assert "error" not in res
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_l6_zero_volume_uses_stored_pcr_with_cache_label() -> None:
    res, mock_save = await _run_pcr(_pcr_chain(0.0, 0.0, 2000.0, 3000.0), 0.8)
    assert res["volume_pcr"] == 0.8
    assert res["state"].endswith("[歷史快取]")
    assert res["oi_pcr"] == 1.5
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_l6_normal_pcr_still_stored() -> None:
    # put 1200 / call 1000 = 1.2 → 寫入歷史
    res, mock_save = await _run_pcr(_pcr_chain(1000.0, 1200.0, 500.0, 500.0), None)
    assert res["volume_pcr"] == 1.2
    mock_save.assert_awaited_once_with("AAPL", "PCR", 1.2)


# ===========================================================================
# L7：持倉報告 IVR 未知顯示「--」(不顯示 0.0%)
# ===========================================================================
def _position_line(**kwargs: Any) -> str:
    from market_analysis.report_formatter import format_position_report

    base: dict[str, Any] = dict(
        symbol="NVDA",
        expiry="2026-12-18",
        strike=150.0,
        opt_type="call",
        cc_tag="",
        entry_price=5.0,
        current_price=6.0,
        pnl_pct=0.2,
        dte=78,
        spx_weighted_delta=12.5,
        status="HOLD",
    )
    base.update(kwargs)
    return format_position_report(**base)


def test_l7_unknown_ivr_renders_dashes() -> None:
    line = _position_line(iv=0.45, iv_rank=None)
    assert "IV/IVR: `45.0%/--`" in line
    assert "0.0%" not in line


def test_l7_ivr_default_is_unknown_not_zero() -> None:
    # 呼叫端沒傳 iv/iv_rank 時也是未知 (舊版預設 0.0 → 「--/0.0%」)
    assert "IV/IVR: `--/--`" in _position_line()


def test_l7_known_ivr_renders_value() -> None:
    assert "IV/IVR: `45.0%/62.5%`" in _position_line(iv=0.45, iv_rank=62.5)


# ===========================================================================
# L12：Covered Call 解鎖衰退閘門三值邏輯 (未知 fail-closed，不補常數)
# ===========================================================================
async def _cc_unlock(
    sahm: Any, us10y: Any, vix: Optional[float], core: Optional[dict] = None
) -> bool:
    from market_analysis import trading_orchestration as orch

    kv = {"macro_sahm_rule": sahm, "macro_us10y": None}
    with patch("database.get_kv_cache", side_effect=lambda k: kv.get(k)), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": us10y} if us10y is not None else {},
    ), patch(
        "services.market_data_service.get_vix_spot_strict",
        new_callable=AsyncMock,
        return_value=vix,
    ), patch(
        "market_analysis.index_microstructure.fetch_core_macro_metrics",
        new_callable=AsyncMock,
        return_value=core or {"_is_fallback": True},
    ):
        return await orch.is_covered_call_unlock_allowed()


@pytest.mark.asyncio
async def test_l12_known_non_recession_unlocks() -> None:
    # sahm 0.2 < 0.5；US10Y 4.1 ≤ 4.5 → 利率×VIX 條件確定為假 → 放行
    assert await _cc_unlock(0.2, 4.1, 25.0) is True


@pytest.mark.asyncio
async def test_l12_known_recession_blocks() -> None:
    assert await _cc_unlock(0.6, 4.1, 15.0) is False  # sahm ≥ 0.5
    assert await _cc_unlock(0.2, 4.8, 22.0) is False  # 4.8 > 4.5 且 22 > 20


@pytest.mark.asyncio
async def test_l12_unknown_sahm_fails_closed() -> None:
    # 舊版補 sahm 0.35 → 放行；現在 sahm 未知且利率×VIX 確定為假仍無法排除衰退
    assert await _cc_unlock(None, 4.1, 15.0) is False


@pytest.mark.asyncio
async def test_l12_unknown_vix_with_high_rate_fails_closed() -> None:
    # 舊版補 VIX 18 → 利率×VIX 為假 → 放行；現在 VIX 未知 + US10Y 4.8 無法判定
    assert await _cc_unlock(0.2, 4.8, None) is False


@pytest.mark.asyncio
async def test_l12_zero_or_negative_sahm_is_a_valid_reading() -> None:
    # 薩姆值 0 / 負值是有效讀值 (失業率處於低點)，不可當缺值而 fail-closed
    assert await _cc_unlock(0.0, 4.1, 15.0) is True
    assert await _cc_unlock(-0.1, 4.1, 15.0) is True


# ===========================================================================
# 逃頂窗口四因子矩陣：未知因子不計分 (不以 CPI 0 / WTI 75 / VTS 0.88 補值)
# ===========================================================================
def test_escape_window_unknown_factors_score_nothing() -> None:
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    # CPI / WTI / VTS 未知、正 Gamma
    t, e, direction, shift, _tier, _status = evaluate_escape_window_regime(
        prob=0.30, cpi_dev=None, wti=None, vts_ratio=None, is_negative_gamma=False
    )
    # Factor 1 鴿派 +1、Factor 4 正 Gamma +1 → 寬鬆 2 分 (舊版 4 分)
    assert (t, e) == (0, 2)
    assert direction == "後推"


def test_escape_window_one_known_hot_input_still_tightens() -> None:
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    # CPI 未知但 WTI 90 > 85 → Factor 2 收縮 +1；VTS 1.05 → +1 → 收縮 2 分前移
    t, e, direction, shift, _tier, _status = evaluate_escape_window_regime(
        prob=None, cpi_dev=None, wti=90.0, vts_ratio=1.05, is_negative_gamma=False
    )
    assert (t, e) == (2, 1)
    assert direction == "前移" and shift == 5


def test_escape_window_cpi_unknown_does_not_count_as_easing() -> None:
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    t, e, *_ = evaluate_escape_window_regime(
        prob=None, cpi_dev=None, wti=70.0, vts_ratio=None, is_negative_gamma=True
    )
    # WTI 70 平穩但 CPI 未知 → Factor 2 不計分；負 Gamma → 收縮 1
    assert (t, e) == (1, 0)


# ===========================================================================
# v089：清除 kv_cache 中仍停留在 v050 種子值的總經指標
# ===========================================================================
def test_v089_purges_only_untouched_seed_values() -> None:
    import sqlite3

    from database.migrations import v050_add_macro_cache as seed
    from database.migrations import v089_purge_seeded_macro_kv as m

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE kv_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.executescript(seed.sql)
        # 已被真實抓取覆寫的鍵必須保留
        conn.execute("UPDATE kv_cache SET value = '0.42' WHERE key = 'macro_sahm_rule'")
        conn.execute("UPDATE kv_cache SET value = '17.3' WHERE key = 'macro_vix'")
        conn.execute("INSERT INTO kv_cache (key, value) VALUES ('other_key', '0.35')")
        conn.executescript(m.sql)
        left = dict(conn.execute("SELECT key, value FROM kv_cache").fetchall())
    finally:
        conn.close()
    assert left == {"macro_sahm_rule": "0.42", "macro_vix": "17.3", "other_key": "0.35"}
    assert m.version == 89


# ===========================================================================
# L8：Max Pain 拆股校正只採計近期拆股
# ===========================================================================
def test_l8_old_splits_do_not_trigger_strike_adjustment() -> None:
    from market_analysis.sentiment.max_pain import _recent_split_factor

    # AAPL 式歷史：2:1 ×3、7:1、4:1 (乘積 224) 全在多年前 → 不校正
    idx = pd.DatetimeIndex(
        ["2000-06-21", "2005-02-28", "2014-06-09", "2020-08-31"], tz="America/New_York"
    )
    splits = pd.Series([2.0, 2.0, 7.0, 4.0], index=idx)
    now = pd.Timestamp("2026-10-01", tz="America/New_York")
    assert _recent_split_factor(splits, now) == 1.0


def test_l8_recent_split_factor_only_counts_lookback_window() -> None:
    from market_analysis.sentiment.max_pain import _recent_split_factor

    # 2020 年 4:1 (舊) + 10 天前 10:1 (近期) → 只取 10
    idx = pd.DatetimeIndex(["2020-08-31", "2026-09-21"], tz="America/New_York")
    splits = pd.Series([4.0, 10.0], index=idx)
    now = pd.Timestamp("2026-10-01", tz="America/New_York")
    assert _recent_split_factor(splits, now) == 10.0


# ===========================================================================
# L9：Gamma Squeeze 引擎的期權持倉讀 TRADE，Theta 未知不補值
# ===========================================================================
def test_l9_options_holdings_built_from_trade_positions() -> None:
    from market_analysis.intraday_pipeline.pipeline import IntradayScanPipeline

    positions = [
        # 賣 2 口 Put，部位 Theta +$30/日 → 單口 30 / (−2 × 100) = −0.15
        # (引擎再乘回 quantity × 100 = +30，方向與部位一致)
        {"symbol": "NVDA", "quantity": -2, "theta": 30.0},
        # 買 3 口 Call，部位 Theta −$45/日 → 單口 −0.15
        {"symbol": "AAPL", "quantity": 3, "theta": -45.0},
        # Theta 尚未刷新 (None) → 略過，不補 −0.05
        {"symbol": "MSFT", "quantity": 1, "theta": None},
    ]
    holdings = IntradayScanPipeline._build_options_holdings(positions)
    assert [(h.symbol, h.quantity, round(h.theta, 4)) for h in holdings] == [
        ("NVDA", -2.0, -0.15),
        ("AAPL", 3.0, -0.15),
    ]
    # 引擎的 Theta 覆蓋：Σ theta × qty × 100 = +30 − 45 = −15
    assert sum(h.theta * h.quantity * 100 for h in holdings) == pytest.approx(-15.0)


# ===========================================================================
# L10：yfinance 備援報價用未調整收盤價，時間戳不是午夜
# ===========================================================================
@pytest.mark.asyncio
async def test_l10_yfinance_quote_uses_unadjusted_prev_close() -> None:
    from services.market_data_service import quote as quote_mod

    idx = pd.DatetimeIndex(["2026-09-29", "2026-09-30"], tz="America/New_York")
    # 未調整：前收 100、現價 99 (除息日股息 1 元，股價開低)
    df = pd.DataFrame(
        {
            "Open": [99.5, 99.2],
            "High": [100.5, 99.8],
            "Low": [99.0, 98.6],
            "Close": [100.0, 99.0],
        },
        index=idx,
    )
    calls: list[dict] = []

    async def _fake_history(_ticker: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(kwargs)
        return df

    with patch.object(quote_mod, "_safe_yf_history", side_effect=_fake_history):
        q = await quote_mod.get_yfinance_quote("KO")

    assert calls[0]["auto_adjust"] is False
    # d = 99 − 100 = −1、dp = −1%
    assert (q["c"], q["pc"], q["d"], q["dp"]) == (99.0, 100.0, -1.0, -1.0)
    # 過去的 K 棒 → 該日 16:00 ET，而不是 00:00
    close_ts = int(pd.Timestamp("2026-09-30 16:00", tz="America/New_York").timestamp())
    assert q["t"] == close_ts


@pytest.mark.asyncio
async def test_l10_single_bar_prev_close_is_unknown() -> None:
    from services.market_data_service import quote as quote_mod

    idx = pd.DatetimeIndex(["2026-09-30"], tz="America/New_York")
    df = pd.DataFrame(
        {"Open": [20.0], "High": [22.0], "Low": [19.5], "Close": [21.0]}, index=idx
    )

    async def _fake_history(_ticker: Any, **_k: Any) -> pd.DataFrame:
        return df

    with patch.object(quote_mod, "_safe_yf_history", side_effect=_fake_history):
        q = await quote_mod.get_yfinance_quote("NEWIPO")

    # 舊版以開盤價 20 當前收 → dp +5%；現在前收未知
    assert q["c"] == 21.0
    assert q["pc"] is None and q["d"] is None and q["dp"] is None


def test_l10_intraday_bar_timestamp_is_now_not_midnight() -> None:
    from datetime import datetime as _dt

    import market_time
    from services.market_data_service.quote import _daily_bar_quote_timestamp

    now = _dt(2026, 9, 30, 11, 15, tzinfo=market_time.ny_tz)
    bar = pd.Timestamp("2026-09-30", tz="America/New_York")
    assert _daily_bar_quote_timestamp(bar, now) == int(now.timestamp())


# ===========================================================================
# L11：心跳 embed 在 metrics 缺失時不以 100.0 冒充現價與 POC
# ===========================================================================
def test_l11_heartbeat_without_metrics_shows_unknown_poc_and_distances() -> None:
    from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed

    embed = create_watchlist_signal_embed(
        "NVDA",
        metrics=None,
        suitable_buy_price=50.0,
        symbol_gex={"put_wall": 40.0, "call_wall": 60.0},
    )
    assert embed is not None
    text = "\n".join(str(f.value) for f in embed.fields)
    assert "Vol POC (籌碼控制中心): N/A" in text
    assert "$100.00" not in text
    # 現價未知 → 與牆的距離顯示 --%，不以建議買價 50 推算 (舊版 +25.00% / +20.00%)
    assert "(空間: --%)" in text
    assert "+25.00%" not in text and "+20.00%" not in text


# ===========================================================================
# L13：雷達 fast path 的 RVOL 在成交量未知時為 None (不冒充 0 =「無量」)
# ===========================================================================
def test_l13_rvol_unknown_when_quote_has_no_volume() -> None:
    from cogs.unified_terminal.radar_data import _compute_rvol

    # Finnhub /quote 沒有 volume 欄位 → quote.get("volume") 為 None
    assert _compute_rvol(None, 1_000_000.0) is None
    assert _compute_rvol(500_000.0, 0.0) is None  # 均量未知
    # 今日 1.5M / 20 日均量 1.0M = 1.5x
    assert _compute_rvol(1_500_000.0, 1_000_000.0) == pytest.approx(1.5)
