"""17 條產業鏈因果矩陣與高頻臨近預測對照表 (Supply Chain Mapping Matrix)。

涵蓋三大維度與先進科技五大支柱：
1. 總體核心 (Macro & Traditional Core): 5 條
2. 商業太空與國防前沿 (Space Economy & Defense): 5 條
3. 先進科技生態矩陣 (Frontier & Advanced Technology): 7 條 (另提供量子運算前沿擴充)
"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.models import SupplyChainLink

# ============================================================================
# 3.1 總體經濟與核心支柱產業鏈 (Macro & Traditional Core - 5 條)
# ============================================================================

LINK_AI_CAPEX = SupplyChainLink(
    link_key="AI_CAPEX",
    title="AI / 超級智慧算力資本支出鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="MACRO_CORE",
    drivers=[
        "XBRL:MSFT:capex",
        "XBRL:AMZN:capex",
        "XBRL:GOOGL:capex",
        "XBRL:META:capex",
        "XBRL:ORCL:capex",
        "XBRL:SPCX:capex",
    ],
    followers=[
        "NVDA",
        "ANET",
        "DELL",
        "VRT",
        "TWSE:2382",
        "TWSE:6669",
        "TWSE:2345",
        "TWSE:2317",
    ],
    description="包含微軟、Meta 及 SpaceX (SPCX / SpaceXSI) 等超級算力建設方之資本支出先行，1–2 季內傳導至伺服器代工與網通晶片廠營收。",
    lead_lag_quarters="1-2Q",
)

LINK_FABLESS_FOUNDRY = SupplyChainLink(
    link_key="FABLESS_FOUNDRY",
    title="晶圓代工與設計鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="MACRO_CORE",
    drivers=[
        "XBRL:NVDA:revenue",
        "XBRL:AMD:revenue",
        "XBRL:QCOM:revenue",
        "XBRL:AVGO:revenue",
    ],
    followers=["TWSE:2330", "TSM"],
    description="晶片設計廠營收成長與庫存回補週期，直接牽動台積電先進製程產能利用率。",
    lead_lag_quarters="1Q",
)

LINK_BRAND_RETAIL_INVENTORY = SupplyChainLink(
    link_key="BRAND_RETAIL_INVENTORY",
    title="零售與品牌存貨周轉鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="MACRO_CORE",
    drivers=["XBRL:WMT:dio", "XBRL:COST:dio", "XBRL:TGT:dio"],
    followers=["PG", "KO", "PEP"],
    description="沃爾瑪/Costco 存貨周轉天數（DIO）上升意味渠道堵塞，反向壓制上游品牌廠出貨動能。",
    lead_lag_quarters="1-2Q",
)

LINK_AIR_TRAVEL = SupplyChainLink(
    link_key="AIR_TRAVEL",
    title="航空運輸實體客流",
    link_type="NOWCAST",
    experimental=False,
    pillar="MACRO_CORE",
    drivers=["TSA:THROUGHPUT"],
    followers=["DAL", "UAL", "LUV"],
    description="TSA 每日安檢客流之 28 日均值，高頻臨近預測美國三大航司當季載客營收。",
    lead_lag_quarters="0Q (NOWCAST)",
)

LINK_RAIL_FREIGHT = SupplyChainLink(
    link_key="RAIL_FREIGHT",
    title="全美鐵路重工業貨運",
    link_type="NOWCAST",
    experimental=False,
    pillar="MACRO_CORE",
    drivers=["FRED:RAILFRTCARLOADSD11"],
    followers=["UNP", "CSX", "NSC"],
    description="鐵路貨運車皮裝載量月增長率，先行驗證北美一級鐵路運輸商實體貨運景氣。",
    lead_lag_quarters="0Q (NOWCAST)",
)

# ============================================================================
# 3.2 商業太空與國防前沿鏈 (Space Economy & Defense - 5 條)
# ============================================================================

LINK_SPACE_CONSTELLATION_LAUNCH = SupplyChainLink(
    link_key="SPACE_CONSTELLATION_LAUNCH",
    title="衛星星系發射載具鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="SPACE_DEFENSE",
    drivers=[
        "XBRL:SPCX:capex",
        "XBRL:ASTS:capex",
        "XBRL:IRDM:capex",
        "XBRL:SATS:capex",
    ],
    followers=["RKLB", "RDW", "LUNR"],
    description="商業航天核心巨頭 SpaceX (SPCX) 與星系營運商資本支出，直接轉化為火箭發射商與衛星平台製造商的在手合約。",
    lead_lag_quarters="1-2Q",
)

LINK_SPACE_STARLINK_BROADBAND_CHAIN = SupplyChainLink(
    link_key="SPACE_STARLINK_BROADBAND_CHAIN",
    title="Starlink 衛星終端與關鍵組件鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="SPACE_DEFENSE",
    drivers=["XBRL:SPCX:revenue", "XBRL:SPCX:capex"],
    followers=["TWSE:2313", "TWSE:6285", "TWSE:2314", "TPEX:3491"],
    description="SpaceX (SPCX) 之 Starlink 寬頻用戶擴張與 Direct-to-Cell 衛星部署，向下游台灣核心高頻 PCB、天線與微波模組釋放採購訂單。",
    lead_lag_quarters="1-2Q",
)

LINK_SPACE_STARLINK_TW_NOWCAST = SupplyChainLink(
    link_key="SPACE_STARLINK_TW_NOWCAST",
    title="低軌衛星台灣供應鏈高頻預測",
    link_type="NOWCAST",
    experimental=False,
    pillar="SPACE_DEFENSE",
    drivers=["TWSE:2313", "TWSE:6285", "TWSE:2314", "TPEX:3491"],
    followers=["SPCX", "IRDM", "VSAT", "RKLB"],
    description="以台系地面接收站與衛星天線零組件高頻月營收加總，領先臨近預測包含 SpaceX (SPCX) 在內全球低軌衛星整體出貨與季度業績動能。",
    lead_lag_quarters="0Q (NOWCAST)",
)

LINK_SPACE_DEFENSE_PRIME = SupplyChainLink(
    link_key="SPACE_DEFENSE_PRIME",
    title="國防太空主承包商傳導鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="SPACE_DEFENSE",
    drivers=[
        "XBRL:LMT:rpo",
        "XBRL:NOC:rpo",
        "XBRL:LHX:rpo",
        "XBRL:BA:rpo",
        "XBRL:SPCX:rpo",
    ],
    followers=["KTOS", "MRCY", "TDY", "HEI"],
    description="國防軍工巨頭與 SpaceX Starshield 之未履行國防訂單積壓（RPO），在後續 2–4 季依序向雷達、航電與次系統供應商釋放採購。",
    lead_lag_quarters="2-4Q",
)

LINK_SPACE_EO_DATA = SupplyChainLink(
    link_key="SPACE_EO_DATA",
    title="太空地球遙測情資鏈",
    link_type="NOWCAST",
    experimental=True,
    pillar="SPACE_DEFENSE",
    drivers=["XBRL:LMT:rpo", "XBRL:NOC:rpo", "XBRL:LHX:rpo"],
    followers=["PL", "BKSY"],
    description="國防部與政府情報機構對主承包商之機密預算分配，高度連動商業光學/雷達遙測資料的年化合約總值（ACV）。",
    lead_lag_quarters="0-1Q (NOWCAST)",
)

# ============================================================================
# 3.3 先進科技生態矩陣 (Frontier & Advanced Technology - 7 條)
# ============================================================================

LINK_ADV_SEMI_COWOS_CPO = SupplyChainLink(
    link_key="ADV_SEMI_COWOS_CPO",
    title="先進封裝與次世代光電",
    link_type="CAUSAL",
    experimental=False,
    pillar="FRONTIER_TECH",
    drivers=[
        "XBRL:NVDA:capex",
        "XBRL:AVGO:revenue",
        "XBRL:MRVL:revenue",
        "XBRL:AMD:revenue",
        "XBRL:SPCX:capex",
    ],
    followers=[
        "TSM",
        "COHR",
        "LITE",
        "TWSE:3450",
        "TWSE:3081",
        "TWSE:2330",
        "TWSE:3711",
    ],
    description="前沿 AI / 超級智慧模型算力需求直接推升 2.5D/3D (CoWoS) 先進封裝產能利用率與矽光子（CPO）光收發模組訂單。",
    lead_lag_quarters="1-2Q",
)

LINK_ADV_SEMI_WFE_EQUIPMENT = SupplyChainLink(
    link_key="ADV_SEMI_WFE_EQUIPMENT",
    title="半導體前端微影與製程設備",
    link_type="CAUSAL",
    experimental=False,
    pillar="FRONTIER_TECH",
    drivers=["XBRL:TSM:capex", "XBRL:INTC:capex", "XBRL:MU:capex"],
    followers=["ASML", "AMAT", "LRCX", "KLAC"],
    description="全球頂級晶圓廠與存儲廠資本支出指引，在 2–3 季後結算為微影（EUV/DUV）、薄膜沉積與晶圓檢測設備商訂單。",
    lead_lag_quarters="2-3Q",
)

LINK_ADV_ENERGY_NUCLEAR_SMR = SupplyChainLink(
    link_key="ADV_ENERGY_NUCLEAR_SMR",
    title="AI / 超算能耗、SMR 核電與清潔能源",
    link_type="CAUSAL",
    experimental=True,
    pillar="FRONTIER_TECH",
    drivers=[
        "XBRL:MSFT:capex",
        "XBRL:AMZN:capex",
        "XBRL:GOOGL:capex",
        "XBRL:META:capex",
        "XBRL:SPCX:capex",
    ],
    followers=["CEG", "VST", "GEV", "CCJ", "SMR", "OKLO"],
    description="超大規模資料中心與超級智能（SpaceXSI Colossus 園區）龐大能源缺口促成核電購電協議（PPA）與小型模組反應爐（SMR）開發。",
    lead_lag_quarters="2-4Q",
)

LINK_ADV_ENERGY_THERMAL_GRID = SupplyChainLink(
    link_key="ADV_ENERGY_THERMAL_GRID",
    title="次世代液冷散熱與微電網儲能",
    link_type="CAUSAL",
    experimental=False,
    pillar="FRONTIER_TECH",
    drivers=[
        "XBRL:MSFT:capex",
        "XBRL:AMZN:capex",
        "XBRL:META:capex",
        "XBRL:SPCX:capex",
    ],
    followers=[
        "VRT",
        "ETN",
        "GEV",
        "FLNC",
        "TSLA",
        "TWSE:2308",
        "TWSE:3017",
        "TWSE:3324",
    ],
    description="百萬瓩級超算中心功率密度突破極限，驅動全液冷機櫃、CDU 散熱與特高壓變壓設備採購暴增。",
    lead_lag_quarters="1-2Q",
)

LINK_ADV_AUTO_MOBILITY_TW_NOWCAST = SupplyChainLink(
    link_key="ADV_AUTO_MOBILITY_TW_NOWCAST",
    title="自主移動載具台灣供應鏈預測",
    link_type="NOWCAST",
    experimental=False,
    pillar="FRONTIER_TECH",
    drivers=[
        "TWSE:3665",
        "TWSE:1536",
        "TWSE:2308",
        "TWSE:3008",
        "TPEX:6279",
    ],
    followers=["TSLA", "RIVN", "MBLY"],
    description="以台系核心動力線束、減速齒輪、ADAS 光學鏡頭與車用電子月營收加總，高頻臨近預測全球智慧電動車與 Robotaxi 平台出貨動能。",
    lead_lag_quarters="0Q (NOWCAST)",
)

LINK_ADV_AUTO_FLEET_DEMAND = SupplyChainLink(
    link_key="ADV_AUTO_FLEET_DEMAND",
    title="全美汽車總體景氣需求鏈",
    link_type="CAUSAL",
    experimental=False,
    pillar="FRONTIER_TECH",
    drivers=["FRED:TOTALSA"],
    followers=["TSLA", "RIVN", "GM", "F"],
    description="全美汽車季調後折年率銷量（SAAR）定義乘用車市場總需求天花板，驗證車廠整體交付指引的宏觀可行性。",
    lead_lag_quarters="1Q",
)

LINK_ADV_ROBOTICS_EMBODIED_NOWCAST = SupplyChainLink(
    link_key="ADV_ROBOTICS_EMBODIED_NOWCAST",
    title="實體 AI、具身智慧與精密傳動",
    link_type="NOWCAST",
    experimental=True,
    pillar="FRONTIER_TECH",
    drivers=[
        "TWSE:2049",
        "TWSE:1590",
        "TWSE:1536",
        "TPEX:4576",
    ],
    followers=["TSLA", "NVDA", "ISRG"],
    description="具身人形機器人處於樣機驗證與產線試產階段，透過精密行星機構、滾珠螺桿與微型伺服驅動廠月營收觀察實體組件拉貨。",
    lead_lag_quarters="0Q (NOWCAST)",
)

# 擴充前沿鏈條（量子運算）
LINK_ADV_QUANTUM_COMPUTING = SupplyChainLink(
    link_key="ADV_QUANTUM_COMPUTING",
    title="量子運算與前沿計算架構",
    link_type="NOWCAST",
    experimental=True,
    pillar="FRONTIER_TECH",
    drivers=["XBRL:IBM:rpo"],
    followers=["IONQ", "RGTI", "QBTS"],
    description="國家安全實驗室（DOE/DOD）之先導量子運算專案撥款與合約（RPO），臨近驗證商業純量子系統之概念驗證（POC）營收。",
    lead_lag_quarters="0-1Q (NOWCAST)",
)

LINK_ROBOTICS_EMBODIED_NOWCAST: SupplyChainLink = LINK_ADV_ROBOTICS_EMBODIED_NOWCAST

# 核心 17 條產業鏈列表 (5 總體 + 5 太空 + 7 先進科技)
CORE_SUPPLY_CHAIN_LINKS: list[SupplyChainLink] = [
    # 3.1 總體核心 (5 條)
    LINK_AI_CAPEX,
    LINK_FABLESS_FOUNDRY,
    LINK_BRAND_RETAIL_INVENTORY,
    LINK_AIR_TRAVEL,
    LINK_RAIL_FREIGHT,
    # 3.2 商業太空與國防 (5 條)
    LINK_SPACE_CONSTELLATION_LAUNCH,
    LINK_SPACE_STARLINK_BROADBAND_CHAIN,
    LINK_SPACE_STARLINK_TW_NOWCAST,
    LINK_SPACE_DEFENSE_PRIME,
    LINK_SPACE_EO_DATA,
    # 3.3 先進科技生態矩陣 (7 條)
    LINK_ADV_SEMI_COWOS_CPO,
    LINK_ADV_SEMI_WFE_EQUIPMENT,
    LINK_ADV_ENERGY_NUCLEAR_SMR,
    LINK_ADV_ENERGY_THERMAL_GRID,
    LINK_ADV_AUTO_MOBILITY_TW_NOWCAST,
    LINK_ADV_AUTO_FLEET_DEMAND,
    LINK_ROBOTICS_EMBODIED_NOWCAST,
]

# 預設產業鏈列表（精準 17 條）
SUPPLY_CHAIN_LINKS: list[SupplyChainLink] = CORE_SUPPLY_CHAIN_LINKS

# 全量產業鏈列表（包含擴充鏈條）
ALL_SUPPLY_CHAIN_LINKS: list[SupplyChainLink] = [
    *CORE_SUPPLY_CHAIN_LINKS,
    LINK_ADV_QUANTUM_COMPUTING,
]

# 快速鍵值索引表
_KEY_INDEX_MAP: dict[str, SupplyChainLink] = {
    link.link_key: link for link in ALL_SUPPLY_CHAIN_LINKS
}


def get_supply_chain_link(link_key: str) -> SupplyChainLink | None:
    """根據鏈條代碼查詢產業鏈定義。"""
    return _KEY_INDEX_MAP.get(link_key.strip().upper())


def _extract_ticker_symbols(identifier: str) -> list[str]:
    """從驅動端或跟隨端識別碼萃取純股票代碼（支援 XBRL、TWSE、TPEx 等前綴）。"""
    ident = identifier.strip().upper()
    if ident.startswith("XBRL:"):
        parts = ident.split(":")
        if len(parts) >= 2:
            return [parts[1]]
    if ident.startswith(("TWSE:", "TPEX:")):
        parts = ident.split(":")
        if len(parts) >= 2:
            ticker = parts[1]
            # 針對台積電等常見雙重掛牌進行關聯映射
            if ticker == "2330":
                return ["2330", "TSM"]
            return [ticker]
    if ident == "TSM":
        return ["TSM", "2330"]
    return [ident]


def extract_symbols_from_link(link: SupplyChainLink) -> list[str]:
    """從產業鏈之驅動端與跟隨端識別碼萃取所有關聯股票代碼清單（去重並排序）。"""
    symbols_set: set[str] = set()
    for ident in link.drivers:
        for sym in _extract_ticker_symbols(ident):
            if sym and not sym.startswith(("TSA", "FRED")):
                symbols_set.add(sym)
    for ident in link.followers:
        for sym in _extract_ticker_symbols(ident):
            if sym and not sym.startswith(("TSA", "FRED")):
                symbols_set.add(sym)
    return sorted(symbols_set)


def get_links_for_symbol(symbol: str) -> list[SupplyChainLink]:
    """查詢與特定標的相關（身為驅動端或跟隨端）的所有產業鏈。"""
    target = symbol.strip().upper()
    # 支援去前綴比對
    if ":" in target:
        target = target.split(":")[-1]

    results: list[SupplyChainLink] = []
    for link in ALL_SUPPLY_CHAIN_LINKS:
        matched = False
        # 比對驅動端
        for driver in link.drivers:
            extracted = _extract_ticker_symbols(driver)
            if target in extracted or target == driver.upper():
                matched = True
                break
        # 比對跟隨端
        if not matched:
            for follower in link.followers:
                extracted = _extract_ticker_symbols(follower)
                if target in extracted or target == follower.upper():
                    matched = True
                    break
        if matched:
            results.append(link)
    return results


def get_all_links(include_experimental: bool = True) -> list[SupplyChainLink]:
    """取得所有產業鏈定義清單（可選是否過濾實驗性鏈條）。"""
    if include_experimental:
        return list(SUPPLY_CHAIN_LINKS)
    return [link for link in SUPPLY_CHAIN_LINKS if not link.experimental]


def get_links_by_pillar(pillar: str) -> list[SupplyChainLink]:
    """根據支柱領域過濾產業鏈定義。"""
    target_pillar = pillar.strip().upper()
    return [link for link in ALL_SUPPLY_CHAIN_LINKS if link.pillar == target_pillar]
