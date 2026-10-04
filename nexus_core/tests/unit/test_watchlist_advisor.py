"""自選標的進場顧問（階段 A）：entry_advisor 核心與 pipeline 派發閘門。

最高優先：既有 30 分鐘心跳的契約不得改變（`scenario` Literal、`tactical`）；
進場顧問是獨立派發路徑，任何擋下都不得燒去重旗標。
"""

import inspect
import time
import typing
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Optional, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from market_analysis import evaluation_recorder
from market_analysis.dynamic_rollover.models import DynamicRegime
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.intraday_pipeline import IntradayScanPipeline, entry_advisor
from market_analysis.intraday_pipeline.entry_advisor import (
    EntryAdvice,
    evaluate_entry_advice,
)
from models.schemas import WatchlistTacticalPlan
from tests.unit.short_entry_helpers import make_short_entry_evaluation

_RADAR: Dict[str, Any] = {
    "quote": {"c": 100.0},
    "gex_profile_data": {"call_wall": 110.0, "put_wall": 95.0},
    "uoa": [],
}


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    entry_advisor.clear_advice_cache()
    evaluation_recorder.clear_buffer()
    yield
    entry_advisor.clear_advice_cache()
    evaluation_recorder.clear_buffer()


def _eval(alert_level: str = "green", scenario: str = "wait") -> Any:
    return SimpleNamespace(
        tactical=SimpleNamespace(alert_level=alert_level, scenario=scenario),
        metrics=SimpleNamespace(current_price=100.0, buy_price_phase1=97.0),
    )


def _advice(**kw: Any) -> EntryAdvice:
    base: Dict[str, Any] = dict(
        passed=True,
        reason="條件一✅ | 條件二✅",
        structure_directive="Long Call",
        strategy="RIGHT_SIDE",
        entry_price=100.0,
        stop_loss=95.0,
        target=112.0,
        rr_ratio=2.4,
    )
    base.update(kw)
    return EntryAdvice(**base)


def _make_pipeline(radar: Optional[Dict[str, Any]] = None) -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot._latest_radar_data_cache = {"NVDA": radar or _RADAR}
    bot._latest_radar_cache_time = time.time()
    return IntradayScanPipeline(bot, NexusGammaSqueezeEngine())


class _Kv:
    """以 set 模擬 kv_cache 去重旗標的讀寫。"""

    def __init__(self) -> None:
        self.saved: Set[str] = set()

    async def save(self, key: str, value: Any, *a: Any, **k: Any) -> None:
        self.saved.add(key)

    def get(self, key: str) -> Any:
        return True if key in self.saved else None


async def _dispatch(
    pipeline: Any,
    advice: Any,
    *,
    watchlist_eval: Any = None,
    strategy: str = "RIGHT_SIDE",
    kv: Optional[_Kv] = None,
    notif: bool = True,
    advisor_dry: bool = False,
    squeeze_dry: bool = False,
    short_dry: bool = False,
    uid: int = 42,
) -> _Kv:
    kv = kv or _Kv()
    ctx = SimpleNamespace(trading_strategy=strategy)
    fake = advice if callable(advice) else AsyncMock(return_value=advice)
    with (
        patch("database.is_notification_enabled", return_value=notif),
        patch("database.get_kv_cache", side_effect=kv.get),
        patch("database.save_kv_cache", side_effect=kv.save),
        patch.object(config, "WATCHLIST_ADVISOR_DRY_RUN", advisor_dry),
        patch.object(config, "SQUEEZE_ENTRY_DRY_RUN", squeeze_dry),
        patch.object(config, "SHORT_ENTRY_DRY_RUN", short_dry),
        patch(
            "market_analysis.intraday_pipeline.entry_advisor.evaluate_entry_advice",
            fake,
        ),
    ):
        await pipeline._dispatch_entry_advisor_alert(
            uid,
            "NVDA",
            watchlist_eval if watchlist_eval is not None else _eval(),
            ctx,
            datetime(2026, 9, 21, 10, 0),
        )
    return kv


# ── 既有心跳契約不變 ──────────────────────────────────────────────


def test_scenario_literal_unchanged() -> None:
    args = typing.get_args(WatchlistTacticalPlan.model_fields["scenario"].annotation)
    assert set(args) == {"premium-harvest", "hard-hedge", "wait"}


