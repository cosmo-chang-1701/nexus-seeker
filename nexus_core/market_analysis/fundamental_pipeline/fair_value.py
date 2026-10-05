"""兩段式現金流折現 (DCF) 與流動性折讓同業乘數法 (Comps) 估值模型。

本模組為純演算法葉模組，無外部 I/O 副作用。
實現資本成本折現率精算、2-Stage FCF DCF 估值、
芝加哥聯準會 NFCI 流動性折讓同業倍數法與安全邊際 (Margin of Safety, MOS) 整合評估。

數學規範：
1. 資本成本折現率 (Cost of Equity):
   ERP_t = ERP_BASE + lambda_NFCI * clip(NFCI_t, -1.0, +2.0)
   ERP_BASE = 0.045 (4.5%), lambda_NFCI = 0.010
   beta = clip(beta_asset, 0.5, 2.0)
   r = DGS10 + beta * ERP_t

2. 兩段式自由現金流折現模型 (Two-Stage FCF DCF):
   FV_DCF = sum_{t=1}^5 [FCF_0 * (1 + g_1)^t / (1 + r)^t]
          + [FCF_0 * (1 + g_1)^5 * (1 + g_T)] / [(r - g_T) * (1 + r)^5]
   - FCF_0 <= 0 則 DCF 模型失效退回同業乘數。
   - g_1 = clip(g_est, -0.10, +0.25)
   - g_T = 0.025 (2.5% 永續成長定錨)
   - 安全利差邊界：r - g_T < 0.010 則強制拒絕 DCF 輸出。

3. 流動性折讓同業乘數法 (Comps Multiple with Liquidity Penalty):
   FV_Comps = Forward_EPS * (Median(Peers_Forward_PE) * exp(-0.10 * clip(NFCI_t, -1.0, +2.0)))
   - 同業有效本益比需至少 3 家。

4. 公允價值整合與安全邊際 (Margin of Safety, MOS):
   MOS = (FV - Spot Price) / FV
   當 MOS >= 0.25 (25% 安全邊際) 且無重大治理紅旗時，評定為深度價值標的。
"""

from __future__ import annotations

import math
import statistics

from market_analysis.fundamental_pipeline.models import (
    CompsInputs,
    CompsResult,
    DCFInputs,
    DCFResult,
    FairValueResult,
)

ERP_BASE: float = 0.045
LAMBDA_NFCI: float = 0.010
DEFAULT_PERPETUAL_GROWTH: float = 0.025
MIN_SPREAD_THRESHOLD: float = 0.010
MIN_PEERS_COUNT: int = 3
DEEP_VALUE_MOS_THRESHOLD: float = 0.25


def calculate_equity_risk_premium(nfci: float | None) -> float:
    """依芝加哥聯準會金融狀況指數 (NFCI) 計算動態股權風險溢價 (ERP)。

    ERP_t = ERP_BASE + lambda_NFCI * clip(NFCI, -1.0, 2.0)
    NFCI 每緊縮 1 個標準差，市場風險溢價上升 100 bps。
    """
    if nfci is None:
        return ERP_BASE
    clipped_nfci = max(-1.0, min(2.0, nfci))
    return ERP_BASE + LAMBDA_NFCI * clipped_nfci


def calculate_cost_of_equity(
    us10y: float,
    nfci: float | None = None,
    beta: float | None = None,
) -> tuple[float, float]:
    """計算資產權益資本成本 (Cost of Equity, r) 與動態 ERP。

    - us10y: 若大於 1.0 則自動除以 100 轉為小數 (例如 4.25% -> 0.0425)。
    - beta: 箝制於 [0.5, 2.0]，缺失時預設 1.0。
    回傳 (r, erp)。
    """
    dgs10 = us10y / 100.0 if us10y > 1.0 else us10y
    erp = calculate_equity_risk_premium(nfci)
    eff_beta = max(0.5, min(2.0, beta if beta is not None else 1.0))
    r = dgs10 + eff_beta * erp
    return r, erp


def calculate_dcf_value(inputs: DCFInputs) -> DCFResult:
    """計算兩段式每股自由現金流折現 (2-Stage FCF DCF) 公允價值。

    拒絕條件：
    1. FCF_0 <= 0 (非正現金流)。
    2. r <= 0 (無意義折現率)。
    3. r - g_T < 0.010 (分母利差過窄防禦)。
    """
    fcf_0 = inputs.fcf_per_share
    if fcf_0 <= 0:
        return DCFResult(
            dcf_value=None,
            is_valid=False,
            rejection_reason="FCF_NON_POSITIVE",
        )

    r = inputs.cost_of_equity
    if r <= 0:
        return DCFResult(
            dcf_value=None,
            is_valid=False,
            rejection_reason="DISCOUNT_RATE_NON_POSITIVE",
        )

    g_t = inputs.perpetual_growth_rate
    spread = r - g_t
    if spread < MIN_SPREAD_THRESHOLD:
        return DCFResult(
            dcf_value=None,
            is_valid=False,
            rejection_reason="SPREAD_TOO_NARROW",
        )

    # 箝制第 1 階段 5 年分析師成長率 [-10%, +25%]
    g_1 = max(-0.10, min(0.25, inputs.growth_rate_1y))

    # 1. 前 5 年預估現金流折現總和
    pv_stage1 = 0.0
    for t in range(1, 6):
        fcf_t = fcf_0 * ((1.0 + g_1) ** t)
        pv_stage1 += fcf_t / ((1.0 + r) ** t)

    # 2. 永續終端價值折現 (Terminal Value PV)
    fcf_5 = fcf_0 * ((1.0 + g_1) ** 5)
    fcf_terminal = fcf_5 * (1.0 + g_t)
    tv = fcf_terminal / spread
    pv_tv = tv / ((1.0 + r) ** 5)

    fair_val = pv_stage1 + pv_tv
    return DCFResult(
        dcf_value=round(fair_val, 2),
        is_valid=True,
        rejection_reason=None,
    )


