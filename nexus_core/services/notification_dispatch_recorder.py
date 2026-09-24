"""notification_dispatch_recorder.py — 已送達可行動通知的前向蒐集記錄器。

為什麼需要：每個通知頻道「讓使用者照做」究竟改善還是傷害了 B&H 投組的 Sortino /
MDD / CVaR，目前沒有任何資料可以回答。唯一的資料來源是從現在開始，把使用者**實際收到**
的可行動訊號記下來，再由 03:30 ET 離峰排程回填 20 個交易日的反事實路徑
（`services/regime_outcome_labeler.py::run_dispatch_outcome_labeling`）。

設計（比照 `market_analysis/evaluation_recorder.py`）：
* `services/notification_dispatcher.notify()` 只在**實際入列之後**呼叫 `record_dispatch()`；
  頻道關閉、去重擋下的事件不會出現在這裡。
* 記錄只是 append 到有界 deque（O(1)、無 I/O）；`flush_dispatch_records()` 由各排程週期
  結尾一次批次寫入（單一交易）。flush 長期失敗時丟棄最舊紀錄而非無限成長（1GB VPS）。
* 只有 leader 實例才記錄：`bot._is_leader_instance is True`（非 leader 的 `queue_dm` 本來
  就不入列）。單元測試的 bot 是 MagicMock，該屬性不是 `True`，因此不會污染資料。
* `config.ENABLE_NOTIFICATION_DISPATCH_LOG=false` 時完全 no-op。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SignalKind = Literal["ENTRY", "REDUCE", "EXIT", "INFO"]
SignalDirection = Literal["LONG", "SHORT"]

_BUFFER_MAXLEN = 512
_NY_TZ = ZoneInfo("America/New_York")

_BUFFER: deque[dict[str, Any]] = deque(maxlen=_BUFFER_MAXLEN)


@dataclass(frozen=True)
class DispatchRecord:
    """一則可行動通知的反事實描述，由推播呼叫端明確填入。

    - `signal_kind`：ENTRY（以 `exposure_ratio` 單位新進場）、REDUCE（持倉降低
      `exposure_ratio` 比例）、EXIT（持倉出場至現金）、INFO（純資訊，不計算反事實）。
    - `direction`：ENTRY 為新部位方向；REDUCE / EXIT 為被調整的既有部位方向。
    - `price`：送達當下的參考價；缺值時 labeler 以送達前最後一根日線收盤代替。
    """

    symbol: str
    signal_kind: SignalKind
    scenario: str = ""
    action: str = ""
    direction: SignalDirection = "LONG"
    exposure_ratio: float = 1.0
    price: Optional[float] = None


def _enabled() -> bool:
    try:
        import config

        return bool(getattr(config, "ENABLE_NOTIFICATION_DISPATCH_LOG", True))
    except Exception:
        return False


def _clean_price(price: Optional[float]) -> Optional[float]:
    try:
        value = float(price) if price is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value > 0 else None


def record_dispatch(
    bot: Any,
    user_id: int,
    channel: str,
    record: DispatchRecord,
    now: Optional[datetime] = None,
) -> None:
    """在實際入列後呼叫；非 leader 實例或功能關閉時不記錄。"""
    if getattr(bot, "_is_leader_instance", False) is not True or not _enabled():
        return
    symbol = str(record.symbol or "").upper()
    if not symbol:
        return
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ratio = max(0.0, min(1.0, float(record.exposure_ratio)))
    _BUFFER.append(
        {
            "dispatched_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": moment.astimezone(_NY_TZ).strftime("%Y-%m-%d"),
            "user_id": int(user_id),
            "channel": channel,
            "symbol": symbol,
            "scenario": record.scenario or "",
            "action": record.action or "",
            "signal_kind": record.signal_kind,
            "direction": record.direction,
            "exposure_ratio": ratio,
            "price": _clean_price(record.price),
        }
    )


def pending_count() -> int:
    return len(_BUFFER)


def clear_buffer() -> None:
    _BUFFER.clear()


async def flush_dispatch_records() -> int:
    """把緩衝區一次批次寫入；失敗時把紀錄放回緩衝區（受 maxlen 限制）。"""
    if not _BUFFER:
        return 0
    rows = list(_BUFFER)
    _BUFFER.clear()
    try:
        from database.notification_dispatch_log import insert_dispatch_records

        return await insert_dispatch_records(rows)
    except Exception as e:
        logger.error(f"[DispatchLog] 寫入通知送達紀錄失敗 ({len(rows)} 筆): {e}")
        _BUFFER.extendleft(reversed(rows))
        return 0


# ---------------------------------------------------------------------------
# 動態轉倉指令 → DispatchRecord
# ---------------------------------------------------------------------------


def rollover_dispatch_record(ins: dict[str, Any]) -> DispatchRecord:
    """把動態轉倉指令轉成反事實描述。

    - LIQUIDATE → EXIT；REDUCE → REDUCE（`sell_ratio`）；OPEN_PYRAMID → ENTRY；
      OPEN_SHORT → 做空 ENTRY；BUY_PROTECTIVE_PUT → EXIT（保護性 Put 的上限近似：
      視為把下行完全移除，高估其效果，判讀時須知悉）。
    - ADVISORY（B&H 顧問）只有結構失效類（exit_tier 以 SL 開頭）視為 EXIT，目標區
      位階告知為 INFO。
    - 期權部位（instrument_type == OPTIONS）與 HOLD / BTC 等無法以標的價格路徑近似的
      動作一律 INFO：仍留下送達紀錄，但不計算反事實。
    """
    action = str(ins.get("action") or "").upper()
    scenario = str(ins.get("scenario") or "")
    symbol = str(ins.get("symbol") or "")
    price = ins.get("limit_price")
    held_direction: SignalDirection = (
        "SHORT" if str(ins.get("direction") or "").upper() == "SHORT" else "LONG"
    )

    kind: SignalKind = "INFO"
    direction: SignalDirection = held_direction
    ratio = 1.0
    if str(ins.get("instrument_type") or "SPOT").upper() == "OPTIONS":
        kind = "INFO"
    elif action == "LIQUIDATE" or action == "BUY_PROTECTIVE_PUT":
        kind = "EXIT"
    elif action == "REDUCE":
        kind = "REDUCE"
        try:
            ratio = float(ins.get("sell_ratio") or 0.0)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio <= 0:
            kind = "INFO"
    elif action == "OPEN_PYRAMID":
        kind, direction = "ENTRY", "LONG"
    elif action == "OPEN_SHORT":
        kind, direction = "ENTRY", "SHORT"
    elif action == "ADVISORY":
        tier = str(ins.get("exit_tier") or "").upper()
        kind = "EXIT" if tier.startswith("SL") else "INFO"

    try:
        price_value: Optional[float] = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_value = None
    return DispatchRecord(
        symbol=symbol,
        signal_kind=kind,
        scenario=scenario,
        action=action,
        direction=direction,
        exposure_ratio=ratio,
        price=price_value,
    )
