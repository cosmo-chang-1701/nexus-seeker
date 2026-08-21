"""Unit tests for Macro Calendar translation and normalization engine."""

from typing import Any
from market_analysis.macro_calendar_translator import (
    translate_macro_event,
)
from cogs.embed_builders.market_embeds import (
    build_market_macro_overview_embed,
    build_calendar_embed,
    create_market_calendar_embed,
)
from services.calendar_service import EconomicEvent


def test_inflation_translations() -> None:
    """測試通膨與物價指數標準譯名。"""
    assert translate_macro_event("Core CPI YoY") == "核心 CPI 年增率"
    assert translate_macro_event("Core CPI MoM") == "核心 CPI 月增率"
    assert translate_macro_event("Core CPI") == "核心 CPI"
    assert translate_macro_event("CPI YoY") == "CPI 年增率"
    assert translate_macro_event("CPI MoM") == "CPI 月增率"
    assert translate_macro_event("CPI") == "CPI (消費者物價指數)"
    assert translate_macro_event("Core PCE Price Index YoY") == "核心 PCE 年增率"
    assert translate_macro_event("Core PCE Price Index MoM") == "核心 PCE 月增率"
    assert translate_macro_event("Core PCE Price Index") == "核心 PCE 物價指數"
    assert translate_macro_event("PCE Price Index YoY") == "PCE 年增率"
    assert translate_macro_event("Core PPI YoY") == "核心 PPI 年增率"
    assert translate_macro_event("PPI MoM") == "PPI 月增率"
    assert (
        translate_macro_event("Michigan 5 Year Inflation Expectations Final")
        == "密大 5 年通膨預期 (終值)"
    )
    assert (
        translate_macro_event("Michigan 5-Year Inflation Expectations Final")
        == "密大 5 年通膨預期 (終值)"
    )
    assert (
        translate_macro_event("Michigan Inflation Expectations Final")
        == "密大 1 年通膨預期 (終值)"
    )
    assert (
        translate_macro_event("NY Fed 1-Year Inflation Expectations")
        == "紐約聯儲 1 年通膨預期"
    )


def test_employment_translations() -> None:
    """測試就業與勞動力市場標準譯名。"""
    assert translate_macro_event("Non Farm Payrolls") == "非農就業人數"
    assert translate_macro_event("Non-Farm Payrolls") == "非農就業人數"
    assert translate_macro_event("Nonfarm Payrolls") == "非農就業人數"
    assert translate_macro_event("ADP Employment Change") == "ADP 就業人數 (小非農)"
    assert translate_macro_event("Unemployment Rate") == "失業率"
    assert translate_macro_event("Underemployment Rate") == "U6 廣義失業率"
    assert translate_macro_event("Initial Jobless Claims") == "初領失業救濟金人數"
    assert (
        translate_macro_event("Continuing Jobless Claims") == "連續請領失業救濟金人數"
    )
    assert translate_macro_event("JOLTs Job Openings") == "JOLTs 職位空缺數"
    assert translate_macro_event("Average Hourly Earnings YoY") == "平均時薪年增率"
    assert (
        translate_macro_event("Employment Cost Index QoQ")
        == "就業成本指數 (ECI) 季增率"
    )
    assert translate_macro_event("Challenger Job Cuts") == "挑戰者企業裁員人數"


def test_fed_and_monetary_policy_translations() -> None:
    """測試聯準會與貨幣政策標準譯名。"""
    assert translate_macro_event("Fed Interest Rate Decision") == "聯準會利率決策"
    assert translate_macro_event("Federal Funds Rate") == "聯邦基金利率"
    assert translate_macro_event("FOMC Rate Decision") == "FOMC 利率決策"
    assert translate_macro_event("FOMC Statement") == "FOMC 貨幣政策聲明"
    assert (
        translate_macro_event("FOMC Press Conference")
        == "FOMC 利率決策記者會 (鮑爾記者會)"
    )
    assert (
        translate_macro_event("FOMC Economic Projections")
        == "FOMC 經濟預測摘要 (點陣圖)"
    )
    assert translate_macro_event("FOMC Meeting Minutes") == "FOMC 會議紀要"
    assert translate_macro_event("Fed Beige Book") == "聯準會褐皮書"
    assert translate_macro_event("Fed Balance Sheet") == "聯準會資產負債表"


def test_dynamic_fed_official_speeches() -> None:
    """測試聯準會官員演講動態辨識。"""
    assert translate_macro_event("Fed Chair Powell Speaks") == "聯準會主席鮑爾發言"
    assert (
        translate_macro_event("Fed Chair Powell Testimony") == "聯準會主席鮑爾國會聽證"
    )
    assert translate_macro_event("Fed Governor Waller Speaks") == "聯準會理事華勒發言"
    assert translate_macro_event("Fed Governor Bowman Speaks") == "聯準會理事鮑曼發言"
    assert translate_macro_event("Fed Kashkari Speaks") == "聯準會卡斯哈里發言"
    assert translate_macro_event("Fed Daly Speaks") == "聯準會戴莉發言"
    assert translate_macro_event("FOMC Member Williams Speaks") == "聯準會威廉斯發言"
    assert translate_macro_event("Fed Musalem Speech") == "聯準會穆薩林發言"


