import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COGS_DIR = ROOT / "cogs"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cogs_do_not_construct_embeds_outside_builder() -> None:
    offenders = []

    for path in sorted(COGS_DIR.glob("*.py")):
        if path.name == "embed_builder.py":
            continue
        if "discord.Embed(" in _read_text(path):
            offenders.append(path.name)

    assert offenders == []


def test_cogs_do_not_use_queue_dm_message_shortcut() -> None:
    offenders = []

    for path in sorted(COGS_DIR.glob("*.py")):
        tree = ast.parse(_read_text(path), filename=str(path))
        has_message_shortcut = False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "queue_dm":
                continue
            if any(keyword.arg == "message" for keyword in node.keywords):
                has_message_shortcut = True
                break

        if has_message_shortcut:
            offenders.append(path.name)

    assert offenders == []


# ---------------------------------------------------------------------------
# NexusEmbed 契約靜態防線 (G1–G4)：純 AST／正規表示式不變式，不得標 slow。
# ---------------------------------------------------------------------------

import re  # noqa: E402

from cogs.embed_builders._core import CURATED_PALETTE  # noqa: E402

_BUILDERS_DIR = ROOT / "cogs" / "embed_builders"
_CORE_FILE = _BUILDERS_DIR / "_core.py"
_SKIP_PARTS = {"tests", ".venv", "venv", "__pycache__", "build"}


def _production_files() -> list[Path]:
    files = []
    for path in sorted(ROOT.rglob("*.py")):
        rel_parts = path.relative_to(ROOT).parts
        if any(part in _SKIP_PARTS for part in rel_parts):
            continue
        files.append(path)
    return files


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_g1_no_raw_discord_embed_construction() -> None:
    """G1：除 _core.py 外，不得呼叫 discord.Embed(...) 或 discord.Embed.from_dict(...)。"""
    offenders = []
    for path in _production_files():
        if path == _CORE_FILE:
            continue
        tree = ast.parse(_read_text(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func) in {
                "discord.Embed",
                "discord.Embed.from_dict",
            }:
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == []


def test_g2_nexus_embed_only_constructed_in_embed_builders() -> None:
    """G2：embed_builders 以外的 production 程式碼不得直接呼叫 NexusEmbed(...)。"""
    offenders = []
    for path in _production_files():
        if _BUILDERS_DIR in path.parents:
            continue
        tree = ast.parse(_read_text(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] == (
                "NexusEmbed"
            ):
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == []


def _brand_strings(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "Nexus" in n.value
    ]


def test_g3_footers_carry_no_brand_text() -> None:
    """G3：set_footer 文字、*FOOTER* 模組常數、footer_text 預設值都不得自帶 Nexus 品牌字。"""
    offenders = []
    for path in sorted((ROOT / "cogs").rglob("*.py")):
        if any(part in _SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if path == _CORE_FILE:
            continue
        tree = ast.parse(_read_text(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Attribute) and node.func.attr == "set_footer"
            ):
                parts = [kw.value for kw in node.keywords if kw.arg == "text"]
                parts += list(node.args[:1])
                for part in parts:
                    if _brand_strings(part):
                        offenders.append(f"{_rel(path)}:{node.lineno} set_footer")
            elif isinstance(node, ast.Assign):
                if any(
                    isinstance(t, ast.Name) and "FOOTER" in t.id.upper()
                    for t in node.targets
                ) and _brand_strings(node.value):
                    offenders.append(f"{_rel(path)}:{node.lineno} FOOTER const")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                positional = args.posonlyargs + args.args
                pairs = list(
                    zip(
                        positional[len(positional) - len(args.defaults) :],
                        args.defaults,
                    )
                )
                pairs += [
                    (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d
                ]
                for arg, default in pairs:
                    if arg.arg == "footer_text" and _brand_strings(default):
                        offenders.append(f"{_rel(path)}:{node.lineno} footer_text")
    assert offenders == []


def test_g4_embed_builders_hex_literals_are_curated() -> None:
    """G4：embed_builders 內所有 0xRRGGBB 字面值都必須屬於 CURATED_PALETTE。"""
    pattern = re.compile(r"0x[0-9A-Fa-f]{6}\b")
    offenders = []
    for path in sorted(_BUILDERS_DIR.rglob("*.py")):
        if path == _CORE_FILE or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
            for match in pattern.findall(line):
                if int(match, 16) not in CURATED_PALETTE:
                    offenders.append(f"{_rel(path)}:{lineno} {match}")
    assert offenders == []


def test_g5_embed_builders_tree_glyphs_use_full_connector() -> None:
    """G5：樹狀符號必須是 `├─`／`└─`；`├ `／`└ `（缺 ─）與 `├──`／`└──` 皆違規。"""
    pattern = re.compile(r"[├└] |[├└]──")
    offenders = []
    for path in sorted(_BUILDERS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{_rel(path)}:{lineno} {line.strip()[:60]}")
    assert offenders == []
