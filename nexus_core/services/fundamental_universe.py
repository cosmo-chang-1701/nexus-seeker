"""基本面分析管線標的池收集與過濾器 (Fundamental Universe Provider)。

職責：
1. 收集全體使用者的持倉 (Holdings) 與自選清單 (Watchlist)。
2. 排除指數 (^SPX 等) 與各類 ETF (寬基、行業、槓桿、反向、大宗商品、債券)。
3. 持倉標的具備最高優先權，其次為自選標的。
4. 去重並箝制上限 (預設 80 檔，由 FUNDAMENTAL_UNIVERSE_MAX_SYMBOLS 控制)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import config
from database.portfolio import get_all_portfolio
from database.watchlist import get_all_watchlist
from market_analysis.dynamic_rollover.constants import CORE_DEFENSE_ETF_SYMBOLS

logger = logging.getLogger(__name__)

# 常見大宗指數、板塊、槓桿反向與債券商品 ETF 清單
KNOWN_ETF_SYMBOLS: frozenset[str] = frozenset(
    {
        # 寬基與指數
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "VOO",
        "VTI",
        "IVV",
        "VEA",
        "VWO",
        "EEM",
        "EFA",
        "VT",
        "RSP",
        "OEF",
        "MDY",
        "IJH",
        "IJR",
        # 行業板塊
        "XLK",
        "XLF",
        "XLE",
        "XLV",
        "XLY",
        "XLP",
        "XLU",
        "XLI",
        "XLB",
        "XLRE",
        "XLC",
        "SMH",
        "SOXX",
        "XBI",
        "IBB",
        "KRE",
        "KBE",
        "XHB",
        "ITB",
        "XME",
        "XOP",
        "GDX",
        "GDXJ",
        "XRT",
        "IYT",
        "JETS",
        "ARKK",
        "ARKG",
        "ARKW",
        "ARKF",
        # 核心防禦、貨幣與現金等價物
        "BOXX",
        "BIL",
        "SGOV",
        "SHV",
        "MINT",
        "JPST",
        "ICSH",
        # 債券
        "TLT",
        "IEF",
        "SHY",
        "BND",
        "AGG",
        "LQD",
        "HYG",
        "JNK",
        "BNDX",
        "VCIT",
        "VCSH",
        # 大宗商品與貴金屬
        "GLD",
        "SLV",
        "IAU",
        "USO",
        "UNG",
        "DBA",
        "DBC",
        "PDBC",
        # 波動率與槓桿/反向
        "VXX",
        "UVXY",
        "SVXY",
        "VIXY",
        "SQQQ",
        "TQQQ",
        "SPXL",
        "SPXS",
        "UPRO",
        "SH",
        "PSQ",
        "SDS",
        "QID",
        "TZA",
        "TNA",
        "SOXL",
        "SOXS",
        "NUGT",
        "DUST",
        "LABU",
        "LABD",
    }
    | set(CORE_DEFENSE_ETF_SYMBOLS)
)


def is_etf_or_index(symbol: str) -> bool:
    """判定標的是否為指數或 ETF。"""
    sym = symbol.strip().upper()
    if not sym:
        return False
    if sym.startswith("^") or "=" in sym or "/" in sym:
        return True
    return sym in KNOWN_ETF_SYMBOLS


def is_valid_equity_symbol(symbol: str) -> bool:
    """判定代號是否符合個股代號格式（相容雙重股權 Class A/B 如 BRK.B / BRK-B）。"""
    s = symbol.strip().upper()
    if not s or len(s) > 10:
        return False
    return any(c.isalnum() for c in s) and all(c.isalnum() or c in ".-" for c in s)


def filter_universe_symbols(
    holding_symbols: list[str],
    watchlist_symbols: list[str],
    max_symbols: int = 80,
) -> list[str]:
    """純函式：依優先權過濾、去重並截斷標的池。

    1. 持倉優先 (Holdings First)
    2. 自選次之 (Watchlist Second)
    3. 排除 ETF 與非個股標的
    4. 去重後上限 max_symbols
    """
    selected: list[str] = []
    seen: set[str] = set()

    # 1. 優先加入持倉個股
    for raw in holding_symbols:
        s = raw.strip().upper()
        if not s or s in seen or is_etf_or_index(s) or not is_valid_equity_symbol(s):
            continue
        seen.add(s)
        selected.append(s)
        if len(selected) >= max_symbols:
            return selected

    # 2. 加入自選個股
    for raw in watchlist_symbols:
        s = raw.strip().upper()
        if not s or s in seen or is_etf_or_index(s) or not is_valid_equity_symbol(s):
            continue
        seen.add(s)
        selected.append(s)
        if len(selected) >= max_symbols:
            break

    return selected


def _is_nonzero_quantity(raw_qty: Any) -> bool:
    """數量是否為非零（空單以負數表示，因此以 != 0 判斷而非 > 0）。"""
    if raw_qty is None:
        return False
    try:
        return float(raw_qty) != 0.0
    except (TypeError, ValueError):
        return False


def _fetch_db_raw_symbols() -> tuple[list[str], list[str]]:
    """從 `assets` 表讀取全站持倉與自選清單（同步，須於 to_thread 中呼叫）。

    重用 `database.portfolio.get_all_portfolio()`（HOLDING + TRADE，數量存於
    JSON metadata 的 `quantity`）與 `database.watchlist.get_all_watchlist()`
    （WATCH），兩者皆以 try/finally 關閉唯讀連線。
    - 持倉：數量 != 0（空單 qty < 0 同樣納入）。
    - 自選：直接取 symbol。
    """
    holdings: list[str] = []
    seen_holdings: set[str] = set()
    for row in get_all_portfolio():
        # 欄位順序：(user_id, asset_id, symbol, opt_type, strike, expiry,
        #            entry_price, quantity, stock_cost, ...)
        sym_raw = row[2]
        if not sym_raw or not _is_nonzero_quantity(row[7]):
            continue
        sym = str(sym_raw).strip().upper()
        if sym and sym not in seen_holdings:
            seen_holdings.add(sym)
            holdings.append(sym)

    watchlist: list[str] = []
    seen_watch: set[str] = set()
    for _uid, sym_raw, _flag in get_all_watchlist():
        if not sym_raw:
            continue
        sym = str(sym_raw).strip().upper()
        if sym and sym not in seen_watch:
            seen_watch.add(sym)
            watchlist.append(sym)

    return holdings, watchlist


async def get_fundamental_universe(
    max_symbols: int | None = None,
) -> list[str]:
    """非同步取得全域基本面掃描標的池。"""
    limit = (
        max_symbols
        if max_symbols is not None
        else getattr(config, "FUNDAMENTAL_UNIVERSE_MAX_SYMBOLS", 80)
    )
    holdings, watchlist = await asyncio.to_thread(_fetch_db_raw_symbols)
    universe = filter_universe_symbols(holdings, watchlist, max_symbols=limit)
    logger.debug(
        f"[FundamentalUniverse] 產生標的池: 共 {len(universe)} 檔 (持倉 {len(holdings)} 檔, 自選 {len(watchlist)} 檔)"
    )
    return universe
