"""Alpaca 即時 1 分 K 串流服務 (leader-only)。

資料流：Alpaca 股票 WebSocket (``bars`` + ``updatedBars``) → 常規時段過濾 →
Forward Fill 補齊 (``market_analysis/stream_bars.py``) → 每檔的分鐘緩衝區、當日
時段統計、15 分 K 聚合。兩個下游：

* ``services/market_data_service/quote.py`` 的 Tier 0：``get_quote_snapshot()``
  以「最後一根真實成交 1 分 K」為現價、以**官方日線昨收**為 ``pc``，條件不足就
  回傳 None 讓呼叫端照舊走 Finnhub。分鐘級（最多落後約 60–90 秒），不是毫秒級。
* ``market_analysis/price_volume_alert.py``：``get_confirmed_15m_bar()`` 回傳最近
  一根已收盤的 15 分 K 與前 20 根均量，只在該時窗「全程資料完整」時才回傳。

「資料完整」由兩個時間戳界定，是本模組最重要的不變式：

* ``coverage_since``：本次連線對該標的完成訂閱 (收到 ``subscription`` 回覆) 的時刻。
* ``complete_since``：分鐘資料自此時刻起連續無缺。訂閱後以 REST 回補當日 09:30
  起的 1 分 K 成功，才會把它往前推到開盤；回補失敗則等於 ``coverage_since``。

斷線時兩者一律清空、15 分 K 歷史也清空重建——寧可暫時退回 yfinance／Finnhub，
也不要用中間缺了一段的資料計算均量或當日開盤價。

本模組全程不碰 SQLite 寫入；訂閱清單的三次讀取合併在一次 ``asyncio.to_thread``。
"""

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import httpx
import websockets

import config
import market_time
from market_analysis.stream_bars import (
    WINDOW_15M,
    Bar15m,
    MinuteBar,
    SessionStats,
    StreamTechnicals,
    StreamTier,
    aggregate_15m,
    apply_forward_fill,
    classify_symbol_tier,
    compute_technicals,
    rebuild_buffer,
)

logger = logging.getLogger(__name__)

STOCK_STREAM_URL_TEMPLATE = "wss://stream.data.alpaca.markets/v2/{feed}"
HISTORICAL_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"

_FIFTEEN_HISTORY_BARS = 40  # 15 分 K 保留根數 (均量需要 20 + 1)
_FIFTEEN_SEED_PRIOR_BARS = 30  # 回補今日開盤前的 15 分 K 根數
_FIFTEEN_SEED_LOOKBACK = timedelta(days=7)  # 足以涵蓋連假
# 1 分 K 在分鐘結束後數秒才送達；時窗結束後再等這段時間才視為可收盤。
WINDOW_GRACE = timedelta(seconds=20)
_REFRESH_INTERVAL_SECONDS = 15 * 60
_SEED_CONCURRENCY = 3
_MAX_PAGES = 20
_ACTIVITY_CHUNK = 100  # 1Day 活躍度查詢每批標的數（URL 長度）

_ERR_AUTH_FAILED = 402
_ERR_SYMBOL_LIMIT = 405
_ERR_CONNECTION_LIMIT = 406
_ERR_INSUFFICIENT_SUBSCRIPTION = 409
_FATAL_ERROR_CODES = {_ERR_AUTH_FAILED, _ERR_INSUFFICIENT_SUBSCRIPTION}

_STREAMABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

_service: Optional["AlpacaStreamService"] = None


def get_stream_service() -> Optional["AlpacaStreamService"]:
    """取得已登記的串流服務；未建立時回傳 None（下游一律照舊走原本資料源）。"""
    return _service


def set_stream_service(service: Optional["AlpacaStreamService"]) -> None:
    global _service
    _service = service


def to_stream_symbol(raw: str) -> str:
    """內部 ticker → Alpaca 代號 (``BRK-B`` → ``BRK.B``)，同時作為狀態字典的鍵。"""
    return (raw or "").strip().strip("$").upper().replace("-", ".")


def is_streamable_symbol(symbol: str) -> bool:
    """排除指數 (``^VIX``)、期貨 (``CL=F``)、加密貨幣對與 VIX。"""
    return bool(_STREAMABLE_PATTERN.match(symbol)) and symbol != "VIX"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ny_date(ts: datetime) -> date:
    return ts.astimezone(market_time.ny_tz).date()