def test_advisor_dispatched_before_engine_enabled_continue() -> None:
    """進場顧問不得掛在 engine_enabled 之後（預設 enable_analyst_agent=0 會到不了）。"""
    src = inspect.getsource(IntradayScanPipeline._run_loop)
    assert src.index("_dispatch_entry_advisor_alert") < src.index(
        "if not engine_enabled or account_state is None"
    )


# ── 派發閘門 ─────────────────────────────────────────────────────


async def test_pass_pushes_embed_with_price_fields_and_burns_flag() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice())
    p.bot.queue_dm.assert_awaited_once()
    embed = p.bot.queue_dm.await_args.kwargs["embed"]
    assert any("價位建議" in str(f.name) for f in embed.fields)
    body = " ".join(str(f.value) for f in embed.fields if "價位建議" in str(f.name))
    for token in ("$100.00", "$95.00", "$112.00", "2.40"):
        assert token in body
    assert kv.saved == {"advisory_entry_42_NVDA_RIGHT_SIDE_20260921"}


@pytest.mark.parametrize(
    "level,scenario",
    [("red", "hard-hedge"), ("red", "wait"), ("yellow", "premium-harvest")],
)
async def test_non_green_heartbeat_never_pushes_entry(
    level: str, scenario: str
) -> None:
    p = _make_pipeline()
    fake = AsyncMock(return_value=_advice())
    kv = await _dispatch(p, fake, watchlist_eval=_eval(level, scenario))
    p.bot.queue_dm.assert_not_awaited()
    fake.assert_not_awaited()
    assert not kv.saved


async def test_failed_gate_does_not_push_or_burn_flag() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice(passed=False))
    p.bot.queue_dm.assert_not_awaited()
    assert not kv.saved


async def test_notification_off_does_not_push_or_burn_flag() -> None:
    p = _make_pipeline()
    fake = AsyncMock(return_value=_advice())
    kv = await _dispatch(p, fake, notif=False)
    p.bot.queue_dm.assert_not_awaited()
    fake.assert_not_awaited()
    assert not kv.saved


async def test_advisor_dry_run_blocks_push_and_does_not_burn_flag() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice(), advisor_dry=True)
    p.bot.queue_dm.assert_not_awaited()
    assert not kv.saved


async def test_squeeze_dry_run_blocks_but_still_records_forward_data() -> None:
    p = _make_pipeline()

    async def _fake(*a: Any, **k: Any) -> EntryAdvice:
        evaluation_recorder.record_gate_reason(
            "ENTRY_SQUEEZE", "NVDA", 100.0, True, "x"
        )
        return _advice(tier=2, size_pct=1.5)

    kv = await _dispatch(p, _fake, squeeze_dry=True)
    p.bot.queue_dm.assert_not_awaited()
    assert not kv.saved
    assert evaluation_recorder.pending_count() == 1  # 乾跑仍記錄


async def test_squeeze_pushes_when_its_dry_run_is_off() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice(tier=1, size_pct=1.0))
    p.bot.queue_dm.assert_awaited_once()
    assert kv.saved == {"advisory_entry_42_NVDA_SQZ_T1_20260921"}


async def test_short_direction_blocked_by_short_entry_dry_run() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice(direction="SHORT"), short_dry=True)
    p.bot.queue_dm.assert_not_awaited()
    assert not kv.saved


async def test_long_not_blocked_by_short_entry_dry_run() -> None:
    p = _make_pipeline()
    await _dispatch(p, _advice(), short_dry=True)
    p.bot.queue_dm.assert_awaited_once()


async def test_same_day_same_regime_is_deduped() -> None:
    p = _make_pipeline()
    kv = await _dispatch(p, _advice())
    await _dispatch(p, _advice(), kv=kv)
    assert p.bot.queue_dm.await_count == 1


async def test_squeeze_tier_upgrade_is_not_deduped() -> None:
    p = _make_pipeline()
    kv = _Kv()
    for tier in (1, 3):
        await _dispatch(p, _advice(tier=tier, size_pct=1.0), kv=kv)
    assert p.bot.queue_dm.await_count == 2
    assert len(kv.saved) == 2


async def test_dispatch_swallows_exceptions() -> None:
    p = _make_pipeline()
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    await _dispatch(p, boom)  # 不得拋出
    p.bot.queue_dm.assert_not_awaited()


# ── 前向蒐集來源 ─────────────────────────────────────────────────


