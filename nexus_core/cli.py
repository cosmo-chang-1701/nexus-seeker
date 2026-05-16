import asyncio
import click
import logging
import os
import sys
import json
from unittest.mock import MagicMock
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint
from dotenv import load_dotenv

# 環境變數與路徑設定
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

# 設定 Logging 為 Quiet 模式
logging.basicConfig(level=logging.ERROR)
console = Console()


class MockBot:
    """模擬 Bot 實例以符合 Service 依賴"""

    def __init__(self):
        self.user = MagicMock()
        self.user.id = 0
        self.user.name = "Nexus-CLI"

    async def queue_dm(self, user_id, message=None, embed=None):
        rprint(f"[bold blue]>> [DM Queue] to {user_id}:[/bold blue]")
        if message:
            rprint(message)
        if embed:
            title = getattr(embed, "title", "No Title")
            desc = getattr(embed, "description", "No Description")
            rprint(Panel(desc, title=f"📊 {title}"))


def run_async(coro):
    """助手函數：在現有 loop 或新 loop 中執行協程"""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            return loop.create_task(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


@click.group()
@click.option("--user-id", default=None, help="Target User ID for operations")
@click.option("--db", default=None, help="Path to SQLite database file")
@click.pass_context
def cli(ctx, user_id, db):
    """🌌 Nexus Seeker Professional CLI Terminal"""
    if db:
        os.environ["NEXUS_DB_NAME"] = db

    import database
    import config
    from services.trading_service import TradingService

    database.init_db()

    ctx.ensure_object(dict)
    ctx.obj["user_id"] = int(user_id) if user_id else config.DISCORD_ADMIN_USER_ID
    ctx.obj["bot"] = MockBot()
    ctx.obj["trading_service"] = TradingService(ctx.obj["bot"])


# ==========================================
# 1. Config & System Group
# ==========================================
@cli.group(name="sys")
def sys_group():
    """系統管理與環境狀態"""
    pass


@sys_group.command(name="health")
@click.pass_context
def health(ctx):
    """檢查系統健康度與市場狀態"""
    from services.market_data_service import get_macro_environment, get_quote

    async def _run():
        with console.status("[bold green]正在獲獲取市場狀態..."):
            macro = await get_macro_environment()
            spy = await get_quote("SPY")

        table = Table(title="🌐 Nexus Seeker 系統健康度")
        table.add_column("指標", style="cyan")
        table.add_column("數值", style="magenta")

        table.add_row("VIX Index", f"{macro.get('vix', 'N/A')}")
        table.add_row("SPY Price", f"${spy.get('c', 'N/A')}")
        table.add_row("User ID", str(ctx.obj["user_id"]))

        console.print(table)

    run_async(_run())


@sys_group.command(name="settings")
@click.option("--capital", type=float, help="全域帳戶資金")
@click.option("--risk-limit", type=float, help="單標的風險上限 (%)")
@click.option("--alert-mode", type=int, help="警報模式 (0=OFF, 1=ALL, 2=PORTFOLIO)")
@click.pass_context
def settings(ctx, capital, risk_limit, alert_mode):
    """查看或更新帳戶設定"""
    import database

    uid = ctx.obj["user_id"]

    if capital is not None or risk_limit is not None or alert_mode is not None:
        kwargs = {}
        if capital is not None:
            kwargs["capital"] = capital
        if risk_limit is not None:
            kwargs["risk_limit"] = risk_limit
        if alert_mode is not None:
            kwargs["option_alert_mode"] = alert_mode

        database.update_user_settings(uid, **kwargs)
        rprint("[bold green]✅ 帳戶設定已更新。[/bold green]")

    u_ctx = database.get_full_user_context(uid)
    table = Table(title=f"⚙️ 用戶 {uid} 配置")
    table.add_column("設定項")
    table.add_column("當前值")
    table.add_row("帳戶資金", f"${u_ctx.capital:,.0f}")
    table.add_row("風險上限", f"{u_ctx.risk_limit}%")
    table.add_row("警報模式", str(u_ctx.option_alert_mode))
    console.print(table)


# ==========================================
# 2. Watchlist Group
# ==========================================
@cli.group(name="watch")
def watch_group():
    """雷達觀察清單管理"""
    pass


@watch_group.command(name="add")
@click.argument("symbol")
@click.option("--llm", is_flag=True, default=True, help="是否啟用 AI 分析")
@click.pass_context
def watch_add(ctx, symbol, llm):
    """將標的加入觀察清單"""
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(ctx.obj["user_id"], symbol.upper(), use_llm=llm)
    rprint(f"[bold green]✅ 已將 {symbol.upper()} 加入觀察清單。[/bold green]")


@watch_group.command(name="list")
@click.pass_context
def watch_list(ctx):
    """列出您的觀察清單"""
    import database

    all_watch = database.get_all_watchlist()
    uid = ctx.obj["user_id"]
    user_watch = [row for row in all_watch if row[0] == uid]

    if not user_watch:
        rprint("[yellow]觀察清單為空。[/yellow]")
        return

    table = Table(title=f"🔭 {uid} 觀察清單")
    table.add_column("標的", style="cyan")
    table.add_column("AI 分析", style="magenta")
    for _, sym, meta_json in user_watch:
        meta = json.loads(meta_json) if meta_json else {}
        table.add_row(sym, "Enabled" if meta.get("use_llm", True) else "Disabled")
    console.print(table)


@watch_group.command(name="remove")
@click.argument("symbol")
@click.pass_context
def watch_remove(ctx, symbol):
    """從觀察清單移除標的"""
    from database.watchlist import remove_watchlist_symbol

    remove_watchlist_symbol(ctx.obj["user_id"], symbol.upper())
    rprint(f"[bold red]🗑️ 已移除 {symbol.upper()}。[/bold red]")


# ==========================================
# 3. Portfolio & Trading Group
# ==========================================
@cli.group(name="pf")
def portfolio_group():
    """持倉與損益管理"""
    pass


@portfolio_group.command(name="pnl")
@click.pass_context
def portfolio_pnl(ctx):
    """查看持倉未實現損益"""
    uid = ctx.obj["user_id"]
    service = ctx.obj["trading_service"]

    async def _run():
        with console.status("[bold green]正在計算損益..."):
            data = await service.get_portfolio_pnl(uid)

        if not data["trades"]:
            rprint("[yellow]目前無持倉紀錄。[/yellow]")
            return

        table = Table(title=f"📦 {uid} 持倉報告")
        table.add_column("ID", style="dim")
        table.add_column("標的")
        table.add_column("數量")
        table.add_column("成本")
        table.add_column("現價")
        table.add_column("損益 (USD)", justify="right")
        table.add_column("幅度 (%)", justify="right")

        for t in data["trades"]:
            color = "green" if t["unrealized_pnl"] >= 0 else "red"
            table.add_row(
                str(t["id"]),
                t["symbol"],
                str(t["quantity"]),
                f"${t['entry_price']:.2f}",
                f"${t['current_price']:.2f}",
                f"[{color}]${t['unrealized_pnl']:,.2f}[/{color}]",
                f"[{color}]{t['pnl_pct']:.2%}[/{color}]",
            )
        table.add_section()
        total_color = "green" if data["total_unrealized_pnl"] >= 0 else "red"
        table.add_row(
            "",
            "TOTAL",
            "",
            "",
            "",
            f"[bold {total_color}]${data['total_unrealized_pnl']:,.2f}[/bold {total_color}]",
            "",
        )
        console.print(table)

    run_async(_run())


@portfolio_group.command(name="runway")
@click.pass_context
def runway_check(ctx):
    """執行財務生存跑道分析"""
    from market_analysis.portfolio import calculate_financial_runway
    import database

    uid = ctx.obj["user_id"]
    u_ctx = database.get_full_user_context(uid)

    from services.asset_manager import AssetManager
    from models.asset import ContextType

    manager = AssetManager()
    assets = manager.get_assets(uid, ContextType.TRADE)
    total_theta = sum(a.metadata.get("theta", 0.0) for a in assets)

    runway = calculate_financial_runway(
        cash_reserve=u_ctx.cash_reserve,
        monthly_expense=u_ctx.monthly_expense,
        daily_theta=total_theta,
    )

    panel = Panel(
        f"現金儲備: ${u_ctx.cash_reserve:,.0f}\n"
        f"每月支出: ${u_ctx.monthly_expense:,.0f}\n"
        f"組合每日 Theta: ${total_theta:,.2f}\n"
        f"[bold cyan]預計財務跑道: {runway:,.1f} 天[/bold cyan]",
        title="🏁 Financial Runway Analysis",
    )
    console.print(panel)


# ==========================================
# 4. Market & Analysis Group
# ==========================================
@cli.group(name="mkt")
def market_group():
    """市場行情與量化掃描"""
    pass


@market_group.command(name="quote")
@click.argument("symbol")
@click.pass_context
def market_quote(ctx, symbol):
    """獲取標的即時報價"""
    from services.market_data_service import get_quote

    async def _run():
        symbol_upper = symbol.upper()
        with console.status(f"[bold green]正在查詢 {symbol_upper}..."):
            data = await get_quote(symbol_upper)
        if not data or data.get("c") == 0:
            rprint(f"[bold red]❌ 無法獲取 {symbol_upper} 報價[/bold red]")
            return
        panel = Panel(
            f"現價: [bold green]${data['c']}[/bold green]\n"
            f"漲跌: {data['d']} ({data['dp']}%)\n"
            f"今日高低: ${data['h']} / ${data['l']}",
            title=f"📊 {symbol_upper} 即時報價",
            expand=False,
        )
        console.print(panel)

    run_async(_run())


@market_group.command(name="ddp")
@click.pass_context
def market_ddp(ctx):
    """執行全站 DDP 掃描"""

    async def _run():
        from database.watchlist import get_all_watchlist

        all_watch = get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watch)))
        if not symbols:
            rprint("[yellow]觀察清單為空。[/yellow]")
            return
        rprint(f"正在對 {len(symbols)} 個標的執行 DDP 掃描...")
        service = ctx.obj["trading_service"]
        results = await service.run_ddp_scan(symbols)
        if not results:
            rprint("[bold blue]🔎 未發現符合 DDP 條件的標的。[/bold blue]")
            return
        for res in results:
            rprint(
                Panel(
                    f"標的: {res['symbol']}\nP/E: {res.get('pe', 'N/A')}\nEPS Growth: {res.get('eps_growth', 'N/A')}%",
                    title="🎯 DDP 訊號",
                )
            )

    run_async(_run())


