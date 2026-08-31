"""警報與通知類 Embed 建構函式。

依領域拆分為：
- option_scan.py：選擇權掃描報告（create_scan_embed）
- psq.py：PowerSqueeze 策略報告（create_psq_embed）
- sentiment_feeds.py：新聞/Reddit/媒體輿情社群（create_news_scan_embed、
  create_reddit_scan_embed、create_media_sentiment_embed）
- polymarket.py：Polymarket 鯨魚追蹤與機率閃崩警報
- quote_and_risk_alerts.py：即時報價與持倉風險/警報（DITM 停利、Gamma 脆弱性、
  VTR 結算、情境警報、保證金、VIX 尾部風險等）
- market_signal_alerts.py：WTI 原油與個股價量突破警報
"""

from cogs.embed_builders.alert_embeds.option_scan import (
    create_scan_embed,
)
from cogs.embed_builders.alert_embeds.psq import (
    create_psq_embed,
)
from cogs.embed_builders.alert_embeds.sentiment_feeds import (
    create_news_scan_embed,
    create_reddit_scan_embed,
    create_media_sentiment_embed,
)
from cogs.embed_builders.alert_embeds.polymarket import (
    create_polymarket_list_embed,
    create_polymarket_status_embed,
    create_polymarket_prob_shift_embed,
)
from cogs.embed_builders.alert_embeds.quote_and_risk_alerts import (
    create_quote_embed,
    create_profit_lock_alert_embed,
    create_gamma_fragility_embed,
    create_ditm_transition_alert_embed,
    create_vtr_settlement_notice_embed,
    create_scenario_alert_embed,
    create_margin_api_alert_embed,
    create_vix_tail_risk_embed,
)
from cogs.embed_builders.alert_embeds.market_signal_alerts import (
    create_wti_alert_embed,
    create_price_volume_alert_embed,
)

__all__ = [
    "create_scan_embed",
    "create_psq_embed",
    "create_news_scan_embed",
    "create_reddit_scan_embed",
    "create_media_sentiment_embed",
    "create_polymarket_list_embed",
    "create_polymarket_status_embed",
    "create_polymarket_prob_shift_embed",
    "create_quote_embed",
    "create_profit_lock_alert_embed",
    "create_gamma_fragility_embed",
    "create_ditm_transition_alert_embed",
    "create_vtr_settlement_notice_embed",
    "create_scenario_alert_embed",
    "create_margin_api_alert_embed",
    "create_vix_tail_risk_embed",
    "create_wti_alert_embed",
    "create_price_volume_alert_embed",
]
