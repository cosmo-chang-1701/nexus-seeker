from fastapi import APIRouter, Query
import yfinance as yf

router = APIRouter()


@router.get("/api/v1/scrape/yf/history/{symbol}")
async def scrape_yf_history(symbol: str, period: str = "1y", interval: str = "1d"):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            period=period, interval=interval, auto_adjust=True, repair=True
        )
        if df is None or df.empty:
            return {"status": "error", "data": "empty"}

        # Reset index to make Date a column, then convert to dict
        df = df.reset_index()
        # Convert datetime to string
        if "Date" in df.columns:
            df["Date"] = df["Date"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
        elif "Datetime" in df.columns:
            df["Datetime"] = df["Datetime"].dt.strftime("%Y-%m-%d %H:%M:%S%z")

        data = df.to_dict(orient="records")
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/scrape/yf/options/{symbol}/expiries")
async def scrape_yf_options_expiries(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        expiries = ticker.options
        return {"status": "success", "data": list(expiries)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/scrape/yf/options/{symbol}/chain")
async def scrape_yf_options_chain(symbol: str, expiry: str = Query(...)):
    try:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        calls = chain.calls.copy()
        puts = chain.puts.copy()

        # Convert datetime/timestamps to strings
        if "lastTradeDate" in calls.columns:
            calls["lastTradeDate"] = calls["lastTradeDate"].astype(str)
        if "lastTradeDate" in puts.columns:
            puts["lastTradeDate"] = puts["lastTradeDate"].astype(str)

        return {
            "status": "success",
            "data": {
                "calls": calls.to_dict(orient="records"),
                "puts": puts.to_dict(orient="records"),
            },
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
