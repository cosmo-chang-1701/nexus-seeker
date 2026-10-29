"""NexusEmbed 5800 字總量超標時的最後防線：逐行縮減尾端欄位，整欄捨棄僅為最後手段。"""

import logging
from typing import Any

from cogs.embed_builders._core import (
    NexusEmbed,
    _MAX_TOTAL_LEN,
    _shrink_field_value,
)

_ZWSP = "​"
_FOOTNOTE = "⚠️ 方向／SWEEP 為 Bid-Ask 啟發式；OI 為前日；📊日累積 ❔未定"


def _ansi_value(rows: int, footnote: bool = True, suffix: str = "") -> str:
    lines = ["到期日 | 履約價 | 口數", "-----------------"]
    lines += [f"2026-11-{i:02d} | {100 + i} | {'9' * 20}" for i in range(rows)]
    if footnote:
        lines.append(_FOOTNOTE)
    return "```ansi\n" + "\n".join(lines) + "\n```" + suffix


def _total(d: dict[str, Any]) -> int:
    total = len(d.get("title") or "") + len(d.get("description") or "")
    total += len((d.get("footer") or {}).get("text") or "")
    for f in d.get("fields", []):
        total += len(f.get("name") or "") + len(f.get("value") or "")
    return total


def _build(tail_value: str, title: str = "測試標題") -> NexusEmbed:
    e = NexusEmbed(title=title)
    for i in range(5):
        e.add_field(name=f"欄位{i}", value="字" * 1000, inline=False)
    e.add_field(name="🐋 UOA", value=tail_value, inline=False)
    return e


def test_ansi_tail_field_trimmed_all_fields_kept() -> None:
    tail = _ansi_value(20)
    e = _build(tail)
    raw: Any = super(NexusEmbed, e).to_dict()
    assert _total(raw) > _MAX_TOTAL_LEN
    d = e.to_dict()
    assert len(d["fields"]) == 6
    assert _total(d) <= _MAX_TOTAL_LEN
    value = d["fields"][-1]["value"]
    assert value.startswith("```ansi\n") and value.endswith("\n```")
    assert "餘" in value and "行略" in value
    for i in range(5):
        assert d["fields"][i]["value"] == "字" * 1000


def test_footnote_survives_after_marker() -> None:
    d = _build(_ansi_value(20)).to_dict()
    lines = d["fields"][-1]["value"].split("\n")
    assert lines[-2] == _FOOTNOTE
    assert lines[-3] == ""
    assert lines[-4].startswith("…（餘 ")


def test_trailing_suffix_after_fence_preserved() -> None:
    d = _build(_ansi_value(20, suffix="\n" + _ZWSP)).to_dict()
    value = d["fields"][-1]["value"]
    assert value.endswith("\n```\n" + _ZWSP)
    assert "餘" in value


def test_to_dict_does_not_mutate_embed() -> None:
    tail = _ansi_value(20)
    e = _build(tail)
    first = e.to_dict()
    second = e.to_dict()
    assert first == second
    assert e.fields[-1].value == tail
    assert all(f.value == "字" * 1000 for f in e.fields[:5])


def test_shrink_spills_to_previous_field() -> None:
    unshrinkable = "```ansi\n" + "字" * 1000 + "\n```"
    e = NexusEmbed(title="t")
    e.add_field(name="前", value=_ansi_value(18, footnote=False), inline=False)
    for i in range(5):
        e.add_field(name=f"固{i}", value=unshrinkable, inline=False)
    raw: Any = super(NexusEmbed, e).to_dict()
    assert _total(raw) > _MAX_TOTAL_LEN
    d = e.to_dict()
    assert len(d["fields"]) == 6
    assert _total(d) <= _MAX_TOTAL_LEN
    assert d["fields"][-1]["value"] == unshrinkable
    assert "餘" in d["fields"][0]["value"]


def test_last_resort_drop_when_shrink_insufficient(
    caplog: Any,
) -> None:
    e = NexusEmbed(title="巨大標題", description="字" * 4000)
    fixed = "```ansi\n" + "字" * 980 + "\n```"
    e.add_field(name="a", value=fixed, inline=False)
    e.add_field(name="b", value=fixed, inline=False)
    e.add_field(name=_ZWSP, value=fixed, inline=False)
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        d = e.to_dict()
    assert len(d["fields"]) < 3
    assert _total(d) <= _MAX_TOTAL_LEN
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "巨大標題" in msg and "#3（續欄）" in msg


def test_log_contains_title_and_continuation_label(caplog: Any) -> None:
    e = NexusEmbed(title="/x 標題")
    for i in range(5):
        e.add_field(name=f"欄位{i}", value="字" * 1000, inline=False)
    e.add_field(name=_ZWSP, value=_ansi_value(20), inline=False)
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        e.to_dict()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "/x 標題" in msg
    assert "#6（續欄）(-" in msg


def test_no_warning_within_budget(caplog: Any) -> None:
    e = NexusEmbed(title="t")
    e.add_field(name="a", value="b")
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        e.to_dict()
    assert not caplog.records


def test_shrink_single_line_keeps_ansi_escape_intact() -> None:
    value = "\x1b[31m" + "字" * 100 + "\x1b[0m"
    out = _shrink_field_value(value, 30)
    assert out.endswith("…") and len(out) < len(value)
    assert "\x1b" not in out.replace("\x1b[31m", "")
    cut = _shrink_field_value("a" * 30 + "\x1b[31m" + "b" * 50, 33)
    assert "\x1b" not in cut and cut.endswith("…")


def test_shrink_never_longer_and_noop_when_fits() -> None:
    v = "```ansi\na\n```"
    assert _shrink_field_value(v, 100) == v
    assert len(_shrink_field_value(v, 3)) <= len(v)
    multi = "\n".join(f"line{i}" for i in range(10))
    out = _shrink_field_value(multi, 30)
    assert len(out) <= 30 and "餘" in out


def test_blank_line_before_footnote_not_counted_and_restored() -> None:
    rows = [f"row{i:02d}" + "x" * 30 for i in range(10)]
    value = "```ansi\n" + "\n".join(rows) + "\n\n" + _FOOTNOTE + "\n```"
    out = _shrink_field_value(value, len(value) - 100)
    lines = out.split("\n")
    marker = next(ln for ln in lines if ln.startswith("…（餘 "))
    kept_rows = [ln for ln in lines if ln.startswith("row")]
    assert marker == f"…（餘 {len(rows) - len(kept_rows)} 行略）"
    i = lines.index(marker)
    assert lines[i + 1] == "" and lines[i + 2] == _FOOTNOTE
    assert lines[-1] == "```"


def test_single_line_fence_returned_unchanged() -> None:
    v = "```ansi " + "字" * 200 + "```"
    assert _shrink_field_value(v, 50) == v
