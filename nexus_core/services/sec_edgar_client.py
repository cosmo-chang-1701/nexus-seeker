"""SEC EDGAR 官方 API 專屬直連非同步客戶端。

核心安全防護與架構原則：
1. 嚴格限速：所有請求經 `rate_gate` 的 sec 閘門（`SEC_LIMITER_MAX_RATE` 預設 8 req/s，低於 SEC 10 req/s
   上限；全 process 共用一個閘門，不論建構幾個客戶端實例），429／含 Request Rate Threshold 的 403 觸發冷卻。
2. 串流下載防爆：強制 1.5MB (1_500_000 bytes) 硬截斷，保障 1GB–2GB VPS 記憶體。
3. 杜絕 XXE：全面透過 defusedxml.ElementTree 解析 XML 檔案。
4. 認證檢查：強制驗證 SEC_USER_AGENT 格式（包含 @ 聯絡信箱），缺少時拋出 SecConfigError。
5. CIK 查詢快取：支援 10 位數自動補零 (zfill(10)) 與官方 company_tickers.json 快取。
   映射表以 TTL（24 小時）快取並經 SingleFlightManager 合併併發下載；映射表新鮮期間
   查無代碼即直接回傳 None（隱含負向快取），下載失敗後 10 分鐘內不重試。
6. 權威受理時間：submissions JSON 的 acceptanceDateTime 不可信（見
   sec_item_router.parse_sec_acceptance_datetime），改讀 `{accession}.hdr.sgml` 表頭。
"""

from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Any
import defusedxml.ElementTree as ET
import httpx

import config
from market_analysis.fundamental_pipeline.press_release import (
    INDEX_HEADERS_BYTE_CAP,
    FilingDocumentEntry,
    parse_index_headers_documents,
)
from market_analysis.fundamental_pipeline.sec_item_router import (
    parse_sec_header_acceptance,
)
from services.http_gate import gated_request, gated_stream
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)

# 常見美股權值股靜態快取種子
_COMMON_CIK_SEEDS: dict[str, str] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "META": "0001326801",
    "TSLA": "0001318605",
    "SMCI": "0001375365",
    "AMD": "0000002488",
    "INTC": "0000050863",
    "QCOM": "0000804328",
    "AVGO": "0001730168",
    "RKLB": "0001819994",
    "PL": "0001844981",
    "LMT": "0000936468",
    "BA": "0000012927",
    "SPY": "0000884394",
    "QQQ": "0001067839",
    "BRK.B": "0001067983",
    "BRK-B": "0001067983",
    "BF.B": "0000014693",
    "BF-B": "0000014693",
}


_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKER_MAP_TTL_SECONDS = 24 * 3600.0  # company_tickers.json 映射表快取有效期
_TICKER_MAP_FAILURE_BACKOFF_SECONDS = 600.0  # 下載失敗後的重試冷卻
_SGML_HEADER_BYTE_CAP = 65_536  # .hdr.sgml 僅約 1KB，受理時間位於首數行


class SecConfigError(RuntimeError):
    """SEC 缺少正式認證聯絡資訊時的防禦性異常。"""


