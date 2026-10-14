"""/iv_scan 只掃描、只回覆呼叫者自己的觀察清單。

過去它掃描全部使用者並把結果私訊給其他人，且沒有管理員檢查——任何人執行一次就會對
全體使用者發送不受通知開關控制的 DM。
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_iv_scan_only_scans_and_replies_to_invoker() -> None:
    from cogs.trading.scanner_commands import ScannerCommandsCog

    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    with patch("services.trading_service.TradingService"):
        cog = ScannerCommandsCog(bot)
    cog.trading_service = MagicMock()
    cog.trading_service.run_iv_opportunity_scan = AsyncMock(
        return_value=[{"symbol": "AAPL"}]
    )

    interaction: Any = MagicMock()
    interaction.user.id = 1
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    watchlist = [(1, "AAPL", None), (2, "TSLA", None), (3, "NVDA", None)]
    with (
        patch("database.get_all_watchlist", return_value=watchlist),
        patch("cogs.embed_builder.create_volatility_embed", return_value=MagicMock()),
    ):
        callback: Any = ScannerCommandsCog.iv_scan.callback
        await callback(cog, interaction)

    cog.trading_service.run_iv_opportunity_scan.assert_awaited_once_with(["AAPL"], 1)
    bot.queue_dm.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()


def _make_decision(kind: str) -> Any:
    from models.execution import (
        ExecutionDecision,
        ExitStrategy,
        GridParameters,
        PositionSizing,
    )

    if kind == "SHIELD":
        return ExecutionDecision(
            decision_type="SHIELD",
            trigger_reason="高波動防禦",
            grid_params=GridParameters(base_price=100.0, dynamic_step_percent=0.02),
            position_sizing=None,
            exit_strategy=ExitStrategy(
                trailing_stop_active=True,
                trigger_price=95.0,
                condition_type="MA20_BREAK",
            ),
        )
    if kind == "SPEAR":
        return ExecutionDecision(
            decision_type="SPEAR",
            trigger_reason="低波動進攻",
            grid_params=None,
            position_sizing=PositionSizing(
                kelly_percentage=0.1,
                max_capital_allocation=5000.0,
                max_theta_exposure=12.5,
            ),
            exit_strategy=None,
        )
    return ExecutionDecision(
        decision_type="STANDBY",
        trigger_reason="測試觀望",
        grid_params=None,
        position_sizing=None,
        exit_strategy=None,
    )


def test_execution_embed_for_dm_is_nexus_embed() -> None:
    """NRO 掃描過去把 dict 直接傳給 queue_dm，入列時 `.to_dict()` 拋錯並中斷整輪
    掃描推播；私訊路徑必須拿到 NexusEmbed（色票、footer 契約一致）。"""
    from cogs.embed_builders import CURATED_PALETTE, NexusEmbed
    from cogs.embed_builders.alert_embeds.execution_decision import (
        create_execution_decision_embed,
    )

    standby = create_execution_decision_embed(_make_decision("STANDBY"))
    assert isinstance(standby, NexusEmbed)
    assert standby.color is not None and standby.color.value in CURATED_PALETTE
    assert "STANDBY" in (standby.title or "")
    assert "Nexus" not in (standby.title or "")

    for kind in ("SHIELD", "SPEAR", "STANDBY"):
        embed = create_execution_decision_embed(_make_decision(kind))
        assert isinstance(embed, NexusEmbed)
        assert embed.color is not None and embed.color.value in CURATED_PALETTE
        d = embed.to_dict()
        names = [f["name"] for f in d["fields"]]
        assert "⚡ 觸發原因" in names
        assert all(f["inline"] is False for f in d["fields"])
        assert d["footer"]["text"] == "🌌 Nexus Seeker • 執行決策矩陣 v1.0"
        assert "icon_url" not in d["footer"]

    shield = create_execution_decision_embed(_make_decision("SHIELD")).to_dict()
    blob = "\n".join(f["value"] for f in shield["fields"])
    assert "Module A (The Shield)" in blob and "$100.00" in blob and "2.00%" in blob
    assert "MA20_BREAK" in blob and "已啟用" in blob and "├─" in blob and "└─" in blob
    spear = create_execution_decision_embed(_make_decision("SPEAR")).to_dict()
    blob = "\n".join(f["value"] for f in spear["fields"])
    assert "Module B (The Spear)" in blob and "10.00%" in blob and "$5,000" in blob
    assert "-$12.50/日" in blob
