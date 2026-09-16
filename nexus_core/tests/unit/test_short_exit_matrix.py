"""做空部位鏡像 SL/TP 出場矩陣 (anti_washout.py) 單元測試。

驗證四個入口 (_correct_wall_topology / _compute_anti_washout_stop /
TP ladder / SL ladder) 在部位方向為空頭時正確分流至鏡像版，且多頭行為
完全不受影響。
"""

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from market_analysis.dynamic_rollover.structural_signals import (
    _detect_whale_call_bto_block,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


def _short_metrics(**overrides: object) -> dict:
    """做空部位的基準 metrics：股數為負即空頭（沿用 portfolio_monitor.py:445
    的既有慣例，不新增 migration 欄位）。"""
    base: dict = {
        "position_side": "SHORT",
        "quantity": -100.0,
        "spot_price": 92.0,
        "price_15m_close": 92.0,
        "put_wall": 85.0,
        "call_wall": 100.0,
        "resistance_wall": 100.0,
        "support_wall": 85.0,
        "atr_15m": 1.0,
        "lvn": 0.0,
        "hvn": 0.0,
        "dte": 30,
        "net_gex": -800_000.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 方向解析
class TestPositionSideResolution:
    def test_negative_quantity_is_short(self, engine: DynamicRolloverEngine) -> None:
        assert engine._resolve_position_side({"quantity": -100.0}) == "SHORT"

    def test_positive_quantity_is_long(self, engine: DynamicRolloverEngine) -> None:
        assert engine._resolve_position_side({"quantity": 100.0}) == "LONG"

    def test_explicit_side_wins_over_quantity(
        self, engine: DynamicRolloverEngine
    ) -> None:
        assert (
            engine._resolve_position_side({"position_side": "SHORT", "quantity": 5.0})
            == "SHORT"
        )

    def test_missing_quantity_defaults_to_long(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """未標記方向的既有部位一律視為多頭，確保零行為變化。"""
        assert engine._resolve_position_side({}) == "LONG"

    def test_garbage_quantity_defaults_to_long(
        self, engine: DynamicRolloverEngine
    ) -> None:
        assert engine._resolve_position_side({"quantity": "abc"}) == "LONG"


# ---------------------------------------------------------------- 錨點與停損
class TestShortAnchorAndStop:
    def test_anchor_is_resistance_wall(self, engine: DynamicRolloverEngine) -> None:
        anchor, floor = engine._correct_wall_topology(_short_metrics())
        assert anchor == 100.0  # 上方阻力頂牆
        assert floor == 85.0  # 下方支撐地板

    def test_anchor_topology_inversion_takes_higher(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """put_wall > call_wall 的拓撲逆轉：做空取較高者為頂牆
        （多頭版取較低者為底牆，兩者鏡像）。"""
        # support_wall 一併清空，才會落到拓撲逆轉修復分支（否則 support_wall
        # 優先，與多頭版的解析序一致）。
        anchor, floor = engine._correct_wall_topology(
            _short_metrics(
                resistance_wall=0.0,
                support_wall=0.0,
                put_wall=110.0,
                call_wall=95.0,
            )
        )
        assert anchor == 110.0
        assert floor == 95.0

    def test_stop_is_anchor_plus_half_atr(self, engine: DynamicRolloverEngine) -> None:
        stop, limit, extreme = engine._compute_anti_washout_stop(
            100.0, _short_metrics()
        )
        assert stop == 100.5  # 100 + 0.5 × 1.0
        assert extreme == 103.0  # 100 + 3.0 × 1.0
        # 限價單掛在停損上方（買回補），取兩種墊片較近者
        assert limit == pytest.approx(min(100.5 + 0.5, 100.5 * 1.005))

    def test_long_stop_is_unchanged(self, engine: DynamicRolloverEngine) -> None:
        """多頭路徑必須在位元層級完全不變（鏡像方法不得污染既有行為）。"""
        long_metrics = {
            "spot_price": 100.0,
            "atr_15m": 1.0,
            "lvn": 0.0,
            "hvn": 0.0,
            "quantity": 100.0,
        }
        stop, _limit, extreme = engine._compute_anti_washout_stop(80.0, long_metrics)
        assert stop == 79.5  # 80 − 0.5 × 1.0
        assert extreme == 77.0  # 80 − 3.0 × 1.0

    def test_lvn_snaps_upward_for_short(self, engine: DynamicRolloverEngine) -> None:
        """做空的 LVN 吸附方向鏡像：把落在流動性真空的停損往**上**推到次級
        HVN 下緣之下（多頭是往下推到 HVN 上緣之上）。"""
        metrics = _short_metrics(
            atr_15m=6.0,
            lvn=103.0,  # 停損 100 + 0.5×6 = 103.0 正好落在 LVN 上
            secondary_hvn=106.0,
        )
        stop, _limit, _extreme = engine._compute_anti_washout_stop(100.0, metrics)
        assert stop == pytest.approx(106.0 - 0.2 * 6.0)  # 104.8

    def test_ratchet_floor_takes_min_for_short(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """空頭的保本棘輪只會把停損往**下**帶（多頭是往上），故取 min。"""
        stop, _limit, _extreme = engine._compute_anti_washout_stop(
            100.0, _short_metrics(ratchet_stop=97.0)
        )
        assert stop == 97.0


# ---------------------------------------------------------------- TP 階梯
class TestShortTpLadder:
    def test_tp1_at_put_wall(self, engine: DynamicRolloverEngine) -> None:
        tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=85.4)  # <= 85 × 1.005 = 85.425
        )
        assert tier == "TP1"
        assert ratio == 0.5
        assert "TP1-支撐初探" in reason

    def test_tp2_wall_break(self, engine: DynamicRolloverEngine) -> None:
        tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=83.0)  # 跌穿 85 達 2.35% >= 1.5%
        )
        assert tier == "TP2"
        assert ratio == 0.3
        assert "TP2-空間擴展" in reason
        assert "跌穿 Put Wall" in reason

    def test_tp2_wall_migrated_down(self, engine: DynamicRolloverEngine) -> None:
        """做市商支撐牆向下遷移 >= 3% 且現價跌穿舊底牆 -> 釋放下行空間。"""
        tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=88.0, put_wall=87.0, previous_put_wall=95.0)
        )
        assert tier == "TP2"
        assert ratio == 0.3
        assert "向下遷移" in reason

    def test_tp2_wall_migration_requires_spot_below_previous_wall(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """牆已下移但現價尚未跌穿舊底牆時，破位未成立，不得觸發 TP2。"""
        tier, _ratio, _reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=96.0, put_wall=87.0, previous_put_wall=95.0)
        )
        assert tier is None

    def test_tp2_wall_migration_rejected_under_threshold(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """遷移幅度未達 3% (95 -> 93，僅 2.1%) 時，不觸發遷移判定。"""
        tier, _ratio, _reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=94.0, put_wall=93.0, previous_put_wall=95.0)
        )
        assert tier is None

    def test_tp2_upward_migration_must_not_trigger(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """支撐牆向**上**遷移對空頭是逆風，絕不可誤觸發 TP2。"""
        tier, _ratio, _reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(spot_price=96.0, put_wall=95.0, previous_put_wall=85.0)
        )
        assert tier is None

    def test_tp3_deep_negative_delta(self, engine: DynamicRolloverEngine) -> None:
        tier, ratio, reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(delta=-0.9)
        )
        assert tier == "TP3"
        assert ratio == 0.2
        assert "TP3-終局回補" in reason

    def test_tp3_vwap_reclaim_with_volume(self, engine: DynamicRolloverEngine) -> None:
        """空頭的趨勢耗竭訊號是 VWAP 帶量**收復**（多頭是帶量失守）。"""
        tier, _ratio, reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics(vwap_reclaim_with_volume=True)
        )
        assert tier == "TP3"
        assert "15m VWAP 帶量收復" in reason

    def test_no_tp_when_mid_range(self, engine: DynamicRolloverEngine) -> None:
        tier, ratio, _reason = engine._evaluate_microstructure_tp_ladder(
            _short_metrics()
        )
        assert tier is None
        assert ratio == 0.0


