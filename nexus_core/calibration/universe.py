"""標的池：watchlist ∪ 持倉 (唯讀) ∪ 固定流動性清單，排除核心防禦 ETF 與反向 ETF。

⚠️ 倖存者偏差：以「現在」的清單回測過去，已下市或被移出清單的標的不在池內，
報告會明確揭露此限制。
"""

import logging

logger = logging.getLogger(__name__)

# 固定流動性清單：選擇權流動性長期良好、涵蓋多個產業的大型股。
LIQUID_UNIVERSE: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "JPM",
    "BAC",
    "GS",
    "XOM",
    "CVX",
    "UNH",
    "LLY",
    "COST",
    "WMT",
    "HD",
    "CRM",
    "ORCL",
    "INTC",
    "MU",
    "QCOM",
    "BA",
    "CAT",
    "DIS",
    "NKE",
    "IWM",
    "SMH",
    "XLF",
    "XLE",
    "PYPL",
    "SHOP",
    "UBER",
    "COIN",
    "PLTR",
)


def _excluded() -> set[str]:
    from market_analysis.dynamic_rollover.constants import (
        CORE_DEFENSE_ETF_SYMBOLS,
        INDEX_INVERSE_MAP,
        SECTOR_INVERSE_MAP,
        SINGLE_STOCK_INVERSE_MAP,
    )

    inverse = set(INDEX_INVERSE_MAP.values()) | set(SECTOR_INVERSE_MAP.values())
    for variants in SINGLE_STOCK_INVERSE_MAP.values():
        inverse |= set(variants.values())
    return set(CORE_DEFENSE_ETF_SYMBOLS) | inverse | {"BOXX"}


def _db_symbols() -> list[str]:
    """唯讀讀取 watchlist 與持倉標的 (NEXUS_DB_NAME 應指向複製的快照)。"""
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT UPPER(symbol) FROM watchlist "
                "UNION SELECT DISTINCT UPPER(symbol) FROM portfolio"
            )
            return sorted({str(r[0]) for r in cur.fetchall() if r[0]})
        finally:
            conn.close()
    except Exception as e:
        logger.info(f"[calibration] 無法讀取 watchlist/持倉 (改用固定清單): {e}")
        return []


def build_universe(max_symbols: int, include_db: bool = True) -> list[str]:
    excluded = _excluded()
    ordered: list[str] = []
    for sym in (_db_symbols() if include_db else []) + list(LIQUID_UNIVERSE):
        s = sym.upper()
        if s in excluded or s in ordered or not s.replace(".", "").isalnum():
            continue
        ordered.append(s)
    return ordered[: max(0, max_symbols)]
