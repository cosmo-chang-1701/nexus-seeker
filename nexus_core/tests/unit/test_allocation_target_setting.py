"""target_equity_pct（宣告股票占比）設定：遷移、持久化與 /settings 面板。"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

import database
from cogs.terminal import AccountSettingsModal, AccountSettingsView, SETTINGS_LABELS
from database.core import get_migrations
from database.migrations import v098_add_target_equity_pct as mig


@pytest.fixture(autouse=True)
def clean_db(db_conn: Any) -> None:
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM user_settings")
    db_conn.commit()


def _raw_target(db_conn: Any, uid: int) -> Any:
    row = db_conn.execute(
        "SELECT target_equity_pct FROM user_settings WHERE user_id = ?", (uid,)
    ).fetchone()
    return row[0]


def _embed_text(uid: int) -> str:
    embed = AccountSettingsView(uid).build_embed()
    return "\n".join(str(f.value) for f in embed.fields)


def _modal(uid: int, view: Any) -> AccountSettingsModal:
    return AccountSettingsModal(
        user_id=uid,
        key="target_equity_pct",
        label="⚖️ 目標股票比例",
        current_value=-1.0,
        placeholder="輸入 0-100，-1 = 關閉",
        view=view,
    )


def test_migration_v098_module() -> None:
    assert mig.version == 98
    assert mig.description
    assert "target_equity_pct" in mig.sql
    assert "DEFAULT -1" in mig.sql
    assert 98 in [m["version"] for m in get_migrations()]


def test_default_is_disabled(db_conn: Any) -> None:
    assert database.get_full_user_context(880001).target_equity_pct == -1.0


def test_existing_row_stays_disabled(db_conn: Any) -> None:
    uid = 880002
    database.upsert_user_config(uid, risk_limit=10.0)
    assert database.get_full_user_context(uid).target_equity_pct == -1.0
    raw = _raw_target(db_conn, uid)
    assert raw is not None and raw == -1.0


@pytest.mark.parametrize(
    "given,expected", [(100, 100.0), (-5, -1.0), (150, 100.0), (0, 0.0), (60, 60.0)]
)
def test_upsert_clamping(db_conn: Any, given: float, expected: float) -> None:
    uid = 880003
    assert database.upsert_user_config(uid, target_equity_pct=given)
    assert database.get_full_user_context(uid).target_equity_pct == expected


def test_null_column_reads_as_disabled(db_conn: Any) -> None:
    uid = 880004
    database.upsert_user_config(uid, risk_limit=10.0)
    db_conn.execute(
        "UPDATE user_settings SET target_equity_pct = NULL WHERE user_id = ?", (uid,)
    )
    db_conn.commit()
    assert database.get_full_user_context(uid).target_equity_pct == -1.0


def test_settings_panel_shows_target(db_conn: Any) -> None:
    uid = 880005
    assert "target_equity_pct" in SETTINGS_LABELS
    assert len(SETTINGS_LABELS) <= 25
    assert "目標股票比例**: `未啟用`" in _embed_text(uid)
    database.upsert_user_config(uid, target_equity_pct=100)
    assert "目標股票比例**: `100%`" in _embed_text(uid)


@pytest.mark.asyncio
async def test_modal_rejects_over_100(db_conn: Any) -> None:
    uid = 880006
    database.upsert_user_config(uid, target_equity_pct=40)
    modal = _modal(uid, AccountSettingsView(uid))
    modal.input_field._value = "150"
    interaction = AsyncMock()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_called_once()
    desc = interaction.response.send_message.call_args[1]["embed"].description
    assert "輸入 -1 關閉" in desc
    assert database.get_full_user_context(uid).target_equity_pct == 40.0


@pytest.mark.asyncio
async def test_modal_rejects_non_number(db_conn: Any) -> None:
    uid = 880007
    database.upsert_user_config(uid, target_equity_pct=40)
    modal = _modal(uid, AccountSettingsView(uid))
    modal.input_field._value = "abc"
    interaction = AsyncMock()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_called_once()
    assert database.get_full_user_context(uid).target_equity_pct == 40.0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["-1", "-5"])
async def test_modal_negative_disables(db_conn: Any, text: str) -> None:
    uid = 880008
    database.upsert_user_config(uid, target_equity_pct=40)
    modal = _modal(uid, AccountSettingsView(uid))
    modal.input_field._value = text
    interaction = AsyncMock()
    await modal.on_submit(interaction)
    assert database.get_full_user_context(uid).target_equity_pct == -1.0
    assert _raw_target(db_conn, uid) == -1.0


@pytest.mark.asyncio
async def test_modal_accepts_zero_and_100(db_conn: Any) -> None:
    uid = 880009
    for text, expected in (("0", 0.0), ("100", 100.0)):
        modal = _modal(uid, AccountSettingsView(uid))
        modal.input_field._value = text
        await modal.on_submit(AsyncMock())
        assert database.get_full_user_context(uid).target_equity_pct == expected


@pytest.mark.asyncio
async def test_modal_rejects_nan(db_conn: Any) -> None:
    uid = 880010
    database.upsert_user_config(uid, target_equity_pct=40)
    modal = _modal(uid, AccountSettingsView(uid))
    modal.input_field._value = "nan"
    interaction = AsyncMock()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_called_once()
    assert database.get_full_user_context(uid).target_equity_pct == 40.0
    assert _raw_target(db_conn, uid) == 40.0


def test_upsert_ignores_nan(db_conn: Any) -> None:
    uid = 880011
    database.upsert_user_config(uid, target_equity_pct=40)
    database.upsert_user_config(uid, target_equity_pct=float("nan"))
    assert database.get_full_user_context(uid).target_equity_pct == 40.0


def test_select_menu_display(db_conn: Any) -> None:
    uid = 880012

    def _desc() -> str:
        select: Any = AccountSettingsView(uid).children[0]
        for opt in select.options:
            if opt.value == "target_equity_pct":
                return str(opt.description)
        raise AssertionError("missing option")

    database.upsert_user_config(uid, risk_limit=10.0)
    assert "目前: 未啟用" in _desc()
    database.upsert_user_config(uid, target_equity_pct=60)
    assert "目前: 60%" in _desc()