def calculate_comps_value(inputs: CompsInputs) -> CompsResult:
    """計算流動性折讓同業乘數法 (Comps Multiple with Liquidity Penalty) 公允價值。

    拒絕條件：
    1. Forward EPS <= 0 (虧損標的無法乘數估值)。
    2. 有效正本益比同業數 < 3。
    3. 中位數本益比 <= 0。
    """
    fwd_eps = inputs.forward_eps
    if fwd_eps <= 0:
        return CompsResult(
            comps_value=None,
            median_pe=None,
            liquidity_penalty_factor=1.0,
            is_valid=False,
            rejection_reason="FORWARD_EPS_NON_POSITIVE",
        )

    valid_pes = [p for p in inputs.peer_pes if p > 0 and not math.isnan(p)]
    if len(valid_pes) < MIN_PEERS_COUNT:
        return CompsResult(
            comps_value=None,
            median_pe=None,
            liquidity_penalty_factor=1.0,
            is_valid=False,
            rejection_reason="INSUFFICIENT_PEERS",
        )

    med_pe = statistics.median(valid_pes)
    if med_pe <= 0:
        return CompsResult(
            comps_value=None,
            median_pe=None,
            liquidity_penalty_factor=1.0,
            is_valid=False,
            rejection_reason="MEDIAN_PE_NON_POSITIVE",
        )

    # 芝加哥聯準會 NFCI 流動性懲罰乘數: exp(-0.10 * clip(NFCI, -1.0, 2.0))
    clipped_nfci = max(-1.0, min(2.0, inputs.nfci))
    penalty_factor = math.exp(-0.10 * clipped_nfci)

    comps_val = fwd_eps * med_pe * penalty_factor
    return CompsResult(
        comps_value=round(comps_val, 2),
        median_pe=round(med_pe, 2),
        liquidity_penalty_factor=round(penalty_factor, 4),
        is_valid=True,
        rejection_reason=None,
    )


def integrate_fair_value(
    spot_price: float,
    dcf_res: DCFResult,
    comps_res: CompsResult,
    discount_rate: float,
    equity_risk_premium: float,
    is_governance_clean: bool = True,
) -> FairValueResult:
    """整合 DCF 與同業乘數法公允價值，計算安全邊際與深度價值旗標。

    - 兩者皆有效：50% / 50% 混合中樞 (BLENDED)。
    - DCF 失效但 Comps 有效：採用 Comps (COMPS_ONLY)。
    - Comps 失效但 DCF 有效：採用 DCF (DCF_ONLY)。
    - 兩者皆失效：回傳 None (NONE)。
    """
    flags: list[str] = []
    if not is_governance_clean:
        flags.append("GOVERNANCE_RISK")

    final_fv: float | None = None
    method: str = "NONE"

    if dcf_res.is_valid and comps_res.is_valid:
        if dcf_res.dcf_value is not None and comps_res.comps_value is not None:
            final_fv = (dcf_res.dcf_value + comps_res.comps_value) / 2.0
            method = "BLENDED"
    elif dcf_res.is_valid and dcf_res.dcf_value is not None:
        final_fv = dcf_res.dcf_value
        method = "DCF_ONLY"
    elif comps_res.is_valid and comps_res.comps_value is not None:
        final_fv = comps_res.comps_value
        method = "COMPS_ONLY"

    if final_fv is None or final_fv <= 0:
        return FairValueResult(
            fair_value=None,
            margin_of_safety=None,
            dcf_value=dcf_res.dcf_value,
            comps_value=comps_res.comps_value,
            discount_rate=round(discount_rate, 4),
            equity_risk_premium=round(equity_risk_premium, 4),
            is_deep_value=False,
            flags=flags,
            method="NONE",
        )

    final_fv = round(final_fv, 2)
    mos: float | None = None
    if spot_price > 0:
        mos = round((final_fv - spot_price) / final_fv, 4)

    is_deep_val = False
    if mos is not None:
        if mos >= DEEP_VALUE_MOS_THRESHOLD:
            if is_governance_clean:
                is_deep_val = True
                flags.append("DEEP_VALUE")
            else:
                flags.append("DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE")
        elif mos >= 0.10:
            flags.append("MODERATE_DISCOUNT")
        elif mos >= -0.10:
            flags.append("FAIRLY_VALUED")
        else:
            flags.append("OVERVALUED")

    return FairValueResult(
        fair_value=final_fv,
        margin_of_safety=mos,
        dcf_value=dcf_res.dcf_value,
        comps_value=comps_res.comps_value,
        discount_rate=round(discount_rate, 4),
        equity_risk_premium=round(equity_risk_premium, 4),
        is_deep_value=is_deep_val,
        flags=flags,
        method=method,
    )