async def test_evaluation_source_is_watchlist_advisor_and_reset() -> None:
    p = _make_pipeline()
    seen: list[Optional[str]] = []

    async def _fake(*a: Any, **k: Any) -> EntryAdvice:
        seen.append(evaluation_recorder._SOURCE.get())
        evaluation_recorder.record_gate_reason("ENTRY_RIGHT", "NVDA", 100.0, True, "x")
        return _advice()

    await _dispatch(p, _fake)
    assert seen == ["WATCHLIST_ADVISOR"]
    assert evaluation_recorder._SOURCE.get() is None  # finally 已還原
    rows = list(evaluation_recorder._BUFFER)
    assert rows and rows[0]["source"] == "WATCHLIST_ADVISOR"


# ── radar 取得 ───────────────────────────────────────────────────


class _CountingSem:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *a: Any) -> None:
        return None


async def test_fresh_shared_radar_does_not_call_slow_fetch() -> None:
    p = _make_pipeline()
    cog = MagicMock()
    cog._fetch_sym_radar_data_slow = AsyncMock()
    p.bot.get_cog.return_value = cog
    assert await p._resolve_candidate_radar("nvda") == _RADAR
    cog._fetch_sym_radar_data_slow.assert_not_awaited()


async def test_stale_shared_radar_falls_back_through_semaphore() -> None:
    p = _make_pipeline()
    p.bot._latest_radar_cache_time = time.time() - 301.0
    cog = MagicMock()
    fresh = {"quote": {"c": 101.0}}
    cog._fetch_sym_radar_data_slow = AsyncMock(return_value=fresh)
    p.bot.get_cog.return_value = cog
    sem = _CountingSem()
    p._radar_fetch_sem = sem
    assert await p._resolve_candidate_radar("NVDA") == fresh
    cog._fetch_sym_radar_data_slow.assert_awaited_once_with("NVDA")
    assert sem.entered == 1


async def test_missing_terminal_cog_or_failed_fetch_returns_none() -> None:
    p = _make_pipeline()
    p.bot._latest_radar_cache_time = 0.0
    p.bot.get_cog.return_value = None
    assert await p._resolve_candidate_radar("NVDA") is None

    cog = MagicMock()
    cog._fetch_sym_radar_data_slow = AsyncMock(side_effect=RuntimeError("x"))
    p.bot.get_cog.return_value = cog
    assert await p._resolve_candidate_radar("NVDA") is None


async def test_no_radar_skips_left_and_short_strategies() -> None:
    p = _make_pipeline()
    p.bot._latest_radar_cache_time = 0.0
    p.bot.get_cog.return_value = None
    for strategy in ("LEFT_SIDE", "SHORT_SIDE"):
        fake = AsyncMock(return_value=_advice())
        await _dispatch(p, fake, strategy=strategy)
        fake.assert_not_awaited()
    p.bot.queue_dm.assert_not_awaited()


async def test_no_radar_still_evaluates_squeeze_path() -> None:
    """擠壓規則只需要 K 線：沒有雷達時以空 radar 與心跳現價照常評估。"""
    p = _make_pipeline()
    p.bot._latest_radar_cache_time = 0.0
    p.bot.get_cog.return_value = None
    fake = AsyncMock(return_value=_advice(tier=1, size_pct=1.0))
    await _dispatch(p, fake, strategy="RIGHT_SIDE")
    fake.assert_awaited_once()
    args = fake.await_args
    assert args is not None
    assert args.args[2] == {} and args.args[3] == 100.0


# ── entry_advisor：策略分派（與 /x 一致）＋記憶化 ─────────────────


_OK = (True, "條件一✅", "Long Call")
_SQZ_OK = EntryAdvice(
    passed=True,
    reason="✅ T2",
    structure_directive=None,
    strategy="RIGHT_SIDE",
    tier=2,
    size_pct=1.5,
)
_SQZ_NO = EntryAdvice(
    passed=False,
    reason="⏸️ 尚未形成擠壓進場條件",
    structure_directive=None,
    strategy="RIGHT_SIDE",
)
_PATH_SQUEEZE = "market_analysis.intraday_pipeline.entry_advisor._evaluate_squeeze_long"
_PATH_LEFT = (
    "market_analysis.dynamic_rollover.left_side_entry._confirm_left_entry_signal"
)
_PATH_SHORT = "market_analysis.dynamic_rollover.short_side_entry.evaluate_short_entry"
_PATH_CLASSIFY = (
    "market_analysis.dynamic_rollover.regime_classifier.classify_dynamic_regime"
)
_LEVELS = "market_analysis.intraday_pipeline.entry_advisor._compute_long_levels"


