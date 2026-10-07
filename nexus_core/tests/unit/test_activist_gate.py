"""單元測試：Schedule 13D 激進投資人與及時性審查 (activist_gate.py)。"""

from __future__ import annotations

from datetime import date
from market_analysis.fundamental_pipeline.activist_gate import (
    count_business_days,
    evaluate_activist_filing,
    parse_schedule_13d_xml,
)

# 依 EDGAR 結構化 Schedule 13D (schemaVersion X0202) 實際 primary_doc.xml 結構精簡
# （參考 Elastic N.V. 0001361570-26-000022 的元素命名與命名空間）。
SCHEDULE_13D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D" xmlns:com="http://www.sec.gov/edgar/common">
  <schemaVersion>X0202</schemaVersion>
  <headerData><submissionType>SCHEDULE 13D</submissionType></headerData>
  <formData>
    <coverPageHeader>
      <securitiesClassTitle>COMMON STOCK</securitiesClassTitle>
      <dateOfEvent>09/28/2026</dateOfEvent>
      <issuerInfo><issuerCIK>0001707753</issuerCIK><issuerName>Target Co</issuerName></issuerInfo>
    </coverPageHeader>
    <reportingPersons>
      <reportingPersonInfo>
        <reportingPersonName>Activist Master Fund LP</reportingPersonName>
        <percentOfClass>6.1</percentOfClass>
      </reportingPersonInfo>
      <reportingPersonInfo>
        <reportingPersonName>Activist GP LLC</reportingPersonName>
        <percentOfClass>7.4</percentOfClass>
      </reportingPersonInfo>
    </reportingPersons>
    <items1To7>
      <item4>
        <transactionPurpose>The Reporting Persons intend to seek board representation and
believe the Issuer should explore strategic alternatives.</transactionPurpose>
      </item4>
    </items1To7>
  </formData>
</edgarSubmission>
"""


def test_count_business_days() -> None:
    """測試計算兩日期之間的營業日（排除週六與週日）。"""
    # 2026-10-02 (週五) 到 2026-10-05 (週一)：1 個營業日 (週一當日)
    assert count_business_days(date(2026, 10, 2), date(2026, 10, 5)) == 1

    # 2026-10-01 (週四) 到 2026-10-08 (週四)：5 個營業日 (週五, 週一, 週二, 週三, 週四)
    assert count_business_days(date(2026, 10, 1), date(2026, 10, 8)) == 5

    # 跨週超過 5 營業日: 2026-10-01 到 2026-10-09 (週五) -> 6 個營業日
    assert count_business_days(date(2026, 10, 1), date(2026, 10, 9)) == 6


def test_evaluate_activist_filing_timely_and_intents() -> None:
    """測試及時申報之 13D 與關鍵訴求識別。"""
    item_4_text = (
        "The Reporting Persons believe that the shares are undervalued and intend to have "
        "discussions with management and the board of directors regarding strategic alternatives "
        "and seeking board representation to enhance shareholder value."
    )

    signal = evaluate_activist_filing(
        symbol="XYZ",
        accession="0001193125-26-000001",
        investor_name="Starboard Value LP",
        ownership_pct=6.5,
        item_4_text=item_4_text,
        event_date="2026-10-01",
        filing_date="2026-10-06",  # 3 營業日 <= 5
    )

    assert signal.symbol == "XYZ"
    assert signal.ownership_pct == 6.5
    assert signal.is_delayed_filing is False
    assert "BOARD_SEAT" in signal.key_intents
    assert "STRATEGIC_REVIEW" in signal.key_intents
    assert "UNDERVALUED_STANCE" in signal.key_intents
    assert "爭取董事會席次" in signal.summary_text


def test_evaluate_activist_filing_delayed() -> None:
    """測試逾期申報（> 5 營業日）觸發 is_delayed_filing 標記。"""
    item_4_text = "The Reporting Persons purchased shares for investment purposes."

    signal = evaluate_activist_filing(
        symbol="ABC",
        accession="0001193125-26-000002",
        investor_name="Carl Icahn",
        ownership_pct=8.2,
        item_4_text=item_4_text,
        event_date="2026-09-15",
        filing_date="2026-10-01",  # 歷經十多個營業日 > 5
    )

    assert signal.is_delayed_filing is True
    assert "申報逾期" in signal.summary_text


def test_count_business_days_excludes_federal_holidays() -> None:
    """SEC 營業日排除聯邦假日（Rule 14d-1(g)(3)），與 NYSE 交易日不同。"""
    # 2026-10-12 (週一) 為哥倫布日：SEC 休息（NYSE 照常開市）
    # 2026-10-09 (週五) -> 2026-10-13 (週二)：僅 10-13 一個營業日
    assert count_business_days(date(2026, 10, 9), date(2026, 10, 13)) == 1

    # 2026-11-11 (週三) 退伍軍人節不計
    assert count_business_days(date(2026, 11, 10), date(2026, 11, 12)) == 1

    # 2026-04-03 (週五) 耶穌受難日：NYSE 休市但 SEC 上班，仍計入
    assert count_business_days(date(2026, 4, 2), date(2026, 4, 3)) == 1


def test_evaluate_activist_filing_delay_counts_holiday() -> None:
    """跨越假日時，逾期判定以 SEC 營業日計算。"""
    # 事件日 2026-10-05 (週一) → 申報日 2026-10-13 (週二)：
    # 10-06..10-09 (4 日) + 10-12 哥倫布日不計 + 10-13 (1 日) = 5 營業日 → 未逾期
    signal = evaluate_activist_filing(
        symbol="XYZ",
        accession="ACC-13D",
        investor_name="Activist LP",
        ownership_pct=5.2,
        item_4_text="seeking board representation",
        event_date="2026-10-05",
        filing_date="2026-10-13",
    )
    assert signal.is_delayed_filing is False


def test_parse_schedule_13d_xml_structured_fields() -> None:
    """結構化 13D：取第一位申報人、最大持股比例、Item 4 與 ISO 事件日。"""
    fields = parse_schedule_13d_xml(SCHEDULE_13D_XML)
    assert fields is not None
    assert fields.investor_name == "Activist Master Fund LP"
    assert fields.ownership_pct == 7.4
    assert "board representation" in fields.item_4_text
    assert fields.event_date == "2026-09-28"


def test_parse_schedule_13d_xml_rejects_legacy_html() -> None:
    """舊版 HTML 申報或缺少申報人時回傳 None。"""
    assert parse_schedule_13d_xml("<html><body>SC 13D</body></html>") is None
    assert parse_schedule_13d_xml("not xml at all") is None


def test_parse_schedule_13d_amendment_without_item4() -> None:
    """修正申報未修改 Item 4 時 item_4_text 為空字串，仍可評估。"""
    xml = SCHEDULE_13D_XML.replace(
        SCHEDULE_13D_XML[
            SCHEDULE_13D_XML.index("<item4>") : SCHEDULE_13D_XML.index("</item4>") + 8
        ],
        "",
    )
    fields = parse_schedule_13d_xml(xml)
    assert fields is not None
    assert fields.item_4_text == ""
    signal = evaluate_activist_filing(
        symbol="TGT",
        accession="ACC-1",
        investor_name=fields.investor_name,
        ownership_pct=fields.ownership_pct,
        item_4_text=fields.item_4_text,
        event_date=fields.event_date,
        filing_date="2026-10-01",
    )
    assert signal.key_intents == []
