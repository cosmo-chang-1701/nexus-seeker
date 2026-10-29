"""自動掃描 DDP 警報的 load_failed 守衛：帳戶資料讀取失敗不推、正常才推。"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.test_user_context_load_failed import _ctx


async def _run_scan(failed: bool) -> AsyncMock:
    from cogs.trading.scan import MarketScanCog

    cog = MarketScanCog(MagicMock())
    report: dict[str, Any] = {"symbol": "AAA", "fiscal_period": "2026-07-26"}
    cog.trading_service.run_ddp_scan = AsyncMock(  # type: ignore[method-assign]
        return_value=[report]
    )
    cog.trading_service.ddp_inspector.record_signal = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    cog.trading_service.run_iv_opportunity_scan = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )
    cog.trading_service.run_market_scan = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    m_notify = AsyncMock()
    with (
        patch(
            "cogs.trading.scan.database.get_all_watchlist", return_value=[(7, "AAA", 1)]
        ),
        patch(
            "cogs.trading.scan.database.get_full_user_context",
            return_value=_ctx(failed, option_alert_mode=1),
        ),
        patch(
            "cogs.trading.scan.is_channel_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch("cogs.embed_builder.create_ddp_embed", return_value=MagicMock()),
        patch.object(
            cog, "_should_send_alert", new_callable=AsyncMock, return_value=True
        ),
        patch("cogs.trading.scan.notify", m_notify),
    ):
        await cog._run_market_scan_logic(is_auto=True)
    return m_notify


def _ddp_calls(m_notify: AsyncMock) -> list[Any]:
    return [
        c
        for c in m_notify.await_args_list
        if c.args[2] == "alpha_market_signals"
        and str(c.kwargs.get("dedup_key", "")).startswith("ddp_alert_")
    ]


@pytest.mark.asyncio
async def test_auto_scan_ddp_alert_skipped_when_context_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ok_calls = _ddp_calls(await _run_scan(False))
    assert len(ok_calls) == 1
    assert ok_calls[0].kwargs["dedup_key"].startswith("ddp_alert_7_AAA_")

    with caplog.at_level(logging.ERROR):
        failed_calls = _ddp_calls(await _run_scan(True))
    assert failed_calls == []
    assert any(
        r.levelno == logging.ERROR and "略過 DDP 警報" in r.getMessage()
        for r in caplog.records
    )
