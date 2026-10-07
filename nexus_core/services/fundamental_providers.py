"""基本面市場共識與耳語預期數據提供者 (Fundamental Consensus & Whisper Providers)。

遵循架構規範：
1. 100% 免費數據源：以 Finnhub 與公開免費用戶端為主。
2. 付費機構資料（FactSet, Bloomberg, Estimize, Zacks）一律實作 NullProvider，無網路請求，零維護成本。
3. 抽象協定 (Protocol) 解耦，支援未來隨插即用。
"""

from __future__ import annotations

import calendar
import time
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from market_analysis.fundamental_pipeline.fiscal_period import (
    format_fiscal_quarter,
    normalize_fiscal_period,
)
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
        self,
        symbol: str,
        fiscal_period: str | None = None,
        as_of: date | None = None,
    ) -> ConsensusData | None:
        """獲取特定標的與季度之分析師預估共識與實際數值。

        - fiscal_period（`YYYY-Qn`）：指定財季。
        - as_of：財報發布日（SEC 受理日，美東），用以對齊財報日曆中最近的條目。
        - 皆未指定：取最近一筆已公布實際 EPS 之財季。
        回傳之 fiscal_period 一律為 `YYYY-Qn`（財年 + 財季）。
        """
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
        self,
        symbol: str,
        fiscal_period: str | None = None,
        as_of: date | None = None,
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


CALENDAR_ALIGN_WINDOW_DAYS: int = 5  # 財報日曆條目與 SEC 受理日之最大對齊誤差（日）
REPORT_LAG_MAX_DAYS: int = (
    100  # 財季結束至財報發布之最長間隔（company_earnings 對齊用）
)
ESTIMATES_FORBIDDEN_COOLDOWN_SECONDS: int = (
    24 * 3600
)  # eps-estimate 403 後改走日曆備援的冷卻秒數
SNAPSHOT_CALENDAR_LOOKAHEAD_DAYS: int = 400  # 日曆備援快照之前瞻查詢範圍


def _parse_iso_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _entry_fiscal_period(entry: dict[str, Any]) -> str | None:
    """由 Finnhub 條目的 year / quarter（財年 / 財季）組出 `YYYY-Qn`；缺漏時回傳 None。

    不以發布日或期末日推算日曆季：非曆年制財年（如 AAPL）會因此錯置季度。
    """
    yr = _safe_int(entry.get("year"))
    qtr = _safe_int(entry.get("quarter"))
    if yr is None or qtr is None:
        return None
    return format_fiscal_quarter(yr, qtr)


def _add_months_end(base: date, months: int) -> date:
    """將月末日期往後推移 months 個月並對齊該月月末。"""
    idx = base.year * 12 + (base.month - 1) + months
    yr, mo = divmod(idx, 12)
    mo += 1
    return date(yr, mo, calendar.monthrange(yr, mo)[1])


def _next_fiscal_quarter(year: int, quarter: int, steps: int = 1) -> tuple[int, int]:
    idx = year * 4 + (quarter - 1) + steps
    return idx // 4, idx % 4 + 1


