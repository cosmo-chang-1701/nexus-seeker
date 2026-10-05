"""單元測試：基本面互動診斷終端與 Embed 構建器 (fundamental_terminal.py)。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders.fundamental_embeds import (
    build_channel_check_overview_embed,
    build_fa_terminal_embed,
    build_governance_flag_embed,
)
from cogs.fundamental_terminal import (
    ChannelCheckSection,
    EarningsSurpriseSection,
    FaSectionRegistry,
    FundamentalTerminalCog,
    GovernanceGateSection,
    MacroLiquiditySection,
)
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    ChannelCheckResult,
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    GovernanceFlagRecord,
    GuidanceExtractionDTO,
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
        assert "宏觀流動性體制" in header
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
        assert "治理與重大事件監控" in header
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


@pytest.mark.asyncio
async def test_earnings_surprise_section_render() -> None:
    """測試 EarningsSurpriseSection 渲染預期差、指引與快照。"""
    sec = EarningsSurpriseSection()

    surprise = EarningsSurpriseDTO(
        symbol="AAPL",
        fiscal_period="2026-Q3",
        actual_eps=1.40,
        consensus_eps=1.35,
        eps_surprise_pct=0.037,
        actual_revenue=85000000000.0,
        consensus_revenue=84000000000.0,
        revenue_surprise_pct=0.0119,
        composite_score=15.2,
    )
    guidance = GuidanceExtractionDTO(
        symbol="AAPL",
        fiscal_period="2026-Q3",
        source_accession="ACC-AAPL-202",
        model_version="gpt-4o",
        confidence_score=0.95,
        tone_delta_score=22.5,
        data_json="{}",
    )
    snapshot = [
        EPSEstimateSnapshotRecord(
            symbol="AAPL",
            snapshot_date="2026-10-05",
            horizon="0q",
            source="finnhub",
            eps_mean=1.45,
        ),
        EPSEstimateSnapshotRecord(
            symbol="AAPL",
            snapshot_date="2026-10-05",
            horizon="+1q",
            source="finnhub",
            eps_mean=1.60,
        ),
    ]

    with (
        patch(
            "cogs.fundamental_terminal.get_latest_earnings_surprise",
            return_value=surprise,
        ),
        patch(
            "cogs.fundamental_terminal.get_latest_guidance_extraction",
            return_value=guidance,
        ),
        patch(
            "cogs.fundamental_terminal.get_eps_estimate_snapshots",
            return_value=snapshot,
        ),
    ):
        header, body = await sec.render("AAPL")
        assert "📊 業績預期差與 PEAD 修正" in header
        assert "2026-Q3" in body
        assert "+15.2" in body
        assert "+3.7%" in body
        assert "+1.2%" in body
        assert "+22.5" in body
        assert "0Q: `$1.45`" in body
        assert "+1Q: `$1.60`" in body


@pytest.mark.asyncio
async def test_earnings_surprise_section_render_with_guidance_details() -> None:
    """測試 EarningsSurpriseSection 解析有效 data_json 時展示指引方向與利潤率。"""
    sec = EarningsSurpriseSection()
    from market_analysis.fundamental_pipeline.models import (
        GuidanceExtraction,
        MarginGuidance,
        ToneMetric,
    )

    guidance_obj = GuidanceExtraction(
        symbol="AAPL",
        fiscal_period="2026-Q3",
        revenue_guidance_midpoint_usd=90000000000.0,
        eps_guidance_midpoint_usd=1.65,
        margin_guidance=[
            MarginGuidance(metric_name="Gross Margin", direction="EXPANDING")
        ],
        backlog_tone=ToneMetric(score=1, quote_snippet=""),
        pricing_power_tone=ToneMetric(score=1, quote_snippet=""),
        supply_chain_tone=ToneMetric(score=0, quote_snippet=""),
        defensive_posture_tone=ToneMetric(score=0, quote_snippet=""),
        reasoning_traditional_chinese="指引上修",
    )
    dto = GuidanceExtractionDTO(
        symbol="AAPL",
        fiscal_period="2026-Q3",
        source_accession="ACC-AAPL-202",
        model_version="gpt-4o",
        confidence_score=0.95,
        tone_delta_score=25.0,
        data_json=guidance_obj.model_dump_json(),
    )
    with (
        patch(
            "cogs.fundamental_terminal.get_latest_earnings_surprise",
            return_value=None,
        ),
        patch(
            "cogs.fundamental_terminal.get_latest_guidance_extraction",
            return_value=dto,
        ),
        patch("cogs.fundamental_terminal.get_eps_estimate_snapshots", return_value=[]),
    ):
        _, body = await sec.render("AAPL")
        assert "指引方向: **RAISED**" in body
        assert "利潤率擴張" in body


@pytest.mark.asyncio
async def test_channel_check_section_render_with_data() -> None:
    """測試 ChannelCheckSection 渲染標的關聯產業鏈與日誌狀態。"""
    sec = ChannelCheckSection()
    assert sec.section_id == "channel_checks"

    mock_record = ChannelCheckLogRecord(
        link_key="ADV_AUTO_MOBILITY_TW_NOWCAST",
        as_of_period="2026-09",
        link_type="NOWCAST",
        experimental=False,
        driver_growth=8.1,
        follower_growth=6.5,
        divergence_pp=-1.6,
        nowcast_direction="NOWCAST_UP",
        nowcast_hit=True,
        correlation=0.85,
        verdict="CONFIRM",
        members_json="{}",
    )

    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol",
        return_value=[mock_record],
    ):
        header, body = await sec.render("TSLA")
        assert "🔗 實體產業鏈交叉驗證" in header
        assert "ADV_AUTO_MOBILITY_TW_NOWCAST" in body
        assert "🟢" in body
        assert "CONFIRM" in body
        assert "驅動 +8.1%" in body


@pytest.mark.asyncio
async def test_channel_check_section_render_multiple_periods_retains_latest() -> None:
    """測試當同產業鏈有多期日誌時，正確保留最新一期（首筆）而非被舊期數覆蓋。"""
    sec = ChannelCheckSection()

    record_new = ChannelCheckLogRecord(
        link_key="ADV_AUTO_MOBILITY_TW_NOWCAST",
        as_of_period="2026-Q3",
        link_type="NOWCAST",
        experimental=False,
        driver_growth=12.0,
        follower_growth=10.0,
        divergence_pp=-2.0,
        nowcast_direction="NOWCAST_UP",
        nowcast_hit=True,
        correlation=0.9,
        verdict="CONFIRM",
        members_json="{}",
    )
    record_old = ChannelCheckLogRecord(
        link_key="ADV_AUTO_MOBILITY_TW_NOWCAST",
        as_of_period="2026-Q2",
        link_type="NOWCAST",
        experimental=False,
        driver_growth=4.0,
        follower_growth=3.0,
        divergence_pp=-1.0,
        nowcast_direction="NOWCAST_UP",
        nowcast_hit=True,
        correlation=0.8,
        verdict="CONFIRM",
        members_json="{}",
    )

    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol",
        return_value=[record_new, record_old],
    ):
        _, body = await sec.render("TSLA")
        # 應顯示最新 Q3 之 12.0%，絕不能被 Q2 之 4.0% 覆寫
        assert "驅動 +12.0%" in body
        assert "驅動 +4.0%" not in body


@pytest.mark.asyncio
async def test_channel_check_section_render_unmapped_symbol() -> None:
    """測試未映射標的之提示訊息。"""
    sec = ChannelCheckSection()
    _header, body = await sec.render("UNMAPPED_SYMBOL_XYZ")
    assert "未涵蓋於當前 17 條核心產業鏈矩陣中" in body


def test_build_channel_check_overview_embed() -> None:
    """測試 build_channel_check_overview_embed 全景分組展示。"""
    from market_analysis.fundamental_pipeline.supply_chain_map import (
        LINK_AI_CAPEX,
        LINK_SPACE_CONSTELLATION_LAUNCH,
    )

    r1 = ChannelCheckResult(
        link_key="AI_CAPEX",
        title=LINK_AI_CAPEX.title,
        link_type="CAUSAL",
        experimental=False,
        as_of_period="2026-Q2",
        driver_growth=25.0,
        follower_growth=22.0,
        divergence_pp=-3.0,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict="CONFIRM",
        summary_text="傳導共振確認",
        members={"pillar": "MACRO_CORE"},
    )
    r2 = ChannelCheckResult(
        link_key="SPACE_CONSTELLATION_LAUNCH",
        title=LINK_SPACE_CONSTELLATION_LAUNCH.title,
        link_type="CAUSAL",
        experimental=False,
        as_of_period="2026-Q2",
        driver_growth=30.0,
        follower_growth=10.0,
        divergence_pp=-20.0,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict="CONFIRM",
        summary_text="傳導共振確認",
        members={"pillar": "SPACE_DEFENSE"},
    )

    embed = build_channel_check_overview_embed([r1, r2], as_of_period="2026-Q2")
    assert isinstance(embed, NexusEmbed)
    assert "2026-Q2" in (embed.title or "")
    assert len(embed.fields) >= 2
