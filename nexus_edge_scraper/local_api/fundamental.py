"""local_api：SEC EDGAR 財報 Metadata/清單/文本抓取與結構化段落擷取。"""

from typing import Any
import logging
import re

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter

from section_extractor import extract_sections

logger = logging.getLogger(__name__)

router = APIRouter()

SEC_USER_AGENT = "NexusSeekerBot (nexusseeker@example.com)"
cik_cache: dict[str, str] = {}

# Item-anchor patterns per SEC filing type. Each entry locates the start of
# the most relevant narrative section in `text_content`; the first ~10k
# chars from that anchor become "final_text" (the raw context sent to the
# LLM). 10-K uses Item 7 (MD&A); 10-Q uses Item 2 (MD&A) since 10-Qs don't
# use 10-K's Item numbering; 8-K has no MD&A at all, so it anchors at the
# first dotted Item header (e.g. "Item 5.02") since which item(s) fire
# varies filing to filing. Unknown/missing form types fall back to 10-K.
_FORM_ANCHOR_PATTERNS: dict[str, "re.Pattern[str]"] = {
    "10-K": re.compile(
        r"(?i)(item\s*7\.\s*management['’]s\s*discussion|"
        r"item\s*1a\.\s*risk\s*factors)"
    ),
    "10-Q": re.compile(
        r"(?i)(item\s*2\.\s*management['’]s\s*discussion|"
        r"item\s*1a\.\s*risk\s*factors)"
    ),
    "8-K": re.compile(r"(?i)item\s*\d+\.\d{2}\b"),
}


async def _get_sec_cik(client: httpx.AsyncClient, symbol: str) -> str | None:
    if not cik_cache:
        try:
            resp = await client.get("https://www.sec.gov/files/company_tickers.json")
            resp.raise_for_status()
            data = resp.json()
            for key, value in data.items():
                cik_cache[value["ticker"]] = str(value["cik_str"]).zfill(10)
        except Exception as e:
            logger.error(f"Failed to fetch SEC CIK dictionary: {e}")
            return None
    return cik_cache.get(symbol.upper())


@router.get("/api/v1/scrape/fundamental/{symbol}/metadata")
async def scrape_fundamental_metadata(symbol: str) -> dict[str, Any]:
    """
    獲取標的最新財報的 Metadata (用於輕量級快取驗證)
    """
    # 延遲從套件頂層 import：讓 `patch("local_api._get_sec_cik")` 能正確攔截
    # 本函式內部呼叫（拆檔後 fundamental.py 自己的模組層級綁定會繞過套件層級
    # 的 monkeypatch，理由同 market_data_service 拆分時 quote.py 內部處理）。
    from local_api import _get_sec_cik as get_sec_cik

    symbol_clean = symbol.upper().replace("$", "")
    try:
        async with httpx.AsyncClient(headers={"User-Agent": SEC_USER_AGENT}) as client:
            cik = await get_sec_cik(client, symbol_clean)
            if not cik:
                return {"status": "error", "data": f"無法找到 {symbol_clean} 的 CIK"}

            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accessions = recent.get("accessionNumber", [])

            for i, form in enumerate(forms):
                if form in ["10-K", "10-Q", "8-K"]:
                    return {
                        "status": "success",
                        "data": {
                            "symbol": symbol_clean,
                            "form": form,
                            "accession_number": accessions[i],
                        },
                    }
            return {
                "status": "error",
                "data": f"近期無 10-K, 10-Q 或 8-K ({symbol_clean})",
            }
    except Exception as e:
        logger.error(f"SEC EDGAR metadata scrape failed for {symbol_clean}: {e}")
        return {"status": "error", "data": str(e)}