def _mkt() -> Any:
    from market_analysis.dynamic_rollover.models import RegimeMarketData

    return RegimeMarketData(df_15m=None, session_vwap=100.0, atr_15m=1.0)


@pytest.fixture
def gates() -> Iterator[Dict[str, AsyncMock]]:
    mocks = {
        "right": AsyncMock(return_value=_SQZ_OK),
        "left": AsyncMock(return_value=_OK),
        "short": AsyncMock(return_value=make_short_entry_evaluation()),
        "classify": AsyncMock(),
        "levels": AsyncMock(return_value=(100.0, 95.0, 112.0, 2.4)),
    }
    with (
        patch(_PATH_SQUEEZE, mocks["right"]),
        patch(_PATH_LEFT, mocks["left"]),
        patch(_PATH_SHORT, mocks["short"]),
        patch(_PATH_CLASSIFY, mocks["classify"]),
        patch(_LEVELS, mocks["levels"]),
    ):
        yield mocks


@pytest.mark.parametrize(
    "strategy,expected",
    [
        ("RIGHT_SIDE", "right"),
        ("LEFT_SIDE", "left"),
        ("SHORT_SIDE", "short"),
        ("SOMETHING_UNKNOWN", "right"),  # 與 symbol_view 一致：其餘走右側
    ],
)
async def test_strategy_dispatch_matches_symbol_view(
    gates: Dict[str, AsyncMock], strategy: str, expected: str
) -> None:
    advice = await evaluate_entry_advice(strategy, "NVDA", _RADAR, 100.0)
    for name in ("right", "left", "short"):
        assert (gates[name].await_count == 1) == (name == expected), name
    gates["classify"].assert_not_awaited()
    assert advice.passed is True


@pytest.mark.parametrize(
    "regime,expected",
    [
        (DynamicRegime.REGIME_III_RIGHT_MOMENTUM, "right"),
        (DynamicRegime.REGIME_III_B_TREND_CONTINUATION, "right"),
        (DynamicRegime.REGIME_II_CHAOS_STANDASIDE, "right"),
        (DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS, "right"),
        (DynamicRegime.REGIME_I_LEFT_CATCH, "left"),
        (DynamicRegime.REGIME_V_BREAKDOWN_CHASE, "short"),
    ],
)
async def test_dynamic_regime_routing(
    gates: Dict[str, AsyncMock], regime: DynamicRegime, expected: str
) -> None:
    """多頭建倉（非左側、非做空的 Regime）一律走擠壓規則；宏觀鎖定改由擠壓
    路徑的否決條件判定，不再在路由層直接拒絕。"""
    gates["classify"].return_value = (regime, "理由", _mkt())
    await evaluate_entry_advice("DYNAMIC", "NVDA", _RADAR, 100.0)
    for name in ("right", "left", "short"):
        assert (gates[name].await_count == 1) == (name == expected), name
    if expected == "right":
        call = gates["right"].await_args
        assert call is not None
        assert call.args[3] == regime.value


async def test_two_users_same_symbol_classify_once(
    gates: Dict[str, AsyncMock],
) -> None:
    gates["classify"].return_value = (
        DynamicRegime.REGIME_III_RIGHT_MOMENTUM,
        "理由",
        _mkt(),
    )
    a = await evaluate_entry_advice("DYNAMIC", "NVDA", _RADAR, 100.0)
    b = await evaluate_entry_advice("DYNAMIC", "NVDA", _RADAR, 100.0)
    assert gates["classify"].await_count == 1
    assert a == b


async def test_cache_key_includes_strategy(gates: Dict[str, AsyncMock]) -> None:
    await evaluate_entry_advice("RIGHT_SIDE", "NVDA", _RADAR, 100.0)
    await evaluate_entry_advice("LEFT_SIDE", "NVDA", _RADAR, 100.0)
    assert gates["right"].await_count == 1
    assert gates["left"].await_count == 1