def _to_rfc3339(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def parse_bar(item: dict[str, Any]) -> Optional[MinuteBar]:
    """解析 Alpaca 的 bar 物件（WebSocket 與 REST 欄位名稱相同）。"""
    ts = _parse_ts(item.get("t"))
    if ts is None:
        return None
    try:
        close = float(item["c"])
        return MinuteBar(
            ts=ts,
            open=float(item.get("o", close)),
            high=float(item.get("h", close)),
            low=float(item.get("l", close)),
            close=close,
            volume=float(item.get("v", 0.0)),
            trade_count=int(item.get("n", 0)),
            vwap=float(item.get("vw", close)),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(slots=True)
class CandidateGroups:
    """訂閱候選：依消費端分組（皆已正規化、已排除不可串流的代號）。"""

    holdings: set[str] = field(default_factory=set)
    price_volume: set[str] = field(default_factory=set)
    watchlist: set[str] = field(default_factory=set)
    # 關注人數：跨價量監測與自選的不重複使用者數（持倉不計，持倉一律最優先）
    watchers: dict[str, int] = field(default_factory=dict)

    def all_symbols(self) -> set[str]:
        return self.holdings | self.price_volume | self.watchlist


def collect_candidate_groups() -> CandidateGroups:
    """讀出三組訂閱候選。

    同步函式（三次 DB 讀取），呼叫端須包在同一次 ``asyncio.to_thread`` 內。
    """
    from database.portfolio import get_all_portfolio_symbol_pairs
    from database.price_volume_watch import get_all_watches
    from database.watchlist import get_all_watchlist

    groups = CandidateGroups()
    watcher_ids: dict[str, set[Any]] = {}

    def _add(target: set[str], raw: Any, user_id: Any = None) -> None:
        sym = to_stream_symbol(str(raw))
        if not is_streamable_symbol(sym):
            return
        target.add(sym)
        if user_id is not None:
            watcher_ids.setdefault(sym, set()).add(user_id)

    for _, sym in get_all_portfolio_symbol_pairs():
        _add(groups.holdings, sym)
    for watch in get_all_watches():
        _add(groups.price_volume, watch.symbol, watch.user_id)
    for row in get_all_watchlist() or []:
        _add(groups.watchlist, row[1], row[0])
    groups.watchers = {sym: len(uids) for sym, uids in watcher_ids.items()}
    return groups


def rank_symbols(
    groups: CandidateGroups,
    activity: dict[str, int],
    large_cap_whitelist: frozenset[str] | set[str],
    cap: int,
) -> list[str]:
    """依串流的實際效益排出訂閱清單，截斷至 ``cap`` 檔。

    1. 持倉：風控最需要新鮮報價，永遠最優先。
    2. 大型股白名單內的價量監測：價量警報 live 模式唯一會採用串流的標的。
    3. 其餘候選：依前一交易日 IEX 成交筆數由高到低，同分依關注人數、再依代號。
       Tier 0 現價要求 90 秒內有真實 IEX 成交，冷門標的多數時間用不上串流，
       不該佔用名額。沒有活躍度資料的標的視為 0（排在最後）。

    同組內也依活躍度排序，持倉超過上限時先保留最活躍的。
    """

    def _key(sym: str) -> tuple[int, int, str]:
        return (-activity.get(sym, 0), -groups.watchers.get(sym, 0), sym)

    tier1 = sorted(groups.holdings, key=_key)
    tier2 = sorted(
        (groups.price_volume & set(large_cap_whitelist)) - groups.holdings, key=_key
    )
    placed = set(tier1) | set(tier2)
    tier3 = sorted(groups.all_symbols() - placed, key=_key)
    return (tier1 + tier2 + tier3)[: max(cap, 0)]


@dataclass(slots=True)
class DailyBaseline:
    session_date: str
    prev_close: float


@dataclass(slots=True)
class SymbolState:
    bars: deque[MinuteBar]
    fifteen: deque[Bar15m] = field(
        default_factory=lambda: deque(maxlen=_FIFTEEN_HISTORY_BARS)
    )
    session: Optional[SessionStats] = None
    baseline: Optional[DailyBaseline] = None
    technicals: Optional[StreamTechnicals] = None
    coverage_since: Optional[datetime] = None
    complete_since: Optional[datetime] = None
    next_window_start: Optional[datetime] = None


def _last_close_before(bars: Sequence[MinuteBar], ts: datetime) -> Optional[float]:
    for bar in reversed(bars):
        if bar.ts < ts:
            return bar.close
    return None


class AlpacaStreamService:
    def __init__(self, bot: Any = None) -> None:
        self.bot = bot
        self._running = False
        self._fatal = False
        self._connection_limited = False
        self._authenticated = False
        self._ws: Any = None
        self._states: dict[str, SymbolState] = {}
        self._desired: list[str] = []
        self._acked: set[str] = set()
        self._symbol_cap = int(config.ALPACA_MAX_STREAM_SYMBOLS)
        # 前一交易日 IEX 成交筆數（訂閱排名用），與其對應的交易日
        self._activity: dict[str, int] = {}
        self._activity_date: Optional[str] = None
        # Tier 0 命中統計：{symbol: [命中, 嘗試]}，每個交易日摘要一次後重設
        self._tier0_stats: dict[str, list[int]] = {}
        self._tier0_stats_date: Optional[str] = None
        self._session_cache: dict[date, Optional[tuple[datetime, datetime]]] = {}
        self._sub_lock = asyncio.Lock()
        self._stream_task: Optional[asyncio.Task[None]] = None
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._aux_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 狀態查詢
    # ------------------------------------------------------------------
    @property
    def is_enabled(self) -> bool:
        return bool(
            config.ENABLE_ALPACA_STREAM
            and config.ALPACA_API_KEY
            and config.ALPACA_API_SECRET
        )

    @property
    def is_connected(self) -> bool:
        """已通過認證（單純連上 socket 不算）。"""
        return self._authenticated

    @property
    def subscribed_symbols(self) -> set[str]:
        return set(self._acked)

    def classify_tier(self, symbol: str) -> StreamTier:
        return classify_symbol_tier(
            to_stream_symbol(symbol), config.ALPACA_LARGE_CAP_SYMBOLS
        )

    def get_technicals(self, symbol: str) -> Optional[StreamTechnicals]:
        st = self._states.get(to_stream_symbol(symbol))
        return st.technicals if st else None

    def get_bars(self, symbol: str) -> list[MinuteBar]:
        st = self._states.get(to_stream_symbol(symbol))
        return list(st.bars) if st else []

    # ------------------------------------------------------------------
    # 交易時段
    # ------------------------------------------------------------------
    def _session_bounds(self, day: date) -> Optional[tuple[datetime, datetime]]:
        """該美東日期的常規時段 (開盤, 收盤) UTC；非交易日回傳 None。

        行事曆查詢失敗不快取（下次重試），也不猜測時段。
        """
        if day in self._session_cache:
            return self._session_cache[day]
        try:
            raw = market_time.get_session_bounds_utc(day, day)
        except Exception as ex:
            logger.warning(f"Alpaca 串流：查詢 {day} 交易時段失敗: {ex}")
            return None
        fmt = "%Y-%m-%d %H:%M:%S"
        bounds: Optional[tuple[datetime, datetime]] = None
        entry = raw.get(day.strftime("%Y-%m-%d"))
        if entry is not None:
            bounds = (
                datetime.strptime(entry[0], fmt).replace(tzinfo=timezone.utc),
                datetime.strptime(entry[1], fmt).replace(tzinfo=timezone.utc),
            )
        if len(self._session_cache) > 16:
            self._session_cache.clear()
        self._session_cache[day] = bounds
        return bounds

    def _session_open_for(self, ts: datetime) -> Optional[datetime]:
        bounds = self._session_bounds(_ny_date(ts))
        return bounds[0] if bounds else None

    def _in_regular_session(self, ts: datetime) -> bool:
        bounds = self._session_bounds(_ny_date(ts))
        return bounds is not None and bounds[0] <= ts < bounds[1]

    # ------------------------------------------------------------------
    # K 棒寫入與 15 分 K 聚合
    # ------------------------------------------------------------------
    def _new_state(self) -> SymbolState:
        return SymbolState(bars=deque(maxlen=int(config.ALPACA_MAX_BUFFER_BARS)))

    def ingest_bar(
        self, symbol: str, bar: MinuteBar, now: Optional[datetime] = None
    ) -> bool:
        """寫入一根串流 1 分 K；回傳是否被採納。

        未訂閱的標的與常規時段以外的 K 棒（盤前盤後）一律丟棄。
        """
        sym = to_stream_symbol(symbol)
        st = self._states.get(sym)
        if st is None:
            return False
        bounds = self._session_bounds(_ny_date(bar.ts))
        if bounds is None or not (bounds[0] <= bar.ts < bounds[1]):
            return False

        replaced: Optional[MinuteBar] = None
        if st.bars and bar.ts <= st.bars[-1].ts:
            for existing in reversed(st.bars):
                if abs((existing.ts - bar.ts).total_seconds()) < 30:
                    replaced = existing
                    break
                if existing.ts < bar.ts:
                    break
        added = apply_forward_fill(
            st.bars, bar, bounds[0], int(config.ALPACA_MAX_FFILL_MINUTES)
        )
        if not added:
            return False

        session_date = _ny_date(bar.ts).isoformat()
        if st.session is None or st.session.session_date != session_date:
            st.session = SessionStats(session_date=session_date)
        if replaced is not None:
            st.session.remove(replaced)
        st.session.add(bar)

        st.technicals = compute_technicals(st.bars, sym, self.classify_tier(sym))
        self._finalize_windows(st, now or _utcnow(), st.bars)
        return True

    def _finalize_windows(
        self, st: SymbolState, now: datetime, source: Sequence[MinuteBar]
    ) -> None:
        """把今日已收盤（含寬限期）的 15 分鐘時窗依序聚合進 ``st.fifteen``。

        任何一個時窗不在「資料完整」區間內，就清空 15 分 K 歷史——均量的 20 根
        回看必須是連續的時窗，中間缺一段比退回 yfinance 更糟。
        """
        bounds = self._session_bounds(_ny_date(now))
        if bounds is None:
            return
        session_open, session_close = bounds
        nxt = st.next_window_start
        if nxt is None or nxt < session_open:
            nxt = session_open
        while (
            nxt + WINDOW_15M <= session_close and nxt + WINDOW_15M + WINDOW_GRACE <= now
        ):
            if st.complete_since is None or st.complete_since > nxt:
                st.fifteen.clear()
            else:
                prev_close = (
                    st.fifteen[-1].close
                    if st.fifteen
                    else _last_close_before(source, nxt)
                )
                agg = aggregate_15m(source, nxt, prev_close)
                if agg is None:
                    st.fifteen.clear()
                else:
                    st.fifteen.append(agg)
            nxt += WINDOW_15M
        st.next_window_start = nxt

    # ------------------------------------------------------------------
    # 下游查詢
    # ------------------------------------------------------------------
    def get_quote_snapshot(
        self, symbol: str, now: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """回傳 Finnhub ``/quote`` 相容的現價字典；任一條件不足回傳 None。

        ``c`` 是最後一根真實成交 1 分 K 的收盤價、``pc`` 是官方日線昨收、``o/h/l``
        是今日常規時段的串流累計（IEX 單一交易所成交，極值可能略窄於全市場）。
        """
        if not self._authenticated:
            return None
        st = self._states.get(to_stream_symbol(symbol))
        if st is None or st.coverage_since is None:
            return None
        now = now or _utcnow()
        today = _ny_date(now)
        bounds = self._session_bounds(today)
        if bounds is None or not (bounds[0] <= now < bounds[1]):
            return None
        stats = self._tier0_stats.setdefault(to_stream_symbol(symbol), [0, 0])
        stats[1] += 1
        # 今日開盤後才完整的資料，開盤價與高低點不可信。
        if st.complete_since is None or st.complete_since > bounds[0]:
            return None
        sess = st.session
        today_str = today.isoformat()
        if (
            sess is None
            or sess.session_date != today_str
            or sess.last_real_bar_end is None
            or sess.last_close <= 0.0
        ):
            return None
        age = (now - sess.last_real_bar_end).total_seconds()
        if age > float(config.ALPACA_TIER0_MAX_AGE_SECONDS):
            return None
        baseline = st.baseline
        if baseline is None or baseline.session_date != today_str:
            return None
        prev_close = baseline.prev_close
        if prev_close <= 0.0:
            return None

        stats[0] += 1
        price = sess.last_close
        change = price - prev_close
        return {
            "c": round(price, 2),
            "d": round(change, 2),
            "dp": round(change / prev_close * 100.0, 4),
            "h": round(sess.high, 2),
            "l": round(sess.low, 2),
            "o": round(sess.open, 2),
            "pc": round(prev_close, 2),
            "t": int(sess.last_real_bar_end.timestamp()),
        }

    def get_confirmed_15m_bar(
        self,
        symbol: str,
        lookback_bars: int,
        now: Optional[datetime] = None,
    ) -> Optional[tuple[Bar15m, float]]:
        """回傳 (最近一根已收盤的 15 分 K, 前 ``lookback_bars`` 根均量)。

        只有當該時窗正是「現在應該看到的最近一根」、時窗全程資料完整、且前面有
        足夠的連續 15 分 K 時才回傳；否則 None（呼叫端退回 yfinance）。
        """
        if not self._authenticated:
            return None
        st = self._states.get(to_stream_symbol(symbol))
        if st is None or st.complete_since is None:
            return None
        now = now or _utcnow()
        bounds = self._session_bounds(_ny_date(now))
        if bounds is None:
            return None
        session_open, session_close = bounds

        self._finalize_windows(st, now, st.bars)

        if now >= session_close + WINDOW_GRACE:
            expected = session_close - WINDOW_15M
        else:
            elapsed = now - WINDOW_GRACE - session_open
            completed = int(elapsed // WINDOW_15M)
            if completed < 1:
                return None
            expected = session_open + (completed - 1) * WINDOW_15M

        if not st.fifteen or st.fifteen[-1].start != expected:
            return None
        if st.complete_since > expected:
            return None
        if len(st.fifteen) < lookback_bars + 1:
            return None
        history = list(st.fifteen)
        lookback = history[-(lookback_bars + 1) : -1]
        avg_volume = sum(b.volume for b in lookback) / float(len(lookback))
        return history[-1], avg_volume

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        if not self.is_enabled:
            logger.info("ℹ️ Alpaca 即時串流未啟用 (ENABLE_ALPACA_STREAM / 金鑰未設定)")
            return
        self._running = True
        self._fatal = False
        self._stream_task = asyncio.create_task(self._run_stream_loop())
        self._refresh_task = asyncio.create_task(self._run_refresh_loop())
        logger.info("🚀 Alpaca 即時串流服務已啟動")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in (self._stream_task, self._refresh_task, *self._aux_tasks):
            if task is not None and not task.done():
                task.cancel()
        self._stream_task = None
        self._refresh_task = None
        self._aux_tasks.clear()
        self._on_disconnect()
        logger.info("🛑 Alpaca 即時串流服務已關閉")

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)

    @property
    def _stream_url(self) -> str:
        feed = (
            config.ALPACA_DATA_FEED
            if config.ALPACA_DATA_FEED in ("iex", "sip")
            else "iex"
        )
        return STOCK_STREAM_URL_TEMPLATE.format(feed=feed)

    def _on_disconnect(self) -> None:
        self._ws = None
        self._authenticated = False
        self._acked = set()
        for st in self._states.values():
            st.coverage_since = None
            st.complete_since = None
            st.next_window_start = None
            st.fifteen.clear()

    async def _run_stream_loop(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                logger.info(f"Alpaca 串流：連線至 {self._stream_url}")
                async with websockets.connect(
                    self._stream_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    self._ws = ws
                    await ws.send(
                        json.dumps(
                            {
                                "action": "auth",
                                "key": config.ALPACA_API_KEY,
                                "secret": config.ALPACA_API_SECRET,
                            }
                        )
                    )
                    async for raw in ws:
                        if not self._running:
                            break
                        await self.handle_payload(raw)
                        if self._fatal:
                            break
                        if self._authenticated:
                            backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.warning(f"Alpaca 串流連線異常: {ex}")
            finally:
                self._on_disconnect()

            if self._fatal or not self._running:
                break
            delay = max(backoff, 60.0) if self._connection_limited else backoff
            self._connection_limited = False
            logger.warning(f"Alpaca 串流將於 {delay:.0f} 秒後重連")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2.0, 300.0)

        if self._fatal:
            logger.error("❌ Alpaca 串流因不可重試的錯誤停止，請檢查金鑰與資料方案")

    async def _run_refresh_loop(self) -> None:
        while self._running:
            try:
                await self.refresh_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.warning(f"Alpaca 串流：刷新訂閱失敗: {ex}")
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)

    async def refresh_subscriptions(self) -> None:
        """重新計算訂閱清單並與目前訂閱比對，順便補齊今日昨收基準與 15 分 K。

        活躍度每個交易日只查一次（新出現的候選才補查），所以同一天內的排名只會
        因候選增減而變動，不會在標的之間反覆換檔（每次換檔都要退訂、重訂、回補）。
        """
        self._log_tier0_stats_if_new_day()
        groups = await asyncio.to_thread(collect_candidate_groups)
        await self._ensure_activity(groups.all_symbols())
        self._desired = rank_symbols(
            groups, self._activity, config.ALPACA_LARGE_CAP_SYMBOLS, self._symbol_cap
        )
        await self._sync_subscriptions()
        now = _utcnow()
        for st in self._states.values():
            self._finalize_windows(st, now, st.bars)
        if self._acked:
            await self._ensure_baselines(sorted(self._acked))

    # ------------------------------------------------------------------
    # 訊息處理
    # ------------------------------------------------------------------
    async def handle_payload(self, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as ex:
            logger.warning(f"Alpaca 串流：無法解析訊息: {ex}")
            return
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                await self._handle_message(item)
            except Exception as ex:
                logger.warning(f"Alpaca 串流：處理訊息失敗 ({item.get('T')}): {ex}")

    async def _handle_message(self, item: dict[str, Any]) -> None:
        msg_type = item.get("T")
        if msg_type == "success":
            if item.get("msg") == "authenticated":
                self._authenticated = True
                logger.info("✅ Alpaca 串流認證成功")
                await self._sync_subscriptions()
        elif msg_type == "subscription":
            self._on_subscription_ack(item)
        elif msg_type in ("b", "u"):
            bar = parse_bar(item)
            symbol = item.get("S")
            if bar is not None and isinstance(symbol, str):
                self.ingest_bar(symbol, bar)
        elif msg_type == "error":
            await self._on_error(item)

    async def _on_error(self, item: dict[str, Any]) -> None:
        code = item.get("code")
        msg = item.get("msg")
        if code in _FATAL_ERROR_CODES:
            self._fatal = True
            logger.error(f"❌ Alpaca 串流錯誤 {code}: {msg}（不再重試）")
        elif code == _ERR_CONNECTION_LIMIT:
            self._connection_limited = True
            logger.warning(
                f"Alpaca 串流連線數超限 ({msg})，可能是藍綠部署重疊，延長退避"
            )
        elif code == _ERR_SYMBOL_LIMIT:
            current = min(self._symbol_cap, max(len(self._desired), 1))
            self._symbol_cap = max(1, current - 5)
            self._desired = self._desired[: self._symbol_cap]
            logger.warning(
                f"Alpaca 串流訂閱數超過方案上限，降為 {self._symbol_cap} 檔後重新訂閱"
            )
            await self._sync_subscriptions()
        else:
            logger.warning(f"Alpaca 串流錯誤 {code}: {msg}")

    async def _sync_subscriptions(self) -> None:
        async with self._sub_lock:
            ws = self._ws
            if not self._authenticated or ws is None:
                return
            desired = set(self._desired)
            to_remove = sorted(self._acked - desired)
            to_add = sorted(desired - self._acked)
            if to_remove:
                await ws.send(
                    json.dumps(
                        {
                            "action": "unsubscribe",
                            "bars": to_remove,
                            "updatedBars": to_remove,
                        }
                    )
                )
            if to_add:
                await ws.send(
                    json.dumps(
                        {"action": "subscribe", "bars": to_add, "updatedBars": to_add}
                    )
                )
            if to_add or to_remove:
                logger.info(f"Alpaca 串流訂閱變更：+{to_add} -{to_remove}")

    def _on_subscription_ack(
        self, item: dict[str, Any], now: Optional[datetime] = None
    ) -> None:
        acked = {str(s) for s in (item.get("bars") or [])}
        now = now or _utcnow()
        added = sorted(acked - self._acked)
        for sym in list(self._states):
            if sym not in acked:
                del self._states[sym]
        for sym in added:
            st = self._states.get(sym)
            if st is None:
                st = self._new_state()
                self._states[sym] = st
            st.coverage_since = now
            st.complete_since = None
            st.next_window_start = None
            st.fifteen.clear()
        self._acked = acked
        logger.info(f"Alpaca 串流目前訂閱 {len(acked)} 檔")
        if added:
            self._spawn(self._prepare_symbols(added))

    # ------------------------------------------------------------------
    # 回補與昨收基準
    # ------------------------------------------------------------------
    async def _prepare_symbols(self, symbols: list[str]) -> None:
        try:
            await self._ensure_baselines(symbols)
            await self._seed_symbols(symbols)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning(f"Alpaca 串流：回補 {symbols} 失敗: {ex}")

    async def _ensure_baselines(self, symbols: Sequence[str]) -> None:
        """確保每檔都有今日的官方昨收（日線 6 小時快取，每交易日每檔最多一次網路請求）。"""
        from services import market_data_service

        today = _ny_date(_utcnow())
        today_str = today.isoformat()
        sem = asyncio.Semaphore(_SEED_CONCURRENCY)

        async def _one(sym: str) -> None:
            st = self._states.get(sym)
            if st is None or (st.baseline and st.baseline.session_date == today_str):
                return
            async with sem:
                try:
                    df = await market_data_service.get_history_df(
                        sym.replace(".", "-"), period="5d", interval="1d"
                    )
                except Exception as ex:
                    logger.warning(f"[{sym}] Alpaca 串流：取得日線昨收失敗: {ex}")
                    return
            prev_close = _prev_close_from_daily(df, today)
            st = self._states.get(sym)
            if st is not None and prev_close is not None:
                st.baseline = DailyBaseline(
                    session_date=today_str, prev_close=prev_close
                )

        await asyncio.gather(*[_one(s) for s in symbols])

    async def _seed_symbols(
        self, symbols: list[str], now: Optional[datetime] = None
    ) -> None:
        """以 REST 回補今日 1 分 K 與今日開盤前的 15 分 K（皆為 IEX，與串流同源）。"""
        from services.llm_service import is_memory_safe

        explicit_now = now
        now = now or _utcnow()
        if not is_memory_safe():
            logger.warning("Alpaca 串流：記憶體水位過高，略過歷史回補")
            self._mark_seed_failed(symbols)
            return

        today = _ny_date(now)
        bounds = self._session_bounds(today)
        session_started = bounds is not None and now > bounds[0]
        minute_rows: Optional[dict[str, list[dict[str, Any]]]] = {}
        if session_started and bounds is not None:
            minute_rows = await self.fetch_historical_bars(
                symbols, "1Min", bounds[0], now
            )
        prior_end = bounds[0] if (session_started and bounds is not None) else now
        fifteen_rows = await self.fetch_historical_bars(
            symbols, "15Min", prior_end - _FIFTEEN_SEED_LOOKBACK, prior_end
        )
        if minute_rows is None or fifteen_rows is None:
            self._mark_seed_failed(symbols)
            return

        # 兩次網路往返之後，時窗收盤判斷要用當下時間。
        now = explicit_now or _utcnow()
        for sym in symbols:
            st = self._states.get(sym)
            if st is None or st.coverage_since is None:
                continue
            seeded = [
                b
                for b in (parse_bar(r) for r in minute_rows.get(sym, []))
                if b is not None and self._in_regular_session(b.ts)
            ]
            merged: dict[datetime, MinuteBar] = {b.ts: b for b in seeded}
            merged.update({b.ts: b for b in st.bars if not b.is_forward_filled})
            all_bars = sorted(merged.values(), key=lambda b: b.ts)

            st.bars = rebuild_buffer(
                all_bars,
                int(config.ALPACA_MAX_BUFFER_BARS),
                self._session_open_for,
                int(config.ALPACA_MAX_FFILL_MINUTES),
            )
            today_str = today.isoformat()
            st.session = SessionStats(session_date=today_str)
            for bar in all_bars:
                if _ny_date(bar.ts) == today:
                    st.session.add(bar)
            st.technicals = compute_technicals(st.bars, sym, self.classify_tier(sym))

            prior: list[Bar15m] = []
            for row in fifteen_rows.get(sym, []):
                b = parse_bar(row)
                if b is None or not self._in_regular_session(b.ts):
                    continue
                prior.append(
                    Bar15m(
                        start=b.ts,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        volume=b.volume,
                    )
                )
            prior.sort(key=lambda b: b.start)
            st.fifteen = deque(
                prior[-_FIFTEEN_SEED_PRIOR_BARS:], maxlen=_FIFTEEN_HISTORY_BARS
            )
            if session_started and bounds is not None:
                st.complete_since = bounds[0]
                st.next_window_start = bounds[0]
            else:
                st.complete_since = st.coverage_since
                st.next_window_start = None
            self._finalize_windows(st, now, all_bars)

    def _mark_seed_failed(self, symbols: Sequence[str]) -> None:
        for sym in symbols:
            st = self._states.get(sym)
            if st is None:
                continue
            st.complete_since = st.coverage_since
            st.next_window_start = None
            st.fifteen.clear()

    async def _ensure_activity(self, symbols: set[str]) -> None:
        """確保每個候選都有前一交易日的 IEX 成交筆數（每個交易日只查一次）。

        換日時整批重查；同一天內只補查新出現的候選。查無資料的標的記為 0，
        避免每 15 分鐘重複查詢。查詢失敗時保留既有值，缺值者視為 0。
        """
        if not symbols:
            return
        try:
            ref_date = market_time.get_last_completed_trading_date()
        except Exception as ex:
            logger.warning(f"Alpaca 串流：取得最近交易日失敗，沿用既有活躍度: {ex}")
            return
        if ref_date != self._activity_date:
            self._activity = {}
            self._activity_date = ref_date
        missing = sorted(symbols - set(self._activity))
        if not missing:
            return

        ref = datetime.strptime(ref_date, "%Y-%m-%d").date()
        start = datetime.combine(
            ref - timedelta(days=10), datetime.min.time(), timezone.utc
        )
        end = datetime.combine(
            ref + timedelta(days=1), datetime.min.time(), timezone.utc
        )
        fetched: dict[str, int] = {}
        for i in range(0, len(missing), _ACTIVITY_CHUNK):
            chunk = missing[i : i + _ACTIVITY_CHUNK]
            rows = await self.fetch_historical_bars(chunk, "1Day", start, end)
            if rows is None:
                return  # 失敗：不寫入 0，下次刷新重試
            for sym in chunk:
                fetched[sym] = _trade_count_on_or_before(rows.get(sym, []), ref)
        self._activity.update(fetched)
        # 只保留仍是候選的標的
        self._activity = {k: v for k, v in self._activity.items() if k in symbols}
        logger.info(f"Alpaca 串流：已更新 {len(fetched)} 檔 {ref_date} IEX 成交筆數")

    def _log_tier0_stats_if_new_day(self) -> None:
        today = _ny_date(_utcnow()).isoformat()
        if self._tier0_stats_date == today:
            return
        if self._tier0_stats_date is not None and self._tier0_stats:
            rows = sorted(
                self._tier0_stats.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1)
            )
            summary = ", ".join(
                f"{sym} {hits}/{tries}" for sym, (hits, tries) in rows if tries > 0
            )
            logger.info(
                f"📈 Alpaca Tier 0 命中率 ({self._tier0_stats_date}，由低到高): {summary}"
            )
        self._tier0_stats = {}
        self._tier0_stats_date = today

    async def fetch_historical_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Optional[dict[str, list[dict[str, Any]]]]:
        """批次呼叫 Alpaca 歷史 K 線 REST（多標的單一請求、自動翻頁）；失敗回傳 None。"""
        if not symbols:
            return {}
        feed = (
            config.ALPACA_DATA_FEED
            if config.ALPACA_DATA_FEED in ("iex", "sip")
            else "iex"
        )
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _to_rfc3339(start),
            "end": _to_rfc3339(end),
            "feed": feed,
            "adjustment": "raw",
            "limit": 10000,
            "sort": "asc",
        }
        headers = {
            "APCA-API-KEY-ID": str(config.ALPACA_API_KEY),
            "APCA-API-SECRET-KEY": str(config.ALPACA_API_SECRET),
        }
        out: dict[str, list[dict[str, Any]]] = {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for _ in range(_MAX_PAGES):
                    resp = await client.get(
                        HISTORICAL_BARS_URL, params=params, headers=headers
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    for sym, rows in (payload.get("bars") or {}).items():
                        out.setdefault(str(sym), []).extend(rows or [])
                    token = payload.get("next_page_token")
                    if not token:
                        break
                    params["page_token"] = token
        except Exception as ex:
            logger.warning(f"Alpaca 歷史 K 線 ({timeframe}) 回補失敗: {ex}")
            return None
        return out


def _prev_close_from_daily(df: Any, today: date) -> Optional[float]:
    """日線 DataFrame 中最後一根日期早於今日的收盤價（今日成形中的日 K 不算）。"""
    if df is None or getattr(df, "empty", True) or "Close" not in df:
        return None
    for idx, close in zip(reversed(df.index), reversed(df["Close"].tolist())):
        try:
            idx_date = idx.date() if hasattr(idx, "date") else None
        except Exception:
            idx_date = None
        if idx_date is None or idx_date >= today:
            continue
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return value
    return None


def _trade_count_on_or_before(rows: Sequence[dict[str, Any]], ref: date) -> int:
    """日 K 中最後一根日期不晚於 ``ref`` 的成交筆數（``n``）；無資料回傳 0。"""
    best: Optional[tuple[date, int]] = None
    for row in rows:
        ts = _parse_ts(row.get("t"))
        if ts is None:
            continue
        d = _ny_date(ts)
        if d > ref:
            continue
        try:
            n = int(row.get("n", 0))
        except (TypeError, ValueError):
            continue
        if best is None or d > best[0]:
            best = (d, n)
    return best[1] if best else 0
