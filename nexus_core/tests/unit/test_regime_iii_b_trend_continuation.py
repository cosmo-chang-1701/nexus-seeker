"""Regime III-B 趨勢延續進場路徑（handoff.md §4 / 階段 2）。

本路徑的存在理由是 handoff.md §1.3 的診斷：右側六重鐵律有兩項要求「特定瞬間」
發生的事件（條件一的放量實體陽線、條件四的當下 UOA），而趨勢的續航段是縮量、
陰陽交錯的——錯過啟動那一根就整波進不去。III-B 只放寬這兩項，其餘四項風控逐字不動。

最高優先測項（若其一失效，整條路徑不是失效就是危險）：
  * `test_regime_iii_takes_priority_over_iii_b` —— III-B 若搶在 III 之前，突破態會被
    降級成放寬態，UOA 時間窗會被錯誤套用到突破上。
  * `test_gate_condition1_strict_mode_rejects_same_data` —— 證明通過是「放寬」造成的，
    不是 fixture 本來就會過；沒有這支，條件一的放寬等於沒被測到。
  * `test_condition4_lookback_recomputes_dte_against_today` / `..._strike_against_spot`
    —— 時間窗只放寬「何時觀測到」，不放寬「現在是否仍然成立」。
"""

from datetime import datetime, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.dynamic_rollover.models import DynamicRegime
from market_analysis.dynamic_rollover.opportunity_cost import (
    _confirm_entry_condition1_breakout,
    _confirm_entry_condition4_uoa_dte,
)
from market_analysis.dynamic_rollover.regime_classifier import classify_dynamic_regime
from market_analysis.dynamic_rollover.structural_signals import (
    count_structure_held_bars,
)


def _make_15m_df(
    closes: list[float],
    volumes: Optional[list[float]] = None,
    start: str = "2024-01-02 09:30",
) -> pd.DataFrame:
    """比照 test_regime_classifier.py::_make_15m_df 的索引語意（每根為起始時間、
    固定錨在過去以確保最後一根已收盤）。open 一律等於 close，使最後一根為十字線
    ——III-B 刻意不要求實體陽線，讓 fixture 明確地**不滿足** Regime III。"""
    vols = volumes if volumes is not None else [1000.0] * len(closes)
    return pd.DataFrame(
        {
            "Open": list(closes),
            "High": [c + 0.5 for c in closes],
            "Low": [c - 0.5 for c in closes],
            "Close": list(closes),
            "Volume": vols,
        },
        index=pd.date_range(start, periods=len(closes), freq="15min"),
    )


# 緩步上行、陰陽交錯的續航段：RSI ≈ 74.7（落在 III-B 的 50 < RSI < 78 區間內），
# ATR₁₅ₘ ≈ 1.15，量能全程平盤（無放量 → Regime III 必然不成立）。
# 最後 6 根中有 5 根收盤 > 98（= max(GammaFlip 95, VWAP 98)），97.6 那根是刻意
# 保留的洗盤針，用來驗證 5/6 容差確實生效。
_TREND_CLOSES = [
    95.0,
    95.6,
    95.2,
    96.0,
    95.7,
    96.4,
    96.1,
    96.9,
    96.5,
    97.3,
    97.0,
    97.8,
    97.4,
    98.2,
    97.9,
    98.6,
    98.3,
    97.6,
    98.9,
    99.2,
    99.0,
]
_TREND_DF = _make_15m_df(_TREND_CLOSES)

# 同一條路徑但只有 4/6 根站穩（把 98.3 與 98.6 打到門檻以下）。
_WEAK_CLOSES = _TREND_CLOSES[:15] + [97.1, 97.2, 97.6, 98.9, 99.2, 99.0]
_WEAK_DF = _make_15m_df(_WEAK_CLOSES)


