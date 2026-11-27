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
    "macro_rrp",
    "macro_fed_balance",
    "macro_fear_greed",
    "macro_gamma_flip_line",
    "macro_spy_gamma_flip",
    "macro_uer",
    "macro_sahm_rule",
    "macro_sahm_rule_date",
    "macro_rrp_change_30d",
    "macro_gex_is_fallback",
    "macro_vts_ratio",
    "macro_ted_spread",
    "macro_liquidity_is_fallback",
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
    many_calls: list[tuple[tuple[str, ...], int]] = []

    def _record_read(key: str, *_args: Any, **_kwargs: Any) -> Any:
        reads.append((key, threading.get_ident()))
        if key == "macro_spx":
            return 5000.0
        return None

    def _record_many(keys: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        many_calls.append((tuple(keys), threading.get_ident()))
        return {}

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        # 報價一律為空，使五個 `_parse` 都走 kv 回退；macro_spx 有值、macro_spy_spot 缺值則觸發 ratio 讀取
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("database.get_kv_cache", side_effect=_record_read),
        patch("database.cache.get_kv_cache_many", side_effect=_record_many),
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

    for key in _MACRO_KEYS + ("macro_spx_spy_ratio",):
        threads = [thread for read_key, thread in reads if read_key == key]
        assert threads, f"{key} 未被讀取"
        assert loop_thread not in threads, f"{key} 在 event loop 執行緒上讀取"
    assert all(
        thread != loop_thread for _k, thread in reads
    ), f"發現 event loop 上的 get_kv_cache 讀取: {reads}"
    assert many_calls, "get_kv_cache_many 未被呼叫"
    assert all(
        thread != loop_thread for _keys, thread in many_calls
    ), f"get_kv_cache_many 在 event loop 執行緒上讀取: {many_calls}"


@pytest.mark.asyncio
async def test_macro_overview_quote_exception_fallback_reads_off_event_loop() -> None:
    """`get_quote` 發生例外進入 `except` 備援路徑時，所有 KV 讀取仍須離開 event loop。"""
    loop_thread = threading.get_ident()
    reads: list[tuple[str, int]] = []
    many_calls: list[tuple[tuple[str, ...], int]] = []

    def _record_read(key: str, *_args: Any, **_kwargs: Any) -> Any:
        reads.append((key, threading.get_ident()))
        if key == "macro_spy_spot":
            return 500.0
        return None

    def _record_many(keys: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        many_calls.append((tuple(keys), threading.get_ident()))
        return {}

    with (
        patch("cogs.unified_terminal.utils.is_memory_safe", return_value=True),
        patch("cogs.unified_terminal.utils._macro_overview_cache", {}),
        patch(
            "services.market_data_service.get_quote",
            new_callable=MagicMock,
            side_effect=RuntimeError("quote failed"),
        ) as mock_get_quote,
        patch("database.get_kv_cache", side_effect=_record_read),
        patch("database.cache.get_kv_cache_many", side_effect=_record_many),
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
        await get_macro_overview_data(2)

    assert mock_get_quote.call_count == 1
    for key in _MACRO_KEYS + ("macro_spx_spy_ratio",):
        threads = [thread for read_key, thread in reads if read_key == key]
        assert threads, f"except 路徑下 {key} 未被讀取"
        assert loop_thread not in threads, f"except 路徑下 {key} 在 event loop 上讀取"
    assert all(thread != loop_thread for _k, thread in reads)
    assert many_calls
    assert all(thread != loop_thread for _keys, thread in many_calls)
