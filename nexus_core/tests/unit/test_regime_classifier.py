from typing import Optional
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.dynamic_rollover.models import DynamicRegime
from market_analysis.dynamic_rollover.regime_classifier import classify_dynamic_regime


def _make_15m_df(
    bars: list[tuple[float, float]], last_open: Optional[float] = None
) -> pd.DataFrame:
    """索引比照 yfinance 15m K 棒語意（每根為該根的「起始時間」），並固定錨在
    過去的日期，確保最後一根必定已收盤、能通過
    price_volume_alert.trim_to_confirmed_15m_bars 的收盤判定，且不受執行時
    時區/時鐘影響而產生 flaky 結果。"""
    closes = [c for c, _ in bars]
    volumes = [v for _, v in bars]
    opens = list(closes)
    if last_open is not None:
        opens[-1] = last_open
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
            "Low": [min(o, c) - 0.5 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.date_range("2024-01-02 09:30", periods=len(closes), freq="15min"),
    )


# Regime III fixture：21 根 close 由平盤 98 跳升至 101 (RSI=100)，量能放大 1.5x，
# 最後一根為陽線 (open=99 < close=101)。
_BREAKOUT_DF = _make_15m_df([(98.0, 1000.0)] * 20 + [(101.0, 1500.0)], last_open=99.0)

# Regime I fixture：21 根 close 由平盤 100 崩跌至 90 (RSI=0)。
_CAPITULATION_DF = _make_15m_df([(100.0, 1000.0)] * 20 + [(90.0, 1000.0)])

# Regime II fixture：平盤微幅上漲，最後一根為陰線 (open>close)，排除 Regime III；
# 現價未深度乖離 VWAP，排除 Regime I。
_FLAT_DF = _make_15m_df([(100.0, 1000.0)] * 20 + [(100.5, 1000.0)], last_open=100.6)


def _gex_profile_iii() -> dict:
    """比照 test_dynamic_rollover.py::_green_candidate_radar 的 GEX profile：
    Gamma Flip 估算 = 95.0，現價下方 (K<Spot) 最大正 GEX 支撐牆 = 95.0。"""
    return {
        "call_wall": 110.0,
        "put_wall": 95.0,
        "net_gex": 800_000.0,
        "gex_profile": {
            "90": -500_000.0,
            "95": 800_000.0,
            "100": 300_000.0,
            "105": -200_000.0,
        },
    }


class TestRegimeIV:
    @pytest.mark.asyncio
    async def test_systemic_liquidity_crisis_forces_regime_iv(self) -> None:
        with patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="SYSTEMIC_LIQUIDITY_CRISIS",
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 150.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "SYSTEMIC_LIQUIDITY_CRISIS" in reason

    @pytest.mark.asyncio
    async def test_short_gamma_critical_forces_regime_iv(self) -> None:
        """SHORT_GAMMA_CRITICAL (做市商翻入負 Gamma 踩踏) 與
        SYSTEMIC_LIQUIDITY_CRISIS 同屬禁止任何多頭開倉的鎖定情境，比照
        index_microstructure.py 與 Scenario 4 對兩者一視同仁的既有慣例。"""
        with patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="SHORT_GAMMA_CRITICAL",
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 150.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "SHORT_GAMMA_CRITICAL" in reason

    @pytest.mark.asyncio
    async def test_deep_vix_backwardation_forces_regime_iv(self) -> None:
        """VIX 期限結構深度倒掛 (front 溢價 3-month > 10%) 單獨即可構成 Regime IV。

        get_market_regime() 的 SHORT_GAMMA_CRITICAL 需同時滿足 VIX>20、
        vts>=1.0 與 SPY 跌破 Gamma Flip 三項，單純的深度倒掛不會觸發它，
        故必須獨立檢查。
        """
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 1.15},
            ),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 150.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "深度倒掛" in reason

    @pytest.mark.asyncio
    async def test_call_wall_too_close_forces_regime_iv(self) -> None:
        with patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 102.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "Call Wall 空間" in reason

    @pytest.mark.asyncio
    async def test_sto_call_physical_cap_forces_regime_iv(self) -> None:
        uoa = [
            {
                "type": "CALL",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 120.0,
                "ratio": 2.0,
            }
        ]
        with patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 110.0, "put_wall": 90.0, "gex_profile": {}},
                uoa,
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "STO Call 壓頂" in reason


