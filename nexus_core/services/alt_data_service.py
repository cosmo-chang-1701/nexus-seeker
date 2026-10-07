"""高頻實體與產業鏈替代數據服務 (Alternative Data & Supply Chain Service)。

資料源（皆為免費公開來源）：
1. TSA 每日安檢客流（兩欄表格：當年度頁 + 歷年頁，以 52 週前同星期幾對齊）。
2. FRED 鐵路貨運 (RAILFRTCARLOADSD11) 與全美汽車總銷量 (TOTALSA)。
3. 台灣證交所 (TWSE) / 櫃買中心 (TPEx) 上市櫃月營收 OpenAPI。
4. SEC XBRL companyconcept：capex / rpo / dio / revenue 驅動端與美股跟隨端指標。

時間對齊：每次檢驗都針對單一曆季 `as_of_period`（`YYYY-Qn`），各觀測值帶觀測期間，
不屬於該期別者一律排除並記錄原因；所有來源皆以 `as_of` 防前視。

嚴格遵循：
- 外部抓取經 SingleFlightManager 合併、記憶體 TTL 快取（只保留解析後的精簡數列）。
- asyncio.Semaphore(3) 節流 TSA / TWSE / TPEx；SEC 走 SecEdgarClient 的 8 req/s 限速。
- 單一成員或單條鏈失敗不中斷其他鏈；零交易執行不變量（純顧問性分析，不推播）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from database.fundamental_pipeline import save_channel_check_logs
from market_analysis.fundamental_pipeline.alt_data_metrics import (
    MetricYoY,
    WindowYoY,
    XbrlFact,
    dio_yoy,
    flow_quarter_yoy,
    instant_yoy,
    monthly_period_yoy,
    parse_companyconcept_facts,
    parse_roc_date,
    parse_roc_year_month,
    parse_tsa_table,
    period_bounds,
    period_of_date,
    tsa_period_yoy,
    validate_period,
)
from market_analysis.fundamental_pipeline.channel_check import (
    evaluate_channel_check,
    result_to_log_record,
)
from market_analysis.fundamental_pipeline.models import (
    ChannelCheckResult,
    SupplyChainLink,
)
from market_analysis.fundamental_pipeline.supply_chain_map import (
    SUPPLY_CHAIN_LINKS,
)
from market_analysis.macro_signals import SeriesKind, usable
from services.macro_signal_service import fetch_fred_series
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)

_ET_ZONE = ZoneInfo("America/New_York")

# TSA 每日安檢客流頁面（當年度；歷年為 `/{year}`）
TSA_PASSENGER_VOLUMES_URL = "https://www.tsa.gov/travel/passenger-volumes"

# 台灣 TWSE 與 TPEx 月營收 OpenAPI 免費公開端點
TWSE_MONTHLY_REVENUE_OPENAPI_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_MONTHLY_REVENUE_OPENAPI_URL = (
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
)

# 快取存活時間 (秒)
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0
SEC_CONCEPT_CACHE_TTL_SECONDS: float = 24 * 3600.0
SEC_CONCEPT_CACHE_MAX_ENTRIES: int = 512

# 非公開申報實體（無 SEC XBRL），一律排除於覆蓋率分母
NON_PUBLIC_ENTITIES: frozenset[str] = frozenset({"SPCX", "SPACEX", "SPACEXSI"})

# SEC XBRL 標籤（依優先序；取第一個能同時提供當季與去年同季者）
XBRL_FLOW_TAGS: dict[str, tuple[str, ...]] = {
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ),
}
XBRL_RPO_TAGS: tuple[str, ...] = ("RevenueRemainingPerformanceObligation",)
XBRL_INVENTORY_TAGS: tuple[str, ...] = ("InventoryNet",)
XBRL_COGS_TAGS: tuple[str, ...] = (
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "CostOfGoodsSold",
)

# 每一群組（美股 / 台股）至少需半數可計算成員才採用群組均值
MIN_GROUP_COVERAGE_RATIO: float = 0.5

_FRED_KINDS: dict[str, SeriesKind] = {
    "RAILFRTCARLOADSD11": "monthly_rail",
    "TOTALSA": "monthly_auto",
}

_TW_EMPTY_TOKENS = frozenset({"", "--", "-", "N/A", "null", "None", "不適用"})


@dataclass(frozen=True)
class TwRevenueRow:
    """台股單一公司月營收年增率與其資料月份。"""

    yoy_pct: float
    data_month: date | None
    issued: date | None


@dataclass
class SideMeasurement:
    """產業鏈單側（驅動端或跟隨端）的綜合增長率與成員明細。"""

    growth: float | None
    notes: list[str] = field(default_factory=list)
    groups: dict[str, Any] = field(default_factory=dict)

    @property
    def note(self) -> str:
        return "；".join(n for n in self.notes if n)


def _today_et() -> date:
    return datetime.now(_ET_ZONE).date()


class AltDataService:
    """高頻實體與產業鏈替代數據服務客戶端。"""

    def __init__(
        self, timeout_seconds: float = 15.0, sec_client: Any | None = None
    ) -> None:
        self._timeout = timeout_seconds
        self._semaphore = asyncio.Semaphore(3)
        self._sec_client: Any | None = sec_client
        self._sec_unavailable_reason: str | None = None

        # 記憶體 TTL 快取（只保留解析後的精簡結構）
        self._tsa_cache: dict[int, tuple[float, dict[date, int]]] = {}
        self._tw_cache: dict[str, tuple[float, dict[str, TwRevenueRow]]] = {}
        self._concept_cache: dict[
            tuple[str, str], tuple[float, list[XbrlFact] | None]
        ] = {}

    # ========================================================================
    # 1. TSA 每日安檢客流
    # ========================================================================

    async def fetch_tsa_daily(
        self, year: int, today: date | None = None
    ) -> dict[date, int]:
        """抓取並解析 TSA 某年度日客流（當年度頁或 `/{year}` 歷年頁）。"""
        ref_today = today if today is not None else _today_et()
        now_ts = time.monotonic()
        is_current = year >= ref_today.year
        ttl = DEFAULT_CACHE_TTL_SECONDS if is_current else 24 * 3600.0
        cached = self._tsa_cache.get(year)
        if cached is not None and (now_ts - cached[0]) < ttl:
            return cached[1]

        url = (
            TSA_PASSENGER_VOLUMES_URL
            if is_current
            else f"{TSA_PASSENGER_VOLUMES_URL}/{year}"
        )

        async def _fetch() -> dict[date, int]:
            async with self._semaphore:
                try:
                    headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        )
                    }
                    async with httpx.AsyncClient(
                        timeout=self._timeout, headers=headers
                    ) as client:
                        resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(
                            f"[AltData] TSA 頁面回應異常狀態碼 {resp.status_code}: {url}"
                        )
                        return {}
                    parsed = parse_tsa_table(resp.text)
                    if not parsed:
                        logger.warning(f"[AltData] TSA 表格格式未匹配: {url}")
                    return {d: v for d, v in parsed.items() if d.year == year}
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[AltData] 拉取 TSA 客流失敗 ({url}): {e}")
                    return {}

        result = cast(
            dict[date, int],
            await SingleFlightManager.run(f"alt_data:tsa:{year}", _fetch),
        )
        if result:
            self._tsa_cache[year] = (now_ts, result)
        return result

    async def get_tsa_period_yoy(
        self, period: str, as_of: date
    ) -> tuple[WindowYoY | None, str]:
        """TSA 目標曆季（截至 as_of）日均客流 vs 52 週前同星期幾年增率。"""
        start, end = period_bounds(period)
        years = sorted({start.year - 1, start.year, end.year})
        daily: dict[date, int] = {}
        for y in years:
            daily.update(await self.fetch_tsa_daily(y, today=as_of))
        if not daily:
            return None, "TSA 頁面無法取得或格式不符"
        res = tsa_period_yoy(daily, period, as_of)
        if res is None:
            return None, f"TSA {period} 配對日數不足 28 日"
        return res, ""

    # ========================================================================
    # 2. FRED 月度序列
    # ========================================================================

    async def get_fred_period_yoy(
        self, series_id: str, period: str, as_of: date
    ) -> tuple[WindowYoY | None, str]:
        """FRED 月度序列目標曆季已公布月份均值 vs 去年同月份年增率（不改用月增率）。"""
        kind = _FRED_KINDS.get(series_id)
        try:
            obs = await fetch_fred_series(series_id, as_of, kind=kind)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AltData] 拉取 FRED {series_id} 失敗: {e}")
            return None, f"FRED {series_id} 抓取失敗"
        pts = [(o.obs_date, o.value) for o in usable(obs, as_of)]
        res = monthly_period_yoy(pts, period)
        if res is None:
            return None, f"FRED {series_id} {period} 尚無可用月份或缺去年同月"
        return res, ""

    # ========================================================================
    # 3. 台灣上市櫃月營收 OpenAPI (TWSE / TPEx)
    # ========================================================================

    async def _fetch_tw_monthly(self, market: str, url: str) -> dict[str, TwRevenueRow]:
        now_ts = time.monotonic()
        cached = self._tw_cache.get(market)
        if cached is not None and (now_ts - cached[0]) < DEFAULT_CACHE_TTL_SECONDS:
            return cached[1]

        async def _fetch() -> dict[str, TwRevenueRow]:
            async with self._semaphore:
                res: dict[str, TwRevenueRow] = {}
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(
                            f"[AltData] {market} OpenAPI 回傳狀態碼: {resp.status_code}"
                        )
                        return res
                    data = resp.json()
                    if not isinstance(data, list):
                        return res
                    for item in data:
                        row = _parse_tw_item(item)
                        if row is not None:
                            res[row[0]] = row[1]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[AltData] 抓取 {market} 月營收失敗: {e}")
                return res

        result = cast(
            dict[str, TwRevenueRow],
            await SingleFlightManager.run(
                f"alt_data:{market.lower()}_monthly_revenue", _fetch
            ),
        )
        if result:
            self._tw_cache[market] = (now_ts, result)
        return result

    async def fetch_twse_monthly_revenues(self) -> dict[str, TwRevenueRow]:
        """上市公司最新月營收年增率（代碼 -> 年增率與資料月份）。"""
        return await self._fetch_tw_monthly("TWSE", TWSE_MONTHLY_REVENUE_OPENAPI_URL)

    async def fetch_tpex_monthly_revenues(self) -> dict[str, TwRevenueRow]:
        """上櫃公司最新月營收年增率（代碼 -> 年增率與資料月份）。"""
        return await self._fetch_tw_monthly("TPEX", TPEX_MONTHLY_REVENUE_OPENAPI_URL)

    async def measure_tw_group(
        self, identifiers: Sequence[str], period: str, as_of: date
    ) -> tuple[float | None, dict[str, Any]]:
        """台股群組等權平均月營收年增率。

        `資料年月` 不屬於目標期別、或 `出表日期` 晚於 as_of 者排除（OpenAPI 只提供最新一個月，
        非 point-in-time，歷史期別通常不可得）。
        """
        codes = [c for c in (_tw_code(i) for i in identifiers) if c]
        if not codes:
            return None, {}
        twse = await self.fetch_twse_monthly_revenues()
        tpex = await self.fetch_tpex_monthly_revenues()
        members: dict[str, Any] = {}
        values: list[float] = []
        for code in codes:
            row = twse.get(code) or tpex.get(code)
            if row is None:
                members[code] = {"status": "查無月營收"}
                continue
            month_str = row.data_month.strftime("%Y-%m") if row.data_month else None
            if row.data_month is None or period_of_date(row.data_month) != period:
                members[code] = {
                    "status": f"資料年月 {month_str or '未知'} 不屬於 {period}"
                }
                continue
            if row.issued is not None and row.issued > as_of:
                members[code] = {"status": f"出表日期 {row.issued} 晚於 {as_of}"}
                continue
            values.append(row.yoy_pct)
            members[code] = {
                "yoy_pct": round(row.yoy_pct, 2),
                "data_month": month_str,
                "detail": "最新單月營收年增率",
            }
        info: dict[str, Any] = {
            "members": members,
            "eligible": len(codes),
            "covered": len(values),
        }
        required = max(1, math.ceil(len(codes) * MIN_GROUP_COVERAGE_RATIO))
        if len(values) < required:
            info["status"] = f"覆蓋 {len(values)}/{len(codes)} 低於門檻"
            return None, info
        growth = round(sum(values) / len(values), 2)
        info["growth"] = growth
        return growth, info

    # ========================================================================
    # 4. SEC XBRL companyconcept 指標（capex / rpo / dio / revenue）
    # ========================================================================

    @property
    def sec_client(self) -> Any | None:
        """目前已建立的 SEC 客戶端（不觸發建立）。"""
        return self._sec_client

    def attach_sec_client(self, client: Any) -> None:
        """注入外部 SEC 客戶端以共用 8 req/s 限速器與 CIK 快取；已有客戶端時不覆蓋。"""
        if self._sec_client is None:
            self._sec_client = client
            self._sec_unavailable_reason = None

    def _get_sec_client(self) -> Any | None:
        if self._sec_client is not None:
            return self._sec_client
        if self._sec_unavailable_reason is not None:
            return None
        try:
            from services.sec_edgar_client import SecEdgarClient

            self._sec_client = SecEdgarClient()
        except Exception as e:  # noqa: BLE001  (含 SecConfigError：未設定 SEC_USER_AGENT)
            self._sec_unavailable_reason = (
                "SEC 客戶端無法建立（未設定合規 SEC_USER_AGENT）"
            )
            logger.warning(f"[AltData] {self._sec_unavailable_reason}: {e}")
            return None
        return self._sec_client

    async def _concept_facts(self, cik: str, tag: str) -> list[XbrlFact] | None:
        """抓取單一 XBRL 概念並快取精簡事實（24 小時；404 亦快取為 None）。"""
        key = (cik, tag)
        now_ts = time.monotonic()
        cached = self._concept_cache.get(key)
        if cached is not None and (now_ts - cached[0]) < SEC_CONCEPT_CACHE_TTL_SECONDS:
            return cached[1]
        client = self._get_sec_client()
        if client is None:
            return None

        async def _fetch() -> list[XbrlFact] | None:
            payload = await client.fetch_company_concept(cik, tag)
            if payload is None:
                return None
            return parse_companyconcept_facts(payload)

        try:
            facts = cast(
                list[XbrlFact] | None,
                await SingleFlightManager.run(f"sec_concept:{cik}:{tag}", _fetch),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AltData] SEC XBRL {cik}/{tag} 抓取失敗: {e}")
            return None  # 失敗不快取，下輪重試
        if len(self._concept_cache) >= SEC_CONCEPT_CACHE_MAX_ENTRIES:
            oldest = min(self._concept_cache, key=lambda k: self._concept_cache[k][0])
            self._concept_cache.pop(oldest, None)
        self._concept_cache[key] = (now_ts, facts)
        return facts

    async def _first_tag_yoy(
        self,
        cik: str,
        tags: Sequence[str],
        compute: Callable[[list[XbrlFact]], MetricYoY | None],
    ) -> tuple[MetricYoY | None, str]:
        for tag in tags:
            facts = await self._concept_facts(cik, tag)
            if not facts:
                continue
            res = compute(facts)
            if res is not None:
                return res, tag
        return None, ""

    async def get_xbrl_metric_yoy(
        self, ticker: str, metric: str, period: str, as_of: date
    ) -> tuple[MetricYoY | None, str]:
        """單一公司 XBRL 指標於目標期別之同口徑年增率；回傳 (結果, 來源標籤或無法取得原因)。"""
        sym = ticker.strip().upper()
        if sym in NON_PUBLIC_ENTITIES:
            return None, "非公開實體，無 SEC 申報"
        client = self._get_sec_client()
        if client is None:
            return None, self._sec_unavailable_reason or "SEC 客戶端不可用"
        try:
            cik = await client.get_cik(sym)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AltData] SEC CIK 查詢失敗 ({sym}): {e}")
            return None, "SEC CIK 查詢失敗"
        if not cik:
            return None, "SEC 查無 CIK（可能為外國申報人或非美股）"
        cik_str = str(cik)

        res: MetricYoY | None
        tag: str
        if metric in XBRL_FLOW_TAGS:
            res, tag = await self._first_tag_yoy(
                cik_str,
                XBRL_FLOW_TAGS[metric],
                lambda f: flow_quarter_yoy(f, period, as_of),
            )
        elif metric == "rpo":
            res, tag = await self._first_tag_yoy(
                cik_str, XBRL_RPO_TAGS, lambda f: instant_yoy(f, period, as_of)
            )
        elif metric == "dio":
            res, tag = None, ""
            for inv_tag in XBRL_INVENTORY_TAGS:
                inventory = await self._concept_facts(cik_str, inv_tag)
                if not inventory:
                    continue
                inv_facts: list[XbrlFact] = inventory
                res, cogs_tag = await self._first_tag_yoy(
                    cik_str,
                    XBRL_COGS_TAGS,
                    lambda f: dio_yoy(inv_facts, f, period, as_of),
                )
                if res is not None:
                    tag = f"{inv_tag}/{cogs_tag}"
                    break
        else:
            return None, f"不支援的指標 {metric}"

        if res is None:
            return None, f"XBRL us-gaap 於 {as_of} 前無 {period} 與去年同季可比資料"
        return res, tag

    async def measure_us_group(
        self, items: Sequence[tuple[str, str]], period: str, as_of: date
    ) -> tuple[float | None, dict[str, Any]]:
        """美股群組（(代號, 指標)）等權平均年增率；非公開實體不計入覆蓋率分母。"""
        if not items:
            return None, {}

        async def _one(ticker: str, metric: str) -> tuple[MetricYoY | None, str]:
            try:
                return await self.get_xbrl_metric_yoy(ticker, metric, period, as_of)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[AltData] {ticker}:{metric} 計算失敗: {e}")
                return None, "計算失敗"

        outcomes = await asyncio.gather(*(_one(t, m) for t, m in items))
        members: dict[str, Any] = {}
        values: list[float] = []
        eligible = 0
        for (ticker, metric), (res, info) in zip(items, outcomes):
            key = f"{ticker}:{metric}"
            if ticker.upper() not in NON_PUBLIC_ENTITIES:
                eligible += 1
            if res is None:
                members[key] = {"status": info}
                continue
            values.append(res.yoy_pct)
            members[key] = {
                "yoy_pct": res.yoy_pct,
                "period_end": res.current_end.isoformat(),
                "prior_end": res.prior_end.isoformat(),
                "detail": res.detail,
                "source": f"SEC XBRL {info}",
            }
        info_out: dict[str, Any] = {
            "members": members,
            "eligible": eligible,
            "covered": len(values),
        }
        if eligible == 0:
            info_out["status"] = "成員皆為非公開實體，無 SEC 申報資料"
            return None, info_out
        required = max(1, math.ceil(eligible * MIN_GROUP_COVERAGE_RATIO))
        if len(values) < required:
            info_out["status"] = f"覆蓋 {len(values)}/{eligible} 低於門檻"
            return None, info_out
        growth = round(sum(values) / len(values), 2)
        info_out["growth"] = growth
        return growth, info_out

    # ========================================================================
    # 5. 產業鏈單側量測與檢驗流程驅動器
    # ========================================================================

    async def _measure_macro(
        self, ident: str, period: str, as_of: date
    ) -> tuple[WindowYoY | None, str]:
        if ident == "TSA:THROUGHPUT":
            return await self.get_tsa_period_yoy(period, as_of)
        return await self.get_fred_period_yoy(ident.split(":", 1)[1], period, as_of)

    async def measure_side(
        self,
        identifiers: Sequence[str],
        period: str,
        as_of: date,
        default_metric: str = "revenue",
    ) -> SideMeasurement:
        """量測產業鏈單側：總經高頻（TSA/FRED）、美股 XBRL、台股月營收分群計算後等權合併。"""
        macro_ids: list[str] = []
        us_items: list[tuple[str, str]] = []
        tw_items: list[str] = []
        for raw in identifiers:
            ident = raw.strip().upper()
            if ident == "TSA:THROUGHPUT" or ident.startswith("FRED:"):
                macro_ids.append(ident)
            elif ident.startswith(("TWSE:", "TPEX:")):
                tw_items.append(ident)
            elif ident.startswith("XBRL:"):
                parts = ident.split(":")
                if len(parts) >= 3:
                    us_items.append((parts[1], parts[2].lower()))
            elif ident:
                us_items.append((ident, default_metric))

        side = SideMeasurement(growth=None)
        group_values: list[float] = []

        if macro_ids:
            macro_vals: list[float] = []
            macro_info: dict[str, Any] = {}
            for ident in macro_ids:
                res, note = await self._measure_macro(ident, period, as_of)
                if res is None:
                    side.notes.append(note)
                    macro_info[ident] = {"status": note}
                    continue
                macro_vals.append(res.yoy_pct)
                macro_info[ident] = {
                    "yoy_pct": res.yoy_pct,
                    "window": f"{res.window_start}~{res.window_end}",
                    "observations": res.observations,
                    "detail": res.detail,
                }
            side.groups["macro"] = macro_info
            if macro_vals:
                group_values.append(round(sum(macro_vals) / len(macro_vals), 2))

        if us_items:
            us_growth, us_info = await self.measure_us_group(us_items, period, as_of)
            side.groups["us"] = us_info
            if us_growth is not None:
                group_values.append(us_growth)
            else:
                side.notes.append(f"美股{us_info.get('status', '無資料')}")

        if tw_items:
            tw_growth, tw_info = await self.measure_tw_group(tw_items, period, as_of)
            side.groups["tw"] = tw_info
            if tw_growth is not None:
                group_values.append(tw_growth)
            else:
                side.notes.append(f"台股{tw_info.get('status', '無資料')}")

        if group_values:
            side.growth = round(sum(group_values) / len(group_values), 2)
        return side

    async def run_channel_check(
        self,
        link: SupplyChainLink,
        as_of_period: str,
        as_of: date | None = None,
        persist: bool = False,
    ) -> ChannelCheckResult:
        """執行單一產業鏈於指定曆季之交叉驗證（兩端皆為同一期別之觀測值）。"""
        period = validate_period(as_of_period)
        ref = as_of if as_of is not None else _today_et()
        drivers = await self.measure_side(link.drivers, period, ref)
        followers = await self.measure_side(link.followers, period, ref)

        notes: list[str] = []
        if drivers.growth is None and drivers.note:
            notes.append(f"驅動端{drivers.note}")
        if followers.growth is None and followers.note:
            notes.append(f"跟隨端{followers.note}")

        result = evaluate_channel_check(
            link=link,
            as_of_period=period,
            driver_growth=drivers.growth,
            follower_growth=followers.growth,
            members_data={
                "as_of": ref.isoformat(),
                "driver_detail": drivers.groups,
                "follower_detail": followers.groups,
            },
            data_note="；".join(notes),
        )

        if persist:
            await save_channel_check_logs([result_to_log_record(result)])
        return result

    async def run_all_channel_checks(
        self,
        as_of_period: str,
        as_of: date | None = None,
        persist: bool = False,
    ) -> list[ChannelCheckResult]:
        """對全部 17 條產業鏈執行交叉驗證；單條失敗不影響其他鏈。"""
        period = validate_period(as_of_period)
        results: list[ChannelCheckResult] = []
        for link in SUPPLY_CHAIN_LINKS:
            try:
                res = await self.run_channel_check(
                    link=link, as_of_period=period, as_of=as_of, persist=False
                )
                results.append(res)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[AltDataService] 執行產業鏈檢驗 {link.link_key} 異常: {e}"
                )

        if persist and results:
            records = [result_to_log_record(r) for r in results]
            await save_channel_check_logs(records)

        return results


def _tw_code(identifier: str) -> str | None:
    code = identifier.strip().upper()
    if code.startswith(("TWSE:", "TPEX:")):
        code = code.split(":")[-1]
    return code if code.isdigit() else None


def _parse_tw_item(item: Any) -> tuple[str, TwRevenueRow] | None:
    """解析 OpenAPI 單列：公司代號、去年同月增減(%)、資料年月（民國）、出表日期（民國）。"""
    if not isinstance(item, dict):
        return None
    code = str(
        item.get("公司代號")
        or item.get("SecuritiesCompanyCode")
        or item.get("CompanyCode")
        or ""
    ).strip()
    yoy_raw = (
        item.get("營業收入-去年同月增減(%)")
        or item.get("營業收入-去年同月增減（％）")
        or item.get("去年同月增減(%)")
        or item.get("去年同月增減（％）")
    )
    if not code or yoy_raw is None:
        return None
    raw_str = str(yoy_raw).replace(",", "").replace("%", "").strip()
    if raw_str in _TW_EMPTY_TOKENS:
        return None
    try:
        yoy = float(raw_str)
    except ValueError:
        return None
    if math.isnan(yoy) or math.isinf(yoy):
        return None
    return code, TwRevenueRow(
        yoy_pct=yoy,
        data_month=parse_roc_year_month(item.get("資料年月")),
        issued=parse_roc_date(item.get("出表日期")),
    )


# 全域單例服務實例
alt_data_service = AltDataService()
