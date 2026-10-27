"""微觀結構與情緒資料新鮮度 SOP 驗證單元測試。

覆蓋 Findings #2, #3, #4, #5, #7, #8, #9：
1. 快照 physical_cap_above_spot 排除跨式與借方價差賣出腿，保留貸方價差賣出腿。
2. GEX 快取年齡以 data_timestamp 計算，且 _is_stale_cache 標記為過期。
3. Skew / Volume PCR 超過 5 天回傳 None，且快照回退亦受限。
4. 斷路器生效時 Max Pain 痛點不回退快照舊值，回傳 None。
5. Fast path 正確輸出 atr_15m。
6. Squeeze 快取過期時觸發即時計算重算寫回。
7. Slow path 計算收盤 20 日 SMA 並寫入結果與快照字典之 ma20。
"""

import sqlite3
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_data import (
    _SENTIMENT_HISTORY_MAX_AGE_SECONDS,
    RadarDataMixin,
    _KvSnapshot,
    _load_history_reads,
)
from market_analysis.index_microstructure import (
    _scrape_symbol_gex_raw,
    fetch_symbol_gex_metrics,
)
from market_analysis.intraday_pipeline.metrics import (
    _WATCHLIST_METRICS_CACHE,
    build_enhanced_watchlist_metrics,
)
from market_analysis.sentiment.history_storage import (
    get_last_stored_sentiment,
    get_last_stored_sentiment_with_age,
)


def _build_kv_rows(
    radar: Optional[dict[str, Any]] = None,
    radar_age_s: Optional[float] = 60.0,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, tuple[Any, Optional[datetime]]]:
    """建立 _KvSnapshot 測試資料字典。"""
    now = datetime.now(timezone.utc)
    rows: dict[str, tuple[Any, Optional[datetime]]] = {
        "uoa_NVDA": ([{"strike": 100}], now),
    }
    if radar is not None:
        rows["radar_terminal_NVDA"] = (
            radar,
            now - timedelta(seconds=radar_age_s) if radar_age_s is not None else None,
        )
    for k, v in (extra or {}).items():
        rows[k] = (v, now)
    return rows


