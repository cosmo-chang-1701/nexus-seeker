"""Macro Economic Calendar Translation and Normalization Engine.

Provides unified English-to-Traditional Chinese mapping for 150+ macroeconomic indicators,
central bank policy events, Treasury auctions, PMIs, and dynamic Fed official speech parsing.
"""

from __future__ import annotations
import re
from typing import Final

# ---------------------------------------------------------------------------
# Fed Officials & Speakers Mapping (聯準會官員人名對照表)
# ---------------------------------------------------------------------------
FED_OFFICIALS_MAP: Final[dict[str, str]] = {
    "Powell": "鮑爾",
    "Waller": "華勒",
    "Jefferson": "傑佛森",
    "Bowman": "鮑曼",
    "Barr": "巴爾",
    "Cook": "庫克",
    "Kugler": "庫格勒",
    "Williams": "威廉斯",
    "Logan": "洛根",
    "Bostic": "波斯提克",
    "Goolsbee": "古爾斯比",
    "Barkin": "巴爾金",
    "Daly": "戴莉",
    "Kashkari": "卡斯哈里",
    "Harker": "哈克",
    "Musalem": "穆薩林",
    "Hammack": "哈瑪克",
    "Schmid": "施密德",
    "Collins": "柯林斯",
    "Mester": "梅斯特",
    "Bullard": "布拉德",
    "Brainard": "布蘭納德",
    "Yellen": "葉倫",
    "Clarida": "克拉里達",
    "Quarles": "夸爾斯",
    "George": "喬治",
    "Rosengren": "羅森格倫",
    "Kaplan": "卡普蘭",
    "Evans": "埃文斯",
    "Lockhart": "洛克哈特",
    "Plosser": "普羅索",
    "Fisher": "費雪",
    "Bernanke": "柏南奇",
    "Greenspan": "葛林斯潘",
}

