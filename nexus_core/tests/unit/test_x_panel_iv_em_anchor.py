"""/x 面板 PR-A：EM 中心、IV tenor 標示、HV 代理揭露、極端高波、結算前 1σ、盤後標題。"""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from cogs.embed_builders.portfolio_embeds import (
    create_tactical_symbol_embed,
    get_scenario_guidance,
)
from market_time import ny_tz
from models.quant import IVMetrics

MRVL_QUOTE = {"c": 284.68, "d": -2.33, "dp": -0.8118, "pc": 287.01}


def _text(embed: Any) -> str:
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _hv_iv(**kw: Any) -> IVMetrics:
    base: dict[str, Any] = dict(
        symbol="MRVL",
        current_iv=0.7356940799879191,
        iv_source="HV_PROXY",
        is_premarket=True,
        event_loading_applied=True,
        has_macro_event=True,
        straddle_implied_iv=0.6112018392634956,
        straddle_expiry="2026-10-16",
        straddle_dte=9,
        expected_move_weekly=24.0959579930421,
        reference_spot_price=284.68,
        hv_20=0.5271110245291736,
        term_structure_ratio=1.0327,
        iv_term_structure_status="Normal",
        iv_history_count=1,
    )
    base.update(kw)
    return IVMetrics(**base)


async def _run_hub(iv: IVMetrics, market_open: bool) -> dict[str, Any]:
    from cogs.unified_terminal.symbol_deep_dive import SymbolDeepDiveMixin

    class _DD(SymbolDeepDiveMixin):
        def __init__(self) -> None:
            self.bot = MagicMock()

    raw: dict[str, Any] = {
        "df_spy": pd.DataFrame(),
        "macro_raw": {"vix": 15.08},
        "quote": dict(MRVL_QUOTE),
        "skew_data": {},
        "pcr_data": {},
        "uoa_data": [],
        "sto_physical_cap_strikes": [],
        "max_pain_data": {},
        "iv_metrics": iv,
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
        patch(
            "market_time.is_market_open",
            return_value=market_open,
        ),
    ):
        m.return_value = {"symbol": "MRVL", "price": 286.0802}
        return await _DD()._process_symbol_hub_data("MRVL", 1, raw)


@pytest.mark.asyncio
async def test_a1_after_hours_em_centered_on_latest_close() -> None:
    res = await _run_hub(_hv_iv(), market_open=False)
    em = res["expected_move_context"]
    assert em["reference_label"] == "最新收盤"
    assert em["reference_price"] == pytest.approx(284.68)
    assert em["expected_move_lower"] == pytest.approx(260.58, abs=0.01)
    assert em["expected_move_upper"] == pytest.approx(308.78, abs=0.01)


@pytest.mark.asyncio
async def test_a1_intraday_stored_iv_also_uses_ref_spot() -> None:
    res = await _run_hub(_hv_iv(iv_source="STORED_IV"), market_open=True)
    em = res["expected_move_context"]
    assert em["reference_label"] == "現價"
    assert em["reference_price"] == pytest.approx(284.68)


def _data(iv: Any, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "symbol": "MRVL",
        "price": 284.68,
        "quote": dict(MRVL_QUOTE),
        "iv_data": iv,
        "expected_move_context": {
            "reference_price": 284.68,
            "reference_label": "最新收盤",
        },
    }
    d.update(extra)
    return d


def test_a2_live_iv_labels_expiry_and_dte() -> None:
    iv = _hv_iv(
        iv_source="LIVE_IV",
        is_premarket=False,
        event_loading_applied=False,
        current_iv_expiry="2026-10-09",
        current_iv_dte=2,
        term_near_expiry="2026-10-16",
        term_far_expiry="2026-11-06",
    )
    text = _text(create_tactical_symbol_embed(_data(iv)))
    assert "最近到期 10-09 (DTE 2) ±20% OI 加權，含結算 Gamma 偏高" in text
    assert "30 天平值" not in text
    assert "近 10-16／遠 11-06 比: 1.03" in text


def test_a2_legacy_cache_without_new_fields_loads() -> None:
    cached = _hv_iv().model_dump()
    for k in (
        "current_iv_expiry",
        "current_iv_dte",
        "term_near_expiry",
        "term_far_expiry",
    ):
        cached.pop(k)
    restored = IVMetrics(**cached)
    assert restored.current_iv_expiry is None and restored.term_far_expiry is None


def test_a3_hv_proxy_shows_loading_and_straddle_ratio() -> None:
    text = _text(create_tactical_symbol_embed(_data(_hv_iv())))
    assert "×1.4 事件加載 = 73.6%" in text
    assert "IV/HV20:" not in text.replace("跨式 IV/HV20:", "")
    assert "跨式 IV/HV20: 61.1% / 52.7% = 1.16x（參考）" in text


def _psq() -> dict[str, Any]:
    return {
        "is_squeezing": False,
        "momentum": 0.1,
        "squeeze_level": "Release",
        "direction": "Neutral",
        "vix_momentum_label": "NORMAL",
    }


def test_a4_hv_proxy_does_not_claim_extreme_vol() -> None:
    iv = _hv_iv(current_iv=0.88, straddle_implied_iv=None)
    text = _text(create_tactical_symbol_embed(_data(iv, psq_result=_psq())))
    assert "極端高波" not in text


def test_a4_straddle_iv_triggers_extreme_vol() -> None:
    iv = _hv_iv(straddle_implied_iv=0.85)
    text = _text(create_tactical_symbol_embed(_data(iv, psq_result=_psq())))
    assert "極端高波環境 (跨式 IV 85%)" in text
    assert "IVR 累積中，無歷史分位" in text


def test_a5_guidance_branches() -> None:
    assert "釘住 $290.00 為主" in get_scenario_guidance(
        284.68, 270.0, pin_strike=290.0, sigma_to_expiry=1.0
    )
    assert "收斂機率低" in get_scenario_guidance(284.68, 270.0, sigma_to_expiry=12.88)
    assert "技術指標" in get_scenario_guidance(284.68, 280.0, sigma_to_expiry=12.88)
    assert "技術指標" in get_scenario_guidance(284.68, 284.0)


def test_a5_panel_shows_sigma_and_low_convergence() -> None:
    iv = _hv_iv()
    mps = [{"expiry": "2026-10-09", "max_pain": 270.0, "distance_pct": 5.44}]
    now = datetime(2026, 10, 7, 21, 0, tzinfo=ny_tz)
    with patch("cogs.embed_builders.portfolio_embeds.datetime") as dt:
        dt.now.return_value = now
        dt.strptime.side_effect = datetime.strptime
        text = _text(
            create_tactical_symbol_embed(_data(iv, max_pain=270.0, month_max_pains=mps))
        )
    assert "±$12.9" in text
    assert "收斂機率低" in text


@pytest.mark.parametrize("hour,word", [(21, "盤後"), (10, "盤前")])
def test_a6_title_session_word(hour: int, word: str) -> None:
    now = datetime(2026, 10, 7, hour, 0, tzinfo=ny_tz)
    with patch("cogs.embed_builders.portfolio_embeds.datetime") as dt:
        dt.now.return_value = now
        dt.strptime.side_effect = datetime.strptime
        embed = create_tactical_symbol_embed(_data(_hv_iv()))
    assert f"[{word}/HV代理]" in (embed.title or "")
