from typing import Any
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from cogs.terminal import TerminalCog
from cogs.sentiment import SentimentCog
from cogs.hedging import HedgingCog
from cogs.trading.scanner_commands import ScannerCommandsCog
from cogs.trading.admin_commands import AdminCommandsCog
from cogs.intelligence import IntelligenceCog
from cogs.calendar import CalendarCog
from cogs.unified_terminal import UnifiedTerminalCog
from database.user_settings import get_full_user_context


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_command_settings(mock_interaction: Any, db_conn: Any):  # type: ignore
    bot = MagicMock()
    cog = TerminalCog(bot)

    # Execute /settings command using .callback
    await cog.update_settings.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        risk_limit=15.0,
        enable_vtr=True,
    )

    # Verify response
    mock_interaction.followup.send.assert_called_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "帳戶設定已更新" in kwargs["embed"].description
    assert "🛡️ 風險限制: `15.0%`" in kwargs["embed"].description

    # Verify database update
    context = get_full_user_context(mock_interaction.user.id)
    assert context.risk_limit == 15.0
    assert context.enable_vtr is True


@pytest.mark.asyncio
async def test_command_add_holding(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    bot = MagicMock()
    cog = TerminalCog(bot)

    # Execute /add_holding
    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="AAPL",
        quantity=10,
        avg_cost=150.0,
    )

    mock_interaction.followup.send.assert_called_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "現貨持倉已登錄" in kwargs["embed"].description

    # Verify DB
    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_command_edit_holding_sets_allocation_and_class(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """
    /edit_holding 應能持久化 asset_class / max_allocation_pct / target_allocation_pct /
    boxx_allocation_pct，並可透過 database.get_user_holdings() 讀回（供動態轉倉引擎
    Scenario 3/5 使用）。
    """
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="NVDA",
        quantity=10,
        avg_cost=100.0,
    )

    await cog.edit_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="NVDA",
        asset_class=discord.app_commands.Choice(name="SATELLITE", value="SATELLITE"),
        max_allocation_pct=30.0,
        target_allocation_pct=15.0,
        boxx_allocation_pct=70.0,
    )

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    nvda = next(h for h in holdings if h["symbol"] == "NVDA")
    assert nvda["asset_class"] == "SATELLITE"
    assert nvda["max_allocation_pct"] == pytest.approx(0.30)
    assert nvda["target_allocation_pct"] == pytest.approx(0.15)
    assert nvda["boxx_allocation_pct"] == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_command_edit_holding_rejects_invalid_boxx_allocation_pct(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """BOXX 防禦閾值超出 (0, 100] 邊界時應被拒絕，不應寫入資料庫。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="AMZN",
        quantity=10,
        avg_cost=100.0,
    )

    await cog.edit_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="AMZN",
        boxx_allocation_pct=150.0,
    )

    mock_interaction.response.send_message.assert_called_once()
    args, kwargs = mock_interaction.response.send_message.call_args
    assert "介於" in kwargs["embed"].description

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    amzn = next(h for h in holdings if h["symbol"] == "AMZN")
    assert amzn["boxx_allocation_pct"] is None


@pytest.mark.asyncio
async def test_command_list_holdings_shows_target_allocation_suggestion(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """CORE 持倉未設定 target_allocation_pct 時，/list_holdings 應顯示總經自動
    建議值作為參考（僅供顯示，不會自動套用生效，仍需使用者自行以 /edit_holding
    設定才會真正影響核心資金部署引擎行為）。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="VOO",
        quantity=10,
        avg_cost=400.0,
    )

    mock_interaction.followup.send.reset_mock()

    with patch(
        "market_analysis.index_microstructure.suggest_target_allocation_pct",
        new_callable=AsyncMock,
        return_value=50.0,
    ):
        await cog.list_holdings.callback(cog, mock_interaction)  # type: ignore

    mock_interaction.followup.send.assert_called_once()
    args, kwargs = mock_interaction.followup.send.call_args
    hint_fields = [f for f in kwargs["embed"].fields if "核心資金部署建議" in f.name]
    assert len(hint_fields) == 1
    assert "VOO" in hint_fields[0].value
    assert "50%" in hint_fields[0].value


