"""自選標的進場顧問 — 進場確認核心（階段 A）。

30 分鐘深度心跳（`IntradayScanPipeline`）本身只有 SHIELD／premium-harvest／WAIT
三個出口，沒有「進場」路由。本模組把 `/x` 的「🔐 進場鐵律檢核」頁籤
(`cogs/unified_terminal/symbol_view.py::btn_entry_rules`) 的判定邏輯抽成可獨立
呼叫的協調函式，由 `pipeline.py::_dispatch_entry_advisor_alert` 以獨立通知頻道
推播——**不**新增 `WatchlistTacticalPlan.scenario` 值、**不**覆寫 `tactical`。

設計要點：

* 策略分派與 `symbol_view.py` **逐位元一致**（同一個問題不該有兩套答案）：
  `RIGHT_SIDE`／`LEFT_SIDE`／`SHORT_SIDE`／`DYNAMIC`(依 Regime 路由)。
* 結果與使用者無關（只取決於標的、策略與當下市況），以
  `(strategy:symbol, 15 分鐘 bar)` 記憶，同一輪多位使用者自選同一標的時不重複
  發動網路 I/O。快取鍵**必含 strategy**：同一檔標的在不同策略下走不同鐵律。
* 價位沿用全庫既有的單一定義：停損 `room_threshold.compute_reference_stop`、
  目標 `room_threshold.resolve_effective_target`（公式 D）、做空價位
  `short_entry_sizing.build_short_entry_levels`，不另寫第二套。
* 任何例外一律 fail-safe 回傳 `passed=False`（不進場），且失敗結果不進快取。
"""

import logging
from typing import Any, Dict, Literal, NamedTuple, Optional

from market_analysis.evaluation_recorder import current_bar_ts
from services.bounded_cache import BoundedCache

logger = logging.getLogger(__name__)

# 15 分鐘雷達共享快取 (bot._latest_radar_data_cache) 的保鮮窗。與
# cogs/trading/portfolio_monitor.py 的 300 秒判定同值：超過即視為過期、改走
# `_fetch_sym_radar_data_slow` 補抓。
_RADAR_FRESH_WINDOW_SECONDS = 300.0

# 每個 (strategy:symbol, bar) 一筆；15 分鐘換 bar 後舊鍵自然被 LRU 擠出。
_ADVICE_CACHE_MAX_SIZE = 256

AdviceDirection = Literal["LONG", "SHORT"]


class EntryAdvice(NamedTuple):
    """進場顧問的判定結果；與使用者無關，可跨使用者共用。"""

    passed: bool
    reason: str
    structure_directive: Optional[str]
    strategy: str
    regime: Optional[str] = None
    regime_reason: Optional[str] = None
    direction: AdviceDirection = "LONG"
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    rr_ratio: Optional[float] = None


_ADVICE_CACHE: BoundedCache = BoundedCache(max_size=_ADVICE_CACHE_MAX_SIZE)


def clear_advice_cache() -> None:
    """清空記憶（單元測試用）。"""
    _ADVICE_CACHE.clear()


def _fail_safe(strategy: str, why: str) -> EntryAdvice:
    return EntryAdvice(
        passed=False, reason=why, structure_directive=None, strategy=strategy
    )