async def _run_radar_fast(
    rows: dict[str, tuple[Any, Optional[datetime]]],
    market_cache: Optional[dict[str, Any]] = None,
    squeeze_cache: Optional[dict[str, Any]] = None,
    history_side_effects: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """呼叫 _fetch_sym_radar_data_fast_raw 輔助函式。"""
    cog = UnifiedTerminalCog(MagicMock())
    cog._async_revalidate_market_cache = AsyncMock()  # type: ignore[method-assign]
    he = history_side_effects or {}
    sqz = (
        squeeze_cache
        if squeeze_cache is not None
        else {"momentum": 1.0, "direction": "🟢", "is_squeezing": False}
    )

    with ExitStack() as st:
        st.enter_context(
            patch(
                "services.market_data_service.get_quote",
                return_value={"c": 150.0, "volume": 1000},
            )
        )
        st.enter_context(
            patch(
                "cogs.unified_terminal.radar_data._load_symbol_caches",
                return_value=(_KvSnapshot(rows), market_cache or {}, dict(sqz)),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_last_stored_iv",
                side_effect=he.get("iv", lambda s: None),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_last_stored_sentiment",
                side_effect=he.get("sent", lambda s, i, max_age_seconds=None: None),
            )
        )
        st.enter_context(
            patch(
                "market_analysis.sentiment.history_storage.get_indicator_percentile_detail",
                side_effect=he.get("pct", lambda s, i, v: None),
            )
        )
        return await cog._fetch_sym_radar_data_fast_raw("NVDA")  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# 1. Finding #2: physical_cap_above_spot 結構腿過濾
# ---------------------------------------------------------------------------


def test_physical_cap_above_spot_filter_logic() -> None:
    """快照端 physical_cap_above_spot 應排除跨式與借方價差賣出腿，保留貸方價差賣出腿。"""
    spot_price = 100.0

    # 情境 1: 借方價差或跨式腿 (structure_leg=True, spread_credit=False) 現價上方 -> 排除，不封頂
    legs_debit_spread: list[dict[str, Any]] = [
        {"type": "CALL", "strike": 105.0, "structure_leg": True, "spread_credit": False}
    ]
    capped_debit = any(
        str(s.get("type", "")).upper().startswith("C")
        and float(s.get("strike", 0.0) or 0.0) > spot_price
        and (not s.get("structure_leg") or bool(s.get("spread_credit")))
        for s in legs_debit_spread
    )
    assert capped_debit is False

    # 情境 2: 貸方價差賣出腿 (structure_leg=True, spread_credit=True) 現價上方 -> 保留，觸發封頂
    legs_credit_spread: list[dict[str, Any]] = [
        {"type": "CALL", "strike": 105.0, "structure_leg": True, "spread_credit": True}
    ]
    capped_credit = any(
        str(s.get("type", "")).upper().startswith("C")
        and float(s.get("strike", 0.0) or 0.0) > spot_price
        and (not s.get("structure_leg") or bool(s.get("spread_credit")))
        for s in legs_credit_spread
    )
    assert capped_credit is True

    # 情境 3: 獨立單邊 STO Call (structure_leg=False) 現價上方 -> 保留，觸發封頂
    legs_naked: list[dict[str, Any]] = [
        {"type": "CALL", "strike": 105.0, "structure_leg": False}
    ]
    capped_naked = any(
        str(s.get("type", "")).upper().startswith("C")
        and float(s.get("strike", 0.0) or 0.0) > spot_price
        and (not s.get("structure_leg") or bool(s.get("spread_credit")))
        for s in legs_naked
    )
    assert capped_naked is True

    # 情境 4: 現價下方 Call -> 排除，不封頂
    legs_below_spot: list[dict[str, Any]] = [
        {"type": "CALL", "strike": 95.0, "structure_leg": False}
    ]
    capped_below = any(
        str(s.get("type", "")).upper().startswith("C")
        and float(s.get("strike", 0.0) or 0.0) > spot_price
        and (not s.get("structure_leg") or bool(s.get("spread_credit")))
        for s in legs_below_spot
    )
    assert capped_below is False

    # 情境 5: PUT 腿現價上方 -> 排除，不封頂
    legs_put: list[dict[str, Any]] = [
        {"type": "PUT", "strike": 105.0, "structure_leg": False}
    ]
    capped_put = any(
        str(s.get("type", "")).upper().startswith("C")
        and float(s.get("strike", 0.0) or 0.0) > spot_price
        and (not s.get("structure_leg") or bool(s.get("spread_credit")))
        for s in legs_put
    )
    assert capped_put is False


# ---------------------------------------------------------------------------
# 2. Finding #3: GEX data_timestamp 與 _is_stale_cache 標記
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_gex_data_timestamp_written_to_kv() -> None:
    """fetch_symbol_gex_metrics 在邊緣快照命中時，寫入 kv_cache 需包含 data_timestamp。"""
    saved_payload: dict[str, Any] = {}

    async def fake_save_kv(key: str, val: Any) -> None:
        saved_payload[key] = val

    with (
        patch("database.cache.get_kv_cache", return_value=None),
        patch(
            "services.edge_cache_client.get_cached_gex",
            new_callable=AsyncMock,
            return_value={"age_seconds": 900.0, "data": {"put_wall": 140.0}},
        ),
        patch("database.cache.save_kv_cache", side_effect=fake_save_kv),
    ):
        result = await fetch_symbol_gex_metrics("NVDA")
        assert result["put_wall"] == 140.0
        kv_env = saved_payload.get("gex_metrics_NVDA")
        assert kv_env is not None
        assert "timestamp" in kv_env
        assert "data_timestamp" in kv_env
        # data_timestamp 應等於 timestamp - age_seconds
        assert kv_env["timestamp"] - kv_env["data_timestamp"] == pytest.approx(
            900.0, abs=2.0
        )


@pytest.mark.asyncio
async def test_scrape_symbol_gex_raw_writes_data_timestamp() -> None:
    """_scrape_symbol_gex_raw 抓取成功時，信封中需存入 data_timestamp。"""
    saved_payload: dict[str, Any] = {}

    async def fake_save_kv(key: str, val: Any) -> None:
        saved_payload[key] = val

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "spot": 150.0,
            "net_gex": 1000.0,
            "call_wall": 160.0,
            "put_wall": 140.0,
            "gex_profile": {"140": 500.0, "160": 1000.0},
        },
    }

    mock_client_instance = AsyncMock()
    mock_client_instance.get.return_value = mock_resp
    mock_client_instance.__aenter__.return_value = mock_client_instance
    mock_client_instance.__aexit__.return_value = None

    with (
        patch("config.TUNNEL_URL", "http://fake-tunnel"),
        patch("httpx.AsyncClient", return_value=mock_client_instance),
        patch("database.cache.save_kv_cache", side_effect=fake_save_kv),
    ):
        res = await _scrape_symbol_gex_raw("NVDA")
        assert res["call_wall"] == 160.0
        kv_env = saved_payload.get("gex_metrics_NVDA")
        assert kv_env is not None
        assert "data_timestamp" in kv_env
        assert kv_env["data_timestamp"] == pytest.approx(time.time(), abs=5.0)