@pytest.mark.asyncio
async def test_command_edit_holding_rejects_target_above_max(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """目標配置比例大於配置上限時應被拒絕，不應寫入資料庫。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="AMD",
        quantity=10,
        avg_cost=100.0,
    )

    await cog.edit_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="AMD",
        max_allocation_pct=20.0,
        target_allocation_pct=50.0,
    )

    mock_interaction.response.send_message.assert_called_once()
    args, kwargs = mock_interaction.response.send_message.call_args
    assert "不可大於" in kwargs["embed"].description

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    amd = next(h for h in holdings if h["symbol"] == "AMD")
    assert amd["max_allocation_pct"] is None


@pytest.mark.asyncio
async def test_command_add_holding_sets_acquired_at(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """新建持倉時應自動記錄今日為建倉日期，供動態轉倉引擎稅務提醒粗估使用。"""
    from datetime import datetime

    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="MSFT",
        quantity=5,
        avg_cost=300.0,
    )

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    msft = next(h for h in holdings if h["symbol"] == "MSFT")
    assert msft["acquired_at"] == datetime.now().strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_command_edit_holding_backfills_acquired_at(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """/edit_holding 應能回填校正真實建倉日期 (例如早於首次登錄機器人的日期)。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="GOOGL",
        quantity=5,
        avg_cost=140.0,
    )
    await cog.edit_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="GOOGL",
        acquired_at="2022-01-15",
    )

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    googl = next(h for h in holdings if h["symbol"] == "GOOGL")
    assert googl["acquired_at"] == "2022-01-15"


@pytest.mark.asyncio
async def test_command_edit_holding_rejects_invalid_acquired_at_format(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="TSLA",
        quantity=5,
        avg_cost=200.0,
    )
    await cog.edit_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="TSLA",
        acquired_at="15/01/2022",
    )

    mock_interaction.response.send_message.assert_called_once()
    args, kwargs = mock_interaction.response.send_message.call_args
    assert "格式錯誤" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_command_add_holding_with_full_config_params(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """/add_holding 應能在建倉當下一次帶入 asset_class / max_allocation_pct /
    target_allocation_pct / boxx_allocation_pct / acquired_at，不需再另外呼叫
    /edit_holding 補設定 (供動態轉倉引擎 Scenario 3/5 使用)。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="META",
        quantity=10,
        avg_cost=300.0,
        asset_class=discord.app_commands.Choice(name="SATELLITE", value="SATELLITE"),
        max_allocation_pct=30.0,
        target_allocation_pct=15.0,
        boxx_allocation_pct=70.0,
        acquired_at="2022-06-01",
    )

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    meta = next(h for h in holdings if h["symbol"] == "META")
    assert meta["asset_class"] == "SATELLITE"
    assert meta["max_allocation_pct"] == pytest.approx(0.30)
    assert meta["target_allocation_pct"] == pytest.approx(0.15)
    assert meta["boxx_allocation_pct"] == pytest.approx(0.70)
    assert meta["acquired_at"] == "2022-06-01"


@pytest.mark.asyncio
async def test_command_add_holding_upsert_merges_config_params(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """對既有持倉再次呼叫 /add_holding 時，新提供的欄位應合併寫入，未提供的既有
    欄位應保留不被清空 (Upsert 分支)。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="NFLX",
        quantity=10,
        avg_cost=400.0,
        asset_class=discord.app_commands.Choice(name="CORE", value="CORE"),
    )

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="NFLX",
        quantity=20,
        avg_cost=420.0,
        max_allocation_pct=40.0,
    )

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    nflx = next(h for h in holdings if h["symbol"] == "NFLX")
    assert nflx["quantity"] == 20
    assert nflx["avg_cost"] == 420.0
    assert nflx["max_allocation_pct"] == pytest.approx(0.40)
    # asset_class 未在第二次呼叫中提供，應維持第一次設定的值不被清空
    assert nflx["asset_class"] == "CORE"


@pytest.mark.asyncio
async def test_command_add_holding_rejects_invalid_boxx_allocation_pct(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    """/add_holding 建倉當下帶入的配置參數也應套用與 /edit_holding 相同的驗證規則。"""
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.add_holding.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        symbol="ORCL",
        quantity=10,
        avg_cost=100.0,
        boxx_allocation_pct=150.0,
    )

    mock_interaction.followup.send.assert_called_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "介於" in kwargs["embed"].description

    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    assert not any(h["symbol"] == "ORCL" for h in holdings)


