"""embed_builders 子套件 — 向後相容統一匯出層。

所有原本在 `cogs.embed_builder` 中的公開函式與類別，均透過此 __init__.py 重新匯出，
確保所有現有呼叫端（其他 Cog、service、test）在重構期間不需修改任何 import 語句。

模組分工：
  _core.py            — NexusEmbed 基底類別與 install_nexus_embed()
  _ansi_utils.py      — ANSI/視覺工具函式
  _embed_helpers.py   — 通用 Embed 工具（欄位 helper、split_embed_by_fields 等）
  scan_embeds.py      — Sentiment Scan、Macro Scan、FOMC、Stress Test、Covered Call、Earnings、Sector Flow
  alert_embeds.py     — Option Scan、PSQ、News/Reddit、Polymarket、Quote、各類警報
  portfolio_embeds.py — Holdings、Trades、Strategic Dash、Tactical Symbol/Hedge
  watchlist_embeds.py — Watchlist 清單、心跳 Signal、總覽
  report_embeds.py    — Portfolio Report、Transition、VTR、Scan Report、Rehedge、DDP、Volatility、AI Analysis
  settings_embeds.py  — Notification Settings、Account Settings、Info、Error
  market_embeds.py    — Max Pain、Financial Runway、System Health、Asset Promotion、
                        Transition Simulation、Market Calendar、IV Risk Scan、
                        Radar Scan、Market Macro Overview
  hedge_embeds.py     — Event Impact、Hedge Settlement/List/Alert、Proactive Event、
                        Memory Alert、Polymarket Whale、Option Defense、Volatility Risk
  order_embeds.py     — Intraday Scan、Active Order、Active Orders、Telemetry Alignment、
                        Pre-Market Briefing、Post-Market Intelligence
"""

# ── Core ─────────────────────────────────────────────────────────────────────
from cogs.embed_builders._core import NexusEmbed, install_nexus_embed

# ── ANSI Utilities ────────────────────────────────────────────────────────────
from cogs.embed_builders._ansi_utils import (
    _visual_len,
    _pad_string,
    _clean_ansi,
    _truncate_with_boundary,
    _safe_float,
    _is_macro_report_marker,
    _chunk_text_blocks,
    _wrap_visual,
    _visual_truncate,
)

# ── Embed Helpers ─────────────────────────────────────────────────────────────
from cogs.embed_builders._embed_helpers import (
    _safe_embed_field_value,
    _chunk_ansi_table,
    _safe_embed_codeblock_value,
    _build_watchlist_style_panel,
    _report_embed_color,
    _extract_report_batch,
    _parse_ai_report_sections,
    _append_ai_report_fields,
    split_embed_by_fields,
    get_embed_length,
    chunk_embeds,
    add_news_field,
    add_reddit_field,
    _parse_and_format_positions_table,
    get_ema_signal_ui,
    _add_trend_and_support_fields,
    _add_sentiment_fields,
    _build_embed_base,
    _add_vix_battle_status_field,
    _add_market_overview_fields,
    _add_volatility_fields,
    _add_performance_and_kelly_fields,
    _add_earnings_fields,
    _add_covered_call_fields,
    _add_expected_move_fields,
    _add_liquidity_fields,
    _add_strategy_upgrade_fields,
    _add_risk_optimization_fields,
    _add_hedge_unlock_fields,
    _add_ai_verification_fields,
)

# ── Scan Embeds ───────────────────────────────────────────────────────────────
from cogs.embed_builders.scan_embeds import (
    _format_uoa_field,
    create_sentiment_scan_embed,
    create_macro_scan_embed,
    create_fomc_escape_window_embed,
    create_stress_test_embed,
    create_covered_call_unlock_embed,
    create_earnings_report_embed,
    create_sector_flow_report_embed,
    create_cc_recovery_embed,
)

# ── Alert Embeds ──────────────────────────────────────────────────────────────
from cogs.embed_builders.alert_embeds import (
    create_scan_embed,
    create_psq_embed,
    create_news_scan_embed,
    create_reddit_scan_embed,
    create_media_sentiment_embed,
    create_polymarket_list_embed,
    create_polymarket_status_embed,
    create_quote_embed,
    create_profit_lock_alert_embed,
    create_gamma_fragility_embed,
    create_pre_market_earnings_embed,
    create_ditm_transition_alert_embed,
    create_intraday_execution_guide_embed,
    create_vtr_settlement_notice_embed,
)

# ── Portfolio Embeds ──────────────────────────────────────────────────────────
from cogs.embed_builders.portfolio_embeds import (
    create_holdings_embed,
    create_trades_embed,
    create_strategic_dash_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
)

# ── Watchlist Embeds ──────────────────────────────────────────────────────────
from cogs.embed_builders.watchlist_embeds import (
    create_watchlist_embed,
    create_watchlist_signal_embed,
    create_watchlist_overview_embed,
)

# ── Report Embeds ─────────────────────────────────────────────────────────────
from cogs.embed_builders.report_embeds import (
    create_portfolio_report_embed,
    create_transition_suggestion_embed,
    build_vtr_stats_embed,
    build_scan_report,
    create_rehedge_embed,
    create_ddp_embed,
    create_volatility_embed,
    build_hedge_analysis_field,
    create_ai_analysis_embed,
    create_next_day_strategy_embed,
)

# ── Settings Embeds ───────────────────────────────────────────────────────────
from cogs.embed_builders.settings_embeds import (
    create_notification_settings_embed,
    create_account_settings_embed,
    create_info_embed,
    create_error_embed,
)