class SecEdgarClient:
    """SEC EDGAR 官方非同步 HTTP 客戶端。"""

    def __init__(
        self,
        user_agent: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        ua = user_agent if user_agent is not None else config.SEC_USER_AGENT
        if not ua or "@" not in ua:
            raise SecConfigError(
                "SEC_USER_AGENT 必須配置且包含合規聯絡信箱（例如 'NexusSeeker sample@example.com'）。"
            )

        self._headers: dict[str, str] = {
            "User-Agent": ua.strip(),
            "Accept-Encoding": "gzip, deflate",
        }
        self._timeout = timeout_seconds
        self._cik_cache: dict[str, str] = dict(_COMMON_CIK_SEEDS)
        # 映射表最近一次成功載入 / 失敗的 monotonic 時間戳
        self._ticker_map_loaded_at: float | None = None
        self._ticker_map_failed_at: float | None = None

    @staticmethod
    def _symbol_variants(symbol: str) -> list[str]:
        """雙重股權標的（如 BRK.B <-> BRK-B）雙向標準化候選清單。"""
        sym_clean = symbol.strip().upper()
        variants = [sym_clean]
        if "." in sym_clean:
            variants.append(sym_clean.replace(".", "-"))
        if "-" in sym_clean:
            variants.append(sym_clean.replace("-", "."))
        return variants

    def _lookup_cached(self, variants: list[str]) -> str | None:
        for v in variants:
            if v in self._cik_cache:
                return self._cik_cache[v]
        return None

    async def _download_ticker_map(self) -> dict[str, str]:
        """下載 company_tickers.json 並轉成 {ticker: cik10}（含 . / - 雙向別名）。"""
        async with httpx.AsyncClient(
            headers=self._headers, timeout=self._timeout
        ) as client:
            resp = await gated_request(
                "sec", client, "GET", _COMPANY_TICKERS_URL, endpoint="company_tickers"
            )
            resp.raise_for_status()
            data = resp.json()

        mapping: dict[str, str] = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).strip().upper()
            raw_cik = entry.get("cik_str")
            if ticker and raw_cik is not None:
                cik10 = str(raw_cik).zfill(10)
                mapping[ticker] = cik10
                if "-" in ticker:
                    mapping[ticker.replace("-", ".")] = cik10
                elif "." in ticker:
                    mapping[ticker.replace(".", "-")] = cik10
        return mapping

    def _ticker_map_is_fresh(self, now: float) -> bool:
        return (
            self._ticker_map_loaded_at is not None
            and now - self._ticker_map_loaded_at < _TICKER_MAP_TTL_SECONDS
        )

    def _in_failure_backoff(self, now: float) -> bool:
        return (
            self._ticker_map_failed_at is not None
            and now - self._ticker_map_failed_at < _TICKER_MAP_FAILURE_BACKOFF_SECONDS
        )

    async def get_cik(self, symbol: str) -> str | None:
        """根據股票代碼查詢 10 位數補零之 CIK，支援雙重股權代碼標準化映射。

        快取策略：
        - 命中記憶體快取（種子或映射表）直接回傳。
        - 映射表在 TTL 內仍新鮮卻查無代碼 → 直接回傳 None，不重新下載（負向快取）。
        - 下載失敗後冷卻期內不重試，直接回傳 None。
        - 併發 miss 經 SingleFlightManager 合併為單一下載。
        """
        variants = self._symbol_variants(symbol)
        cached = self._lookup_cached(variants)
        if cached is not None:
            return cached

        now = time.monotonic()
        if self._ticker_map_is_fresh(now) or self._in_failure_backoff(now):
            return None

        try:
            mapping: dict[str, str] = await SingleFlightManager.run(
                "sec_company_tickers_json", self._download_ticker_map
            )
        except Exception as e:
            self._ticker_map_failed_at = time.monotonic()
            logger.warning(f"查詢 SEC 官方 Ticker-CIK 映射表失敗 ({variants[0]}): {e}")
            return None

        self._cik_cache.update(mapping)
        self._ticker_map_loaded_at = time.monotonic()
        self._ticker_map_failed_at = None
        return self._lookup_cached(variants)

    async def fetch_acceptance_datetime(self, cik: str, accession: str) -> datetime:
        """讀取申報 SGML 表頭 `{accession}.hdr.sgml` 的權威受理時間（帶時區之美東時間）。

        失敗（HTTP 錯誤、標籤缺漏）一律拋出例外，由呼叫端視為該筆處理失敗。
        """
        cik_int = str(int(cik))
        acc_no_dash = accession.replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/"
            f"{accession}.hdr.sgml"
        )
        header_text = await self.fetch_document_text(
            url, byte_cap=_SGML_HEADER_BYTE_CAP
        )
        return parse_sec_header_acceptance(header_text)

    @staticmethod
    def filing_directory_url(cik: str, accession: str) -> str:
        """組出申報目錄 URL（`/Archives/edgar/data/{cik}/{accession 去橫線}`，不含結尾斜線）。"""
        cik_int = str(int(cik))
        acc_no_dash = accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}"

    async def fetch_filing_documents(
        self, filing_dir_url: str, accession: str
    ) -> list[FilingDocumentEntry]:
        """讀取 `{accession}-index-headers.html`，列出申報內各文件之 TYPE 與檔名（如 EX-99.1）。"""
        url = f"{filing_dir_url.rstrip('/')}/{accession}-index-headers.html"
        text = await self.fetch_document_text(url, byte_cap=INDEX_HEADERS_BYTE_CAP)
        return parse_index_headers_documents(text)

    async def fetch_company_submissions(self, cik: str) -> dict[str, Any]:
        """拉取指定 CIK 之最近申報事件清單 (Submissions API)。"""
        cik10 = str(cik).strip().zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik10}.json"

        async with httpx.AsyncClient(
            headers=self._headers, timeout=self._timeout
        ) as client:
            resp = await gated_request(
                "sec", client, "GET", url, endpoint="submissions"
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def fetch_company_concept(
        self, cik: str, tag: str, taxonomy: str = "us-gaap"
    ) -> dict[str, Any] | None:
        """拉取單一 XBRL 概念的歷史事實 (companyconcept API)。

        刻意不用 companyfacts：單一公司 companyfacts 解壓後約 5MB、json 解析峰值約 26MB，
        companyconcept 每個標籤僅數十 KB，較符合 1–2GB VPS 記憶體限制。
        HTTP 404（公司從未申報此標籤）回傳 None；其他錯誤拋出例外。
        """
        cik10 = str(cik).strip().zfill(10)
        url = (
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/"
            f"{taxonomy}/{tag}.json"
        )
        async with httpx.AsyncClient(
            headers=self._headers, timeout=self._timeout
        ) as client:
            resp = await gated_request(
                "sec", client, "GET", url, endpoint="companyconcept"
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def fetch_document_text(self, url: str, byte_cap: int = 1_500_000) -> str:
        """串流拉取原始文本，到達 byte_cap 上限時執行防爆截斷。"""
        async with httpx.AsyncClient(
            headers=self._headers, timeout=self._timeout
        ) as client:
            async with gated_stream(
                "sec", client, "GET", url, endpoint="document"
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= byte_cap:
                        logger.warning(
                            f"SEC 文件超過記憶體防護門檻 {byte_cap} bytes，執行安全截斷: {url}"
                        )
                        break
                raw_content = b"".join(chunks)
                return raw_content.decode("utf-8", errors="replace")

    async def fetch_xml_root(self, url: str, byte_cap: int = 1_500_000) -> ET.Element:
        """安全拉取 XML 並透過 defusedxml 解析根節點（自動移除命名空間前綴）。"""
        content_text = await self.fetch_document_text(url, byte_cap=byte_cap)
        root = ET.fromstring(content_text)
        for node in root.iter():
            if "}" in node.tag:
                node.tag = node.tag.split("}", 1)[1]
        return root