class FinnhubConsensusProvider:
    """基於 Finnhub 公開 API 之共識資料提供者。

    Finnhub 免費方案實測（2026-10）：`company_eps_estimates` 回 403；個股 `earnings_calendar`
    只回傳今日起之條目；`company_earnings` 可取最近 4 季 EPS 實際 / 預估（無營收）。
    year / quarter 欄位皆為「財年 / 財季」（AAPL 2025-12 季 = 2026-Q1）。
    """

    # eps-estimate 回 403（免費方案無權限）後的冷卻截止時間（monotonic 秒，類別層級共用）
    _estimates_forbidden_until: float = 0.0

    def __init__(self) -> None:
        pass

    async def get_consensus(
        self,
        symbol: str,
        fiscal_period: str | None = None,
        as_of: date | None = None,
    ) -> ConsensusData | None:
        """獲取標的之財報共識與實際業績（期別對齊規則見 ConsensusProvider）。"""
        from services.market_data_service._core import _execute_api_call, _get_client
        from services.market_data_service.fundamentals import get_earnings_calendar

        sym_upper = symbol.strip().upper()
        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        today = now_et.date()
        today_str = today.isoformat()
        target_period = (
            normalize_fiscal_period(fiscal_period)
            if fiscal_period is not None
            else None
        )
        if fiscal_period is not None and target_period is None:
            logger.warning(
                f"[FinnhubConsensusProvider] 無法辨識之財季格式 ({sym_upper}): {fiscal_period!r}"
            )
            return None
        ref_day = as_of if as_of is not None else today

        try:
            cal_entries = await get_earnings_calendar(
                sym_upper,
                from_date=(ref_day - timedelta(days=400)).isoformat(),
                to_date=(ref_day + timedelta(days=30)).isoformat(),
            )
            parsed: list[tuple[str, date, dict[str, Any]]] = []
            for entry in cal_entries:
                p_str = _entry_fiscal_period(entry)
                d = _parse_iso_date(entry.get("date"))
                if p_str is None or d is None:
                    continue
                parsed.append((p_str, d, entry))

            chosen: tuple[str, date, dict[str, Any]] | None = None
            if target_period is not None:
                chosen = next((c for c in parsed if c[0] == target_period), None)
            elif as_of is not None:
                near = [
                    c
                    for c in parsed
                    if abs((c[1] - as_of).days) <= CALENDAR_ALIGN_WINDOW_DAYS
                ]
                if near:
                    # 最接近受理日者；同距離時偏好發布日不晚於受理日之條目
                    chosen = min(
                        near, key=lambda c: (abs((c[1] - as_of).days), c[1] > as_of)
                    )
            else:
                reported = [
                    c
                    for c in parsed
                    if c[1] <= today and _safe_float(c[2].get("epsActual")) is not None
                ]
                if reported:
                    chosen = max(reported, key=lambda c: c[1])

            if chosen is not None:
                p_str, _, entry = chosen
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
                if any(v is not None for v in (act_eps, est_eps, act_rev, est_rev)):
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

        # 備援：company_earnings（最近 4 季 EPS，period 為財季期末日）
        try:
            client = _get_client()
            resp = await _execute_api_call(
                client.company_earnings, symbol=sym_upper, limit=4
            )
            if resp and isinstance(resp, list):
                items: list[tuple[str, date | None, dict[str, Any]]] = []
                for item in resp:
                    if not isinstance(item, dict):
                        continue
                    p_str = _entry_fiscal_period(item)
                    if p_str is None:
                        continue
                    items.append((p_str, _parse_iso_date(item.get("period")), item))
                # 依財季由新到舊
                items.sort(key=lambda c: c[0], reverse=True)

                for p_str, period_end, item in items:
                    act_eps = _safe_float(item.get("actual"))
                    est_eps = _safe_float(item.get("estimate"))
                    if act_eps is None and est_eps is None:
                        continue
                    if target_period is not None:
                        if p_str != target_period:
                            continue
                    elif as_of is not None:
                        if period_end is None:
                            continue
                        lag = (as_of - period_end).days
                        if not (0 < lag <= REPORT_LAG_MAX_DAYS):
                            continue
                    elif act_eps is None:
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

    @staticmethod
    def _horizon_rows(
        sym_upper: str,
        today: date,
        data_list: list[dict[str, Any]],
        labels: tuple[EstimateHorizon, EstimateHorizon],
    ) -> list[EPSEstimateSnapshotRecord]:
        """自 company_eps_estimates 清單取出「期末日 >= 今天」之前兩期（尚未結束之當期與次期）。"""
        dated: list[tuple[date, dict[str, Any]]] = []
        for item in data_list:
            period_end = _parse_iso_date(item.get("period"))
            if period_end is not None and period_end >= today:
                dated.append((period_end, item))
        dated.sort(key=lambda c: c[0])

        rows: list[EPSEstimateSnapshotRecord] = []
        is_annual = labels[0] == "0y"
        for label, (period_end, item) in zip(labels, dated[:2]):
            mean_val = _safe_float(item.get("epsAvg"))
            if mean_val is None:
                continue
            if is_annual:
                fy = _safe_int(item.get("year"))
                fiscal_period: str | None = (
                    f"{fy}-FY" if fy is not None else period_end.isoformat()
                )
            else:
                fiscal_period = _entry_fiscal_period(item) or period_end.isoformat()
            rows.append(
                EPSEstimateSnapshotRecord(
                    symbol=sym_upper,
                    snapshot_date=today.isoformat(),
                    horizon=label,
                    source="finnhub",
                    eps_mean=mean_val,
                    eps_high=_safe_float(item.get("epsHigh")),
                    eps_low=_safe_float(item.get("epsLow")),
                    analyst_count=_safe_int(item.get("numberAnalysts")),
                    fiscal_period=fiscal_period,
                )
            )
        return rows

    async def get_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        """抓取分析師各期 EPS 預估快照。

        期限語意（horizon）：以「財期期末日 >= 今天（美東）」為起點——
        - 0q：尚未結束之當前財季（期末日 >= 今天的第一個財季）；+1q：其下一個財季。
        - 0y：尚未結束之當前財年；+1y：其下一個財年。
        已結束但尚未公布財報的財季不屬於任何 horizon。
        """
        from services.market_data_service._core import _execute_api_call, _get_client

        sym_upper = symbol.strip().upper()
        today = datetime.now(timezone.utc).astimezone(_ET_ZONE).date()
        snapshots: list[EPSEstimateSnapshotRecord] = []

        if time.monotonic() < FinnhubConsensusProvider._estimates_forbidden_until:
            # 免費方案 eps-estimate 回 403：冷卻期內不再打（每次 403 仍會消耗限流配額）
            snapshots.extend(await self._calendar_quarter_snapshots(sym_upper, today))
            return snapshots

        try:
            client = _get_client()
            q_data = await _execute_api_call(
                client.company_eps_estimates, symbol=sym_upper, freq="quarterly"
            )
            q_list = q_data.get("data", []) if isinstance(q_data, dict) else []
            snapshots.extend(
                self._horizon_rows(sym_upper, today, q_list, ("0q", "+1q"))
            )

            a_data = await _execute_api_call(
                client.company_eps_estimates, symbol=sym_upper, freq="annual"
            )
            a_list = a_data.get("data", []) if isinstance(a_data, dict) else []
            snapshots.extend(
                self._horizon_rows(sym_upper, today, a_list, ("0y", "+1y"))
            )
        except Exception as e:
            msg = str(e).lower()
            if "403" in msg or "access" in msg:
                FinnhubConsensusProvider._estimates_forbidden_until = (
                    time.monotonic() + ESTIMATES_FORBIDDEN_COOLDOWN_SECONDS
                )
                logger.info(
                    "[FinnhubConsensusProvider] company_eps_estimates 無存取權限（免費方案 403），"
                    f"{ESTIMATES_FORBIDDEN_COOLDOWN_SECONDS // 3600} 小時內改走財報日曆備援"
                )
            else:
                logger.debug(
                    f"[FinnhubConsensusProvider] company_eps_estimates 失敗 ({sym_upper}): {e}"
                )

        if not any(s.horizon in ("0q", "+1q") for s in snapshots):
            snapshots.extend(await self._calendar_quarter_snapshots(sym_upper, today))
        return snapshots

    async def _calendar_quarter_snapshots(
        self, sym_upper: str, today: date
    ) -> list[EPSEstimateSnapshotRecord]:
        """備援：以 company_earnings 最近已公布財季之期末日推算當前財季，再對齊財報日曆之預估。

        步驟：取最近一筆已公布財季 (Y, Q, 期末日 P)，逐季將 P 推移 3 個月，第一個推算期末日
        >= 今天的財季即為 0q，下一季為 +1q；再以財年 / 財季比對日曆條目之 epsEstimate。
        無法推算（無已公布財季）時不寫入，避免把「已結束待公布」的財季標成 0q。
        """
        from services.market_data_service._core import _execute_api_call, _get_client
        from services.market_data_service.fundamentals import get_earnings_calendar

        rows: list[EPSEstimateSnapshotRecord] = []
        try:
            client = _get_client()
            resp = await _execute_api_call(
                client.company_earnings, symbol=sym_upper, limit=4
            )
            anchors: list[tuple[int, int, date]] = []
            if resp and isinstance(resp, list):
                for item in resp:
                    if not isinstance(item, dict):
                        continue
                    yr = _safe_int(item.get("year"))
                    qtr = _safe_int(item.get("quarter"))
                    period_end = _parse_iso_date(item.get("period"))
                    if (
                        yr is not None
                        and qtr is not None
                        and 1 <= qtr <= 4
                        and period_end is not None
                        and _safe_float(item.get("actual")) is not None
                    ):
                        anchors.append((yr, qtr, period_end))
            if not anchors:
                return rows
            yr, qtr, period_end = max(anchors, key=lambda a: a[2])

            steps = 1
            while _add_months_end(period_end, 3 * steps) < today and steps <= 8:
                steps += 1
            current_q = _next_fiscal_quarter(yr, qtr, steps)
            next_q = _next_fiscal_quarter(yr, qtr, steps + 1)
            wanted: dict[str, EstimateHorizon] = {
                f"{current_q[0]}-Q{current_q[1]}": "0q",
                f"{next_q[0]}-Q{next_q[1]}": "+1q",
            }

            cal_entries = await get_earnings_calendar(
                sym_upper,
                from_date=today.isoformat(),
                to_date=(
                    today + timedelta(days=SNAPSHOT_CALENDAR_LOOKAHEAD_DAYS)
                ).isoformat(),
            )
            for entry in cal_entries:
                p_str = _entry_fiscal_period(entry)
                horizon = wanted.get(p_str or "")
                est = _safe_float(entry.get("epsEstimate"))
                if horizon is None or est is None:
                    continue
                if any(r.horizon == horizon for r in rows):
                    continue
                rows.append(
                    EPSEstimateSnapshotRecord(
                        symbol=sym_upper,
                        snapshot_date=today.isoformat(),
                        horizon=horizon,
                        source="finnhub_calendar",
                        eps_mean=est,
                        eps_high=None,
                        eps_low=None,
                        analyst_count=None,
                        fiscal_period=p_str,
                    )
                )
        except Exception as e:
            logger.debug(
                f"[FinnhubConsensusProvider] calendar 備援預估讀取失敗 ({sym_upper}): {e}"
            )
        rows.sort(key=lambda r: 0 if r.horizon == "0q" else 1)
        return rows
