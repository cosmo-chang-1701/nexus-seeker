"""高頻實體與產業鏈替代數據服務 (alt_data_service) 單元測試。"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from market_analysis.fundamental_pipeline.supply_chain_map import (
    LINK_SPACE_STARLINK_TW_NOWCAST,
)
from market_analysis.macro_signals import Observation
from services.alt_data_service import AltDataService


@pytest.fixture
def service() -> AltDataService:
    return AltDataService(timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_fetch_tsa_throughput_growth_success(service: AltDataService) -> None:
    """測試 TSA 每日客流 HTML 解析與 28 日均值增長率精算。"""
    sample_html = """
    <html>
      <table>
        <tr><th>Date</th><th>2026</th><th>2025</th></tr>
        <tr><td>10/05/2026</td><td>2,400,000</td><td>2,000,000</td></tr>
        <tr><td>10/04/2026</td><td>2,300,000</td><td>2,100,000</td></tr>
        <tr><td>10/03/2026</td><td>2,200,000</td><td>2,000,000</td></tr>
        <tr><td>10/02/2026</td><td>2,500,000</td><td>2,200,000</td></tr>
        <tr><td>10/01/2026</td><td>2,450,000</td><td>2,150,000</td></tr>
        <tr><td>09/30/2026</td><td>2,350,000</td><td>2,050,000</td></tr>
        <tr><td>09/29/2026</td><td>2,400,000</td><td>2,100,000</td></tr>
      </table>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        growth = await service.fetch_tsa_throughput_growth()
        assert growth is not None
        assert growth > 0.0
        # 驗證快取命中 (第二次呼叫不重複發起 GET 請求)
        growth_cached = await service.fetch_tsa_throughput_growth()
        assert growth_cached == growth
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_tsa_throughput_network_error(service: AltDataService) -> None:
    """測試 TSA 網絡異常時優雅降級回傳 None。"""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("Connection Timeout")
        growth = await service.fetch_tsa_throughput_growth()
        assert growth is None


@pytest.mark.asyncio
async def test_fetch_rail_freight_growth(service: AltDataService) -> None:
    """測試 FRED 鐵路車皮裝載量年增率精算。"""
    today = date(2026, 10, 5)
    # 建立 14 個月的月度觀測數據
    obs: list[Observation] = []
    base_val = 100.0
    for i in range(14):
        obs_date = date(2025, 8, 1) + (date(2025, 9, 1) - date(2025, 8, 1)) * i
        obs.append(
            Observation(
                obs_date=obs_date,
                value=base_val + i * 2.0,
                available_date=date(2025, 8, 1),
            )
        )

    with patch(
        "services.alt_data_service.fetch_fred_series", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = obs
        growth = await service.fetch_rail_freight_growth(today)
        assert growth is not None
        assert growth > 0.0


@pytest.mark.asyncio
async def test_twse_and_tpex_revenue_parsing(service: AltDataService) -> None:
    """測試台灣 TWSE 與 TPEx 月營收 OpenAPI 資料解析。"""
    twse_sample = [
        {
            "出表日期": "115/03/10",
            "資料年月": "115/02",
            "公司代號": "2313",
            "公司名稱": "華通",
            "營業收入-去年同月增減(%)": "12.50",
        },
        {
            "出表日期": "115/03/10",
            "資料年月": "115/02",
            "公司代號": "6285",
            "公司名稱": "啟碁",
            "營業收入-去年同月增減(%)": "8.30",
        },
    ]

    tpex_sample = [
        {
            "出表日期": "115/03/10",
            "資料年月": "115/02",
            "公司代號": "3491",
            "公司名稱": "昇達科",
            "營業收入-去年同月增減(%)": "24.60",
        }
    ]

    async def mock_get(url: str, *args: Any, **kwargs: Any) -> Any:
        resp = MagicMock()
        resp.status_code = 200
        if "openapi.twse.com.tw" in url:
            resp.json.return_value = twse_sample
        else:
            resp.json.return_value = tpex_sample
        return resp

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as patched_get:
        patched_get.side_effect = mock_get

        # 測試台股叢集等權平均增長
        # 2313 (+12.5%), 6285 (+8.3%), 3491 (+24.6%) -> avg = (12.5 + 8.3 + 24.6) / 3 = 15.13%
        cluster_growth = await service.get_tw_supply_chain_growth(
            ["TWSE:2313", "TWSE:6285", "TPEX:3491"]
        )
        assert cluster_growth is not None
        assert cluster_growth == 15.13

        # 不存在的代號回傳 None
        unknown_growth = await service.get_tw_supply_chain_growth(["TWSE:99999"])
        assert unknown_growth is None


def test_get_xbrl_metric_growth_with_cached_data(
    service: AltDataService,
) -> None:
    """測試從本地財務快取讀取資本支出或營收增長。"""
    with patch("services.alt_data_service.get_cached_financials") as mock_financials:
        mock_financials.return_value = {
            "capex_growth_yoy": "28.5",
            "revenue_growth": "0.15",
        }

        capex_growth = service.get_xbrl_metric_growth("MSFT", "capex")
        assert capex_growth == 28.5

        rev_growth = service.get_xbrl_metric_growth("MSFT", "revenue")
        assert rev_growth == 15.0

        # SpaceX (SPCX) 非公開實體直接優雅回傳 None
        assert service.get_xbrl_metric_growth("SPCX", "capex") is None


@pytest.mark.asyncio
async def test_run_channel_check_pipeline(service: AltDataService) -> None:
    """測試端到端單一產業鏈交叉驗證執行。"""
    with patch.object(
        service, "get_driver_growth_for_link", new_callable=AsyncMock
    ) as mock_driver, patch.object(
        service, "get_follower_growth_for_link", new_callable=AsyncMock
    ) as mock_follower:
        mock_driver.return_value = 8.1
        mock_follower.return_value = None

        result = await service.run_channel_check(
            LINK_SPACE_STARLINK_TW_NOWCAST, as_of_period="2026-09"
        )
        assert result.link_key == "SPACE_STARLINK_TW_NOWCAST"
        assert result.nowcast_direction == "NOWCAST_UP"
        assert result.verdict == "CONFIRM"


@pytest.mark.asyncio
async def test_run_all_channel_checks(service: AltDataService) -> None:
    """測試全量 17 條產業鏈執行。"""
    with patch.object(
        service, "get_driver_growth_for_link", new_callable=AsyncMock
    ) as mock_driver, patch.object(
        service, "get_follower_growth_for_link", new_callable=AsyncMock
    ) as mock_follower:
        mock_driver.return_value = 10.0
        mock_follower.return_value = 12.0

        results = await service.run_all_channel_checks(
            as_of_period="2026-Q2", persist=False
        )
        assert len(results) == 17
        for r in results:
            assert r.verdict == "CONFIRM"
