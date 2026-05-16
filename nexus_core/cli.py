import asyncio
import click
import logging
import os
import sys

# 確保環境變數在導入 config 之前被設定 (若 CLI 執行時有指定)
# 如果沒有指定，我們在 cli() 函數中會處理
from typing import Optional, Any
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

# 環境變數與路徑設定
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

# 設定 Logging 為 Quiet 模式，CLI 只顯示 Rich 輸出
logging.basicConfig(level=logging.ERROR)
console = Console()

class MockBot:
    """模擬 Bot 實例以符合 Service 依賴"""
    def __init__(self):
        self.user = "Nexus-CLI"
    
    async def queue_dm(self, user_id, message=None, embed=None):
        rprint(f"[bold blue]>> DM Queue to {user_id}:[/bold blue]")
        if message:
            rprint(message)
        if embed:
            rprint(Panel(embed.description or "No Description", title=embed.title))


def run_async(coro):
    """助手函數：在現有 loop 或新 loop 中執行協程"""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            # 在測試環境中，通常已經有 running loop
            return loop.create_task(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)

@click.group()
@click.option('--user-id', default=None, help='Target User ID for operations')
@click.option('--db', default=None, help='Path to SQLite database file')
@click.pass_context
def cli(ctx, user_id, db):
    """Nexus Seeker Professional CLI Terminal"""
    if db:
        os.environ['NEXUS_DB_NAME'] = db
    
    import database
    import config
    from services.trading_service import TradingService
    
    database.init_db()
    
    ctx.ensure_object(dict)
    ctx.obj['user_id'] = int(user_id) if user_id else config.DISCORD_ADMIN_USER_ID
    ctx.obj['bot'] = MockBot()
    ctx.obj['trading_service'] = TradingService(ctx.obj['bot'])

@cli.command()
@click.pass_context
def health(ctx):
    """檢查系統健康度與市場狀態"""
    from services.market_data_service import get_macro_environment, get_quote
    async def _run():
        with console.status("[bold green]正在獲取市場狀態..."):
            macro = await get_macro_environment()
            spy = await get_quote("SPY")
        
        table = Table(title="🌐 Nexus Seeker 系統健康度")
        table.add_column("指標", style="cyan")
        table.add_column("數值", style="magenta")
        
        table.add_row("VIX Index", f"{macro.get('vix', 'N/A')}")
        table.add_row("SPY Price", f"${spy.get('c', 'N/A')}")
        table.add_row("User ID", str(ctx.obj['user_id']))
        
        console.print(table)
    run_async(_run())

@cli.command()
@click.argument('symbol')
@click.pass_context
def quote(ctx, symbol):
    """獲取標的即時報價"""
    from services.market_data_service import get_quote
    async def _run():
        symbol_upper = symbol.upper()
        with console.status(f"[bold green]正在查詢 {symbol_upper}..."):
            data = await get_quote(symbol_upper)
        
        if not data or data.get('c') == 0:
            rprint(f"[bold red]❌ 無法獲取 {symbol_upper} 報價[/bold red]")
            return

        panel = Panel(
            f"現價: [bold green]${data['c']}[/bold green]\n"
            f"漲跌: {data['d']} ({data['dp']}%)\n"
            f"今日高低: ${data['h']} / ${data['l']}",
            title=f"📊 {symbol_upper} 即時報價",
            expand=False
        )
        console.print(panel)
    run_async(_run())

@cli.command()
@click.pass_context
def scan_ddp(ctx):
    """執行全站 DDP 掃描"""
    async def _run():
        from database.watchlist import get_all_watchlist
        all_watch = get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watch)))
        
        if not symbols:
            rprint("[yellow]觀察清單為空。[/yellow]")
            return

        rprint(f"正在掃描 {len(symbols)} 個標的...")
        service = ctx.obj['trading_service']
        results = await service.run_ddp_scan(symbols)
        
        if not results:
            rprint("[bold blue]🔎 掃描完成，未發現符合 DDP 條件的標的。[/bold blue]")
            return

        for res in results:
            rprint(Panel(f"標的: {res['symbol']}\n原因: {res.get('reason', 'N/A')}", title="🎯 DDP 訊號偵測"))
    run_async(_run())

@cli.command()
@click.pass_context
def portfolio(ctx):
    """查看持倉未實現損益"""
    async def _run():
        uid = ctx.obj['user_id']
        service = ctx.obj['trading_service']
        
        with console.status("[bold green]正在計算損益..."):
            data = await service.get_portfolio_pnl(uid)
        
        if not data['trades']:
            rprint("[yellow]目前無持倉紀錄。[/yellow]")
            return

        table = Table(title=f"📦 {uid} 持倉未實現損益報告")
        table.add_column("ID", style="dim")
        table.add_column("標的")
        table.add_column("數量")
        table.add_column("成本")
        table.add_column("現價")
        table.add_column("損益 (USD)", justify="right")
        table.add_column("幅度 (%)", justify="right")

        for t in data['trades']:
            color = "green" if t['unrealized_pnl'] >= 0 else "red"
            table.add_row(
                str(t['id']),
                t['symbol'],
                str(t['quantity']),
                f"${t['entry_price']:.2f}",
                f"${t['current_price']:.2f}",
                f"[{color}]${t['unrealized_pnl']:,.2f}[/{color}]",
                f"[{color}]{t['pnl_pct']:.2%}[/{color}]"
            )
        
        table.add_section()
        total_color = "green" if data['total_unrealized_pnl'] >= 0 else "red"
        table.add_row(
            "", "TOTAL", "", "", "",
            f"[bold {total_color}]${data['total_unrealized_pnl']:,.2f}[/bold {total_color}]",
            ""
        )
        
        console.print(table)
    run_async(_run())

if __name__ == "__main__":
    cli()
