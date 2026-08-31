"""委託單管理、盤中掃描與盤前/盤後報告 Embed 建構函式。

依領域拆分為：
- active_orders.py：_build_active_order_ansi_card、create_active_order_card_embed、
  create_active_orders_embed
- telemetry_alignment.py：_build_telemetry_alignment_ansi_card、
  create_telemetry_alignment_embeds、create_telemetry_alignment_embed
- pre_market_briefing.py：build_pre_market_briefing_embed
- post_market_intelligence.py：_parse_post_market_ai_commentary、
  _format_to_target_center_style、_format_to_target_center_style_with_title、
  build_post_market_intelligence_embed
"""

from cogs.embed_builders.order_embeds.active_orders import (
    _build_active_order_ansi_card,
    create_active_order_card_embed,
    create_active_orders_embed,
)
from cogs.embed_builders.order_embeds.telemetry_alignment import (
    _build_telemetry_alignment_ansi_card,
    create_telemetry_alignment_embeds,
    create_telemetry_alignment_embed,
)
from cogs.embed_builders.order_embeds.pre_market_briefing import (
    build_pre_market_briefing_embed,
)
from cogs.embed_builders.order_embeds.post_market_intelligence import (
    _parse_post_market_ai_commentary,
    _format_to_target_center_style,
    _format_to_target_center_style_with_title,
    build_post_market_intelligence_embed,
)

__all__ = [
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
