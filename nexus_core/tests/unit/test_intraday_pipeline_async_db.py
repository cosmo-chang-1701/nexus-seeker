"""IntradayScanPipeline 的 SQLite 讀取必須離開 event loop 執行緒（AGENTS.md §1）。

做法：在每個被 patch 的 database 函式內記錄 `threading.get_ident()`，再與
測試協程所在的 event loop 執行緒 ID 比對。若有任何一次同步讀取仍跑在 loop
上，測試即失敗。
"""

import threading
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from market_analysis.intraday_pipeline import IntradayScanPipeline


def _recorder(
    name: str,
    calls: list[tuple[str, int]],
    value: Any = None,
    fn: Optional[Callable[..., Any]] = None,
) -> Callable[..., Any]:
    """建立一個 side_effect：記錄呼叫名稱與執行緒 ID 後回傳固定值或 fn 的結果。"""

    def _side_effect(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, threading.get_ident()))
        if fn is not None:
            return fn(*args, **kwargs)
        return value

    return _side_effect


@pytest.mark.asyncio
async def test_dispatch_entry_advisor_alert_db_reads_off_loop() -> None:
    """_dispatch_entry_advisor_alert 的 is_notification_enabled／get_kv_cache 不得在 loop 上執行。"""
    loop_tid = threading.get_ident()
    calls: list[tuple[str, int]] = []
    pipeline = IntradayScanPipeline(MagicMock())

    watchlist_eval: Any = SimpleNamespace(
        tactical=SimpleNamespace(alert_level="green"),
        metrics=SimpleNamespace(current_price=100.0, buy_price_phase1=90.0),
    )
    ctx = SimpleNamespace(trading_strategy=None)
    now_ny = datetime(2026, 6, 5, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    advice = SimpleNamespace(
        passed=True,
        tier=None,
        strategy="RIGHT_SIDE",
        regime="III",
        regime_reason="",
        direction="LONG",
        entry_price=100.0,
        stop_loss=90.0,
        target=None,
        rr_ratio=None,
        reason="",
        squeeze=None,
        structure_directive=None,
    )

    with patch.object(
        pipeline, "_resolve_candidate_radar", AsyncMock(return_value={})
    ), patch(
        "market_analysis.intraday_pipeline.entry_advisor.evaluate_entry_advice",
        AsyncMock(return_value=advice),
    ), patch("config.WATCHLIST_ADVISOR_DRY_RUN", False), patch(
        "database.is_notification_enabled",
        side_effect=_recorder("is_notification_enabled", calls, value=True),
    ), patch(
        # 快取命中即提早返回，避免走到 embed 組裝與推播。
        "database.get_kv_cache",
        side_effect=_recorder("get_kv_cache", calls, value={"sent": True}),
    ):
        await pipeline._dispatch_entry_advisor_alert(
            42, "AAPL", watchlist_eval, ctx, now_ny
        )

    names = {name for name, _ in calls}
    assert {"is_notification_enabled", "get_kv_cache"} <= names
    assert all(tid != loop_tid for _, tid in calls), calls


@pytest.mark.asyncio
@pytest.mark.slow
async def test_run_loop_iteration_db_reads_off_loop() -> None:
    """_run_loop 單輪迭代中的所有 SQLite 讀取都必須離開 event loop 執行緒。"""
    loop_tid = threading.get_ident()
    calls: list[tuple[str, int]] = []
    pipeline = IntradayScanPipeline(MagicMock())
    pipeline.is_running = True
    mock_now = datetime(2026, 6, 5, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    # 非綠燈標的，才會走完整心跳 embed 路徑（觸發持倉／標籤讀取）。
    evaluation = SimpleNamespace(
        metrics=SimpleNamespace(
            symbol="MU",
            current_price=410.5,
            iv_rank=None,
            option_skew=None,
            skew_percentile=None,
            option_skew_state="N/A",
            buy_zone_status="測試買區",
            sell_zone_status="測試賣區",
        ),
        tactical=SimpleNamespace(
            alert_level="yellow",
            scenario="wait",
            sddm_route="STANDBY",
        ),
        event_context=SimpleNamespace(summary="未偵測到近期重大事件"),
        symbol_gex=None,
    )
    ctx = SimpleNamespace(
        user_id=42,
        capital=120000.0,
        risk_limit=12.0,
        trading_strategy=None,
        load_failed=False,
    )

    async def _stop(_secs: Any) -> None:
        pipeline.is_running = False

    pipeline.evaluate_watchlist_symbol = AsyncMock(return_value=evaluation)  # type: ignore[method-assign]

    # 以 ExitStack 扁平化堆疊 patch（單一 with 內 patch 過多會超過 CPython 巢狀區塊上限）。
    patchers = [
        patch(
            "market_analysis.intraday_pipeline.pipeline.is_market_open",
            return_value=True,
        ),
        patch("services.llm_service.is_memory_safe", return_value=True),
        patch(
            "database.get_all_user_ids",
            side_effect=_recorder("get_all_user_ids", calls, value=[42]),
        ),
        patch(
            "database.get_full_user_context",
            side_effect=_recorder("get_full_user_context", calls, value=ctx),
        ),
        patch(
            "database.get_user_watchlist",
            side_effect=_recorder("get_user_watchlist", calls, value=[("MU", 1)]),
        ),
        patch(
            "database.get_user_notification_settings",
            side_effect=_recorder(
                "get_user_notification_settings",
                calls,
                value={"heartbeat_symbol_deep": True},
            ),
        ),
        patch(
            "database.get_user_holdings",
            side_effect=_recorder("get_user_holdings", calls, value=[]),
        ),
        patch(
            "database.get_all_trade_positions",
            side_effect=_recorder("get_all_trade_positions", calls, value=[]),
        ),
        patch(
            "database.is_symbol_in_portfolio",
            side_effect=_recorder("is_symbol_in_portfolio", calls, value=False),
        ),
        patch(
            "database.watchlist_tags.get_watchlist_tags",
            side_effect=_recorder("get_watchlist_tags", calls, value=[]),
        ),
        patch(
            "services.trading_service.get_adjusted_user_capital",
            AsyncMock(return_value=120000.0),
        ),
        patch(
            "market_analysis.intraday_pipeline.pipeline.calculate_dynamic_trading_signals",
            return_value={},
        ),
        patch(
            "market_analysis.intraday_pipeline.pipeline.derive_watchlist_option_guidance",
            return_value="option guidance",
        ),
        patch(
            "market_analysis.intraday_pipeline.pipeline.build_watchlist_option_plan",
            AsyncMock(return_value="option-plan"),
        ),
        patch(
            "market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary",
            return_value="rule-skew-commentary",
        ),
        patch(
            "cogs.embed_builder.create_watchlist_signal_embed",
            return_value=MagicMock(),
        ),
        patch("services.notification_dispatcher.notify", AsyncMock()),
        patch.object(
            pipeline._wall_break_monitor,
            "process_symbol",
            AsyncMock(return_value=SimpleNamespace(intraday=None, t1=None)),
        ),
        patch(
            "market_analysis.evaluation_recorder.flush_evaluations",
            AsyncMock(return_value=0),
        ),
        patch(
            "services.notification_dispatch_recorder.flush_dispatch_records",
            AsyncMock(return_value=None),
        ),
        patch("asyncio.sleep", side_effect=_stop),
    ]

    with ExitStack() as stack:
        mock_datetime_class = stack.enter_context(
            patch("market_analysis.intraday_pipeline.pipeline.datetime")
        )
        for patcher in patchers:
            stack.enter_context(patcher)
        mock_datetime_class.now.return_value = mock_now
        await pipeline._run_loop()

    expected = {
        "get_all_user_ids",
        "get_full_user_context",
        "get_user_watchlist",
        "get_user_notification_settings",
        "get_user_holdings",
        "get_all_trade_positions",
        "is_symbol_in_portfolio",
        "get_watchlist_tags",
    }
    names = {name for name, _ in calls}
    assert expected <= names, expected - names
    assert all(tid != loop_tid for _, tid in calls), calls
    assert pipeline.is_running is False