# ---------------------------------------------------------------- SL 階梯
class TestShortSlLadder:
    def test_structural_break_on_upward_penetration(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """做空的結構失效是現價**升穿**停損（多頭是跌破）。"""
        tier, ratio, reason, new_stop = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(spot_price=101.0), 100.0, 100.5, "OPTIONS"
        )
        assert tier == "SL_STRUCTURAL"
        assert ratio == 1.0
        assert "升穿" in reason
        assert new_stop is None

    def test_spot_track_waits_for_15m_close(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """SPOT 軌道等 15m 實體收盤，現貨瞬時穿刺不觸發。"""
        tier, _ratio, _reason, _new = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(spot_price=101.0, price_15m_close=99.0),
            100.0,
            100.5,
            "SPOT",
        )
        assert tier != "SL_STRUCTURAL"

    def test_regime_flip_on_positive_net_gex(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """Net GEX 回正 -> 做市商買跌賣漲吸收波動，順勢助跌路徑消失。"""
        tier, ratio, reason, _new = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(net_gex=500_000.0), 100.0, 100.5, "OPTIONS"
        )
        assert tier == "SL_REGIME_FLIP"
        assert ratio == 1.0
        assert "回正" in reason

    def test_missing_net_gex_does_not_trigger(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """資料缺失 (None) 不得被誤判為「已確認正 Gamma」而強制回補。"""
        tier, _ratio, _reason, _new = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(net_gex=None), 100.0, 100.5, "OPTIONS"
        )
        assert tier != "SL_REGIME_FLIP"

    def test_whale_call_block_forces_cover(self, engine: DynamicRolloverEngine) -> None:
        tier, ratio, reason, _new = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(is_whale_call_block=True), 100.0, 100.5, "OPTIONS"
        )
        assert tier == "SL_WHALE_CALL"
        assert ratio == 1.0
        assert "逼空風險" in reason

    def test_trailing_breakeven_moves_stop_down(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """現價跌幅達距 Put Wall 空間之 50% -> 停損下移至保本點。"""
        # anchor 100、put_wall 85、spot 92.5 -> progress = 7.5/15 = 50%
        tier, ratio, reason, new_stop = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(spot_price=92.5, avg_cost=95.0), 100.0, 105.0, "OPTIONS"
        )
        assert tier == "SL_TRAILING_BREAKEVEN"
        assert ratio == 0.0
        assert new_stop == 95.0  # min(avg_cost, anchor)
        assert "停損下移至保本點" in reason

    def test_no_sl_when_healthy(self, engine: DynamicRolloverEngine) -> None:
        tier, _ratio, _reason, _new = engine._evaluate_microstructure_sl_ladder(
            _short_metrics(spot_price=97.0), 100.0, 100.5, "OPTIONS"
        )
        assert tier is None


