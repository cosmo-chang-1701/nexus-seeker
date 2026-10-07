"""SEC Form 4 內部人交易申報 XML 解析器。

使用 defusedxml.ElementTree 安全解析 ownershipDocument，杜絕 XXE 與實體膨脹攻擊。
提取非衍生品交易（nonDerivativeTransaction）、C-Suite 高管標記、10b5-1 交易計畫特徵與交易代碼。
"""

from __future__ import annotations

import logging
import re
import defusedxml.ElementTree as ET

from market_analysis.fundamental_pipeline.models import InsiderTxRecord

logger = logging.getLogger(__name__)

# C-Suite 職稱關鍵字特徵
_C_SUITE_TITLE_PATTERNS = [
    r"\bCEO\b",
    r"\bCFO\b",
    r"\bCOO\b",
    r"\bCTO\b",
    r"\bCIO\b",
    # 排除 Vice President / Senior Vice President / Executive Vice-President 等副總層級
    r"(?<!VICE )(?<!VICE-)\bPRESIDENT\b",
    r"CHIEF EXECUTIVE OFFICER",
    r"CHIEF FINANCIAL OFFICER",
    r"CHIEF OPERATING OFFICER",
    r"CHIEF TECHNOLOGY OFFICER",
    r"CHIEF INFORMATION OFFICER",
    r"GENERAL COUNSEL",
]
_C_SUITE_REGEX = re.compile("|".join(_C_SUITE_TITLE_PATTERNS), re.IGNORECASE)

# 10b5-1 腳註關鍵字（不分大小寫與常見變體）
_10B5_KEYWORD_RE = re.compile(r"10b-?5[-–]?1|10b5", re.IGNORECASE)
# 同一子句（不跨越句點、分號、逗號）內位於關鍵字之前的否定語，例如
# "not made pursuant to a Rule 10b5-1 plan"、"no 10b5-1 trading plan"。
_10B5_NEGATION_RE = re.compile(
    r"\b(?:not|no|neither|nor|never|without)\b[^.;,]{0,80}?$|n't\b[^.;,]{0,80}?$",
    re.IGNORECASE,
)


class Form4ParseError(ValueError):
    """Form 4 XML 無法解析（格式錯誤或截斷）。"""


def footnote_asserts_10b5_1(text: str) -> bool:
    """腳註是否「肯定地」指出交易依 10b5-1 計畫執行。

    只要有任一處提及 10b5-1 且同一子句中位於其前方沒有否定語，即視為肯定；
    "not ... pursuant to a Rule 10b5-1 plan" 這類否定敘述不計入。
    """
    for match in _10B5_KEYWORD_RE.finditer(text):
        prefix = text[: match.start()]
        if not _10B5_NEGATION_RE.search(prefix):
            return True
    return False


def _strip_namespaces(el: ET.Element) -> ET.Element:
    """遞迴移除 ElementTree 節點標籤中所有 XML Namespace 前綴，提升查詢強健度。"""
    for node in el.iter():
        if "}" in node.tag:
            node.tag = node.tag.split("}", 1)[1]
    return el


def _is_c_suite_role(officer_title: str | None, is_officer: bool) -> bool:
    """判斷該高管是否屬於 C-Suite 核心決策層。"""
    if not officer_title:
        return False
    normalized = re.sub(r"\s+", " ", officer_title.strip())
    return bool(_C_SUITE_REGEX.search(normalized))


def _extract_text(element: ET.Element | None, xpath: str, default: str = "") -> str:
    """安全提取指定 XPath 節點之文字內容。"""
    if element is None:
        return default
    node = element.find(xpath)
    if node is not None and node.text:
        return str(node.text).strip()
    return default


def _extract_float(
    element: ET.Element | None, xpath: str, default: float = 0.0
) -> float:
    """安全提取浮點數值。"""
    val_str = _extract_text(element, xpath, "")
    if not val_str:
        return default
    try:
        return float(val_str.replace(",", ""))
    except ValueError:
        return default


