"""`/dash`、戰略看板按鈕與 `/market` 的 SQLite 讀取須離開 event loop。"""

import threading
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.unified_terminal import PortfolioHubView, UnifiedTerminalCog
from cogs.unified_terminal.utils import get_macro_overview_data
from tests.unit.network_guard import NetworkGuard

_THRESHOLD = "market_analysis.trading_orchestration.get_safety_payout_threshold"
_MACRO_KEYS: tuple[str, ...] = (
    "macro_spx",
    "macro_vix",
    "macro_us10y",
    "macro_wti",
    "macro_spy_spot",
    "macro_cpi_actual",
    "macro_cpi_expected",
    "macro_cpi_is_fallback",
    "macro_cpi_deviation",
)


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """攔截所有對外連線；即使呼叫端吞掉例外，teardown 仍會讓測試失敗。"""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.polymarket_service = MagicMock()
    bot.polymarket_service.get_market_snapshot = AsyncMock(return_value=[])
    return bot


def _ctx() -> Any:
    return SimpleNamespace(
        capital=112511.0,
        total_theta=50.0,
        monthly_expense=1500.0,
        cash_reserve=5000.0,
        is_professional_mode=False,
        total_weighted_delta=10.0,
        total_vanna=2.0,
    )


def _record_context_threads(threads: list[int]) -> Callable[..., Any]:
    def _side_effect(*_args: Any) -> Any:
        threads.append(threading.get_ident())
        return _ctx()

    return _side_effect


@pytest.mark.asyncio
async def test_portfolio_hub_reads_user_context_off_event_loop(
    mock_interaction: Any, mock_bot: Any
) -> None:
    cog = UnifiedTerminalCog(mock_bot)
    loop_thread = threading.get_ident()
    threads: list[int] = []

    with (
        patch(
            "services.trading_service.TradingService.get_portfolio_pnl",
            new_callable=AsyncMock,
            return_value={"trades": [], "total_unrealized_pnl": 0.0},
        ),
        patch(
            "services.trading_service.TradingService.get_market_nav",
            new_callable=AsyncMock,
            return_value={"nav": 100000.0},
        ),
        patch(
            "database.get_full_user_context",
            side_effect=_record_context_threads(threads),
        ) as mock,
        patch(
            "services.withdrawal_runway_service.get_runway_display",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
        patch(
            "services.market_data_service.get_macro_environment",
            new_callable=AsyncMock,
            return_value={"vix": 18.0},
        ),
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(_THRESHOLD, return_value=13000.0),
    ):
        await cog.portfolio_hub.callback(cog, mock_interaction)  # type: ignore[call-arg, arg-type]

    mock.assert_called_once_with(mock_interaction.user.id)
    assert len(threads) == 1
    assert threads[0] != loop_thread
    mock_interaction.followup.send.assert_called_once()


@pytest.mark.asyncio
async def test_portfolio_view_btn_home_reads_user_context_off_event_loop(
    mock_interaction: Any, mock_bot: Any
) -> None:
    view = PortfolioHubView(user_id=123, bot=mock_bot)
    loop_thread = threading.get_ident()
    threads: list[int] = []

    with (
        patch(
            "services.trading_service.TradingService.get_portfolio_pnl",
            new_callable=AsyncMock,
            return_value={"trades": [], "total_unrealized_pnl": 0.0},
        ),
        patch(
            "services.trading_service.TradingService.get_market_nav",
            new_callable=AsyncMock,
            return_value={"nav": 100000.0},
        ),
        patch(
            "database.get_full_user_context",
            side_effect=_record_context_threads(threads),
        ) as mock,
        patch(
            "services.withdrawal_runway_service.get_runway_display",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=18.0,
        ),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={"c": 670.0},
        ),
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(_THRESHOLD, return_value=13000.0),
    ):
        await view.btn_home.callback(mock_interaction)

    mock.assert_called_once_with(123)
    assert len(threads) == 1
    assert threads[0] != loop_thread
    _, kwargs = mock_interaction.edit_original_response.call_args
    assert "戰略看板" in kwargs["embed"].title


@pytest.mark.asyncio
async def test_macro_overview_kv_fallback_and_cpi_reads_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    reads: list[tuple[str, int]] = []

    def _record_read(key: str, *_args: Any, **_kwargs: Any) -> None:
        reads.append((key, threading.get_ident()))

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        # 報價一律為空，使五個 `_parse` 都走 kv 回退
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("database.get_kv_cache", side_effect=_record_read),
        patch("database.save_kv_cache", new_callable=AsyncMock),
        patch("database.cache.save_kv_cache", new_callable=AsyncMock),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "cogs.unified_terminal.utils.get_credit_spreads",
            new_callable=AsyncMock,
            return_value={"hy": None, "ig": None},
        ),
        patch(_THRESHOLD, return_value=13000.0),
    ):
        await get_macro_overview_data(1)

    # 只斷言本次移出 event loop 的鍵；其餘 kv 讀取依設計仍在 loop 上
    for key in _MACRO_KEYS:
        threads = [thread for read_key, thread in reads if read_key == key]
        assert threads, f"{key} 未被讀取"
        assert loop_thread not in threads, f"{key} 在 event loop 執行緒上讀取"
