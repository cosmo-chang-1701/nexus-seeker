"""SEC EDGAR 申報事件同步與回填服務 (FilingEventService)。

職責：
1. 依據標的游標 (sec_filing_cursor) 增量拉取 SEC EDGAR 申報事件。
2. 針對新追蹤標的執行最多 90 天 Form 4 內部人交易回填 (Backfill)。
3. 分流解析 Form 4 XML 並儲存內部人交易 (insider_transaction)。
4. 識別 8-K Item 4.02 / 5.02 並儲存治理審查旗標 (governance_flag)。
5. 結構化 Schedule 13D / 13D/A 下載後交由 activist_gate 評估（record-only，只記日誌）。
6. 嚴格遵循零交易執行不變量與 DRY_RUN 預設規範，推播統一走 defense_fundamental_thesis 頻道。

接線狀態：由 `cogs/trading/fundamental_pipeline_monitor.py` 的 `sec_filing_sync_hourly`
ClockJob（平日 07:00–20:00 ET 整點）以背景任務呼叫 `sync_universe_filings`，
規格見 docs/macro_sentiment/06_sec_event_stream_and_governance_gate.md。

受理時間：submissions JSON 的 acceptanceDateTime 不可信（部分公司比真實 UTC 多出美東
偏移），只當作「真實時間的上界」做初步篩選；每筆實際處理的申報都改讀 SGML 表頭
`{accession}.hdr.sgml` 取得權威美東受理時間，事件與游標皆儲存正規化後的美東 ISO 8601。

游標不變量：任何一筆申報（表頭、Form 4 下載或解析）失敗時，游標只推進到「第一筆失敗
之前」最新的成功申報，失敗者下次重試；首次回填只要有失敗就不寫游標，下次重新回填。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from market_analysis.fundamental_pipeline.activist_gate import (
    evaluate_activist_filing,
    parse_schedule_13d_xml,
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
    SEC_JSON_ACCEPTANCE_MAX_SKEW,
    classify_filing_session,
    extract_8k_items,
    parse_sec_acceptance_datetime,
    route_filing,
)
from services.fundamental_universe import get_fundamental_universe
from services.sec_edgar_client import SecEdgarClient

logger = logging.getLogger(__name__)

_FORM4_FORMS = ("4", "4/A")
_FORM_8K_FORMS = ("8-K", "8-K/A")


@dataclass(frozen=True)
class _PendingNotification:
    """待推播之治理旗標與其事件日（美東受理日期）。"""

    flag: GovernanceFlagRecord
    event_date: str


def _json_acceptance_upper_bound(raw: str) -> datetime | None:
    """submissions JSON acceptanceDateTime 依字面時區解析，作為真實受理時間的上界。"""
    if not raw:
        return None
    try:
        return parse_sec_acceptance_datetime(raw)
    except (ValueError, TypeError):
        return None


def _cursor_datetime(cursor: FilingCursorRecord) -> datetime | None:
    """解析游標時間；舊版直接存 JSON 字串（UTC 標示）者保守回退最大偏差。"""
    raw = cursor.last_accepted_at.strip()
    if not raw:
        return None
    try:
        dt = parse_sec_acceptance_datetime(raw)
    except (ValueError, TypeError):
        return None
    if raw.endswith("Z") or raw.endswith("+00:00"):
        dt = dt - SEC_JSON_ACCEPTANCE_MAX_SKEW
    return dt


def _earliest(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate < current else current


_FAILURE_FLOOR_UNKNOWN = datetime.min.replace(tzinfo=timezone.utc)


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
        """惰性獲取或初始化 SEC EDGAR 客戶端（缺少合規 User-Agent 時拋出 SecConfigError）。"""
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
            "failed": 0,
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

        cursor_dt = _cursor_datetime(cursor) if cursor is not None else None

        # 計算回填時間門檻
        now_utc = datetime.now(timezone.utc)
        cutoff_backfill_date = (now_utc - timedelta(days=backfill_form4_days)).strftime(
            "%Y-%m-%d"
        )
        cutoff_gov_date = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d")

        new_events: list[FilingEventRecord] = []
        new_txs: list[InsiderTxRecord] = []
        new_flags: list[GovernanceFlagRecord] = []
        pending_notifications: list[_PendingNotification] = []
        # 成功處理之 (權威受理時間, accession)
        processed: list[tuple[datetime, str]] = []
        # 所有失敗申報中最早的（可能）受理時間；游標不得越過此時間
        failure_floor: datetime | None = None

        cik_int = str(int(cik))

        for idx, accession in enumerate(accession_list):
            form = form_list[idx] if idx < len(form_list) else "UNKNOWN"
            f_date = filing_date_list[idx] if idx < len(filing_date_list) else ""
            acc_at_raw = accepted_at_list[idx] if idx < len(accepted_at_list) else ""
            p_doc = primary_doc_list[idx] if idx < len(primary_doc_list) else ""
            raw_items = items_list[idx] if idx < len(items_list) else ""
            json_upper = _json_acceptance_upper_bound(acc_at_raw)

            is_backfill = False
            if cursor is not None:
                # 增量模式：JSON 值是真實時間上界，上界不晚於游標者必為舊申報
                if accession == cursor.last_accession:
                    continue
                if (
                    json_upper is not None
                    and cursor_dt is not None
                    and json_upper <= cursor_dt
                ):
                    continue
            else:
                # 首次執行：僅回填窗口內的 Form 4 / 8-K，其餘表單只處理最新一筆
                if form in _FORM4_FORMS:
                    if f_date < cutoff_backfill_date:
                        continue
                    is_backfill = True
                elif form in _FORM_8K_FORMS:
                    # 8-K 申報於 30 天治理審查窗口內應保留回填，防禦性捕獲近期重編與高管變更
                    if f_date < cutoff_gov_date:
                        continue
                    is_backfill = True
                elif idx > 0:
                    continue

            # 權威受理時間（SGML 表頭，美東牆上時間）
            try:
                accepted_et = await client.fetch_acceptance_datetime(cik, accession)
            except Exception as e:
                logger.error(
                    f"[FilingEventService] 讀取申報表頭受理時間失敗 ({accession}, {sym_upper}): {e}"
                )
                stats["failed"] += 1
                failure_floor = _earliest(
                    failure_floor,
                    json_upper - SEC_JSON_ACCEPTANCE_MAX_SKEW
                    if json_upper is not None
                    else _FAILURE_FLOOR_UNKNOWN,
                )
                continue

            if cursor_dt is not None and accepted_et <= cursor_dt:
                # JSON 值偏高造成的誤判：權威時間不晚於游標，屬已處理之舊申報
                continue

            acc_no_dash = accession.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{p_doc}"
                if p_doc
                else None
            )

            accepted_iso = accepted_et.isoformat()
            session = classify_filing_session(accepted_et)
            parsed_items = extract_8k_items(raw_items) if raw_items else []
            routes = route_filing(form, parsed_items)

            # 處理 Form 4 內部人交易（移除 xsl 樣式目錄前綴以取得純 XML）
            filing_txs: list[InsiderTxRecord] = []
            if form in _FORM4_FORMS and p_doc:
                clean_p_doc = p_doc.split("/")[-1] if "/" in p_doc else p_doc
                raw_xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{clean_p_doc}"
                try:
                    xml_text = await client.fetch_document_text(raw_xml_url)
                    filing_txs = parse_form4_xml(
                        xml_text,
                        accession,
                        sym_upper,
                        is_backfill=is_backfill,
                        raise_on_error=True,
                    )
                except Exception as e:
                    logger.error(
                        f"[FilingEventService] 下載或解析 Form 4 失敗 ({accession}, {sym_upper})，"
                        f"游標將停在此筆之前並於下次重試: {e}"
                    )
                    stats["failed"] += 1
                    failure_floor = _earliest(failure_floor, accepted_et)
                    continue

            if "ACTIVIST_13D" in routes and p_doc:
                await self._evaluate_activist_13d(
                    sym_upper,
                    accession,
                    cik_int,
                    acc_no_dash,
                    p_doc,
                    filing_date=f_date or accepted_et.date().isoformat(),
                )

            # 處理 8-K 治理審查
            filing_flags: list[GovernanceFlagRecord] = []
            if form in _FORM_8K_FORMS and parsed_items:
                filing_flags = generate_governance_flags_from_8k(
                    symbol=sym_upper,
                    accession=accession,
                    items=parsed_items,
                    accepted_at=accepted_et,
                )

            new_events.append(
                FilingEventRecord(
                    accession=accession,
                    symbol=sym_upper,
                    form=form,
                    items=",".join(parsed_items) if parsed_items else None,
                    accepted_at=accepted_iso,
                    session=session,
                    primary_doc_url=doc_url,
                    routes_json=json.dumps(routes),
                    is_backfill=is_backfill,
                )
            )
            new_txs.extend(filing_txs)
            new_flags.extend(filing_flags)
            if not is_backfill:
                # 回填的歷史事件只入庫、不推播
                event_date = accepted_et.date().isoformat()
                pending_notifications.extend(
                    _PendingNotification(flag=f, event_date=event_date)
                    for f in filing_flags
                )
            processed.append((accepted_et, accession))

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

        # 若啟用推播且非乾跑模式，發送治理風控警訊（回填事件不推播）
        if (
            pending_notifications
            and not config.FUNDAMENTAL_PIPELINE_DRY_RUN
            and self._bot is not None
        ):
            await self._dispatch_governance_notifications(
                sym_upper, pending_notifications
            )

        await self._advance_cursor(
            sym_upper,
            cik,
            cursor,
            cursor_dt,
            processed,
            failure_floor,
            newest_accession=accession_list[0],
        )
        return stats

    async def _evaluate_activist_13d(
        self,
        symbol: str,
        accession: str,
        cik_int: str,
        acc_no_dash: str,
        primary_doc: str,
        filing_date: str,
    ) -> None:
        """下載結構化 Schedule 13D 並交由 activist_gate 評估（record-only，只記日誌）。

        盡力而為：結果不入庫、不推播，因此下載或解析失敗只記 warning，不計入 failed、
        不阻擋游標。舊版 HTML / 純文字 `SC 13D` 沒有結構化欄位，直接略過。
        """
        clean_doc = primary_doc.split("/")[-1]
        if not clean_doc.lower().endswith(".xml"):
            logger.debug(
                f"[FilingEventService] {symbol} 13D ({accession}) 非結構化 XML，略過激進投資人評估。"
            )
            return
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{clean_doc}"
        try:
            client = await self.get_client()
            xml_text = await client.fetch_document_text(url)
        except Exception as e:
            logger.warning(
                f"[FilingEventService] 下載 {symbol} 13D ({accession}) 失敗，略過激進投資人評估: {e}"
            )
            return

        fields = parse_schedule_13d_xml(xml_text)
        if fields is None:
            logger.warning(
                f"[FilingEventService] 無法解析 {symbol} 13D ({accession}) 結構化欄位，略過激進投資人評估。"
            )
            return

        signal = evaluate_activist_filing(
            symbol=symbol,
            accession=accession,
            investor_name=fields.investor_name,
            ownership_pct=fields.ownership_pct,
            item_4_text=fields.item_4_text,
            event_date=fields.event_date,
            filing_date=filing_date,
        )
        logger.info(
            f"[FilingEventService] 13D 激進投資人評估 {symbol} ({accession}): "
            f"意圖={signal.key_intents or '無'}，逾期={signal.is_delayed_filing}；{signal.summary_text}"
        )

    async def _advance_cursor(
        self,
        symbol: str,
        cik: str,
        cursor: FilingCursorRecord | None,
        cursor_dt: datetime | None,
        processed: Sequence[tuple[datetime, str]],
        failure_floor: datetime | None,
        newest_accession: str,
    ) -> None:
        """依成功處理結果推進游標；任何失敗都讓游標停在第一筆失敗之前。"""
        if cursor is None and failure_floor is not None:
            # 首次回填有失敗：不寫游標，下次以回填模式（不推播）整批重試
            logger.warning(
                f"[FilingEventService] {symbol} 首次回填有申報處理失敗，暫不建立游標，下次重新回填。"
            )
            return

        candidates = [
            p for p in processed if failure_floor is None or p[0] < failure_floor
        ]
        if candidates:
            best_dt, best_accession = max(candidates)
        elif cursor is None:
            # 首次執行但窗口內無可處理申報：以最新一筆的權威受理時間初始化游標，
            # 避免每次都重新全量回填
            client = await self.get_client()
            try:
                best_dt = await client.fetch_acceptance_datetime(cik, newest_accession)
            except Exception as e:
                logger.error(
                    f"[FilingEventService] 初始化 {symbol} 游標時讀取表頭失敗 ({newest_accession}): {e}"
                )
                return
            best_accession = newest_accession
        else:
            return

        if cursor_dt is not None and best_dt <= cursor_dt:
            return

        await upsert_sec_filing_cursor(
            FilingCursorRecord(
                symbol=symbol,
                cik=cik,
                last_accepted_at=best_dt.isoformat(),
                last_accession=best_accession,
            )
        )

    async def _dispatch_governance_notifications(
        self,
        symbol: str,
        pending: Sequence[_PendingNotification],
    ) -> None:
        """當觸發 CRITICAL 或 HIGH 等級治理旗標時，向持倉者推播顧問性警訊。

        去重鍵為 `governance_flag_{user_id}_{symbol}_{flag_kind}_{事件日}`（前綴已登記於
        database/cache.py::_KV_CACHE_DEDUP_KEY_PREFIXES）：同一事件在游標因失敗停留而
        重處理時不會重複推播。REVIEW / INFO 不推播。
        """
        critical_or_high = [
            p for p in pending if p.flag.severity in ("CRITICAL", "HIGH")
        ]
        if not critical_or_high:
            return

        from cogs.embed_builders.fundamental_embeds import build_governance_flag_embed
        from database.portfolio import get_all_portfolio_symbol_pairs
        from services.notification_dispatcher import notify

        pairs = await asyncio.to_thread(get_all_portfolio_symbol_pairs)
        holder_ids = sorted(
            {uid for uid, sym in pairs if sym.upper() == symbol.upper()}
        )

        for item in critical_or_high:
            flag = item.flag
            embed = build_governance_flag_embed(symbol, flag)
            for user_id in holder_ids:
                dedup_key = f"governance_flag_{user_id}_{symbol}_{flag.flag_kind}_{item.event_date}"
                try:
                    await notify(
                        self._bot,
                        user_id,
                        "defense_fundamental_thesis",
                        embed=embed,
                        dedup_key=dedup_key,
                    )
                except Exception as e:
                    logger.warning(f"發送治理警訊至用戶 {user_id} 失敗: {e}")

    async def sync_universe_filings(
        self,
        max_symbols: int | None = None,
        backfill_form4_days: int = 90,
    ) -> dict[str, int]:
        """同步全域基本面標的池的 SEC 申報。

        缺少合規 SEC_USER_AGENT 時在併發前即拋出 SecConfigError（fail fast）；
        個別標的的例外以 logger.error 記錄，不中斷其他標的。
        """
        # 先建立客戶端：設定錯誤應立即中止，而不是被 gather 吞成 N 筆相同的例外
        await self.get_client()

        universe = await get_fundamental_universe(max_symbols=max_symbols)
        total_stats: dict[str, int] = {
            "events": 0,
            "insider_txs": 0,
            "governance_flags": 0,
            "failed": 0,
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

        failed_symbols = 0
        for sym, res in zip(universe, results):
            if isinstance(res, BaseException):
                failed_symbols += 1
                logger.error(
                    f"[FilingEventService] 同步 {sym} 申報時發生未預期例外: {res!r}",
                    exc_info=res,
                )
                continue
            for key in total_stats:
                total_stats[key] += res.get(key, 0)

        logger.info(
            f"[FilingEventService] 宇宙申報同步完成: 處理 {len(universe)} 檔標的"
            f"（例外 {failed_symbols} 檔），新增事件 {total_stats['events']} 筆，"
            f"內部人交易 {total_stats['insider_txs']} 筆，治理旗標 {total_stats['governance_flags']} 筆，"
            f"處理失敗申報 {total_stats['failed']} 筆。"
        )
        return total_stats
