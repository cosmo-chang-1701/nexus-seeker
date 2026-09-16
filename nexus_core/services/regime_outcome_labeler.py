"""regime_outcome_labeler.py — 為前向蒐集的評估紀錄回填事後走勢 (離峰排程)。

由 `cogs/trading/scheduler.py::regime_outcome_labeler` 於 03:30 ET 呼叫。流程：

1. 讀取評估時間早於「已走完 5 個交易日」保守截點、尚未標註的紀錄。
2. 依標的分組，**逐一**抓取 K 線 (間隔 1 秒，避免觸發 Yahoo 限流)：
   紀錄在 55 天內用 15m (yfinance 上限 60 天)，否則退回 1h (yfinance 上限 730 天，取 729 天避開邊界拒絕)；
   紀錄未帶 ATR₁D 時另抓日線 6 個月計算 (只取評估日以前的 K 棒，無前視)。
3. 標註計算 (純 pandas) 丟到 `asyncio.to_thread`，不佔 event loop。
4. 單一交易批次寫入；抓不到資料的紀錄標為 NO_DATA。
5. 執行保留期清理。

標註定義見 `market_analysis/outcome_labeling.py` (與離線校準工具共用)。
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

import pandas as pd

from market_analysis.outcome_labeling import (
    LABEL_VERSION,
    ForwardPathLabel,
    label_forward_path,
    labeling_ready_before,
    plan_outcome,
)

logger = logging.getLogger(__name__)

_INTRADAY_15M_MAX_AGE_DAYS = 55
_FETCH_SLEEP_SECONDS = 1.0


class LabelRunSummary(NamedTuple):
    pending: int
    labeled: int
    no_data: int
    symbols: int


def _parse_evaluated_at(value: Any) -> Optional[datetime]:
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    result: datetime = ts.to_pydatetime()
    return result


def _touch_for(
    label: ForwardPathLabel, k: float
) -> tuple[Optional[int], Optional[int]]:
    for t in label.touches:
        if abs(t.k - k) < 1e-9:
            return t.touch, t.bars_to_touch
    return None, None


def build_outcome_row(
    row: dict[str, Any],
    bars: Optional[pd.DataFrame],
    daily: Optional[pd.DataFrame],
) -> dict[str, Any]:
    """純函式：由一筆評估紀錄與 K 線算出 outcome 列 (資料不足時 label_status=NO_DATA)。"""
    no_data: dict[str, Any] = {
        "evaluation_id": row["id"],
        "label_status": "NO_DATA",
        "label_version": LABEL_VERSION,
    }
    entry_ts = _parse_evaluated_at(row.get("evaluated_at"))
    entry_price = float(row.get("spot") or 0.0)
    if entry_ts is None or entry_price <= 0 or bars is None or bars.empty:
        return no_data

    atr_1d = float(row.get("atr_1d") or 0.0)
    if atr_1d <= 0 and daily is not None and not daily.empty:
        from market_analysis.atr_utils import compute_atr_14_from_daily_df

        idx = pd.DatetimeIndex(daily.index)
        cutoff = pd.Timestamp(entry_ts)
        if idx.tz is None:
            cutoff = cutoff.tz_localize(None)
        else:
            cutoff = cutoff.tz_convert(idx.tz)
        atr_1d = compute_atr_14_from_daily_df(daily[idx <= cutoff])
    if atr_1d <= 0:
        return no_data

    label = label_forward_path(bars, entry_ts, entry_price, atr_1d)
    if label is None:
        return no_data

    k1, _ = _touch_for(label, 1.0)
    k15, k15_bars = _touch_for(label, 1.5)
    k2, _ = _touch_for(label, 2.0)
    return {
        "evaluation_id": row["id"],
        "label_status": "LABELED",
        "label_version": LABEL_VERSION,
        "entry_ref_price": label.entry_ref_price,
        "atr_1d_ref": label.atr_1d_ref,
        "fwd_ret_1h": label.fwd_ret_1h,
        "fwd_ret_eod": label.fwd_ret_eod,
        "fwd_ret_1d": label.fwd_ret_1d,
        "fwd_ret_3d": label.fwd_ret_3d,
        "fwd_ret_5d": label.fwd_ret_5d,
        "max_up_atr_5d": label.max_up_atr_5d,
        "max_down_atr_5d": label.max_down_atr_5d,
        "first_touch_k1": k1,
        "first_touch_k15": k15,
        "first_touch_k2": k2,
        "touch_bars_k15": k15_bars,
        "plan_outcome": plan_outcome(
            bars,
            entry_ts,
            str(row.get("direction") or "LONG"),
            row.get("stop_price"),
            row.get("target_price"),
        ),
    }


def _build_outcome_rows(
    rows: list[dict[str, Any]],
    bars: Optional[pd.DataFrame],
    daily: Optional[pd.DataFrame],
) -> list[dict[str, Any]]:
    return [build_outcome_row(r, bars, daily) for r in rows]


async def run_outcome_labeling(
    max_rows: int = 300,
    max_symbols: int = 40,
    now_utc: Optional[datetime] = None,
    retention_days: int = 365,
) -> LabelRunSummary:
    from database.regime_evaluation_log import (
        fetch_pending_evaluations,
        purge_regime_evaluation_log,
        save_evaluation_outcomes,
    )
    from services import market_data_service

    now = now_utc or datetime.now(timezone.utc)
    cutoff = labeling_ready_before(now).strftime("%Y-%m-%d %H:%M:%S")
    pending = await asyncio.to_thread(fetch_pending_evaluations, cutoff, max_rows)

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pending:
        by_symbol[str(row.get("symbol") or "").upper()].append(row)
    symbols = [s for s in by_symbol if s][:max_symbols]

    outcomes: list[dict[str, Any]] = []
    for i, symbol in enumerate(symbols):
        rows = by_symbol[symbol]
        if i > 0:
            await asyncio.sleep(_FETCH_SLEEP_SECONDS)
        oldest = min(
            (_parse_evaluated_at(r.get("evaluated_at")) for r in rows),
            key=lambda d: d or now,
        )
        use_hourly = oldest is not None and oldest < now - timedelta(
            days=_INTRADAY_15M_MAX_AGE_DAYS
        )
        bars: Optional[pd.DataFrame] = None
        daily: Optional[pd.DataFrame] = None
        try:
            if use_hourly:
                bars = await market_data_service.get_history_df(
                    symbol, period="729d", interval="1h"
                )
            else:
                bars = await market_data_service.get_history_df(
                    symbol, period="60d", interval="15m"
                )
            if any(float(r.get("atr_1d") or 0.0) <= 0 for r in rows):
                daily = await market_data_service.get_history_df(
                    symbol, period="6mo", interval="1d"
                )
        except Exception as e:
            logger.warning(f"[OutcomeLabeler] {symbol} K 線抓取失敗: {e}")

        symbol_outcomes = await asyncio.to_thread(
            _build_outcome_rows, rows, bars, daily
        )
        outcomes.extend(symbol_outcomes)

    if outcomes:
        await save_evaluation_outcomes(outcomes)
    try:
        await purge_regime_evaluation_log(retention_days=retention_days)
    except Exception as e:
        logger.error(f"[OutcomeLabeler] 保留期清理失敗: {e}")

    labeled = sum(1 for o in outcomes if o["label_status"] == "LABELED")
    summary = LabelRunSummary(
        pending=len(pending),
        labeled=labeled,
        no_data=len(outcomes) - labeled,
        symbols=len(symbols),
    )
    logger.info(f"[OutcomeLabeler] 完成: {summary}")
    return summary
