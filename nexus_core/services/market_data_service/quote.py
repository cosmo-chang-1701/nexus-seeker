"""market_data_service：即時報價 (Quote) 與標的驗證。"""

from typing import Any, Dict, List, Optional
import asyncio
import logging
import re
import time

import pandas as pd
import yfinance as yf

from market_time import is_market_open, ny_tz
from services.market_data_service._core import (
    _sanitize_ticker,
    _to_yfinance_symbol,
    call_yf,
    get_edge_client,
)
from services.market_data_service.caches import (
    _FINNHUB_QUOTE_STALE_THRESHOLD_SECONDS,
    _INVALID_SYMBOL_CACHE_TTL,
    _quote_cache,
    _QUOTE_CACHE_TTL,
    _VALID_SYMBOL_CACHE_TTL,
    _valid_symbol_cache,
)

logger = logging.getLogger(__name__)

_TICKER_PATTERN = re.compile(r"^[\^A-Z0-9.-]{1,10}$")
_FALLBACK_TABLES: tuple[str, ...] = (
    "assets",
    "market_cache",
    "historical_iv",
    "active_orders",
)


# ---------------------------------------------------------------------------
# Quote (即時報價)
# ---------------------------------------------------------------------------
async def _fetch_history_via_edge(
    symbol: str, *, period: str, interval: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """優先透過 Edge 節點即時抓取 K 線。未設定 TUNNEL_URL 或抓取失敗/空值時回傳 None。"""
    from config import TUNNEL_URL
    import urllib.parse

    base_url = (
        getattr(TUNNEL_URL, "rstrip", lambda x: TUNNEL_URL)("/") if TUNNEL_URL else ""
    )
    if not base_url:
        return None

    try:
        req_url = f"{base_url}/api/v1/scrape/yf/history/{urllib.parse.quote(str(symbol))}?period={period}"
        if interval:
            req_url += f"&interval={interval}"

        async with get_edge_client() as client:
            res = await client.get(req_url)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    records = data.get("data", [])
                    if records:
                        df_edge = pd.DataFrame(records)
                        if "Date" in df_edge.columns:
                            df_edge["Date"] = pd.to_datetime(
                                df_edge["Date"], utc=True
                            ).dt.tz_convert(ny_tz)
                            df_edge.set_index("Date", inplace=True)
                        elif "Datetime" in df_edge.columns:
                            df_edge["Datetime"] = pd.to_datetime(
                                df_edge["Datetime"], utc=True
                            ).dt.tz_convert(ny_tz)
                            df_edge.set_index("Datetime", inplace=True)
                        logger.info(f"[{symbol}] Edge 節點成功抓取 K 線")
                        return df_edge
                    else:
                        # Edge 節點已明確確認查無數據 (如標的下市)，回傳空 DataFrame 避免無謂本地重試
                        return pd.DataFrame()
    except Exception as ex:
        logger.warning(f"[{symbol}] Edge 節點即時抓取 K 線失敗: {ex}")

    return None


async def _direct_yf_history(
    ticker: yf.Ticker, *, period: str, interval: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """nexus_core 直連 yfinance 抓取 K 線（降級方案），加入 repair 容錯。"""
    df = None
    try:
        kwargs: dict[str, Any] = {
            "period": period,
            "auto_adjust": True,
            "repair": True,
        }
        if interval is not None:
            kwargs["interval"] = interval

        df = await call_yf(ticker.history, **kwargs)
    except Exception as e:
        logger.warning(
            f"yfinance history (repair=True) 失敗: {e}，嘗試降級使用 repair=False 重試..."
        )
        try:
            kwargs_fallback: dict[str, Any] = {
                "period": period,
                "auto_adjust": True,
                "repair": False,
            }
            if interval is not None:
                kwargs_fallback["interval"] = interval
            df = await call_yf(ticker.history, **kwargs_fallback)
        except Exception as e2:
            logger.warning(f"yfinance history 直接呼叫失敗: {e2}")

    if df is None or getattr(df, "empty", True):
        return None
    return df


async def _safe_yf_history(
    ticker: yf.Ticker,
    *,
    period: str,
    interval: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """安全包裝 yfinance history：優先透過 Edge 節點抓取，
    nexus_core 直連 yfinance（含 repair 容錯）僅作為 Edge 不可用時的降級方案。
    """

    symbol = getattr(ticker, "ticker", "")
    if symbol:
        df_edge = await _fetch_history_via_edge(
            symbol, period=period, interval=interval
        )
        if df_edge is not None:
            if not df_edge.empty:
                return df_edge
            # Edge 節點已確認無數據，直接返回 None，不再走本地直連
            return None
        logger.info(f"[{symbol}] 降級改用本地 yfinance 直連抓取 K 線...")

    return await _direct_yf_history(ticker, period=period, interval=interval)


async def get_yfinance_quote(symbol: str) -> Dict[str, Any]:
    """使用 yfinance 取得即時報價，並轉換格式與 Finnhub 相容。

    防禦性處理：
    - 自動清洗 ticker（移除 `$` 與空白），避免 yfinance HTTP 400。
    - 若回傳為空則記錄 warning 並回傳空 dict，避免中斷批次任務。
    """

    yf_symbol = _to_yfinance_symbol(symbol)
    try:
        ticker = yf.Ticker(yf_symbol)
        # 抓取最近 2 天資料以計算昨日收盤 (pc)
        df = await _safe_yf_history(ticker, period="2d")
        if df is None:
            logger.warning(f"[{yf_symbol}] yfinance quote 回傳資料為空")
            import yfinance as yf_module

            if hasattr(yf_module, "shared") and hasattr(yf_module.shared, "_ERRORS"):
                if yf_symbol in yf_module.shared._ERRORS:
                    err_msg = str(yf_module.shared._ERRORS[yf_symbol]).lower()
                    if "no data found" in err_msg or "delisted" in err_msg:
                        raise ValueError("SYMBOL_NOT_FOUND")
            return {}

        latest = df.iloc[-1]
        prev_close = df.iloc[-2]["Close"] if len(df) > 1 else latest["Open"]
        current_price = latest["Close"]

        change = current_price - prev_close
        pct_change = (change / prev_close) * 100 if prev_close != 0 else 0.0

        return {
            "c": round(float(current_price), 2),
            "d": round(float(change), 2),
            "dp": round(float(pct_change), 4),
            "h": round(float(latest["High"]), 2),
            "l": round(float(latest["Low"]), 2),
            "o": round(float(latest["Open"]), 2),
            "pc": round(float(prev_close), 2),
            "t": int(df.index[-1].timestamp()),
        }
    except Exception as e:
        if "SYMBOL_NOT_FOUND" in str(e):
            raise
        logger.error(f"[{yf_symbol}] yfinance quote 失敗: {e}")
        return {}


def _is_finnhub_quote_stale(data: Dict[str, Any]) -> bool:
    """盤中判斷 Finnhub `/quote` 回傳的報價是否陳舊（例如帳號無美股即時報價權限，
    只回傳上一交易日收盤價）。僅在市場開盤時檢查——收盤/週末時 `t` 停留在最後
    成交時間屬正常現象，不應誤判為過期。"""
    if not is_market_open():
        return False

    quote_ts = data.get("t")
    if not quote_ts:
        return False

    age_seconds = time.time() - float(quote_ts)
    return age_seconds > _FINNHUB_QUOTE_STALE_THRESHOLD_SECONDS


async def get_quote(symbol: str, allow_stale: bool = False) -> Dict[str, Any]:
    """取得即時報價 (非同步，支援 15 秒快取與併發請求合併)。對於指數型標的，強制轉向 yfinance。

    ⚠️ **為什麼 15 秒 TTL 不足以取代 single-flight**：快取寫入發生在網路 `await`
    之後，同一瞬間 miss 的呼叫端會各自打一次 Finnhub／yfinance。已知的併發同標的
    呼叫點：`/x symbol` 的 `quote_task` 與同一並行池中 `uoa_detector` 的
    `get_quote(symbol)`、以及 `get_option_chain` 內 Strike Pruning 的 `_get_spot()`。
    """
    symbol = _sanitize_ticker(symbol)
    now = time.time()
    if symbol in _quote_cache:
        val, expiry = _quote_cache[symbol]
        if now < expiry or (allow_stale and val.get("c", 0) > 0):
            return val  # type: ignore

    from services.single_flight import SingleFlightManager

    # allow_stale 必須併入 key：它會改變 _fetch() 的行為（是否接受時間戳過舊的
    # Finnhub 報價，或強制轉 yfinance fallback），共乘會拿到不同語意的資料。
    # 比照 options.py::get_option_chain 對 force_live 的處理。
    return await SingleFlightManager.run(  # type: ignore[no-any-return]
        f"quote_{symbol}_{'stale_ok' if allow_stale else 'std'}",
        _fetch_quote_uncached,
        symbol,
        allow_stale,
        now,
    )


async def _fetch_quote_uncached(
    symbol: str, allow_stale: bool, now: float
) -> Dict[str, Any]:
    """實際抓取即時報價並寫入 `_quote_cache`（Finnhub 優先、yfinance 降級）。

    `SYMBOL_NOT_FOUND` 會外拋給所有共乘者——那是「標的不存在」的確定結論，
    不是暫時性失敗，不應被吞成空 dict。
    """
    # 延遲從套件頂層 import：讓單元測試對 `services.market_data_service.X`
    # (is_finnhub_rate_limited / _get_client / _execute_api_call / get_yfinance_quote)
    # 的 patch 能正確攔截本函式內部呼叫（這幾個名稱originally 與 get_quote 同屬
    # 一個模組，拆分為套件後若在檔案頂層綁定，會繞過套件層級的 monkeypatch）。
    from services.market_data_service import (
        _execute_api_call,
        _get_client,
        get_yfinance_quote as _get_yfinance_quote,
        is_finnhub_rate_limited,
    )

    async def _fetch() -> Any:
        if symbol.startswith("^") or symbol == "VIX" or symbol.endswith("=F"):
            return await _get_yfinance_quote(symbol)

        if is_finnhub_rate_limited():
            logger.warning(
                f"[{symbol}] Finnhub 處於限流冷卻中，直接轉向 yfinance fallback，避免堆積等待"
            )
            return await _get_yfinance_quote(symbol)

        client = _get_client()
        try:
            data = await _execute_api_call(client.quote, symbol)
            if data and data.get("c", 0) > 0:
                if not allow_stale and _is_finnhub_quote_stale(data):
                    logger.warning(
                        f"[{symbol}] Finnhub quote 時間戳過舊 (t={data.get('t')})，"
                        f"疑似帳號無即時報價權限，改用 yfinance fallback"
                    )
                    return await _get_yfinance_quote(symbol)
                return data

            # 若 Finnhub 回傳無效或報權限錯誤 (c=0 有可能是權限問題或標的不存在)
            # 快速驗證標的是否存在，避免在無效標的上耗費 yfinance 的長時間請求
            lookup = await _execute_api_call(client.symbol_lookup, symbol)
            if lookup:
                results_list = lookup.get("result", [])
                target_syms = {
                    symbol,
                    symbol.replace("-", "."),
                    symbol.replace(".", "-"),
                }
                has_match = any(
                    str(r.get("symbol", "")).upper() in target_syms
                    or str(r.get("displaySymbol", "")).upper() in target_syms
                    for r in results_list
                )
                if not has_match:
                    logger.warning(
                        f"[{symbol}] Finnhub 確認標的不存在 (無匹配代號)，直接中斷"
                    )
                    raise ValueError("SYMBOL_NOT_FOUND")

            # 嘗試作為 fallback 轉向 yfinance
            logger.warning(f"[{symbol}] Finnhub quote 無效，嘗試 yfinance fallback")
            return await _get_yfinance_quote(symbol)
        except Exception as e:
            if "SYMBOL_NOT_FOUND" in str(e):
                raise
            # 任何 Finnhub 錯誤（包含限流 429、權限問題等）皆強制轉向 yfinance fallback，避免回傳 {} 導致下游算式崩潰
            logger.warning(
                f"[{symbol}] Finnhub quote 失敗: {e}，強制轉向 yfinance fallback"
            )
            return await _get_yfinance_quote(symbol)

    try:
        res = await _fetch()
    except Exception as ex:
        _quote_cache[symbol] = ({}, now + _QUOTE_CACHE_TTL)
        if "SYMBOL_NOT_FOUND" in str(ex):
            raise
        return {}

    if res and res.get("c", 0) > 0:
        _quote_cache[symbol] = (res, now + _QUOTE_CACHE_TTL)
    else:
        _quote_cache[symbol] = ({}, now + _QUOTE_CACHE_TTL)
    return res  # type: ignore


def _check_symbols_in_db(symbols: list[str]) -> set[str]:
    """比對本地資料庫中是否已有該批標的之運作紀錄（快篩機制）。"""
    if not symbols:
        return set()

    import sqlite3
    from database.connection import get_read_connection

    found: set[str] = set()
    upper_symbols: list[str] = [s.strip().upper() for s in symbols if s]
    if not upper_symbols:
        return found

    try:
        conn = get_read_connection()
        try:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in upper_symbols)
            for table in _FALLBACK_TABLES:
                try:
                    # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                    cursor.execute(
                        f"SELECT DISTINCT symbol FROM {table} WHERE symbol IN ({placeholders})",
                        upper_symbols,
                    )
                    for row in cursor.fetchall():
                        found.add(str(row[0]).upper())
                    if len(found) == len(upper_symbols):
                        break
                except sqlite3.OperationalError:
                    continue
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"validate_symbol 資料庫後備驗證失敗: {e}")

    return found