@market_group.command(name="skew")
@click.argument("symbol")
@click.pass_context
def market_skew(ctx, symbol):
    """執行 Skew 偏斜掃描"""
    from market_analysis.sentiment_engine import SentimentEngine

    async def _run():
        with console.status(f"正在分析 {symbol.upper()} 情緒..."):
            res = await SentimentEngine.calculate_skew(symbol.upper())
        rprint(
            Panel(
                f"Skew 分位點: {res['skew']}%\n狀態: {res['state']}",
                title=f"📐 {symbol.upper()} Skew Analysis",
            )
        )

    run_async(_run())


# ==========================================
# 5. Admin Group
# ==========================================
@cli.group(name="admin")
def admin_group():
    """管理員工具 (需權限)"""
    pass


@admin_group.command(name="force-scan")
@click.pass_context
def force_scan(ctx):
    """立即執行全站掃描"""
    service = ctx.obj["trading_service"]
    rprint("[bold yellow]🚀 啟動全站強制掃描... (這可能需要幾分鐘)[/bold yellow]")

    async def _run():
        import database

        all_watch = database.get_all_watchlist()
        symbols = list(set(row[1] for row in all_watch))
        await service.run_ddp_scan(symbols)
        rprint("[bold green]✅ 掃描完成。[/bold green]")

    run_async(_run())


if __name__ == "__main__":
    cli()