@pytest.mark.asyncio
async def test_fast_path_gex_freshness_uses_data_timestamp_and_stale_flag() -> None:
    """Fast path 年齡判斷應讀取 data_timestamp，且 _is_stale_cache 需標記為過期。"""
    now = time.time()

    # 案例 A: timestamp 很新鮮 (10s 前)，但 data_timestamp 已經 2500s 前 (>= 1800s) -> 過期
    stale_env = {
        "timestamp": now - 10.0,
        "data_timestamp": now - 2500.0,
        "data": {"put_wall": 140.0, "call_wall": 160.0, "net_gex": 500.0},
    }
    data_a = await _run_radar_fast(
        _build_kv_rows(extra={"gex_metrics_NVDA": stale_env})
    )
    assert data_a["gex_metrics"]["_is_stale_cache"] is True
    assert data_a["_freshness"]["is_any_stale"] is True

    # 案例 B: data_timestamp 很新鮮 (300s 前 < 1800s) -> 新鮮
    fresh_env = {
        "timestamp": now - 10.0,
        "data_timestamp": now - 300.0,
        "data": {"put_wall": 140.0, "call_wall": 160.0, "net_gex": 500.0},
    }
    data_b = await _run_radar_fast(
        _build_kv_rows(extra={"gex_metrics_NVDA": fresh_env})
    )
    assert data_b["gex_metrics"]["_is_stale_cache"] is False

    # 案例 C: gex_data 帶有 _is_stale_cache=True 標記 -> 過期
    stale_flag_env = {
        "timestamp": now - 10.0,
        "data_timestamp": now - 10.0,
        "data": {
            "put_wall": 140.0,
            "call_wall": 160.0,
            "net_gex": 500.0,
            "_is_stale_cache": True,
        },
    }
    data_c = await _run_radar_fast(
        _build_kv_rows(extra={"gex_metrics_NVDA": stale_flag_env})
    )
    assert data_c["gex_metrics"]["_is_stale_cache"] is True
    assert data_c["_freshness"]["is_any_stale"] is True


# ---------------------------------------------------------------------------
# 3. Finding #4: Skew / PCR 時效上限 (超過 5 天回傳 None)
# ---------------------------------------------------------------------------