async def validate_symbol(symbol: str, check_db: bool = True) -> bool:
    """驗證標的代號是否有效 (具備即時報價、本地資料庫對比及格式後備機制)。"""
    if not symbol:
        return False

    symbol = symbol.strip().upper()

    # 1. 基礎格式驗證：代號長度不合理或包含非法字元直接過濾
    if not _TICKER_PATTERN.match(symbol):
        return False

    now = time.time()

    # 2. 記憶體快取快篩
    if symbol in _valid_symbol_cache:
        val, expiry = _valid_symbol_cache[symbol]
        if now < expiry:
            return bool(val)

    if symbol in _quote_cache:
        q, _ = _quote_cache[symbol]
        if q and q.get("c", 0) > 0:
            _valid_symbol_cache[symbol] = (True, now + _VALID_SYMBOL_CACHE_TTL)
            return True

    # 3. 本地資料庫優先比對（assets, market_cache, historical_iv, active_orders）
    if check_db and symbol in _check_symbols_in_db([symbol]):
        _valid_symbol_cache[symbol] = (True, now + _VALID_SYMBOL_CACHE_TTL)
        return True

    # 4. 嘗試獲取即時報價，若有價格大於 0 則必為有效標的
    # 延遲從套件頂層 import get_quote：讓 `patch("services.market_data_service.get_quote")`
    # 能正確攔截這裡的內部呼叫。
    from services.market_data_service import get_quote as _get_quote

    try:
        quote = await _get_quote(symbol, allow_stale=True)
        if quote and quote.get("c", 0) > 0:
            _valid_symbol_cache[symbol] = (True, now + _VALID_SYMBOL_CACHE_TTL)
            return True
    except Exception as e:
        if "SYMBOL_NOT_FOUND" in str(e):
            logger.warning(f"[{symbol}] API 回傳標的不存在，直接中斷後續動作")
            _valid_symbol_cache[symbol] = (False, now + _INVALID_SYMBOL_CACHE_TTL)
            return False
        logger.warning(f"validate_symbol 獲取報價異常: {e}")

    _valid_symbol_cache[symbol] = (False, now + _INVALID_SYMBOL_CACHE_TTL)
    return False


