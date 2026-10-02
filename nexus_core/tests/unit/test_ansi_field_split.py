"""`_add_ansi_field_safely` 段落邊界切欄與續欄命名。"""

import discord

from cogs.embed_builders._embed_helpers import (
    ANSI_CONTINUATION_FIELD_NAME,
    _add_ansi_field_safely,
)


def _block(header: str, n: int, width: int = 60) -> list[str]:
    return [f" ── {header} ──"] + [f" ├─ {header}-{i}-" + "x" * width for i in range(n)]


def test_short_input_single_field_keeps_name() -> None:
    embed = discord.Embed()
    _add_ansi_field_safely(embed, "🧲 GEX", ["```ansi", "a", "", "b", "```"])
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "🧲 GEX"
    assert embed.fields[0].value == "```ansi\na\n\nb\n```\n​"


def test_split_on_paragraph_boundary_with_hidden_continuation_name() -> None:
    lines = ["```ansi"] + _block("A", 8) + [""] + _block("B", 8) + [""]
    lines += _block("C", 8) + ["```"]
    embed = discord.Embed()
    _add_ansi_field_safely(embed, "🧲 GEX", lines)

    assert len(embed.fields) >= 2
    assert embed.fields[0].name == "🧲 GEX"
    for field in embed.fields[1:]:
        assert field.name == ANSI_CONTINUATION_FIELD_NAME
        assert "續" not in str(field.name)
    for field in embed.fields:
        value = str(field.value)
        assert len(value) <= 1024
        assert value.startswith("```ansi\n") and value.endswith("\n```\n​")
        # 每個段落完整落在同一欄：出現段落標頭就要有該段的最後一行
        for header in ("A", "B", "C"):
            if f" ── {header} ──" in value:
                assert f"{header}-7-" in value
    joined = "".join(str(f.value) for f in embed.fields)
    for header in ("A", "B", "C"):
        assert joined.count(f" ── {header} ──") == 1


def test_oversized_single_paragraph_falls_back_to_line_split() -> None:
    lines = _block("BIG", 40)
    embed = discord.Embed()
    _add_ansi_field_safely(embed, "🐋 UOA", lines)

    assert len(embed.fields) >= 2
    for field in embed.fields:
        assert len(str(field.value)) <= 1024
    joined = "".join(str(f.value) for f in embed.fields)
    for i in range(40):
        assert f"BIG-{i}-" in joined
