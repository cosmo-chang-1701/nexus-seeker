"""market_data_service：期權到期日與期權鏈 (Option Chain)。"""

from typing import Any, List, Optional
import asyncio
import logging
import time
from collections import namedtuple

import pandas as pd
import yfinance as yf

from services.market_data_service._core import (
    _sanitize_ticker,
    call_yf,
    get_edge_client,
)
from services.market_data_service.caches import (
    _EDGE_SNAPSHOT_MAX_AGE_SECONDS,
    _option_chain_cache,
    _OPTION_CHAIN_CACHE_TTL,
    _option_chain_fingerprint_cache,
    _option_expiries_cache,
    _OPTION_EXPIRIES_CACHE_TTL,
)

logger = logging.getLogger(__name__)


OptionChainData = namedtuple("OptionChainData", ["calls", "puts", "underlying"])


async def _retry_once(
    coro_factory: Any, *, delay: float = 0.75, label: str = ""
) -> Any:
    """對指定的協程工廠函式重試一次（短暫固定延遲後重試）。僅用於期權到期日
    /期權鏈抓取三層降級架構「單一層內部」的暫時性故障重試，非通用重試機制，
    不影響降級層級的順序或本身的 try/except 結構。"""
    try:
        return await coro_factory()
    except Exception as e:
        logger.warning(f"{label} 第一次嘗試失敗 ({e})，{delay}s 後重試一次...")
        await asyncio.sleep(delay)
        return await coro_factory()