async def batch_validate_symbols(symbols: list[str]) -> dict[str, bool]:
    """批次驗證標的代號是否有效（高效能快篩 + 併發降級）。

    優化策略：
    1. 格式快篩 (Regex)：無效格式立即判定為 False。
    2. 記憶體快取篩查：若在 `_valid_symbol_cache` 或 `_quote_cache` 中有命中，直接使用快取。
    3. 資料庫批次快篩：一次性在 assets, market_cache 等表中批次比對，大幅減少 I/O 與網路往返。
    4. 對於未命中 DB/快取的少數未知標的，先以 clean symbol 去重，再併發執行 market_data_service.validate_symbol(check_db=False)。
    """
    if not symbols:
        return {}

    from services import market_data_service

    results: dict[str, bool] = {}
    clean_map: dict[str, str] = {}
    for raw in symbols:
        if not raw:
            results[raw] = False
            continue
        s_upper = raw.strip().upper()
        clean_map[raw] = s_upper
        if not _TICKER_PATTERN.match(s_upper):
            results[raw] = False

    pending_raw: list[str] = [r for r in symbols if r not in results]
    if not pending_raw:
        return results

    now = time.time()
    # 記憶體快取檢查
    for r in pending_raw:
        sym = clean_map[r]
        if sym in _valid_symbol_cache:
            val, expiry = _valid_symbol_cache[sym]
            if now < expiry:
                results[r] = bool(val)
        elif sym in _quote_cache:
            q, _ = _quote_cache[sym]
            if q and q.get("c", 0) > 0:
                results[r] = True
                _valid_symbol_cache[sym] = (True, now + _VALID_SYMBOL_CACHE_TTL)

    # 本地資料庫批次快篩
    unresolved_raw: list[str] = [r for r in pending_raw if r not in results]
    if unresolved_raw:
        unresolved_symbols: list[str] = [clean_map[r] for r in unresolved_raw]
        db_found = _check_symbols_in_db(unresolved_symbols)
        for r in unresolved_raw:
            sym = clean_map[r]
            if sym in db_found:
                results[r] = True
                _valid_symbol_cache[sym] = (True, now + _VALID_SYMBOL_CACHE_TTL)

    # 仍未解析之標的（未在 DB 亦未在記憶體中，去重後走即時驗證，且略過重複之 DB 查詢）
    remaining_raw: list[str] = [r for r in unresolved_raw if r not in results]
    if remaining_raw:
        unique_syms: list[str] = list({clean_map[r] for r in remaining_raw})
        sem = asyncio.Semaphore(10)

        async def _eval_one(clean_sym: str) -> tuple[str, bool]:
            async with sem:
                try:
                    try:
                        is_val = await market_data_service.validate_symbol(
                            clean_sym, check_db=False
                        )
                    except TypeError:
                        is_val = await market_data_service.validate_symbol(clean_sym)
                    return clean_sym, is_val
                except Exception:
                    return clean_sym, False

        resolved_pairs: list[tuple[str, bool]] = await asyncio.gather(
            *[_eval_one(s) for s in unique_syms]
        )
        sym_status_map: dict[str, bool] = dict(resolved_pairs)
        for r in remaining_raw:
            results[r] = sym_status_map.get(clean_map[r], False)

    return results


async def batch_get_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """批次取得多檔標的的即時報價。

    防禦性設計：
    - 先做 ticker 清洗（移除 `$`/空白並大寫），避免 yfinance/Finnhub 請求格式錯誤。
    - 使用批次級 ``asyncio.Semaphore(3)`` 限制同時在途的報價請求數量，
      避免大量 watchlist 標的同時湧入下游 limiter 佇列而觸發 Finnhub 429。
      （每個 ``get_quote()`` 仍有自己的 per-call rate limiter，此為外層保護層。）
    """
    clean_symbols = [_sanitize_ticker(s) for s in symbols if s]
    batch_sem = asyncio.Semaphore(3)

    async def _throttled_quote(sym: str) -> Dict[str, Any]:
        async with batch_sem:
            return await get_quote(sym)

    tasks = [_throttled_quote(sym) for sym in clean_symbols]
    quotes = await asyncio.gather(*tasks)
    return {sym: q for sym, q in zip(clean_symbols, quotes) if q}
