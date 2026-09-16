"""做空六重嚴格過濾鐵律 (market_analysis/dynamic_rollover/short_side_entry.py)
單元測試。結構比照 tests/unit/test_left_side_entry.py。

⚠️ 本模組是本系統唯一的空頭方向進場路徑。左側 (LEFT_SIDE) 儘管技術定義與右側
相反，本質仍是做多（Put Wall 底牆接刀、向上回歸空間），不要混淆。
"""

from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.dynamic_rollover.short_side_entry import (
    _confirm_short_entry_condition1_breakdown,
    _confirm_short_entry_condition2_resistance_wall,
    _confirm_short_entry_condition3_downside_room,
    _confirm_short_entry_condition4_smart_money_pressure,
    _confirm_short_entry_condition6_candidate_dte_ivr,
    _confirm_short_entry_signal,
    _find_next_negative_gex_peak,
)

_FAR_EXPIRY = (datetime.now().date() + timedelta(days=25)).strftime("%Y-%m-%d")


def _make_15m_df(
    bars: list[tuple[float, float]], last_open: Optional[float] = None
) -> pd.DataFrame:
    """索引固定錨在過去的日期，確保最後一根必定已收盤、能通過
    price_volume_alert.trim_to_confirmed_15m_bars 的收盤判定。"""
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


# 破位 fixture：21 根 close 由平盤 100 跌破至 92，量能放大 2x，
# 最後一根為實體陰線 (open=98 > close=92)。
_BREAKDOWN_DF = _make_15m_df([(100.0, 1000.0)] * 20 + [(92.0, 2000.0)], last_open=98.0)

# 非破位 fixture：最後一根為陽線，量能未放大。
_FLAT_DF = _make_15m_df([(100.0, 1000.0)] * 20 + [(100.5, 900.0)], last_open=100.0)


def _short_candidate_radar() -> dict:
    """做空六重鐵律全數通過的候選標的基準 fixture。

    現價 $92：已跌破 Put Wall $95（破位追空子模式），上方阻力頂牆 $97
    (GEX +3M，>= 500k 薄紙牆門檻)。條件二量的是**停損距離**：
    停損 = 97 + 0.5×1.0 = 97.5 → 距現價 5.98%，落在 [2.72%, 絕對 8%] 之內。
    （頂牆若設 $100，停損距離 9.24% 會超過絕對上界。）
    下方次級負 Gamma 節點 $80（空間 13.04% >= 2.0×ATR₁D/92 = 10.87%）。
    含一筆符合條件四門檻的 PUT BTO 方向性押注。
    """
    return {
        "quote": {"c": 92.0},
        "iv_metrics": {"iv_rank": 20.0},
        "atr_15m": 1.0,
        "atr_14": 5.0,
        "gex_profile_data": {
            "put_wall": 95.0,
            "call_wall": 97.0,
            "net_gex": -800_000.0,
            "gex_profile": {
                "80": -2_000_000.0,
                "88": -500_000.0,
                "95": -300_000.0,
                "97": 3_000_000.0,
            },
        },
        "uoa": [
            {
                "type": "PUT",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 400_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ],
    }


# ---------------------------------------------------------------- 條件一
class TestCondition1:
    @pytest.mark.asyncio
    async def test_breakdown_with_volume_passes(self) -> None:
        reasons: list = []
        gex = {"gex_profile": {"90": -1_000_000.0, "100": 500_000.0}}
        with patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=_BREAKDOWN_DF,
        ):
            passed, _df = await _confirm_short_entry_condition1_breakdown(
                "TEST", 92.0, gex, 100.0, reasons, net_gex=-500_000.0
            )
        assert passed is True
        assert "做空條件一✅" in reasons[0]
        assert "實體陰線" in reasons[0]

    @pytest.mark.asyncio
    async def test_bullish_candle_fails(self) -> None:
        """陽線縮量不得判為破位——鏡像右側「排除陰線放量摜壓假突破」。"""
        reasons: list = []
        gex = {"gex_profile": {"90": -1_000_000.0, "100": 500_000.0}}
        with patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=_FLAT_DF,
        ):
            passed, _df = await _confirm_short_entry_condition1_breakdown(
                "TEST", 100.5, gex, 100.0, reasons, net_gex=-500_000.0
            )
        assert passed is False
        assert "非陰線" in reasons[0]

    @pytest.mark.asyncio
    async def test_global_long_gamma_without_flip_fails_structurally(self) -> None:
        """全鏈 Net GEX > 0 且無 Flip 交叉點 -> 做市商正 Gamma 自穩定，
        結構性多頭直接不通過（右側「Net GEX < 0 直接不通過」的鏡像）。"""
        reasons: list = []
        gex = {"gex_profile": {"90": 500_000.0, "100": 600_000.0}}
        passed, _df = await _confirm_short_entry_condition1_breakdown(
            "TEST", 92.0, gex, 100.0, reasons, net_gex=1_100_000.0
        )
        assert passed is False
        assert "全域 Long Gamma 自穩定" in reasons[0]
        assert "結構性多頭直接不通過" in reasons[0]

    @pytest.mark.asyncio
    async def test_short_gamma_no_flip_uses_vwap_fallback(self) -> None:
        """全鏈 Net GEX < 0 且無交叉點 -> 改以 VWAP − 0.5×ATR₁₅ₘ 作替代門檻。"""
        reasons: list = []
        gex = {"gex_profile": {"90": -500_000.0, "100": -600_000.0}}
        with patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=_BREAKDOWN_DF,
        ):
            passed, _df = await _confirm_short_entry_condition1_breakdown(
                "TEST", 92.0, gex, 100.0, reasons, net_gex=-1_100_000.0
            )
        assert passed is True
        assert "Fallback" in reasons[0]

    @pytest.mark.asyncio
    async def test_invalid_spot_fails(self) -> None:
        reasons: list = []
        passed, _df = await _confirm_short_entry_condition1_breakdown(
            "TEST", 0.0, {}, 100.0, reasons
        )
        assert passed is False
        assert "現價無效" in reasons[0]


