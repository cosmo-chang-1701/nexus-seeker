"""系統資源狀態與記憶體健康度診斷指令邏輯。"""

from typing import Any

import discord

from cogs.embed_builder import create_system_health_embed


async def sys_health_impl(bot: Any, interaction: discord.Interaction) -> Any:
    await interaction.response.defer(ephemeral=True)
    import psutil
    import os
    import httpx
    from services import market_data_service
    import config

    # 1. 取得主節點 (Droplet) 系統資源
    import platform

    main_os = platform.system()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_load = psutil.cpu_percent()
    disk = psutil.disk_usage("/")
    process = psutil.Process(os.getpid())
    proc_mem = process.memory_info().rss / (1024 * 1024)  # MB

    # 2. 取得邊緣節點 (MacBook) 系統資源
    edge_data = None
    tunnel_url = getattr(config, "TUNNEL_URL", "")
    if tunnel_url:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{tunnel_url.rstrip('/')}/api/v1/health/sys")
                if res.status_code == 200:
                    edge_data = res.json()
        except Exception:
            pass  # 忽略錯誤，視為離線或未設定

    # 3. 快取狀態
    # 注意：這裡直接存取 private 變數僅供監控
    sma_count = len(market_data_service._sma_cache)
    ema_count = len(market_data_service._ema_cache)
    poly_cache_count = 0
    orderbook_count = 0

    if hasattr(bot, "polymarket_service"):
        poly_cache_count = len(bot.polymarket_service._market_cache)
        orderbook_count = len(bot.polymarket_service._order_books)

    embed = create_system_health_embed(
        main_os=main_os,
        memory_percent=mem.percent,
        memory_available_mb=mem.available / (1024**2),
        swap_percent=swap.percent,
        cpu_percent=cpu_load,
        process_memory_mb=proc_mem,
        disk_percent=disk.percent,
        disk_free_gb=disk.free / (1024**3),
        sma_cache_size=sma_count,
        ema_cache_size=ema_count,
        poly_cache_size=poly_cache_count,
        orderbook_size=orderbook_count,
        edge_stats=edge_data,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)
