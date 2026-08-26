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


async def fetch_nearest_option_chain(symbol: str) -> Optional[Dict[str, Any]]:
    """一次性取得最近到期日的期權鏈與到期日字串。

    `yf.Ticker.option_chain(date=None)` 底層打的 `v7/finance/options/{symbol}`
    （不帶 date）本身就會回傳最近到期日的完整 calls/puts，且會把
    `expirationDates` 一併寫進該 Ticker 實例的內部快取；緊接著讀取
    `.options` 會直接命中這個內部快取、不再觸發第二次網路請求。相較於
    分別呼叫 `fetch_option_expiries()` + `fetch_option_chain_dict()`
    （各自建立新的 Ticker 實例，等於對同一份「最近到期日」資料重複打了
    兩次請求），這裡只需要一次 HTTP 請求。僅供背景排程 (scheduler.py)
    在只需要「最近到期日」時使用；`/expiries` 與 `/chain` 端點維持不變，
    供 nexus_core 查詢任意（非最近）到期日時使用。"""

    def _fetch() -> Optional[Dict[str, Any]]:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain()
        expiries = ticker.options
        if not expiries:
            return None
        expiry = expiries[0]

        calls = chain.calls.copy()
        puts = chain.puts.copy()

        if "lastTradeDate" in calls.columns:
            calls["lastTradeDate"] = calls["lastTradeDate"].astype(str)
        if "lastTradeDate" in puts.columns:
            puts["lastTradeDate"] = puts["lastTradeDate"].astype(str)

        return {
            "expiry": expiry,
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