def parse_form4_xml(
    xml_content: str | bytes | ET.Element,
    accession: str,
    symbol: str,
    is_backfill: bool = False,
    raise_on_error: bool = False,
) -> list[InsiderTxRecord]:
    """解析 Form 4 XML 並產出結構化內部人交易明細記錄。

    支援傳入原始 XML 字串、位元組或已解析之 ElementTree 根結點。
    `raise_on_error=True` 時，XML 無法解析會拋出 `Form4ParseError`（供同步服務
    把該筆視為失敗、游標停在失敗之前）；預設維持回傳空清單。
    """
    if isinstance(xml_content, (str, bytes)):
        try:
            root = ET.fromstring(xml_content)
        except Exception as e:
            logger.error(f"解析 Form 4 XML 失敗 ({accession}, {symbol}): {e}")
            if raise_on_error:
                raise Form4ParseError(str(e)) from e
            return []
    else:
        root = xml_content

    # 0. 移除命名空間以相容 SEC 官方 XML 架構
    _strip_namespaces(root)

    # 1. 提取申報人 (Reporting Owner) 與職務（支援多位共同申報人，如家族信託與高管本人）
    # 共同申報時以「主申報人」CIK 作為身分鍵：優先取具董事 / 高管身分者（個人內部人），
    # 否則取文件中第一位；聚合時用 CIK 判斷是否為同一內部人，避免名稱串接字串失真。
    reporting_owners = root.findall(".//reportingOwner")
    owner_names: list[str] = []
    all_roles: list[str] = []
    is_c_suite = False
    first_cik: str | None = None
    insider_cik: str | None = None

    if not reporting_owners:
        owner_name = "REPORTING_OWNER"
        owner_role = "OTHER"
    else:
        for ro in reporting_owners:
            r_name = _extract_text(ro, ".//reportingOwnerId/rptOwnerName", "")
            if r_name:
                owner_names.append(r_name)
            r_cik_raw = _extract_text(ro, ".//reportingOwnerId/rptOwnerCik", "")
            r_cik = r_cik_raw.zfill(10) if r_cik_raw.isdigit() else None
            is_off = _extract_text(
                ro, ".//reportingOwnerRelationship/isOfficer", "0"
            ).lower() in ("1", "true")
            is_dir = _extract_text(
                ro, ".//reportingOwnerRelationship/isDirector", "0"
            ).lower() in ("1", "true")
            is_ten = _extract_text(
                ro, ".//reportingOwnerRelationship/isTenPercentOwner", "0"
            ).lower() in ("1", "true")
            title = _extract_text(ro, ".//reportingOwnerRelationship/officerTitle", "")
            if r_cik is not None:
                if first_cik is None:
                    first_cik = r_cik
                if insider_cik is None and (is_off or is_dir):
                    insider_cik = r_cik

            if _is_c_suite_role(title, is_off):
                is_c_suite = True

            ro_roles: list[str] = []
            if title:
                ro_roles.append(title)
            elif is_off:
                ro_roles.append("OFFICER")
            if is_dir:
                ro_roles.append("DIRECTOR")
            if is_ten:
                ro_roles.append("10%_OWNER")
            if ro_roles:
                all_roles.append(" / ".join(ro_roles))

        owner_name = ", ".join(owner_names) if owner_names else "REPORTING_OWNER"
        owner_role = " | ".join(all_roles) if all_roles else "OTHER"
    owner_cik = insider_cik or first_cik

    # 修正申報（Form 4/A）：documentType 為 "4/A"
    doc_type = _extract_text(root, "documentType", "").upper()
    is_amendment = doc_type.endswith("/A")

    # 2. 全域 10b5-1 標記檢查 (<aff10b5One>)
    has_global_10b5 = False
    for aff_node in root.findall(".//aff10b5One"):
        if aff_node.text and aff_node.text.strip().lower() in ("1", "true"):
            has_global_10b5 = True
            break

    # 收集腳註中「肯定地」提及 10b5-1 的 footnoteId（排除 "not ... 10b5-1" 否定敘述）
    footnote_10b5_ids: set[str] = set()
    for fn in root.findall(".//footnotes/footnote"):
        fn_id = fn.attrib.get("id", "").strip()
        fn_text = "".join(fn.itertext())
        if fn_text and footnote_asserts_10b5_1(fn_text):
            if fn_id:
                footnote_10b5_ids.add(fn_id)

    # 3. 遍歷非衍生品交易 (nonDerivativeTransaction)
    tx_nodes = root.findall(".//nonDerivativeTransaction")
    results: list[InsiderTxRecord] = []

    for idx, tx_node in enumerate(tx_nodes, start=1):
        tx_code = _extract_text(
            tx_node, ".//transactionCoding/transactionCode", "UNKNOWN"
        ).upper()
        tx_date = _extract_text(tx_node, ".//transactionDate/value", "")
        shares = _extract_float(
            tx_node, ".//transactionAmounts/transactionShares/value", 0.0
        )
        price = _extract_float(
            tx_node, ".//transactionAmounts/transactionPricePerShare/value", 0.0
        )
        ad_code = _extract_text(
            tx_node, ".//transactionAmounts/transactionAcquiredDisposedCode/value", "D"
        ).upper()
        shares_after = _extract_float(
            tx_node,
            ".//postTransactionAmounts/sharesOwnedFollowingTransaction/value",
            0.0,
        )

        # 判斷此單筆交易是否涉及 10b5-1
        is_tx_10b5 = has_global_10b5
        if not is_tx_10b5 and footnote_10b5_ids:
            for fn_ref in tx_node.findall(".//footnoteId"):
                fn_ref_id = fn_ref.attrib.get("id", "").strip()
                if fn_ref_id and fn_ref_id in footnote_10b5_ids:
                    is_tx_10b5 = True
                    break

        record = InsiderTxRecord(
            accession=accession,
            line_no=idx,
            symbol=symbol.upper(),
            owner_name=owner_name,
            owner_cik=owner_cik,
            owner_role=owner_role,
            is_c_suite=is_c_suite,
            tx_date=tx_date,
            tx_code=tx_code,
            shares=shares,
            price=price,
            acquired_disposed=ad_code,
            shares_after=shares_after,
            is_10b5_1=is_tx_10b5,
            is_backfill=is_backfill,
            is_amendment=is_amendment,
        )
        results.append(record)

    return results
