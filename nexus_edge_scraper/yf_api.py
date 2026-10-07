from typing import Any, Dict, List, Optional
import asyncio
import threading
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

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


async def fetch_last_close(symbol: str) -> Optional[float]:
    """最近一個完整交易日的收盤價 (收盤後呼叫即為當日收盤)。背景執行緒執行。"""

    def _fetch() -> Optional[float]:
        df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if df is None or df.empty:
            return None
        closes = df["Close"].dropna()
        return float(closes.iloc[-1]) if not closes.empty else None

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


# history 端點同時最多 2 個 ticker.history 在跑：路由已改同步 def（FastAPI 丟 threadpool，
# 預設約 40 執行緒），若不設上限，core 端一輪 watchlist 併發會同時對 Yahoo 打出數十條請求，
# 極易觸發 429。2 與 core 端背景 Semaphore(2) 對齊。
_HISTORY_SEMAPHORE = threading.BoundedSemaphore(2)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """判斷例外是否為 Yahoo 限流（YFRateLimitError 或訊息含 Too Many Requests／429）。"""
    if isinstance(exc, YFRateLimitError):
        return True
    msg = str(exc).lower()
    return "too many requests" in msg or "429" in msg


@router.get("/api/v1/scrape/yf/history/{symbol}", response_model=None)
def scrape_yf_history(
    symbol: str, period: str = "1y", interval: str = "1d", auto_adjust: bool = True
) -> Dict[str, Any] | JSONResponse:
    # 刻意用同步 def：ticker.history() 是阻塞 I/O，原本 async def 內直接呼叫會卡死整個
    # edge event loop（含背景期權輪詢）；改 def 後 FastAPI 自動丟 threadpool。
    try:
        ticker = yf.Ticker(symbol)
        with _HISTORY_SEMAPHORE:
            try:
                df = ticker.history(
                    period=period,
                    interval=interval,
                    auto_adjust=auto_adjust,
                    repair=True,
                )
            except Exception as first_exc:
                # 限流不應再以 repair=False 重打一次（只會加重 429）
                if _is_rate_limit_error(first_exc):
                    raise
                df = ticker.history(
                    period=period,
                    interval=interval,
                    auto_adjust=auto_adjust,
                    repair=False,
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
        if _is_rate_limit_error(e):
            # 回 429 讓 core 端啟動統一冷卻，且不改用資料中心 IP 直連
            return JSONResponse(status_code=429, content={"status": "rate_limited"})
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
