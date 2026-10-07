"""內在價值估值與分析師修正動能協調服務 (Valuation & Revision Service)。

職責：
1. 注入流動性體制中樞 (NFCI, US10Y, ERP)。
2. 整合 Finnhub `/stock/metric`（P/FCF、前瞻本益比、Beta、成長率）、同業倍數與報價。
3. 驅動兩段式 DCF、流動性折讓同業乘數法與安全邊際 (MOS) 計算。
4. 每日刷新分析師 EPS 共識快照（`refresh_estimate_snapshots`，由 20:00 排程先於估值呼叫），
   以財期配對 t 與 t-30d 快照計算修正動能，並以 NYSE 交易日判定 PEAD 共振。
5. 遵循 Single-Writer Invariant，將公允價值與動能分數寫入持久層；無效值存 NULL，不寫 0.0 哨兵。

Finnhub `/stock/metric?metric=all` 實測（2026-10，免費方案，MSFT／AAPL／NVDA 皆 133 個鍵）：
不存在 `fcfPerShareTTM` / `freeCashFlowPerShareTTM`；`cashFlowPerShareTTM` 不是自由現金流口徑。
每股 FCF 改以 `現價 / pfcfShareTTM` 推得，前瞻 EPS 以 `現價 / forwardPE` 推得。
ETF（如 SPY）只回 19 個鍵，兩者皆缺，估值為 NONE。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import market_time
from database.fundamental_pipeline import (
    get_active_governance_flags,
    get_eps_estimate_snapshots,
    get_latest_liquidity_regime,
    get_latest_processed_earnings_surprise,
    get_prior_eps_estimate_snapshots,
    save_fair_value,
    save_revision_score,
)
from market_analysis.fundamental_pipeline.fair_value import (
    DEFAULT_GROWTH_EST,
    DEFAULT_US10Y_PCT,
    FLAG_BETA_DEFAULT,
    FLAG_FCF_UNAVAILABLE,
    FLAG_FORWARD_EPS_TRAILING,
    FLAG_G_EST_DEFAULT,
    FLAG_G_EST_HISTORICAL,
    FLAG_NFCI_MISSING,
    FLAG_PEER_PE_TRAILING,
    FLAG_US10Y_DEFAULT,
    calculate_comps_value,
    calculate_cost_of_equity,
    calculate_dcf_value,
    integrate_fair_value,
)
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.models import (
    CompsInputs,
    DCFInputs,
    DCFResult,
    EPSEstimateSnapshotRecord,
    FairValueRecord,
    FairValueResult,
    RevisionMomentumResult,
    RevisionScoreRecord,
)
from market_analysis.fundamental_pipeline.revision_momentum import (
    evaluate_revision_momentum,
    prior_window,
)
from services.fundamental_providers import ConsensusProvider

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")

MAX_PEERS: int = 10  # 同業本益比抽樣上限
# 歷史成長率備援順序（Finnhub 以百分比表示）
_HISTORICAL_GROWTH_KEYS: tuple[str, ...] = (
    "epsGrowthTTMYoy",
    "epsGrowth3Y",
    "epsGrowth5Y",
    "revenueGrowthTTMYoy",
)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def _first_metric(metrics: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """依序取第一個非 None 的數值（`is not None` 判斷，不吞掉 0）。"""
    for key in keys:
        val = _safe_float(metrics.get(key))
        if val is not None:
            return val
    return None


def derive_fcf_per_share(metrics: dict[str, Any], spot: float) -> float | None:
    """每股自由現金流 = 現價 / pfcfShareTTM（P/FCF 為負代表 FCF 為負，結果亦為負）。

    缺 P/FCF、P/FCF 為 0 或無現價時回傳 None。
    """
    pfcf = _safe_float(metrics.get("pfcfShareTTM"))
    if pfcf is None or pfcf == 0 or spot <= 0:
        return None
    return spot / pfcf


def derive_forward_eps(metrics: dict[str, Any], spot: float) -> float | None:
    """前瞻 EPS = 現價 / forwardPE（口徑由 Finnhub 定義的前瞻共識）；缺值或無現價時回傳 None。"""
    fpe = _safe_float(metrics.get("forwardPE"))
    if fpe is None or fpe == 0 or spot <= 0:
        return None
    return spot / fpe


def derive_growth_estimate(
    metrics: dict[str, Any], forward_eps: float | None
) -> tuple[float, str | None]:
    """g_est 推估，回傳 (g_est, flag)。

    1. 前瞻：forward EPS / 近四季 EPS - 1（兩者皆為正時）；flag 為 None。
    2. 歷史：EPS / 營收歷史成長率（百分比轉小數）；flag = G_EST_HISTORICAL。
    3. 預設：DEFAULT_GROWTH_EST；flag = G_EST_DEFAULT。
    """
    eps_ttm = _first_metric(metrics, ("epsTTM", "epsExclExtraItemsTTM"))
    if (
        forward_eps is not None
        and forward_eps > 0
        and eps_ttm is not None
        and eps_ttm > 0
    ):
        return forward_eps / eps_ttm - 1.0, None
    hist = _first_metric(metrics, _HISTORICAL_GROWTH_KEYS)
    if hist is not None:
        return hist / 100.0, FLAG_G_EST_HISTORICAL
    return DEFAULT_GROWTH_EST, FLAG_G_EST_DEFAULT


def peer_pe(metrics: dict[str, Any]) -> tuple[float | None, bool]:
    """同業本益比：優先 forwardPE，缺值時退回近四季本益比；回傳 (PE, 是否為 trailing)。"""
    fpe = _safe_float(metrics.get("forwardPE"))
    if fpe is not None and fpe > 0:
        return fpe, False
    trailing = _first_metric(metrics, ("peTTM", "peExclExtraTTM", "peNormalizedAnnual"))
    if trailing is not None and trailing > 0:
        return trailing, True
    return None, False


class ValuationDataProvider(Protocol):
    """基本面估值數據提供者抽象介面。"""

    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        """取得標的公司基本面財務指標字典。"""
        ...

    async def get_peers(self, symbol: str) -> list[str]:
        """取得標的同業代號清單。"""
        ...

    async def get_spot_price(self, symbol: str) -> float | None:
        """取得標的最新現貨市價。"""
        ...


PEERS_CACHE_TTL_SECONDS: float = (
    7 * 24 * 3600
)  # 同業清單變動緩慢，快取 7 天以節省限流配額


class FinnhubValuationDataProvider:
    """基於 Finnhub 免費公開 API 之估值數據提供者。

    指標經 `get_basic_financials` 走 SQLite 24 小時快取（同業之間共用）；同業清單於實例內
    快取 7 天（排程執行器跨日重用同一實例），降低 Finnhub 背景限流（12 次/分）壓力。
    """

    def __init__(self) -> None:
        self._peers_cache: dict[str, tuple[float, list[str]]] = {}

    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        from services.market_data_service.fundamentals import (
            get_basic_financials,
        )

        try:
            return await get_basic_financials(symbol)
        except Exception as e:
            logger.debug(
                f"[FinnhubValuationDataProvider] get_basic_financials 失敗 ({symbol}): {e}"
            )
            empty_dict: dict[str, Any] = {}
            return empty_dict

    async def get_peers(self, symbol: str) -> list[str]:
        from services.market_data_service._core import (
            _execute_api_call,
            _get_client,
        )

        sym_upper = symbol.strip().upper()
        cached = self._peers_cache.get(sym_upper)
        if (
            cached is not None
            and time.monotonic() - cached[0] < PEERS_CACHE_TTL_SECONDS
        ):
            return list(cached[1])
        try:
            client = _get_client()
            resp = await _execute_api_call(client.company_peers, symbol=sym_upper)
            if resp and isinstance(resp, list):
                res: list[str] = [
                    str(p).strip().upper()
                    for p in resp
                    if p and str(p).strip().upper() != sym_upper
                ]
                self._peers_cache[sym_upper] = (time.monotonic(), res)
                return list(res)
            empty_list: list[str] = []
            return empty_list
        except Exception as e:
            logger.debug(
                f"[FinnhubValuationDataProvider] company_peers 失敗 ({symbol}): {e}"
            )
            empty_list_err: list[str] = []
            return empty_list_err

    async def get_spot_price(self, symbol: str) -> float | None:
        from services.market_data_service.quote import get_quote

        try:
            q = await get_quote(symbol)
            if q and "c" in q and q["c"] is not None:
                val = _safe_float(q["c"])
                if val is not None and val > 0:
                    return val
            return None
        except Exception as e:
            logger.debug(
                f"[FinnhubValuationDataProvider] get_quote 失敗 ({symbol}): {e}"
            )
            return None


class ValuationService:
    """內在價值估值與分析師修正動能協調服務核心類別。"""

    def __init__(
        self,
        data_provider: ValuationDataProvider | None = None,
        consensus_provider: ConsensusProvider | None = None,
    ) -> None:
        self.data_provider: ValuationDataProvider = (
            data_provider
            if data_provider is not None
            else FinnhubValuationDataProvider()
        )
        self._consensus_provider = consensus_provider

    async def refresh_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        """抓取並寫入當日分析師 EPS 共識快照（沿用 PR3 `sync_symbol_estimates`）。

        快照日為美東當日；免費方案只有 0q / +1q（日曆備援），每列帶財期供修正動能配對。
        """
        from services.earnings_surprise_service import EarningsSurpriseService

        service = EarningsSurpriseService(consensus_provider=self._consensus_provider)
        return await service.sync_symbol_estimates(symbol)

    async def _days_since_announcement(
        self, announced_on: str | None, target_date: date
    ) -> int | None:
        """財報發布日（美東）之後至 target_date 的 NYSE 交易日數；日期不明時回傳 None。"""
        if not announced_on:
            return None
        try:
            announced = date.fromisoformat(announced_on[:10])
        except ValueError:
            return None
        if announced > target_date:
            return None
        try:
            return await asyncio.to_thread(
                market_time.count_nyse_sessions_after, announced, target_date
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[ValuationService] NYSE 行事曆查詢失敗，PEAD 改以平日數估算: {e}"
            )
            days = 0
            d = announced
            while d < target_date:
                d = date.fromordinal(d.toordinal() + 1)
                if d.weekday() < 5:
                    days += 1
            return days

    async def compute_and_save_valuation(
        self,
        symbol: str,
        as_of_date: date | None = None,
    ) -> tuple[FairValueResult, RevisionMomentumResult]:
        """完整執行指定標的之估值與動能計算並寫入資料庫（快照刷新由呼叫端先行）。"""
        sym_upper = symbol.strip().upper()
        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        target_date = as_of_date if as_of_date is not None else now_et.date()
        date_str = target_date.isoformat()
        input_flags: list[str] = []

        # 1. 注入宏觀流動性體制讀數
        liquidity = await asyncio.to_thread(get_latest_liquidity_regime)
        nfci = liquidity.nfci if liquidity is not None else None
        if nfci is None:
            input_flags.append(FLAG_NFCI_MISSING)
        us10y = liquidity.us10y if liquidity is not None else None
        if us10y is None:
            us10y = DEFAULT_US10Y_PCT
            input_flags.append(FLAG_US10Y_DEFAULT)

        # 2. 獲取基本面財務指標與現貨市價
        metrics, spot_price = await asyncio.gather(
            self.data_provider.get_company_metrics(sym_upper),
            self.data_provider.get_spot_price(sym_upper),
        )
        spot_val = spot_price if spot_price is not None and spot_price > 0 else 0.0

        # 每股自由現金流 FCF_0 = 現價 / P/FCF
        fcf_0 = derive_fcf_per_share(metrics, spot_val)

        # Beta
        beta = _safe_float(metrics.get("beta"))
        if beta is None:
            input_flags.append(FLAG_BETA_DEFAULT)

        # 前瞻 EPS = 現價 / forwardPE；缺值時以近四季 EPS 代理並標註
        fwd_eps = derive_forward_eps(metrics, spot_val)
        if fwd_eps is None:
            trailing_eps = _first_metric(metrics, ("epsTTM", "epsExclExtraItemsTTM"))
            if trailing_eps is not None:
                fwd_eps = trailing_eps
                input_flags.append(FLAG_FORWARD_EPS_TRAILING)

        # g_est：前瞻 EPS 相對近四季 EPS 的隱含成長率；缺值退回歷史、再退回預設
        growth_1y, growth_flag = derive_growth_estimate(
            metrics,
            fwd_eps if FLAG_FORWARD_EPS_TRAILING not in input_flags else None,
        )
        if growth_flag is not None:
            input_flags.append(growth_flag)

        # 3. 同業本益比（優先前瞻本益比，上限 MAX_PEERS 檔）
        peers = await self.data_provider.get_peers(sym_upper)
        peer_pes: list[float] = []
        used_trailing_peer = False
        if peers:
            peers_metrics = await asyncio.gather(
                *(self.data_provider.get_company_metrics(p) for p in peers[:MAX_PEERS])
            )
            for pm in peers_metrics:
                pe_val, is_trailing = peer_pe(pm)
                if pe_val is not None:
                    peer_pes.append(pe_val)
                    used_trailing_peer = used_trailing_peer or is_trailing
        if used_trailing_peer:
            input_flags.append(FLAG_PEER_PE_TRAILING)

        # 4. 計算折現率、DCF 與 Comps
        cost_of_equity, erp = calculate_cost_of_equity(
            us10y=us10y, nfci=nfci, beta=beta
        )
        if fcf_0 is None:
            input_flags.append(FLAG_FCF_UNAVAILABLE)
            dcf_res = DCFResult(
                dcf_value=None, is_valid=False, rejection_reason=FLAG_FCF_UNAVAILABLE
            )
        else:
            dcf_res = calculate_dcf_value(
                DCFInputs(
                    fcf_per_share=fcf_0,
                    growth_rate_1y=growth_1y,
                    cost_of_equity=cost_of_equity,
                )
            )
        comps_res = calculate_comps_value(
            CompsInputs(
                forward_eps=fwd_eps if fwd_eps is not None else 0.0,
                peer_pes=peer_pes,
                nfci=nfci,
            )
        )

        # 5. 治理閘門：只有 HIGH / CRITICAL 視為紅旗（REVIEW / INFO 僅待人工複核）
        gov_flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
        gov_status = evaluate_governance_status(sym_upper, gov_flags)
        governance_red = gov_status.max_severity in ("HIGH", "CRITICAL")

        # 6. 整合公允價值與安全邊際
        fv_res = integrate_fair_value(
            spot_price=spot_val,
            dcf_res=dcf_res,
            comps_res=comps_res,
            discount_rate=cost_of_equity,
            equity_risk_premium=erp,
            is_governance_clean=not governance_red,
        )
        fv_res = replace(fv_res, flags=[*input_flags, *fv_res.flags])

        # 7. 修正動能：current 取 as_of 以前最新快照日 t，prior 限 [t-35, t-28] 窗口、以財期配對
        curr_snapshots = await asyncio.to_thread(
            get_eps_estimate_snapshots, sym_upper, None, date_str
        )
        prior_snapshots: list[EPSEstimateSnapshotRecord] = []
        prior_target: date | None = None
        if curr_snapshots:
            try:
                curr_date = date.fromisoformat(curr_snapshots[0].snapshot_date[:10])
            except ValueError:
                curr_date = target_date
            win_start, win_end, prior_target = prior_window(curr_date)
            prior_snapshots = await asyncio.to_thread(
                get_prior_eps_estimate_snapshots,
                sym_upper,
                win_start.isoformat(),
                win_end.isoformat(),
            )

        # PEAD：最近一筆 PROCESSED 財報預期差，以 NYSE 交易日計算距發布日天數
        latest_surprise = await asyncio.to_thread(
            get_latest_processed_earnings_surprise, sym_upper, date_str
        )
        surprise_score: float | None = None
        days_since_surprise: int | None = None
        if latest_surprise is not None and latest_surprise.composite_score is not None:
            surprise_score = latest_surprise.composite_score
            days_since_surprise = await self._days_since_announcement(
                latest_surprise.announced_on, target_date
            )

        rev_res = evaluate_revision_momentum(
            current_snapshots=curr_snapshots,
            prior_snapshots=prior_snapshots,
            surprise_score=surprise_score,
            days_since_surprise=days_since_surprise,
            prior_target_date=prior_target,
        )
        rev_res.details["current_snapshot_date"] = (
            curr_snapshots[0].snapshot_date if curr_snapshots else None
        )

        # 8. 持久化至資料庫 (Single-Writer Invariant)；無效值存 NULL
        fv_record = FairValueRecord(
            symbol=sym_upper,
            trading_date=date_str,
            dcf_value=fv_res.dcf_value,
            comps_value=fv_res.comps_value,
            fair_value=fv_res.fair_value,
            margin_of_safety=fv_res.margin_of_safety,
            discount_rate=fv_res.discount_rate,
            equity_risk_premium=fv_res.equity_risk_premium,
            flags_json=json.dumps(fv_res.flags, ensure_ascii=False),
            method=fv_res.method,
            spot_price=spot_val if spot_val > 0 else None,
        )
        await save_fair_value(fv_record)

        rev_record = RevisionScoreRecord(
            symbol=sym_upper,
            trading_date=date_str,
            score_30d=rev_res.score_30d,
            breadth_ratio=rev_res.breadth_ratio,
            is_pead_aligned=rev_res.is_pead_aligned,
            detail_json=json.dumps(rev_res.details, ensure_ascii=False),
        )
        await save_revision_score(rev_record)

        mos_str = (
            f"{fv_res.margin_of_safety:+.2%}"
            if fv_res.margin_of_safety is not None
            else "N/A"
        )
        rev_str = (
            f"{rev_res.score_30d:+.1f}" if rev_res.score_30d is not None else "N/A"
        )
        logger.info(
            f"[ValuationService] {sym_upper} 估值完成: FV={fv_res.fair_value} ({fv_res.method}, "
            f"MOS={mos_str}, 動能={rev_str}, PEAD={rev_res.is_pead_aligned}, 旗標={fv_res.flags})"
        )
        return fv_res, rev_res
