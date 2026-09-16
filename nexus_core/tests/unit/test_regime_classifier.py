from typing import Any, Optional
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

# Regime V fixture：21 根 close 由平盤 100 破位跌至 92 (RSI=0)，量能放大 2x，
# 最後一根為實體陰線 (open=98 > close=92)。
_BREAKDOWN_DF = _make_15m_df([(100.0, 1000.0)] * 20 + [(92.0, 2000.0)], last_open=98.0)


def _gex_profile_v() -> dict:
    """Regime V 破位追空 fixture：現價 92 已跌破 Put Wall 95；上方阻力頂牆
    $97 (GEX +3M)，停損 = 97 + 0.5×ATR₁₅ₘ → 距現價約 6.0%，落在
    [2.5×ATR₁₅ₘ/92, 絕對 8%] 之內；下方次級負 Gamma 節點 $80，
    空間 13.04% >= 2.0×ATR₁D/92。
    Gamma Flip 估算需 > 現價 (做市商處於負 Gamma 順勢助跌)。

    ⚠️ 全鏈 Net GEX 必須為**負**：estimate_symbol_gamma_flip 有 Regime 一致性
    驗證——total_gex > 0 (LONG_GAMMA) 時只接受 strike <= spot 的交叉點，會把
    現價上方的 $100 交叉點剔除而回傳 0.0，Regime V 的 `gamma_flip > 0` 前提
    就不成立。故此處刻意把下方負值節點加深，使全鏈合計為負。"""
    return {
        "call_wall": 97.0,
        "put_wall": 95.0,
        "net_gex": -2_500_000.0,
        "gex_profile": {
            "80": -4_000_000.0,
            "88": -1_000_000.0,
            "95": -500_000.0,
            "97": 3_000_000.0,
        },
    }


def _gex_profile_iii() -> dict:
    """比照 test_dynamic_rollover.py::_green_candidate_radar 的 GEX profile：
    Gamma Flip 估算 = 95.0，現價下方 (K<Spot) 最大正 GEX 支撐牆 = 95.0。

    ⚠️ 空間門檻改為動態自適應波動率門檻後，本 fixture 的 call_wall 自 110 上調
    至 120、put_wall 自 95 上調至 97：以現價 100、ATR₁₅ₘ≈1.2 計算，
    Risk = (100 − (97 − 1.5×1.2))/100 ≈ 4.8%，門檻 = 2.2 × 4.8% ≈ 10.6%，
    舊值 call_wall 110 的 10% 空間已不足以通過。這正是本次改版的預期行為
    ——put_wall 距現價 5% 的標的，call_wall 只在上方 10% 並不構成 2.2:1 盈虧比。
    """
    return {
        "call_wall": 120.0,
        "put_wall": 97.0,
        "net_gex": 800_000.0,
        "gex_profile": {
            "90": -500_000.0,
            "95": 800_000.0,
            "100": 300_000.0,
            "105": -200_000.0,
        },
    }


def _patch_atr_1d(value: float = 3.0) -> Any:
    """統一 patch `fetch_atr_1d`。

    測試環境中 `get_history_df` 一律被 patch 成回傳 15m fixture frame，若讓
    `fetch_atr_1d` 走真實路徑就會把 15m 的 ATR 當成日線 ATR，導致
    「ATR₁D ≈ ATR₁₅ₘ」這個現實中不可能的組合，使緩衝雙邊界退化為空區間
    (下界 2.5×ATR₁₅ₘ > 上界 1.8×ATR₁D)。此處固定給一個真實量級的日線 ATR
    (≈ 5 × ATR₁₅ₘ，符合 √26 折算關係)。
    """
    return patch(
        "market_analysis.atr_utils.fetch_atr_1d",
        new_callable=AsyncMock,
        return_value=value,
    )


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
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            _patch_atr_1d(),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 102.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "Call Wall $102.00 空間 +2.00% 不足動態門檻" in reason

    @pytest.mark.asyncio
    async def test_call_wall_negative_room_percentage_formatting(self) -> None:
        """Call Wall 低於現價 (已觸及或跌破) 時，距離為負值且必然小於動態
        門檻，格式化應包含帶負號百分比與門檻值。"""
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
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            _patch_atr_1d(),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 98.0, "put_wall": 90.0, "gex_profile": {}},
                [],
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "Call Wall $98.00 空間 -2.00% 不足動態門檻" in reason

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
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            _patch_atr_1d(),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 110.0, "put_wall": 90.0, "gex_profile": {}},
                uoa,
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "STO Call 壓頂 @ $120.00（位於 Call Wall 上方）" in reason

    @pytest.mark.asyncio
    async def test_sto_call_physical_cap_without_call_wall_falls_back_to_spot(
        self,
    ) -> None:
        """Call Wall 缺失 (<=0 或 None) 時，封頂基準退回現價，reason 應顯示（位於現價上方）。"""
        uoa = [
            {
                "type": "CALL",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 120.0,
                "ratio": 2.0,
            }
        ]
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
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            _patch_atr_1d(),
        ):
            # 測試 call_wall 為 0.0
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 0.0, "put_wall": 90.0, "gex_profile": {}},
                uoa,
            )
            assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
            assert "STO Call 壓頂 @ $120.00（位於現價上方）" in reason

            # 測試 call_wall 為 None
            regime_none, reason_none, _ = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": None, "put_wall": 90.0, "gex_profile": {}},
                uoa,
            )
            assert regime_none == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
            assert "STO Call 壓頂 @ $120.00（位於現價上方）" in reason_none

    @pytest.mark.asyncio
    async def test_call_wall_and_sto_cap_both_present_in_reason(self) -> None:
        """Call Wall 空間不足與 STO Call 物理封頂同時觸發時，兩項原因皆應完整呈現。"""
        uoa = [
            {
                "type": "CALL",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 120.0,
                "ratio": 2.0,
            }
        ]
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
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            _patch_atr_1d(),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST",
                100.0,
                {"call_wall": 102.0, "put_wall": 90.0, "gex_profile": {}},
                uoa,
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "Call Wall $102.00 空間 +2.00% 不足動態門檻" in reason
        assert "STO Call 壓頂 @ $120.00（位於 Call Wall 上方）" in reason


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
            _patch_atr_1d(),
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
            # fetch_atr_1d 若走真實路徑會再次呼叫 get_history_df（日線），
            # 破壞下方「不得對 candidate 重複抓取」的斷言精確性。
            _patch_atr_1d(),
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
            _patch_atr_1d(),
        ):
            regime, reason, _df = await classify_dynamic_regime(
                "TEST", 90.0, gex_profile_data, []
            )
        assert regime == DynamicRegime.REGIME_I_LEFT_CATCH
        assert "極端負乖離吸籌確認" in reason