# ---------------------------------------------------------------- 條件二
class TestCondition2:
    def test_resistance_wall_above_spot_passes(self) -> None:
        reasons: list = []
        passed, wall = _confirm_short_entry_condition2_resistance_wall(
            "TEST",
            _short_candidate_radar()["gex_profile_data"],
            92.0,
            reasons,
            atr_15m=1.0,
            atr_1d=5.0,
        )
        assert passed is True
        assert wall == 97.0
        assert "做空條件二✅" in reasons[0]

    def test_no_resistance_wall_fails(self) -> None:
        """現價上方無正 GEX 峰值 -> 未偵測到有效頂牆。"""
        reasons: list = []
        passed, wall = _confirm_short_entry_condition2_resistance_wall(
            "TEST",
            {"gex_profile": {"80": 3_000_000.0, "85": 1_000_000.0}},
            92.0,
            reasons,
            atr_15m=1.0,
            atr_1d=4.0,
        )
        assert passed is False
        assert wall == 0.0
        assert "未偵測到有效正 Gamma 壓制頂牆" in reasons[0]

    def test_thin_wall_is_rejected(self) -> None:
        """曝險低於 GEX_THIN_WALL_THRESHOLD (500k) 的薄紙牆不予信任。"""
        reasons: list = []
        passed, wall = _confirm_short_entry_condition2_resistance_wall(
            "TEST",
            {"gex_profile": {"100": 100_000.0}},
            92.0,
            reasons,
            atr_15m=1.0,
            atr_1d=4.0,
        )
        assert passed is False
        assert wall == 0.0

    def test_wall_too_close_fails(self) -> None:
        """頂牆貼現價過近：空單停損設在頂牆上方，牆太近時反抽即掃損。"""
        reasons: list = []
        passed, _wall = _confirm_short_entry_condition2_resistance_wall(
            "TEST",
            {"gex_profile": {"93": 3_000_000.0}},
            92.0,
            reasons,
            # 停損 = 93 + 0.5 = 93.5 → 距現價 1.63% < 下界 2.5×1/92 = 2.72%
            atr_15m=1.0,
            atr_1d=4.0,
        )
        assert passed is False
        assert "空單停損落在日內雜訊帶內" in reasons[0]

    def test_wall_too_far_fails(self) -> None:
        reasons: list = []
        passed, _wall = _confirm_short_entry_condition2_resistance_wall(
            "TEST",
            {"gex_profile": {"120": 3_000_000.0}},
            92.0,
            reasons,
            atr_15m=1.0,
            atr_1d=4.0,  # 停損距離 30.98% > 絕對上界 8%
        )
        assert passed is False
        assert "絕對風險上限" in reasons[0]