async def test_exception_is_fail_safe_and_not_cached(
    gates: Dict[str, AsyncMock],
) -> None:
    gates["right"].side_effect = [RuntimeError("net"), _SQZ_OK]
    first = await evaluate_entry_advice("RIGHT_SIDE", "NVDA", _RADAR, 100.0)
    assert first.passed is False
    second = await evaluate_entry_advice("RIGHT_SIDE", "NVDA", _RADAR, 100.0)
    assert second.passed is True  # 失敗結果未入快取，下一輪可重試


async def test_invalid_spot_or_radar_is_fail_safe(gates: Dict[str, AsyncMock]) -> None:
    assert (
        await evaluate_entry_advice("RIGHT_SIDE", "NVDA", _RADAR, 0.0)
    ).passed is False
    gates["right"].assert_not_awaited()
    for strategy in ("LEFT_SIDE", "SHORT_SIDE"):
        assert (
            await evaluate_entry_advice(strategy, "NVDA", {}, 100.0)
        ).passed is False
    gates["left"].assert_not_awaited()
    gates["short"].assert_not_awaited()
    # 擠壓路徑不需要雷達
    assert (await evaluate_entry_advice("RIGHT_SIDE", "NVDA", {}, 100.0)).passed is True


async def test_failed_gate_skips_level_computation(gates: Dict[str, AsyncMock]) -> None:
    gates["right"].return_value = _SQZ_NO
    advice = await evaluate_entry_advice("RIGHT_SIDE", "NVDA", _RADAR, 100.0)
    assert advice.passed is False and advice.entry_price is None
    gates["levels"].assert_not_awaited()


async def test_left_catch_is_flagged_for_phase1_entry(
    gates: Dict[str, AsyncMock],
) -> None:
    await evaluate_entry_advice("LEFT_SIDE", "NVDA", _RADAR, 100.0, phase1_price=97.0)
    levels_call = gates["levels"].await_args
    assert levels_call is not None
    assert levels_call.args[-2:] == (97.0, True)


# ── 做空價位：沿用 build_short_entry_levels，fail-closed ─────────


async def test_short_side_uses_short_levels(gates: Dict[str, AsyncMock]) -> None:
    advice = await evaluate_entry_advice("SHORT_SIDE", "NVDA", _RADAR, 100.0)
    assert advice.passed and advice.direction == "SHORT"
    assert (advice.entry_price, advice.stop_loss, advice.target) == (100.0, 105.5, 90.0)
    assert advice.rr_ratio == pytest.approx(1.82)


async def test_short_without_legal_levels_does_not_pass(
    gates: Dict[str, AsyncMock],
) -> None:
    gates["short"].return_value = make_short_entry_evaluation(put_wall=0.0)
    advice = await evaluate_entry_advice("SHORT_SIDE", "NVDA", _RADAR, 100.0)
    assert advice.passed is False


# ── 多頭價位計算 ─────────────────────────────────────────────────


async def test_long_levels_use_shared_stop_and_target_definitions() -> None:
    from market_analysis.room_threshold import (
        compute_reference_stop,
        resolve_effective_target,
    )

    radar = {
        "quote": {"c": 100.0},
        "gex_profile_data": {"call_wall": 112.0, "put_wall": 96.0},
        "atr_14": 3.0,
    }
    with (
        patch(
            "market_analysis.dynamic_rollover._shared.resolve_room_threshold_inputs",
            AsyncMock(return_value=(96.0, 1.0, 3.0)),
        ),
        patch(
            "market_analysis.dynamic_rollover.structural_signals._scan_gex_walls",
            return_value=(96.0, 112.0, 1.0, 1.0),
        ),
        patch("market_analysis.atr_utils.fetch_high_60d", AsyncMock(return_value=0.0)),
    ):
        entry, stop, target, rr = await entry_advisor._compute_long_levels(
            "NVDA", radar, 100.0, None, None, False
        )
    exp_stop = compute_reference_stop(100.0, 96.0, 1.0, "LONG")
    exp_target = resolve_effective_target(100.0, 112.0, 0.0, 3.0).target
    assert entry == 100.0
    assert stop == round(exp_stop, 2)
    assert target == round(exp_target, 2)
    assert rr == round((exp_target - 100.0) / (100.0 - exp_stop), 2)