@pytest.mark.asyncio
async def test_command_skew_scan(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    bot = MagicMock()
    cog = SentimentCog(bot)

    # Mock all 4 tasks called in gather
    with (
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
        ) as mock_pcr,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
            new_callable=AsyncMock,
        ) as mock_uoa,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
    ):
        mock_skew.return_value = {"symbol": "SPY", "skew": 5.0, "state": "Normal"}
        mock_pcr.return_value = {"symbol": "SPY", "pcr": 0.8, "state": "Normal"}
        mock_uoa.return_value = []
        mock_mp.return_value = {"max_pain": 500}

        await cog.skew_scan.callback(cog, mock_interaction, symbol="SPY")  # type: ignore

        mock_interaction.followup.send.assert_called_once()
        assert "embed" in mock_interaction.followup.send.call_args[1]


@pytest.mark.asyncio
async def test_command_ddp_scan(
    mock_interaction: Any, db_conn: Any, mock_market_data: Any
) -> None:
    bot = MagicMock()
    cog = ScannerCommandsCog(bot)

    # Add something to watchlist
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(mock_interaction.user.id, "TSLA")

    with patch(
        "market_analysis.ddp_inspector.DDPInspector.run_scan", new_callable=AsyncMock
    ) as mock_scan:
        mock_scan.return_value = [
            {
                "symbol": "TSLA",
                "signal": "BULLISH",
                "current_pe": 30.0,
                "pe_mean_3y": 45.0,
                "eps_growth": 0.2,
                "rev_accel": True,
                "confidence_score": 0.85,
                "forward_pe": 25.0,
            }
        ]

        await cog.ddp_scan.callback(cog, mock_interaction)  # type: ignore
        assert (
            mock_interaction.response.send_message.called
            or mock_interaction.followup.send.called
        )


@pytest.mark.asyncio
async def test_command_poly_list(mock_interaction: Any, db_conn: Any):  # type: ignore
    bot = MagicMock()
    bot.polymarket_service = MagicMock()
    bot.polymarket_service.get_active_markets.return_value = [
        {"title": "Test Market", "price": 0.5}
    ]

    cog = IntelligenceCog(bot)
    await cog.poly_list.callback(cog, mock_interaction)  # type: ignore

    mock_interaction.followup.send.assert_called_once()
    assert "embed" in mock_interaction.followup.send.call_args[1]


@pytest.mark.asyncio
async def test_command_settle_hedge(mock_interaction: Any, db_conn: Any):  # type: ignore
    cursor = db_conn.cursor()
    # Correct columns for hedge_alerts
    # vix_level is at index 2, hedge_contracts at index 7, status at index 10
    # Wait, let's check the schema again to be sure of indices
    # (id, user_id, vix_level, vix_stage_move, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, narration, status, created_at, executed_at)
    cursor.execute(
        """
        INSERT INTO hedge_alerts (user_id, vix_level, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, status)
        VALUES (?, 20.0, 10.0, 50.0, 'SPY', 10, 'Hedge instructions', 'PENDING')
    """,
        (mock_interaction.user.id,),
    )
    alert_id = cursor.lastrowid
    db_conn.commit()

    bot = MagicMock()
    cog = HedgingCog(bot)

    await cog.settle_hedge.callback(  # type: ignore
        cog,  # type: ignore
        mock_interaction,
        alert_id=alert_id,
        actual_qty=12,
    )

    mock_interaction.followup.send.assert_called_once()
    assert "embed" in mock_interaction.followup.send.call_args[1]

    cursor.execute(
        "SELECT status, hedge_contracts FROM hedge_alerts WHERE id = ?", (alert_id,)
    )
    row = cursor.fetchone()
    assert row[0] == "EXECUTED"
    assert row[1] == 12


@pytest.mark.asyncio
async def test_command_hedge_list(mock_interaction: Any, db_conn: Any):  # type: ignore
    cursor = db_conn.cursor()
    cursor.execute(
        """
        INSERT INTO hedge_alerts (user_id, vix_level, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, status)
        VALUES (?, 22.5, 15.0, 40.0, 'SPY', 8, 'Hedge instructions', 'PENDING')
    """,
        (mock_interaction.user.id,),
    )
    db_conn.commit()

    bot = MagicMock()
    cog = HedgingCog(bot)

    await cog.hedge_list.callback(cog, mock_interaction)  # type: ignore

    mock_interaction.followup.send.assert_called_once()
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "最近對沖警報列表" in embed.title
    assert "#1" in embed.description
    assert "22.50" in embed.description


