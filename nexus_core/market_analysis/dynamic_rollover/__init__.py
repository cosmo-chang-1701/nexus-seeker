"""Facade for the Dynamic Rollover Engine (動態轉倉引擎).

Public import path stays `market_analysis.dynamic_rollover`. Scenario logic is
split across sibling modules (fundamental_thesis / opportunity_cost /
anti_washout / margin_defense / structural_signals); this file assembles
`DynamicRolloverEngine` and re-exports the public API + the module-attribute
names that existing tests patch via `@patch("market_analysis.dynamic_rollover.X")`.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from config import LLM_MODEL_NAME
from database.user_settings import get_full_user_context
from market_analysis.gamma_cliff_confirmation import is_gamma_cliff_confirmed
from market_analysis.ivr_strategy_gate import is_selling_locked_by_ivr
from services.llm_service import client, is_memory_safe
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)

from .anti_washout import (  # noqa: E402
    _AntiWashoutMixin,
    apply_ivr_strategy_overlay_impl,
    check_satellite_rebalancing_impl,
)
from .constants import CORE_DEFENSE_ETF_SYMBOLS  # noqa: E402
from .fundamental_thesis import evaluate_fundamental_thesis_impl  # noqa: E402
from .margin_defense import _MarginDefenseMixin, evaluate_margin_defense_impl  # noqa: E402
from .models import FundamentalThesisResult, RolloverScenario  # noqa: E402
from .opportunity_cost import _OpportunityCostMixin  # noqa: E402
from .structural_signals import (  # noqa: E402
    _resolve_canonical_anchor_base,
    _scan_gex_walls,
    compute_structural_breakdown_signals_impl,
)

__all__ = [
    "DynamicRolloverEngine",
    "CORE_DEFENSE_ETF_SYMBOLS",
    "FundamentalThesisResult",
    "RolloverScenario",
    "_resolve_canonical_anchor_base",
    "_scan_gex_walls",
]


class DynamicRolloverEngine(
    _OpportunityCostMixin, _AntiWashoutMixin, _MarginDefenseMixin
):
    def __init__(self) -> None:
        self._structural_signals_cache: BoundedCache = BoundedCache(max_size=256)

    async def evaluate_fundamental_thesis(
        self,
        symbol: str,
        fundamental_text: str,
        form_type: str = "",
        sections: Optional[Dict[str, str]] = None,
    ) -> Optional[FundamentalThesisResult]:
        """
        邏輯 (1): 原型假設破滅
        傳入 FastAPI 爬取的法說會或財報文本，使用 LLM 判定基本面護城河是否流失。
        `form_type` (10-K/10-Q/8-K) 與 `sections` (結構化擷取段落) 為選填，
        用於依財報格式客製化 LLM 分析框架；留空則行為與未區分格式時完全一致。
        """
        return await evaluate_fundamental_thesis_impl(
            client,
            is_memory_safe,
            LLM_MODEL_NAME,
            symbol,
            fundamental_text,
            form_type=form_type,
            sections=sections,
        )

    def _apply_ivr_strategy_overlay(
        self, options_strategy: str, strategy_override: str, ivr: float
    ) -> str:
        """
        IVR 策略防禦與微調。
        NOTE: strategy_override 傳入非空字串時會【完全取代】IVR 鎖定後綴邏輯 (elif，
        而非疊加)。此設計用於 Bear Call Spread / Trailing Stop 等戰術覆寫場景，
        該場景下 strategy_override 本身的文字已包含完整防守資訊，故刻意跳過 IVR 後綴。
        """
        return apply_ivr_strategy_overlay_impl(
            is_selling_locked_by_ivr, options_strategy, strategy_override, ivr
        )

    async def _compute_structural_breakdown_signals(
        self,
        symbol: str,
        spot: float,
        put_wall: float,
        gamma_flip: float,
        atr_14: float,
        sqz_mom: float,
        skew: float,
        price_15m_close: float,
        gex_profile_data: Optional[Dict[str, Any]],
        asset_class: str,
        call_wall: float = 0.0,
        hvn: float = 0.0,
    ) -> Tuple[bool, bool, float, float, float, float]:
        """
        共用結構性破位 / 主力空頭封殺訊號計算，供 Scenario 3/4 共同呼叫。
        詳見 structural_signals.compute_structural_breakdown_signals_impl。
        """
        return await compute_structural_breakdown_signals_impl(
            self,
            is_gamma_cliff_confirmed,
            symbol,
            spot,
            put_wall,
            gamma_flip,
            atr_14,
            sqz_mom,
            skew,
            price_15m_close,
            gex_profile_data,
            asset_class,
            call_wall=call_wall,
            hvn=hvn,
        )

    async def check_satellite_rebalancing(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        total_account_value: float,
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (3): 核心與衛星比例再平衡 + 深度微觀結構與選擇權籌碼驅動
        包含勝率傾斜與雜訊避險等高階戰術。
        """
        return await check_satellite_rebalancing_impl(
            self, get_full_user_context, user_id, portfolio_assets, total_account_value
        )

    async def evaluate_margin_defense(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (4): 槓桿與保證金防禦 (Leverage & Margin Defense)
        """
        return await evaluate_margin_defense_impl(
            self,
            get_full_user_context,
            user_id,
            portfolio_assets,
            already_flagged_symbols,
        )
