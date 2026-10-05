"""高頻實體與產業鏈替代數據服務 (Alternative Data & Supply Chain Service)。

整合三大免費公開數據源：
1. TSA 每日安檢客流 (TSA Throughput)
2. FRED 鐵路重工業貨運 (RAILFRTCARLOADSD11) 與全美汽車總銷量 (TOTALSA)
3. 台灣證券交易所 (TWSE) 與證券櫃檯買賣中心 (TPEx) 上市櫃月營收 OpenAPI

嚴格遵循：
- 100% 免費公開資料源，付費資料庫掛 Null 實作。
- SingleFlightManager 併發折疊與記憶體 TTL 快取，防範 1GB–2GB VPS 記憶體暴增。
- asyncio.Semaphore(3) 併發節流。
- 零交易執行不變量 (純顧問性分析)。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from database.financials import get_cached_financials
from database.fundamental_pipeline import save_channel_check_logs
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
from market_analysis.macro_signals import usable
from services.macro_signal_service import fetch_fred_series
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)

_ET_ZONE = ZoneInfo("America/New_York")

# TSA 每日安檢客流頁面
TSA_PASSENGER_VOLUMES_URL = "https://www.tsa.gov/travel/passenger-volumes"

# 台灣 TWSE 與 TPEx 月營收 OpenAPI 免費公開端點
TWSE_MONTHLY_REVENUE_OPENAPI_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_MONTHLY_REVENUE_OPENAPI_URL = (
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
)

# 快取存活時間 (秒)
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0


class AltDataService:
    """高頻實體與產業鏈替代數據服務客戶端。"""

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds
        self._semaphore = asyncio.Semaphore(3)

        # 記憶體 TTL 快取
        self._tsa_cache: tuple[float, float | None] | None = None
        self._twse_cache: tuple[float, dict[str, float]] | None = None
        self._tpex_cache: tuple[float, dict[str, float]] | None = None

    # ========================================================================
    # 1. TSA 每日安檢客流
    # ========================================================================

    async def fetch_tsa_throughput_growth(
        self, as_of: date | None = None
    ) -> float | None:
        """獲取 TSA 每日客流之 28 日均值年增率 (%)。"""
        now_ts = datetime.now(timezone.utc).timestamp()
        if (
            self._tsa_cache is not None
            and (now_ts - self._tsa_cache[0]) < DEFAULT_CACHE_TTL_SECONDS
        ):
            return self._tsa_cache[1]

        cache_key = "alt_data:tsa_throughput"

        async def _fetch() -> float | None:
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
                        resp = await client.get(TSA_PASSENGER_VOLUMES_URL)
                        if resp.status_code != 200:
                            logger.warning(
                                f"[AltData] TSA 頁面回應異常狀態碼: {resp.status_code}"
                            )
                            return None
                        html_text = resp.text

                    # 正則萃取最近 28 筆客流數字與去年同期數字
                    # 匹配表格列 <td>Date</td><td>Current Year</td><td>Prior Year</td> (相容帶 class/屬性標籤)
                    row_pattern = re.compile(
                        r"<tr[^>]*>\s*<td[^>]*>\s*(\d{1,2}/\d{1,2}/\d{4})\s*</td>\s*"
                        r"<td[^>]*>\s*([\d,]+)\s*</td>\s*"
                        r"<td[^>]*>\s*([\d,]+)\s*</td>",
                        re.IGNORECASE,
                    )
                    matches = row_pattern.findall(html_text)
                    if not matches:
                        logger.info("[AltData] TSA 表格格式未匹配，嘗試通用數字萃取")
                        return None

                    curr_vols: list[float] = []
                    prior_vols: list[float] = []
                    for match in matches[:28]:
                        try:
                            curr_v = float(match[1].replace(",", ""))
                            prior_v = float(match[2].replace(",", ""))
                            if curr_v > 0 and prior_v > 0:
                                curr_vols.append(curr_v)
                                prior_vols.append(prior_v)
                        except ValueError:
                            continue

                    if len(curr_vols) < 7:
                        return None

                    avg_curr = sum(curr_vols) / len(curr_vols)
                    avg_prior = sum(prior_vols) / len(prior_vols)
                    if avg_prior <= 0:
                        return None

                    growth_pct = round(((avg_curr - avg_prior) / avg_prior) * 100.0, 2)
                    return growth_pct
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[AltData] 拉取 TSA 客流數據失敗: {e}")
                    return None

        result = await SingleFlightManager.run(cache_key, _fetch)
        typed_result = cast(float | None, result)
        self._tsa_cache = (now_ts, typed_result)
        return typed_result

    # ========================================================================
    # 2. FRED 鐵路重工業貨運 & 全美汽車總銷量
    # ========================================================================

    async def fetch_rail_freight_growth(
        self, as_of: date | None = None
    ) -> float | None:
        """獲取全美鐵路貨運車皮裝載量 (FRED: RAILFRTCARLOADSD11) 月年增率 (%)。"""
        today_date = as_of if as_of is not None else datetime.now(_ET_ZONE).date()
        try:
            obs = await fetch_fred_series(
                "RAILFRTCARLOADSD11", today_date, kind="monthly_rail"
            )
            usable_obs = usable(obs, today_date)
            if len(usable_obs) < 2:
                return None

            latest = usable_obs[-1].value
            latest_date = usable_obs[-1].obs_date

            # 優先搜尋距離約 1 年 (330 ~ 400 天) 之對應觀測
            year_ago_val: float | None = None
            for o in reversed(usable_obs[:-1]):
                delta_days = (latest_date - o.obs_date).days
                if 330 <= delta_days <= 400:
                    year_ago_val = o.value
                    break

            if year_ago_val is None and len(usable_obs) >= 13:
                year_ago_val = usable_obs[-13].value

            if year_ago_val is not None and year_ago_val > 0:
                return round(((latest - year_ago_val) / year_ago_val) * 100.0, 2)

            # 若無法取得 1 年前觀測，退回最近一期之月增率 (MoM)
            prev = usable_obs[-2].value
            if prev > 0:
                return round(((latest - prev) / prev) * 100.0, 2)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AltData] 拉取 FRED 鐵路貨運數據失敗: {e}")
            return None

    async def fetch_auto_fleet_demand_growth(
        self, as_of: date | None = None
    ) -> float | None:
        """獲取全美汽車總體景氣銷量 (FRED: TOTALSA) 月年增率 (%)。"""
        today_date = as_of if as_of is not None else datetime.now(_ET_ZONE).date()
        try:
            obs = await fetch_fred_series("TOTALSA", today_date, kind="monthly_auto")
            usable_obs = usable(obs, today_date)
            if len(usable_obs) < 2:
                return None

            latest = usable_obs[-1].value
            latest_date = usable_obs[-1].obs_date

            # 優先搜尋距離約 1 年 (330 ~ 400 天) 之對應觀測
            year_ago_val: float | None = None
            for o in reversed(usable_obs[:-1]):
                delta_days = (latest_date - o.obs_date).days
                if 330 <= delta_days <= 400:
                    year_ago_val = o.value
                    break

            if year_ago_val is None and len(usable_obs) >= 13:
                year_ago_val = usable_obs[-13].value

            if year_ago_val is not None and year_ago_val > 0:
                return round(((latest - year_ago_val) / year_ago_val) * 100.0, 2)

            # 若無法取得 1 年前觀測，退回最近一期之月增率 (MoM)
            prev = usable_obs[-2].value
            if prev > 0:
                return round(((latest - prev) / prev) * 100.0, 2)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AltData] 拉取 FRED 汽車總銷量數據失敗: {e}")
            return None

    # ========================================================================
    # 3. 台灣上市櫃月營收 OpenAPI (TWSE / TPEx)
    # ========================================================================

    async def fetch_twse_monthly_revenues(self) -> dict[str, float]:
        """從台灣證交所 OpenAPI 拉取全市場上市月營收年增率字典 (代碼 -> YoY %)。"""
        now_ts = datetime.now(timezone.utc).timestamp()
        if (
            self._twse_cache is not None
            and (now_ts - self._twse_cache[0]) < DEFAULT_CACHE_TTL_SECONDS
        ):
            return self._twse_cache[1]

        cache_key = "alt_data:twse_monthly_revenue"

        async def _fetch() -> dict[str, float]:
            async with self._semaphore:
                res: dict[str, float] = {}
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.get(TWSE_MONTHLY_REVENUE_OPENAPI_URL)
                        if resp.status_code != 200:
                            logger.warning(
                                f"[AltData] TWSE OpenAPI 回傳狀態碼: {resp.status_code}"
                            )
                            return res
                        data = resp.json()
                        if not isinstance(data, list):
                            return res

                        for item in data:
                            if not isinstance(item, dict):
                                continue
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
                            if code and yoy_raw is not None:
                                raw_str = (
                                    str(yoy_raw)
                                    .replace(",", "")
                                    .replace("%", "")
                                    .strip()
                                )
                                if raw_str and raw_str not in (
                                    "--",
                                    "-",
                                    "N/A",
                                    "null",
                                    "None",
                                    "不適用",
                                ):
                                    try:
                                        res[code] = float(raw_str)
                                    except ValueError:
                                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[AltData] 抓取 TWSE 月營收失敗: {e}")
                return res

        result = await SingleFlightManager.run(cache_key, _fetch)
        typed_twse = cast(dict[str, float], result)
        self._twse_cache = (now_ts, typed_twse)
        return typed_twse

    async def fetch_tpex_monthly_revenues(self) -> dict[str, float]:
        """從櫃買中心 OpenAPI 拉取全市場上櫃月營收年增率字典 (代碼 -> YoY %)。"""
        now_ts = datetime.now(timezone.utc).timestamp()
        if (
            self._tpex_cache is not None
            and (now_ts - self._tpex_cache[0]) < DEFAULT_CACHE_TTL_SECONDS
        ):
            return self._tpex_cache[1]

        cache_key = "alt_data:tpex_monthly_revenue"

        async def _fetch() -> dict[str, float]:
            async with self._semaphore:
                res: dict[str, float] = {}
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.get(TPEX_MONTHLY_REVENUE_OPENAPI_URL)
                        if resp.status_code != 200:
                            logger.warning(
                                f"[AltData] TPEx OpenAPI 回傳狀態碼: {resp.status_code}"
                            )
                            return res
                        data = resp.json()
                        if not isinstance(data, list):
                            return res

                        for item in data:
                            if not isinstance(item, dict):
                                continue
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
                            if code and yoy_raw is not None:
                                raw_str = (
                                    str(yoy_raw)
                                    .replace(",", "")
                                    .replace("%", "")
                                    .strip()
                                )
                                if raw_str and raw_str not in (
                                    "--",
                                    "-",
                                    "N/A",
                                    "null",
                                    "None",
                                    "不適用",
                                ):
                                    try:
                                        res[code] = float(raw_str)
                                    except ValueError:
                                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[AltData] 抓取 TPEx 月營收失敗: {e}")
                return res

        result = await SingleFlightManager.run(cache_key, _fetch)
        typed_tpex = cast(dict[str, float], result)
        self._tpex_cache = (now_ts, typed_tpex)
        return typed_tpex

    async def get_tw_supply_chain_growth(
        self, identifiers: Sequence[str]
    ) -> float | None:
        """計算指定台股供應鏈代碼組合之平均營收年增率 (%)。"""
        # 提煉台股純數字代號
        target_codes: list[str] = []
        for ident in identifiers:
            code = ident.strip().upper()
            if code.startswith(("TWSE:", "TPEX:")):
                code = code.split(":")[-1]
            if code.isdigit():
                target_codes.append(code)

        if not target_codes:
            return None

        twse_data = await self.fetch_twse_monthly_revenues()
        tpex_data = await self.fetch_tpex_monthly_revenues()

        valid_growths: list[float] = []
        for code in target_codes:
            if code in twse_data:
                valid_growths.append(twse_data[code])
            elif code in tpex_data:
                valid_growths.append(tpex_data[code])

        if not valid_growths:
            return None

        avg_growth = sum(valid_growths) / len(valid_growths)
        return round(avg_growth, 2)

    # ========================================================================
    # 4. 財務指標與 XBRL 資本支出增長率
    # ========================================================================

    def get_xbrl_metric_growth(
        self, ticker: str, metric: str = "capex"
    ) -> float | None:
        """從本地財務快取取得標的之資本支出或營收年增率。"""
        sym = ticker.strip().upper()
        # 若為 SpaceX 等未公開發行實體，回傳 None 觸發優雅降級
        if sym in ("SPCX", "SPACEX", "SPACEXSI"):
            return None

        cached = get_cached_financials(sym)
        if not cached:
            return None

        # 嘗試從快取中讀取指標增長率（相容單元測試 mock 與 Finnhub 實際欄位）
        candidate_keys: list[str] = [f"{metric}_growth_yoy"]
        if metric == "capex":
            candidate_keys.extend(
                ["capexGrowthTTMYoy", "capexGrowthAnnual", "capex_growth"]
            )
        elif metric == "revenue":
            candidate_keys.extend(
                ["revenueGrowthTTMYoy", "revenueGrowthAnnual", "revenue_growth_ttm"]
            )
        elif metric == "dio":
            candidate_keys.extend(["dio_growth", "inventoryTurnoverTTMYoy"])
        elif metric == "rpo":
            candidate_keys.extend(["rpoGrowthYoy", "rpo_growth"])

        for k in candidate_keys:
            if k in cached and cached[k] is not None:
                try:
                    return round(float(cached[k]), 2)
                except (ValueError, TypeError):
                    pass

        if metric == "revenue" and "revenue_growth" in cached:
            try:
                raw_rev = float(cached["revenue_growth"])
                if -1.0 <= raw_rev <= 1.0:
                    return round(raw_rev * 100.0, 2)
                return round(raw_rev, 2)
            except (ValueError, TypeError):
                pass

        return None

    # ========================================================================
    # 5. 產業鏈因果與高頻檢驗流程驅動器
    # ========================================================================

    async def get_driver_growth_for_link(
        self, link: SupplyChainLink, as_of: date | None = None
    ) -> float | None:
        """解析並獲取該產業鏈驅動端綜合增長率。"""
        # 特殊單一實體宏觀指標判定
        if link.link_key == "AIR_TRAVEL":
            return await self.fetch_tsa_throughput_growth(as_of)

        if link.link_key == "RAIL_FREIGHT":
            return await self.fetch_rail_freight_growth(as_of)

        if link.link_key == "ADV_AUTO_FLEET_DEMAND":
            return await self.fetch_auto_fleet_demand_growth(as_of)

        # 台股供應鏈先行 (NOWCAST)
        has_tw_drivers = any(d.startswith(("TWSE:", "TPEX:")) for d in link.drivers)
        if has_tw_drivers:
            return await self.get_tw_supply_chain_growth(link.drivers)

        # XBRL 驅動端 (Capex / Revenue / RPO)
        growths: list[float] = []
        for d in link.drivers:
            if d.startswith("XBRL:"):
                parts = d.split(":")
                if len(parts) >= 3:
                    ticker, metric = parts[1], parts[2]
                    g = self.get_xbrl_metric_growth(ticker, metric)
                    if g is not None:
                        growths.append(g)

        if growths:
            return round(sum(growths) / len(growths), 2)
        return None

    async def get_follower_growth_for_link(
        self, link: SupplyChainLink, as_of: date | None = None
    ) -> float | None:
        """解析並獲取該產業鏈跟隨端綜合增長率。"""
        # 台股供應鏈作為跟隨端
        has_tw_followers = any(f.startswith(("TWSE:", "TPEX:")) for f in link.followers)
        if has_tw_followers:
            return await self.get_tw_supply_chain_growth(link.followers)

        # 美股跟隨端標的財報營收增長
        growths: list[float] = []
        for f in link.followers:
            g = self.get_xbrl_metric_growth(f, metric="revenue")
            if g is not None:
                growths.append(g)

        if growths:
            return round(sum(growths) / len(growths), 2)
        return None

    async def run_channel_check(
        self,
        link: SupplyChainLink,
        as_of_period: str,
        as_of: date | None = None,
        persist: bool = False,
    ) -> ChannelCheckResult:
        """執行單一產業鏈之交叉驗證。"""
        driver_growth = await self.get_driver_growth_for_link(link, as_of)
        follower_growth = await self.get_follower_growth_for_link(link, as_of)

        result = evaluate_channel_check(
            link=link,
            as_of_period=as_of_period,
            driver_growth=driver_growth,
            follower_growth=follower_growth,
        )

        if persist:
            record = result_to_log_record(result)
            await save_channel_check_logs([record])

        return result

    async def run_all_channel_checks(
        self,
        as_of_period: str,
        as_of: date | None = None,
        persist: bool = False,
    ) -> list[ChannelCheckResult]:
        """對所有 17 條產業鏈執行全景交叉驗證。"""
        results: list[ChannelCheckResult] = []
        for link in SUPPLY_CHAIN_LINKS:
            try:
                res = await self.run_channel_check(
                    link=link,
                    as_of_period=as_of_period,
                    as_of=as_of,
                    persist=False,
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


# 全域單例服務實例
alt_data_service = AltDataService()
