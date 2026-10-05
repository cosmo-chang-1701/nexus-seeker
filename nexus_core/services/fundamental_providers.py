"""基本面市場共識與耳語預期數據提供者 (Fundamental Consensus & Whisper Providers)。

遵循架構規範：
1. 100% 免費數據源：以 Finnhub 與公開免費用戶端為主。
2. 付費機構資料（FactSet, Bloomberg, Estimize, Zacks）一律實作 NullProvider，無網路請求，零維護成本。
3. 抽象協定 (Protocol) 解耦，支援未來隨插即用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from market_analysis.fundamental_pipeline.models import (
    EPSEstimateSnapshotRecord,
    EstimateHorizon,
)

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")


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
                from_date=(now_et.replace(year=now_et.year - 1)).strftime("%Y-%m-%d"),
                to_date=(now_et.replace(year=now_et.year + 1)).strftime("%Y-%m-%d"),
            )
            for entry in cal_entries:
                yr = entry.get("year")
                qtr = entry.get("quarter")
                p_str = f"{yr}-Q{qtr}" if yr and qtr else str(entry.get("date", ""))
                if fiscal_period is not None and p_str != fiscal_period:
                    continue

                act_eps = entry.get("epsActual")
                est_eps = entry.get("epsEstimate")
                act_rev = entry.get("revenueActual")
                est_rev = entry.get("revenueEstimate")
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
                        actual_eps=float(act_eps) if act_eps is not None else None,
                        consensus_eps=float(est_eps) if est_eps is not None else None,
                        actual_revenue=float(act_rev) if act_rev is not None else None,
                        consensus_revenue=float(est_rev)
                        if est_rev is not None
                        else None,
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

                    act_eps = item.get("actual")
                    est_eps = item.get("estimate")
                    return ConsensusData(
                        symbol=sym_upper,
                        fiscal_period=p_str,
                        actual_eps=float(act_eps) if act_eps is not None else None,
                        consensus_eps=float(est_eps) if est_eps is not None else None,
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
                # 排序日期
                sorted_q = sorted(data_list, key=lambda x: str(x.get("period", "")))
                # 0q: 當季預估, +1q: 次季預估
                for idx, h in enumerate([("0q", 0), ("+1q", 1)]):
                    h_code = cast(EstimateHorizon, h[0])
                    item_idx = h[1]
                    if item_idx < len(sorted_q):
                        item = sorted_q[item_idx]
                        mean_val = item.get("epsAvg")
                        if mean_val is not None:
                            snapshots.append(
                                EPSEstimateSnapshotRecord(
                                    symbol=sym_upper,
                                    snapshot_date=today_str,
                                    horizon=h_code,
                                    source="finnhub",
                                    eps_mean=float(mean_val),
                                    eps_high=float(item["epsHigh"])
                                    if item.get("epsHigh") is not None
                                    else None,
                                    eps_low=float(item["epsLow"])
                                    if item.get("epsLow") is not None
                                    else None,
                                    analyst_count=int(item["numberAnalysts"])
                                    if item.get("numberAnalysts") is not None
                                    else None,
                                )
                            )

            # 嘗試拉取年度預估
            a_data = await _execute_api_call(
                client.company_eps_estimates, symbol=sym_upper, freq="annual"
            )
            a_list = a_data.get("data", []) if isinstance(a_data, dict) else []
            if a_list:
                sorted_a = sorted(a_list, key=lambda x: str(x.get("period", "")))
                for idx, h in enumerate([("0y", 0), ("+1y", 1)]):
                    h_code = cast(EstimateHorizon, h[0])
                    item_idx = h[1]
                    if item_idx < len(sorted_a):
                        item = sorted_a[item_idx]
                        mean_val = item.get("epsAvg")
                        if mean_val is not None:
                            snapshots.append(
                                EPSEstimateSnapshotRecord(
                                    symbol=sym_upper,
                                    snapshot_date=today_str,
                                    horizon=h_code,
                                    source="finnhub",
                                    eps_mean=float(mean_val),
                                    eps_high=float(item["epsHigh"])
                                    if item.get("epsHigh") is not None
                                    else None,
                                    eps_low=float(item["epsLow"])
                                    if item.get("epsLow") is not None
                                    else None,
                                    analyst_count=int(item["numberAnalysts"])
                                    if item.get("numberAnalysts") is not None
                                    else None,
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
                    est = entry.get("epsEstimate")
                    if est is not None:
                        snapshots.append(
                            EPSEstimateSnapshotRecord(
                                symbol=sym_upper,
                                snapshot_date=today_str,
                                horizon="0q",
                                source="finnhub_calendar",
                                eps_mean=float(est),
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