async def test_long_levels_rr_is_none_when_denominator_non_positive() -> None:
    with (
        patch(
            "market_analysis.dynamic_rollover._shared.resolve_room_threshold_inputs",
            AsyncMock(return_value=(96.0, 0.0, 3.0)),  # ATR₁₅ₘ 缺失 → 無停損
        ),
        patch(
            "market_analysis.dynamic_rollover.structural_signals._scan_gex_walls",
            return_value=(96.0, 112.0, 1.0, 1.0),
        ),
        patch("market_analysis.atr_utils.fetch_high_60d", AsyncMock(return_value=0.0)),
    ):
        _e, stop, _t, rr = await entry_advisor._compute_long_levels(
            "NVDA", {"gex_profile_data": {"call_wall": 112.0}}, 100.0, None, None, False
        )
    assert stop is None and rr is None


# ── embed 向後相容 ───────────────────────────────────────────────


def test_embed_without_prices_has_no_price_field() -> None:
    from cogs.embed_builders.portfolio_embeds import create_entry_rules_embed

    embed = create_entry_rules_embed("NVDA", True, ["條件一✅"])
    assert not any("價位建議" in str(f.name) for f in embed.fields)


def test_embed_rr_none_renders_na() -> None:
    from cogs.embed_builders.portfolio_embeds import create_entry_rules_embed

    embed = create_entry_rules_embed(
        "NVDA", True, ["條件一✅"], entry_price=100.0, rr_ratio=None
    )
    body = " ".join(str(f.value) for f in embed.fields if "價位建議" in str(f.name))
    assert "$100.00" in body and "N/A" in body


# ── 擠壓路徑轉接（_evaluate_squeeze_long）──────────────────────────


async def test_squeeze_long_maps_result_and_records_forward_data() -> None:
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.resistance import (
        ResistanceContext,
        ResistanceZone,
    )
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    res = ResistanceContext(
        atr_1d=4.0,
        overhead=ResistanceZone(108.0, 109.0, 2),
        is_approaching=False,
        broken=None,
    )
    result = SqueezeEntryResult(
        "ENTRY",
        3,
        2.5,
        95.5,
        "✅ T3：D Green Dot",
        ["D Green Dot"],
        3,
        "上方壓力區 $108.00–$109.00（觸及 2 次）",
    )
    vetoes = AsyncMock(return_value=(["財報"], "逃頂警戒 WATCH"))
    evaluate = AsyncMock(return_value=SqueezeEvaluation(result, {}, res))
    token = evaluation_recorder.set_evaluation_source("WATCHLIST_ADVISOR")
    try:
        with (
            patch(
                "market_analysis.squeeze_entry.vetoes.resolve_long_entry_vetoes",
                vetoes,
            ),
            patch("market_analysis.squeeze_entry.evaluate_symbol", evaluate),
        ):
            advice = await entry_advisor._evaluate_squeeze_long(
                "DYNAMIC", "NVDA", 100.0, "REGIME_II_CHAOS_STANDASIDE", "休眠"
            )
    finally:
        evaluation_recorder.reset_evaluation_source(token)

    args = evaluate.await_args
    assert args is not None
    assert args.args == ("NVDA", 100.0, ["財報"], "逃頂警戒 WATCH")
    assert advice.passed is True and advice.tier == 3 and advice.size_pct == 2.5
    assert advice.entry_price == 100.0 and advice.stop_loss == 95.5
    assert advice.target is None and advice.rr_ratio is None
    assert advice.regime == "REGIME_II_CHAOS_STANDASIDE"
    assert "上方壓力區" in advice.reason
    assert evaluation_recorder.pending_count() == 1


async def test_squeeze_advice_pushes_squeeze_embed() -> None:
    from market_analysis.squeeze_entry import SqueezeEvaluation
    from market_analysis.squeeze_entry.rules import SqueezeEntryResult

    p = _make_pipeline()
    result = SqueezeEntryResult("ENTRY", 1, 1.0, 95.0, "✅ T1：5m Green Dot")
    advice = _advice(
        tier=1,
        size_pct=1.0,
        squeeze=SqueezeEvaluation(result, {}, None),
        target=None,
        rr_ratio=None,
    )
    with patch(
        "cogs.embed_builders.squeeze_entry_embeds.create_squeeze_entry_embed",
        wraps=None,
    ) as builder:
        builder.return_value = MagicMock()
        await _dispatch(p, advice)
    builder.assert_called_once()
    assert builder.call_args.kwargs["passed"] is True
    p.bot.queue_dm.assert_awaited_once()