# ── Market Embeds ─────────────────────────────────────────────────────────────
from cogs.embed_builders.market_embeds import (
    create_max_pain_embed,
    create_financial_runway_embed,
    create_system_health_embed,
    create_asset_promotion_embed,
    create_transition_simulation_embed,
    create_market_calendar_embed,
    create_iv_risk_scan_embed,
    build_radar_scan_embed,
    build_market_macro_overview_embed,
)

# ── Hedge & Risk Alert Embeds ─────────────────────────────────────────────────
from cogs.embed_builders.hedge_embeds import (
    create_event_impact_embed,
    create_hedge_settlement_embed,
    create_hedge_list_embed,
    create_hedge_alert_embed,
    create_proactive_event_alert_embed,
    create_memory_alert_embed,
    create_polymarket_whale_alert_embed,
    create_option_defense_alert_embed,
    create_volatility_risk_alert_embed,
)

# ── Order & Timing Embeds ─────────────────────────────────────────────────────
from cogs.embed_builders.order_embeds import (
    create_intraday_scan_embed,
    _build_active_order_ansi_card,
    create_active_order_card_embed,
    create_active_orders_embed,
    _build_telemetry_alignment_ansi_card,
    create_telemetry_alignment_embeds,
    create_telemetry_alignment_embed,
    build_pre_market_briefing_embed,
    _parse_post_market_ai_commentary,
    _format_to_target_center_style,
    _format_to_target_center_style_with_title,
    build_post_market_intelligence_embed,
)

__all__ = [
    # Core
    "NexusEmbed",
    "install_nexus_embed",
    # ANSI utils
    "_visual_len",
    "_pad_string",
    "_clean_ansi",
    "_truncate_with_boundary",
    "_safe_float",
    "_is_macro_report_marker",
    "_chunk_text_blocks",
    "_wrap_visual",
    "_visual_truncate",
    # Embed helpers
    "_safe_embed_field_value",
    "_chunk_ansi_table",
    "_safe_embed_codeblock_value",
    "_build_watchlist_style_panel",
    "_report_embed_color",
    "_extract_report_batch",
    "_parse_ai_report_sections",
    "_append_ai_report_fields",
    "split_embed_by_fields",
    "get_embed_length",
    "chunk_embeds",
    "add_news_field",
    "add_reddit_field",
    "_parse_and_format_positions_table",
    "get_ema_signal_ui",
    "_add_trend_and_support_fields",
    "_add_sentiment_fields",
    "_build_embed_base",
    "_add_vix_battle_status_field",
    "_add_market_overview_fields",
    "_add_volatility_fields",
    "_add_performance_and_kelly_fields",
    "_add_earnings_fields",
    "_add_covered_call_fields",
    "_add_expected_move_fields",
    "_add_liquidity_fields",
    "_add_strategy_upgrade_fields",
    "_add_risk_optimization_fields",
    "_add_hedge_unlock_fields",
    "_add_ai_verification_fields",
    # Scan embeds
    "_format_uoa_field",
    "create_sentiment_scan_embed",
    "create_macro_scan_embed",
    "create_fomc_escape_window_embed",
    "create_stress_test_embed",
    "create_covered_call_unlock_embed",
    "create_earnings_report_embed",
    "create_sector_flow_report_embed",
    "create_cc_recovery_embed",
    # Alert embeds
    "create_scan_embed",
    "create_psq_embed",
    "create_news_scan_embed",
    "create_reddit_scan_embed",
    "create_media_sentiment_embed",
    "create_polymarket_list_embed",
    "create_polymarket_status_embed",
    "create_quote_embed",
    "create_profit_lock_alert_embed",
    "create_gamma_fragility_embed",
    "create_pre_market_earnings_embed",
    "create_ditm_transition_alert_embed",
    "create_intraday_execution_guide_embed",
    "create_vtr_settlement_notice_embed",
    # Portfolio embeds
    "create_holdings_embed",
    "create_trades_embed",
    "create_strategic_dash_embed",
    "create_tactical_symbol_embed",
    "create_tactical_hedge_embed",
    # Watchlist embeds
    "create_watchlist_embed",
    "create_watchlist_signal_embed",
    "create_watchlist_overview_embed",
    # Report embeds
    "create_portfolio_report_embed",
    "create_transition_suggestion_embed",
    "build_vtr_stats_embed",
    "build_scan_report",
    "create_rehedge_embed",
    "create_ddp_embed",
    "create_volatility_embed",
    "build_hedge_analysis_field",
    "create_ai_analysis_embed",
    "create_next_day_strategy_embed",
    # Settings embeds
    "create_notification_settings_embed",
    "create_account_settings_embed",
    "create_info_embed",
    "create_error_embed",
    # Market embeds
    "create_max_pain_embed",
    "create_financial_runway_embed",
    "create_system_health_embed",
    "create_asset_promotion_embed",
    "create_transition_simulation_embed",
    "create_market_calendar_embed",
    "create_iv_risk_scan_embed",
    "build_radar_scan_embed",
    "build_market_macro_overview_embed",
    # Hedge embeds
    "create_event_impact_embed",
    "create_hedge_settlement_embed",
    "create_hedge_list_embed",
    "create_hedge_alert_embed",
    "create_proactive_event_alert_embed",
    "create_memory_alert_embed",
    "create_polymarket_whale_alert_embed",
    "create_option_defense_alert_embed",
    "create_volatility_risk_alert_embed",
    # Order embeds
    "create_intraday_scan_embed",
    "_build_active_order_ansi_card",
    "create_active_order_card_embed",
    "create_active_orders_embed",
    "_build_telemetry_alignment_ansi_card",
    "create_telemetry_alignment_embeds",
    "create_telemetry_alignment_embed",
    "build_pre_market_briefing_embed",
    "_parse_post_market_ai_commentary",
    "_format_to_target_center_style",
    "_format_to_target_center_style_with_title",
    "build_post_market_intelligence_embed",
]
