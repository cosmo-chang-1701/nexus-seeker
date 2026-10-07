"""高頻實體與產業鏈替代數據服務 (alt_data_service) 單元測試。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from market_analysis.fundamental_pipeline.models import SupplyChainLink
from market_analysis.fundamental_pipeline.supply_chain_map import (
    LINK_AI_CAPEX,
    LINK_BRAND_RETAIL_INVENTORY,
    LINK_SPACE_STARLINK_TW_NOWCAST,
    SUPPLY_CHAIN_LINKS,
)
from market_analysis.macro_signals import Observation
from services.alt_data_service import AltDataService, SideMeasurement

_FIX = Path(__file__).parent / "fixtures" / "alt_data"
_AS_OF = date(2026, 10, 7)


def _load(name: str) -> Any:
    return json.loads((_FIX / name).read_text())


class _FakeSec:
    """以真實 companyconcept fixture 模擬 SecEdgarClient。"""

    def __init__(self, concepts: dict[tuple[str, str], Any]) -> None:
        self._concepts = concepts
        self.calls: list[tuple[str, str]] = []

    async def get_cik(self, symbol: str) -> str | None:
        return {
            "MSFT": "789019",
            "AMZN": "1018724",
            "WMT": "104169",
            "LMT": "936468",
        }.get(symbol)

    async def fetch_company_concept(self, cik: str, tag: str) -> Any:
        self.calls.append((cik, tag))
        return self._concepts.get((cik, tag))


def _fake_sec() -> _FakeSec:
    return _FakeSec(
        {
            ("789019", "PaymentsToAcquirePropertyPlantAndEquipment"): _load(
                "sec_concept_msft_PaymentsToAcquirePropertyPlantAndEquipment.json"
            ),
            ("1018724", "PaymentsToAcquirePropertyPlantAndEquipment"): _load(
                "sec_concept_amzn_PaymentsToAcquirePropertyPlantAndEquipment.json"
            ),
            ("1018724", "PaymentsToAcquireProductiveAssets"): _load(
                "sec_concept_amzn_PaymentsToAcquireProductiveAssets.json"
            ),
            ("104169", "InventoryNet"): _load("sec_concept_wmt_InventoryNet.json"),
            ("104169", "CostOfRevenue"): _load("sec_concept_wmt_CostOfRevenue.json"),
            ("936468", "RevenueRemainingPerformanceObligation"): _load(
                "sec_concept_lmt_RevenueRemainingPerformanceObligation.json"
            ),
            ("789019", "RevenueFromContractWithCustomerExcludingAssessedTax"): _load(
                "sec_concept_msft_RevenueFromContractWithCustomerExcludingAssessedTax.json"
            ),
        }
    )


@pytest.fixture
def service() -> AltDataService:
    return AltDataService(timeout_seconds=5.0, sec_client=_fake_sec())


# ---------------------------------------------------------------------------
# SEC XBRL 指標（capex / rpo / dio / revenue）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xbrl_capex_falls_back_to_productive_assets_tag(
    service: AltDataService,
) -> None:
    res, tag = await service.get_xbrl_metric_yoy("AMZN", "capex", "2026-Q2", _AS_OF)
    assert res is not None
    assert tag == "PaymentsToAcquireProductiveAssets"
    assert res.yoy_pct == pytest.approx(68.44, abs=0.01)


@pytest.mark.asyncio
async def test_xbrl_rpo_and_dio_are_real_sources(service: AltDataService) -> None:
    rpo, rpo_tag = await service.get_xbrl_metric_yoy("LMT", "rpo", "2026-Q2", _AS_OF)
    assert rpo is not None and rpo_tag == "RevenueRemainingPerformanceObligation"
    dio, dio_tag = await service.get_xbrl_metric_yoy("WMT", "dio", "2026-Q2", _AS_OF)
    assert dio is not None
    assert dio_tag == "InventoryNet/CostOfRevenue"
    assert dio.yoy_pct == pytest.approx(2.07, abs=0.01)


@pytest.mark.asyncio
async def test_xbrl_reasons_for_unavailable(service: AltDataService) -> None:
    res, reason = await service.get_xbrl_metric_yoy("SPCX", "capex", "2026-Q2", _AS_OF)
    assert res is None and "非公開" in reason
    res, reason = await service.get_xbrl_metric_yoy("ZZZZ", "capex", "2026-Q2", _AS_OF)
    assert res is None and "CIK" in reason
    # 當季 10-Q 尚未申報（2026-Q3 於 10/7 尚無資料）
    res, reason = await service.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q3", _AS_OF)
    assert res is None and "2026-Q3" in reason


@pytest.mark.asyncio
async def test_xbrl_concepts_are_cached(service: AltDataService) -> None:
    sec = service._sec_client
    assert isinstance(sec, _FakeSec)
    await service.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q2", _AS_OF)
    await service.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q1", _AS_OF)
    assert (
        sec.calls.count(("789019", "PaymentsToAcquirePropertyPlantAndEquipment")) == 1
    )


@pytest.mark.asyncio
async def test_sec_unavailable_without_user_agent() -> None:
    svc = AltDataService()
    with patch("config.SEC_USER_AGENT", ""):
        res, reason = await svc.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q2", _AS_OF)
    assert res is None
    assert "SEC_USER_AGENT" in reason


@pytest.mark.asyncio
async def test_attach_sec_client_shares_external_client() -> None:
    """注入外部 SEC 客戶端後沿用之（共用限速器）；已有客戶端時不覆蓋。"""
    svc = AltDataService()
    with patch("config.SEC_USER_AGENT", ""):
        res, _ = await svc.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q2", _AS_OF)
    assert res is None and svc.sec_client is None

    shared = _fake_sec()
    svc.attach_sec_client(shared)
    assert svc.sec_client is shared
    res, _ = await svc.get_xbrl_metric_yoy("MSFT", "capex", "2026-Q1", _AS_OF)
    assert res is not None

    svc.attach_sec_client(_fake_sec())
    assert svc.sec_client is shared


def test_no_finnhub_or_mock_only_keys_remain() -> None:
    """Finnhub /stock/metric 沒有 capex YoY / RPO / DIO 欄位：不得再讀取假欄位。"""
    src = (Path(__file__).parents[2] / "services" / "alt_data_service.py").read_text()
    for fake in (
        "capexGrowthTTMYoy",
        "capexGrowthAnnual",
        "rpoGrowthYoy",
        "inventoryTurnoverTTMYoy",
        "capex_growth_yoy",
        "get_cached_financials",
    ):
        assert fake not in src


# ---------------------------------------------------------------------------
# 台股月營收（真實 OpenAPI 片段）
# ---------------------------------------------------------------------------


def _tw_mock_get() -> AsyncMock:
    twse = _load("twse_t187ap05_L_snippet.json")
    tpex = _load("tpex_t187ap05_O_snippet.json")

    async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = twse if "openapi.twse.com.tw" in url else tpex
        return resp

    return AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_tw_group_uses_data_month_for_period_alignment(
    service: AltDataService,
) -> None:
    """資料年月 115/08 (2026-08) 屬於 2026-Q3：可用；目標 2026-Q2 時一律排除。"""
    with patch("httpx.AsyncClient.get", new=_tw_mock_get()):
        g_q3, info_q3 = await service.measure_tw_group(
            ["TWSE:2313", "TWSE:6285", "TPEX:3491"], "2026-Q3", _AS_OF
        )
        g_q2, info_q2 = await service.measure_tw_group(
            ["TWSE:2313", "TWSE:6285", "TPEX:3491"], "2026-Q2", _AS_OF
        )
    assert g_q3 is not None
    assert info_q3["covered"] == 3
    assert info_q3["members"]["3491"]["data_month"] == "2026-08"
    assert g_q2 is None
    assert "不屬於 2026-Q2" in info_q2["members"]["2313"]["status"]


@pytest.mark.asyncio
async def test_tw_group_rejects_snapshot_issued_after_as_of(
    service: AltDataService,
) -> None:
    """出表日期 2026-09-17 晚於 as_of 2026-09-10 → 前視排除。"""
    with patch("httpx.AsyncClient.get", new=_tw_mock_get()):
        g, info = await service.measure_tw_group(
            ["TWSE:2313"], "2026-Q3", date(2026, 9, 10)
        )
    assert g is None
    assert "晚於" in info["members"]["2313"]["status"]


# ---------------------------------------------------------------------------
# TSA（真實兩欄頁面：當年度頁 + 歷年頁）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tsa_fetches_current_and_prior_year_pages(
    service: AltDataService,
) -> None:
    pages = {
        "https://www.tsa.gov/travel/passenger-volumes": (
            _FIX / "tsa_passenger_volumes_current_2026.html"
        ).read_text(),
        "https://www.tsa.gov/travel/passenger-volumes/2025": (
            _FIX / "tsa_passenger_volumes_2025.html"
        ).read_text(),
    }
    requested: list[str] = []

    async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
        requested.append(url)
        resp = MagicMock()
        resp.status_code = 200 if url in pages else 404
        resp.text = pages.get(url, "")
        return resp

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        res, note = await service.get_tsa_period_yoy("2026-Q3", _AS_OF)
        # 第二次命中快取，不再發出請求
        n_requests = len(requested)
        await service.get_tsa_period_yoy("2026-Q3", _AS_OF)
    assert res is not None, note
    assert res.yoy_pct == pytest.approx(-2.77, abs=0.01)
    assert set(requested) == set(pages)
    assert len(requested) == n_requests


@pytest.mark.asyncio
async def test_tsa_blocked_returns_none_with_reason(service: AltDataService) -> None:
    resp = MagicMock()
    resp.status_code = 403
    resp.text = "Access Denied"
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        res, note = await service.get_tsa_period_yoy("2026-Q3", _AS_OF)
    assert res is None
    assert "TSA" in note


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fred_period_yoy_no_mom_fallback(service: AltDataService) -> None:
    obs = [
        Observation(date(2026, 7, 1), 110.0, date(2026, 9, 1)),
        Observation(date(2026, 8, 1), 112.0, date(2026, 9, 30)),
    ]
    with patch(
        "services.alt_data_service.fetch_fred_series", new=AsyncMock(return_value=obs)
    ):
        res, note = await service.get_fred_period_yoy(
            "RAILFRTCARLOADSD11", "2026-Q3", _AS_OF
        )
    assert res is None
    assert "去年同月" in note


@pytest.mark.asyncio
async def test_fred_period_yoy_respects_available_date(service: AltDataService) -> None:
    obs = [
        Observation(date(2025, 7, 1), 100.0, date(2025, 9, 1)),
        Observation(date(2025, 8, 1), 100.0, date(2025, 9, 30)),
        Observation(date(2026, 7, 1), 110.0, date(2026, 9, 1)),
        # 8 月數據 9/30 才公布：as_of 9/15 時不可用
        Observation(date(2026, 8, 1), 200.0, date(2026, 9, 30)),
    ]
    with patch(
        "services.alt_data_service.fetch_fred_series", new=AsyncMock(return_value=obs)
    ):
        res, _ = await service.get_fred_period_yoy(
            "TOTALSA", "2026-Q3", date(2026, 9, 15)
        )
    assert res is not None
    assert res.observations == 1
    assert res.yoy_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 單側量測：美股與台股分群後等權合併
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measure_side_combines_us_and_tw_groups(service: AltDataService) -> None:
    """跟隨端同時有美股與台股時，兩群分別計算後等權合併，美股不再被整批丟棄。"""

    async def _us(items: Any, period: str, as_of: date) -> tuple[float, dict[str, Any]]:
        return 20.0, {"covered": 4}

    async def _tw(ids: Any, period: str, as_of: date) -> tuple[float, dict[str, Any]]:
        return 10.0, {"covered": 4}

    with (
        patch.object(service, "measure_us_group", new=AsyncMock(side_effect=_us)) as us,
        patch.object(service, "measure_tw_group", new=AsyncMock(side_effect=_tw)) as tw,
    ):
        side = await service.measure_side(LINK_AI_CAPEX.followers, "2026-Q2", _AS_OF)
    assert side.growth == 15.0
    us_items = us.await_args_list[0].args[0]
    assert ("NVDA", "revenue") in us_items and ("VRT", "revenue") in us_items
    assert tw.await_args_list[0].args[0] == [
        "TWSE:2382",
        "TWSE:6669",
        "TWSE:2345",
        "TWSE:2317",
    ]


@pytest.mark.asyncio
async def test_us_group_coverage_threshold_excludes_non_public(
    service: AltDataService,
) -> None:
    """AI_CAPEX 驅動端 MSFT/AMZN 有資料、GOOGL/META/ORCL 查無 → 2/5 低於半數門檻；SPCX 不計入分母。"""
    items = [(d.split(":")[1], d.split(":")[2]) for d in LINK_AI_CAPEX.drivers]
    growth, info = await service.measure_us_group(items, "2026-Q2", _AS_OF)
    assert info["eligible"] == 5
    assert info["covered"] == 2
    assert growth is None
    assert "低於門檻" in info["status"]
    growth2, info2 = await service.measure_us_group(items[:3], "2026-Q2", _AS_OF)
    assert growth2 == pytest.approx(round((109.63 + 68.44) / 2, 2), abs=0.02)


# ---------------------------------------------------------------------------
# 產業鏈端到端
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_channel_check_rejects_bad_period(service: AltDataService) -> None:
    with pytest.raises(ValueError):
        await service.run_channel_check(
            LINK_SPACE_STARLINK_TW_NOWCAST, as_of_period="2026-09"
        )


@pytest.mark.asyncio
async def test_run_channel_check_insufficient_carries_reason(
    service: AltDataService,
) -> None:
    async def _side(
        ids: Any, period: str, as_of: date, default_metric: str = "revenue"
    ) -> SideMeasurement:
        if ids is LINK_AI_CAPEX.drivers:
            return SideMeasurement(growth=None, notes=["美股覆蓋 1/5 低於門檻"])
        return SideMeasurement(growth=12.0)

    with patch.object(service, "measure_side", new=AsyncMock(side_effect=_side)):
        res = await service.run_channel_check(LINK_AI_CAPEX, "2026-Q3", as_of=_AS_OF)
    assert res.verdict == "INSUFFICIENT"
    assert "驅動端美股覆蓋 1/5 低於門檻" in res.summary_text
    assert res.members["as_of"] == "2026-10-07"


@pytest.mark.asyncio
async def test_brand_retail_inventory_uses_inverse_polarity(
    service: AltDataService,
) -> None:
    """零售 DIO 年增 +10%（渠道堵塞）、品牌廠營收 -8%：反向關係下判定共振確認。"""
    assert LINK_BRAND_RETAIL_INVENTORY.polarity == -1

    async def _side(
        ids: Any, period: str, as_of: date, default_metric: str = "revenue"
    ) -> SideMeasurement:
        return SideMeasurement(
            growth=10.0 if ids is LINK_BRAND_RETAIL_INVENTORY.drivers else -8.0
        )

    with patch.object(service, "measure_side", new=AsyncMock(side_effect=_side)):
        res = await service.run_channel_check(
            LINK_BRAND_RETAIL_INVENTORY, "2026-Q2", as_of=_AS_OF
        )
    assert res.verdict == "CONFIRM"
    assert res.driver_growth == 10.0  # 保存原始量測值
    assert res.divergence_pp == pytest.approx(2.0)
    assert "反向" in res.summary_text


@pytest.mark.asyncio
async def test_run_all_isolates_single_link_failure(service: AltDataService) -> None:
    """單條鏈例外不影響其餘 16 條，且只批次寫入一次。"""
    calls: list[str] = []

    async def _check(
        link: SupplyChainLink,
        as_of_period: str,
        as_of: date | None = None,
        persist: bool = False,
    ) -> Any:
        calls.append(link.link_key)
        if link.link_key == "AI_CAPEX":
            raise RuntimeError("boom")
        from market_analysis.fundamental_pipeline.channel_check import (
            evaluate_channel_check,
        )

        return evaluate_channel_check(link, as_of_period, 5.0, 4.0)

    save = AsyncMock()
    with (
        patch.object(service, "run_channel_check", new=AsyncMock(side_effect=_check)),
        patch("services.alt_data_service.save_channel_check_logs", new=save),
    ):
        results = await service.run_all_channel_checks(
            "2026-Q2", as_of=_AS_OF, persist=True
        )
    assert len(calls) == len(SUPPLY_CHAIN_LINKS) == 17
    assert len(results) == 16
    save.assert_awaited_once()
    assert len(save.await_args_list[0].args[0]) == 16
