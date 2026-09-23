"""stream_bars.py — Alpaca 即時 1 分 K 串流的純計算層。

本模組只負責「一串分鐘 K 棒 → 補齊後的序列／當日時段統計／15 分 K 聚合／技術指標」
的純函式運算；連線、訂閱與回補全部在 ``services/alpaca_stream_service.py``。

設計約束（刻意維持）：

* **只依賴 stdlib**。比照 ``room_threshold.py`` / ``sentiment/skew_taxonomy.py``
  的葉模組設計，才能同時被 ``services/`` 與 ``market_analysis/`` 匯入而不產生
  循環相依，也讓單元測試不需要任何網路或資料庫。
* **時間戳一律是 UTC、代表 K 棒的「起始」時間**（Alpaca 的 ``t`` 欄位語意）。
  一根 1 分 K 在 ``ts + 60s`` 才收盤、才會被送達；任何「新鮮度」判斷都必須以
  收盤時刻 (``bar_end``) 為準，而非 ``ts``——以 ``ts`` 計算年齡，K 棒送達當下就
  已經超過 60 秒。
* **Forward Fill 的合成 K 棒只用來維持時間網格**（讓 EMA／RSI 的每一步都代表
  1 分鐘）。它們不得進入 VWAP、當日高低點或「最後成交時間」的計算——那些數字
  必須只反映真實成交。
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Iterable, MutableSequence, Optional, Sequence

BAR_INTERVAL = timedelta(minutes=1)
WINDOW_15M = timedelta(minutes=15)


class StreamTier(str, Enum):
    """串流標的分級。

    分級不決定是否 Forward Fill（所有美股一律補齊，無缺口時不會產生任何合成
    K 棒），只決定下游是否信任 IEX 單一交易所的成交量：IEX 約佔全市場成交量
    2~3%，對中小型股而言大多數分鐘根本沒有 IEX 成交，其放量倍數雜訊極大。
    """

    LARGE_CAP_US = "large_cap_us"
    SMALL_MID_CAP_US = "small_mid_cap_us"


def classify_symbol_tier(symbol: str, large_cap_whitelist: Iterable[str]) -> StreamTier:
    """以白名單判定分級（不以日均量推斷：白名單以外一律視為中小型股）。"""
    clean = symbol.strip().upper()
    whitelist = {s.strip().upper() for s in large_cap_whitelist}
    return (
        StreamTier.LARGE_CAP_US if clean in whitelist else StreamTier.SMALL_MID_CAP_US
    )


@dataclass(slots=True, frozen=True)
class MinuteBar:
    """一根 1 分 K。``ts`` 為 UTC、K 棒起始時間。"""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int = 0
    vwap: float = 0.0
    is_forward_filled: bool = False

    @property
    def bar_end(self) -> datetime:
        return self.ts + BAR_INTERVAL


@dataclass(slots=True, frozen=True)
class Bar15m:
    """一根已收盤的 15 分 K。``start`` 為 UTC、時窗起始時間。"""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def apply_forward_fill(
    buffer: MutableSequence[MinuteBar],
    incoming: MinuteBar,
    session_open_utc: Optional[datetime],
    max_gap_minutes: int,
) -> list[MinuteBar]:
    """把 ``incoming`` 併入 ``buffer``，必要時以前收補齊缺漏的分鐘。

    規則：
      * 同一分鐘（誤差 < 30 秒）→ 取代最後一根（Alpaca ``updatedBars`` 的遲到成交
        修正走這條）。
      * 比最後一根更舊 → 若緩衝區內有同一分鐘則取代，否則丟棄（亂序到達）。
      * 缺口 2..``max_gap_minutes`` 分鐘 → 以前收補齊中間每一分鐘（Volume=0），
        但合成 K 棒不得早於 ``session_open_utc``。
      * 缺口更大（停牌、跨日）→ 不補，直接接續，避免一次產生數百根合成 K 棒。

    回傳本次新增（或取代）進緩衝區的 K 棒，依時間排序。
    """
    if not buffer:
        buffer.append(incoming)
        return [incoming]

    last = buffer[-1]
    delta_seconds = (incoming.ts - last.ts).total_seconds()

    if abs(delta_seconds) < 30:
        buffer[-1] = incoming
        return [incoming]

    if delta_seconds < 0:
        for idx in range(len(buffer) - 1, -1, -1):
            if abs((buffer[idx].ts - incoming.ts).total_seconds()) < 30:
                buffer[idx] = incoming
                return [incoming]
            if buffer[idx].ts < incoming.ts:
                break
        return []

    gap_minutes = int(round(delta_seconds / 60.0))
    added: list[MinuteBar] = []
    if 1 < gap_minutes <= max_gap_minutes:
        prev_close = last.close
        for step in range(1, gap_minutes):
            fill_ts = last.ts + step * BAR_INTERVAL
            if session_open_utc is not None and fill_ts < session_open_utc:
                continue
            synthetic = MinuteBar(
                ts=fill_ts,
                open=prev_close,
                high=prev_close,
                low=prev_close,
                close=prev_close,
                volume=0.0,
                trade_count=0,
                vwap=prev_close,
                is_forward_filled=True,
            )
            buffer.append(synthetic)
            added.append(synthetic)

    buffer.append(incoming)
    added.append(incoming)
    return added


def rebuild_buffer(
    bars: Iterable[MinuteBar],
    maxlen: int,
    session_open_for: Callable[[datetime], Optional[datetime]],
    max_gap_minutes: int,
) -> deque[MinuteBar]:
    """由一串（可能亂序、含重複的）真實 K 棒重建補齊後的緩衝區。

    ``session_open_for(ts)`` 回傳該 K 棒所屬交易時段的開盤 UTC 時刻（或 None）。
    """
    ordered = sorted((b for b in bars if not b.is_forward_filled), key=lambda b: b.ts)
    buffer: deque[MinuteBar] = deque(maxlen=maxlen)
    for bar in ordered:
        apply_forward_fill(buffer, bar, session_open_for(bar.ts), max_gap_minutes)
    return buffer


@dataclass(slots=True)
class SessionStats:
    """當日常規時段的累計統計（只納入真實 K 棒）。

    與 200 根的分鐘緩衝區分開維護：緩衝區滾動後早盤的 K 棒會被擠出，但當日開盤
    價、高低點與錨定開盤的 VWAP 必須涵蓋整個時段。
    """

    session_date: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    last_close: float = 0.0
    last_real_bar_end: Optional[datetime] = None
    first_ts: Optional[datetime] = None
    pv_sum: float = 0.0
    volume_sum: float = 0.0

    def add(self, bar: MinuteBar) -> None:
        if bar.is_forward_filled:
            return
        if self.first_ts is None or bar.ts <= self.first_ts:
            self.first_ts = bar.ts
            self.open = bar.open
        self.high = bar.high if self.high == 0.0 else max(self.high, bar.high)
        self.low = bar.low if self.low == 0.0 else min(self.low, bar.low)
        if self.last_real_bar_end is None or bar.bar_end >= self.last_real_bar_end:
            self.last_real_bar_end = bar.bar_end
            self.last_close = bar.close
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.pv_sum += typical * bar.volume
        self.volume_sum += bar.volume

    def remove(self, bar: MinuteBar) -> None:
        """撤銷某根 K 棒對 VWAP 累計的貢獻（``updatedBars`` 取代舊值時使用）。

        高低點不回退：修正後的 K 棒只可能擴大、不會收窄真實成交區間。
        """
        if bar.is_forward_filled:
            return
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.pv_sum -= typical * bar.volume
        self.volume_sum -= bar.volume

    @property
    def vwap(self) -> Optional[float]:
        if self.volume_sum <= 0.0:
            return None
        return self.pv_sum / self.volume_sum


def aggregate_15m(
    bars: Sequence[MinuteBar],
    window_start: datetime,
    prev_close: Optional[float],
) -> Optional[Bar15m]:
    """把 ``[window_start, window_start + 15m)`` 內的真實 1 分 K 聚合為 15 分 K。

    時窗內沒有任何真實成交時，以 ``prev_close`` 產生一根 Volume=0 的平盤 K 棒
    （與 IEX 冷清時段的真實狀況一致）；連 ``prev_close`` 都沒有則回傳 None。
    """
    window_end = window_start + WINDOW_15M
    real = [
        b for b in bars if not b.is_forward_filled and window_start <= b.ts < window_end
    ]
    if not real:
        if prev_close is None:
            return None
        return Bar15m(
            start=window_start,
            open=prev_close,
            high=prev_close,
            low=prev_close,
            close=prev_close,
            volume=0.0,
        )
    real.sort(key=lambda b: b.ts)
    return Bar15m(
        start=window_start,
        open=real[0].open,
        high=max(b.high for b in real),
        low=min(b.low for b in real),
        close=real[-1].close,
        volume=sum(b.volume for b in real),
    )


@dataclass(slots=True, frozen=True)
class StreamTechnicals:
    """分鐘級技術指標快照（以補齊後的時間網格計算）。"""

    symbol: str
    tier: StreamTier
    price: float
    ema_9: Optional[float]
    ema_21: Optional[float]
    sma_20: Optional[float]
    sma_50: Optional[float]
    rsi_14: Optional[float]
    volume_spike_ratio: float
    bars_count: int
    ffill_count: int


def _ema(values: Sequence[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    k = 2.0 / (length + 1.0)
    ema = sum(values[:length]) / float(length)
    for val in values[length:]:
        ema = val * k + ema * (1.0 - k)
    return round(ema, 4)


def _sma(values: Sequence[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    return round(sum(values[-length:]) / float(length), 4)


def _wilder_rsi(closes: Sequence[float], length: int = 14) -> Optional[float]:
    if len(closes) < length + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:length]) / float(length)
    avg_loss = sum(losses[:length]) / float(length)
    for i in range(length, len(deltas)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / float(length)
        avg_loss = (avg_loss * (length - 1) + losses[i]) / float(length)
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def compute_technicals(
    bars: Sequence[MinuteBar], symbol: str, tier: StreamTier
) -> Optional[StreamTechnicals]:
    """以補齊後的分鐘序列計算 EMA／SMA／RSI 與放量倍數；空序列回傳 None。

    放量倍數的分母刻意包含 Forward Fill 的 Volume=0：冷清時段的均量本來就低，
    突然有單進場時倍數應該靈敏反映。
    """
    if not bars:
        return None
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    latest = bars[-1]

    spike = 1.0
    if len(bars) >= 2:
        lookback = volumes[-21:-1]
        avg_vol = sum(lookback) / float(len(lookback))
        if avg_vol > 0:
            spike = round(latest.volume / avg_vol, 2)
        elif latest.volume > 0:
            spike = 5.0

    return StreamTechnicals(
        symbol=symbol,
        tier=tier,
        price=round(latest.close, 4),
        ema_9=_ema(closes, 9),
        ema_21=_ema(closes, 21),
        sma_20=_sma(closes, 20),
        sma_50=_sma(closes, 50),
        rsi_14=_wilder_rsi(closes, 14),
        volume_spike_ratio=spike,
        bars_count=len(bars),
        ffill_count=sum(1 for b in bars if b.is_forward_filled),
    )