# ---------------------------------------------------------------------------
# Master Macro Event Translation Dictionary (總經事件標準中英對照表)
# ---------------------------------------------------------------------------
_RAW_MACRO_EVENT_TRANSLATIONS: dict[str, str] = {
    # --- 1. 通膨與物價指數 (Inflation & Price Indices) ---
    "Core CPI YoY": "核心 CPI 年增率",
    "Core CPI MoM": "核心 CPI 月增率",
    "Core CPI": "核心 CPI",
    "CPI YoY": "CPI 年增率",
    "CPI MoM": "CPI 月增率",
    "CPI s.a.": "CPI (經季調)",
    "CPI s.a": "CPI (經季調)",
    "CPI": "CPI (消費者物價指數)",
    "Consumer Price Index YoY": "CPI 年增率",
    "Consumer Price Index MoM": "CPI 月增率",
    "Consumer Price Index": "CPI (消費者物價指數)",
    "Core PCE Price Index YoY": "核心 PCE 年增率",
    "Core PCE Price Index MoM": "核心 PCE 月增率",
    "Core PCE Price Index": "核心 PCE 物價指數",
    "Core PCE YoY": "核心 PCE 年增率",
    "Core PCE MoM": "核心 PCE 月增率",
    "Core PCE": "核心 PCE",
    "PCE Price Index YoY": "PCE 年增率",
    "PCE Price Index MoM": "PCE 月增率",
    "PCE Price Index": "PCE (個人消費支出物價指數)",
    "PCE YoY": "PCE 年增率",
    "PCE MoM": "PCE 月增率",
    "PCE": "PCE 物價指數",
    "Core PPI YoY": "核心 PPI 年增率",
    "Core PPI MoM": "核心 PPI 月增率",
    "Core PPI": "核心 PPI",
    "PPI YoY": "PPI 年增率",
    "PPI MoM": "PPI 月增率",
    "PPI": "PPI (生產者物價指數)",
    "Producer Price Index YoY": "PPI 年增率",
    "Producer Price Index MoM": "PPI 月增率",
    "Producer Price Index": "PPI (生產者物價指數)",
    "Export Prices YoY": "出口物價年增率",
    "Export Prices MoM": "出口物價月增率",
    "Export Price Index": "出口物價指數",
    "Import Prices YoY": "進口物價年增率",
    "Import Prices MoM": "進口物價月增率",
    "Import Price Index": "進口物價指數",
    "Michigan 5 Year Inflation Expectations Final": "密大 5 年通膨預期 (終值)",
    "Michigan 5 Year Inflation Expectations Prel": "密大 5 年通膨預期 (初值)",
    "Michigan 5 Year Inflation Expectations": "密大 5 年通膨預期",
    "Michigan 5-Year Inflation Expectations Final": "密大 5 年通膨預期 (終值)",
    "Michigan 5-Year Inflation Expectations Prel": "密大 5 年通膨預期 (初值)",
    "Michigan 5-Year Inflation Expectations": "密大 5 年通膨預期",
    "Michigan 5 Yr Inflation Expectations Final": "密大 5 年通膨預期 (終值)",
    "Michigan 5 Yr Inflation Expectations Prel": "密大 5 年通膨預期 (初值)",
    "Michigan 5 Yr Inflation Expectations": "密大 5 年通膨預期",
    "Michigan Inflation Expectations Final": "密大 1 年通膨預期 (終值)",
    "Michigan Inflation Expectations Prel": "密大 1 年通膨預期 (初值)",
    "Michigan Inflation Expectations": "密大通膨預期",
    "NY Fed 1-Year Inflation Expectations": "紐約聯儲 1 年通膨預期",
    "NY Fed 3-Year Inflation Expectations": "紐約聯儲 3 年通膨預期",
    # --- 2. 就業與勞動力市場 (Employment & Labor Market) ---
    "Non Farm Payrolls": "非農就業人數",
    "Non-Farm Payrolls": "非農就業人數",
    "Nonfarm Payrolls": "非農就業人數",
    "ADP Employment Change": "ADP 就業人數 (小非農)",
    "ADP Nonfarm Employment Change": "ADP 就業人數 (小非農)",
    "Unemployment Rate": "失業率",
    "Underemployment Rate": "U6 廣義失業率",
    "U6 Unemployment Rate": "U6 廣義失業率",
    "Initial Jobless Claims 4-week Average": "初領失業金 4 週移動平均",
    "Jobless Claims 4-week Average": "初領失業金 4 週移動平均",
    "Initial Jobless Claims": "初領失業救濟金人數",
    "Continuing Jobless Claims": "連續請領失業救濟金人數",
    "JOLTs Job Openings": "JOLTs 職位空缺數",
    "JOLTs Job Quits": "JOLTs 自願離職人數",
    "JOLTs Layoffs and Discharges": "JOLTs 裁員與解僱人數",
    "Average Hourly Earnings YoY": "平均時薪年增率",
    "Average Hourly Earnings MoM": "平均時薪月增率",
    "Average Weekly Hours": "平均每週工時",
    "Labor Force Participation Rate": "勞動力參與率",
    "Employment Cost Index QoQ": "就業成本指數 (ECI) 季增率",
    "Employment Cost Index YoY": "就業成本指數 (ECI) 年增率",
    "Employment Cost Index": "就業成本指數 (ECI)",
    "Challenger Job Cuts YoY": "挑戰者企業裁員年增率",
    "Challenger Job Cuts": "挑戰者企業裁員人數",
    # --- 3. 聯準會與貨幣政策 (Central Bank & Monetary Policy) ---
    "Fed Interest Rate Decision": "聯準會利率決策",
    "Federal Funds Rate": "聯邦基金利率",
    "Interest Rate Decision": "聯準會利率決策",
    "FOMC Rate Decision": "FOMC 利率決策",
    "FOMC Statement": "FOMC 貨幣政策聲明",
    "FOMC Press Conference": "FOMC 利率決策記者會 (鮑爾記者會)",
    "FOMC Economic Projections": "FOMC 經濟預測摘要 (點陣圖)",
    "FOMC Meeting Minutes": "FOMC 會議紀要",
    "Fed Minutes": "聯準會會議紀要",
    "Fed Beige Book": "聯準會褐皮書",
    "Beige Book": "聯準會褐皮書",
    "Fed Balance Sheet": "聯準會資產負債表",
    "Fed Chair Powell Speaks": "聯準會主席鮑爾發言",
    "Fed Chair Powell Speech": "聯準會主席鮑爾發言",
    "Fed Chair Powell Testimony": "聯準會主席鮑爾國會聽證",
    # --- 4. 經濟成長與 GDP (Economic Growth & National Accounts) ---
    "GDP Growth Rate QoQ Adv": "GDP 成長率季增年率 (初值)",
    "GDP Growth Rate QoQ 2nd Est": "GDP 成長率季增年率 (修訂值)",
    "GDP Growth Rate QoQ Prel": "GDP 成長率季增年率 (修訂值)",
    "GDP Growth Rate QoQ Final": "GDP 成長率季增年率 (終值)",
    "GDP Growth Rate QoQ": "GDP 成長率 (季增年率)",
    "GDP Growth Rate YoY": "GDP 成長率 (年增率)",
    "GDP Growth Rate Adv": "GDP 成長率 (初值)",
    "GDP Growth Rate Prel": "GDP 成長率 (修訂值)",
    "GDP Growth Rate Final": "GDP 成長率 (終值)",
    "GDP Growth Rate": "GDP 成長率",
    "Real GDP QoQ": "實質 GDP 季增率",
    "Real GDP": "實質 GDP",
    "GDP Price Index QoQ": "GDP 平減指數季增率",
    "GDP Price Index": "GDP 平減指數",
    "Personal Income MoM": "個人所得月增率",
    "Personal Income YoY": "個人所得年增率",
    "Personal Spending MoM": "個人支出月增率",
    "Personal Spending YoY": "個人支出年增率",
    "Real Personal Consumption MoM": "實質個人消費月增率",
    "Current Account": "經常帳餘額",
    # --- 5. 景氣調查與 PMI (PMIs & Business Surveys) ---
    "ISM Manufacturing PMI": "ISM 製造業 PMI",
    "ISM Manufacturing Prices": "ISM 製造業價格指數",
    "ISM Manufacturing Employment": "ISM 製造業就業指數",
    "ISM Manufacturing New Orders": "ISM 製造業新訂單指數",
    "ISM Non-Manufacturing PMI": "ISM 非製造業 PMI",
    "ISM Services PMI": "ISM 服務業 PMI",
    "ISM Services Prices Paid": "ISM 服務業價格指數",
    "ISM Services Employment": "ISM 服務業就業指數",
    "ISM Services New Orders": "ISM 服務業新訂單指數",
    "S&P Global Manufacturing PMI Flash": "S&P 製造業 PMI (初值)",
    "S&P Global Manufacturing PMI Adv": "S&P 製造業 PMI (初值)",
    "S&P Global Manufacturing PMI Prel": "S&P 製造業 PMI (初值)",
    "S&P Global Manufacturing PMI Final": "S&P 製造業 PMI (終值)",
    "S&P Global Manufacturing PMI": "S&P 製造業 PMI",
    "S&P Global Services PMI Flash": "S&P 服務業 PMI (初值)",
    "S&P Global Services PMI Adv": "S&P 服務業 PMI (初值)",
    "S&P Global Services PMI Prel": "S&P 服務業 PMI (初值)",
    "S&P Global Services PMI Final": "S&P 服務業 PMI (終值)",
    "S&P Global Services PMI": "S&P 服務業 PMI",
    "S&P Global Composite PMI Flash": "S&P 綜合 PMI (初值)",
    "S&P Global Composite PMI Adv": "S&P 綜合 PMI (初值)",
    "S&P Global Composite PMI Prel": "S&P 綜合 PMI (初值)",
    "S&P Global Composite PMI Final": "S&P 綜合 PMI (終值)",
    "S&P Global Composite PMI": "S&P 綜合 PMI",
    "Chicago PMI": "芝加哥 PMI",
    "Philadelphia Fed Manufacturing Index": "費城聯儲製造業指數",
    "Philly Fed Manufacturing Index": "費城聯儲製造業指數",
    "Philly Fed Services Index": "費城聯儲服務業指數",
    "Philly Fed Employment": "費城聯儲就業指數",
    "Philly Fed New Orders": "費城聯儲新訂單指數",
    "Philly Fed Prices Paid": "費城聯儲物價支付指數",
    "Empire State Manufacturing Index": "紐約聯儲帝國州製造業指數",
    "NY Empire State Manufacturing Index": "紐約聯儲帝國州製造業指數",
    "Richmond Fed Manufacturing Index": "里奇蒙聯儲製造業指數",
    "Richmond Fed Services Index": "里奇蒙聯儲服務業指數",
    "Dallas Fed Manufacturing Index": "達拉斯聯儲製造業指數",
    "Dallas Fed Services Index": "達拉斯聯儲服務業指數",
    "Kansas Fed Manufacturing Index": "堪薩斯聯儲製造業指數",
    "Kansas Fed Services Index": "堪薩斯聯儲服務業指數",
    # --- 6. 消費、零售與信心 (Retail, Consumption & Sentiment) ---
    "Retail Sales MoM": "零售銷售月增率",
    "Retail Sales YoY": "零售銷售年增率",
    "Retail Sales Ex Autos MoM": "核心零售銷售月增率 (除汽車)",
    "Core Retail Sales MoM": "核心零售銷售月增率",
    "Retail Sales Ex Gas/Autos MoM": "零售銷售月增率 (除汽車與汽油)",
    "Retail Sales Control Group MoM": "零售銷售對照組月增率",
    "Retail Sales": "零售銷售",
    "Core Retail Sales": "核心零售銷售",
    "Retail Inventories Ex Autos MoM Adv": "除汽車外零售庫存月增率 (初值)",
    "Retail Inventories MoM": "零售庫存月增率",
    "Wholesale Inventories MoM Adv": "批發庫存月增率 (初值)",
    "Wholesale Inventories MoM": "批發庫存月增率",
    "Business Inventories MoM": "企業庫存月增率",
    "CB Consumer Confidence": "CB 消費者信心指數",
    "Michigan Consumer Sentiment Final": "密大消費者信心指數 (終值)",
    "Michigan Consumer Sentiment Prel": "密大消費者信心指數 (初值)",
    "Michigan Consumer Sentiment": "密大消費者信心指數",
    "Michigan Current Conditions Final": "密大現況指數 (終值)",
    "Michigan Current Conditions Prel": "密大現況指數 (初值)",
    "Michigan Current Conditions": "密大現況指數",
    "Michigan Consumer Expectations Final": "密大消費者預期指數 (終值)",
    "Michigan Consumer Expectations Prel": "密大消費者預期指數 (初值)",
    "Michigan Consumer Expectations": "密大消費者預期指數",
    # --- 7. 房地產與建築市場 (Housing & Real Estate) ---
    "Building Permits MoM": "營建許可月增率",
    "Building Permits Prel": "營建許可 (初值)",
    "Building Permits Final": "營建許可 (終值)",
    "Building Permits": "營建許可",
    "Housing Starts MoM": "新屋開工月增率",
    "Housing Starts": "新屋開工",
    "Existing Home Sales MoM": "成屋銷售月增率",
    "Existing Home Sales": "成屋銷售",
    "Pending Home Sales MoM": "待完成房屋銷售月增率",
    "Pending Home Sales YoY": "待完成房屋銷售年增率",
    "Pending Home Sales": "待完成房屋銷售",
    "New Home Sales MoM": "新屋銷售月增率",
    "New Home Sales": "新屋銷售",
    "Case-Shiller Home Price Index YoY": "標普/凱斯席勒房價指數年增率",
    "Case-Shiller Home Price Index MoM": "標普/凱斯席勒房價指數月增率",
    "S&P/Case-Shiller Home Price Index": "標普/凱斯席勒房價指數",
    "FHFA House Price Index MoM": "FHFA 房價指數月增率",
    "FHFA House Price Index YoY": "FHFA 房價指數年增率",
    "NAHB Housing Market Index": "NAHB 建商信心指數",
    "Construction Spending MoM": "營建支出月增率",
    "MBA 30-Year Mortgage Rate": "MBA 30 年期房貸利率",
    "MBA Mortgage Applications": "MBA 抵押貸款申請指數",
    "Mortgage Applications": "MBA 抵押貸款申請指數",
    "30-Year Mortgage Rate": "30 年期房貸利率",
    # --- 8. 工業生產與工廠訂單 (Industrial Production & Orders) ---
    "Industrial Production MoM": "工業生產月增率",
    "Industrial Production YoY": "工業生產年增率",
    "Industrial Production": "工業生產",
    "Manufacturing Production MoM": "製造業產出月增率",
    "Manufacturing Production YoY": "製造業產出年增率",
    "Capacity Utilization Rate": "產能利用率",
    "Durable Goods Orders MoM Adv": "耐久財訂單月增率 (初值)",
    "Durable Goods Orders MoM": "耐久財訂單月增率",
    "Core Durable Goods Orders MoM": "核心耐久財訂單月增率",
    "Durable Goods Orders Ex Trans MoM": "核心耐久財訂單月增率 (除運輸)",
    "Durable Goods Orders Ex Defense MoM": "耐久財訂單月增率 (除國防)",
    "Durable Goods Orders": "耐久財訂單",
    "Factory Orders MoM": "工廠訂單月增率",
    "Factory Orders Ex Trans MoM": "工廠訂單月增率 (除運輸)",
    "Factory Orders": "工廠訂單",
    "Goods Trade Balance Adv": "商品貿易帳 (初值)",
    "Goods Trade Balance": "商品貿易帳",
    "Trade Balance": "貿易帳餘額",
    # --- 9. 能源與大宗商品庫存 (Energy & Commodity Inventories) ---
    "EIA Crude Oil Stocks Change": "EIA 原油庫存變動",
    "Crude Oil Inventories": "EIA 原油庫存變動",
    "API Crude Oil Stock Change": "API 原油庫存變動",
    "EIA Gasoline Stocks Change": "EIA 汽油庫存變動",
    "EIA Gasoline Inventories": "EIA 汽油庫存變動",
    "EIA Distillate Fuel Oil Inventories": "EIA 蒸餾油庫存變動",
    "EIA Distillate Stocks Change": "EIA 蒸餾油庫存變動",
    "EIA Natural Gas Storage Change": "EIA 天然氣庫存變動",
    "Natural Gas Storage": "EIA 天然氣庫存變動",
    "Baker Hughes Oil Rig Count": "貝克休斯石油鑽井平台數",
    "Baker Hughes Total Rig Count": "貝克休斯總鑽井平台數",
    # --- 10. 國債拍賣與財政部公告 (Treasury Auctions & Refunding) ---
    "Treasury Refunding Announcement": "美財政部季度發債計畫 (QRA)",
    "Monthly Treasury Budget": "美國財政部月度預算",
    "2-Year Note Auction": "2 年期美債拍賣",
    "3-Year Note Auction": "3 年期美債拍賣",
    "5-Year Note Auction": "5 年期美債拍賣",
    "7-Year Note Auction": "7 年期美債拍賣",
    "10-Year Note Auction": "10 年期美債拍賣",
    "20-Year Bond Auction": "20 年期美債拍賣",
    "30-Year Bond Auction": "30 年期美債拍賣",
    "10-Year TIPS Auction": "10 年期 TIPS 通膨補償債券拍賣",
    "30-Year TIPS Auction": "30 年期 TIPS 通膨補償債券拍賣",
    "4-Week Bill Auction": "4 週國庫券拍賣",
    "8-Week Bill Auction": "8 週國庫券拍賣",
    "13-Week Bill Auction": "3 個月國庫券拍賣",
    "3-Month Bill Auction": "3 個月國庫券拍賣",
    "26-Week Bill Auction": "6 個月國庫券拍賣",
    "6-Month Bill Auction": "6 個月國庫券拍賣",
    "52-Week Bill Auction": "1 年期國庫券拍賣",
    "1-Year Bill Auction": "1 年期國庫券拍賣",
}