class TestRegimeV:
    @pytest.mark.asyncio
    async def test_breakdown_structure_classifies_regime_v(self) -> None:
        """跌破 Gamma Flip / VWAP / Put Wall + 放量陰線 + RSI < 45
        + 次級節點空間充足 -> Regime V 破位追空態。"""
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKDOWN_DF,
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=99.0,
            ),
            _patch_atr_1d(5.0),
        ):
            regime, reason, market_data = await classify_dynamic_regime(
                "TEST", 92.0, _gex_profile_v(), []
            )
        assert regime == DynamicRegime.REGIME_V_BREAKDOWN_CHASE
        assert "結構破位負 Gamma 順勢助跌確認" in reason
        assert "次級負 Gamma 節點 $80.00" in reason
        # 供做空六重鐵律重用同一份快照
        assert market_data.df_15m is not None
        assert market_data.session_vwap == 99.0
        assert market_data.atr_15m > 0

    @pytest.mark.asyncio
    async def test_regime_v_takes_priority_over_structural_cap(self) -> None:
        """個股結構封頂 (Call Wall 空間不足) 與破位條件同時成立時，Regime V
        必須優先——否則做空永遠被 Regime IV 遮蔽而無法觸發。

        本 fixture 的 Call Wall $97 距現價 $92 僅 5.43%，遠低於動態門檻
        (put_wall 95 > spot 92 已穿刺，Risk 偏高)，故 is_structural_cap 成立。
        """
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKDOWN_DF,
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=99.0,
            ),
            _patch_atr_1d(5.0),
        ):
            regime, _reason, _md = await classify_dynamic_regime(
                "TEST", 92.0, _gex_profile_v(), []
            )
        assert regime == DynamicRegime.REGIME_V_BREAKDOWN_CHASE

    @pytest.mark.asyncio
    async def test_macro_lockout_beats_regime_v(self) -> None:
        """宏觀鎖定壓過 Regime V：系統性流動性危機下做空同樣會被劇烈軋空。"""
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="SYSTEMIC_LIQUIDITY_CRISIS",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKDOWN_DF,
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            _patch_atr_1d(5.0),
        ):
            regime, reason, _md = await classify_dynamic_regime(
                "TEST", 92.0, _gex_profile_v(), []
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        assert "SYSTEMIC_LIQUIDITY_CRISIS" in reason

    @pytest.mark.asyncio
    async def test_macro_lockout_skips_15m_fetch(self) -> None:
        """宏觀鎖定分支刻意在 15m/ATR 抓取之前早退，避免多發一次
        force_refresh 請求（每輪次對每個候選標的都會走這條路徑）。"""
        with (
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="SHORT_GAMMA_CRITICAL",
            ),
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
            ) as mock_history,
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_1d",
                new_callable=AsyncMock,
            ) as mock_atr_1d,
        ):
            regime, _reason, _md = await classify_dynamic_regime(
                "TEST", 92.0, _gex_profile_v(), []
            )
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
        mock_history.assert_not_awaited()
        mock_atr_1d.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_next_strike_space_blocks_regime_v(self) -> None:
        """下方無足夠的次級負 Gamma 節點空間 -> 不得追空（在真空區追高殺低）。"""
        gex = _gex_profile_v()
        gex["gex_profile"] = {
            "91": -4_000_000.0,  # 距現價僅 1.09%，遠低於 2.0×ATR₁D
            "97": 3_000_000.0,
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
                return_value=_BREAKDOWN_DF,
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 0.9},
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=99.0,
            ),
            _patch_atr_1d(5.0),
        ):
            regime, _reason, _md = await classify_dynamic_regime("TEST", 92.0, gex, [])
        assert regime != DynamicRegime.REGIME_V_BREAKDOWN_CHASE


class TestRegimeII:
    @pytest.mark.asyncio
    async def test_no_structural_signal_defaults_to_regime_ii(self) -> None:
        # ⚠️ put_wall 原為 60.0（距現價 40%）：動態門檻下 Risk 高達 41.6%，
        # 推出 2.2×41.6% ≈ 91% 的荒謬門檻，反而讓 call_wall 150 (空間 50%)
        # 被判定為「空間不足」而觸發 Regime IV。改為真實量級的 96.0。
        gex_profile_data = {
            "call_wall": 150.0,
            "put_wall": 96.0,
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
            _patch_atr_1d(),
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