@router.get("/api/v1/scrape/fundamental/{symbol}/list")
async def scrape_fundamental_list(symbol: str) -> dict[str, Any]:
    """
    獲取標的近期財報清單 (10-K, 10-Q, 8-K)
    """
    from local_api import _get_sec_cik as get_sec_cik

    symbol_clean = symbol.upper().replace("$", "")
    try:
        async with httpx.AsyncClient(headers={"User-Agent": SEC_USER_AGENT}) as client:
            cik = await get_sec_cik(client, symbol_clean)
            if not cik:
                return {
                    "status": "error",
                    "data": f"無法在 SEC 資料庫中找到 {symbol_clean} 的 CIK",
                }

            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accessions = recent.get("accessionNumber", [])
            report_dates = recent.get("reportDate", [])

            reports = []
            for i, form in enumerate(forms):
                if form in ["10-K", "10-Q", "8-K"]:
                    reports.append(
                        {
                            "form": form,
                            "accession_number": accessions[i],
                            "report_date": report_dates[i]
                            if i < len(report_dates)
                            else "Unknown",
                        }
                    )
                    if len(reports) >= 10:
                        break
            return {"status": "success", "data": reports}
    except Exception as e:
        logger.error(f"SEC EDGAR list scrape failed for {symbol_clean}: {e}")
        return {"status": "error", "data": str(e)}


@router.get("/api/v1/scrape/fundamental/{symbol}")
async def scrape_fundamental_text(
    symbol: str, accession_number: str | None = None
) -> dict[str, Any]:
    """
    獲取標的最新財報與基本面文本 (SEC EDGAR 8-K / 10-Q)
    """
    from local_api import _get_sec_cik as get_sec_cik

    symbol_clean = symbol.upper().replace("$", "")

    try:
        async with httpx.AsyncClient(headers={"User-Agent": SEC_USER_AGENT}) as client:
            cik = await get_sec_cik(client, symbol_clean)
            if not cik:
                return {
                    "status": "error",
                    "data": f"無法在 SEC 資料庫中找到 {symbol_clean} 的 CIK",
                }

            # 1. 取得近期申報列表
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accessions = recent.get("accessionNumber", [])
            primary_docs = recent.get("primaryDocument", [])

            # 2. 尋找指定的 accession_number，或最新的 10-K, 10-Q 或 8-K
            target_idx = -1
            if accession_number:
                for i, acc in enumerate(accessions):
                    if acc == accession_number:
                        target_idx = i
                        break
            else:
                for i, form in enumerate(forms):
                    if form in ["10-K", "10-Q", "8-K"]:
                        target_idx = i
                        break

            if target_idx == -1:
                return {
                    "status": "error",
                    "data": f"找不到指定的財報記錄或近期無 10-K, 10-Q, 8-K ({symbol_clean})",
                }

            accession_num = accessions[target_idx]
            accession_no_dash = accession_num.replace("-", "")
            primary_doc = primary_docs[target_idx]

            # 3. 獲取文件原始碼
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no_dash}/{primary_doc}"
            doc_resp = await client.get(doc_url, timeout=15.0)
            doc_resp.raise_for_status()

            # 4. 抽取純文字並過濾無效 XBRL 會計標籤
            soup = BeautifulSoup(doc_resp.text, "lxml")
            text_content = soup.get_text(separator="\\n", strip=True)

            # 移除 SEC 財報中無意義的會計標籤 (如 us-gaap:, tsla:, ix: 等)
            text_content = re.sub(
                r"([a-zA-Z0-9\-]+:[A-Za-z0-9]+[\n\s]+)+", "\\n", text_content
            )

            # 精準擷取 MD&A 或 Risk Factors 段落 (依 form_type 分流錨點正規表達式)
            form_type = forms[target_idx]
            anchor_pattern = _FORM_ANCHOR_PATTERNS.get(
                form_type, _FORM_ANCHOR_PATTERNS["10-K"]
            )
            match = anchor_pattern.search(text_content)
            if match:
                start_idx = match.start()
                final_text = text_content[start_idx : start_idx + 10000]
            else:
                final_text = text_content[:10000]

            # 5. 結構化段落擷取 (Forward Guidance / Margin / Market Share / Financials / Ops / Key Events)
            extracted = extract_sections(text_content, form_type=form_type)

            return {
                "status": "success",
                "data": {
                    "symbol": symbol_clean,
                    "text": final_text,
                    "sections": extracted.to_dict(),
                    "source": "sec_edgar",
                    "source_url": doc_url,
                    "accession_number": accession_num,
                    "form_type": form_type,
                },
            }
    except Exception as e:
        logger.error(f"SEC EDGAR scrape failed for {symbol_clean}: {e}")
        return {"status": "error", "data": str(e)}
