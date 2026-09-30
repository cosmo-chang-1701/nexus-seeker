"""盤中量化掃描與自選股評估管道。

依領域拆分為：
- metrics.py：自選股量化指標建構（build_enhanced_watchlist_metrics 等）
- events.py：事件風控上下文（build_watchlist_event_context 等）
- evaluation.py：單一標的完整評估（evaluate_watchlist_symbol）
- skew_commentary.py：Skew 規則化判讀與進階過濾器
- pipeline.py：IntradayScanPipeline 異步掃描管道

注意：`build_enhanced_watchlist_metrics`、`build_watchlist_event_context`、
`build_watchlist_skew_rule_commentary` 在跨子模組呼叫處（evaluation.py、
pipeline.py）皆改為函式內延遲匯入（`from market_analysis.intraday_pipeline
import ...`），以確保測試對本檔案頂層屬性的 `patch(...)` 仍然有效。
`derive_watchlist_option_guidance`、`build_watchlist_option_plan`、
`is_market_open`、`datetime` 為外部模組符號，測試改為直接 patch
`market_analysis.intraday_pipeline.pipeline.<name>`。
"""

from market_analysis.intraday_pipeline.metrics import (
    _quote_price,
    get_cached_volume_poc,
    save_cached_volume_poc,
    get_cached_gex_putwall,
    save_cached_gex_putwall,
    _estimate_volume_poc,
    _relative_strength_vs_spy,
    _estimate_options_wall_metrics,
    build_enhanced_watchlist_metrics,
    _WATCHLIST_METRICS_CACHE,
    _WATCHLIST_METRICS_TTL,
)
from market_analysis.intraday_pipeline.events import (
    _hours_to_days_text,
    _resolve_watchlist_event_mode,
    _build_watchlist_event_summary,
    build_watchlist_event_context,
)
from market_analysis.intraday_pipeline.evaluation import (
    evaluate_watchlist_symbol,
)
from market_analysis.intraday_pipeline.skew_commentary import (
    _skew_route_sync_note,
    _skew_momentum_note,
    _format_skew_commentary,
    build_watchlist_skew_rule_commentary,
    evaluate_advanced_filters,
    _SKEW_PCR_DIVERGENCE_WARNING,
    _SKEW_BADGE_NEUTRAL,
    _SKEW_BADGE_PREMIUM_HARVEST,
    _SKEW_BADGE_DEFENSIVE,
    _SKEW_BADGE_DIVERGENCE,
    _SKEW_BADGE_BULLISH,
)
from market_analysis.intraday_pipeline.pipeline import (
    IntradayScanPipeline,
)

# ── 相容性 re-export：原單一檔案在模組頂層匯入的外部符號 ──────────────────
from market_time import ny_tz, is_market_open
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEvaluation,
    WatchlistEventContext,
    WatchlistRiskMode,
    WatchlistTacticalPlan,
    ScanParams,
)
from risk_engine.nro import WatchlistRiskController
from services.market_data_service import BoundedCache
from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
)
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.signal_calculator import (
    _derive_buy_levels,
    _derive_sell_levels,
    _buy_zone_status,
    _sell_zone_status,
    _extract_pe_ratio,
    calculate_dynamic_trading_signals,
)
from market_analysis.option_guidance import (
    derive_watchlist_option_guidance,
    build_watchlist_option_plan,
)

__all__ = [
    # metrics.py
    "_quote_price",
    "get_cached_volume_poc",
    "save_cached_volume_poc",
    "get_cached_gex_putwall",
    "save_cached_gex_putwall",
    "_estimate_volume_poc",
    "_relative_strength_vs_spy",
    "_estimate_options_wall_metrics",
    "build_enhanced_watchlist_metrics",
    "_WATCHLIST_METRICS_CACHE",
    "_WATCHLIST_METRICS_TTL",
    # events.py
    "_hours_to_days_text",
    "_resolve_watchlist_event_mode",
    "_build_watchlist_event_summary",
    "build_watchlist_event_context",
    # evaluation.py
    "evaluate_watchlist_symbol",
    # skew_commentary.py
    "_skew_route_sync_note",
    "_skew_momentum_note",
    "_format_skew_commentary",
    "build_watchlist_skew_rule_commentary",
    "evaluate_advanced_filters",
    "_SKEW_PCR_DIVERGENCE_WARNING",
    "_SKEW_BADGE_NEUTRAL",
    "_SKEW_BADGE_PREMIUM_HARVEST",
    "_SKEW_BADGE_DEFENSIVE",
    "_SKEW_BADGE_DIVERGENCE",
    "_SKEW_BADGE_BULLISH",
    # pipeline.py
    "IntradayScanPipeline",
    # pass-through re-exports
    "ny_tz",
    "is_market_open",
    "EnhancedWatchlistMetrics",
    "WatchlistEvaluation",
    "WatchlistEventContext",
    "WatchlistRiskMode",
    "WatchlistTacticalPlan",
    "ScanParams",
    "WatchlistRiskController",
    "BoundedCache",
    "TraderAccountState",
    "OptionHolding",
    "TickerMarketData",
    "NexusGammaSqueezeEngine",
    "_derive_buy_levels",
    "_derive_sell_levels",
    "_buy_zone_status",
    "_sell_zone_status",
    "_extract_pe_ratio",
    "calculate_dynamic_trading_signals",
    "derive_watchlist_option_guidance",
    "build_watchlist_option_plan",
]
