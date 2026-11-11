"""`uoa_oi_missing` 旗標由 UOA 偵測器經深度分析原始 dict 傳到 /x 資料模型。"""

from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin
from models.quant import IVMetrics


class _DD(SymbolDeepDiveMixin):
    def __init__(self) -> None:
        self.bot = MagicMock()
        self.bot.polymarket_service = None


def _raw(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "df_spy": pd.DataFrame(),
        "macro_raw": {"vix": 15.08},
        "quote": {"c": 120.0, "pc": 119.0},
        "skew_data": {},
        "pcr_data": {},
        "uoa_data": [],
        "sto_physical_cap_strikes": [],
        "max_pain_data": {},
        "iv_metrics": IVMetrics(symbol="NVDA", current_iv=0.5),
        "reddit_text": "",
        "poly_markets": [],
        "ddp_report": {},
        "df_hist_1d": pd.DataFrame(),
        "month_max_pains": [],
        "gex_profile_data": None,
        "volume_profile": None,
        "atr_15m": 0.0,
        "session_vwap": 0.0,
        "bar_15m": None,
        "catalysts": [],
    }
    d.update(extra)
    return d


async def _process(raw: dict[str, Any]) -> dict[str, Any]:
    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
        patch("market_math.analyze_symbol", new_callable=AsyncMock) as m,
        patch(
            "cogs.unified_terminal.utils.find_matching_polymarket_odds",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "cogs.unified_terminal.utils.calculate_polymarket_weighted_odds",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("database.get_full_user_context", return_value=MagicMock()),
    ):
        m.return_value = {"symbol": "NVDA", "price": 120.0}
        return await _DD()._process_symbol_hub_data("NVDA", 1, raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False, None])
async def test_process_copies_flag_into_result(flag: Any) -> None:
    res = await _process(_raw(uoa_oi_missing=flag))
    assert res["uoa_oi_missing"] is flag


@pytest.mark.asyncio
async def test_process_without_flag_defaults_to_none() -> None:
    res = await _process(_raw())
    assert res["uoa_oi_missing"] is None


@pytest.mark.asyncio
async def test_fetch_raw_puts_detector_flag_into_raw_dict() -> None:
    expiry = (date.today() + timedelta(days=15)).strftime("%Y-%m-%d")
    sentiment = "market_analysis.sentiment_engine.SentimentEngine"
    am = AsyncMock
    with (
        patch(
            "services.market_data_service.get_spy_history_df", new_callable=am
        ) as spy,
        patch(
            "services.market_data_service.get_macro_environment", new_callable=am
        ) as mac,
        patch("services.market_data_service.get_quote", new_callable=am) as quote,
        patch("services.market_data_service.get_history_df", new_callable=am) as hist,
        patch(
            "services.market_data_service.get_all_option_expiries", new_callable=am
        ) as exp,
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=am,
        ) as gex,
        patch("market_analysis.volume_profile.calculate_volume_profile") as vp,
        patch("services.reddit_service.get_reddit_details", new_callable=am) as reddit,
        patch(
            "market_analysis.ddp_inspector.DDPInspector.inspect_symbol", new_callable=am
        ) as ddp,
        patch(f"{sentiment}.calculate_skew", new_callable=am) as skew,
        patch(f"{sentiment}.calculate_pcr", new_callable=am) as pcr,
        patch(f"{sentiment}.detect_uoa_with_physical_caps", new_callable=am) as uoa,
        patch(f"{sentiment}.calculate_max_pain", new_callable=am) as mp,
        patch(f"{sentiment}.fetch_and_calculate_iv_metrics", new_callable=am) as iv,
        patch(f"{sentiment}.get_unified_max_pain", new_callable=am) as ump,
        patch(
            "cogs.unified_terminal.symbol_deep_dive._evaluate_squeeze_for_panel",
            new_callable=am,
            return_value=None,
        ),
        patch(
            "cogs.unified_terminal.symbol_deep_dive.last_chain_oi_missing",
            return_value=True,
        ) as flag_getter,
    ):
        spy.return_value = pd.DataFrame({"Close": [500.0]})
        mac.return_value = {"vix": 15.0}
        quote.return_value = {"c": 120.0}
        hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})
        exp.return_value = [expiry]
        gex.return_value = {"put_wall": 100.0}
        vp.return_value = {"hvn": 0.0, "lvn": 0.0}
        reddit.return_value = ("", [])
        ddp.return_value = {"is_ddp": False}
        skew.return_value = {"skew": 1.0}
        pcr.return_value = {"pcr": 0.9}
        uoa.return_value = ([], [])
        mp.return_value = {"max_pain": 115.0}
        ump.return_value = {"max_pain": 115.0, "distance_pct": 0.0}
        iv.return_value = MagicMock()

        raw = await _DD()._fetch_single_symbol_data_raw("NVDA")

    flag_getter.assert_called_once_with("NVDA")
    assert raw["uoa_oi_missing"] is True
