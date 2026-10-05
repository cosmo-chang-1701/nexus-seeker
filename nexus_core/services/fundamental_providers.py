"""基本面市場共識與耳語預期數據提供者 (Fundamental Consensus & Whisper Providers)。

遵循架構規範：
1. 100% 免費數據源：以 Finnhub 與公開免費用戶端為主。
2. 付費機構資料（FactSet, Bloomberg, Estimize, Zacks）一律實作 NullProvider，無網路請求，零維護成本。
3. 抽象協定 (Protocol) 解耦，支援未來隨插即用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from market_analysis.fundamental_pipeline.models import (
    EPSEstimateSnapshotRecord,
    EstimateHorizon,
)

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ConsensusData:
    """市場分析師共識與業績數據。"""

    symbol: str
    fiscal_period: str
    actual_eps: float | None = None
    consensus_eps: float | None = None
    actual_revenue: float | None = None
    consensus_revenue: float | None = None
    session: str = "UNKNOWN"
    source: str = "finnhub"
    snapshot_date: str = ""


class ConsensusProvider(Protocol):
    """分析師業績預估共識提供者抽象介面。"""

    async def get_consensus(
        self, symbol: str, fiscal_period: str | None = None
    ) -> ConsensusData | None:
        """獲取特定標的與季度之分析師預估共識與實際數值。"""
        ...

    async def get_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        """獲取分析師 EPS 預估快照 (0q, +1q, 0y, +1y)。"""
        ...


class WhisperProvider(Protocol):
    """買方耳語預期 (Whisper EPS) 提供者抽象介面。"""

    async def get_whisper(
        self, symbol: str, fiscal_period: str | None = None
    ) -> float | None:
        """獲取買方非官方耳語每股盈餘預期。"""
        ...


# ============================================================================
# 空實作 (Null Providers for Paid / Mock Fallbacks)
# ============================================================================


class NullConsensusProvider:
    """付費機構資料庫之空實作 (FactSet, Bloomberg, Zacks 等，預設停用)。"""

    async def get_consensus(
        self, symbol: str, fiscal_period: str | None = None
    ) -> ConsensusData | None:
        return None

    async def get_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        empty_list: list[EPSEstimateSnapshotRecord] = []
        return empty_list


class NullWhisperProvider:
    """付費耳語數據之空實作 (EarningsWhispers, Estimize 等，預設停用)。"""

    async def get_whisper(
        self, symbol: str, fiscal_period: str | None = None
    ) -> float | None:
        return None


# ============================================================================
# Finnhub 免費公開共識提供者 (Finnhub Consensus Provider)
# ============================================================================


class FinnhubConsensusProvider:
    """基於 Finnhub 公開 API 之共識資料提供者。"""

    def __init__(self) -> None:
        pass

    async def get_consensus(
        self, symbol: str, fiscal_period: str | None = None
    ) -> ConsensusData | None:
        """獲取標的之財報共識與實際業績。"""
        from services.market_data_service._core import _execute_api_call, _get_client
        from services.market_data_service.fundamentals import get_earnings_calendar

        sym_upper = symbol.strip().upper()
        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        today_str = now_et.strftime("%Y-%m-%d")

        try:
            # 優先嘗試 Finnhub earnings calendar
            cal_entries = await get_earnings_calendar(
                sym_upper,
                from_date=(now_et - timedelta(days=365)).strftime("%Y-%m-%d"),
                to_date=(now_et + timedelta(days=365)).strftime("%Y-%m-%d"),
            )
            for entry in cal_entries:
                yr = entry.get("year")
                qtr = entry.get("quarter")
                if yr and qtr:
                    p_str = f"{yr}-Q{qtr}"
                else:
                    date_val = str(entry.get("date") or "")
                    try:
                        d = date.fromisoformat(date_val)
                        p_str = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
                    except Exception:
                        p_str = date_val

                if fiscal_period is not None and p_str != fiscal_period:
                    continue

                act_eps = _safe_float(entry.get("epsActual"))
                est_eps = _safe_float(entry.get("epsEstimate"))
                act_rev = _safe_float(entry.get("revenueActual"))
                est_rev = _safe_float(entry.get("revenueEstimate"))
                hour_raw = str(entry.get("hour") or "").strip().lower()
                session_val = (
                    "BMO"
                    if hour_raw == "bmo"
                    else ("AMC" if hour_raw == "amc" else "UNKNOWN")
                )

                if (
                    act_eps is not None
                    or est_eps is not None
                    or act_rev is not None
                    or est_rev is not None
                ):
                    return ConsensusData(
                        symbol=sym_upper,
                        fiscal_period=p_str,
                        actual_eps=act_eps,
                        consensus_eps=est_eps,
                        actual_revenue=act_rev,
                        consensus_revenue=est_rev,
                        session=session_val,
                        source="finnhub",
                        snapshot_date=today_str,
                    )
        except Exception as e:
            logger.debug(
                f"[FinnhubConsensusProvider] earnings_calendar 讀取失敗 ({sym_upper}): {e}"
            )

        # 備援嘗試 company_earnings
        try:
            client = _get_client()
            resp = await _execute_api_call(
                client.company_earnings, symbol=sym_upper, limit=4
            )
            if resp and isinstance(resp, list):
                for item in resp:
                    yr = item.get("year")
                    qtr = item.get("quarter")
                    p_str = (
                        f"{yr}-Q{qtr}" if yr and qtr else str(item.get("period", ""))
                    )
                    if fiscal_period is not None and p_str != fiscal_period:
                        continue

                    act_eps = _safe_float(item.get("actual"))
                    est_eps = _safe_float(item.get("estimate"))
                    if act_eps is None and est_eps is None:
                        continue

                    return ConsensusData(
                        symbol=sym_upper,
                        fiscal_period=p_str,
                        actual_eps=act_eps,
                        consensus_eps=est_eps,
                        actual_revenue=None,
                        consensus_revenue=None,
                        session="UNKNOWN",
                        source="finnhub",
                        snapshot_date=today_str,
                    )
        except Exception as e:
            logger.debug(
                f"[FinnhubConsensusProvider] company_earnings 讀取失敗 ({sym_upper}): {e}"
            )

        return None

    async def get_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        """抓取分析師各期 EPS 預估中位數快照 (0q, +1q, 0y, +1y)。"""
        from services.market_data_service._core import _execute_api_call, _get_client

        sym_upper = symbol.strip().upper()
        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        today_str = now_et.strftime("%Y-%m-%d")
        snapshots: list[EPSEstimateSnapshotRecord] = []

        try:
            client = _get_client()
            # 嘗試拉取季度預估
            q_data = await _execute_api_call(
                client.company_eps_estimates, symbol=sym_upper, freq="quarterly"
            )
            data_list = q_data.get("data", []) if isinstance(q_data, dict) else []
            if data_list:
                # 排序日期並優先過濾掉過於久遠的歷史期別 (90 天前以前)
                sorted_q = sorted(data_list, key=lambda x: str(x.get("period", "")))
                cutoff_date = (now_et - timedelta(days=90)).strftime("%Y-%m-%d")
                future_or_recent_q = [
                    item
                    for item in sorted_q
                    if str(item.get("period", "")) >= cutoff_date
                ]
                active_q = future_or_recent_q if future_or_recent_q else sorted_q

                # 0q: 當季預估, +1q: 次季預估
                for idx, h in enumerate([("0q", 0), ("+1q", 1)]):
                    h_code = cast(EstimateHorizon, h[0])
                    item_idx = h[1]
                    if item_idx < len(active_q):
                        item = active_q[item_idx]
                        mean_val = _safe_float(item.get("epsAvg"))
                        if mean_val is not None:
                            snapshots.append(
                                EPSEstimateSnapshotRecord(
                                    symbol=sym_upper,
                                    snapshot_date=today_str,
                                    horizon=h_code,
                                    source="finnhub",
                                    eps_mean=mean_val,
                                    eps_high=_safe_float(item.get("epsHigh")),
                                    eps_low=_safe_float(item.get("epsLow")),
                                    analyst_count=_safe_int(item.get("numberAnalysts")),
                                )
                            )

            # 嘗試拉取年度預估
            a_data = await _execute_api_call(
                client.company_eps_estimates, symbol=sym_upper, freq="annual"
            )
            a_list = a_data.get("data", []) if isinstance(a_data, dict) else []
            if a_list:
                sorted_a = sorted(a_list, key=lambda x: str(x.get("period", "")))
                cutoff_a = (now_et - timedelta(days=365)).strftime("%Y-%m-%d")
                future_or_recent_a = [
                    item for item in sorted_a if str(item.get("period", "")) >= cutoff_a
                ]
                active_a = future_or_recent_a if future_or_recent_a else sorted_a

                for idx, h in enumerate([("0y", 0), ("+1y", 1)]):
                    h_code = cast(EstimateHorizon, h[0])
                    item_idx = h[1]
                    if item_idx < len(active_a):
                        item = active_a[item_idx]
                        mean_val = _safe_float(item.get("epsAvg"))
                        if mean_val is not None:
                            snapshots.append(
                                EPSEstimateSnapshotRecord(
                                    symbol=sym_upper,
                                    snapshot_date=today_str,
                                    horizon=h_code,
                                    source="finnhub",
                                    eps_mean=mean_val,
                                    eps_high=_safe_float(item.get("epsHigh")),
                                    eps_low=_safe_float(item.get("epsLow")),
                                    analyst_count=_safe_int(item.get("numberAnalysts")),
                                )
                            )
        except Exception as e:
            logger.debug(
                f"[FinnhubConsensusProvider] company_eps_estimates 失敗 ({sym_upper}): {e}"
            )

        # 若 API 未回傳或無權限，嘗試由 earnings_calendar 備援提取 0q
        if not snapshots:
            try:
                from services.market_data_service.fundamentals import (
                    get_earnings_calendar,
                )

                cal_entries = await get_earnings_calendar(sym_upper)
                for entry in cal_entries:
                    est = _safe_float(entry.get("epsEstimate"))
                    if est is not None:
                        snapshots.append(
                            EPSEstimateSnapshotRecord(
                                symbol=sym_upper,
                                snapshot_date=today_str,
                                horizon="0q",
                                source="finnhub_calendar",
                                eps_mean=est,
                                eps_high=None,
                                eps_low=None,
                                analyst_count=None,
                            )
                        )
                        break
            except Exception as e:
                logger.debug(
                    f"[FinnhubConsensusProvider] calendar 備援預估讀取失敗: {e}"
                )

        return snapshots
