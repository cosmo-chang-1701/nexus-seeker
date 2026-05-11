import asyncio
import logging

# Mocking/Importing necessary services
from services import market_data_service
from market_analysis.sentiment_engine import SentimentEngine
from config import get_vix_tier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SECTORS = {
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
}


async def gather_report_data():
    # 1. Market Snapshot
    macro = await market_data_service.get_macro_environment()
    vix = macro.get("vix", 18.0)
    vix_tier = get_vix_tier(vix)
    spy_quote = await market_data_service.get_quote("SPY")

    # 2. Sector Rotation Data
    sector_results = []
    for symbol, name in SECTORS.items():
        try:
            # Price & Vol
            df = await market_data_service.get_history_df(symbol, period="1mo")
            if df.empty:
                continue

            pct_change = (
                (df["Close"].iloc[-1] - df["Close"].iloc[-2])
                / df["Close"].iloc[-2]
                * 100
            )
            vol_current = df["Volume"].iloc[-1]
            vol_avg = df["Volume"].tail(20).mean()
            rel_vol = vol_current / vol_avg if vol_avg > 0 else 1.0

            # Sentiment (Ignore DB errors)
            try:
                skew_data = await SentimentEngine.calculate_skew(symbol)
            except Exception:
                skew_data = {"skew": 0, "state": "N/A"}

            try:
                uoa = await SentimentEngine.detect_uoa(symbol)
            except Exception:
                uoa = []

            sector_results.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "pct_change": pct_change,
                    "rel_vol": rel_vol,
                    "skew": skew_data.get("skew", 0),
                    "skew_state": skew_data.get("state", "N/A"),
                    "uoa_count": len(uoa),
                }
            )
        except Exception as e:
            logger.error(f"Error gathering data for {symbol}: {e}")

    # 3. Polymarket / Events (Fetch directly via Gamma API)
    poly_events = []
    try:
        import httpx

        GAMMA_API_BASE = "https://gamma-api.polymarket.com"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{GAMMA_API_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": 10},
            )
            if resp.status_code == 200:
                markets = resp.json()
                for m in markets[:5]:
                    poly_events.append(
                        {
                            "question": m.get("question"),
                            "outcome": m.get("outcomes"),
                            "price": m.get("outcomePrices"),
                        }
                    )
    except Exception as e:
        logger.error(f"Error fetching Polymarket data: {e}")

    # 4. Max Pain
    try:
        spy_max_pain = await SentimentEngine.calculate_max_pain("SPY")
    except Exception:
        spy_max_pain = {"error": "DB/Data Error"}

    return {
        "vix": vix,
        "vix_tier": vix_tier,
        "spy_quote": spy_quote,
        "sectors": sector_results,
        "poly_events": poly_events,
        "spy_max_pain": spy_max_pain,
    }


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    data = loop.run_until_complete(gather_report_data())
    import json

    print(json.dumps(data, indent=2, ensure_ascii=False))
