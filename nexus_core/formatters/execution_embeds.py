from typing import Any, Dict
from models.execution import ExecutionDecision


def build_execution_embed(decision: ExecutionDecision) -> dict:
    """
    將 ExecutionDecision Pydantic 模型轉換為 Discord Embed 字典格式。

    視覺規範：
    - SHIELD: 紅色 (0xFF0000)，代表高風險環境下的防禦網。
    - SPEAR: 綠色 (0x00FF00)，代表高勝率環境下的精準打擊。
    - STANDBY: 灰色 (0x808080)，代表中性觀望。

    所有標籤與內容皆採用繁體中文 (zh-TW)。
    """
    # 顏色配置
    COLORS = {"SHIELD": 0xFF0000, "SPEAR": 0x00FF00, "STANDBY": 0x808080}

    # 基礎 Embed 結構
    embed: Dict[str, Any] = {
        "title": f"🛡️ Nexus Seeker 執行決策：{decision.decision_type}",
        "description": f"**[觸發原因]**\n{decision.trigger_reason}",
        "color": COLORS.get(decision.decision_type, 0x000000),
        "fields": [],
        "footer": {
            "text": "Nexus Seeker Execution Decision Matrix v1.0",
            "icon_url": "https://raw.githubusercontent.com/cosmo-chang/nexus-seeker/main/assets/hero.png",
        },
    }

    # 1. 加入宏觀狀態欄位
    embed["fields"].append(
        {
            "name": "📊 [Macro Status]",
            "value": (
                f"決策類型：`{decision.decision_type}`\n"
                f"路由模組：`{'Module A (The Shield)' if decision.decision_type == 'SHIELD' else 'Module B (The Spear)' if decision.decision_type == 'SPEAR' else 'Idle (Standby)'}`"
            ),
            "inline": False,
        }
    )

    # 2. 加入特定模組參數
    if decision.decision_type == "SHIELD" and decision.grid_params:
        params = decision.grid_params
        embed["fields"].append(
            {
                "name": "🕸️ [Grid Spacing]",
                "value": (
                    f"基準價格：`${params.base_price:,.2f}`\n"
                    f"動態步長：`{params.dynamic_step_percent*100:.2f}%`"
                ),
                "inline": True,
            }
        )

    elif decision.decision_type == "SPEAR" and decision.position_sizing:
        sizing = decision.position_sizing
        embed["fields"].append(
            {
                "name": "⚖️ [Position Sizing / Risk Caps]",
                "value": (
                    f"凱利建議：`{sizing.kelly_percentage*100:.2f}%`\n"
                    f"最大分配：`${sizing.max_capital_allocation:,.0f}`\n"
                    f"Theta 限制：`-${sizing.max_theta_exposure:,.2f}/日`"
                ),
                "inline": True,
            }
        )

    # 3. 加入出場策略
    if decision.exit_strategy:
        exit_strat = decision.exit_strategy
        embed["fields"].append(
            {
                "name": "🚪 [Exit Strategy]",
                "value": (
                    f"移動止損：`{'已啟用' if exit_strat.trailing_stop_active else '未啟用'}`\n"
                    f"觸發價格：`${exit_strat.trigger_price:,.2f}`\n"
                    f"條件類型：`{exit_strat.condition_type}`"
                ),
                "inline": True,
            }
        )

    return embed
