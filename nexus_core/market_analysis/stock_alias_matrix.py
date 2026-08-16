"""Stock Alias and Keyword Resolution Matrix for Polymarket, Reddit, and Intelligence services."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Set

from database.cache import get_kv_cache, save_kv_cache
from services.market_data_service import get_company_profile

logger = logging.getLogger(__name__)

# 靜態美股權值股、熱門科技股、產業龍頭與指數 ETF 映射庫
STOCK_ALIAS_MAP: Dict[str, List[str]] = {
    # 科技巨頭 / Magnificent 7
    "NVDA": [
        "nvda",
        "nvidia",
        "jensen huang",
        "blackwell",
        "hopper",
        "cuda",
        "h100",
        "b200",
        "geforce",
    ],
    "AAPL": [
        "aapl",
        "apple",
        "tim cook",
        "iphone",
        "vision pro",
        "ipad",
        "macbook",
        "wwdc",
        "apple intelligence",
    ],
    "MSFT": [
        "msft",
        "microsoft",
        "satya nadella",
        "azure",
        "copilot",
        "windows",
        "activision",
        "openai",
    ],
    "GOOGL": [
        "googl",
        "goog",
        "google",
        "alphabet",
        "sundar pichai",
        "waymo",
        "deepmind",
        "gemini",
        "youtube",
    ],
    "GOOG": [
        "googl",
        "goog",
        "google",
        "alphabet",
        "sundar pichai",
        "waymo",
        "deepmind",
        "gemini",
        "youtube",
    ],
    "AMZN": [
        "amzn",
        "amazon",
        "aws",
        "andy jassy",
        "jeff bezos",
        "prime video",
        "anthropic",
    ],
    "META": [
        "meta",
        "facebook",
        "instagram",
        "mark zuckerberg",
        "zuckerberg",
        "threads",
        "llama",
        "oculus",
        "quest",
    ],
    "TSLA": [
        "tsla",
        "tesla",
        "elon musk",
        "robotaxi",
        "cybertruck",
        "fsd",
        "optimus",
        "supercharger",
        "gigafactory",
    ],
    # AI / 半導體與伺服器硬體
    "AMD": ["amd", "advanced micro devices", "lisa su", "mi300", "ryzen", "radeon"],
    "AVGO": ["avgo", "broadcom", "hock tan"],
    "TSM": [
        "tsm",
        "tsmc",
        "taiwan semiconductor",
        "morris chang",
        "cc wei",
        "foundry",
    ],
    "ASML": ["asml", "euv", "lithography"],
    "MU": ["mu", "micron", "memory", "hbm", "sanjay mehrotra"],
    "INTC": ["intc", "intel", "pat gelsinger", "foundry", "gaudi"],
    "QCOM": ["qcom", "qualcomm", "snapdragon", "cristiano amon"],
    "ARM": ["arm", "arm holdings", "rene haas"],
    "SMCI": ["smci", "super micro", "supermicro", "charles liang"],
    "MRVL": ["mrvl", "marvell"],
    "KLAC": ["klac", "kla"],
    "LRCX": ["lrcx", "lam research"],
    "AMAT": ["amat", "applied materials"],
    # 軟體 / 雲端 / 資安 / 加密生態
    "PLTR": ["pltr", "palantir", "alex karp", "aip", "gotham", "foundry"],
    "COIN": ["coin", "coinbase", "brian armstrong", "base network"],
    "MSTR": ["mstr", "microstrategy", "michael saylor", "saylor"],
    "CRWD": ["crwd", "crowdstrike", "george kurtz", "falcon"],
    "PANW": ["panw", "palo alto networks", "nikesh arora"],
    "SNOW": ["snow", "snowflake", "sridhar ramaswamy"],
    "ORCL": ["orcl", "oracle", "larry ellison", "safra catz", "oracle cloud"],
    "CRM": ["crm", "salesforce", "marc benioff"],
    "ADBE": ["adbe", "adobe", "shantanu narayen", "firefly"],
    "HOOD": ["hood", "robinhood", "vlad tenev"],
    "UBER": ["uber", "dara khosrowshahi"],
    "ABNB": ["abnb", "airbnb", "brian chesky"],
    "NFLX": ["nflx", "netflix", "ted sarandos", "greg peters"],
    "SPOT": ["spot", "spotify", "daniel ek"],
    "BABA": ["baba", "alibaba", "jack ma", "eddie wu"],
    "PDD": ["pdd", "pinduoduo", "temu"],
    # 傳統權值巨頭 / 生技醫療 / 工業航太 / 金融能源
    "LLY": ["lly", "eli lilly", "mounjaro", "zepbound"],
    "NVO": ["nvo", "novo nordisk", "ozempic", "wegovy"],
    "BA": ["ba", "boeing", "kelly ortberg", "737 max", "starliner"],
    "DIS": ["dis", "disney", "bob iger"],
    "WMT": ["wmt", "walmart", "doug mcmillon"],
    "COST": ["cost", "costco", "ron vachris"],
    "JPM": ["jpm", "jpmorgan", "jamie dimon"],
    "GS": ["gs", "goldman sachs", "david solomon"],
    "MS": ["ms", "morgan stanley", "ted pick"],
    "BRK": ["brk", "berkshire", "warren buffett", "buffett", "charlie munger"],
    "BRK.A": ["brk", "berkshire", "warren buffett", "buffett"],
    "BRK.B": ["brk", "berkshire", "warren buffett", "buffett"],
    "XOM": ["xom", "exxon", "exxonmobil", "darren woods"],
    "CVX": ["cvx", "chevron", "mike wirth"],
    # 宏觀 / 指數 / 產業 ETF
    "SPY": [
        "spy",
        "s&p 500",
        "s&p",
        "sp500",
        "standard & poor",
        "stock market",
        "fed",
        "recession",
        "rate cut",
        "cpi",
    ],
    "VOO": [
        "voo",
        "s&p 500",
        "s&p",
        "sp500",
        "stock market",
        "fed",
        "recession",
        "rate cut",
    ],
    "IVV": [
        "ivv",
        "s&p 500",
        "s&p",
        "sp500",
        "stock market",
        "fed",
        "recession",
        "rate cut",
    ],
    "QQQ": ["qqq", "nasdaq 100", "nasdaq", "ndx", "tech stocks", "big tech"],
    "QQQM": ["qqqm", "qqq", "nasdaq 100", "nasdaq", "ndx", "tech stocks"],
    "DIA": ["dia", "dow jones", "djia", "dow"],
    "IWM": ["iwm", "russell 2000", "russell", "small cap"],
    "SMH": ["smh", "soxx", "semiconductor", "semis", "chips", "chips act", "gpu"],
    "SOXX": ["soxx", "smh", "semiconductor", "semis", "chips", "chips act"],
    "XLK": ["xlk", "technology select sector", "tech sector"],
    "XLE": ["xle", "energy select sector", "crude oil", "oil price", "opec"],
    "XLF": ["xlf", "financial select sector", "bank", "interest rate"],
    "VIX": ["vix", "volatility index", "fear gauge", "market crash"],
}


class StockAliasMatrix:
    """統一美股別名與搜尋字串解析矩陣 (含四層自動補齊與快取機制)。"""

    GENERIC_FIRST_WORDS: Set[str] = {
        "super",
        "taiwan",
        "american",
        "general",
        "united",
        "national",
        "first",
        "international",
        "china",
        "global",
        "western",
        "digital",
        "advanced",
        "applied",
        "pacific",
        "southern",
        "central",
        "new",
        "north",
        "south",
        "east",
    }

    _dynamic_alias_cache: Dict[str, List[str]] = {}

    @classmethod
    async def get_aliases_for_symbol(cls, symbol: str) -> List[str]:
        """獲取指定代碼之所有有效別名（四層漸進式自動補齊）。

        Tier 1: 靜態內建庫 (0ms)
        Tier 2: 記憶體動態快取 (0ms)
        Tier 3: SQLite kv_cache (< 2ms)
        Tier 4: 動態推導 (Profile Clean) 並自動持久化補齊
        """
        sym_clean = symbol.upper().strip()
        if not sym_clean:
            return []

        # ── Tier 1: 靜態內建知識庫 ─────────────────────────────
        if sym_clean in STOCK_ALIAS_MAP:
            return list(STOCK_ALIAS_MAP[sym_clean])

        # ── Tier 2: 記憶體動態快取 ─────────────────────────────
        if sym_clean in cls._dynamic_alias_cache:
            return list(cls._dynamic_alias_cache[sym_clean])

        # ── Tier 3: SQLite kv_cache ───────────────────────────
        try:
            cached = get_kv_cache(f"stock_aliases_{sym_clean}")
            if cached and isinstance(cached, list):
                cls._dynamic_alias_cache[sym_clean] = [str(x) for x in cached]
                return cls._dynamic_alias_cache[sym_clean]
        except Exception as e:
            logger.debug(f"Failed to load aliases from kv_cache for {sym_clean}: {e}")

        # ── Tier 4: 動態推導與自動補齊 ─────────────────────────
        aliases: List[str] = [sym_clean.lower()]
        try:
            profile = await get_company_profile(sym_clean)
            if profile and "name" in profile:
                raw_name = str(profile.get("name", ""))
                cleaned_name = cls.clean_company_name(raw_name)
                if cleaned_name and cleaned_name.lower() not in aliases:
                    aliases.append(cleaned_name.lower())

                # 若為雙字以上品牌 (如 "Rocket Lab")，額外提取第一主字 (若非通用詞且長度 >= 4)
                words = cleaned_name.split()
                if (
                    len(words) >= 2
                    and len(words[0]) >= 4
                    and words[0].lower() not in cls.GENERIC_FIRST_WORDS
                ):
                    if words[0].lower() not in aliases:
                        aliases.append(words[0].lower())
        except Exception as e:
            logger.debug(f"Failed to auto-derive aliases for {sym_clean}: {e}")

        # 自動寫入 Tier 2 記憶體快取與 Tier 3 SQLite
        cls._dynamic_alias_cache[sym_clean] = aliases
        try:
            await save_kv_cache(f"stock_aliases_{sym_clean}", aliases)
            logger.info(
                f"✨ [StockAliasMatrix] 已自動補齊並持久化 {sym_clean} 之別名: {aliases}"
            )
        except Exception as e:
            logger.debug(f"Failed to save aliases to kv_cache for {sym_clean}: {e}")

        return aliases

    @classmethod
    def clean_company_name(cls, name: str) -> str:
        """
        清洗公司名稱，防範 Super/Taiwan 等單字被誤切。
        將法律實體後綴、地域與股票類別精準剔除，同時保留品牌核心雙字。
        """
        if not name:
            return ""

        # 替換標點符號為空格並分詞
        raw_tokens = re.sub(r"[,\.\(\)\-/]", " ", name).split()
        legal_stopwords = {
            "inc",
            "corp",
            "corporation",
            "ltd",
            "limited",
            "holdings",
            "holding",
            "group",
            "plc",
            "nv",
            "sa",
            "co",
            "company",
            "companies",
            "usa",
            "adr",
            "class",
            "cl",
            "a",
            "b",
            "c",
        }

        words = [w for w in raw_tokens if w.lower() not in legal_stopwords]
        if not words:
            return ""

        # 若第一個單字屬於常見通用詞，保留前兩個單字 (例如 Super Micro, Taiwan Semiconductor)
        if len(words) >= 2 and words[0].lower() in cls.GENERIC_FIRST_WORDS:
            return f"{words[0]} {words[1]}"

        # 若清洗後詞數小於等於 2，直接回傳全名（例如 Rocket Lab, AST SpaceMobile, Palantir Technologies）
        if len(words) <= 2:
            return " ".join(words)

        return words[0]

    @classmethod
    def build_reddit_query(
        cls, symbol: str, aliases: Optional[List[str]] = None
    ) -> str:
        """構建 Reddit RSS 專用 Boolean Search Query。

        範例:
        NVDA -> '"NVDA" OR "$NVDA" OR "NVIDIA" OR "Jensen Huang"'
        SMCI -> '"SMCI" OR "$SMCI" OR "Super Micro" OR "Supermicro"'
        """
        sym_upper = symbol.upper().strip()
        terms: List[str] = [f'"{sym_upper}"', f'"${sym_upper}"']

        if aliases:
            for a in aliases:
                a_clean = a.strip()
                if (
                    a_clean.upper() != sym_upper
                    and len(a_clean) > 2
                    and a_clean.lower() not in cls.GENERIC_FIRST_WORDS
                ):
                    term_str = f'"{a_clean}"'
                    if term_str not in terms:
                        terms.append(term_str)
                if len(terms) >= 4:
                    break

        return " OR ".join(terms)

    @classmethod
    def is_text_matching_symbol(
        cls, text: str, symbol: str, aliases: Optional[List[str]] = None
    ) -> bool:
        """嚴格詞界匹配判斷文字是否關聯該美股。"""
        if not text:
            return False
        text_lower = text.lower()
        sym_lower = symbol.lower().strip()

        # 1. 嚴格代碼匹配
        if re.search(rf"\b{re.escape(sym_lower)}\b", text_lower):
            return True

        # 2. 別名匹配
        if aliases:
            for alt in aliases:
                alt_clean = alt.strip().lower()
                if not alt_clean:
                    continue
                # 短詞 (長度 <= 3) 需嚴格詞界檢查，長詞可子字串匹配
                if len(alt_clean) <= 3:
                    if re.search(rf"\b{re.escape(alt_clean)}\b", text_lower):
                        return True
                else:
                    if alt_clean in text_lower:
                        return True
        return False
