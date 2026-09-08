from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

import market_time

from market_analysis.dynamic_rollover.left_side_entry import (
    _confirm_left_entry_condition1_exhaustion_reversal,
    _confirm_left_entry_condition2_put_wall_test,
    _confirm_left_entry_condition3_no_panic_cliff,
    _confirm_left_entry_condition4_smart_money_absorption,
    _confirm_left_entry_condition5_macro_earnings_vts_gate,
    _confirm_left_entry_condition6_candidate_dte_ivr,
    _confirm_left_entry_signal,
    _detect_bullish_reversal_candle_pattern,
)


def _make_15m_df(
    bars: list[tuple[float, float]], last_open: Optional[float] = None
) -> pd.DataFrame:
    """比照 test_dynamic_rollover.py::_make_15m_df 的建構方式：最後一筆視為
    待確認的收盤根，其餘根 Open==Close (十字)。

    索引比照 yfinance 15m K 棒語意（每根為該根的「起始時間」），並固定錨在
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


# 極端負乖離、超賣、恐慌吸收巨量、未收最低點的待確認根 (Pin Bar 型態)：
# Open=95 (十字打底), Low=88 (長下影), Close=94.8 (未收最低)。
_OVERSOLD_PANIC_DF = _make_15m_df(
    [(95.0, 1000.0)] * 20 + [(94.8, 3000.0)], last_open=95.0
)


def _with_forming_last_bar(df: pd.DataFrame) -> pd.DataFrame:
    """把索引改寫為「最後一根仍在成型中」(起始時間距今僅 5 分鐘)，其餘每根往前
    推 15 分鐘，用於驗證未收盤 K 棒不得參與判定。"""
    df = df.copy()
    now_ny = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    last_start = now_ny - timedelta(minutes=5)
    df.index = pd.DatetimeIndex(
        [last_start - timedelta(minutes=15 * i) for i in range(len(df) - 1, -1, -1)]
    )
    return df


def _patch_last_bar_wick(df: pd.DataFrame, low: float) -> pd.DataFrame:
    df = df.copy()
    df.iloc[-1, df.columns.get_loc("Low")] = low
    return df


_OVERSOLD_HAMMER_DF = _patch_last_bar_wick(_OVERSOLD_PANIC_DF, low=88.0)

_FAR_EXPIRY = (datetime.now().date() + timedelta(days=25)).strftime("%Y-%m-%d")
_NEAR_EXPIRY = (datetime.now().date() + timedelta(days=3)).strftime("%Y-%m-%d")


def _left_candidate_radar() -> dict:
    """左側六重鐵律全數通過的候選標的基準 fixture：現價 $95 密著 Put Wall
    $95（區間 -1%~+1.5%），Put Wall 曝險量級 $6M（>= 5M 代理門檻），無追空
    踩踏 PUT BTO，含一筆符合條件四門檻的 PUT STO 護盤單。"""
    return {
        "quote": {"c": 95.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {
            "put_wall": 95.0,
            "call_wall": 110.0,
            "net_gex": -200_000.0,
            "gex_profile": {"95": -6_000_000.0, "110": 400_000.0},
        },
        "uoa": [
            {
                "type": "PUT",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 400_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ],
    }


# ---- 條件一 ----


class TestCondition1:
    def test_detect_pattern_capitulation_bar_fails(self) -> None:
        capitulation_df = _make_15m_df([(95.0, 1000.0)] * 3, last_open=100.0)
        # Close 貼近 Low：大陰線實體灌破
        capitulation_df.iloc[-1, capitulation_df.columns.get_loc("Low")] = 94.9
        capitulation_df.iloc[-1, capitulation_df.columns.get_loc("Close")] = 95.0
        capitulation_df.iloc[-1, capitulation_df.columns.get_loc("High")] = 100.5
        passed, reason = _detect_bullish_reversal_candle_pattern(capitulation_df)
        assert passed is False
        assert "大陰線實體灌破" in reason

    def test_detect_pattern_hammer_passes(self) -> None:
        hammer_df = _make_15m_df([(95.0, 1000.0)] * 3, last_open=95.0)
        hammer_df.iloc[-1, hammer_df.columns.get_loc("Low")] = 88.0
        hammer_df.iloc[-1, hammer_df.columns.get_loc("Close")] = 95.5
        passed, reason = _detect_bullish_reversal_candle_pattern(hammer_df)
        assert passed is True
        assert "錘頭" in reason

    def test_detect_pattern_dragonfly_doji_passes(self) -> None:
        """蜻蜓十字 (實體趨近零 + 長下影線) 是教科書級底部反轉訊號，必須通過。

        錘頭分支因為需要 body > 0 防呆 (否則墓碑十字也會誤判為錘頭) 而無法涵蓋
        它，故以「下影線佔全距比例」獨立判定補回。
        """
        doji_df = _make_15m_df([(95.0, 1000.0)] * 3, last_open=95.0)
        # Open == Close == 95.0 (實體為零)、Low = 88.0 (長下影)、High = 95.3
        doji_df.iloc[-1, doji_df.columns.get_loc("Low")] = 88.0
        doji_df.iloc[-1, doji_df.columns.get_loc("High")] = 95.3
        passed, reason = _detect_bullish_reversal_candle_pattern(doji_df)
        assert passed is True
        assert "蜻蜓十字" in reason

    def test_detect_pattern_gravestone_doji_still_fails(self) -> None:
        """墓碑十字 (實體趨近零 + 長上影、幾乎無下影) 是頂部訊號，不得被
        蜻蜓十字的比例判定誤放行。"""
        doji_df = _make_15m_df([(95.0, 1000.0)] * 3, last_open=95.0)
        doji_df.iloc[-1, doji_df.columns.get_loc("Low")] = 94.9
        doji_df.iloc[-1, doji_df.columns.get_loc("High")] = 102.0
        passed, reason = _detect_bullish_reversal_candle_pattern(doji_df)
        assert passed is False
        assert "未偵測到" in reason

    def test_detect_pattern_insufficient_data_fails(self) -> None:
        passed, reason = _detect_bullish_reversal_candle_pattern(
            _make_15m_df([(1.0, 1.0)])
        )
        assert passed is False
        assert "資料不足" in reason

    @pytest.mark.asyncio
    async def test_condition1_all_signals_align_passes(self) -> None:
        reasons: list = []
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_OVERSOLD_HAMMER_DF,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
        ):
            # VWAP=100, ATR=1.0 -> 乖離門檻 = 100 - 1.5*1.0 = 98.5；現價 95.0 <= 98.5 通過
            passed, _df = await _confirm_left_entry_condition1_exhaustion_reversal(
                "TEST", 95.0, 100.0, reasons
            )
        assert passed is True
        assert "左側條件一✅" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition1_ignores_still_forming_last_bar_volume(self) -> None:
        """回歸鎖定：最後一根仍在成型的 K 棒只累積了一部分成交量，若被誤用於
        「縮量窒息 (量 <= 0.7x 均量)」判定，會讓該子條件在每根 K 棒的前段時間
        恆為真，形成危險方向的偽陽性。

        此處已收盤根的量能為均量水準 (1000 vs 均量 1000，既非縮量也非爆量)，
        只有成型中那根 (量 100) 看起來像縮量窒息。條件一必須以已收盤根為準，
        判定為「未達量能訊號」。
        """
        bars = [(95.0, 1000.0)] * 21 + [(94.8, 100.0)]
        df = _with_forming_last_bar(_make_15m_df(bars, last_open=95.0))
        reasons: list = []
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=df,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
        ):
            passed, _df = await _confirm_left_entry_condition1_exhaustion_reversal(
                "TEST", 95.0, 100.0, reasons
            )
        assert passed is False
        assert "未達量能訊號" in reasons[0]
        assert "縮量窒息" not in reasons[0]

    @pytest.mark.asyncio
    async def test_condition1_fails_shallow_deviation(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=_OVERSOLD_HAMMER_DF,
        ):
            # VWAP=95.5, ATR=1.0 -> 門檻=94.0；現價 95.0 > 94.0，乖離不足
            passed, _df = await _confirm_left_entry_condition1_exhaustion_reversal(
                "TEST", 95.0, 95.5, reasons
            )
        assert passed is False
        assert "左側條件一❌" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition1_fails_on_invalid_spot(self) -> None:
        reasons: list = []
        passed, _df = await _confirm_left_entry_condition1_exhaustion_reversal(
            "TEST", 0.0, 100.0, reasons
        )
        assert passed is False
        assert "candidate 現價無效" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition1_fails_on_fetch_exception(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            passed, _df = await _confirm_left_entry_condition1_exhaustion_reversal(
                "TEST", 95.0, 100.0, reasons
            )
        assert passed is False
        assert "左側條件一❌" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition1_reuses_prefetched_df_15m(self) -> None:
        reasons: list = []
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
            ) as mock_history,
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
        ):
            await _confirm_left_entry_condition1_exhaustion_reversal(
                "TEST", 95.0, 100.0, reasons, df_15m=_OVERSOLD_HAMMER_DF
            )
        mock_history.assert_not_awaited()


# ---- 條件二 ----


class TestCondition2:
    def test_condition2_thick_wall_densely_attached_passes(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition2_put_wall_test(
            "TEST",
            {"put_wall": 95.0, "gex_profile": {"95": -6_000_000.0}},
            95.5,
            reasons,
        )
        assert passed is True
        assert "左側條件二✅" in reasons[0]

    def test_condition2_thin_wall_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition2_put_wall_test(
            "TEST",
            {"put_wall": 95.0, "gex_profile": {"95": -100_000.0}},
            95.5,
            reasons,
        )
        assert passed is False
        assert "⚠️GEX曝險量級代理" in reasons[0]

    def test_condition2_not_densely_attached_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition2_put_wall_test(
            "TEST",
            {"put_wall": 80.0, "gex_profile": {"80": -6_000_000.0}},
            95.0,
            reasons,
        )
        assert passed is False

    def test_condition2_missing_put_wall_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition2_put_wall_test("TEST", {}, 95.0, reasons)
        assert passed is False
        assert "資料缺失" in reasons[0]


# ---- 條件三 ----


class TestCondition3:
    def test_condition3_sufficient_room_passes(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition3_no_panic_cliff(
            [],
            {"put_wall": 90.0, "gex_profile": {"90": 300_000.0}},
            90.0,
            95.0,
            reasons,
        )
        assert passed is True
        assert "左側條件三✅" in reasons[0]

    def test_condition3_chase_selloff_fails(self) -> None:
        reasons: list = []
        uoa = [
            {
                "type": "PUT",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 85.0,
                "ratio": 1.5,
                "notional_value": 300_000.0,
            }
        ]
        passed = _confirm_left_entry_condition3_no_panic_cliff(
            uoa, {"put_wall": 90.0}, 90.0, 95.0, reasons
        )
        assert passed is False
        assert "追空踩踏" in reasons[0]

    def test_condition3_insufficient_room_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition3_no_panic_cliff(
            [], {"put_wall": 90.0}, 90.0, 91.0, reasons
        )
        assert passed is False
        assert "回歸空間" in reasons[0]

    def test_condition3_no_reference_level_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition3_no_panic_cliff(
            [], {}, 90.0, 0.0, reasons
        )
        assert passed is False


# ---- 條件四 ----


class TestCondition4:
    def test_condition4_put_sto_passes(self) -> None:
        reasons: list = []
        uoa = [
            {
                "type": "PUT",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 400_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ]
        passed = _confirm_left_entry_condition4_smart_money_absorption(
            uoa, 95.0, reasons
        )
        assert passed is True
        assert "PUT STO 護盤" in reasons[0]

    def test_condition4_call_bto_long_dated_passes(self) -> None:
        reasons: list = []
        far_expiry = (datetime.now().date() + timedelta(days=40)).strftime("%Y-%m-%d")
        uoa = [
            {
                "type": "CALL",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 95.0,
                "ratio": 0.9,
                "notional_value": 250_000.0,
                "expiry": far_expiry,
            }
        ]
        passed = _confirm_left_entry_condition4_smart_money_absorption(
            uoa, 95.0, reasons
        )
        assert passed is True
        assert "CALL BTO 底層潛伏" in reasons[0]

    def test_condition4_no_qualifying_entries_fails(self) -> None:
        reasons: list = []
        passed = _confirm_left_entry_condition4_smart_money_absorption(
            [], 95.0, reasons
        )
        assert passed is False
        assert "左側條件四❌" in reasons[0]

    def test_condition4_dte_too_low_fails(self) -> None:
        reasons: list = []
        uoa = [
            {
                "type": "PUT",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 400_000.0,
                "expiry": _NEAR_EXPIRY,
            }
        ]
        passed = _confirm_left_entry_condition4_smart_money_absorption(
            uoa, 95.0, reasons
        )
        assert passed is False


# ---- 條件五 ----


class TestCondition5:
    @pytest.mark.asyncio
    async def test_condition5_skips_when_prior_failed(self) -> None:
        reasons: list = []
        passed = await _confirm_left_entry_condition5_macro_earnings_vts_gate(
            "TEST", False, reasons
        )
        assert passed is True
        assert "左側條件五⏭️" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition5_backwardation_fails(self) -> None:
        reasons: list = []
        with (
            patch("database.calendar_cache.get_cached_earnings", return_value=None),
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="NORMAL",
            ),
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value={"vts_ratio": 1.2},
            ),
        ):
            passed = await _confirm_left_entry_condition5_macro_earnings_vts_gate(
                "TEST", True, reasons
            )
        assert passed is False
        assert "VIX 期限結構倒掛" in reasons[-1]

    @pytest.mark.asyncio
    async def test_condition5_normal_vts_passes(self) -> None:
        reasons: list = []
        with (
            patch("database.calendar_cache.get_cached_earnings", return_value=None),
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
        ):
            passed = await _confirm_left_entry_condition5_macro_earnings_vts_gate(
                "TEST", True, reasons
            )
        assert passed is True
        assert "左側條件五✅" in reasons[-1]


# ---- 條件六 ----


class TestCondition6:
    @pytest.mark.asyncio
    async def test_condition6_skips_when_prior_failed(self) -> None:
        reasons: list = []
        passed, directive = await _confirm_left_entry_condition6_candidate_dte_ivr(
            "TEST", False, 20.0, reasons
        )
        assert passed is True
        assert directive is None
        assert "左側條件六⏭️" in reasons[0]

    @pytest.mark.asyncio
    async def test_condition6_low_ivr_suggests_long_call(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_FAR_EXPIRY],
        ):
            passed, directive = await _confirm_left_entry_condition6_candidate_dte_ivr(
                "TEST", True, 20.0, reasons
            )
        assert passed is True
        assert directive == "輕度 ITM/ATM Call 買進"

    @pytest.mark.asyncio
    async def test_condition6_high_ivr_suggests_spread(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_FAR_EXPIRY],
        ):
            passed, directive = await _confirm_left_entry_condition6_candidate_dte_ivr(
                "TEST", True, 75.0, reasons
            )
        assert passed is True
        assert directive is not None
        assert "Bull Call Spread" in directive

    @pytest.mark.asyncio
    async def test_condition6_near_expiry_fails(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_NEAR_EXPIRY],
        ):
            passed, directive = await _confirm_left_entry_condition6_candidate_dte_ivr(
                "TEST", True, 20.0, reasons
            )
        assert passed is False
        assert directive is None


# ---- Orchestrator ----


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_all_six_conditions_pass(self) -> None:
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_OVERSOLD_HAMMER_DF,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            patch("database.calendar_cache.get_cached_earnings", return_value=None),
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
                "services.market_data_service.get_all_option_expiries",
                new_callable=AsyncMock,
                return_value=[_FAR_EXPIRY],
            ),
        ):
            confirmed, reason, directive = await _confirm_left_entry_signal(
                "TEST", _left_candidate_radar(), 95.0
            )
        assert confirmed is True
        assert "左側條件一✅" in reason
        assert "左側條件二✅" in reason
        assert "左側條件三✅" in reason
        assert "左側條件四✅" in reason
        assert "左側條件五✅" in reason
        assert "左側條件六✅" in reason
        assert directive == "輕度 ITM/ATM Call 買進"

    @pytest.mark.asyncio
    async def test_short_circuit_skips_condition5_and_6(self) -> None:
        radar = _left_candidate_radar()
        radar["uoa"] = []  # 條件四刻意失敗，觸發條件五/六短路
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_OVERSOLD_HAMMER_DF,
            ),
            patch(
                "market_analysis.atr_utils.fetch_atr_15m",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
        ):
            confirmed, reason, directive = await _confirm_left_entry_signal(
                "TEST", radar, 95.0
            )
        assert confirmed is False
        assert "左側條件四❌" in reason
        assert "左側條件五⏭️" in reason
        assert "左側條件六⏭️" in reason
        assert directive is None
