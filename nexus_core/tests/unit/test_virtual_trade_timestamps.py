"""虛擬交易時間戳時區一致性：closed_at 與 opened_at 同為 UTC。"""

import datetime
import time
from typing import Any

import pytest

from database.virtual_trading import (
    add_virtual_trade,
    close_virtual_trade,
    get_virtual_trade_by_id,
)


def _parse(ts: str) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc
    )


def test_closed_at_is_utc_and_not_before_opened_at(
    db_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 模擬正式環境容器 TZ=America/New_York；修正前 closed_at 會寫成美東本地時間
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        _assert_closed_at_utc()
    finally:
        monkeypatch.undo()
        time.tzset()


def _assert_closed_at_utc() -> None:
    trade_id = add_virtual_trade(
        user_id=1,
        symbol="NVDA",
        opt_type="call",
        strike=215.0,
        expiry="2026-12-18",
        entry_price=1.30,
        quantity=-1,
    )
    assert close_virtual_trade(trade_id, exit_price=1.10)

    row = get_virtual_trade_by_id(trade_id)
    assert row is not None
    opened = _parse(str(row["opened_at"]))
    closed = _parse(str(row["closed_at"]))
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # 同一測試內開倉後立即平倉：兩者皆為 UTC 時，差距應在數秒內且平倉不早於開倉
    assert closed >= opened
    assert (closed - opened) < datetime.timedelta(minutes=1)
    assert abs(now_utc - closed) < datetime.timedelta(minutes=1)