# ---------------------------------------------------------------- 條件三
class TestCondition3:
    def test_range_short_sub_mode_passes(self) -> None:
        """區間內做空：現價 > Put Wall，目標位 = Put Wall。"""
        reasons: list = []
        gex = {
            "put_wall": 85.0,
            "gex_profile": {"85": -1_000_000.0, "105": 3_000_000.0},
        }
        # 現價 100、頂牆 105、ATR₁₅ₘ 0.5 -> Stop = 105.75 -> Risk = 5.75%
        # 門檻 = max(2.2×5.75%, 1.5×2%, 3.5%) = 12.65%；空間 15% 通過。
        passed, mode = _confirm_short_entry_condition3_downside_room(
            [], gex, 100.0, 105.0, reasons, atr_15m=0.5, atr_1d=2.0
        )
        assert passed is True
        assert mode == "區間內做空"
        assert "做空條件三✅[區間內]" in reasons[0]

    def test_range_short_insufficient_room_fails(self) -> None:
        reasons: list = []
        gex = {"put_wall": 97.0, "gex_profile": {"105": 3_000_000.0}}
        passed, mode = _confirm_short_entry_condition3_downside_room(
            [], gex, 100.0, 105.0, reasons, atr_15m=0.5, atr_1d=2.0
        )
        assert passed is False
        assert mode == "區間內做空"
        assert "做空條件三❌[區間內]" in reasons[0]

    def test_breakdown_chase_sub_mode_passes(self) -> None:
        """破位追空：現價 <= Put Wall，改判次級負 Gamma 節點空間 >= 2.0×ATR₁D。"""
        reasons: list = []
        radar = _short_candidate_radar()
        passed, mode = _confirm_short_entry_condition3_downside_room(
            [], radar["gex_profile_data"], 92.0, 97.0, reasons, atr_15m=1.0, atr_1d=4.0
        )
        assert passed is True
        assert mode == "破位追空"
        assert "次級負 Gamma 節點 $80.00" in reasons[0]
        assert "收復 Put Wall $95.00 即論點失效" in reasons[0]

    def test_breakdown_chase_without_next_peak_fails_closed(self) -> None:
        """下方無顯著負 Gamma 節點 -> fail-closed（追空是進攻動作）。"""
        reasons: list = []
        gex = {"put_wall": 95.0, "gex_profile": {"97": 3_000_000.0}}
        passed, mode = _confirm_short_entry_condition3_downside_room(
            [], gex, 92.0, 97.0, reasons, atr_15m=1.0, atr_1d=4.0
        )
        assert passed is False
        assert "fail-closed" in reasons[0]

    def test_whale_put_sto_catch_bid_fails(self) -> None:
        """主力大額 PUT STO 接刀 -> 拋壓路徑被墊住，否決。"""
        reasons: list = []
        uoa = [
            {
                "type": "PUT",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 300_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ]
        radar = _short_candidate_radar()
        passed, _mode = _confirm_short_entry_condition3_downside_room(
            uoa, radar["gex_profile_data"], 92.0, 97.0, reasons, atr_15m=1.0, atr_1d=4.0
        )
        assert passed is False
        assert "PUT STO 接刀" in reasons[0]

    def test_find_next_negative_gex_peak(self) -> None:
        gex = {
            "gex_profile": {
                "82": -2_000_000.0,
                "88": -500_000.0,
                "95": 100_000.0,  # 正值，不得選
                "105": -9_000_000.0,  # 現價上方，不得選
            }
        }
        assert _find_next_negative_gex_peak(gex, 92.0) == 82.0
        assert _find_next_negative_gex_peak({}, 92.0) == 0.0
        assert _find_next_negative_gex_peak(gex, 0.0) == 0.0


# ---------------------------------------------------------------- 條件四
class TestCondition4:
    def test_put_bto_directional_bet_passes(self) -> None:
        reasons: list = []
        assert (
            _confirm_short_entry_condition4_smart_money_pressure(
                _short_candidate_radar()["uoa"], 92.0, reasons
            )
            is True
        )
        assert "PUT BTO 方向性押注" in reasons[0]

    def test_call_sto_capping_passes(self) -> None:
        reasons: list = []
        uoa = [
            {
                "type": "CALL",
                "action": "🔴 賣出開倉 (STO - Bid)",
                "strike": 100.0,
                "ratio": 1.2,
                "notional_value": 250_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ]
        assert (
            _confirm_short_entry_condition4_smart_money_pressure(uoa, 92.0, reasons)
            is True
        )
        assert "CALL STO 上方築頂" in reasons[0]

    def test_deep_otm_put_lottery_ticket_is_excluded(self) -> None:
        """strike < 現價 × 0.85 的深度價外 PUT 是低成本尾部投機，不算機構信心。"""
        reasons: list = []
        uoa = [
            {
                "type": "PUT",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 60.0,  # 92 × 0.85 = 78.2
                "ratio": 3.0,
                "notional_value": 900_000.0,
                "expiry": _FAR_EXPIRY,
            }
        ]
        assert (
            _confirm_short_entry_condition4_smart_money_pressure(uoa, 92.0, reasons)
            is False
        )
        assert "做空條件四❌" in reasons[0]

    def test_short_dte_is_filtered(self) -> None:
        reasons: list = []
        near = (datetime.now().date() + timedelta(days=2)).strftime("%Y-%m-%d")
        uoa = [
            {
                "type": "PUT",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 90.0,
                "ratio": 1.5,
                "notional_value": 400_000.0,
                "expiry": near,
            }
        ]
        assert (
            _confirm_short_entry_condition4_smart_money_pressure(uoa, 92.0, reasons)
            is False
        )

    def test_empty_uoa_fails(self) -> None:
        reasons: list = []
        assert (
            _confirm_short_entry_condition4_smart_money_pressure([], 92.0, reasons)
            is False
        )


# ---------------------------------------------------------------- 條件六
class TestCondition6:
    @pytest.mark.asyncio
    async def test_low_ivr_suggests_long_put(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_FAR_EXPIRY],
        ):
            passed, directive = await _confirm_short_entry_condition6_candidate_dte_ivr(
                "TEST", True, 20.0, reasons
            )
        assert passed is True
        assert directive == "Long Put (輕度 OTM)"

    @pytest.mark.asyncio
    async def test_high_ivr_switches_to_bear_call_spread(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_FAR_EXPIRY],
        ):
            passed, directive = await _confirm_short_entry_condition6_candidate_dte_ivr(
                "TEST", True, 80.0, reasons
            )
        assert passed is True
        assert directive is not None and "Bear Call Spread" in directive

    @pytest.mark.asyncio
    async def test_short_dte_fails(self) -> None:
        reasons: list = []
        near = (datetime.now().date() + timedelta(days=3)).strftime("%Y-%m-%d")
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[near],
        ):
            passed, directive = await _confirm_short_entry_condition6_candidate_dte_ivr(
                "TEST", True, 20.0, reasons
            )
        assert passed is False
        assert directive is None

    @pytest.mark.asyncio
    async def test_short_circuit_returns_true_without_io(self) -> None:
        reasons: list = []
        with patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as mock_exp:
            passed, directive = await _confirm_short_entry_condition6_candidate_dte_ivr(
                "TEST", False, 20.0, reasons
            )
        assert passed is True
        assert directive is None
        mock_exp.assert_not_awaited()
        assert "做空條件六⏭️" in reasons[0]


