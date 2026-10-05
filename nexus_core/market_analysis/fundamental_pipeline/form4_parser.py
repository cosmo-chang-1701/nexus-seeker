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
    r"\bPRESIDENT\b",
    r"CHIEF EXECUTIVE OFFICER",
    r"CHIEF FINANCIAL OFFICER",
    r"CHIEF OPERATING OFFICER",
    r"CHIEF TECHNOLOGY OFFICER",
    r"CHIEF INFORMATION OFFICER",
    r"GENERAL COUNSEL",
]
_C_SUITE_REGEX = re.compile("|".join(_C_SUITE_TITLE_PATTERNS), re.IGNORECASE)


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
    return bool(_C_SUITE_REGEX.search(officer_title))


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
) -> list[InsiderTxRecord]:
    """解析 Form 4 XML 並產出結構化內部人交易明細記錄。

    支援傳入原始 XML 字串、位元組或已解析之 ElementTree 根結點。
    """
    if isinstance(xml_content, (str, bytes)):
        try:
            root = ET.fromstring(xml_content)
        except Exception as e:
            logger.error(f"解析 Form 4 XML 失敗 ({accession}, {symbol}): {e}")
            return []
    else:
        root = xml_content

    # 0. 移除命名空間以相容 SEC 官方 XML 架構
    _strip_namespaces(root)

    # 1. 提取申報人 (Reporting Owner) 與職務（支援多位共同申報人，如家族信託與高管本人）
    reporting_owners = root.findall(".//reportingOwner")
    owner_names: list[str] = []
    all_roles: list[str] = []
    is_c_suite = False

    if not reporting_owners:
        owner_name = "REPORTING_OWNER"
        owner_role = "OTHER"
    else:
        for ro in reporting_owners:
            r_name = _extract_text(ro, ".//reportingOwnerId/rptOwnerName", "")
            if r_name:
                owner_names.append(r_name)
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

    # 2. 全域 10b5-1 標記檢查 (<aff10b5One>)
    has_global_10b5 = False
    for aff_node in root.findall(".//aff10b5One"):
        if aff_node.text and aff_node.text.strip().lower() in ("1", "true"):
            has_global_10b5 = True
            break

    # 收集腳註中提及 10b5-1 的 footnoteId (不分大小寫與常見變體)
    footnote_10b5_ids: set[str] = set()
    for fn in root.findall(".//footnotes/footnote"):
        fn_id = fn.attrib.get("id", "").strip()
        if fn.text:
            text_lower = fn.text.lower()
            if any(k in text_lower for k in ("10b5-1", "10b5", "10b-5-1", "10b5–1")):
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
        )
        results.append(record)

    return results
