"""單元測試：/fa 嵌入視窗的 NexusEmbed 版面規範（ansi 區塊、樹狀子項、行邊界截斷、降級標題）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cogs import fundamental_terminal as ft
from cogs.embed_builders.fundamental_embeds import (
    ansi_bold,
    build_fa_terminal_embed,
    build_governance_flag_embed,
)
from cogs.fundamental_terminal import (
    FA_SECTION_ERROR_TEXT,
    FaSectionRegistry,
    FundamentalTerminalCog,
    ValuationEngineSection,
    WatchCandidateSection,
)
from market_analysis.fundamental_pipeline.models import (
    FairValueRecord,
    GovernanceFlagRecord,
    RevisionScoreRecord,
    WatchCandidateRecord,
)

_FENCE_OPEN = "```ansi\n"
_FENCE_CLOSE = "\n```"
_TITLE_SUFFIX = " [部分區塊載入失敗]"


def _six_sections(body: str) -> list[tuple[str, str]]:
    return [(f"📌 區塊 {i}", body) for i in range(6)]


def _total_len(payload: dict[str, Any]) -> int:
    return (
        len(payload.get("title") or "")
        + len(payload.get("description") or "")
        + len(payload["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in payload["fields"])
    )


def test_ansi_bold_uses_plain_bold_escape() -> None:
    assert ansi_bold("EASY") == "\u001b[1mEASY\u001b[0m"


def test_every_field_is_wrapped_in_ansi_block() -> None:
    sections = [
        ("🌊 A", f"• 體制: {ansi_bold('EASY')}"),
        ("🚨 B", ""),
        ("📊 C", "• 第一行\n• 第二行"),
    ]
    payload = build_fa_terminal_embed("TSLA", "Tesla", sections).to_dict()
    for field in payload["fields"]:
        assert field["value"].startswith(_FENCE_OPEN)
        assert field["value"].endswith(_FENCE_CLOSE)
    assert payload["fields"][0]["value"] == (
        "```ansi\n• 體制: \u001b[1mEASY\u001b[0m\n```"
    )
    assert payload["fields"][1]["value"] == "```ansi\n無資料\n```"
    assert "​" not in json.dumps(payload, ensure_ascii=False)


def test_title_and_description_follow_layout_rules() -> None:
    embed = build_fa_terminal_embed("tsla", "Tesla, Inc.", [("🌊 A", "• x")])
    assert embed.title == "🔬 基本面事件與全景估值雷達 | TSLA"
    assert "NEXUS" not in (embed.title or "").upper()
    description = embed.description or ""
    assert description.startswith("標的資產: Tesla, Inc. ｜ 查詢時間: ")
    assert description.endswith(" ET")
    assert "資料時效" not in description

    degraded = build_fa_terminal_embed(
        "TSLA", "Tesla", [("🌊 A", "• x")], degraded=True
    )
    assert (degraded.title or "").endswith(_TITLE_SUFFIX)


def test_oversize_input_truncates_by_line_and_keeps_fences() -> None:
    body = "\n".join(f"• 第 {i} 行" + "資料" * 100 for i in range(10))
    assert len(body) > 1500
    payload = build_fa_terminal_embed("NVDA", "NVIDIA", _six_sections(body)).to_dict()
    fields = payload["fields"]
    assert len(fields) == 6
    assert _total_len(payload) <= 5750
    for field in fields:
        value = field["value"]
        assert len(value) <= 1024
        assert value.startswith(_FENCE_OPEN) and value.endswith(_FENCE_CLOSE)
        assert value.count("```") == 2
        assert re.search(r"…（餘 \d+ 行略）\n```$", value)
        # 只丟整行：保留下來的行必為原始行的完整內容
        for line in value[len(_FENCE_OPEN) : -len(_FENCE_CLOSE)].split("\n")[:-1]:
            assert line in body.split("\n")


def test_single_overlong_line_is_cut_without_ansi_residue() -> None:
    body = ansi_bold("很長" * 800)
    payload = build_fa_terminal_embed("NVDA", "NVIDIA", [("🌊 A", body)]).to_dict()
    value = payload["fields"][0]["value"]
    assert len(value) <= 1024
    assert value.startswith(_FENCE_OPEN) and value.endswith(_FENCE_CLOSE)
    assert "\u001b" not in value
    assert value.count("```") == 2


def test_short_fields_are_not_touched_when_others_are_trimmed() -> None:
    long_body = "\n".join(f"• 第 {i} 行" + "資料" * 100 for i in range(10))
    sections = [("🌊 短", "• 短內容")] + [(f"📌 長 {i}", long_body) for i in range(5)]
    payload = build_fa_terminal_embed("NVDA", "NVIDIA", sections).to_dict()
    assert payload["fields"][0]["value"] == "```ansi\n• 短內容\n```"
    assert len(payload["fields"]) == 6
    assert _total_len(payload) <= 5750


@pytest.mark.asyncio
async def test_registry_error_path_uses_chinese_header() -> None:
    registry = FaSectionRegistry()
    registry.register(ValuationEngineSection())
    with patch(
        "cogs.fundamental_terminal.get_latest_fair_value",
        side_effect=RuntimeError("boom"),
    ):
        rendered = await registry.render_all("AAPL")
    assert rendered == [("⚠️ 💰 內在價值、安全邊際與修正動能", FA_SECTION_ERROR_TEXT)]


def _interaction() -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _run_fa(rendered: list[tuple[str, str]]) -> Any:
    cog = FundamentalTerminalCog(MagicMock())
    interaction = _interaction()
    callback_fn: Any = cog.fa_command.callback
    with (
        patch.object(
            ft.fa_section_registry,
            "render_all",
            new=AsyncMock(return_value=rendered),
        ),
        patch(
            "services.market_data_service.fundamentals.get_company_profile",
            new=AsyncMock(return_value={"name": "Tesla, Inc."}),
        ),
    ):
        await callback_fn(cog, interaction, "TSLA")
    _, kwargs = interaction.followup.send.call_args
    return kwargs["embed"]


@pytest.mark.asyncio
async def test_cog_marks_degraded_title_when_section_failed() -> None:
    embed = await _run_fa([("🌊 A", "• ok"), ("⚠️ 💰 內在價值", FA_SECTION_ERROR_TEXT)])
    assert embed.title == f"🔬 基本面事件與全景估值雷達 | TSLA{_TITLE_SUFFIX}"


@pytest.mark.asyncio
async def test_cog_healthy_run_has_no_degraded_suffix() -> None:
    embed = await _run_fa([("🌊 A", "• ok"), ("🚨 B", "• ok")])
    assert embed.title == "🔬 基本面事件與全景估值雷達 | TSLA"
    assert _TITLE_SUFFIX not in (embed.title or "")


def _fv() -> FairValueRecord:
    return FairValueRecord(
        symbol="MSFT",
        trading_date="2026-10-06",
        dcf_value=600.0,
        comps_value=560.0,
        fair_value=580.0,
        margin_of_safety=0.09,
        discount_rate=0.0866,
        equity_risk_premium=0.0425,
        flags_json='["US10Y_DEFAULT", "FCF_UNAVAILABLE"]',
        method="BLENDED",
        spot_price=527.98,
    )


def _rev(flags: list[str]) -> RevisionScoreRecord:
    return RevisionScoreRecord(
        symbol="MSFT",
        trading_date="2026-10-06",
        score_30d=12.0,
        breadth_ratio=None,
        is_pead_aligned=False,
        detail_json=json.dumps({"flags": flags}),
    )


@pytest.mark.asyncio
async def test_valuation_flags_and_revision_flags_use_tree_symbols() -> None:
    rev = _rev(["NO_PRIOR_SNAPSHOT", "BREADTH_UNAVAILABLE"])
    with (
        patch("cogs.fundamental_terminal.get_latest_fair_value", return_value=_fv()),
        patch("cogs.fundamental_terminal.get_latest_revision_score", return_value=rev),
    ):
        _, body = await ValuationEngineSection().render("MSFT")
    lines = body.split("\n")
    # 估值旗標（2 項）：├─ 在前、└─ 在最後
    warn = [ln for ln in lines if "⚠️" in ln]
    assert len(warn) == 2
    assert warn[0].startswith("  ├─ ⚠️ ") and warn[1].startswith("  └─ ⚠️ ")
    # 修正動能旗標（2 項）
    rev_idx = next(i for i, ln in enumerate(lines) if "分析師修正動能" in ln)
    children = lines[rev_idx + 1 :]
    assert len(children) == 2
    assert children[0].startswith("  ├─ ") and children[1].startswith("  └─ ")
    assert "↳" not in body
    assert "**" not in body and "`" not in body


@pytest.mark.asyncio
async def test_single_revision_flag_is_last_child() -> None:
    rev = _rev(["NO_PRIOR_SNAPSHOT"])
    with (
        patch("cogs.fundamental_terminal.get_latest_fair_value", return_value=None),
        patch("cogs.fundamental_terminal.get_latest_revision_score", return_value=rev),
    ):
        _, body = await ValuationEngineSection().render("MSFT")
    assert body.split("\n")[-1].startswith("  └─ ")
    assert "├─" not in body


@pytest.mark.asyncio
async def test_watch_candidate_notes_use_tree_symbols() -> None:
    cands = [
        WatchCandidateRecord(
            trading_date="2026-10-06",
            symbol="INTC",
            rank=2,
            status="WATCH",
            reasons_json=json.dumps(
                {
                    "composite_score": 30.0,
                    "margin_of_safety": 0.4,
                    "pead_aligned": True,
                    "governance_note": "治理備註",
                    "valuation_note": "估值備註",
                }
            ),
        )
    ]
    with patch("cogs.fundamental_terminal.get_watch_candidates", return_value=cands):
        _, body = await WatchCandidateSection().render("INTC")
    lines = body.split("\n")
    assert lines[-2] == "  ├─ 治理備註"
    assert lines[-1] == "  └─ 估值備註"
    assert "↳" not in body and "**" not in body and "`" not in body


def _gov_flag(severity: str, detail_json: str | None) -> GovernanceFlagRecord:
    return GovernanceFlagRecord(
        symbol="SMCI",
        source_accession="ACC-402",
        flag_kind="ITEM_4_02_RESTATEMENT",
        severity=severity,  # type: ignore[arg-type]
        detail_json=detail_json,
        expires_at="2026-11-05 00:00:00",
    )


def test_governance_embed_colors_and_ansi_fields() -> None:
    crit = build_governance_flag_embed(
        "SMCI", _gov_flag("CRITICAL", '{"snippet": "Non-reliance."}')
    )
    high = build_governance_flag_embed(
        "SMCI", _gov_flag("HIGH", '{"snippet": "Resignation."}')
    )
    assert crit.color is not None and crit.color.value == 0xE74C3C
    assert high.color is not None and high.color.value == 0xF39C12

    payload = crit.to_dict()
    sev, snippet, advice = payload["fields"]
    assert sev["value"].startswith(_FENCE_OPEN) and sev["value"].endswith(_FENCE_CLOSE)
    assert ansi_bold("CRITICAL") in sev["value"]
    inner = sev["value"][len(_FENCE_OPEN) : -len(_FENCE_CLOSE)]
    assert "**" not in inner and "`" not in inner
    assert snippet["value"].startswith(_FENCE_OPEN)
    assert snippet["value"].endswith(_FENCE_CLOSE)
    assert not advice["value"].startswith("```")
    assert "​" not in json.dumps(payload, ensure_ascii=False)


def test_governance_snippet_with_backticks_does_not_break_fence() -> None:
    snippet = "前文 ``` 惡意 ```ansi 結尾 ``` " + "字" * 900
    flag = _gov_flag("CRITICAL", json.dumps({"snippet": snippet}))
    value = build_governance_flag_embed("SMCI", flag).to_dict()["fields"][1]["value"]
    assert value.startswith(_FENCE_OPEN) and value.endswith(_FENCE_CLOSE)
    assert value.count("```") == 2
    assert len(value) <= 1024


def test_source_scan_has_no_markdown_arrows_or_padding() -> None:
    root = Path(__file__).resolve().parents[2]
    embeds_src = (root / "cogs/embed_builders/fundamental_embeds.py").read_text(
        encoding="utf-8"
    )
    assert "\\u200b" not in embeds_src and "​" not in embeds_src

    import ast

    tree = ast.parse(
        (root / "cogs/fundamental_terminal.py").read_text(encoding="utf-8")
    )
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstring_ids.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            for bad in ("**", "`", "↳"):
                assert bad not in node.value, (bad, node.value)