def _gex_iii_b() -> dict:
    """Gamma Flip 估算 = 95.0、現價下方支撐牆 = 95.0、Call Wall = 120。

    以 spot 100 / ATR₁₅ₘ≈1.15 / ATR₁D=3.0 計算：
      動態空間門檻 = max(2.2 × 3.57%, 1.5 × 3%, 3.5%) ≈ 7.9%，Call Wall 空間 20% 通過；
      支撐牆停損距離 ≈ 5.6%，落在 [2.5 × 1.15/100 = 2.9%, 絕對 8%] 之內。
    net_gex 為正，滿足 III-B 條件二「做市商未翻負」。
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


def _patch_classifier_io(
    df: Any = _TREND_DF,
    session_vwap: float = 98.0,
    macro_regime: str = "NORMAL",
) -> Any:
    return (
        patch(
            "market_analysis.index_microstructure.get_market_regime",
            new_callable=AsyncMock,
            return_value=macro_regime,
        ),
        patch(
            "services.market_data_service.get_history_df",
            new_callable=AsyncMock,
            return_value=df,
        ),
        patch(
            "services.market_data_service.get_vix_term_structure",
            new_callable=AsyncMock,
            return_value={"vts_ratio": 0.9},
        ),
        patch(
            "market_analysis.vwap_utils.fetch_session_vwap",
            new_callable=AsyncMock,
            return_value=session_vwap,
        ),
        patch(
            "market_analysis.atr_utils.fetch_atr_1d",
            new_callable=AsyncMock,
            return_value=3.0,
        ),
        # 遠高於 spot，避免意外觸發晴空萬里擴展而讓天花板變成動態值
        # （本檔案測的是 III-B，不是公式 D）。
        patch(
            "market_analysis.atr_utils.fetch_high_60d",
            new_callable=AsyncMock,
            return_value=1_000_000.0,
        ),
    )


async def _classify(
    spot: float = 100.0,
    gex: Optional[dict] = None,
    df: Any = _TREND_DF,
    session_vwap: float = 98.0,
    macro_regime: str = "NORMAL",
    uoa: Optional[list] = None,
) -> tuple[DynamicRegime, str]:
    patches = _patch_classifier_io(df, session_vwap, macro_regime)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        regime, reason, _md = await classify_dynamic_regime(
            "TEST", spot, gex if gex is not None else _gex_iii_b(), uoa or []
        )
    return regime, reason


# ---------------------------------------------------------------------------
# 1. count_structure_held_bars（路由層與進場確認層共用的演算法）
# ---------------------------------------------------------------------------
class TestCountStructureHeldBars:
    def test_counts_bars_above_both_levels(self) -> None:
        held, window, same_session = count_structure_held_bars(_TREND_DF, 95.0, 98.0)
        assert (held, window, same_session) == (5, 6, True)

    def test_uses_the_higher_of_the_two_levels(self) -> None:
        """門檻是 max(結構分界線, VWAP)——只站穩其中較低的一條不算數。"""
        held, _window, _same = count_structure_held_bars(_TREND_DF, 99.5, 98.0)
        assert held == 0

    def test_spanning_two_sessions_fails_safe(self) -> None:
        """跨交易時段時 session_vwap 這個純量對舊 K 棒沒有意義，一律不成立。"""
        day1 = _make_15m_df([99.0] * 3, start="2024-01-02 15:15")
        day2 = _make_15m_df([99.0] * 3, start="2024-01-03 09:30")
        spanning = pd.concat([day1, day2])
        held, window, same_session = count_structure_held_bars(spanning, 95.0, 98.0)
        assert same_session is False
        assert (held, window) == (0, 6)

    def test_insufficient_bars_fails_safe(self) -> None:
        held, window, same_session = count_structure_held_bars(
            _make_15m_df([99.0] * 4), 95.0, 98.0
        )
        assert (held, window, same_session) == (0, 4, False)

    def test_missing_inputs_fail_safe(self) -> None:
        assert count_structure_held_bars(None, 95.0, 98.0) == (0, 0, False)
        assert count_structure_held_bars(_TREND_DF, 0.0, 98.0) == (0, 0, False)
        assert count_structure_held_bars(_TREND_DF, 95.0, 0.0) == (0, 0, False)


# ---------------------------------------------------------------------------
# 2. 分類器：Regime III-B 的判定與優先序
# ---------------------------------------------------------------------------
class TestRegimeIIIBClassification:
    @pytest.mark.asyncio
    async def test_sustained_structure_classifies_regime_iii_b(self) -> None:
        regime, reason = await _classify()
        assert regime == DynamicRegime.REGIME_III_B_TREND_CONTINUATION
        assert "趨勢延續確認" in reason
        assert "5 根同時站穩" in reason

    @pytest.mark.asyncio
    async def test_regime_iii_takes_priority_over_iii_b(self) -> None:
        """⚠️ 最高優先測項：突破當下兩者的條件都會成立，必須歸類為 Regime III。

        若優先序反了，突破態會被降級成放寬態，連帶把條件四的 5 日 UOA 回看窗
        錯誤地套用到「應該要求主力此刻正在表態」的突破進場上。
        """
        # 在同一條趨勢的最後補上一根放量實體陽線 → Regime III 的量價條件成立
        breakout_df = _make_15m_df(
            _TREND_CLOSES + [101.0],
            volumes=[1000.0] * len(_TREND_CLOSES) + [3000.0],
        )
        breakout_df.iloc[-1, breakout_df.columns.get_loc("Open")] = 99.0
        regime, reason = await _classify(df=breakout_df)
        assert regime == DynamicRegime.REGIME_III_RIGHT_MOMENTUM
        assert "結構突破" in reason

    @pytest.mark.asyncio
    async def test_only_four_of_six_held_is_not_iii_b(self) -> None:
        regime, _reason = await _classify(df=_WEAK_DF)
        assert regime != DynamicRegime.REGIME_III_B_TREND_CONTINUATION

    @pytest.mark.asyncio
    async def test_non_positive_net_gex_is_not_iii_b(self) -> None:
        """做市商已翻入負 Gamma 時，「趨勢」隨時可能變成順勢踩踏。"""
        gex = _gex_iii_b()
        gex["net_gex"] = -100.0
        gex["gex_profile"] = {"90": -900_000.0, "95": 800_000.0, "105": -200_000.0}
        regime, _reason = await _classify(gex=gex)
        assert regime != DynamicRegime.REGIME_III_B_TREND_CONTINUATION

    @pytest.mark.asyncio
    async def test_missing_net_gex_fails_safe(self) -> None:
        """net_gex 與 gex_profile 皆不可得時不得以 0.0 冒充「已知為非負」。"""
        gex = {"call_wall": 120.0, "put_wall": 97.0, "gex_profile": None}
        regime, _reason = await _classify(gex=gex)
        assert regime != DynamicRegime.REGIME_III_B_TREND_CONTINUATION

    @pytest.mark.asyncio
    async def test_overbought_rsi_is_not_iii_b(self) -> None:
        """RSI 上限防在超買頂部追高——這正是趨勢延續路徑最危險的失效模式。"""
        hot = [95.0 + 0.4 * i for i in range(21)]
        regime, _reason = await _classify(df=_make_15m_df(hot), spot=103.0)
        assert regime != DynamicRegime.REGIME_III_B_TREND_CONTINUATION

    @pytest.mark.asyncio
    async def test_structural_cap_still_takes_priority(self) -> None:
        """Regime IV 個股結構封頂排在 III-B 之前：上方沒空間時不論趨勢多完好都不進場。"""
        gex = _gex_iii_b()
        gex["call_wall"] = 101.0
        regime, _reason = await _classify(gex=gex)
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS

    @pytest.mark.asyncio
    async def test_macro_lockout_still_takes_priority(self) -> None:
        regime, _reason = await _classify(macro_regime="SHORT_GAMMA_CRITICAL")
        assert regime == DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS


# ---------------------------------------------------------------------------
# 3. 前向蒐集：III-B 必須被記成「多頭、且放行」
# ---------------------------------------------------------------------------
class TestForwardCollection:
    @pytest.mark.asyncio
    async def test_iii_b_recorded_as_long_and_decision_one(self) -> None:
        """漏記 = 校準資料靜默歸零：`calibration forward-report` 依 decision 分組
        統計勝率，III-B 若被記成 decision=0 就永遠進不了「放行樣本」那一組，
        而那正是 §5.8 判定能否翻轉 DRY_RUN 所依據的唯一證據。"""
        from market_analysis import evaluation_recorder

        evaluation_recorder.clear_buffer()
        with evaluation_recorder.evaluation_source("UNIT_TEST"):
            regime, _reason = await _classify()
        assert regime == DynamicRegime.REGIME_III_B_TREND_CONTINUATION
        assert evaluation_recorder.pending_count() == 1
        row = evaluation_recorder._BUFFER[0]
        evaluation_recorder.clear_buffer()
        assert row["regime"] == "REGIME_III_B_TREND_CONTINUATION"
        assert row["direction"] == "LONG"
        assert row["decision"] == 1


# ---------------------------------------------------------------------------
# 4. 六重鐵律條件一：放量實體陽線 → 持續站穩
# ---------------------------------------------------------------------------
class TestGateCondition1:
    @pytest.mark.asyncio
    async def test_trend_mode_passes_without_volume_surge_or_bullish_candle(
        self,
    ) -> None:
        reasons: list[str] = []
        passed = await _confirm_entry_condition1_breakout(
            "TEST",
            100.0,
            _gex_iii_b()["gex_profile"],
            reasons,
            net_gex=800_000.0,
            df_15m=_TREND_DF,
            session_vwap=98.0,
            trend_continuation=True,
        )
        assert passed is True
        assert "[趨勢延續]" in reasons[0]
        assert "本路徑不要求放量與實體陽線" in reasons[0]

    @pytest.mark.asyncio
    async def test_strict_mode_rejects_same_data(self) -> None:
        """⚠️ 最高優先測項：同一份資料在嚴格模式下必須**不通過**。

        沒有這支測試，上一支的「通過」可能只是因為 fixture 本身就滿足舊條件，
        條件一的放寬等於完全沒被驗證到。
        """
        reasons: list[str] = []
        passed = await _confirm_entry_condition1_breakout(
            "TEST",
            100.0,
            _gex_iii_b()["gex_profile"],
            reasons,
            net_gex=800_000.0,
            df_15m=_TREND_DF,
            session_vwap=98.0,
        )
        assert passed is False
        assert "[趨勢延續]" not in reasons[0]

    @pytest.mark.asyncio
    async def test_trend_mode_rejects_insufficient_held_bars(self) -> None:
        reasons: list[str] = []
        passed = await _confirm_entry_condition1_breakout(
            "TEST",
            100.0,
            _gex_iii_b()["gex_profile"],
            reasons,
            net_gex=800_000.0,
            df_15m=_WEAK_DF,
            session_vwap=98.0,
            trend_continuation=True,
        )
        assert passed is False

    @pytest.mark.asyncio
    async def test_trend_mode_still_requires_standing_above_vwap(self) -> None:
        """站穩結構與站穩 VWAP 兩項**不放寬**——放寬等於容許在負 Gamma 區追多。"""
        reasons: list[str] = []
        passed = await _confirm_entry_condition1_breakout(
            "TEST",
            100.0,
            _gex_iii_b()["gex_profile"],
            reasons,
            net_gex=800_000.0,
            df_15m=_TREND_DF,
            session_vwap=120.0,
            trend_continuation=True,
        )
        assert passed is False


# ---------------------------------------------------------------------------
# 5. 六重鐵律條件四：UOA 5 交易日回看窗
# ---------------------------------------------------------------------------
def _uoa(
    days_to_expiry: int = 30,
    strike: float = 105.0,
    ratio: float = 1.2,
    notional: float = 500_000.0,
    action: str = "BTO",
    opt_type: str = "CALL",
) -> dict:
    return {
        "type": opt_type,
        "action": action,
        "ratio": ratio,
        "notional_value": notional,
        "strike": strike,
        "expiry": (datetime.now().date() + timedelta(days=days_to_expiry)).strftime(
            "%Y-%m-%d"
        ),
    }


class TestGateCondition4Lookback:
    def test_historical_record_satisfies_condition(self) -> None:
        reasons: list[str] = []
        assert (
            _confirm_entry_condition4_uoa_dte(
                [], 100.0, reasons, historical_uoa=[_uoa()]
            )
            is True
        )
        assert "回看窗" in reasons[0]

    def test_without_lookback_same_data_fails(self) -> None:
        """Regime III（突破態）不傳歷史清單，語意維持「當下必須存在」。"""
        reasons: list[str] = []
        assert _confirm_entry_condition4_uoa_dte([], 100.0, reasons) is False
        assert "回看窗" not in reasons[0]

    def test_recomputes_dte_against_today(self) -> None:
        """5 天前的 DTE 14 合約今天只剩 9 天；若今天已跌破門檻就不該再算數。
        時間窗放寬的是「何時觀測到」，不是「現在是否仍然成立」。"""
        reasons: list[str] = []
        assert (
            _confirm_entry_condition4_uoa_dte(
                [], 100.0, reasons, historical_uoa=[_uoa(days_to_expiry=3)]
            )
            is False
        )

    def test_recomputes_strike_against_current_spot(self) -> None:
        """主力當初買的價外 Call，若現價已衝過該履約價，那筆買盤已完成使命，
        不構成對「再往上」的背書。"""
        reasons: list[str] = []
        assert (
            _confirm_entry_condition4_uoa_dte(
                [], 120.0, reasons, historical_uoa=[_uoa(strike=105.0)]
            )
            is False
        )

    def test_historical_records_still_face_all_four_filters(self) -> None:
        reasons: list[str] = []
        assert (
            _confirm_entry_condition4_uoa_dte(
                [],
                100.0,
                reasons,
                historical_uoa=[
                    _uoa(action="STO"),
                    _uoa(opt_type="PUT"),
                    _uoa(ratio=0.1),
                    _uoa(notional=1_000.0),
                ],
            )
            is False
        )

    def test_live_snapshot_takes_precedence_in_reason_text(self) -> None:
        reasons: list[str] = []
        assert (
            _confirm_entry_condition4_uoa_dte(
                [_uoa()], 100.0, reasons, historical_uoa=[_uoa(notional=900_000.0)]
            )
            is True
        )
        assert "回看窗" in reasons[0]


# ---------------------------------------------------------------------------
# 6. uoa_history 存取層
# ---------------------------------------------------------------------------
class TestUoaHistoryRows:
    def test_maps_detect_uoa_payload(self) -> None:
        from database.uoa_history import _to_rows

        rows = _to_rows("aaa", "2026-09-18T10:00:00", [_uoa()])
        assert len(rows) == 1
        assert rows[0][1] == "AAA"
        assert rows[0][4] == "CALL"

    def test_skips_unparsable_entries(self) -> None:
        from database.uoa_history import _to_rows

        # 型別刻意混雜：本函式的輸入來自 detect_uoa 的回傳值，上游若改版
        # 或抓取失敗都可能塞進非預期型別，記錄器不得因此拋例外。
        malformed: list[Any] = [
            "not-a-dict",
            {"type": "CALL"},  # 缺 expiry / action
            {**_uoa(), "strike": 0.0},  # strike 非正
            {**_uoa(), "strike": "bad"},  # 無法轉 float
        ]
        rows = _to_rows("AAA", "2026-09-18T10:00:00", malformed)
        assert rows == []


# ---------------------------------------------------------------------------
# 7. 派發端乾跑閘門（跨情境，以 entry_regime 為鍵）
# ---------------------------------------------------------------------------
def _iii_b_instruction() -> dict:
    return {
        "symbol": "BBB",
        "action": "REDUCE",
        "sell_ratio": 0.5,
        "target_core": "AAA",
        "reason": "💡 機會成本轉倉",
        "suggested_strategy": "Buy Shares",
        "structure_directive": None,
        "scenario": "OPPORTUNITY_COST",
        "instrument_type": "SPOT",
        "limit_price": 100.0,
        "is_manual_override_required": False,
        "cash_impact": None,
        "entry_regime": DynamicRegime.REGIME_III_B_TREND_CONTINUATION.value,
    }


async def _run_iii_b_cycle(dry_run: bool) -> tuple[Any, Any]:
    import time as _time

    from cogs.trading.portfolio_monitor import PortfolioMonitorCog
    from tests.unit.test_trading_output import _mock_all_rollover_scenarios

    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)
    bot._latest_radar_data_cache = {"AAA": {"quote": {"c": 100.0}}}
    bot._latest_radar_cache_time = _time.time()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)
    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[_iii_b_instruction()]
    )

    with (
        patch(
            "cogs.trading.portfolio_monitor.market_time.is_market_open",
            return_value=True,
        ),
        patch("services.llm_service.is_memory_safe", return_value=True),
        patch(
            "database.holdings.get_all_holdings",
            return_value=[
                {
                    "id": 1,
                    "user_id": 7,
                    "symbol": "BBB",
                    "quantity": 100,
                    "avg_cost": 90.0,
                    "asset_class": "SATELLITE",
                }
            ],
        ),
        patch("database.get_user_ids_by_trading_strategy", return_value=[]),
        patch(
            "services.market_data_service.get_vix_spot_strict",
            new_callable=AsyncMock,
            return_value=20.0,
        ),
        patch("database.watchlist.get_user_watchlist", return_value=[]),
        patch(
            "market_analysis.trading_orchestration.recommend_covered_calls",
            new_callable=AsyncMock,
            return_value={"recommendations": []},
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("database.get_kv_cache", return_value=None),
        patch("database.save_kv_cache", new_callable=AsyncMock),
        patch(
            "database.log_rollover_instruction", new_callable=AsyncMock
        ) as mock_audit,
        patch(
            "cogs.trading.portfolio_monitor.create_dynamic_rollover_embed",
            # 必須是可 setattr 的物件：派發迴圈會掛上 `_view` 決定要渲染
            # RolloverActionView 還是 ManualOverrideView。
            return_value=MagicMock(),
        ),
        patch("cogs.trading.portfolio_monitor.config.REGIME_III_B_DRY_RUN", dry_run),
    ):
        await cog.monitor_real_portfolio_task()
    return bot, mock_audit


class TestDryRunGate:
    @pytest.mark.asyncio
    async def test_dry_run_suppresses_dm_but_keeps_audit(self) -> None:
        bot, mock_audit = await _run_iii_b_cycle(dry_run=True)
        bot.queue_dm.assert_not_awaited()
        mock_audit.assert_awaited()

    @pytest.mark.asyncio
    async def test_flag_off_delivers_dm(self) -> None:
        """證明上一支的「不推播」是乾跑旗標造成的，而不是測試環境本來就不會推播。"""
        bot, _mock_audit = await _run_iii_b_cycle(dry_run=False)
        bot.queue_dm.assert_awaited()
