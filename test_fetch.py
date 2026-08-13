import asyncio
from nexus_core.services.reddit_service import get_reddit_context
from nexus_core.services.polymarket_service import PolymarketService
from nexus_core.database.user_settings import upsert_user_config
import os
import sys

# Ensure nexus_core is in the python path
sys.path.insert(0, os.path.abspath("nexus_core"))


class MockBot:
    def __init__(self):
        pass


async def main():
    # Enable tunnel for test
    upsert_user_config(1, enable_local_tunnel=True)

    symbol = "NVDA"
    print(f"Testing Reddit for {symbol}...")
    try:
        reddit_res = await get_reddit_context(symbol, enable_tunnel=True)
        print(f"Reddit result: {reddit_res}")
    except Exception as e:
        print(f"Reddit error: {e}")

    print(f"\nTesting Polymarket for {symbol}...")
    bot = MockBot()
    poly = PolymarketService(bot)
    try:
        poly.start()
        await asyncio.sleep(2)  # Give it time to fetch
        markets = await poly.get_market_snapshot(limit=0)

        from nexus_core.cogs.unified_terminal.utils import find_matching_polymarket_odds

        odds = await find_matching_polymarket_odds(symbol, markets)
        print(f"Polymarket matched odds: {odds}")

    except Exception as e:
        print(f"Polymarket error: {e}")
    finally:
        poly.stop()


if __name__ == "__main__":
    asyncio.run(main())