# ---------------------------------------------------------------- Orchestrator
class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_all_six_conditions_pass(self) -> None:
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKDOWN_DF,
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
                "services.market_data_service.get_all_option_expiries",
                new_callable=AsyncMock,
                return_value=[_FAR_EXPIRY],
            ),
        ):
            confirmed, reason, directive = await _confirm_short_entry_signal(
                "TEST", _short_candidate_radar(), 92.0
            )
        assert confirmed is True
        for i in range(1, 7):
            assert f"做空條件{'一二三四五六'[i - 1]}✅" in reason
        assert directive == "Long Put (輕度 OTM)"

    @pytest.mark.asyncio
    async def test_short_circuit_skips_condition5_and_6(self) -> None:
        radar = _short_candidate_radar()
        radar["uoa"] = []  # 條件四刻意失敗
        with (
            patch(
                "services.market_data_service.get_history_df",
                new_callable=AsyncMock,
                return_value=_BREAKDOWN_DF,
            ),
            patch(
                "market_analysis.vwap_utils.fetch_session_vwap",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            patch(
                "services.market_data_service.get_all_option_expiries",
                new_callable=AsyncMock,
            ) as mock_exp,
        ):
            confirmed, reason, directive = await _confirm_short_entry_signal(
                "TEST", radar, 92.0
            )
        assert confirmed is False
        assert directive is None
        assert "做空條件五⏭️" in reason
        assert "做空條件六⏭️" in reason
        mock_exp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signature_matches_left_and_right_gates(self) -> None:
        """三套鐵律的簽章必須完全一致，呼叫端才能無差別解包。"""
        import inspect

        from market_analysis.dynamic_rollover.left_side_entry import (
            _confirm_left_entry_signal,
        )

        left = inspect.signature(_confirm_left_entry_signal)
        short = inspect.signature(_confirm_short_entry_signal)
        assert list(left.parameters) == list(short.parameters)


def _patched_all_pass_io():  # type: ignore[no-untyped-def]
    """六重鐵律全數通過所需的 I/O patch 組合 (ExitStack)。"""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=_BREAKDOWN_DF,
        )
    )
    stack.enter_context(
        patch(
            "market_analysis.vwap_utils.fetch_session_vwap",
            new_callable=AsyncMock,
            return_value=100.0,
        )
    )
    stack.enter_context(
        patch("database.calendar_cache.get_cached_earnings", return_value=None)
    )
    stack.enter_context(
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value="NORMAL",
        )
    )
    stack.enter_context(
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=[_FAR_EXPIRY],
        )
    )
    return stack