async def get_all_option_expiries(symbol: str) -> List[str]:
    """取得該標的所有可用的期權到期日 (支援 12 小時快取)。"""
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _option_expiries_cache:
        cached_val, expiry = _option_expiries_cache[symbol]
        if now < expiry:
            return list(cached_val)

    res = []
    from config import TUNNEL_URL
    import urllib.parse

    base_url = (
        getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/") if TUNNEL_URL else ""
    )
    if base_url:
        req_url = (
            f"{base_url}/api/v1/scrape/yf/options/{urllib.parse.quote(symbol)}/expiries"
        )
        try:
            async with get_edge_client() as client:
                resp = await _retry_once(
                    lambda: client.get(req_url),
                    label=f"[{symbol}] Edge 節點抓取期權到期日",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success" and data.get("data"):
                        res = data.get("data", [])
                        logger.info(f"[{symbol}] Edge 節點成功抓取期權到期日")
        except Exception as ex:
            logger.warning(f"[{symbol}] Edge 節點即時抓取期權到期日失敗: {ex}")

    if not res:
        if base_url:
            logger.info(f"[{symbol}] 降級改用本地 yfinance 直連抓取期權到期日...")
        try:
            ticker = yf.Ticker(symbol)
            expiries = await _retry_once(
                lambda: call_yf(lambda: ticker.options),
                label=f"[{symbol}] yfinance 抓取期權到期日",
            )
            res = list(expiries)
        except Exception as e:
            logger.warning(f"[{symbol}] yfinance 獲取期權到期日失敗: {e}")

    if res:
        _option_expiries_cache[symbol] = (
            list(res),
            now + _OPTION_EXPIRIES_CACHE_TTL,
        )
    return res


async def _fetch_option_chain_raw(
    symbol: str, expiry: str, force_live: bool = False
) -> Optional[Any]:
    """底層期權鏈抓取實作（優先 Edge Snapshot 快照 -> Edge 即時 Scrape -> 本地
    yfinance 直連）。force_live=True 時完全略過 Edge Snapshot 這一層（最舊可能
    達 _EDGE_SNAPSHOT_MAX_AGE_SECONDS），直接進行 Edge 即時 scrape 或本地
    yfinance 直連，保證回傳的是即時抓取的資料，僅供已透過 Discord defer
    (不受 3 秒互動逾時限制) 的深度分析路徑使用。"""
    calls_full = None
    puts_full = None
    underlying_full: Optional[Any] = None

    # 優先讀取 edge 背景排程寫入的 Option Chain 快照（毫秒級 SQLite 讀取），
    # 命中且夠新鮮就直接採用，跳過下方的 edge 即時 scrape / yfinance 直連。
    if not force_live:
        from services import edge_cache_client

        edge_cached_chain = await edge_cache_client.get_cached_option_chain(
            symbol, expiry
        )
        if edge_cached_chain is not None:
            edge_age = edge_cached_chain.get("age_seconds")
            if edge_age is not None and edge_age < _EDGE_SNAPSHOT_MAX_AGE_SECONDS:
                edge_data = edge_cached_chain["data"]
                edge_calls = pd.DataFrame(edge_data.get("calls", []))
                edge_puts = pd.DataFrame(edge_data.get("puts", []))
                if not (edge_calls.empty and edge_puts.empty):
                    calls_full = edge_calls
                    puts_full = edge_puts
                    underlying_full = {}

    if calls_full is None or puts_full is None:
        from config import TUNNEL_URL
        import urllib.parse

        base_url = (
            getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/")
            if TUNNEL_URL
            else ""
        )
        if base_url:
            req_url = f"{base_url}/api/v1/scrape/yf/options/{urllib.parse.quote(symbol)}/chain?expiry={expiry}"
            try:
                async with get_edge_client() as client:
                    resp = await _retry_once(
                        lambda: client.get(req_url),
                        label=f"[{symbol}] Edge 節點抓取期權鏈",
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "success" and data.get("data"):
                            calls_full = pd.DataFrame(data["data"].get("calls", []))
                            puts_full = pd.DataFrame(data["data"].get("puts", []))
                            underlying_full = {}
                            logger.info(f"[{symbol}] Edge 節點成功抓取期權鏈")
            except Exception as ex:
                logger.warning(f"[{symbol}] Edge 節點即時抓取期權鏈失敗: {ex}")

        if (
            calls_full is None
            or puts_full is None
            or (calls_full.empty and puts_full.empty)
        ):
            if base_url:
                logger.info(
                    f"[{symbol}] 降級改用本地 yfinance 直連抓取期權鏈 (expiry={expiry})..."
                )
            try:
                ticker = yf.Ticker(symbol)
                chain = await _retry_once(
                    lambda: call_yf(ticker.option_chain, expiry),
                    label=f"[{symbol}] yfinance 抓取期權鏈 (expiry={expiry})",
                )
                if chain is not None:
                    calls_full = chain.calls.copy() if chain.calls is not None else None
                    puts_full = chain.puts.copy() if chain.puts is not None else None
                    underlying_full = (
                        chain.underlying.copy()
                        if hasattr(chain.underlying, "copy")
                        else chain.underlying
                    )
            except Exception as e:
                logger.warning(f"[{symbol}] 獲取期權鏈失敗 (expiry={expiry}): {e}")

    if (
        calls_full is not None
        and puts_full is not None
        and not (calls_full.empty and puts_full.empty)
    ):
        return OptionChainData(
            calls=calls_full, puts=puts_full, underlying=underlying_full
        )
    return None


async def get_option_chain(
    symbol: str,
    expiry: str,
    prune_pct: Optional[float] = 0.1,
    force_live: bool = False,
) -> Optional[Any]:
    """取得指定到期日的期權鏈 (支援 60 秒請求去重快取、SingleFlight 併發合併與
    Copy 隔離；資料新鮮度由下游 _fetch_option_chain_raw() 依 edge 快照
    age_seconds 把關，不再由本層快取 TTL 決定)。

    force_live=True 時完全略過本層的 60 秒請求去重快取讀取，並要求下游
    _fetch_option_chain_raw() 略過 Edge Snapshot 分層，保證回傳即時抓取的
    資料。僅供已透過 Discord defer（不受 3 秒互動逾時限制）的深度分析路徑
    使用；仍會把結果寫回 60 秒去重快取，讓同一視窗內的其他一般呼叫端受益於
    更新鮮的資料。"""
    symbol = _sanitize_ticker(symbol)
    cache_key = (symbol, expiry)
    now = time.time()

    async def _get_spot() -> float:
        # 延遲從套件頂層 import get_quote：讓 `patch("services.market_data_service.get_quote")`
        # 能正確攔截這裡的內部呼叫（get_quote 定義於 quote.py，跨子模組呼叫若在
        # 檔案頂層綁定會繞過套件層級的 monkeypatch，理由同 quote.py 內部處理）。
        from services.market_data_service import get_quote

        try:
            quote = await get_quote(symbol)
            return float(quote.get("c", 0.0)) if quote else 0.0
        except Exception as e:
            logger.warning(
                f"[{symbol}] Strike Pruning 報價獲取失敗 ({e})，降級為不裁減全量資料。"
            )
            return 0.0

    def _prune(
        calls: Optional[pd.DataFrame],
        puts: Optional[pd.DataFrame],
        spot: float,
        pct: Optional[float],
    ) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        if spot <= 0.0 or pct is None:
            return calls, puts
        lower = spot * (1.0 - pct)
        upper = spot * (1.0 + pct)
        if calls is not None and not calls.empty:
            calls = calls[(calls["strike"] >= lower) & (calls["strike"] <= upper)]
        if puts is not None and not puts.empty:
            puts = puts[(puts["strike"] >= lower) & (puts["strike"] <= upper)]
        return calls, puts

    cached_val = None
    if not force_live and cache_key in _option_chain_cache:
        val, expiry_time = _option_chain_cache[cache_key]
        if now < expiry_time:
            cached_val = val

    if cached_val is None:
        from services.single_flight import SingleFlightManager

        # force_live 併入 SingleFlight key，避免與同一時間非強制即時的呼叫
        # 共用同一個進行中的請求而意外拿到 Edge Snapshot 分層的舊資料。
        single_flight_key = (
            f"opt_chain_raw_{symbol}_{expiry}_{'live' if force_live else 'std'}"
        )
        cached_val = await SingleFlightManager.run(
            single_flight_key,
            _fetch_option_chain_raw,
            symbol,
            expiry,
            force_live,
        )
        if cached_val is not None:
            _option_chain_cache[cache_key] = (
                cached_val,
                now + _OPTION_CHAIN_CACHE_TTL,
            )
            try:
                fingerprint = _compute_option_chain_fingerprint(
                    cached_val.calls, cached_val.puts
                )
                prev_fingerprint = _option_chain_fingerprint_cache.get(cache_key)
                if prev_fingerprint is not None and prev_fingerprint == fingerprint:
                    logger.warning(
                        f"[{symbol}] 期權鏈 OI/IV 指紋與上次抓取完全相同，"
                        f"Yahoo 端資料本輪可能尚未真正刷新（本次抓取仍是延遲快照）。"
                    )
                _option_chain_fingerprint_cache[cache_key] = fingerprint
            except Exception as fp_err:
                logger.debug(f"[{symbol}] 期權鏈指紋比對失敗（不影響主流程）: {fp_err}")

    if cached_val is not None:
        calls_copy = cached_val.calls.copy() if cached_val.calls is not None else None
        puts_copy = cached_val.puts.copy() if cached_val.puts is not None else None

        spot_price = await _get_spot()
        calls_copy, puts_copy = _prune(calls_copy, puts_copy, spot_price, prune_pct)

        underlying_copy = (
            cached_val.underlying.copy()
            if hasattr(cached_val.underlying, "copy")
            else cached_val.underlying
        )
        return OptionChainData(
            calls=calls_copy, puts=puts_copy, underlying=underlying_copy
        )

    return None


def _compute_option_chain_fingerprint(
    calls: Optional[pd.DataFrame], puts: Optional[pd.DataFrame]
) -> tuple[Any, ...]:
    def _rows(df: Optional[pd.DataFrame]) -> tuple[Any, ...]:
        if df is None or df.empty:
            return ()
        cols = [
            c
            for c in ("strike", "openInterest", "impliedVolatility")
            if c in df.columns
        ]
        if "strike" not in cols:
            return ()
        sub = df[cols].fillna(-1.0).sort_values("strike")
        return tuple(tuple(row) for row in sub.itertuples(index=False, name=None))

    return (_rows(calls), _rows(puts))
