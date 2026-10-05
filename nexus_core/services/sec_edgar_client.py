"""SEC EDGAR 官方 API 專屬直連非同步客戶端。

核心安全防護與架構原則：
1. 嚴格限速：aiolimiter.AsyncLimiter(max_rate=8, time_period=1.0) ≤ 8 req/s (低於 SEC 10 req/s 上限)。
2. 串流下載防爆：強制 1.5MB (1_500_000 bytes) 硬截斷，保障 1GB–2GB VPS 記憶體。
3. 杜絕 XXE：全面透過 defusedxml.ElementTree 解析 XML 檔案。
4. 認證檢查：強制驗證 SEC_USER_AGENT 格式（包含 @ 聯絡信箱），缺少時拋出 SecConfigError。
5. CIK 查詢快取：支援 10 位數自動補零 (zfill(10)) 與官方 company_tickers.json 快取。
"""

from __future__ import annotations

import logging
from typing import Any
from aiolimiter import AsyncLimiter
import defusedxml.ElementTree as ET
import httpx

import config

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


class SecConfigError(RuntimeError):
    """SEC 缺少正式認證聯絡資訊時的防禦性異常。"""


class SecEdgarClient:
    """SEC EDGAR 官方非同步 HTTP 客戶端。"""

    def __init__(
        self,
        user_agent: str | None = None,
        timeout_seconds: float = 20.0,
        max_rate: float = 8.0,
        time_period: float = 1.0,
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
        self._limiter = AsyncLimiter(max_rate=max_rate, time_period=time_period)
        self._timeout = timeout_seconds
        self._cik_cache: dict[str, str] = dict(_COMMON_CIK_SEEDS)

    async def get_cik(self, symbol: str) -> str | None:
        """根據股票代碼查詢 10 位數補零之 CIK，支援雙重股權代碼標準化映射。"""
        sym_clean = symbol.strip().upper()
        # 雙重股權標的（如 BRK.B <-> BRK-B）雙向標準化候選清單
        variants = [sym_clean]
        if "." in sym_clean:
            variants.append(sym_clean.replace(".", "-"))
        if "-" in sym_clean:
            variants.append(sym_clean.replace("-", "."))

        for v in variants:
            if v in self._cik_cache:
                return self._cik_cache[v]

        # 若未命中快取，向 SEC 官方取得 company_tickers.json
        try:
            url = "https://www.sec.gov/files/company_tickers.json"
            async with self._limiter:
                async with httpx.AsyncClient(
                    headers=self._headers, timeout=self._timeout
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()

                    for entry in data.values():
                        ticker = str(entry.get("ticker", "")).strip().upper()
                        raw_cik = entry.get("cik_str")
                        if ticker and raw_cik is not None:
                            cik10 = str(raw_cik).zfill(10)
                            self._cik_cache[ticker] = cik10
                            if "-" in ticker:
                                self._cik_cache[ticker.replace("-", ".")] = cik10
                            elif "." in ticker:
                                self._cik_cache[ticker.replace(".", "-")] = cik10

            for v in variants:
                if v in self._cik_cache:
                    return self._cik_cache[v]
            return None
        except Exception as e:
            logger.warning(f"查詢 SEC 官方 Ticker-CIK 映射表失敗 ({sym_clean}): {e}")
            return None

    async def fetch_company_submissions(self, cik: str) -> dict[str, Any]:
        """拉取指定 CIK 之最近申報事件清單 (Submissions API)。"""
        cik10 = str(cik).strip().zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik10}.json"

        async with self._limiter:
            async with httpx.AsyncClient(
                headers=self._headers, timeout=self._timeout
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return data

    async def fetch_document_text(self, url: str, byte_cap: int = 1_500_000) -> str:
        """串流拉取原始文本，到達 byte_cap 上限時執行防爆截斷。"""
        async with self._limiter:
            async with httpx.AsyncClient(
                headers=self._headers, timeout=self._timeout
            ) as client:
                async with client.stream("GET", url) as response:
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