def _positive(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None


async def _compute_long_levels(
    symbol: str,
    radar: Dict[str, Any],
    spot: float,
    df_15m: Optional[Any],
    phase1_price: Optional[float],
    is_left_catch: bool,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """多頭 (右側／左側) 的 (進場, 停損, 目標, 盈虧比)。任何一項算不出來回 None。

    價位純屬顯示資訊，不影響 `passed`；缺資料時該欄位留空而非自行編造 fallback
    ——`resolve_effective_target` 對 60 日高點／ATR 缺失自有降級階梯。
    """
    from market_analysis.atr_utils import fetch_high_60d
    from market_analysis.dynamic_rollover._shared import resolve_room_threshold_inputs
    from market_analysis.dynamic_rollover.structural_signals import (
        _resolve_canonical_anchor_base,
        _scan_gex_walls,
    )
    from market_analysis.room_threshold import (
        compute_reference_stop,
        resolve_effective_target,
    )

    gex_profile_data = radar.get("gex_profile_data")
    gex = gex_profile_data if isinstance(gex_profile_data, dict) else {}

    put_wall, atr_15m, atr_1d = await resolve_room_threshold_inputs(
        symbol, radar, gex, df_15m, target_spot=spot
    )
    call_wall = _positive(gex.get("call_wall")) or 0.0
    gamma_flip = _positive(radar.get("gamma_flip")) or 0.0
    support_wall, _res, _sg, _rg = _scan_gex_walls(symbol, gex or None, spot=spot)

    entry = spot
    if is_left_catch and phase1_price is not None and 0 < phase1_price < spot:
        # 左側是掛在接刀位等成交，不是追現價。
        entry = phase1_price

    stop: Optional[float] = None
    if atr_15m > 0:
        anchor = _resolve_canonical_anchor_base(
            support_wall, put_wall, call_wall, gamma_flip, 0.0, spot
        )
        stop = compute_reference_stop(spot, anchor, atr_15m, "LONG")

    high_60d = await fetch_high_60d(symbol)
    eff = resolve_effective_target(spot, call_wall, high_60d, atr_1d)
    target = eff.target if eff.target > 0 else None

    rr: Optional[float] = None
    if stop is not None and target is not None and entry - stop > 0 and target > entry:
        rr = (target - entry) / (entry - stop)

    def _r(v: Optional[float]) -> Optional[float]:
        return round(v, 2) if v is not None else None

    return _r(entry), _r(stop), _r(target), _r(rr)


async def _evaluate_uncached(
    strategy: str,
    symbol: str,
    radar: Dict[str, Any],
    spot: float,
    phase1_price: Optional[float],
) -> EntryAdvice:
    from market_analysis.dynamic_rollover import DynamicRolloverEngine
    from market_analysis.dynamic_rollover.left_side_entry import (
        _confirm_left_entry_signal,
    )
    from market_analysis.dynamic_rollover.models import (
        DynamicRegime,
        ShortEntryEvaluation,
        TradingStrategyMode,
    )
    from market_analysis.dynamic_rollover.short_entry_sizing import (
        build_short_entry_levels,
    )
    from market_analysis.dynamic_rollover.short_side_entry import evaluate_short_entry

    engine = DynamicRolloverEngine()
    regime_value: Optional[str] = None
    regime_reason: Optional[str] = None
    direction: AdviceDirection = "LONG"
    is_left_catch = False
    short_ev: Optional[ShortEntryEvaluation] = None
    passed: bool
    reason: str
    directive: Optional[str] = None
    df_15m: Optional[Any] = None

    if strategy == TradingStrategyMode.LEFT_SIDE.value:
        is_left_catch = True
        passed, reason, directive = await _confirm_left_entry_signal(
            symbol, radar, spot
        )
    elif strategy == TradingStrategyMode.SHORT_SIDE.value:
        direction = "SHORT"
        short_ev = await evaluate_short_entry(symbol, radar, spot)
    elif strategy == TradingStrategyMode.DYNAMIC.value:
        from market_analysis.dynamic_rollover.regime_classifier import (
            classify_dynamic_regime,
        )

        regime, regime_reason, market_data = await classify_dynamic_regime(
            symbol, spot, radar.get("gex_profile_data") or {}, radar.get("uoa") or []
        )
        regime_value = regime.value
        df_15m = market_data.df_15m
        if regime == DynamicRegime.REGIME_III_RIGHT_MOMENTUM:
            passed, reason, directive = await engine._confirm_entry_signal(
                symbol,
                radar,
                spot,
                df_15m=market_data.df_15m,
                session_vwap=market_data.session_vwap,
            )
        elif regime == DynamicRegime.REGIME_III_B_TREND_CONTINUATION:
            passed, reason, directive = await engine._confirm_entry_signal(
                symbol,
                radar,
                spot,
                df_15m=market_data.df_15m,
                session_vwap=market_data.session_vwap,
                trend_continuation=True,
            )
        elif regime == DynamicRegime.REGIME_I_LEFT_CATCH:
            is_left_catch = True
            passed, reason, directive = await _confirm_left_entry_signal(
                symbol,
                radar,
                spot,
                df_15m=market_data.df_15m,
                session_vwap=market_data.session_vwap,
                atr_15m=market_data.atr_15m,
            )
        elif regime == DynamicRegime.REGIME_V_BREAKDOWN_CHASE:
            direction = "SHORT"
            short_ev = await evaluate_short_entry(
                symbol,
                radar,
                spot,
                df_15m=market_data.df_15m,
                session_vwap=market_data.session_vwap,
                atr_15m=market_data.atr_15m,
            )
        else:
            return EntryAdvice(
                passed=False,
                reason=f"⛔ Regime `{regime_value}`：{regime_reason}",
                structure_directive=None,
                strategy=strategy,
                regime=regime_value,
                regime_reason=regime_reason,
            )
    else:
        passed, reason, directive = await engine._confirm_entry_signal(
            symbol, radar, spot
        )

    if direction == "SHORT":
        assert short_ev is not None
        passed = short_ev.all_passed
        reason = short_ev.reason
        directive = short_ev.structure_directive
        entry: Optional[float] = None
        stop: Optional[float] = None
        target: Optional[float] = None
        rr: Optional[float] = None
        if passed:
            levels = build_short_entry_levels(short_ev)
            if levels is None:
                # fail-closed：價位不合法就不推播，與 SHORT_ENTRY 情境同一原則。
                passed = False
                reason = f"{reason} | ⛔ 無法推導合法的做空價位，不推播"
            else:
                entry = levels.entry_price
                stop = levels.stop_price
                target = levels.target_price
                rr = levels.reward_risk_ratio
        return EntryAdvice(
            passed=passed,
            reason=reason,
            structure_directive=directive,
            strategy=strategy,
            regime=regime_value,
            regime_reason=regime_reason,
            direction="SHORT",
            entry_price=entry,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
        )

    entry_l: Optional[float] = None
    stop_l: Optional[float] = None
    target_l: Optional[float] = None
    rr_l: Optional[float] = None
    if passed:
        try:
            entry_l, stop_l, target_l, rr_l = await _compute_long_levels(
                symbol, radar, spot, df_15m, phase1_price, is_left_catch
            )
        except Exception as e:
            logger.warning(f"[{symbol}] 進場顧問價位計算失敗（僅缺價位欄位）: {e}")
    return EntryAdvice(
        passed=passed,
        reason=reason,
        structure_directive=directive,
        strategy=strategy,
        regime=regime_value,
        regime_reason=regime_reason,
        direction="LONG",
        entry_price=entry_l,
        stop_loss=stop_l,
        target=target_l,
        rr_ratio=rr_l,
    )


async def evaluate_entry_advice(
    strategy: str,
    symbol: str,
    radar: Dict[str, Any],
    spot: float,
    phase1_price: Optional[float] = None,
) -> EntryAdvice:
    """依使用者交易策略對單一標的做進場確認，並在通過時附上價位。

    以 `(strategy:symbol, 15 分鐘 bar)` 記憶；例外一律 fail-safe 回傳未通過，
    且失敗結果不入快取（下一輪仍可重試）。
    """
    sym = symbol.upper()
    if spot <= 0 or not radar:
        return _fail_safe(strategy, "⛔ 缺少有效現價或雷達資料，本輪略過")

    cache_key = (f"{strategy}:{sym}", current_bar_ts())
    if cache_key in _ADVICE_CACHE:
        cached: EntryAdvice = _ADVICE_CACHE[cache_key]
        return cached

    try:
        advice = await _evaluate_uncached(strategy, sym, radar, spot, phase1_price)
    except Exception as e:
        logger.warning(f"[{sym}] 進場顧問評估失敗 (strategy={strategy}): {e}")
        return _fail_safe(strategy, f"⛔ 進場確認發生例外，本輪略過: {e}")

    _ADVICE_CACHE[cache_key] = advice
    return advice
