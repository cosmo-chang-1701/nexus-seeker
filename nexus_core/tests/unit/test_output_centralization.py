import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COGS_DIR = ROOT / "cogs"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cogs_do_not_construct_embeds_outside_builder():
    offenders = []

    for path in sorted(COGS_DIR.glob("*.py")):
        if path.name == "embed_builder.py":
            continue
        if "discord.Embed(" in _read_text(path):
            offenders.append(path.name)

    assert offenders == []


def test_cogs_do_not_use_queue_dm_message_shortcut():
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
