"""Unit tests for StockAliasMatrix (4-Tier Auto-Populating & Resolution Matrix)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from market_analysis.stock_alias_matrix import StockAliasMatrix, STOCK_ALIAS_MAP


def test_static_alias_mapping() -> None:
    """驗證靜態映射表涵蓋 Mag 7、AI 半導體、雲端資安與宏觀 ETF。"""
    assert "NVDA" in STOCK_ALIAS_MAP
    assert "nvidia" in STOCK_ALIAS_MAP["NVDA"]
    assert "jensen huang" in STOCK_ALIAS_MAP["NVDA"]

    assert "GOOGL" in STOCK_ALIAS_MAP
    assert "google" in STOCK_ALIAS_MAP["GOOGL"]
    assert "alphabet" in STOCK_ALIAS_MAP["GOOGL"]

    assert "SMCI" in STOCK_ALIAS_MAP
    assert "super micro" in STOCK_ALIAS_MAP["SMCI"]
    assert "supermicro" in STOCK_ALIAS_MAP["SMCI"]

    assert "SPY" in STOCK_ALIAS_MAP
    assert "s&p 500" in STOCK_ALIAS_MAP["SPY"]
    assert "rate cut" in STOCK_ALIAS_MAP["SPY"]


def test_clean_company_name() -> None:
    """驗證公司名稱智慧清洗演算法。"""
    # 移除標準後綴
    assert StockAliasMatrix.clean_company_name("Apple Inc.") == "Apple"
    assert StockAliasMatrix.clean_company_name("Microsoft Corporation") == "Microsoft"
    assert StockAliasMatrix.clean_company_name("Tesla, Inc.") == "Tesla"
    assert (
        StockAliasMatrix.clean_company_name("Palantir Technologies Inc.")
        == "Palantir Technologies"
    )

    # 通用字前綴保護 (防範 Super/Taiwan/American 誤切)
    assert (
        StockAliasMatrix.clean_company_name("Super Micro Computer, Inc.")
        == "Super Micro"
    )
    assert (
        StockAliasMatrix.clean_company_name(
            "Taiwan Semiconductor Manufacturing Company Limited"
        )
        == "Taiwan Semiconductor"
    )
    assert (
        StockAliasMatrix.clean_company_name("American Express Company")
        == "American Express"
    )
    assert StockAliasMatrix.clean_company_name("First Solar, Inc.") == "First Solar"

    # 新興成長股
    assert StockAliasMatrix.clean_company_name("Rocket Lab USA, Inc.") == "Rocket Lab"
    assert (
        StockAliasMatrix.clean_company_name("AST SpaceMobile, Inc.")
        == "AST SpaceMobile"
    )
    assert StockAliasMatrix.clean_company_name("Duolingo, Inc.") == "Duolingo"


def test_build_reddit_query() -> None:
    """驗證 Reddit 專用 Boolean Search Query 產生器。"""
    query_nvda = StockAliasMatrix.build_reddit_query(
        "NVDA", ["nvda", "nvidia", "jensen huang", "blackwell"]
    )
    assert '"NVDA"' in query_nvda
    assert '"$NVDA"' in query_nvda
    assert '"nvidia"' in query_nvda or '"NVIDIA"' in query_nvda

    # 驗證不會放入通用單字
    query_smci = StockAliasMatrix.build_reddit_query(
        "SMCI", ["smci", "super micro", "supermicro"]
    )
    assert '"SMCI"' in query_smci
    assert '"$SMCI"' in query_smci
    assert '"super micro"' in query_smci
    assert '"super"' not in query_smci.split(" OR ")


def test_is_text_matching_symbol() -> None:
    """驗證嚴格詞界與別名文字撮合。"""
    aliases_nvda = ["nvda", "nvidia", "jensen huang", "blackwell"]

    # 正向匹配
    assert (
        StockAliasMatrix.is_text_matching_symbol(
            "Will NVIDIA beat Q2 earnings?", "NVDA", aliases_nvda
        )
        is True
    )
    assert (
        StockAliasMatrix.is_text_matching_symbol(
            "NVDA stock price above $180", "NVDA", aliases_nvda
        )
        is True
    )
    assert (
        StockAliasMatrix.is_text_matching_symbol(
            "Jensen Huang keynote at Computex", "NVDA", aliases_nvda
        )
        is True
    )

    # 負向匹配與防範模糊誤殺
    assert (
        StockAliasMatrix.is_text_matching_symbol(
            "Will French election result in socialist win?",
            "SMCI",
            ["smci", "super micro"],
        )
        is False
    )
    assert (
        StockAliasMatrix.is_text_matching_symbol(
            "Is this a super great day?", "SMCI", ["smci", "super micro"]
        )
        is False
    )


@pytest.mark.asyncio
async def test_auto_derivation_and_cache() -> None:
    """驗證未收錄標的之四層自動推導、補齊與持久化快取 (4-Tier)。"""
    test_symbol = "XYZUNLISTED"
    # 重設記憶體快取
    StockAliasMatrix._dynamic_alias_cache.pop(test_symbol, None)

    mock_profile = {
        "name": "XYZ Robotics Technologies, Inc.",
        "ticker": test_symbol,
    }

    with patch(
        "market_analysis.stock_alias_matrix.get_kv_cache", return_value=None
    ) as _mock_get_kv, patch(
        "market_analysis.stock_alias_matrix.save_kv_cache", new_callable=AsyncMock
    ) as mock_save_kv, patch(
        "market_analysis.stock_alias_matrix.get_company_profile",
        new_callable=AsyncMock,
        return_value=mock_profile,
    ):
        # 第一次查詢：觸發 Tier 4 自動推導
        aliases = await StockAliasMatrix.get_aliases_for_symbol(test_symbol)
        assert test_symbol.lower() in aliases
        assert "xyz robotics technologies" in aliases or "xyz" in aliases
        mock_save_kv.assert_called_once()

        # 第二次查詢：觸發 Tier 2 記憶體快取 (0ms)
        cached_aliases = await StockAliasMatrix.get_aliases_for_symbol(test_symbol)
        assert cached_aliases == aliases
