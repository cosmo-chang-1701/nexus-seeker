"""不指定到期日的 Max Pain 讀取須取預設到期日列，而非 ``updated_at`` 最新列。

深度分析會寫入 30 天內各到期日列，遠月列較新時過去會讓雷達與
``get_unified_max_pain(symbol)`` 顯示或沿用遠月痛點；單一到期日的 30% 異常也只能
標記該到期日的快取列。
"""

import asyncio
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Coroutine, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import config
from database.core import run_migrations
from database.market_cache import (
    get_market_cache,
    save_market_cache,
    save_wall_levels,
)
from market_analysis.sentiment.max_pain import (
    _current_week_friday,
    get_unified_max_pain,
    select_default_expiry,
)
from market_time import ny_tz

# 固定「今日」：2027-03-08（週一），當週五 3/12 為正常交易日。
_FIXED_TODAY = date(2027, 3, 8)


@pytest.fixture
def temp_db() -> Iterator[None]:
    tf = tempfile.NamedTemporaryFile(suffix=".db")
    orig_db = config.DB_NAME
    try:
        config.DB_NAME = tf.name
        run_migrations()
        yield
    finally:
        config.DB_NAME = orig_db
        tf.close()


@pytest.fixture
def fixed_today() -> Iterator[None]:
    with patch("database.market_cache._today_ny", return_value=_FIXED_TODAY):
        yield


def _utc_ts(hours_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def _seed(
    symbol: str,
    expiry: str,
    max_pain: float,
    hours_ago: float = 0.1,
    spot: float = 100.0,
) -> None:
    assert await save_market_cache(
        symbol,
        max_pain,
        95.0,
        105.0,
        reference_spot_price=spot,
        expiry=expiry,
    )
    conn = sqlite3.connect(config.DB_NAME)
    try:
        conn.execute(
            "UPDATE market_cache SET updated_at = ? WHERE symbol = ? AND expiry = ?",
            (_utc_ts(hours_ago), symbol, expiry),
        )
        conn.commit()
    finally:
        conn.close()


def _stale_flag(symbol: str, expiry: str) -> int:
    row = get_market_cache(symbol, expiry)
    assert row is not None
    return int(row["is_stale"])


def _near_and_far() -> tuple[str, str]:
    """依實際今日產生 (預設到期日, 遠月到期日)；遠月仍在 30 天阻斷線內。"""
    friday = _current_week_friday()
    return friday.isoformat(), (friday + timedelta(days=14)).isoformat()


async def _run_and_drain(coro: Coroutine[Any, Any, Any]) -> Any:
    """執行 coro，並等待它衍生的背景任務完成。"""
    before = asyncio.all_tasks()
    result = await coro
    spawned = asyncio.all_tasks() - before - {asyncio.current_task()}
    if spawned:
        await asyncio.gather(*spawned)
    return result


# ---------------------------------------------------------------------------
# select_default_expiry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "expiries", "expected"),
    [
        # 週五優先於週一／週三
        (
            _FIXED_TODAY,
            ["2027-03-08", "2027-03-10", "2027-03-12", "2027-03-19"],
            "2027-03-12",
        ),
        # 週五缺：取最早的週到期（週一），與輸入順序無關
        (_FIXED_TODAY, ["2027-03-10", "2027-03-08"], "2027-03-08"),
        (_FIXED_TODAY, ["2027-03-19", "2027-03-10"], "2027-03-10"),
        # 沒有週到期：14 天內最近一檔
        (_FIXED_TODAY, ["2027-03-22", "2027-03-19", "2027-04-16"], "2027-03-19"),
        # 已過期與無法解析者跳過
        (
            _FIXED_TODAY,
            ["WEEKLY", "", "2027-03-05", "2027-03-19"],
            "2027-03-19",
        ),
        # 週五當天滾到下週五
        (date(2027, 3, 12), ["2027-03-12", "2027-03-19"], "2027-03-19"),
        # 假日前移：2026-04-03 耶穌受難日休市，目標改為週四 04-02
        (date(2026, 3, 30), ["2026-03-31", "2026-04-02"], "2026-04-02"),
        # 無 14 天內到期
        (_FIXED_TODAY, ["2027-03-23", "2027-04-16"], None),
        (_FIXED_TODAY, [], None),
    ],
)
def test_select_default_expiry(
    today: date, expiries: list[str], expected: Optional[str]
) -> None:
    assert select_default_expiry(expiries, today) == expected


