"""單元測試：基本面互動診斷終端與 Embed 構建器 (fundamental_terminal.py)。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders.fundamental_embeds import (
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
from market_analysis.fundamental_pipeline.supply_chain_map import get_links_for_symbol
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    FilingCursorRecord,
    GovernanceFlagRecord,
    GuidanceExtraction,
    GuidanceExtractionDTO,
    LiquidityReading,
    MarginGuidance,
    ToneMetric,
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


_SYNCED_CURSOR = FilingCursorRecord(
    symbol="TSLA",
    cik="0001318605",
    last_accepted_at="2026-10-05T16:30:00-04:00",
    last_accession="0001318605-26-000001",
    updated_at="2026-10-05 21:00:00",
)


@pytest.mark.asyncio
async def test_governance_gate_section_never_synced_is_not_shown_as_clean() -> None:
    """從未同步（無游標）時不得顯示「正常」或 NEUTRAL，必須明示尚無資料。"""
    sec = GovernanceGateSection()

    with (
        patch("cogs.fundamental_terminal.get_sec_filing_cursor", return_value=None),
        patch(
            "cogs.fundamental_terminal.get_active_governance_flags", return_value=[]
        ) as mock_flags,
        patch(
            "cogs.fundamental_terminal.get_insider_transactions", return_value=[]
        ) as mock_txs,
    ):
        _, body = await sec.render("TSLA")

    assert "尚無申報同步資料" in body
    assert "待排程首次同步" in body
    assert "尚未排程" not in body
    assert "🟢" not in body
    assert "正常" not in body
    assert "NEUTRAL" not in body
    mock_flags.assert_not_called()
    mock_txs.assert_not_called()


@pytest.mark.asyncio
async def test_governance_gate_section_render() -> None:
    """已同步且乾淨時才顯示 🟢，並與內部人行為一併渲染。"""
    sec = GovernanceGateSection()

    with (
        patch(
            "cogs.fundamental_terminal.get_sec_filing_cursor",
            return_value=_SYNCED_CURSOR,
        ),
        patch("cogs.fundamental_terminal.get_active_governance_flags", return_value=[]),
        patch("cogs.fundamental_terminal.get_insider_transactions", return_value=[]),
    ):
        header, body = await sec.render("TSLA")
        assert "治理與重大事件監控" in header
        assert "治理狀態: 🟢 已同步，最近未觸發 4.02 / 5.02 警訊" in body
        assert "2026-10-05 21:00" in body
        assert "內部人行為 (30D): ⚪ **NEUTRAL**" in body


@pytest.mark.asyncio
async def test_governance_gate_section_review_flag_shows_pending_review() -> None:
    """5.02 降級後的 REVIEW 旗標顯示為待人工複核（🟡），而非風控審查。"""
    sec = GovernanceGateSection()
    review_flag = GovernanceFlagRecord(
        symbol="TSLA",
        source_accession="ACC-1",
        flag_kind="ITEM_5_02_OFFICER_CHANGE",
        severity="REVIEW",
        expires_at="2099-01-01 00:00:00",
    )
    with (
        patch(
            "cogs.fundamental_terminal.get_sec_filing_cursor",
            return_value=_SYNCED_CURSOR,
        ),
        patch(
            "cogs.fundamental_terminal.get_active_governance_flags",
            return_value=[review_flag],
        ),
        patch("cogs.fundamental_terminal.get_insider_transactions", return_value=[]),
    ):
        _, body = await sec.render("TSLA")
    assert "🟡" in body
    assert "待人工複核" in body
    assert "觸發風控審查" not in body


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


def _guidance_obj(
    period: str,
    *,
    revenue: float | None = None,
    eps: float | None = None,
    target: str | None = None,
    backlog: int = 0,
    margins: list[MarginGuidance] | None = None,
) -> GuidanceExtraction:
    return GuidanceExtraction(
        symbol="AAPL",
        fiscal_period=period,
        guidance_target_period=target,
        revenue_guidance_midpoint_usd=revenue,
        eps_guidance_midpoint_usd=eps,
        margin_guidance=margins or [],
        backlog_tone=ToneMetric(score=backlog, quote_snippet=""),
        pricing_power_tone=ToneMetric(score=1, quote_snippet=""),
        supply_chain_tone=ToneMetric(score=0, quote_snippet=""),
        defensive_posture_tone=ToneMetric(score=0, quote_snippet=""),
        reasoning_traditional_chinese="測試",
    )


def _guidance_dto(
    obj: GuidanceExtraction, period: str, accession: str, tone_delta: float
) -> GuidanceExtractionDTO:
    return GuidanceExtractionDTO(
        symbol="AAPL",
        fiscal_period=period,
        source_accession=accession,
        model_version="gpt-4o",
        confidence_score=0.57,
        tone_delta_score=tone_delta,
        data_json=obj.model_dump_json(),
    )


_SNAPSHOTS = [
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


def _patch_pr3(
    surprise: EarningsSurpriseDTO | None,
    guidance: GuidanceExtractionDTO | None,
    snapshots: list[EPSEstimateSnapshotRecord],
    prior: GuidanceExtractionDTO | None = None,
) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "cogs.fundamental_terminal.get_latest_earnings_surprise",
            return_value=surprise,
        )
    )
    stack.enter_context(
        patch(
            "cogs.fundamental_terminal.get_latest_guidance_extraction",
            return_value=guidance,
        )
    )
    stack.enter_context(
        patch(
            "cogs.fundamental_terminal.get_eps_estimate_snapshots",
            return_value=snapshots,
        )
    )
    stack.enter_context(
        patch(
            "cogs.fundamental_terminal.get_prior_guidance_extraction",
            return_value=prior,
        )
    )
    return stack


@pytest.mark.asyncio
async def test_earnings_surprise_section_no_data_is_explicit() -> None:
    """完全沒有資料時明示待財報 8-K 觸發，不得出現看似正常的佔位內容。"""
    sec = EarningsSurpriseSection()
    with _patch_pr3(None, None, []):
        header, body = await sec.render("AAPL")
    assert "尚無財報預期差資料" in body
    assert "待下一份財報 8-K 觸發" in body
    assert "尚未排程" not in body
    assert "待下輪" not in body and "追蹤中" not in body
    assert "PEAD" not in header and "Surprise" not in header


@pytest.mark.asyncio
async def test_earnings_surprise_section_render() -> None:
    """渲染預期差、絕對語意分數與 delta 分開呈現、中文 horizon 標籤。"""
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
    curr = _guidance_obj("2026-Q3", backlog=2)  # tone (2+1)/4/2*100 = 37.5
    prior = _guidance_obj("2026-Q2", backlog=0)  # tone 12.5
    with _patch_pr3(
        surprise,
        _guidance_dto(curr, "2026-Q3", "ACC-Q3", 25.0),
        _SNAPSHOTS,
        prior=_guidance_dto(prior, "2026-Q2", "ACC-Q2", 0.0),
    ):
        header, body = await sec.render("AAPL")
    assert header == "📊 業績預期差與財報後漂移"
    assert "2026-Q3" in body
    assert "+15.2" in body
    assert "+3.7%" in body
    assert "+1.2%" in body
    # 絕對分數與邊際變化分開呈現
    assert "語意分數 **+37.5**" in body
    assert "較前期（2026-Q2）`+25.0`" in body
    assert "本季: `$1.45`" in body
    assert "下季: `$1.60`" in body
    assert "0Q" not in body and "+1Q" not in body
    assert "小基數" not in body


@pytest.mark.asyncio
async def test_earnings_surprise_section_without_prior_shows_no_delta() -> None:
    """沒有前期指引時不得把當期分數顯示成邊際變化。"""
    sec = EarningsSurpriseSection()
    curr = _guidance_obj("2026-Q3", backlog=2)
    with _patch_pr3(None, _guidance_dto(curr, "2026-Q3", "ACC-Q3", 0.0), []):
        _, body = await sec.render("AAPL")
    assert "語意分數 **+37.5**" in body
    assert "無前期指引可比" in body
    assert "較前期" not in body


@pytest.mark.asyncio
async def test_earnings_surprise_section_guidance_verdict_uses_prior() -> None:
    """/fa 讀取前一期指引一起比較：同目標期別數值上修 → 調升（繁中呈現）。"""
    sec = EarningsSurpriseSection()
    margins = [MarginGuidance(metric_name="Gross Margin", direction="EXPANDING")]
    curr = _guidance_obj(
        "2026-Q3", revenue=95e9, eps=7.2, target="FY2026", margins=margins
    )
    prior = _guidance_obj("2026-Q2", revenue=90e9, eps=6.8, target="FY2026")
    prior_lookup = MagicMock(return_value=_guidance_dto(prior, "2026-Q2", "ACC-Q2", 0))
    with _patch_pr3(None, _guidance_dto(curr, "2026-Q3", "ACC-Q3", 0.0), []):
        with patch(
            "cogs.fundamental_terminal.get_prior_guidance_extraction", new=prior_lookup
        ):
            _, body = await sec.render("AAPL")
    prior_lookup.assert_called_once_with("AAPL", "2026-Q3", "ACC-Q3")
    assert "前瞻指引方向: **調升**（利潤率擴張）" in body
    assert "RAISED" not in body and "Expanding" not in body


@pytest.mark.asyncio
async def test_earnings_surprise_section_guidance_without_numbers_is_unknown() -> None:
    """沒有可比較的數值指引時顯示「無法判定」並說明原因，不以語意態度推論。"""
    sec = EarningsSurpriseSection()
    curr = _guidance_obj("2026-Q3", backlog=2)
    with _patch_pr3(None, _guidance_dto(curr, "2026-Q3", "ACC-Q3", 0.0), []):
        _, body = await sec.render("AAPL")
    assert "前瞻指引方向: **無法判定**" in body
    assert "無同期別前後數值指引" in body


@pytest.mark.asyncio
async def test_earnings_surprise_section_small_base_and_pending() -> None:
    """共識 EPS 絕對值 < 0.05 時標註小基數；PENDING 顯示待實際值公布。"""
    sec = EarningsSurpriseSection()
    small = EarningsSurpriseDTO(
        symbol="RKLB",
        fiscal_period="2026-Q2",
        actual_eps=0.05,
        consensus_eps=-0.02,
        eps_surprise_pct=0.5,
        composite_score=100.0,
    )
    with _patch_pr3(small, None, []):
        _, body = await sec.render("RKLB")
    assert "小基數" in body
    assert "尚無指引擷取資料（待下一份財報 8-K 觸發）" in body
    assert "尚無共識快照資料（待 NYSE 交易日 19:00 ET 快照排程）" in body

    pending = EarningsSurpriseDTO(
        symbol="RKLB", fiscal_period="2026-Q3", consensus_eps=0.10, status="PENDING"
    )
    with _patch_pr3(pending, None, []):
        _, body = await sec.render("RKLB")
    assert "⏳ 待實際值公布（共識 EPS: `$0.10`）" in body


def _cc_log(
    link_key: str,
    period: str,
    verdict: str,
    driver: float | None = None,
    follower: float | None = None,
    divergence: float | None = None,
    direction: str | None = None,
    members_json: str = "{}",
    link_type: str = "NOWCAST",
) -> ChannelCheckLogRecord:
    return ChannelCheckLogRecord(
        link_key=link_key,
        as_of_period=period,
        link_type=link_type,  # type: ignore[arg-type]
        experimental=False,
        driver_growth=driver,
        follower_growth=follower,
        divergence_pp=divergence,
        nowcast_direction=direction,  # type: ignore[arg-type]
        nowcast_hit=None,
        correlation=None,
        verdict=verdict,  # type: ignore[arg-type]
        members_json=members_json,
    )


@pytest.mark.asyncio
async def test_channel_check_section_no_data_is_explicit() -> None:
    """排程尚未寫入任何紀錄時明示「尚無資料」，不得顯示看似判定結果的資料不足 / INSUFFICIENT。"""
    sec = ChannelCheckSection()
    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol", return_value=[]
    ):
        header, body = await sec.render("TSLA")
    assert header == "🔗 實體產業鏈交叉驗證"
    assert "尚無產業鏈檢驗資料（待每日 18:00 ET 排程寫入）" in body
    assert "INSUFFICIENT" not in body and "資料不足" not in body
    assert "自主移動載具台灣供應鏈預測" in body


@pytest.mark.asyncio
async def test_channel_check_section_render_with_data_in_chinese() -> None:
    """渲染判定結果：中文鏈名、中文判定與類型，不顯示英文狀態碼或 link_key。"""
    sec = ChannelCheckSection()
    assert sec.section_id == "channel_checks"
    record = _cc_log(
        "ADV_AUTO_MOBILITY_TW_NOWCAST",
        "2026-Q3",
        "CONFIRM",
        driver=8.1,
        follower=6.5,
        divergence=-1.6,
        direction="NOWCAST_UP",
    )
    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol", return_value=[record]
    ):
        _, body = await sec.render("TSLA")
    assert "自主移動載具台灣供應鏈預測（高頻臨近預測）2026-Q3: 🟢 **共振確認**" in body
    assert "驅動 +8.1%" in body
    assert "預測向上" in body
    assert "尚無檢驗紀錄" in body  # TSLA 其他鏈尚無紀錄
    for english in (
        "CONFIRM",
        "DIVERGE",
        "INSUFFICIENT",
        "NOWCAST",
        "CAUSAL",
        "ADV_AUTO",
    ):
        assert english not in body


@pytest.mark.asyncio
async def test_channel_check_section_prefers_latest_substantive_verdict() -> None:
    """同鏈多期：較新一期資料不足時，顯示最新的實質判定；全為資料不足才顯示原因。"""
    sec = ChannelCheckSection()
    newer_insufficient = _cc_log(
        "ADV_AUTO_MOBILITY_TW_NOWCAST",
        "2026-Q3",
        "INSUFFICIENT",
        members_json='{"summary_text": "數據不充分：驅動端台股覆蓋 0/5 低於門檻"}',
    )
    older = _cc_log("ADV_AUTO_MOBILITY_TW_NOWCAST", "2026-Q2", "CONFIRM", driver=12.0)
    fleet_insufficient = _cc_log(
        "ADV_AUTO_FLEET_DEMAND",
        "2026-Q3",
        "INSUFFICIENT",
        link_type="CAUSAL",
        members_json='{"summary_text": "數據不充分 (驅動端或跟隨端增長率缺失)：跟隨端美股覆蓋 1/4 低於門檻"}',
    )
    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol",
        return_value=[newer_insufficient, fleet_insufficient, older],
    ):
        _, body = await sec.render("TSLA")
    assert "2026-Q2: 🟢 **共振確認**（驅動 +12.0%）" in body
    assert "全美汽車總體景氣需求鏈（因果傳導）2026-Q3: ⚪ **資料不足**" in body
    assert "跟隨端美股覆蓋 1/4 低於門檻" in body


@pytest.mark.asyncio
async def test_channel_check_section_inverse_polarity_label() -> None:
    sec = ChannelCheckSection()
    record = _cc_log(
        "BRAND_RETAIL_INVENTORY",
        "2026-Q2",
        "CONFIRM",
        driver=10.0,
        follower=-8.0,
        link_type="CAUSAL",
    )
    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol", return_value=[record]
    ):
        _, body = await sec.render("WMT")
    assert "驅動（反向） +10.0%" in body


@pytest.mark.asyncio
async def test_channel_check_section_respects_discord_field_limit() -> None:
    sec = ChannelCheckSection()
    long_reason = '{"summary_text": "數據不充分：' + "很長的原因" * 40 + '"}'
    logs = [
        _cc_log(link.link_key, "2026-Q3", "INSUFFICIENT", members_json=long_reason)
        for link in get_links_for_symbol("TSLA")
    ]
    with patch(
        "cogs.fundamental_terminal.get_channel_checks_by_symbol", return_value=logs
    ):
        _, body = await sec.render("TSLA")
    assert len(body) <= 1000


@pytest.mark.asyncio
async def test_channel_check_section_render_unmapped_symbol() -> None:
    """測試未映射標的之提示訊息。"""
    sec = ChannelCheckSection()
    _header, body = await sec.render("UNMAPPED_SYMBOL_XYZ")
    assert "未涵蓋於當前 17 條核心產業鏈矩陣中" in body


# ============================================================================
# PR5：6 個區塊的總長預算、估值 / 動能 / 候選名單呈現
# ============================================================================


def test_fa_embed_trims_longest_fields_instead_of_dropping_sections() -> None:
    """6 個區塊總長超過 5800 時截短最長欄位，所有區塊都保留、短欄位不動。"""
    long_body = "\n".join(f"• 第 {i} 行產業鏈檢驗明細" + "資料" * 20 for i in range(40))
    sections = [
        ("🌊 宏觀流動性體制", "• 當前體制: **EASY**"),
        ("🚨 治理", long_body),
        ("📊 業績預期差", long_body),
        ("💰 內在價值、安全邊際與修正動能", long_body),
        ("📋 次日基本面候選評級", long_body),
        ("🔗 產業鏈交叉驗證", long_body),
    ]
    embed = build_fa_terminal_embed("NVDA", "NVIDIA Corp", sections)
    payload = embed.to_dict()
    fields = payload["fields"]
    assert len(fields) == 6  # 不整欄丟棄
    total = (
        len(payload.get("title") or "")
        + len(payload.get("description") or "")
        + len(payload["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in fields)
    )
    assert total <= 5800
    assert fields[0]["value"].startswith("• 當前體制: **EASY**")
    assert fields[-1]["name"] == "🔗 產業鏈交叉驗證"


def test_fa_registry_has_six_sections_with_channel_check_last() -> None:
    from cogs.fundamental_terminal import fa_section_registry

    ids = [s.section_id for s in fa_section_registry._sections]
    assert len(ids) == 6
    assert ids[-1] == ChannelCheckSection().section_id


@pytest.mark.asyncio
async def test_valuation_section_null_fair_value_and_default_flags() -> None:
    """method = NONE / NULL 公允價值明示無有效估值；預設值旗標逐條標註；動能 None 不顯示 0。"""
    from cogs.fundamental_terminal import ValuationEngineSection
    from market_analysis.fundamental_pipeline.models import (
        FairValueRecord,
        RevisionScoreRecord,
    )

    fv = FairValueRecord(
        symbol="SPY",
        trading_date="2026-10-06",
        dcf_value=None,
        comps_value=None,
        fair_value=None,
        margin_of_safety=None,
        discount_rate=0.0866,
        equity_risk_premium=0.045,
        flags_json='["US10Y_DEFAULT", "FCF_UNAVAILABLE"]',
        method="NONE",
    )
    rev = RevisionScoreRecord(
        symbol="SPY",
        trading_date="2026-10-06",
        score_30d=None,
        breadth_ratio=None,
        is_pead_aligned=False,
        detail_json='{"flags": ["NO_PRIOR_SNAPSHOT", "BREADTH_UNAVAILABLE"]}',
    )
    with (
        patch("cogs.fundamental_terminal.get_latest_fair_value", return_value=fv),
        patch("cogs.fundamental_terminal.get_latest_revision_score", return_value=rev),
    ):
        _, body = await ValuationEngineSection().render("SPY")
    assert "無有效估值" in body
    assert "$0.00" not in body
    assert "4.25%" in body  # 預設 10 年債殖利率被套用時必須呈現
    assert "P/FCF" in body
    assert "無可比財期" in body
    assert "t-30 日" in body
    assert "廣度項不計" in body


@pytest.mark.asyncio
async def test_valuation_section_blended_with_method_label() -> None:
    from cogs.fundamental_terminal import ValuationEngineSection
    from market_analysis.fundamental_pipeline.models import FairValueRecord

    fv = FairValueRecord(
        symbol="MSFT",
        trading_date="2026-10-06",
        dcf_value=600.0,
        comps_value=560.0,
        fair_value=580.0,
        margin_of_safety=0.09,
        discount_rate=0.0866,
        equity_risk_premium=0.0425,
        flags_json='["FAIRLY_VALUED"]',
        method="BLENDED",
        spot_price=527.98,
    )
    with (
        patch("cogs.fundamental_terminal.get_latest_fair_value", return_value=fv),
        patch("cogs.fundamental_terminal.get_latest_revision_score", return_value=None),
    ):
        _, body = await ValuationEngineSection().render("MSFT")
    assert "$580.00" in body
    assert "DCF＋同業倍數各半" in body
    assert "+9.00%" in body
    assert "待 20:00 估值排程" in body


@pytest.mark.asyncio
async def test_watch_candidate_section_shows_list_date() -> None:
    from cogs.fundamental_terminal import WatchCandidateSection
    from market_analysis.fundamental_pipeline.models import WatchCandidateRecord

    cands = [
        WatchCandidateRecord(
            trading_date="2026-10-06",
            symbol="INTC",
            rank=2,
            status="WATCH",
            reasons_json=(
                '{"composite_score": 30.0, "margin_of_safety": 0.4, '
                '"pead_aligned": true, "governance_note": "HIGH 治理旗標生效中：不晉升 CANDIDATE"}'
            ),
        )
    ]
    with patch("cogs.fundamental_terminal.get_watch_candidates", return_value=cands):
        _, body = await WatchCandidateSection().render("INTC")
    assert "名單日 2026-10-06" in body
    assert "觀察" in body
    assert "HIGH 治理旗標" in body
