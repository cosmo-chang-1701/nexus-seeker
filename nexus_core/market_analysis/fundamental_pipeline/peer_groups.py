"""手動同業分組表（純資料與純函式葉模組，無外部 I/O）。

Finnhub 同業 API 只分到「半導體」等粗產業層級，會把記憶體股配到 AI 邏輯晶片股，
同業中位數 PE 失真。此處以人工分組取代；新增標的的維護方式見
docs/valuation_pricing/06_revision_momentum_and_fair_value.md。

解析優先序：GROUP（同組成員去掉自己） > NO_COMPS（無合理可比同業） > FINNHUB（維持現行流程）。
CYCLICAL_GROUPS 只用於標註與報表，不影響任何計算。
"""

from __future__ import annotations

from dataclasses import dataclass

PEER_GROUPS: dict[str, tuple[str, ...]] = {
    "記憶體與儲存": ("MU", "SNDK", "SKHY", "WDC", "STX"),
    "AI 運算與網通晶片": ("NVDA", "AMD", "MRVL", "ALAB", "AVGO", "CRDO"),
    "半導體 IP 與 EDA": ("ARM", "CDNS", "SNPS", "RMBS"),
    "整合元件製造": ("INTC", "TXN", "ADI", "MCHP", "ON", "NXPI"),
    "光通訊與化合物半導體": ("LITE", "AAOI", "AXTI", "COHR", "FN", "CIEN"),
    "超大型平台": ("GOOGL", "META", "AAPL", "MSFT", "AMZN"),
    "資安與雲端軟體": ("CRWD", "NET", "PLTR", "DDOG", "ZS", "PANW"),
    "AI 雲端算力": ("CRWV", "NBIS", "CIFR", "IREN"),
    "券商與加密平台": ("HOOD", "COIN", "SCHW", "IBKR"),
    "太空": ("RKLB", "SPCX", "ASTS", "LUNR"),
    "AI 電力設備": ("BE", "GEV", "VRT", "ETN"),
    "銅礦": ("FCX", "SCCO", "TECK", "BHP", "RIO"),
    "稀土與關鍵礦產": ("CRML", "MP", "USAR"),
}
CYCLICAL_GROUPS: frozenset[str] = frozenset({"記憶體與儲存", "銅礦", "稀土與關鍵礦產"})
NO_COMPS_SYMBOLS: frozenset[str] = frozenset({"TSLA", "MRNA"})

SOURCE_GROUP = "GROUP"
SOURCE_NO_COMPS = "NO_COMPS"
SOURCE_FINNHUB = "FINNHUB"


@dataclass(frozen=True)
class PeerGroupResolution:
    source: str  # GROUP / NO_COMPS / FINNHUB
    group: str | None
    peers: tuple[str, ...]
    cyclical: bool


def resolve_peer_group(symbol: str) -> PeerGroupResolution:
    """依分組表解析標的的同業來源。輸入先轉大寫並去除空白。"""
    sym = symbol.strip().upper()
    for name, members in PEER_GROUPS.items():
        if sym in members:
            return PeerGroupResolution(
                source=SOURCE_GROUP,
                group=name,
                peers=tuple(m for m in members if m != sym),
                cyclical=name in CYCLICAL_GROUPS,
            )
    if sym in NO_COMPS_SYMBOLS:
        return PeerGroupResolution(
            source=SOURCE_NO_COMPS, group=None, peers=(), cyclical=False
        )
    return PeerGroupResolution(
        source=SOURCE_FINNHUB, group=None, peers=(), cyclical=False
    )