# Pre-sort translations by key length descending to guarantee longest match first
MACRO_EVENT_TRANSLATIONS: Final[dict[str, str]] = dict(
    sorted(
        _RAW_MACRO_EVENT_TRANSLATIONS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# Regex pattern to match Fed official speeches / testimony
_FED_SPEAKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:Fed(?:eral Reserve)?|FOMC)\s+(?:Chair(?:man)?|Governor|President|Member)?\s*([A-Za-z]+)\s+(?:Speaks|Speech|Testimony|Press Conference|Statement)",
    re.IGNORECASE,
)


class MacroCalendarTranslator:
    """Centralized Macro Economic Calendar Translator."""

    @staticmethod
    def translate(event_name: str) -> str:
        """Translate a raw macroeconomic event title into clean Traditional Chinese."""
        if not event_name or not isinstance(event_name, str):
            return ""

        clean_name: str = event_name.strip()
        if not clean_name:
            return ""

        # 0. Idempotency Check: Already translated to a known Chinese standard value
        if clean_name in MACRO_EVENT_TRANSLATIONS.values():
            return clean_name

        # 1. Exact match / Longest Substring token replacement
        for eng_key, chi_val in MACRO_EVENT_TRANSLATIONS.items():
            if eng_key.lower() == clean_name.lower():
                return chi_val

            # Use word-boundary regex to prevent partial token mangling or double translation
            pattern: str = r"(?i)\b" + re.escape(eng_key) + r"\b"
            if re.search(pattern, clean_name):
                if chi_val in clean_name:
                    continue
                clean_name = re.sub(pattern, chi_val, clean_name, count=1).strip()
                # Clean up redundant 'Report' / 'Index' trailing word if main indicator is already translated
                clean_name = re.sub(r"(?i)\s*\bReport\b", "", clean_name).strip()
                return clean_name

        # 2. Dynamic Fed Speaker Parsing
        fed_match = _FED_SPEAKER_PATTERN.search(clean_name)
        if fed_match:
            raw_speaker_last_name: str = fed_match.group(1).capitalize()
            chi_speaker_name: str = FED_OFFICIALS_MAP.get(
                raw_speaker_last_name, raw_speaker_last_name
            )
            is_chair: bool = "chair" in fed_match.group(0).lower()
            is_governor: bool = "governor" in fed_match.group(0).lower()
            is_testimony: bool = "testimony" in fed_match.group(0).lower()

            action: str = "國會聽證" if is_testimony else "發言"
            if is_chair:
                title_str = "聯準會主席"
            elif is_governor:
                title_str = "聯準會理事"
            else:
                title_str = "聯準會"

            replacement: str = f"{title_str}{chi_speaker_name}{action}"
            return clean_name.replace(fed_match.group(0), replacement)

        return clean_name


def translate_macro_event(event_name: str) -> str:
    """Convenience function to translate a macro event."""
    return MacroCalendarTranslator.translate(event_name)
