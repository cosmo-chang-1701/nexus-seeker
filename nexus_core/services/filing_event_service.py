"""SEC EDGAR 申報事件同步與回填服務 (FilingEventService)。

職責：
1. 依據標的游標 (sec_filing_cursor) 增量拉取 SEC EDGAR 申報事件。
2. 針對新追蹤標的執行最多 90 天 Form 4 內部人交易回填 (Backfill)。
3. 分流解析 Form 4 XML 並儲存內部人交易 (insider_transaction)。
4. 識別 8-K Item 4.02 / 5.02 並儲存治理審查旗標 (governance_flag)。
5. 嚴格遵循零交易執行不變量與 DRY_RUN 預設規範，推播統一走 defense_fundamental_thesis 頻道。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Sequence

import config
from database.fundamental_pipeline import (
    get_sec_filing_cursor,
    save_governance_flags,
    save_insider_transactions,
    save_sec_filing_events,
    upsert_sec_filing_cursor,
)
from market_analysis.fundamental_pipeline.form4_parser import parse_form4_xml
from market_analysis.fundamental_pipeline.governance_gate import (
    generate_governance_flags_from_8k,
)
from market_analysis.fundamental_pipeline.models import (
    FilingCursorRecord,
    FilingEventRecord,
    GovernanceFlagRecord,
    InsiderTxRecord,
)
from market_analysis.fundamental_pipeline.sec_item_router import (
    classify_filing_session,
    extract_8k_items,
    route_filing,
)
from services.fundamental_universe import get_fundamental_universe
from services.sec_edgar_client import SecEdgarClient

logger = logging.getLogger(__name__)


class FilingEventService:
    """SEC 申報事件排程同步與處理協調服務。"""

    def __init__(
        self,
        client: SecEdgarClient | None = None,
        bot: Any | None = None,
    ) -> None:
        self._client = client
        self._bot = bot

    async def get_client(self) -> SecEdgarClient:
        """惰性獲取或初始化 SEC EDGAR 客戶端。"""
        if self._client is None:
            self._client = SecEdgarClient()
        return self._client

    async def sync_symbol_filings(
        self,
        symbol: str,
        backfill_form4_days: int = 90,
    ) -> dict[str, int]:
        """增量同步或回填單一標的的 SEC 申報事件。"""
        sym_upper = symbol.strip().upper()
        stats: dict[str, int] = {
            "events": 0,
            "insider_txs": 0,
            "governance_flags": 0,
        }

        cursor = await asyncio.to_thread(get_sec_filing_cursor, sym_upper)
        client = await self.get_client()

        cik = await client.get_cik(sym_upper)
        if not cik:
            logger.warning(
                f"[FilingEventService] 無法解析 {sym_upper} 之 CIK，跳過同步。"
            )
            return stats

        try:
            submissions = await client.fetch_company_submissions(cik)
        except Exception as e:
            logger.error(
                f"[FilingEventService] 拉取 {sym_upper} (CIK: {cik}) 申報清單失敗: {e}"
            )
            return stats

        recent = submissions.get("filings", {}).get("recent", {})
        accession_list: list[str] = recent.get("accessionNumber", [])
        if not accession_list:
            return stats

        form_list: list[str] = recent.get("form", [])
        filing_date_list: list[str] = recent.get("filingDate", [])
        accepted_at_list: list[str] = recent.get("acceptanceDateTime", [])
        primary_doc_list: list[str] = recent.get("primaryDocument", [])
        items_list: list[str] = recent.get("items", [])

        is_first_run = cursor is None
        last_accepted = cursor.last_accepted_at if cursor else ""

        # 計算回填時間門檻
        now_utc = datetime.now(timezone.utc)
        cutoff_backfill_date = (now_utc - timedelta(days=backfill_form4_days)).strftime(
            "%Y-%m-%d"
        )
        cutoff_gov_date = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d")

        new_events: list[FilingEventRecord] = []
        new_txs: list[InsiderTxRecord] = []
        new_flags: list[GovernanceFlagRecord] = []

        cik_int = str(int(cik))
        total_filings = len(accession_list)

        newest_accepted_at = (
            accepted_at_list[0]
            if (is_first_run and accepted_at_list)
            else (cursor.last_accepted_at if cursor else "")
        )
        newest_accession = (
            accession_list[0]
            if (is_first_run and accession_list)
            else (cursor.last_accession if cursor else "")
        )

        for idx in range(total_filings):
            accession = accession_list[idx]
            form = form_list[idx] if idx < len(form_list) else "UNKNOWN"
            f_date = filing_date_list[idx] if idx < len(filing_date_list) else ""
            acc_at = accepted_at_list[idx] if idx < len(accepted_at_list) else ""
            p_doc = primary_doc_list[idx] if idx < len(primary_doc_list) else ""
            raw_items = items_list[idx] if idx < len(items_list) else ""

            # 增量判斷：若已有游標且比游標舊或相等，停止或跳過
            if cursor and acc_at <= last_accepted:
                continue

            # 首次執行若超出回填範圍且非最新第一筆，跳過
            is_backfill = False
            if is_first_run:
                if form in ("4", "4/A"):
                    if f_date < cutoff_backfill_date:
                        continue
                    is_backfill = True
                elif form in ("8-K", "8-K/A"):
                    # 8-K 申報於 30 天治理審查窗口內應保留回填，防禦性捕獲近期重編與高管變更
                    if f_date < cutoff_gov_date:
                        continue
                    is_backfill = True
                elif idx > 0:
                    continue

            # 追蹤最新 acceptanceDateTime
            if acc_at > newest_accepted_at:
                newest_accepted_at = acc_at
                newest_accession = accession

            acc_no_dash = accession.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{p_doc}"
                if p_doc
                else None
            )

            session = classify_filing_session(acc_at)
            parsed_items = extract_8k_items(raw_items) if raw_items else []
            routes = route_filing(form, parsed_items)

            event = FilingEventRecord(
                accession=accession,
                symbol=sym_upper,
                form=form,
                items=",".join(parsed_items) if parsed_items else None,
                accepted_at=acc_at,
                session=session,
                primary_doc_url=doc_url,
                routes_json=json.dumps(routes),
                is_backfill=is_backfill,
            )
            new_events.append(event)

            # 處理 Form 4 內部人交易（移除 xsl 樣式目錄前綴以取得純 XML）
            if form in ("4", "4/A") and p_doc:
                clean_p_doc = p_doc.split("/")[-1] if "/" in p_doc else p_doc
                raw_xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{clean_p_doc}"
                try:
                    xml_text = await client.fetch_document_text(raw_xml_url)
                    txs = parse_form4_xml(
                        xml_text, accession, sym_upper, is_backfill=is_backfill
                    )
                    if txs:
                        new_txs.extend(txs)
                except Exception as e:
                    logger.warning(
                        f"[FilingEventService] 解析 Form 4 失敗 ({accession}, {sym_upper}): {e}"
                    )

            # 處理 8-K 治理審查
            if form in ("8-K", "8-K/A") and parsed_items:
                flags = generate_governance_flags_from_8k(
                    symbol=sym_upper,
                    accession=accession,
                    items=parsed_items,
                    accepted_at=acc_at,
                )
                if flags:
                    new_flags.extend(flags)

        # 批次寫入資料庫
        if new_events:
            await save_sec_filing_events(new_events)
            stats["events"] = len(new_events)

        if new_txs:
            await save_insider_transactions(new_txs)
            stats["insider_txs"] = len(new_txs)

        if new_flags:
            await save_governance_flags(new_flags)
            stats["governance_flags"] = len(new_flags)

            # 若啟用推播且非乾跑模式，發送治理風控警訊
            if not config.FUNDAMENTAL_PIPELINE_DRY_RUN and self._bot is not None:
                await self._dispatch_governance_notifications(sym_upper, new_flags)

        # 更新游標
        if newest_accepted_at and (
            cursor is None or newest_accepted_at > cursor.last_accepted_at
        ):
            new_cursor = FilingCursorRecord(
                symbol=sym_upper,
                cik=cik,
                last_accepted_at=newest_accepted_at,
                last_accession=newest_accession,
            )
            await upsert_sec_filing_cursor(new_cursor)

        return stats

    async def _dispatch_governance_notifications(
        self,
        symbol: str,
        flags: Sequence[GovernanceFlagRecord],
    ) -> None:
        """當觸發 CRITICAL 或 HIGH 等級治理旗標時，向持倉者推播顧問性警訊。"""
        critical_or_high = [f for f in flags if f.severity in ("CRITICAL", "HIGH")]
        if not critical_or_high:
            return

        from cogs.embed_builders.fundamental_embeds import build_governance_flag_embed
        from database.portfolio import get_all_portfolio_symbol_pairs
        from services.notification_dispatcher import notify

        pairs = await asyncio.to_thread(get_all_portfolio_symbol_pairs)
        holder_ids = [uid for uid, sym in pairs if sym.upper() == symbol.upper()]

        for flag in critical_or_high:
            embed = build_governance_flag_embed(symbol, flag)
            for user_id in holder_ids:
                try:
                    await notify(
                        self._bot,
                        user_id,
                        "defense_fundamental_thesis",
                        embed=embed,
                    )
                except Exception as e:
                    logger.warning(f"發送治理警訊至用戶 {user_id} 失敗: {e}")

    async def sync_universe_filings(
        self,
        max_symbols: int | None = None,
        backfill_form4_days: int = 90,
    ) -> dict[str, int]:
        """同步全域基本面標的池的 SEC 申報。"""
        universe = await get_fundamental_universe(max_symbols=max_symbols)
        total_stats: dict[str, int] = {
            "events": 0,
            "insider_txs": 0,
            "governance_flags": 0,
        }

        semaphore = asyncio.Semaphore(3)

        async def _sync_with_sem(sym: str) -> dict[str, int]:
            async with semaphore:
                return await self.sync_symbol_filings(
                    sym, backfill_form4_days=backfill_form4_days
                )

        results = await asyncio.gather(
            *[_sync_with_sem(s) for s in universe], return_exceptions=True
        )

        for res in results:
            if isinstance(res, dict):
                total_stats["events"] += res.get("events", 0)
                total_stats["insider_txs"] += res.get("insider_txs", 0)
                total_stats["governance_flags"] += res.get("governance_flags", 0)

        logger.info(
            f"[FilingEventService] 宇宙申報同步完成: 處理 {len(universe)} 檔標的，"
            f"新增事件 {total_stats['events']} 筆，內部人交易 {total_stats['insider_txs']} 筆，"
            f"治理旗標 {total_stats['governance_flags']} 筆。"
        )
        return total_stats