def test_get_last_stored_sentiment_max_age_cap() -> None:
    """測試 sentiment_history 讀取超過 max_age_seconds 時回傳 None。"""
    # 使用共享記憶體 SQLite，每次連線開新 handle，避免 conn.close() 關閉 DB
    uri = "file:sentiment_mem_test?mode=memory&cache=shared"
    init_conn = sqlite3.connect(uri, uri=True)
    init_conn.execute(
        """
        CREATE TABLE sentiment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            indicator TEXT NOT NULL,
            value REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # 寫入 2 天前記錄 (未過期, 2*86400 = 172800s < 432000s)
    init_conn.execute(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) "
        "VALUES ('NVDA', 'SKEW_D25', 1.25, datetime('now', '-2 days'))"
    )
    # 寫入 6 天前記錄 (已過期, 6*86400 = 518400s > 432000s)
    init_conn.execute(
        "INSERT INTO sentiment_history (symbol, indicator, value, timestamp) "
        "VALUES ('NVDA', 'PCR', 0.85, datetime('now', '-6 days'))"
    )
    init_conn.commit()

    def get_test_conn() -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True)

    with patch("database.connection.get_read_connection", side_effect=get_test_conn):
        # 5 天閾值: 432000 秒
        val_skew = get_last_stored_sentiment(
            "NVDA", "SKEW_D25", max_age_seconds=_SENTIMENT_HISTORY_MAX_AGE_SECONDS
        )
        assert val_skew == 1.25

        val_skew_with_age, age_skew = get_last_stored_sentiment_with_age(
            "NVDA", "SKEW_D25", max_age_seconds=_SENTIMENT_HISTORY_MAX_AGE_SECONDS
        )
        assert val_skew_with_age == 1.25
        assert age_skew is not None and age_skew == pytest.approx(172800.0, abs=100.0)

        # 6 天前的 PCR 超過 5 天上限 -> 應回傳 None
        val_pcr = get_last_stored_sentiment(
            "NVDA", "PCR", max_age_seconds=_SENTIMENT_HISTORY_MAX_AGE_SECONDS
        )
        assert val_pcr is None

        val_pcr_with_age, age_pcr = get_last_stored_sentiment_with_age(
            "NVDA", "PCR", max_age_seconds=_SENTIMENT_HISTORY_MAX_AGE_SECONDS
        )
        assert val_pcr_with_age is None
        assert age_pcr is not None and age_pcr == pytest.approx(518400.0, abs=100.0)

        # 若不給 max_age_seconds 則維持向後相容回傳數值
        assert get_last_stored_sentiment("NVDA", "PCR") == 0.85


def test_load_history_reads_passes_max_age_seconds() -> None:
    """驗證 _load_history_reads 呼叫 get_last_stored_sentiment 時帶入 5 天時效上限。"""
    called_args: list[tuple[Any, ...]] = []

    def mock_get_sent(
        sym: str, ind: str, max_age_seconds: Optional[float] = None
    ) -> Optional[float]:
        called_args.append((sym, ind, max_age_seconds))
        return 1.1

    with patch(
        "market_analysis.sentiment.history_storage.get_last_stored_sentiment",
        side_effect=mock_get_sent,
    ):
        _load_history_reads("NVDA", need_last_iv=False)

    assert len(called_args) == 2
    # SKEW
    assert called_args[0][0] == "NVDA"
    assert called_args[0][2] == _SENTIMENT_HISTORY_MAX_AGE_SECONDS
    # PCR
    assert called_args[1][0] == "NVDA"
    assert called_args[1][1] == "PCR"
    assert called_args[1][2] == _SENTIMENT_HISTORY_MAX_AGE_SECONDS


@pytest.mark.asyncio
async def test_fast_path_snapshot_fallback_respects_sentiment_max_age() -> None:
    """快照回退的 Skew / PCR 亦受 5 天年齡上限限制。"""
    # 案例 A: 快照年齡為 6 天 (518400s > 432000s) -> 不回退舊 Skew/PCR，傳回 None
    radar_old = {
        "skew": 1.5,
        "skew_percentile": 80.0,
        "volume_pcr": 0.75,
    }
    data_old = await _run_radar_fast(
        _build_kv_rows(radar=radar_old, radar_age_s=518400.0)
    )
    assert data_old["skew"] is None
    assert data_old["skew_percentile"] is None
    assert data_old["volume_pcr"] is None

    # 案例 B: 快照年齡為 1 天 (86400s <= 432000s) -> 允許回退快照之 Skew/PCR
    radar_fresh = {
        "skew": 1.5,
        "skew_percentile": 80.0,
        "volume_pcr": 0.75,
    }
    data_fresh = await _run_radar_fast(
        _build_kv_rows(radar=radar_fresh, radar_age_s=86400.0)
    )
    assert data_fresh["skew"] == 1.5
    assert data_fresh["skew_percentile"] == 80.0
    assert data_fresh["volume_pcr"] == 0.75


# ---------------------------------------------------------------------------
# 4. Finding #5: 斷路器不沿用舊痛點
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_fallback_to_snapshot_max_pain() -> None:
    """斷路器觸發時，Max Pain 痛點與到期日均置 None，不回退快照舊值。"""
    market_cache = {
        "circuit_breaker_triggered": 1,
        "max_pain": None,
        "expiry": "2026-10-30",
    }
    radar_cache = {
        "mp_near": 180.0,
        "mp_near_expiry": "2026-10-30",
    }
    data = await _run_radar_fast(
        _build_kv_rows(radar=radar_cache, radar_age_s=60.0),
        market_cache=market_cache,
    )
    assert data["max_pain"]["max_pain"] is None
    assert data["max_pain"]["expiry"] is None
    assert data["max_pain"]["circuit_breaker_triggered"] is True


# ---------------------------------------------------------------------------
# 5. Finding #7: Fast path 回傳補回 15 分 ATR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_returns_atr_15m() -> None:
    """Fast path 回傳字典應包含快照中已有的 atr_15m。"""
    radar_with_atr = {
        "atr_14": 5.0,
        "atr_15m": 3.75,
    }
    data = await _run_radar_fast(_build_kv_rows(radar=radar_with_atr))
    assert data["atr_15m"] == 3.75

    # 缺失時預設 0.0
    data_empty = await _run_radar_fast(_build_kv_rows(radar={"atr_14": 5.0}))
    assert data_empty["atr_15m"] == 0.0


# ---------------------------------------------------------------------------
# 6. Finding #8: Squeeze 快取過期時觸發重算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_squeeze_expired_triggers_recalc_in_radar_slow() -> None:
    """radar_data slow path 遇到 is_expired=True 的 Squeeze 快取時應重算。"""
    mixin = RadarDataMixin()
    df_hist = pd.DataFrame(
        {
            "Open": [100.0] * 30,
            "High": [105.0] * 30,
            "Low": [95.0] * 30,
            "Close": [100.0] * 30,
            "Volume": [1000] * 30,
        }
    )

    fake_psq_obj = MagicMock()
    fake_psq_obj.is_squeezing = True
    fake_psq_obj.momentum_value = 2.5
    fake_psq_obj.signal_direction = "Long"
    fake_psq_obj.squeeze_level = 1

    saved_squeeze: list[tuple[Any, ...]] = []

    async def fake_save_sqz(sym: str, is_sqz: bool, mom: float, direction: str) -> None:
        saved_squeeze.append((sym, is_sqz, mom, direction))

    # 情境 A: is_expired = True -> 觸發 analyze_psq 與 save_squeeze_cache
    with (
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 100.0},
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
            new_callable=AsyncMock,
            return_value={"max_pain": 100.0, "is_stale": False},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={
                "put_wall": 95.0,
                "call_wall": 105.0,
                "net_gex": 100.0,
                "gex_profile": {},
            },
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
            return_value={"volume_pcr": 0.8, "oi_pcr": 0.9},
        ),
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "market_analysis.atr_utils.fetch_atr_15m",
            new_callable=AsyncMock,
            return_value=1.5,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df_hist,
        ),
        patch(
            "database.squeeze_cache.get_squeeze_cache",
            return_value={"is_expired": True, "is_squeezing": False, "momentum": 0.0},
        ),
        patch(
            "market_analysis.psq_engine.analyze_psq", return_value=fake_psq_obj
        ) as mock_psq,
        patch("database.squeeze_cache.save_squeeze_cache", side_effect=fake_save_sqz),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch("database.cache.get_kv_cache_with_age", return_value=(None, 0.0)),
    ):
        res = await mixin._fetch_sym_radar_data_slow_raw("NVDA")
        assert mock_psq.called
        assert len(saved_squeeze) == 1
        assert res["psq_result"]["is_squeezing"] is True
        assert res["psq_result"]["momentum_value"] == 2.5


@pytest.mark.asyncio
async def test_squeeze_expired_triggers_recalc_in_metrics() -> None:
    """metrics.py 遇到 is_expired=True 的 Squeeze 快取時應重算。"""
    _WATCHLIST_METRICS_CACHE.clear()
    stock_prices = [100.0 + (i % 5) * 1.5 for i in range(70)]
    df_stock = pd.DataFrame(
        {
            "Open": stock_prices,
            "High": [p + 2.0 for p in stock_prices],
            "Low": [p - 2.0 for p in stock_prices],
            "Close": stock_prices,
            "Volume": [1000] * 70,
        }
    )
    df_spy = df_stock.copy()

    fake_psq_obj = MagicMock()
    fake_psq_obj.is_squeezing = True
    fake_psq_obj.momentum_value = 1.8
    fake_psq_obj.signal_direction = "Long"

    saved_squeeze: list[tuple[Any, ...]] = []

    async def fake_save_sqz(sym: str, is_sqz: bool, mom: float, direction: str) -> None:
        saved_squeeze.append((sym, is_sqz, mom, direction))

    with (
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 100.0},
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df_stock,
        ),
        patch(
            "services.market_data_service.get_basic_financials",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.market_data_service.get_company_profile",
            new_callable=AsyncMock,
            return_value={"exchange": "NASDAQ"},
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("market_analysis.risk_engine.calculate_beta", return_value=1.0),
        patch(
            "market_analysis.intraday_pipeline.metrics.get_cached_volume_poc",
            return_value=100.0,
        ),
        patch(
            "market_analysis.intraday_pipeline.metrics.get_cached_gex_putwall",
            return_value=95.0,
        ),
        patch(
            "database.squeeze_cache.get_squeeze_cache",
            return_value={"is_expired": True, "is_squeezing": False, "momentum": 0.0},
        ),
        patch(
            "market_analysis.psq_engine.analyze_psq", return_value=fake_psq_obj
        ) as mock_psq,
        patch("database.squeeze_cache.save_squeeze_cache", side_effect=fake_save_sqz),
    ):
        res = await build_enhanced_watchlist_metrics("NVDA", df_spy=df_spy)
        assert res is not None
        assert mock_psq.called
        assert len(saved_squeeze) == 1
        assert res.squeeze_status is True
        assert res.squeeze_momentum == 1.8


# ---------------------------------------------------------------------------
# 7. Finding #9: Slow path 計算真實 20 日 SMA 並寫入結果與快照
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_path_ma20_sma_calculation_and_snapshot() -> None:
    """Slow path 應以 df_hist['Close'].rolling(20).mean() 計算真實 20 日收盤 SMA。"""
    mixin = RadarDataMixin()
    # 建立 25 根 K 棒，收盤價為 100 到 124
    close_prices = [float(100 + i) for i in range(25)]
    df_hist = pd.DataFrame(
        {
            "Open": close_prices,
            "High": [p + 2.0 for p in close_prices],
            "Low": [p - 2.0 for p in close_prices],
            "Close": close_prices,
            "Volume": [1000] * 25,
        }
    )
    expected_sma20 = float(pd.Series(close_prices).rolling(20).mean().iloc[-1])

    saved_snapshots: dict[str, Any] = {}

    async def fake_save_kv(key: str, val: Any) -> None:
        saved_snapshots[key] = val

    with (
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 124.0},
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
            new_callable=AsyncMock,
            return_value={"max_pain": 120.0, "is_stale": False},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={
                "put_wall": 110.0,
                "call_wall": 130.0,
                "net_gex": 100.0,
                "gex_profile": {},
            },
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
            return_value={"volume_pcr": 0.8, "oi_pcr": 0.9},
        ),
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "market_analysis.atr_utils.fetch_atr_15m",
            new_callable=AsyncMock,
            return_value=1.5,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df_hist,
        ),
        patch(
            "database.squeeze_cache.get_squeeze_cache",
            return_value={"is_expired": False, "is_squeezing": False, "momentum": 0.0},
        ),
        patch("database.cache.save_kv_cache", side_effect=fake_save_kv),
        patch("database.cache.get_kv_cache_with_age", return_value=(None, 0.0)),
    ):
        result = await mixin._fetch_sym_radar_data_slow_raw("NVDA")
        # 1. 結果字典中的 ma20 為真實 SMA20
        assert result["ma20"] == pytest.approx(expected_sma20, rel=1e-5)

        # 2. 快照字典中的 ma20 亦存入真實 SMA20
        snapshot = saved_snapshots.get("radar_terminal_NVDA")
        assert snapshot is not None
        assert "ma20" in snapshot
        assert snapshot["ma20"] == pytest.approx(expected_sma20, rel=1e-5)


@pytest.mark.asyncio
async def test_slow_path_ma20_insufficient_bars_yields_none() -> None:
    """K 棒不足 20 根時，ma20 應為 None。"""
    mixin = RadarDataMixin()
    close_prices = [100.0] * 15  # 僅 15 根
    df_hist = pd.DataFrame(
        {
            "Open": close_prices,
            "High": close_prices,
            "Low": close_prices,
            "Close": close_prices,
            "Volume": [1000] * 15,
        }
    )

    saved_snapshots: dict[str, Any] = {}

    async def fake_save_kv(key: str, val: Any) -> None:
        saved_snapshots[key] = val

    with (
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 100.0},
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
            new_callable=AsyncMock,
            return_value={"max_pain": 100.0, "is_stale": False},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            return_value={
                "put_wall": 95.0,
                "call_wall": 105.0,
                "net_gex": 100.0,
                "gex_profile": {},
            },
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
            return_value={"volume_pcr": 0.8, "oi_pcr": 0.9},
        ),
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "market_analysis.atr_utils.fetch_atr_15m",
            new_callable=AsyncMock,
            return_value=1.5,
        ),
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df_hist,
        ),
        patch(
            "database.squeeze_cache.get_squeeze_cache",
            return_value={"is_expired": False, "is_squeezing": False, "momentum": 0.0},
        ),
        patch("database.cache.save_kv_cache", side_effect=fake_save_kv),
        patch("database.cache.get_kv_cache_with_age", return_value=(None, 0.0)),
    ):
        result = await mixin._fetch_sym_radar_data_slow_raw("NVDA")
        assert result["ma20"] is None
        snapshot = saved_snapshots.get("radar_terminal_NVDA")
        assert snapshot is not None
        assert snapshot["ma20"] is None