@pytest.mark.asyncio
async def test_command_vtr_stats(mock_interaction: Any, db_conn: Any):  # type: ignore
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.vtr_stats.callback(cog, mock_interaction)  # type: ignore
    assert (
        mock_interaction.response.send_message.called
        or mock_interaction.followup.send.called
    )


@pytest.mark.asyncio
async def test_command_sys_health(mock_interaction: Any):  # type: ignore
    bot = MagicMock()
    cog = TerminalCog(bot)

    with (
        patch("psutil.virtual_memory") as mock_mem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.cpu_percent") as mock_cpu,
    ):
        # Case 1: Healthy
        mock_mem.return_value.percent = 50.0
        mock_mem.return_value.available = 512 * 1024 * 1024
        mock_disk.return_value.percent = 40.0
        mock_disk.return_value.free = 10 * 1024 * 1024 * 1024
        mock_cpu.return_value = 10.0

        await cog.sys_health.callback(cog, mock_interaction)  # type: ignore
        mock_interaction.followup.send.assert_called()
        args, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]
        assert "✅ **狀態優良**" in embed.fields[-1].value
        assert discord.Color.green() == embed.color

        # Case 2: Disk Full Danger
        mock_interaction.followup.send.reset_mock()
        mock_disk.return_value.percent = 96.0
        await cog.sys_health.callback(cog, mock_interaction)  # type: ignore
        args, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]
        assert "🆘 **極度危險 (OOM 警告)**" in embed.fields[-1].value
        assert discord.Color.red() == embed.color


