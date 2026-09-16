"""SHORT_ENTRY 情境 (dynamic_rollover/short_entry_deployment.py) 與做空候選來源
(opportunity_cost._find_best_short_target) 單元測試。"""

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine, ShortCandidateInput
from market_analysis.dynamic_rollover.models import DynamicRegime, RegimeMarketData
from tests.unit.short_entry_helpers import make_short_entry_evaluation

_DEPLOY = "market_analysis.dynamic_rollover.short_entry_deployment"


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


def _bearish_radar(spot: float = 100.0) -> dict:
    return {
        "quote": {"c": spot},
        "psq_result": {"squeeze_level": "Release", "signal_direction": "Short"},
        "gex_profile_data": {},
        "uoa": [],
    }


def _ctx(strategy: str = "SHORT_SIDE", capital: float = 100_000.0) -> MagicMock:
    return MagicMock(trading_strategy=strategy, capital=capital, risk_limit=15.0)


# ---------------------------------------------------------------- 候選來源
class TestFindBestShortTarget:
    @staticmethod
    def _run(
        engine: DynamicRolloverEngine,
        watchlist: list[str],
        radar: dict,
        cache: Optional[dict] = None,
        earnings: Optional[dict] = None,
        exclude: Optional[set] = None,
    ) -> Optional[str]:
        default_row = {
            "reference_spot_price": 100.0,
            "expected_move_lower": 90.0,  # 下行預期波幅 10%
            "is_stale": False,
            "is_degraded": False,
        }
        cache = cache or {}
        with (
            patch(
                "database.watchlist.get_user_watchlist",
                return_value=[(s, 0.0) for s in watchlist],
            ),
            patch(
                "database.calendar_cache.get_cached_earnings",
                side_effect=lambda s: (earnings or {}).get(s),
            ),
            patch(
                "database.market_cache.get_market_cache",
                side_effect=lambda s: cache.get(s, default_row),
            ),
        ):
            return engine._find_best_short_target(1, exclude or set(), radar)

    def test_picks_bearish_candidate(self, engine: DynamicRolloverEngine) -> None:
        radar = {"AAA": _bearish_radar()}
        assert self._run(engine, ["AAA"], radar) == "AAA"

    def test_excludes_holdings_core_etfs_and_inverse_etfs(
        self, engine: DynamicRolloverEngine
    ) -> None:
        radar = {s: _bearish_radar() for s in ("AAA", "VOO", "SQQQ", "SH")}
        assert (
            self._run(engine, ["AAA", "VOO", "SQQQ", "SH"], radar, exclude={"aaa"})
            is None
        )

    def test_excludes_symbol_without_radar_snapshot(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """零網路 I/O：快照裡沒有的標的直接略過，不為挑候選逐檔抓雷達。"""
        assert self._run(engine, ["AAA"], {}) is None

    def test_bullish_momentum_is_filtered(self, engine: DynamicRolloverEngine) -> None:
        radar = {
            "AAA": {"psq_result": {"squeeze_level": "High", "signal_direction": "Long"}}
        }
        assert self._run(engine, ["AAA"], radar) is None

    def test_stale_cache_and_small_move_filtered(
        self, engine: DynamicRolloverEngine
    ) -> None:
        radar = {"AAA": _bearish_radar(), "BBB": _bearish_radar()}
        cache = {
            "AAA": {
                "reference_spot_price": 100.0,
                "expected_move_lower": 90.0,
                "is_stale": True,
            },
            "BBB": {"reference_spot_price": 100.0, "expected_move_lower": 97.0},
        }
        assert self._run(engine, ["AAA", "BBB"], radar, cache=cache) is None

    def test_earnings_buffer_excluded(self, engine: DynamicRolloverEngine) -> None:
        from datetime import datetime, timedelta

        soon = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
        radar = {"AAA": _bearish_radar()}
        assert (
            self._run(engine, ["AAA"], radar, earnings={"AAA": {"earnings_date": soon}})
            is None
        )

    def test_ranks_by_weaker_momentum(self, engine: DynamicRolloverEngine) -> None:
        """同樣的下行空間，動能越弱 (PSQ 越低) 排序越前。"""
        radar = {
            "WEAK": _bearish_radar(),  # Release + Short -> 5
            "MILD": {
                "psq_result": {"squeeze_level": "Normal", "signal_direction": "Short"}
            },  # 20
        }
        assert self._run(engine, ["MILD", "WEAK"], radar) == "WEAK"


# ---------------------------------------------------------------- 情境
class TestShortEntryOpportunity:
    @pytest.mark.asyncio
    async def test_confirmed_short_produces_single_instruction(
        self, engine: DynamicRolloverEngine
    ) -> None:
        ev = make_short_entry_evaluation()
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx()),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
                return_value=ev,
            ),
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1, [ShortCandidateInput("aaa", _bearish_radar())], vix_spot=20.0
            )
        assert len(out) == 1
        ins = out[0]
        assert ins["symbol"] == "AAA"
        assert ins["scenario"] == "SHORT_ENTRY"
        assert ins["action"] == "OPEN_SHORT"
        assert ins["direction"] == "SHORT"
        assert ins["sell_ratio"] == 0.0
        assert "Buy" not in str(ins.get("suggested_strategy"))
        plan: Any = ins.get("short_entry_plan")
        assert plan is not None
        assert plan["share_qty"] > 0
        assert plan["stop_price"] > plan["entry_price"] > plan["target_price"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", ["RIGHT_SIDE", "LEFT_SIDE"])
    async def test_long_strategies_never_evaluated(
        self, engine: DynamicRolloverEngine, strategy: str
    ) -> None:
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx(strategy)),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
            ) as mock_eval,
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1, [ShortCandidateInput("AAA", _bearish_radar())], vix_spot=20.0
            )
        assert out == []
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_margin_defense_suppresses(
        self, engine: DynamicRolloverEngine
    ) -> None:
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx()),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
            ) as mock_eval,
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1,
                [ShortCandidateInput("AAA", _bearish_radar())],
                vix_spot=20.0,
                suppress_reason="MARGIN_DEFENSE",
            )
        assert out == []
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vix_extreme_blocks_before_evaluation(
        self, engine: DynamicRolloverEngine
    ) -> None:
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx()),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
            ) as mock_eval,
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1, [ShortCandidateInput("AAA", _bearish_radar())], vix_spot=38.0
            )
        assert out == []
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_most_one_instruction_best_reward_risk(
        self, engine: DynamicRolloverEngine
    ) -> None:
        good = make_short_entry_evaluation(put_wall=85.0)  # R:R 更佳
        ok = make_short_entry_evaluation()
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx()),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
                side_effect=[ok, good],
            ),
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1,
                [
                    ShortCandidateInput("AAA", _bearish_radar()),
                    ShortCandidateInput("BBB", _bearish_radar()),
                ],
                vix_spot=20.0,
            )
        assert len(out) == 1
        assert out[0]["symbol"] == "BBB"

    @pytest.mark.asyncio
    async def test_precomputed_evaluation_is_reused(
        self, engine: DynamicRolloverEngine
    ) -> None:
        ev = make_short_entry_evaluation()
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx("DYNAMIC")),
            patch(
                "market_analysis.dynamic_rollover.regime_classifier.classify_dynamic_regime",
                new_callable=AsyncMock,
            ) as mock_classify,
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
            ) as mock_eval,
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1,
                [
                    ShortCandidateInput(
                        "AAA",
                        _bearish_radar(),
                        precomputed=ev,
                        entry_regime="REGIME_V_BREAKDOWN_CHASE",
                    )
                ],
                vix_spot=20.0,
            )
        assert len(out) == 1
        assert out[0].get("entry_regime") == "REGIME_V_BREAKDOWN_CHASE"
        mock_classify.assert_not_awaited()
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dynamic_non_regime_v_never_reaches_short_gate(
        self, engine: DynamicRolloverEngine
    ) -> None:
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx("DYNAMIC")),
            patch(
                "market_analysis.dynamic_rollover.regime_classifier.classify_dynamic_regime",
                new_callable=AsyncMock,
                return_value=(
                    DynamicRegime.REGIME_II_CHAOS_STANDASIDE,
                    "混沌",
                    RegimeMarketData(),
                ),
            ),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
            ) as mock_eval,
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1, [ShortCandidateInput("AAA", _bearish_radar())], vix_spot=20.0
            )
        assert out == []
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_evaluation_memoized_across_users(
        self, engine: DynamicRolloverEngine
    ) -> None:
        """做空評估與使用者無關：同一 15 分鐘 bar 內第二位使用者不重打網路。"""
        ev = make_short_entry_evaluation()
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx()),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
                return_value=ev,
            ) as mock_eval,
        ):
            for uid in (1, 2):
                await engine.evaluate_short_entry_opportunity(
                    uid, [ShortCandidateInput("AAA", _bearish_radar())], vix_spot=20.0
                )
        assert mock_eval.await_count == 1

    @pytest.mark.asyncio
    async def test_unconfirmed_or_tiny_account_yields_nothing(
        self, engine: DynamicRolloverEngine
    ) -> None:
        with (
            patch(f"{_DEPLOY}.get_full_user_context", return_value=_ctx(capital=500.0)),
            patch(
                "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry",
                new_callable=AsyncMock,
                side_effect=[
                    make_short_entry_evaluation(all_passed=False),
                    make_short_entry_evaluation(),
                ],
            ),
        ):
            out = await engine.evaluate_short_entry_opportunity(
                1,
                [
                    ShortCandidateInput("AAA", _bearish_radar()),
                    ShortCandidateInput("BBB", _bearish_radar()),
                ],
                vix_spot=20.0,
            )
        # $500 × 0.5% = $2.5 風險預算 < 停損距離 $5.5 → 0 股
        assert out == []


def test_get_user_ids_by_trading_strategy_filters_modes(db_conn: Any) -> None:
    from database.user_settings import (
        get_user_ids_by_trading_strategy,
        upsert_user_config,
    )

    upsert_user_config(9001, trading_strategy="SHORT_SIDE")
    upsert_user_config(9002, trading_strategy="DYNAMIC")
    upsert_user_config(9003, trading_strategy="RIGHT_SIDE")
    ids = get_user_ids_by_trading_strategy(("SHORT_SIDE", "DYNAMIC"))
    assert sorted(ids) == [9001, 9002]
    assert get_user_ids_by_trading_strategy(()) == []


def test_trading_strategy_whitelist_matches_enum() -> None:
    """database 層白名單必須與 TradingStrategyMode 同步：曾漏掉 SHORT_SIDE，
    使「做空交易」設定被靜默改寫為 RIGHT_SIDE。"""
    from database.user_settings import _VALID_TRADING_STRATEGIES
    from market_analysis.dynamic_rollover.models import TradingStrategyMode

    assert _VALID_TRADING_STRATEGIES == {m.value for m in TradingStrategyMode}