# ---------------------------------------------------------------------------
# get_market_cache(near_term=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_term_ignores_newer_far_row(
    temp_db: None, fixed_today: None
) -> None:
    await _seed("NTA", "2027-03-12", 100.0, hours_ago=3)
    await _seed("NTA", "2027-04-02", 130.0, hours_ago=0.1)

    near = get_market_cache("nta", near_term=True)
    assert near is not None
    assert near["expiry"] == "2027-03-12"
    assert near["max_pain"] == 100.0

    latest = get_market_cache("NTA")
    assert latest is not None
    assert latest["expiry"] == "2027-04-02"

    explicit = get_market_cache("NTA", "2027-04-02", near_term=True)
    assert explicit is not None
    assert explicit["max_pain"] == 130.0


@pytest.mark.asyncio
async def test_near_term_weekly_order_without_friday(
    temp_db: None, fixed_today: None
) -> None:
    await _seed("NTB", "2027-03-10", 102.0, hours_ago=0.1)
    await _seed("NTB", "2027-03-08", 101.0, hours_ago=3)

    row = get_market_cache("NTB", near_term=True)
    assert row is not None
    assert row["expiry"] == "2027-03-08"


@pytest.mark.asyncio
async def test_near_term_falls_back_to_nearest_unexpired_row(
    temp_db: None, fixed_today: None
) -> None:
    await _seed("NTC", "2027-03-28", 110.0, hours_ago=3)
    await _seed("NTC", "2027-04-16", 120.0, hours_ago=0.1)

    row = get_market_cache("NTC", near_term=True)
    assert row is not None
    assert row["expiry"] == "2027-03-28"


@pytest.mark.asyncio
async def test_near_term_returns_none_without_unexpired_dated_row(
    temp_db: None, fixed_today: None
) -> None:
    await _seed("NTD", "2027-03-05", 90.0)
    await _seed("NTD", "WEEKLY", 0.0)

    assert get_market_cache("NTD", near_term=True) is None
    assert get_market_cache("NTD") is not None


# ---------------------------------------------------------------------------
# 讀取端
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unified_max_pain_uses_default_expiry_when_far_row_newer(
    temp_db: None,
) -> None:
    near, far = _near_and_far()
    await _seed("NTE", near, 100.0, hours_ago=3)
    await _seed("NTE", far, 120.0, hours_ago=0.1)

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "market_analysis.sentiment.max_pain._calculate_max_pain_raw",
        new_callable=AsyncMock,
    ) as mock_calc:
        result = await get_unified_max_pain("NTE")

    mock_calc.assert_not_called()
    assert result["expiry"] == near
    assert result["max_pain"] == 100.0


@pytest.mark.asyncio
async def test_unified_max_pain_failure_falls_back_to_default_expiry_row(
    temp_db: None,
) -> None:
    near, far = _near_and_far()
    await _seed("NTF", near, 100.0, hours_ago=7)
    await _seed("NTF", far, 120.0, hours_ago=0.1)

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "market_analysis.sentiment.max_pain.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=MagicMock(expected_move_weekly=5.0),
    ), patch(
        "market_analysis.sentiment.max_pain._calculate_max_pain_raw",
        new_callable=AsyncMock,
        return_value={"error": "No chain data"},
    ), patch(
        "market_analysis.sentiment.max_pain.IVContext.get_expected_move",
        new_callable=AsyncMock,
        return_value={"expected_move_lower": 95.0, "expected_move_upper": 105.0},
    ):
        result = await get_unified_max_pain("NTF")

    assert result["fallback_source"] == "SQLite"
    assert result["expiry"] == near
    assert result["max_pain"] == 100.0
    far_row = get_market_cache("NTF", far)
    assert far_row is not None
    assert far_row["max_pain"] == 120.0
    assert far_row["is_stale"] == 0


@pytest.mark.asyncio
async def test_radar_and_watchlist_read_default_expiry_row(temp_db: None) -> None:
    from cogs.embed_builders.watchlist_embeds import _format_watchlist_price_info
    from cogs.unified_terminal.radar_data import _load_symbol_caches

    near, far = _near_and_far()
    await _seed("NTH", near, 80.0, hours_ago=3)
    await _seed("NTH", far, 125.0, hours_ago=0.1)

    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    _, market_cache_row, _ = _load_symbol_caches("NTH", today_str)
    assert market_cache_row is not None
    assert market_cache_row["expiry"] == near
    assert market_cache_row["max_pain"] == 80.0

    assert "MP+25.0%" in _format_watchlist_price_info("NTH")


