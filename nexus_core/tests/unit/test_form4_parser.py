"""單元測試：SEC Form 4 內部人申報 XML 解析器 (form4_parser.py)。"""

from __future__ import annotations

import pathlib

import pytest

from market_analysis.fundamental_pipeline.form4_parser import (
    Form4ParseError,
    _is_c_suite_role,
    footnote_asserts_10b5_1,
    parse_form4_xml,
)

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "sec"

# 測試用標準 Form 4 XML 片段
_SAMPLE_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>MUSK ELON</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
            <isDirector>1</isDirector>
            <isTenPercentOwner>1</isTenPercentOwner>
            <officerTitle>Chief Executive Officer</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>0</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding>
                <transactionCode>P</transactionCode>
            </transactionCoding>
            <transactionDate>
                <value>2026-10-01</value>
            </transactionDate>
            <transactionAmounts>
                <transactionShares>
                    <value>50000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>210.50</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>A</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction>
                    <value>411000000</value>
                </sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionDate>
                <value>2026-10-02</value>
            </transactionDate>
            <transactionAmounts>
                <transactionShares>
                    <value>10000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>215.00</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>D</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction>
                    <value>410990000</value>
                </sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""

_SAMPLE_10B5_FOOTNOTE_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
            <officerTitle>Chief Executive Officer</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionDate>
                <value>2026-10-03</value>
            </transactionDate>
            <transactionAmounts>
                <transactionShares>
                    <value>25000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>225.00</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>D</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction>
                    <value>3200000</value>
                </sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <footnoteId id="F1"/>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <footnotes>
        <footnote id="F1">The sales reported in this Form 4 were effected pursuant to a Rule 10b5-1 trading plan adopted on May 15, 2026.</footnote>
    </footnotes>