class TestRegimeIII:
    @pytest.mark.asyncio
    async def test_breakout_structure_classifies_regime_iii(self) -> None:
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKOUT_DF,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=98.0,
            ),
        ):
            regime, reason, df_15m = await classify_dynamic_regime(
                "TEST", 100.0, _gex_profile_iii(), []
            )
        assert regime == DynamicRegime.REGIME_III_RIGHT_MOMENTUM
        assert "結構突破伽馬擠壓確認" in reason
        assert df_15m is not None

    @pytest.mark.asyncio
    async def test_reuses_prefetched_df_15m(self) -> None:
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
            ) as mock_history,
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=98.0,
            ),
            # Regime IV 的 VIX 深度倒掛檢查內部也會走 get_history_df (^VIX)，
            # 此處一併 mock 以維持「不得對 candidate 重複抓取 15m K 線」這個
            # 斷言的精確性。
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
        ):
            regime, _reason, _df = await classify_dynamic_regime(
                "TEST", 100.0, _gex_profile_iii(), [], df_15m=_BREAKOUT_DF
            )
        mock_history.assert_not_awaited()
        assert regime == DynamicRegime.REGIME_III_RIGHT_MOMENTUM


class TestRegimeI:
    @pytest.mark.asyncio
    async def test_deep_deviation_classifies_regime_i(self) -> None:
        gex_profile_data = {
            "call_wall": 110.0,
            "put_wall": 90.0,
            "gex_profile": {"85": -100_000.0, "90": 100_000.0},
        }
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_CAPITULATION_DF,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=95.0,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=2.0,
            ),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST", 90.0, gex_profile_data, []
            )
        assert regime == DynamicRegime.REGIME_I_LEFT_CATCH
        assert "極端負乖離吸籌確認" in reason


class TestRegimeII:
    @pytest.mark.asyncio
    async def test_no_structural_signal_defaults_to_regime_ii(self) -> None:
        gex_profile_data = {
            "call_wall": 150.0,
            "put_wall": 60.0,
            "gex_profile": {"95": 100_000.0},
        }
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_FLAT_DF,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST", 100.0, gex_profile_data, []
            )
        assert regime == DynamicRegime.REGIME_II_CHAOS_STANDASIDE

    @pytest.mark.asyncio
    async def test_missing_spot_fails_safe_to_regime_ii(self) -> None:
        regime, reason, _df = await classify_dynamic_regime("TEST", 0.0, {}, [])
        assert regime == DynamicRegime.REGIME_II_CHAOS_STANDASIDE
        assert "資料缺失" in reason

    @pytest.mark.asyncio
    async def test_insufficient_15m_data_fails_safe_to_regime_ii(self) -> None:
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_make_15m_df([(100.0, 1000.0)] * 3),
            ),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 150.0, "put_wall": 60.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_II_CHAOS_STANDASIDE
        assert "15m 已收盤 K 線資料不足" in reason


@pytest.mark.asyncio
async def test_regime_iv_uses_strict_sto_ratio_threshold() -> None:
    """Regime IV 的 STO Call 封頂偵測必須顯式套用 1.5x 門檻——沿用函式預設的 1.0
    會讓單筆 ratio 1.2 的例行 STO 平倉單把正常盤況誤判為全面鎖倉態，連帶封鎖
    DYNAMIC 模式下的所有進場。"""
    routine_sto = [
        {
            "type": "CALL",
            "action": "🔴 賣出開倉 (STO - Bid)",
            "strike": 160.0,
            "ratio": 1.2,
            "notional_value": 400_000.0,
        }
    ]
    with (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        ),
        patch(
            "services.market_data_service.get_vix_term_structure",
            new_callable=AsyncMock,
            return_value={"vts_ratio": 0.9},
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        regime, reason, _data = await classify_dynamic_regime(
            "TEST",
            100.0,
            {"call_wall": 150.0, "put_wall": 90.0, "gex_profile": {}},
            routine_sto,
        )

    assert regime != DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
    assert "STO Call 壓頂" not in reason
