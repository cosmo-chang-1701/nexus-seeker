"""services/alpaca_stream_service.py — 串流服務的單元測試（不開真實 socket／網路）。"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.stream_bars import Bar15m, MinuteBar
from services import alpaca_stream_service as svc_mod
from services.alpaca_stream_service import (
    AlpacaStreamService,
    DailyBaseline,
    _prev_close_from_daily,
    CandidateGroups,
    _trade_count_on_or_before,
    collect_candidate_groups,
    rank_symbols,
    get_stream_service,
    set_stream_service,
    to_stream_symbol,
)

DAY = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)
W = timedelta(minutes=15)


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _service() -> AlpacaStreamService:
    svc = AlpacaStreamService()
    svc._session_cache[DAY] = (OPEN, CLOSE)
    svc._spawn = MagicMock(side_effect=lambda coro: coro.close())  # type: ignore[method-assign]
    return svc


def _bar(minute: int, close: float = 100.0, volume: float = 100.0) -> MinuteBar:
    return MinuteBar(
        ts=OPEN + timedelta(minutes=minute),
        open=close, high=close, low=close, close=close, volume=volume,
    )  # fmt: skip


def _subscribe(svc: AlpacaStreamService, symbols: list[str], at: datetime) -> None:
    svc._authenticated = True
    svc._on_subscription_ack({"T": "subscription", "bars": symbols}, now=at)


# ---------------------------------------------------------------------------
# 認證與訊息處理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_message_is_not_authentication() -> None:
    svc = _service()
    svc._ws = FakeWS()
    await svc.handle_payload(json.dumps([{"T": "success", "msg": "connected"}]))
    assert not svc.is_connected


@pytest.mark.asyncio
async def test_authenticated_subscribes_desired_symbols() -> None:
    svc = _service()
    ws = FakeWS()
    svc._ws = ws
    svc._desired = ["AAPL", "SOFI"]
    await svc.handle_payload(json.dumps([{"T": "success", "msg": "authenticated"}]))
    assert svc.is_connected
    assert ws.sent == [
        {
            "action": "subscribe",
            "bars": ["AAPL", "SOFI"],
            "updatedBars": ["AAPL", "SOFI"],
        }
    ]


@pytest.mark.asyncio
async def test_sync_sends_diff_only() -> None:
    svc = _service()
    ws = FakeWS()
    svc._ws = ws
    _subscribe(svc, ["AAPL", "SOFI"], OPEN)
    svc._desired = ["AAPL", "PLTR"]
    await svc._sync_subscriptions()
    assert ws.sent == [
        {"action": "unsubscribe", "bars": ["SOFI"], "updatedBars": ["SOFI"]},
        {"action": "subscribe", "bars": ["PLTR"], "updatedBars": ["PLTR"]},
    ]


def test_subscription_ack_creates_and_releases_state() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL", "SOFI"], OPEN)
    assert set(svc._states) == {"AAPL", "SOFI"}
    assert svc._states["AAPL"].coverage_since == OPEN
    svc._spawn.assert_called_once()  # type: ignore[attr-defined]

    svc._on_subscription_ack({"T": "subscription", "bars": ["AAPL"]}, now=OPEN)
    assert set(svc._states) == {"AAPL"}


@pytest.mark.asyncio
async def test_error_402_is_fatal_and_406_extends_backoff() -> None:
    svc = _service()
    await svc.handle_payload(json.dumps([{"T": "error", "code": 406, "msg": "limit"}]))
    assert svc._connection_limited and not svc._fatal
    await svc.handle_payload(
        json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}])
    )
    assert svc._fatal


@pytest.mark.asyncio
async def test_error_405_reduces_symbol_cap_and_resubscribes() -> None:
    svc = _service()
    ws = FakeWS()
    svc._ws = ws
    svc._authenticated = True
    svc._desired = [f"S{i}" for i in range(30)]
    await svc.handle_payload(
        json.dumps([{"T": "error", "code": 405, "msg": "symbol limit"}])
    )
    assert svc._symbol_cap == 25
    assert len(svc._desired) == 25
    assert ws.sent and ws.sent[-1]["action"] == "subscribe"


@pytest.mark.asyncio
async def test_bar_message_is_ingested() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN)
    payload = [{"T": "b", "S": "AAPL", "t": "2026-09-22T13:31:00Z", "o": 1, "h": 2,
                "l": 0.5, "c": 1.5, "v": 1000, "n": 10, "vw": 1.4}]  # fmt: skip
    await svc.handle_payload(json.dumps(payload))
    bars = svc.get_bars("AAPL")
    assert len(bars) == 1 and bars[0].close == 1.5


@pytest.mark.asyncio
async def test_malformed_payload_does_not_raise() -> None:
    svc = _service()
    await svc.handle_payload("not json")
    await svc.handle_payload(json.dumps([{"T": "b", "S": "AAPL", "t": "bad"}]))


def test_disconnect_clears_coverage() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN)
    svc._states["AAPL"].complete_since = OPEN
    svc._on_disconnect()
    st = svc._states["AAPL"]
    assert not svc.is_connected
    assert st.coverage_since is None and st.complete_since is None
    assert svc.subscribed_symbols == set()


# ---------------------------------------------------------------------------
# 寫入過濾
# ---------------------------------------------------------------------------


def test_ingest_drops_extended_hours_and_unsubscribed_bars() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN)
    assert not svc.ingest_bar("AAPL", _bar(-5), now=OPEN)  # 盤前
    assert not svc.ingest_bar("AAPL", _bar(390), now=CLOSE + W)  # 盤後
    assert not svc.ingest_bar("TSLA", _bar(1), now=OPEN + W)  # 未訂閱
    assert svc.ingest_bar("AAPL", _bar(1), now=OPEN + W)


def test_ingest_updated_bar_replaces_and_fixes_vwap() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN)
    svc.ingest_bar("AAPL", _bar(0, close=10.0, volume=100.0), now=OPEN)
    svc.ingest_bar("AAPL", _bar(1, close=20.0, volume=100.0), now=OPEN)
    svc.ingest_bar("AAPL", _bar(0, close=12.0, volume=100.0), now=OPEN)  # updatedBars
    sess = svc._states["AAPL"].session
    assert sess is not None
    assert sess.vwap == pytest.approx((12.0 + 20.0) / 2)
    assert len(svc.get_bars("AAPL")) == 2


def test_symbol_normalization() -> None:
    assert to_stream_symbol("$brk-b ") == "BRK.B"


# ---------------------------------------------------------------------------
# 訂閱清單
# ---------------------------------------------------------------------------


def test_collect_candidate_groups_normalizes_dedups_and_counts_watchers() -> None:
    w1, w2 = MagicMock(), MagicMock()
    w1.symbol, w1.user_id = "NVDA", 1
    w2.symbol, w2.user_id = "NVDA", 2
    with (
        patch(
            "database.portfolio.get_all_portfolio_symbol_pairs",
            return_value={(1, "TSLA"), (2, "TSLA"), (2, "^VIX"), (2, "BRK-B")},
        ),
        patch("database.price_volume_watch.get_all_watches", return_value=[w1, w2]),
        patch(
            "database.watchlist.get_all_watchlist",
            return_value=[
                (1, "TSLA", True),
                (1, "CL=F", True),
                (3, "amd", True),
                (1, "NVDA", True),
            ],
        ),  # fmt: skip
    ):
        g = collect_candidate_groups()
    assert g.holdings == {"TSLA", "BRK.B"}
    assert g.price_volume == {"NVDA"}
    assert g.watchlist == {"TSLA", "AMD", "NVDA"}
    assert g.watchers == {"NVDA": 2, "TSLA": 1, "AMD": 1}
    assert g.all_symbols() == {"TSLA", "BRK.B", "NVDA", "AMD"}


def test_rank_symbols_holdings_then_large_cap_watches_then_activity() -> None:
    g = CandidateGroups(
        holdings={"MU", "ZZZ"},
        price_volume={"AAPL", "SOFI"},
        watchlist={"AAPL", "AMD", "ACME", "QUIET", "MU"},
        watchers={"AMD": 1, "ACME": 3, "QUIET": 1, "SOFI": 1, "AAPL": 2},
    )
    activity = {"MU": 500, "ZZZ": 10, "AAPL": 1, "AMD": 9000, "SOFI": 50, "ACME": 50}
    ranked = rank_symbols(g, activity, frozenset({"AAPL", "AMD"}), cap=10)
    # 持倉依活躍度 → 白名單內的價量監測（即使活躍度最低）→ 其餘依活躍度、關注人數
    assert ranked == ["MU", "ZZZ", "AAPL", "AMD", "ACME", "SOFI", "QUIET"]
    assert rank_symbols(g, activity, frozenset({"AAPL"}), cap=3) == [
        "MU",
        "ZZZ",
        "AAPL",
    ]


def test_rank_symbols_holdings_over_cap_keeps_most_active() -> None:
    g = CandidateGroups(holdings={"A", "B", "C"})
    assert rank_symbols(g, {"C": 3, "B": 2}, frozenset(), cap=2) == ["C", "B"]


def test_rank_symbols_is_not_alphabetical_truncation() -> None:
    g = CandidateGroups(watchlist={f"A{i}" for i in range(5)} | {"ZZ"})
    ranked = rank_symbols(g, {"ZZ": 100}, frozenset(), cap=2)
    assert ranked[0] == "ZZ"


def test_trade_count_uses_reference_day_not_today() -> None:
    rows = [
        {"t": "2026-09-21T04:00:00Z", "n": 100},
        {"t": "2026-09-22T04:00:00Z", "n": 200},
        {"t": "2026-09-23T04:00:00Z", "n": 5},  # 今日成形中
    ]
    assert _trade_count_on_or_before(rows, DAY) == 200
    assert _trade_count_on_or_before([], DAY) == 0


@pytest.mark.asyncio
async def test_activity_fetched_once_per_day_and_only_for_new_symbols() -> None:
    svc = _service()
    calls: list[list[str]] = []

    async def fake_fetch(symbols: Any, timeframe: str, start: Any, end: Any) -> Any:
        calls.append(list(symbols))
        return {s: [{"t": "2026-09-22T04:00:00Z", "n": 7}] for s in symbols}

    with (
        patch.object(svc, "fetch_historical_bars", side_effect=fake_fetch),
        patch("market_time.get_last_completed_trading_date", return_value="2026-09-22"),
    ):
        await svc._ensure_activity({"AAPL", "SOFI"})
        await svc._ensure_activity({"AAPL", "SOFI"})
        await svc._ensure_activity({"AAPL", "SOFI", "PLTR"})
    assert calls == [["AAPL", "SOFI"], ["PLTR"]]
    assert svc._activity == {"AAPL": 7, "SOFI": 7, "PLTR": 7}

    with (
        patch.object(svc, "fetch_historical_bars", side_effect=fake_fetch),
        patch("market_time.get_last_completed_trading_date", return_value="2026-09-23"),
    ):
        await svc._ensure_activity({"AAPL"})
    assert calls[-1] == ["AAPL"]
    assert svc._activity == {"AAPL": 7}


@pytest.mark.asyncio
async def test_activity_fetch_failure_does_not_record_zero() -> None:
    svc = _service()
    with (
        patch.object(svc, "fetch_historical_bars", new=AsyncMock(return_value=None)),
        patch("market_time.get_last_completed_trading_date", return_value="2026-09-22"),
    ):
        await svc._ensure_activity({"AAPL"})
    assert "AAPL" not in svc._activity


@pytest.mark.asyncio
async def test_refresh_subscriptions_uses_ranked_list() -> None:
    svc = _service()
    ws = FakeWS()
    svc._ws = ws
    svc._authenticated = True
    svc._symbol_cap = 2
    groups = CandidateGroups(watchlist={"AAA", "BBB", "CCC"})
    svc._activity_date = "2026-09-22"
    svc._activity = {"CCC": 9, "BBB": 5, "AAA": 1}
    with (
        patch.object(svc_mod, "collect_candidate_groups", return_value=groups),
        patch("market_time.get_last_completed_trading_date", return_value="2026-09-22"),
    ):
        await svc.refresh_subscriptions()
    assert svc._desired == ["CCC", "BBB"]
    assert ws.sent == [
        {"action": "subscribe", "bars": ["BBB", "CCC"], "updatedBars": ["BBB", "CCC"]}
    ]


def test_tier0_hit_rate_counts_attempts_and_hits() -> None:
    svc = _ready_quote_service()
    svc.get_quote_snapshot("AAPL", now=OPEN + timedelta(minutes=11))
    svc.get_quote_snapshot("AAPL", now=OPEN + timedelta(minutes=20))  # 過期
    assert svc._tier0_stats["AAPL"] == [1, 2]


# ---------------------------------------------------------------------------
# Tier 0 報價
# ---------------------------------------------------------------------------


def _ready_quote_service(prev_close: float = 90.0) -> AlpacaStreamService:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN - timedelta(hours=1))
    st = svc._states["AAPL"]
    st.complete_since = OPEN
    st.baseline = DailyBaseline(session_date=DAY.isoformat(), prev_close=prev_close)
    svc.ingest_bar("AAPL", _bar(0, close=95.0), now=OPEN)
    svc.ingest_bar("AAPL", _bar(10, close=99.0), now=OPEN)
    return svc


def test_quote_snapshot_uses_daily_prev_close_not_previous_minute() -> None:
    svc = _ready_quote_service(prev_close=90.0)
    now = OPEN + timedelta(minutes=11, seconds=30)
    q = svc.get_quote_snapshot("AAPL", now=now)
    assert q is not None
    assert q["c"] == 99.0
    assert q["pc"] == 90.0  # 官方昨收，而非上一分鐘 (95.0 或合成的前收)
    assert q["d"] == 9.0
    assert q["dp"] == pytest.approx(10.0)
    assert q["o"] == 95.0 and q["h"] == 99.0 and q["l"] == 95.0
    assert q["t"] == int((OPEN + timedelta(minutes=11)).timestamp())


def test_quote_snapshot_rejects_stale_bar() -> None:
    svc = _ready_quote_service()
    assert svc.get_quote_snapshot("AAPL", now=OPEN + timedelta(minutes=14)) is None


def test_quote_snapshot_requires_baseline_complete_data_and_session() -> None:
    now = OPEN + timedelta(minutes=11)
    svc = _ready_quote_service()
    svc._states["AAPL"].baseline = None
    assert svc.get_quote_snapshot("AAPL", now=now) is None

    svc = _ready_quote_service()
    svc._states["AAPL"].complete_since = OPEN + timedelta(minutes=5)
    assert svc.get_quote_snapshot("AAPL", now=now) is None

    svc = _ready_quote_service()
    assert svc.get_quote_snapshot("AAPL", now=CLOSE + timedelta(minutes=1)) is None

    svc = _ready_quote_service()
    svc._authenticated = False
    assert svc.get_quote_snapshot("AAPL", now=now) is None


# ---------------------------------------------------------------------------
# 15 分 K 確認
# ---------------------------------------------------------------------------


def _ready_15m_service(
    complete_since: Optional[datetime] = OPEN,
) -> AlpacaStreamService:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN - timedelta(hours=1))
    st = svc._states["AAPL"]
    prev_open = OPEN - timedelta(days=1)
    for i in range(20):
        st.fifteen.append(
            Bar15m(
                start=prev_open + i * W,
                open=100,
                high=100,
                low=100,
                close=100,
                volume=1500.0,
            )
        )
    st.complete_since = complete_since
    st.next_window_start = OPEN
    for m in range(45):
        vol = 1000.0 if m >= 30 else 100.0
        bar = _bar(m, close=100.0 + m / 100, volume=vol)
        svc.ingest_bar("AAPL", bar, now=bar.bar_end + timedelta(seconds=5))
    return svc


def test_confirmed_15m_bar_returns_latest_window_with_avg_volume() -> None:
    svc = _ready_15m_service()
    result = svc.get_confirmed_15m_bar(
        "AAPL", 20, now=OPEN + 3 * W + timedelta(seconds=30)
    )
    assert result is not None
    bar, avg_volume = result
    assert bar.start == OPEN + 2 * W
    assert bar.volume == 15000.0
    assert avg_volume == pytest.approx(1500.0)
    assert bar.close == pytest.approx(100.44)


def test_confirmed_15m_bar_within_grace_returns_previous_window() -> None:
    svc = _ready_15m_service()
    result = svc.get_confirmed_15m_bar(
        "AAPL", 20, now=OPEN + 3 * W + timedelta(seconds=10)
    )
    assert result is not None
    assert result[0].start == OPEN + W


def test_confirmed_15m_bar_incomplete_coverage_returns_none() -> None:
    svc = _ready_15m_service(complete_since=OPEN + timedelta(minutes=20))
    assert (
        svc.get_confirmed_15m_bar("AAPL", 20, now=OPEN + 3 * W + timedelta(seconds=30))
        is None
    )


def test_confirmed_15m_bar_insufficient_history_returns_none() -> None:
    svc = _ready_15m_service()
    st = svc._states["AAPL"]
    while len(st.fifteen) > 5:
        st.fifteen.popleft()
    assert (
        svc.get_confirmed_15m_bar("AAPL", 20, now=OPEN + 3 * W + timedelta(seconds=30))
        is None
    )


# ---------------------------------------------------------------------------
# 回補
# ---------------------------------------------------------------------------


def _rest_row(ts: datetime, close: float, volume: float) -> dict[str, Any]:
    return {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close, "h": close,
            "l": close, "c": close, "v": volume, "n": 1, "vw": close}  # fmt: skip


@pytest.mark.asyncio
async def test_seed_merges_rest_with_stream_and_marks_complete() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN + timedelta(minutes=20))
    svc.ingest_bar(
        "AAPL", _bar(20, close=555.0, volume=7.0), now=OPEN + timedelta(minutes=21)
    )

    now = OPEN + timedelta(minutes=21, seconds=30)
    minute_rows = {
        "AAPL": [
            _rest_row(OPEN + timedelta(minutes=m), 100.0 + m, 10.0) for m in range(21)
        ]
    }
    prev_day = OPEN - timedelta(days=1)
    fifteen_rows = {
        "AAPL": [_rest_row(prev_day + i * W, 99.0, 1500.0) for i in range(26)]
        + [_rest_row(prev_day - timedelta(hours=2), 1.0, 1.0)]  # 盤前，應被濾掉
    }

    async def fake_fetch(symbols: Any, timeframe: str, start: Any, end: Any) -> Any:
        return minute_rows if timeframe == "1Min" else fifteen_rows

    svc._session_cache[DAY - timedelta(days=1)] = (
        prev_day,
        prev_day + timedelta(hours=6, minutes=30),
    )
    with (
        patch.object(svc, "fetch_historical_bars", side_effect=fake_fetch),
        patch("services.llm_service.is_memory_safe", return_value=True),
    ):
        await svc._seed_symbols(["AAPL"], now=now)

    st = svc._states["AAPL"]
    assert st.complete_since == OPEN
    closes = {b.ts: b.close for b in st.bars}
    assert closes[OPEN + timedelta(minutes=20)] == 555.0  # 串流值優先於 REST
    assert closes[OPEN] == 100.0
    assert st.session is not None and st.session.open == 100.0
    # 26 根前一日 15 分 K + 今日已收盤的 13:30 時窗
    assert len(st.fifteen) == 27
    assert st.fifteen[-1].start == OPEN
    assert all(b.volume != 1.0 for b in st.fifteen)


@pytest.mark.asyncio
async def test_seed_failure_falls_back_to_coverage_since() -> None:
    svc = _service()
    at = OPEN + timedelta(minutes=20)
    _subscribe(svc, ["AAPL"], at)
    with (
        patch.object(svc, "fetch_historical_bars", new=AsyncMock(return_value=None)),
        patch("services.llm_service.is_memory_safe", return_value=True),
    ):
        await svc._seed_symbols(["AAPL"], now=at + timedelta(minutes=1))
    st = svc._states["AAPL"]
    assert st.complete_since == at
    assert len(st.fifteen) == 0


@pytest.mark.asyncio
async def test_seed_skipped_when_memory_unsafe() -> None:
    svc = _service()
    _subscribe(svc, ["AAPL"], OPEN)
    fetch = AsyncMock()
    with (
        patch.object(svc, "fetch_historical_bars", new=fetch),
        patch("services.llm_service.is_memory_safe", return_value=False),
    ):
        await svc._seed_symbols(["AAPL"], now=OPEN + W)
    fetch.assert_not_called()
    assert svc._states["AAPL"].complete_since == OPEN


def test_prev_close_from_daily_excludes_today() -> None:
    df = pd.DataFrame(
        {"Close": [80.0, 90.0, 95.0]},
        index=pd.to_datetime(["2026-09-18", "2026-09-21", "2026-09-22"]),
    )
    assert _prev_close_from_daily(df, DAY) == 90.0
    assert _prev_close_from_daily(pd.DataFrame(), DAY) is None


# ---------------------------------------------------------------------------
# 生命週期與 registry
# ---------------------------------------------------------------------------


def test_registry_roundtrip() -> None:
    original = get_stream_service()
    try:
        svc = AlpacaStreamService()
        set_stream_service(svc)
        assert get_stream_service() is svc
        set_stream_service(None)
        assert get_stream_service() is None
    finally:
        set_stream_service(original)


@pytest.mark.asyncio
async def test_start_is_noop_when_disabled() -> None:
    svc = AlpacaStreamService()
    with patch.object(svc_mod.config, "ENABLE_ALPACA_STREAM", False):
        svc.start()
    assert svc._stream_task is None and svc._refresh_task is None