@pytest.mark.asyncio
async def test_all_commands_structure(
    mock_interaction: Any, db_conn: Any, mock_bot: Any
) -> None:
    """
    Smoke test to ensure command callbacks are structurally correct and compatible with parameters.
    """
    terminal = TerminalCog(mock_bot)
    sentiment = SentimentCog(mock_bot)
    hedging = HedgingCog(mock_bot)
    scanner = ScannerCommandsCog(mock_bot)
    admin = AdminCommandsCog(mock_bot)
    intelligence = IntelligenceCog(mock_bot)
    calendar = CalendarCog(mock_bot)
    unified = UnifiedTerminalCog(mock_bot)

    # --- Terminal Commands ---
    await terminal.update_settings.callback(terminal, mock_interaction, risk_limit=25.0)  # type: ignore
    assert (
        "帳戶設定已更新"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )
    mock_interaction.followup.send.reset_mock()

    await terminal.add_watch.callback(  # type: ignore
        terminal,  # type: ignore
        mock_interaction,
        symbol="NVDA",
    )
    assert (
        "已加入觀察清單"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )
    mock_interaction.followup.send.reset_mock()

    await terminal.list_watch.callback(terminal, mock_interaction)  # type: ignore
    assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    with patch("psutil.virtual_memory") as mem, patch(
        "psutil.disk_usage"
    ) as disk, patch("psutil.cpu_percent") as cpu:
        mem.return_value.percent = 40.0
        mem.return_value.available = 1024 * 1024 * 1024
        disk.return_value.percent = 30.0
        disk.return_value.free = 50 * 1024 * 1024 * 1024
        cpu.return_value = 5.0
        await terminal.sys_health.callback(terminal, mock_interaction)  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    await terminal.notif_settings.callback(terminal, mock_interaction)  # type: ignore
    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    kwargs = mock_interaction.followup.send.call_args.kwargs
    assert "embed" in kwargs
    assert "view" in kwargs
    from cogs.settings_ui import NotificationSettingsView

    assert isinstance(kwargs["view"], NotificationSettingsView)
    mock_interaction.response.defer.reset_mock()
    mock_interaction.followup.send.reset_mock()

    # --- Sentiment Commands ---
    with patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as m_skew, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
    ) as m_pcr, patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
    ) as m_uoa, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
    ) as m_mp:
        m_skew.return_value = {"symbol": "TSLA", "skew": 1.0, "state": "Normal"}
        m_pcr.return_value = {"symbol": "TSLA", "pcr": 1.0, "state": "Normal"}
        m_uoa.return_value = []
        m_mp.return_value = {"max_pain": 200}
        await sentiment.skew_scan.callback(sentiment, mock_interaction, symbol="TSLA")  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Intelligence Commands ---
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        # Finnhub quote requires c, d, dp, h, l, o, pc
        m_quote.return_value = {
            "c": 150.0,
            "d": 2.0,
            "dp": 1.3,
            "h": 155.0,
            "l": 145.0,
            "o": 148.0,
            "pc": 148.0,
        }
        await intelligence.quote.callback(intelligence, mock_interaction, symbol="AAPL")  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    with patch(
        "services.news_service.fetch_recent_news", new_callable=AsyncMock
    ) as m_news:
        m_news.return_value = "Test News Content"
        await intelligence.scan_news.callback(  # type: ignore
            intelligence,  # type: ignore
            mock_interaction,
            symbol="AAPL",
        )
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Calendar Commands ---
    with patch(
        "services.market_data_service.get_earnings_calendar", new_callable=AsyncMock
    ) as m_cal:
        m_cal.return_value = [{"date": "2026-05-15", "symbol": "AAPL"}]
        await calendar.calendar.callback(calendar, mock_interaction)  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Unified Terminal Commands ---
    with patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ), patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        m_quote.return_value = {"c": 500.0}
        await unified.symbol_hub.callback(unified, mock_interaction, symbol="SPY")  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Hedging Commands ---
    await hedging.hedge_list.callback(hedging, mock_interaction)  # type: ignore
    assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Trading Commands ---
    with patch(
        "market_analysis.ddp_inspector.DDPInspector.run_scan", new_callable=AsyncMock
    ) as m_ddp:
        m_ddp.return_value = []
        await scanner.ddp_scan.callback(scanner, mock_interaction)  # type: ignore
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # Test force_macro_update admin check failure
    await admin.force_macro_update.callback(admin, mock_interaction)  # type: ignore
    assert mock_interaction.response.send_message.called
    mock_interaction.response.send_message.reset_mock()

    # Test force_macro_update success with admin permission
    from config import DISCORD_ADMIN_USER_ID

    mock_interaction.user.id = DISCORD_ADMIN_USER_ID
    with patch(
        "market_analysis.index_microstructure.fetch_gex_metrics", new_callable=AsyncMock
    ) as m_gex, patch(
        "services.calendar_service.calendar_service.update_fedwatch_probability",
        new_callable=AsyncMock,
    ) as m_fw:
        m_gex.return_value = {"spy_spot": 510.0, "gamma_flip": 515.0}
        await admin.force_macro_update.callback(admin, mock_interaction)  # type: ignore
        assert mock_interaction.followup.send.called
        m_gex.assert_called_once()
        m_fw.assert_called_once()
    mock_interaction.followup.send.reset_mock()

    # Test force_macro_update marks the embed when GEX data is degraded/stale-cache
    with patch(
        "market_analysis.index_microstructure.fetch_gex_metrics", new_callable=AsyncMock
    ) as m_gex, patch(
        "services.calendar_service.calendar_service.update_fedwatch_probability",
        new_callable=AsyncMock,
    ):
        m_gex.return_value = {
            "spy_spot": 510.0,
            "gamma_flip": 515.0,
            "_is_stale_cache": True,
        }
        await admin.force_macro_update.callback(admin, mock_interaction)  # type: ignore
        sent_embed = mock_interaction.followup.send.call_args.kwargs["embed"]
        assert "使用快取資料" in sent_embed.description
    mock_interaction.followup.send.reset_mock()


@pytest.mark.asyncio
async def test_command_remove_watch(mock_interaction: Any, db_conn: Any, mock_bot: Any):  # type: ignore
    terminal = TerminalCog(mock_bot)
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(mock_interaction.user.id, "AMD")
    await terminal.remove_watch.callback(terminal, mock_interaction, symbol="AMD")  # type: ignore
    assert (
        "已移除觀察標的"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )


@pytest.mark.asyncio
async def test_command_event_impact(mock_interaction: Any, db_conn: Any, mock_bot: Any):  # type: ignore
    cal_cog = CalendarCog(mock_bot)
    from database.portfolio import add_portfolio_record

    # (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost, delta, theta, gamma, category)
    add_portfolio_record(
        mock_interaction.user.id,
        "AAPL",
        "CALL",
        150.0,
        "2026-12-17",
        5.0,
        1,
        0.0,
        0.5,
        -0.1,
        0.01,
    )

    with patch("market_analysis.greeks.calculate_vanna", return_value=0.01), patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        m_quote.return_value = {"c": 100.0}
        await cal_cog.event_impact.callback(  # type: ignore
            cal_cog,  # type: ignore
            mock_interaction,
            symbol="AAPL",
            vol_move=25.0,
        )
        assert mock_interaction.followup.send.called
