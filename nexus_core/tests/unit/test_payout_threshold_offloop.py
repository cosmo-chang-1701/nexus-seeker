"""安全提領紅線（SQLite 讀取）須在 async 路徑上離開 event loop，且顯示不變。"""

import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders.portfolio_embeds import create_strategic_dash_embed
from cogs.unified_terminal import PortfolioHubView, UnifiedTerminalCog
from tests.unit.network_guard import NetworkGuard
from tests.unit.test_macro_spx_spy_basis import _run as run_macro_overview

_THRESHOLD = "market_analysis.trading_orchestration.get_safety_payout_threshold"
_RUNWAY = "🏁 提領跑道 (Withdrawal Runway)"


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


def _record_threads(threads: list[int], result: float) -> Callable[..., float]:
    def _side_effect(*_args: Any) -> float:
        threads.append(threading.get_ident())
        return result

    return _side_effect


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


def test_dash_embed_uses_passed_payout_threshold() -> None:
    with patch(_THRESHOLD, side_effect=AssertionError("不應同步讀取")) as mock:
        embed = create_strategic_dash_embed(
            _ctx(), {}, vix_spot=18.0, payout_threshold=16500.0
        )

    mock.assert_not_called()
    runway = next(str(f.value) for f in embed.fields if f.name == _RUNWAY)
    assert "安全提領紅線: $16,500" in re.sub(r"\x1b\[[0-9;]*m", "", runway)


@pytest.mark.asyncio
async def test_macro_overview_reads_payout_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []
    with (
        # 總經日曆預抓與 FRED 信用利差不在沿用的 patch 組內，另行替換以保持離線
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch(
            "cogs.unified_terminal.utils.get_credit_spreads",
            new_callable=AsyncMock,
            return_value={"hy": None, "ig": None},
        ),
        patch(_THRESHOLD, side_effect=_record_threads(threads, 13000.0)) as mock,
    ):
        await run_macro_overview({"^SPX": {"c": 7744.06}}, {})

    mock.assert_called_once_with()
    assert len(threads) == 1
    assert threads[0] != loop_thread


@pytest.mark.asyncio
async def test_portfolio_hub_reads_payout_off_event_loop(
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
            "services.market_data_service.get_macro_environment",
            new_callable=AsyncMock,
            return_value={"vix": 18.0},
        ),
        patch("database.get_full_user_context", return_value=_ctx()),
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
        patch(_THRESHOLD, side_effect=_record_threads(threads, 13000.0)) as mock,
    ):
        await cog.portfolio_hub.callback(cog, mock_interaction)  # type: ignore

    mock.assert_called_once_with()
    assert len(threads) == 1
    assert threads[0] != loop_thread
    mock_interaction.followup.send.assert_called_once()


@pytest.mark.asyncio
async def test_portfolio_view_btn_home_reads_payout_off_event_loop(
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
        patch("database.get_full_user_context", return_value=_ctx()),
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
        patch(_THRESHOLD, side_effect=_record_threads(threads, 13000.0)) as mock,
    ):
        await view.btn_home.callback(mock_interaction)

    mock.assert_called_once_with()
    assert len(threads) == 1
    assert threads[0] != loop_thread
    _, kwargs = mock_interaction.edit_original_response.call_args
    assert "戰略看板" in kwargs["embed"].title
