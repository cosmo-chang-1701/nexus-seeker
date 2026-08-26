# Facade for SentimentEngine
from typing import Any
from typing import Dict

from .history_storage import (
    _trigger_background_cache_clear,
    save_sentiment_history,
    get_indicator_percentile,
    get_last_stored_iv,
    get_last_stored_sentiment,
    save_historical_iv,
    INDEX_SYMBOLS,
    _revalidating_symbols,
)
from .options_flow import calculate_skew, calculate_pcr
from .max_pain import (
    get_unified_max_pain,
    calculate_max_pain,
    _calculate_max_pain_raw,
    _current_week_friday,
)
from .uoa_detector import detect_uoa, detect_uoa_with_physical_caps
from .iv_metrics import (
    IVContext,
    fetch_and_calculate_iv_metrics,
    _calculate_straddle_implied_em,
)

__all__ = ["SentimentEngine", "_current_week_friday"]


class SentimentEngine:
    INDEX_SYMBOLS = INDEX_SYMBOLS
    _revalidating_symbols = _revalidating_symbols

    @staticmethod
    def _trigger_background_cache_clear(symbol: str) -> Any:
        return _trigger_background_cache_clear(symbol)

    @staticmethod
    async def calculate_skew(symbol: str, force_live: bool = False) -> Dict[str, Any]:
        return await calculate_skew(symbol, force_live=force_live)

    @staticmethod
    async def calculate_pcr(symbol: str, force_live: bool = False) -> Dict[str, Any]:
        return await calculate_pcr(symbol, force_live=force_live)

    @staticmethod
    async def get_unified_max_pain(*args, **kwargs):  # type: ignore
        return await get_unified_max_pain(*args, **kwargs)

    @staticmethod
    async def calculate_max_pain(*args, **kwargs):  # type: ignore
        return await calculate_max_pain(*args, **kwargs)

    @staticmethod
    async def _calculate_max_pain_raw(*args, **kwargs):  # type: ignore
        return await _calculate_max_pain_raw(*args, **kwargs)

    @staticmethod
    async def detect_uoa(*args, **kwargs):  # type: ignore
        return await detect_uoa(*args, **kwargs)

    @staticmethod
    async def detect_uoa_with_physical_caps(*args, **kwargs):  # type: ignore
        return await detect_uoa_with_physical_caps(*args, **kwargs)

    @staticmethod
    async def save_sentiment_history(*args, **kwargs):  # type: ignore
        return await save_sentiment_history(*args, **kwargs)

    @staticmethod
    def get_indicator_percentile(*args, **kwargs):  # type: ignore
        return get_indicator_percentile(*args, **kwargs)

    @staticmethod
    def get_last_stored_iv(*args, **kwargs):  # type: ignore
        return get_last_stored_iv(*args, **kwargs)

    @staticmethod
    def get_last_stored_sentiment(*args, **kwargs):  # type: ignore
        return get_last_stored_sentiment(*args, **kwargs)

    @staticmethod
    async def save_historical_iv(*args, **kwargs):  # type: ignore
        return await save_historical_iv(*args, **kwargs)

    @staticmethod
    async def fetch_and_calculate_iv_metrics(*args, **kwargs):  # type: ignore
        return await fetch_and_calculate_iv_metrics(*args, **kwargs)

    @staticmethod
    async def _calculate_straddle_implied_em(*args, **kwargs):  # type: ignore
        return await _calculate_straddle_implied_em(*args, **kwargs)

    @staticmethod
    async def get_expected_move(*args, **kwargs):  # type: ignore
        return await IVContext.get_expected_move(*args, **kwargs)
