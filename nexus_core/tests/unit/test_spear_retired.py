"""SPEAR（Gamma Squeeze 引擎）已退役的回歸護欄。

多頭進場改由多週期擠壓進場 (market_analysis/squeeze_entry/) 負責；
引擎、領域模型、警報 embed 與去重前綴都不應再出現。
"""

import importlib
from unittest.mock import MagicMock

import pytest


# 不用 find_spec("market_analysis.models") is None：殘留的 __pycache__/ 會讓
# 空目錄變成 namespace package，判斷會假失敗。
def test_gamma_squeeze_engine_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("market_analysis.gamma_squeeze_engine")


def test_trader_models_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("market_analysis.models.trader_models")


def test_pipeline_constructs_without_engine() -> None:
    from market_analysis.intraday_pipeline import IntradayScanPipeline

    pipeline = IntradayScanPipeline(MagicMock())
    assert not hasattr(pipeline, "engine")


def test_gamma_squeeze_embed_builder_removed() -> None:
    from cogs.embed_builders import alert_embeds

    assert not hasattr(alert_embeds, "create_gamma_squeeze_alert_embed")


def test_gamma_squeeze_dedup_prefix_removed() -> None:
    from database.cache import _KV_CACHE_DEDUP_KEY_PREFIXES

    assert "gamma_squeeze_alert_" not in _KV_CACHE_DEDUP_KEY_PREFIXES