# ---------------------------------------------------------------- 巨鯨偵測器
class TestWhaleCallBtoDetector:
    def test_detects_near_atm_call_bto(self) -> None:
        uoa = [
            {
                "type": "CALL",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 93.0,
                "paced_ratio": 2.0,
                "notional_value": 800_000.0,
            }
        ]
        assert _detect_whale_call_bto_block(uoa, 92.0) is True

    def test_put_bto_is_ignored(self) -> None:
        """PUT BTO 是多頭部位的對沖訊號，不得觸發空頭的 SL-主力對沖。"""
        uoa = [
            {
                "type": "PUT",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 93.0,
                "paced_ratio": 2.0,
                "notional_value": 800_000.0,
            }
        ]
        assert _detect_whale_call_bto_block(uoa, 92.0) is False

    def test_far_otm_call_is_ignored(self) -> None:
        uoa = [
            {
                "type": "CALL",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 130.0,  # |130-92|/92 = 41% > 5% 近平值容差
                "paced_ratio": 2.0,
                "notional_value": 800_000.0,
            }
        ]
        assert _detect_whale_call_bto_block(uoa, 92.0) is False

    def test_below_notional_threshold_is_ignored(self) -> None:
        uoa = [
            {
                "type": "CALL",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 93.0,
                "paced_ratio": 2.0,
                "notional_value": 100_000.0,
            }
        ]
        assert _detect_whale_call_bto_block(uoa, 92.0) is False

    def test_empty_list_fails_safe(self) -> None:
        assert _detect_whale_call_bto_block([], 92.0) is False
        assert _detect_whale_call_bto_block(None, 92.0) is False


# ---------------------------------------------------------------- 資料通路
class TestPreviousPutWallPersistence:
    """`previous_put_wall` 的資料通路必須完整，否則做空 TP2 的牆體遷移分支
    會永遠沉默地退回 1.5% 跌破判定（這正是本次實作前的實際狀態——v069 只加了
    call_wall 側，沒有對應的 put_wall 側）。"""

    def test_migration_v074_registers_both_columns(self) -> None:
        from database.migrations import v074_add_previous_put_wall as mig

        # database/core.py::get_migrations() 只收錄同時具備 version /
        # description / sql 三個模組層級屬性的模組，其餘一律「無聲跳過」。
        assert mig.version == 74
        assert isinstance(mig.description, str) and mig.description
        assert hasattr(mig, "sql")

    def test_migration_v074_is_idempotent(self) -> None:
        import sqlite3

        from database.migrations import v074_add_previous_put_wall as mig

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE market_cache (symbol TEXT)")
            mig.migrate_data(conn)
            mig.migrate_data(conn)  # 重複執行不得拋例外
            cols = {r[1] for r in conn.execute("PRAGMA table_info(market_cache)")}
            assert {"put_wall", "previous_put_wall"} <= cols
        finally:
            conn.close()

    def test_save_market_cache_accepts_put_wall_kwargs(self) -> None:
        import inspect

        from database.market_cache import save_market_cache

        params = inspect.signature(save_market_cache).parameters
        assert "put_wall" in params
        assert "previous_put_wall" in params
        # 與 call_wall 側完全對稱
        assert params["put_wall"].default is None
        assert params["previous_put_wall"].default is None
