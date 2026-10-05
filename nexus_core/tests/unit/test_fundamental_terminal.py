"""單元測試：基本面互動診斷終端與 Embed 構建器 (fundamental_terminal.py)。"""

from __future__ import annotations

import discord
import pytest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders.fundamental_embeds import (
    build_fa_terminal_embed,
    build_governance_flag_embed,
)
from cogs.fundamental_terminal import (
    FaSectionRegistry,
    FundamentalTerminalCog,
    GovernanceGateSection,
    MacroLiquiditySection,
)
from market_analysis.fundamental_pipeline.models import (
    GovernanceFlagRecord,
    LiquidityReading,
)


def test_build_fa_terminal_embed() -> None:
    """測試全景診斷 Embed 構建與 NexusEmbed 規格相容性。"""
    sections = [
        ("🌊 宏觀流動性體制", "• 當前體制: **EASY** (NFCI: -0.58)"),
        ("🚨 治理與重大事件監控", "• 狀態: 🟢 治理無重大異常"),
    ]

    embed = build_fa_terminal_embed(
        symbol="TSLA",
        company_name="Tesla, Inc.",
        sections=sections,
    )

    assert isinstance(embed, NexusEmbed)
    assert "TSLA" in (embed.title or "")
    assert "Tesla, Inc." in (embed.description or "")
    assert len(embed.fields) == 2
    assert embed.fields[0].name == "🌊 宏觀流動性體制"


def test_build_governance_flag_embed_critical_and_high() -> None:
    """測試 CRITICAL 與 HIGH 等級治理警報 Embed 之調色盤與欄位。"""
    flag_critical = GovernanceFlagRecord(
        symbol="SMCI",
        source_accession="ACC-402",
        flag_kind="ITEM_4_02_RESTATEMENT",
        severity="CRITICAL",
        detail_json='{"snippet": "Non-reliance on financial statements."}',
        expires_at="2026-11-05 00:00:00",
    )
    embed_crit = build_governance_flag_embed("SMCI", flag_critical)
    assert isinstance(embed_crit, NexusEmbed)
    assert "CRITICAL" in str(embed_crit.fields[0].value)
    assert embed_crit.color.value == 0xE74C3C

    flag_high = GovernanceFlagRecord(
        symbol="ABC",
        source_accession="ACC-502",
        flag_kind="ITEM_5_02_OFFICER_DEPARTURE",
        severity="HIGH",
        detail_json='{"snippet": "Resignation of Chief Executive Officer."}',
        expires_at="2026-11-05 00:00:00",
    )
    embed_high = build_governance_flag_embed("ABC", flag_high)
    assert embed_high.color.value == 0xF39C12


@pytest.mark.asyncio
async def test_fa_section_registry_extensibility() -> None:
    """測試 FaSectionRegistry 註冊機制與容錯能力。"""
    registry = FaSectionRegistry()

    class MockWorkingSection:
        @property
        def section_id(self) -> str:
            return "working"

        async def render(self, symbol: str) -> tuple[str, str]:
            return "✅ 測試區塊", f"{symbol} 數據正常"

    class MockFailingSection:
        @property
        def section_id(self) -> str:
            return "failing"

        async def render(self, symbol: str) -> tuple[str, str]:
            raise RuntimeError("模擬異常")

    registry.register(MockWorkingSection())
    registry.register(MockFailingSection())

    rendered = await registry.render_all("AAPL")
    assert len(rendered) == 2
    assert rendered[0] == ("✅ 測試區塊", "AAPL 數據正常")
    assert rendered[1] == ("⚠️ failing", "資料載入異常")


@pytest.mark.asyncio
async def test_macro_liquidity_section_render() -> None:
    """測試 MacroLiquiditySection 正確格式化讀數。"""
    sec = MacroLiquiditySection()
    from datetime import date

    reading = LiquidityReading(
        trading_date=date(2026, 10, 5),
        nfci=-0.52,
        anfci=-0.48,
        net_liquidity_bn=6250.0,
        net_liquidity_chg_13w_pct=2.45,
        reserves_chg_13w_pct=1.1,
        us10y=4.12,
        regime="EASY",
        equity_risk_premium=0.0398,
    )

    with patch(
        "cogs.fundamental_terminal.get_latest_liquidity_regime", return_value=reading
    ):
        header, body = await sec.render("SPY")
        assert "EASY" in body
        assert "-0.52" in body
        assert "+2.5%" in body
        assert "3.98%" in body


@pytest.mark.asyncio
async def test_governance_gate_section_render() -> None:
    """測試 GovernanceGateSection 渲染治理狀態與內部人行為。"""
    sec = GovernanceGateSection()

    with (
        patch("cogs.fundamental_terminal.get_active_governance_flags", return_value=[]),
        patch("cogs.fundamental_terminal.get_insider_transactions", return_value=[]),
    ):
        header, body = await sec.render("TSLA")
        assert "治理狀態: 🟢 正常無重大異常" in body
        assert "內部人行為 (30D): ⚪ **NEUTRAL**" in body


@pytest.mark.asyncio
async def test_fundamental_terminal_cog_command() -> None:
    """測試 /fa 斜線指令之 defer、ephemeral 與 followup 流程。"""
    bot = MagicMock()
    cog = FundamentalTerminalCog(bot)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    callback_fn: Any = cog.fa_command.callback
    await callback_fn(cog, interaction, "TSLA")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    assert isinstance(kwargs.get("embed"), NexusEmbed)
