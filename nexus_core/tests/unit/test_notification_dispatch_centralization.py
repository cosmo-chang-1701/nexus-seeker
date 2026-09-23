"""強制「主動推播一律經 services/notification_dispatcher」的不變式（比照
test_output_centralization.py / test_db_write_centralization.py）。

存在理由（回歸防護）：過去約 40 個推播點各自 `if is_notification_enabled(...):
await bot.queue_dm(...)`，於是出現完全沒查開關的推播（機器人啟停廣播、NRO 期權掃描、
`/iv_scan` 私訊其他使用者）、在 LLM 呼叫之後才查開關、以及先寫去重旗標才查開關等缺陷。
這類缺陷在單元測試裡看不出來——bot 是 mock，推播「有沒有被開關控制」不會讓任何斷言失敗。

規則：
1. `queue_dm(` 只能出現在 `bot.py`（佇列本身與超長 embed 拆分）、dispatcher 本身，
   以及只發給管理員、刻意不經使用者頻道的 `services/memory_manager.py`。
2. 傳給 `notify` / `notify_many` / `is_channel_enabled` / `is_notification_enabled` 的
   頻道字串字面值必須是註冊表內的 key（抓拼錯的頻道名；拼錯時 `is_notification_enabled`
   會對未知 key 回傳 True，等於無聲地繞過開關）。
3. `NotificationKey` Literal 與 `CHANNELS` 註冊表必須一致。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator

from database.notification_channels import (
    ALL_NOTIFICATION_KEYS,
    CHANNELS,
    NOTIFICATION_KEY_VALUES,
)

_CORE_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCAN_TARGETS = (
    "bot.py",
    "cogs",
    "services",
    "market_analysis",
    "ui",
    "risk_engine",
    "formatters",
    "database",
)

_EXCLUDED_PARTS = {"build", "venv", ".venv", "__pycache__", "migrations"}

_QUEUE_DM_ALLOWED = {
    "bot.py",
    "services/notification_dispatcher.py",
    # 管理員專用的記憶體 / 電池警報：收件人固定為 DISCORD_ADMIN_USER_ID，
    # 不屬於任何使用者通知頻道
    "services/memory_manager.py",
}

_CHANNEL_ARG_FUNCS = {
    "notify": 2,
    "notify_many": 2,
    "is_channel_enabled": 1,
    "is_notification_enabled": 1,
}


def _iter_source_files() -> Iterator[tuple[str, pathlib.Path]]:
    for target in _SCAN_TARGETS:
        root = _CORE_ROOT / target
        if root.is_file():
            yield target, root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in _EXCLUDED_PARTS for part in path.parts):
                continue
            yield path.relative_to(_CORE_ROOT).as_posix(), path


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _calls() -> Iterator[tuple[str, ast.Call]]:
    for rel, path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield rel, node


def test_scan_actually_covers_push_sites() -> None:
    """守衛：掃描規則失效（路徑寫錯）時不可空跑通過。"""
    notify_calls = [
        rel for rel, c in _calls() if _call_name(c.func) in {"notify", "notify_many"}
    ]
    assert len(notify_calls) >= 25, f"偵測到的 notify 呼叫過少 ({len(notify_calls)})"


def test_queue_dm_only_called_from_allowlist() -> None:
    offenders = sorted(
        f"{rel}:{c.lineno}"
        for rel, c in _calls()
        if _call_name(c.func) == "queue_dm" and rel not in _QUEUE_DM_ALLOWED
    )
    assert not offenders, (
        "主動推播必須經 services/notification_dispatcher.notify()（先查頻道開關、"
        "再去重、入列後才寫旗標），不得直接呼叫 queue_dm：\n  " + "\n  ".join(offenders)
    )


def test_channel_literals_are_registered() -> None:
    unknown: list[str] = []
    for rel, c in _calls():
        idx = _CHANNEL_ARG_FUNCS.get(_call_name(c.func) or "")
        if idx is None:
            continue
        arg: ast.expr | None = c.args[idx] if len(c.args) > idx else None
        if arg is None:
            arg = next((kw.value for kw in c.keywords if kw.arg == "channel"), None)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if arg.value not in ALL_NOTIFICATION_KEYS:
                unknown.append(f"{rel}:{c.lineno} → {arg.value!r}")
    assert not unknown, "未登記於通知頻道註冊表的 key：\n  " + "\n  ".join(unknown)


def test_literal_type_matches_registry() -> None:
    assert NOTIFICATION_KEY_VALUES == {c.key for c in CHANNELS}
    assert len(ALL_NOTIFICATION_KEYS) == len(set(ALL_NOTIFICATION_KEYS))
