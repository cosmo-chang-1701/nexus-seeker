"""內在價值估值與分析師修正動能協調服務 (Valuation & Revision Service)。

職責：
1. 注入流動性體制中樞 (NFCI, US10Y, ERP)。
2. 整合 Finnhub 財務報表 (FCF, Beta, 成長率)、同業倍數與報價。
3. 驅動兩段式 DCF、流動性折讓同業乘數法與安全邊際 (MOS) 計算。
4. 追蹤分析師 EPS 共識預估快照，計算 30 天前後修正動能與 PEAD 共振。
5. 遵循 Single-Writer Invariant，將公允價值與動能分數寫入持久層。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from database.fundamental_pipeline import (
    get_active_governance_flags,
    get_eps_estimate_snapshots,
    get_latest_earnings_surprise,
    get_latest_liquidity_regime,
    get_prior_eps_estimate_snapshots,
    save_eps_estimate_snapshots,
    save_fair_value,
    save_revision_score,
)
from market_analysis.fundamental_pipeline.fair_value import (
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
    FairValueRecord,
    FairValueResult,
    RevisionMomentumResult,
    RevisionScoreRecord,
)
from market_analysis.fundamental_pipeline.revision_momentum import (
    evaluate_revision_momentum,
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


class FinnhubValuationDataProvider:
    """基於 Finnhub 免費公開 API 之估值數據提供者。"""

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

        try:
            client = _get_client()
            resp = await _execute_api_call(
                client.company_peers, symbol=symbol.strip().upper()
            )
            if resp and isinstance(resp, list):
                res: list[str] = [
                    str(p).strip().upper()
                    for p in resp
                    if p and str(p).strip().upper() != symbol.strip().upper()
                ]
                return res
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

    def __init__(self, data_provider: ValuationDataProvider | None = None) -> None:
        self.data_provider: ValuationDataProvider = (
            data_provider
            if data_provider is not None
            else FinnhubValuationDataProvider()
        )

    async def compute_and_save_valuation(
        self,
        symbol: str,
        as_of_date: date | None = None,
    ) -> tuple[FairValueResult, RevisionMomentumResult]:
        """完整執行指定標的之估值與動能計算並寫入資料庫。"""
        sym_upper = symbol.strip().upper()
        now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
        target_date = as_of_date if as_of_date is not None else now_et.date()
        date_str = target_date.isoformat()

        # 1. 注入宏觀流動性體制讀數
        liquidity = await asyncio.to_thread(get_latest_liquidity_regime)
        nfci = liquidity.nfci if liquidity and liquidity.nfci is not None else 0.0
        us10y = liquidity.us10y if liquidity and liquidity.us10y is not None else 4.25

        # 2. 獲取基本面財務指標與現貨市價
        metrics, spot_price = await asyncio.gather(
            self.data_provider.get_company_metrics(sym_upper),
            self.data_provider.get_spot_price(sym_upper),
        )
        spot_val = spot_price if spot_price is not None and spot_price > 0 else 0.0

        # 每股自由現金流 FCF_0
        raw_fcf = (
            metrics.get("fcfPerShareTTM")
            or metrics.get("freeCashFlowPerShareTTM")
            or metrics.get("cashFlowPerShareTTM")
        )
        fcf_0 = _safe_float(raw_fcf) or 0.0

        # Beta 與成長率
        raw_beta = metrics.get("beta")
        beta = _safe_float(raw_beta)

        raw_growth = (
            metrics.get("epsGrowthTTMYoy")
            or metrics.get("epsGrowth3Y")
            or metrics.get("epsGrowth5Y")
            or metrics.get("revenueGrowthTTMYoy")
        )
        growth_float = _safe_float(raw_growth)
        growth_1y = (growth_float / 100.0) if growth_float is not None else 0.05

        # Forward EPS
        raw_fwd_eps = (
            metrics.get("epsNormalizedAnnual")
            or metrics.get("epsTTM")
            or metrics.get("epsExclExtraItemsTTM")
        )
        fwd_eps = _safe_float(raw_fwd_eps) or 0.0

        # 3. 獲取分析師共識快照 (供動能計算與 Forward EPS 備援)
        curr_snapshots = await asyncio.to_thread(get_eps_estimate_snapshots, sym_upper)
        if not curr_snapshots:
            try:
                from services.fundamental_providers import (
                    FinnhubConsensusProvider,
                )

                provider = FinnhubConsensusProvider()
                fetched_snaps = await provider.get_estimate_snapshots(sym_upper)
                if fetched_snaps:
                    await save_eps_estimate_snapshots(fetched_snaps)
                    curr_snapshots = fetched_snaps
            except Exception as e:
                logger.debug(f"[ValuationService] 快照補抓失敗 ({sym_upper}): {e}")

        # 若 metrics 缺乏有效 Forward EPS，以分析師共識 0y 或 +1y 預估中值備援
        if fwd_eps <= 0 and curr_snapshots:
            snap_0y = next(
                (s for s in curr_snapshots if s.horizon == "0y" and s.eps_mean > 0),
                None,
            )
            if snap_0y:
                fwd_eps = snap_0y.eps_mean
            else:
                snap_1y = next(
                    (
                        s
                        for s in curr_snapshots
                        if s.horizon == "+1y" and s.eps_mean > 0
                    ),
                    None,
                )
                if snap_1y:
                    fwd_eps = snap_1y.eps_mean

        # 4. 獲取同業本益比 (上限 10 檔)
        peers = await self.data_provider.get_peers(sym_upper)
        peer_pes: list[float] = []
        if peers:
            sample_peers = peers[:10]
            peer_metric_tasks = [
                self.data_provider.get_company_metrics(p) for p in sample_peers
            ]
            peers_metrics = await asyncio.gather(*peer_metric_tasks)
            for pm in peers_metrics:
                pe_val = _safe_float(
                    pm.get("peTTM")
                    or pm.get("peNormalizedAnnual")
                    or pm.get("peExclExtraTTM")
                )
                if pe_val is not None and pe_val > 0:
                    peer_pes.append(pe_val)

        # 5. 計算折現率、DCF 與 Comps
        cost_of_equity, erp = calculate_cost_of_equity(
            us10y=us10y, nfci=nfci, beta=beta
        )
        dcf_res = calculate_dcf_value(
            DCFInputs(
                fcf_per_share=fcf_0,
                growth_rate_1y=growth_1y,
                cost_of_equity=cost_of_equity,
            )
        )
        comps_res = calculate_comps_value(
            CompsInputs(
                forward_eps=fwd_eps,
                peer_pes=peer_pes,
                nfci=nfci,
            )
        )

        # 6. 治理閘門審查
        gov_flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
        gov_status = evaluate_governance_status(sym_upper, gov_flags)

        # 7. 整合公允價值與安全邊際
        fv_res = integrate_fair_value(
            spot_price=spot_val,
            dcf_res=dcf_res,
            comps_res=comps_res,
            discount_rate=cost_of_equity,
            equity_risk_premium=erp,
            is_governance_clean=gov_status.is_clean,
        )

        # 8. 獲取約 30 天前 (28-35 天視角) 歷史快照並計算修正動能
        before_30d = (target_date - timedelta(days=28)).isoformat()
        prior_snapshots = await asyncio.to_thread(
            get_prior_eps_estimate_snapshots, sym_upper, before_30d
        )

        # 查詢最新季度預期差以驗證 PEAD
        latest_surprise = await asyncio.to_thread(
            get_latest_earnings_surprise, sym_upper
        )
        surprise_score: float | None = None
        days_since_surprise: int | None = None
        if latest_surprise and latest_surprise.composite_score is not None:
            surprise_score = latest_surprise.composite_score
            # 推算發布天數
            if latest_surprise.created_at:
                try:
                    c_date = datetime.fromisoformat(latest_surprise.created_at).date()
                    days_since_surprise = (target_date - c_date).days
                except Exception:
                    days_since_surprise = 30
            else:
                days_since_surprise = 30

        rev_res = evaluate_revision_momentum(
            current_snapshots=curr_snapshots,
            prior_snapshots=prior_snapshots,
            surprise_score=surprise_score,
            days_since_surprise=days_since_surprise,
        )

        # 8. 持久化至資料庫 (Single-Writer Invariant)
        fair_val_to_save = fv_res.fair_value if fv_res.fair_value is not None else 0.0
        mos_to_save = (
            fv_res.margin_of_safety if fv_res.margin_of_safety is not None else 0.0
        )
        fv_record = FairValueRecord(
            symbol=sym_upper,
            trading_date=date_str,
            dcf_value=fv_res.dcf_value,
            comps_value=fv_res.comps_value,
            fair_value=fair_val_to_save,
            margin_of_safety=mos_to_save,
            discount_rate=fv_res.discount_rate,
            equity_risk_premium=fv_res.equity_risk_premium,
            flags_json=json.dumps(fv_res.flags, ensure_ascii=False),
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
        logger.info(
            f"[ValuationService] {sym_upper} 估值完成: FV=${fv_res.fair_value} "
            f"(MOS={mos_str}, 動能={rev_res.score_30d:+.1f}, PEAD={rev_res.is_pead_aligned})"
        )
        return fv_res, rev_res
