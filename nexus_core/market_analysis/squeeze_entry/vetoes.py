"""擠壓進場與加碼共用的否決／降級條件（承擔新曝險，一律 fail-closed）。

* 否決：財報緩衝期、大盤負 Gamma 踩踏／流動性危機／Regime 未知（沿用右側鐵律
  條件五 `_confirm_entry_condition5_macro_earnings_gate`），以及 VIX 期限結構深度
  倒掛（Regime IV 宏觀鎖定的第三個分支）。
* 降級：宏觀逃頂評分 tier != NORMAL（含算不出來的 UNKNOWN）。
"""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


async def compute_macro_escape_tier() -> str:
    """宏觀逃頂評分 tier（NORMAL／…／UNKNOWN）。

    未知輸入一律傳 None（不補備援值），`evaluate_macro_top_escape_score` 會把
    「無法確定為常態」回傳為 UNKNOWN；任何例外同樣回傳 UNKNOWN（fail-closed）。
    """
    try:
        from database.cache import get_kv_cache
        from market_analysis.index_microstructure import (
            evaluate_macro_top_escape_score,
            fetch_core_macro_metrics,
            get_market_regime,
        )
        from services.market_data_service import get_vix_term_structure

        regime = await get_market_regime()
        is_negative_gamma: Optional[bool] = (
            None
            if regime == "UNKNOWN"
            else regime in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS")
        )
        vts_data = await get_vix_term_structure()
        vts_ratio: Optional[float] = (
            float(vts_data["vts_ratio"])
            if vts_data.get("is_valid", False) and vts_data.get("vts_ratio") is not None
            else None
        )
        core_metrics = await fetch_core_macro_metrics()
        fg_raw = core_metrics.get("fear_greed")
        fear_greed: Optional[float] = (
            float(fg_raw)
            if fg_raw is not None and not core_metrics.get("_is_fallback")
            else None
        )
        prob = get_kv_cache("macro_fedwatch_probability")
        _, tier, _, _ = evaluate_macro_top_escape_score(
            vts_ratio=vts_ratio,
            fear_greed=fear_greed,
            prob=prob,
            is_negative_gamma=is_negative_gamma,
            satellite_euphoria_ratio=None,
        )
        return str(tier)
    except Exception as e:
        logger.warning(f"宏觀逃頂評分計算失敗，視為 UNKNOWN: {e}")
        return "UNKNOWN"


async def resolve_long_entry_vetoes(symbol: str) -> Tuple[List[str], Optional[str]]:
    """回傳 (否決理由清單, 降級理由)；清單為空代表無否決。"""
    from market_analysis.dynamic_rollover.constants import (
        _REGIME_IV_VTS_BACKWARDATION_RATIO,
    )
    from market_analysis.dynamic_rollover.opportunity_cost import (
        _confirm_entry_condition5_macro_earnings_gate,
    )

    vetoes: List[str] = []
    reasons: List[str] = []
    ok, _ = await _confirm_entry_condition5_macro_earnings_gate(symbol, True, reasons)
    if not ok:
        vetoes.extend(r.replace("條件五❌：", "") for r in reasons if "❌" in r)

    try:
        from services.market_data_service import get_vix_term_structure

        vts = await get_vix_term_structure()
        vts_ratio = float(vts.get("vts_ratio", 0.0) or 0.0)
        if vts_ratio >= _REGIME_IV_VTS_BACKWARDATION_RATIO:
            vetoes.append(f"VIX 期限結構深度倒掛 (vts={vts_ratio:.2f})，宏觀鎖定")
    except Exception as e:
        logger.warning(f"[{symbol}] VIX 期限結構抓取失敗，不視為倒掛: {e}")

    tier = await compute_macro_escape_tier()
    downgrade = None if tier == "NORMAL" else f"逃頂警戒 {tier}"
    return vetoes, downgrade