# ---------------------------------------------------------------- 完整評估結果
class TestEvaluateShortEntry:
    @pytest.mark.asyncio
    async def test_breakdown_chase_surfaces_levels(self) -> None:
        """完整評估必須一併回傳下游建立價位所需的中間值，不得讓 SHORT_ENTRY
        情境重新抓取或重算 (確認與下單價位須建立在同一份快照上)。"""
        from market_analysis.dynamic_rollover.short_side_entry import (
            evaluate_short_entry,
        )

        with _patched_all_pass_io():
            ev = await evaluate_short_entry("TEST", _short_candidate_radar(), 92.0)
        assert ev.all_passed is True
        assert ev.sub_mode == "破位追空"
        assert ev.conditions == (True, True, True, True, True, True)
        assert ev.spot == 92.0
        assert ev.resistance_wall == 97.0
        assert ev.put_wall == 95.0
        assert ev.call_wall == 97.0
        assert ev.next_negative_node == 80.0
        # ATR₁₅ₘ 取自條件一實際使用的同一份 15m frame (而非雷達快照)
        assert ev.atr_15m > 0
        assert ev.structure_directive == "Long Put (輕度 OTM)"

    @pytest.mark.asyncio
    async def test_range_mode_has_no_next_node(self) -> None:
        """區間內做空的目標是 Put Wall，next_negative_node 不適用 (0.0)。"""
        from market_analysis.dynamic_rollover.short_side_entry import (
            evaluate_short_entry,
        )

        radar = _short_candidate_radar()
        radar["gex_profile_data"]["put_wall"] = 80.0
        with _patched_all_pass_io():
            ev = await evaluate_short_entry("TEST", radar, 92.0)
        assert ev.sub_mode == "區間內做空"
        assert ev.next_negative_node == 0.0
        assert ev.put_wall == 80.0

    @pytest.mark.asyncio
    async def test_short_circuit_marks_skipped_conditions_none(self) -> None:
        from market_analysis.dynamic_rollover.short_side_entry import (
            evaluate_short_entry,
        )

        radar = _short_candidate_radar()
        radar["uoa"] = []  # 條件四刻意失敗
        with _patched_all_pass_io():
            ev = await evaluate_short_entry("TEST", radar, 92.0)
        assert ev.all_passed is False
        assert ev.conditions[3] is False
        assert ev.conditions[4] is None
        assert ev.conditions[5] is None

    @pytest.mark.asyncio
    async def test_wrapper_matches_full_evaluation(self) -> None:
        from market_analysis.dynamic_rollover.short_side_entry import (
            evaluate_short_entry,
        )

        with _patched_all_pass_io():
            ev = await evaluate_short_entry("TEST", _short_candidate_radar(), 92.0)
        with _patched_all_pass_io():
            triple = await _confirm_short_entry_signal(
                "TEST", _short_candidate_radar(), 92.0
            )
        assert triple == (ev.all_passed, ev.reason, ev.structure_directive)

    @pytest.mark.asyncio
    async def test_condition5_macro_lockout_wording_is_short_specific(self) -> None:
        """宏觀鎖定時做空同樣封鎖，但文案不得寫「嚴禁開倉個股買方」。"""
        with (
            patch("database.calendar_cache.get_cached_earnings", return_value=None),
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value="SHORT_GAMMA_CRITICAL",
            ),
        ):
            from market_analysis.dynamic_rollover.short_side_entry import (
                _confirm_short_entry_condition5_macro_earnings_gate,
            )

            reasons: list = []
            passed = await _confirm_short_entry_condition5_macro_earnings_gate(
                "TEST", True, reasons
            )
        assert passed is False
        joined = " ".join(reasons)
        assert "空單" in joined
        assert "買方" not in joined