def test_pmis_and_regional_fed_surveys() -> None:
    """測試 PMI 與各地區聯儲景氣調查標準譯名。"""
    assert translate_macro_event("ISM Manufacturing PMI") == "ISM 製造業 PMI"
    assert translate_macro_event("ISM Services PMI") == "ISM 服務業 PMI"
    assert (
        translate_macro_event("S&P Global Manufacturing PMI Flash")
        == "S&P 製造業 PMI (初值)"
    )
    assert (
        translate_macro_event("S&P Global Services PMI Final")
        == "S&P 服務業 PMI (終值)"
    )
    assert translate_macro_event("S&P Global Composite PMI") == "S&P 綜合 PMI"
    assert translate_macro_event("Chicago PMI") == "芝加哥 PMI"
    assert (
        translate_macro_event("Philadelphia Fed Manufacturing Index")
        == "費城聯儲製造業指數"
    )
    assert (
        translate_macro_event("Empire State Manufacturing Index")
        == "紐約聯儲帝國州製造業指數"
    )
    assert (
        translate_macro_event("Richmond Fed Manufacturing Index")
        == "里奇蒙聯儲製造業指數"
    )
    assert (
        translate_macro_event("Dallas Fed Manufacturing Index")
        == "達拉斯聯儲製造業指數"
    )


def test_housing_retail_energy_treasury_auctions() -> None:
    """測試房地產、零售、能源與國債拍賣標準譯名。"""
    assert translate_macro_event("Building Permits") == "營建許可"
    assert translate_macro_event("Housing Starts") == "新屋開工"
    assert translate_macro_event("Existing Home Sales") == "成屋銷售"
    assert translate_macro_event("New Home Sales") == "新屋銷售"
    assert translate_macro_event("Retail Sales MoM") == "零售銷售月增率"
    assert (
        translate_macro_event("Retail Sales Ex Autos MoM")
        == "核心零售銷售月增率 (除汽車)"
    )
    assert translate_macro_event("CB Consumer Confidence") == "CB 消費者信心指數"
    assert (
        translate_macro_event("Michigan Consumer Sentiment Final")
        == "密大消費者信心指數 (終值)"
    )
    assert translate_macro_event("Industrial Production MoM") == "工業生產月增率"
    assert translate_macro_event("Durable Goods Orders MoM") == "耐久財訂單月增率"
    assert translate_macro_event("EIA Crude Oil Stocks Change") == "EIA 原油庫存變動"
    assert (
        translate_macro_event("Baker Hughes Oil Rig Count") == "貝克休斯石油鑽井平台數"
    )
    assert (
        translate_macro_event("Treasury Refunding Announcement")
        == "美財政部季度發債計畫 (QRA)"
    )
    assert translate_macro_event("10-Year Note Auction") == "10 年期美債拍賣"
    assert translate_macro_event("30-Year Bond Auction") == "30 年期美債拍賣"
    assert translate_macro_event("2-Year Note Auction") == "2 年期美債拍賣"


def test_edge_cases_and_fallbacks() -> None:
    """測試邊界條件與未收錄詞條。"""
    assert translate_macro_event("") == ""
    assert translate_macro_event("   ") == ""
    assert (
        translate_macro_event("Unknown Unique Indicator") == "Unknown Unique Indicator"
    )
    # Case insensitivity check
    assert translate_macro_event("cpi yoy") == "CPI 年增率"
    assert translate_macro_event("non farm payrolls") == "非農就業人數"


def test_embed_builders_integration_with_macro_translation() -> None:
    """測試 Embed 渲染層對翻譯引擎的整合。"""
    # 1. build_market_macro_overview_embed
    macro_data: dict[str, Any] = {
        "spx": 5200.0,
        "vix": 16.5,
        "us10y": 4.25,
        "gamma_flip_line": 5150.0,
        "wti": 75.0,
        "rrp": 420.5,
        "fed_balance": 7.25,
        "cpi_nfp_calendar": "08/20 FOMC 利率決策\n └─ 09/05 非農就業人數",
        "fear_greed": 55.0,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "rrp_change_30d": 5.0,
    }
    overview_embed = build_market_macro_overview_embed(macro_data)
    overview_fields = {f.name: str(f.value) for f in overview_embed.fields}
    assert "📅 總經事件公布日程 (Macro Calendar)" in overview_fields
    assert (
        "08/20 FOMC 利率決策" in overview_fields["📅 總經事件公布日程 (Macro Calendar)"]
    )
    assert (
        "09/05 非農就業人數" in overview_fields["📅 總經事件公布日程 (Macro Calendar)"]
    )

    # 2. create_market_calendar_embed with English event
    ev1 = EconomicEvent(
        event="Core CPI YoY",
        time="2026-09-10T12:30:00Z",
        impact="high",
        country="US",
        tte_hours=48.0,
    )
    cal_embed = create_market_calendar_embed([ev1])
    assert len(cal_embed.fields) == 1
    assert "🔴 核心 CPI 年增率 (US)" == str(cal_embed.fields[0].name)

    # 3. build_calendar_embed with English events
    ev2 = EconomicEvent(
        event="Non Farm Payrolls",
        time="2026-09-05T12:30:00Z",
        impact="high",
        country="US",
        tte_hours=24.0,
    )
    full_cal_embed = build_calendar_embed(
        macro_events=[ev2],
        earnings_events=[],
        fedwatch_prob=0.85,
    )
    cal_fields = {f.name: str(f.value) for f in full_cal_embed.fields}
    assert "💡 當月重要總經事件 (Macro Events) — 數據源: Edge Scraper" in cal_fields
    assert (
        "非農就業人數"
        in cal_fields["💡 當月重要總經事件 (Macro Events) — 數據源: Edge Scraper"]
    )
