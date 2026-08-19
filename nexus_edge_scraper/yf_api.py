from typing import Any, Dict, List, Optional
import asyncio
from fastapi import APIRouter, Query
import yfinance as yf

router = APIRouter()


async def fetch_option_expiries(symbol: str) -> List[str]:
    """取得標的期權到期日清單。阻塞的 yfinance 呼叫在背景執行緒執行，
    供即時端點與背景排程 (scheduler.py) 共用，避免各自重複實作、
    也避免排程逐一輪詢多個標的時凍結 edge 自己的事件迴圈。"""

    def _fetch() -> List[str]:
        return list(yf.Ticker(symbol).options)

    return await asyncio.to_thread(_fetch)


async def fetch_option_chain_dict(symbol: str, expiry: str) -> Optional[Dict[str, Any]]:
    """取得指定到期日的完整期權鏈 (calls/puts)。阻塞的 yfinance 呼叫在背景
    執行緒執行，供即時端點與背景排程 (scheduler.py) 共用。"""

    def _fetch() -> Optional[Dict[str, Any]]:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        calls = chain.calls.copy()
        puts = chain.puts.copy()

        if "lastTradeDate" in calls.columns:
            calls["lastTradeDate"] = calls["lastTradeDate"].astype(str)
        if "lastTradeDate" in puts.columns:
            puts["lastTradeDate"] = puts["lastTradeDate"].astype(str)

        return {
            "calls": calls.to_dict(orient="records"),
            "puts": puts.to_dict(orient="records"),
        }

    return await asyncio.to_thread(_fetch)


@router.get("/api/v1/scrape/yf/history/{symbol}")
async def scrape_yf_history(
    symbol: str, period: str = "1y", interval: str = "1d"
) -> Dict[str, Any]:
    try:
        ticker = yf.Ticker(symbol)
        try:
            df = ticker.history(
                period=period, interval=interval, auto_adjust=True, repair=True
            )
        except Exception:
            df = ticker.history(
                period=period, interval=interval, auto_adjust=True, repair=False
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
async def scrape_yf_options_expiries(symbol: str) -> Dict[str, Any]:
    try:
        expiries = await fetch_option_expiries(symbol)
        return {"status": "success", "data": expiries}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/scrape/yf/options/{symbol}/chain")
async def scrape_yf_options_chain(
    symbol: str, expiry: str = Query(...)
) -> Dict[str, Any]:
    try:
        data = await fetch_option_chain_dict(symbol, expiry)
        if data is None:
            return {"status": "error", "message": "empty chain"}
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}
