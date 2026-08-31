"""掃描與情緒類 Embed 建構函式。

依領域拆分為：
- sentiment_scan.py：期權情緒掃描報告（create_sentiment_scan_embed）
- macro_and_fomc.py：巨觀環境掃描與 FOMC 逃頂窗口
- risk_stress_test.py：GTC 掛單現金赤字壓力測試
- covered_call.py：Covered Call 解鎖建議與防禦性收租篩選
- earnings_and_sector_flow.py：財報與產業資金流輪動報告
- radar_panel.py：Unified Radar Panel 狀態顯示
"""

from cogs.embed_builders.scan_embeds.sentiment_scan import (
    _format_uoa_field,
    create_sentiment_scan_embed,
)
from cogs.embed_builders.scan_embeds.macro_and_fomc import (
    create_macro_scan_embed,
    create_fomc_escape_window_embed,
)
from cogs.embed_builders.scan_embeds.risk_stress_test import (
    create_stress_test_embed,
)
from cogs.embed_builders.scan_embeds.covered_call import (
    create_covered_call_unlock_embed,
    create_cc_recovery_embed,
)
from cogs.embed_builders.scan_embeds.earnings_and_sector_flow import (
    create_earnings_report_embed,
    create_sector_flow_report_embed,
)
from cogs.embed_builders.scan_embeds.radar_panel import (
    build_unified_radar_panel_embed,
)

__all__ = [
    "_format_uoa_field",
    "create_sentiment_scan_embed",
    "create_macro_scan_embed",
    "create_fomc_escape_window_embed",
    "create_stress_test_embed",
    "create_covered_call_unlock_embed",
    "create_cc_recovery_embed",
    "create_earnings_report_embed",
    "create_sector_flow_report_embed",
    "build_unified_radar_panel_embed",
]