@pytest.mark.asyncio
async def test_watchlist_latest_price_fallback(
    temp_db: None, fixed_today: None
) -> None:
    from cogs.embed_builders.watchlist_embeds import _format_watchlist_price_info

    # 只有 WEEKLY 失敗列：只顯示參考價，不附 MP 偏離
    await _seed("WLA", "WEEKLY", 10.0, spot=12.34)
    assert _format_watchlist_price_info("WLA") == "$12.34"

    # 過期到期日列＋牆位新建列（紀元時間戳不會成為最新列）
    await _seed("WLB", "2027-03-05", 90.0, hours_ago=48, spot=88.5)
    assert await save_wall_levels("WLB", call_wall=110.0, put_wall=80.0)
    assert _format_watchlist_price_info("WLB") == "$88.50"

    # 只有牆位新建列：沒有參考價
    assert await save_wall_levels("WLC", call_wall=110.0, put_wall=80.0)
    assert _format_watchlist_price_info("WLC") == "-- (尚未預熱)"

    # 有近月列時行為不變，即使 WEEKLY 失敗列較新
    await _seed("WLD", "2027-03-12", 80.0, hours_ago=3)
    await _seed("WLD", "WEEKLY", 0.0, spot=12.34)
    assert "MP+25.0%" in _format_watchlist_price_info("WLD")


# ---------------------------------------------------------------------------
# 30% 異常只標記該到期日
# ---------------------------------------------------------------------------


class _AnomalyChain:
    """痛點落在 50、現價 100：偏離超過 30%。"""

    def __init__(self) -> None:
        self.calls = pd.DataFrame({"strike": [50.0], "openInterest": [100.0]})
        self.puts = pd.DataFrame({"strike": [50.0], "openInterest": [100.0]})


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.asyncio
async def test_anomaly_marks_only_resolved_expiry(
    temp_db: None, explicit: bool
) -> None:
    near, far = _near_and_far()
    symbol = "NTI" if explicit else "NTJ"
    # explicit：遠月異常；否則為未指定到期日、解析到預設到期日的異常。
    target, other = (far, near) if explicit else (near, far)
    await _seed(symbol, target, 100.0, hours_ago=7)
    await _seed(symbol, other, 101.0, hours_ago=0.1)

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[near, far],
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=_AnomalyChain(),
    ), patch(
        "services.market_data_service.get_stock_splits",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
        return_value={},
    ), patch(
        "market_analysis.sentiment.max_pain.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=MagicMock(expected_move_weekly=5.0),
    ), patch(
        "market_analysis.sentiment.max_pain.IVContext.get_expected_move",
        new_callable=AsyncMock,
        return_value={"expected_move_lower": 95.0, "expected_move_upper": 105.0},
    ):
        result = await _run_and_drain(
            get_unified_max_pain(symbol, expiry=target if explicit else None)
        )

    assert result["expiry"] == target
    assert _stale_flag(symbol, target) == 1
    assert _stale_flag(symbol, other) == 0
    assert get_market_cache(symbol, "WEEKLY") is None


@pytest.mark.asyncio
async def test_unified_circuit_breaker_passes_resolved_expiry(temp_db: None) -> None:
    _, far = _near_and_far()
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 100.0},
    ), patch(
        "market_analysis.sentiment.max_pain.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=MagicMock(expected_move_weekly=5.0),
    ), patch(
        "market_analysis.sentiment.max_pain._calculate_max_pain_raw",
        new_callable=AsyncMock,
        return_value={"symbol": "NTK", "expiry": far, "max_pain": 200.0},
    ), patch(
        "market_analysis.sentiment.max_pain.IVContext.get_expected_move",
        new_callable=AsyncMock,
        return_value={"expected_move_lower": 95.0, "expected_move_upper": 105.0},
    ), patch(
        "market_analysis.sentiment.max_pain._trigger_background_cache_clear"
    ) as mock_clear:
        await get_unified_max_pain("NTK")

    mock_clear.assert_called_once_with("NTK", far)


@pytest.mark.asyncio
async def test_background_cache_clear_marks_only_given_expiry(temp_db: None) -> None:
    from market_analysis.sentiment.history_storage import (
        _trigger_background_cache_clear,
    )

    near, far = _near_and_far()
    await _seed("NTL", near, 100.0)
    await _seed("NTL", far, 120.0)

    async def _trigger() -> None:
        _trigger_background_cache_clear("NTL", far)

    with patch(
        "market_analysis.sentiment.max_pain.calculate_max_pain",
        new_callable=AsyncMock,
    ):
        await _run_and_drain(_trigger())

    assert _stale_flag("NTL", far) == 1
    assert _stale_flag("NTL", near) == 0