</ownershipDocument>
"""


def test_parse_form4_xml_standard() -> None:
    """測試解析標準 Form 4 XML 之非衍生品買入與賣出明細。"""
    records = parse_form4_xml(
        _SAMPLE_FORM4_XML, accession="0001318605-26-000010", symbol="TSLA"
    )
    assert len(records) == 2

    # 第一筆: 自費增持 P 代碼
    tx1 = records[0]
    assert tx1.symbol == "TSLA"
    assert tx1.owner_name == "MUSK ELON"
    assert tx1.is_c_suite is True
    assert "Chief Executive Officer" in tx1.owner_role
    assert tx1.tx_code == "P"
    assert tx1.acquired_disposed == "A"
    assert tx1.shares == 50000.0
    assert tx1.price == 210.50
    assert tx1.is_10b5_1 is False

    # 第二筆: 公開市場賣出 S 代碼
    tx2 = records[1]
    assert tx2.tx_code == "S"
    assert tx2.acquired_disposed == "D"
    assert tx2.shares == 10000.0
    assert tx2.price == 215.00


def test_parse_form4_xml_10b5_detection_via_footnote() -> None:
    """測試透過腳註辨識 10b5-1 排程計畫。"""
    records = parse_form4_xml(
        _SAMPLE_10B5_FOOTNOTE_XML, accession="0000320193-26-000050", symbol="AAPL"
    )
    assert len(records) == 1
    tx = records[0]
    assert tx.owner_name == "COOK TIMOTHY D"
    assert tx.is_c_suite is True
    assert tx.is_10b5_1 is True


def test_parse_form4_malformed_xml() -> None:
    """測試畸形或無效 XML 內容防禦，回傳空清單而不崩潰。"""
    records = parse_form4_xml("INVALID NOT XML CONTENT", accession="000", symbol="TEST")
    assert records == []


def test_parse_form4_xml_with_namespace() -> None:
    """測試包含官方 xmlns 命名空間的真實 SEC Form 4 XML 解析。"""
    xml_with_ns = """<?xml version="1.0"?>
    <ownershipDocument xmlns="http://www.sec.gov/edgar/ownership">
        <reportingOwner>
            <reportingOwnerId>
                <rptOwnerName>HUANG JENSEN</rptOwnerName>
            </reportingOwnerId>
            <reportingOwnerRelationship>
                <isOfficer>1</isOfficer>
                <officerTitle>President and CEO</officerTitle>
            </reportingOwnerRelationship>
        </reportingOwner>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionCoding>
                    <transactionCode>P</transactionCode>
                </transactionCoding>
                <transactionDate>
                    <value>2026-10-04</value>
                </transactionDate>
                <transactionAmounts>
                    <transactionShares><value>20000</value></transactionShares>
                    <transactionPricePerShare><value>125.00</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                </transactionAmounts>
                <postTransactionAmounts>
                    <sharesOwnedFollowingTransaction><value>5000000</value></sharesOwnedFollowingTransaction>
                </postTransactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
    </ownershipDocument>
    """
    records = parse_form4_xml(
        xml_with_ns, accession="0001045810-26-000088", symbol="NVDA"
    )
    assert len(records) == 1
    assert records[0].symbol == "NVDA"
    assert records[0].owner_name == "HUANG JENSEN"
    assert records[0].is_c_suite is True
    assert records[0].shares == 20000.0


def test_parse_form4_xml_uppercase_10b5_footnote() -> None:
    """測試大寫與常見法律用語變體之 10b5-1 腳註解析。"""
    xml_upper_fn = """<?xml version="1.0"?>
    <ownershipDocument>
        <reportingOwner>
            <reportingOwnerId><rptOwnerName>EXECUTIVE A</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
        </reportingOwner>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
                <transactionDate><value>2026-10-04</value></transactionDate>
                <transactionAmounts>
                    <transactionShares><value>5000</value></transactionShares>
                    <transactionPricePerShare><value>100.00</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
                    <footnoteId id="F1"/>
                </transactionAmounts>
                <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
        <footnotes>
            <footnote id="F1">THE SALES REPORTED IN THIS FORM 4 WERE EFFECTED PURSUANT TO A RULE 10B5-1 TRADING PLAN ADOPTED ON MAY 1, 2026.</footnote>
        </footnotes>
    </ownershipDocument>
    """
    records = parse_form4_xml(xml_upper_fn, accession="0001-26-01", symbol="TEST")
    assert len(records) == 1
    assert records[0].is_10b5_1 is True


def test_parse_form4_xml_multiple_reporting_owners() -> None:
    """測試多位共同申報人（信託實體 + 高管個人）結構下之 C-Suite 識別與角色聚合。"""
    xml_multi_owner = """<?xml version="1.0"?>
    <ownershipDocument>
        <reportingOwner>
            <reportingOwnerId><rptOwnerName>Huang Family Trust</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
        </reportingOwner>
        <reportingOwner>
            <reportingOwnerId><rptOwnerName>HUANG JENSEN</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>President and CEO</officerTitle></reportingOwnerRelationship>
        </reportingOwner>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
                <transactionDate><value>2026-10-04</value></transactionDate>
                <transactionAmounts>
                    <transactionShares><value>10000</value></transactionShares>
                    <transactionPricePerShare><value>120.00</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                </transactionAmounts>
                <postTransactionAmounts><sharesOwnedFollowingTransaction><value>100000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
    </ownershipDocument>
    """
    records = parse_form4_xml(
        xml_multi_owner, accession="0001045810-26-000099", symbol="NVDA"
    )
    assert len(records) == 1
    assert "Huang Family Trust" in records[0].owner_name
    assert "HUANG JENSEN" in records[0].owner_name
    assert records[0].is_c_suite is True


@pytest.mark.parametrize(
    "title,expected",
    [
        ("President and CEO", True),
        ("President", True),
        ("Chief Financial Officer", True),
        ("Vice President", False),
        ("Senior Vice President, Worldwide Sales", False),
        ("Executive Vice-President, Operations", False),
        ("SVP, General Manager", False),
        ("Corporate  Vice   President", False),
        ("Executive Vice President and Chief Financial Officer", True),
        ("Senior Vice President, General Counsel", True),
    ],
)
def test_c_suite_excludes_vice_president(title: str, expected: bool) -> None:
    """`PRESIDENT` 不得比對到 Vice President / SVP / EVP 等副總層級。"""
    assert _is_c_suite_role(title, True) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "This transaction was made pursuant to a Rule 10b5-1 trading plan adopted "
            "by the reporting person on May 28, 2026.",
            True,
        ),
        ("The sale was not made pursuant to a Rule 10b5-1 trading plan.", False),
        ("These shares were not sold under any 10b5-1 plan.", False),
        ("No Rule 10b5-1 plan was in effect for this sale.", False),
        ("The reporting person didn't rely on a 10b5-1 plan.", False),
        (
            "The reporting person did not sell any other shares; the sales reported "
            "were effected pursuant to a Rule 10b5-1 trading plan.",
            True,
        ),
        ("Shares withheld to satisfy tax withholding obligations.", False),
    ],
)
def test_footnote_10b5_negation(text: str, expected: bool) -> None:
    """腳註 "not ... 10b5-1" 否定敘述不得視為 10b5-1 交易。"""
    assert footnote_asserts_10b5_1(text) is expected


def test_parse_form4_negated_10b5_footnote_is_discretionary() -> None:
    """引用否定腳註的賣出交易應維持非 10b5-1（自主決策）。"""
    xml_negated = _SAMPLE_10B5_FOOTNOTE_XML.replace(
        "were effected pursuant to a Rule 10b5-1 trading plan adopted on May 15, 2026.",
        "were not effected pursuant to a Rule 10b5-1 trading plan.",
    )
    records = parse_form4_xml(xml_negated, accession="0001-26-02", symbol="AAPL")
    assert len(records) == 1
    assert records[0].is_10b5_1 is False


def test_parse_form4_raise_on_error() -> None:
    """raise_on_error=True 時，格式錯誤 XML 拋出 Form4ParseError（供同步服務停住游標）。"""
    with pytest.raises(Form4ParseError):
        parse_form4_xml(
            "<ownershipDocument><broken>",
            accession="0001-26-03",
            symbol="TEST",
            raise_on_error=True,
        )


def test_parse_form4_real_aapl_fixture() -> None:
    """真實樣本 AAPL Form 4 0001140361-26-038674：CIK、職稱與 10b5-1 標記。"""
    xml_text = (_FIXTURE_DIR / "aapl_form4_0001140361-26-038674.xml").read_text()
    records = parse_form4_xml(xml_text, "0001140361-26-038674", "AAPL")
    assert records
    assert all(r.owner_cik == "0001214156" for r in records)
    assert all(r.owner_name == "COOK TIMOTHY D" for r in records)
    assert all(r.is_amendment is False for r in records)
    # Executive Chair 不在 C-Suite 職稱清單
    assert all(r.is_c_suite is False for r in records)
    # 該申報勾選 aff10b5One=true
    assert all(r.is_10b5_1 for r in records)


def test_parse_form4a_real_fixture_marks_amendment() -> None:
    """真實樣本 GOOG Form 4/A 0001193125-26-188493：documentType 4/A 標記為修正申報。"""
    xml_text = (_FIXTURE_DIR / "goog_form4a_0001193125-26-188493.xml").read_text()
    records = parse_form4_xml(xml_text, "0001193125-26-188493", "GOOGL")
    assert records
    assert all(r.is_amendment is True for r in records)
    assert all(r.owner_cik == "0001238734" for r in records)


def test_parse_form4_joint_filing_uses_insider_cik() -> None:
    """共同申報（信託 + 高管）以具高管 / 董事身分者的 CIK 作為身分鍵。"""
    xml_joint = """<?xml version="1.0"?>
    <ownershipDocument>
        <documentType>4</documentType>
        <reportingOwner>
            <reportingOwnerId><rptOwnerCik>0002000001</rptOwnerCik><rptOwnerName>Huang Family Trust</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
        </reportingOwner>
        <reportingOwner>
            <reportingOwnerId><rptOwnerCik>1197649</rptOwnerCik><rptOwnerName>HUANG JENSEN</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>President and CEO</officerTitle></reportingOwnerRelationship>
        </reportingOwner>
        <nonDerivativeTable>
            <nonDerivativeTransaction>
                <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
                <transactionDate><value>2026-10-04</value></transactionDate>
                <transactionAmounts>
                    <transactionShares><value>10</value></transactionShares>
                    <transactionPricePerShare><value>120.00</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                </transactionAmounts>
            </nonDerivativeTransaction>
        </nonDerivativeTable>
    </ownershipDocument>
    """
    records = parse_form4_xml(xml_joint, accession="ACC-JOINT", symbol="NVDA")
    assert len(records) == 1
    # 補零為 10 位，且優先取高管本人而非文件第一位的信託
    assert records[0].owner_cik == "0001197649"
    assert records[0].owner_key == "CIK:0001197649"
