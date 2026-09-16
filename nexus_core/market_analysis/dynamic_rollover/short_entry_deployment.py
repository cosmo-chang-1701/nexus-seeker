"""short_entry_deployment.py — SHORT_ENTRY 情境：獨立的做空進場訊號。

在此之前，做空六重鐵律 (short_side_entry.py) 通過後接手的全是多頭下游：
候選來源只看上漲空間、確認結果被丟進「賣衛星、買候選」的機會成本轉倉
(PowerSqueeze > 80 門檻、Buy Shares 工具別)、core_deployment 甚至把 CORE 超額
現金以 Buy Shares 部署進剛被確認要做空的標的。本情境把做空確認從那條路徑
完全脫鉤，產生自帶進場／停損／目標與倉位的獨立指令。

本情境**不代為下單**：輸出是建議。使用者成交後以負股數／負口數登錄，出場由
做空鏡像出場矩陣 (anti_washout.py) 接管。
"""

import math
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)
from zoneinfo import ZoneInfo

from database.user_settings import get_full_user_context

from . import logger
from .constants import (
    _SHORT_ENTRY_MAX_CANDIDATES,
    _SHORT_ENTRY_MAX_INSTRUCTIONS_PER_CYCLE,
)
from .models import (
    DynamicRegime,
    RolloverInstruction,
    RolloverScenario,
    ShortEntryEvaluation,
    TradingStrategyMode,
)
from .short_entry_sizing import (
    build_short_entry_levels,
    build_short_entry_plan,
    compute_short_entry_sizing,
)

_NY_TZ = ZoneInfo("America/New_York")
_SHORT_ENTRY_STRATEGIES = frozenset(
    {TradingStrategyMode.SHORT_SIDE.value, TradingStrategyMode.DYNAMIC.value}
)


class ShortCandidateInput(NamedTuple):
    symbol: str
    radar: Optional[Mapping[str, Any]]
    # Scenario 2 (DYNAMIC + Regime V) 已算好的評估結果，有則直接沿用、不重算。
    precomputed: Optional[ShortEntryEvaluation] = None
    entry_regime: Optional[str] = None
    rsi_15m: Optional[float] = None


def _current_bar_ts() -> str:
    """現在 (ET) 向下取整至 15 分鐘，作為評估結果的記憶鍵。"""
    now = datetime.now(_NY_TZ)
    floored = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    return floored.isoformat()


def _radar_spot(radar: Mapping[str, Any]) -> float:
    quote = radar.get("quote")
    if isinstance(quote, Mapping):
        try:
            return float(quote.get("c", 0.0) or 0.0)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


class _ShortEntryMixin:
    """SHORT_ENTRY 情境，混入 DynamicRolloverEngine。"""

    if TYPE_CHECKING:
        _short_entry_eval_cache: Dict[Tuple[str, str], Any]

    async def _evaluate_short_candidate(
        self, strategy: str, candidate: ShortCandidateInput
    ) -> Tuple[Optional[ShortEntryEvaluation], Optional[str], Optional[float]]:
        """回傳 (評估結果, entry_regime, rsi_15m)。DYNAMIC 非 Regime V 時評估結果為 None。

        評估結果與使用者無關 (只取決於標的與當下市況)，以 (symbol, 15 分鐘 bar)
        記憶，同一週期多位使用者共用同一候選時不重複打網路。
        """
        if candidate.precomputed is not None:
            return candidate.precomputed, candidate.entry_regime, candidate.rsi_15m
        radar = candidate.radar
        if not radar:
            return None, None, None
        spot = _radar_spot(radar)
        if spot <= 0:
            return None, None, None

        cache: Optional[Dict[Tuple[str, str], Any]] = getattr(
            self, "_short_entry_eval_cache", None
        )
        cache_key = (f"{strategy}:{candidate.symbol}", _current_bar_ts())
        if cache is not None and cache_key in cache:
            cached: Tuple[
                Optional[ShortEntryEvaluation], Optional[str], Optional[float]
            ] = cache[cache_key]
            return cached

        from .short_side_entry import evaluate_short_entry

        radar_dict: Dict[str, Any] = dict(radar)
        result: Tuple[Optional[ShortEntryEvaluation], Optional[str], Optional[float]]
        if strategy == TradingStrategyMode.DYNAMIC.value:
            from .regime_classifier import classify_dynamic_regime

            regime, _reason, market_data = await classify_dynamic_regime(
                candidate.symbol,
                spot,
                radar_dict.get("gex_profile_data") or {},
                radar_dict.get("uoa") or [],
            )
            if regime != DynamicRegime.REGIME_V_BREAKDOWN_CHASE:
                result = (None, regime.value, None)
            else:
                ev = await evaluate_short_entry(
                    candidate.symbol,
                    radar_dict,
                    spot,
                    df_15m=market_data.df_15m,
                    session_vwap=market_data.session_vwap,
                    atr_15m=market_data.atr_15m,
                )
                result = (ev, regime.value, market_data.rsi_15m)
        else:
            ev = await evaluate_short_entry(candidate.symbol, radar_dict, spot)
            result = (ev, None, None)

        if cache is not None:
            cache[cache_key] = result
        return result

    async def evaluate_short_entry_opportunity(
        self,
        user_id: int,
        candidates: Sequence[ShortCandidateInput],
        vix_spot: Optional[float],
        suppress_reason: Optional[str] = None,
    ) -> List[RolloverInstruction]:
        """對 SHORT_SIDE / DYNAMIC 使用者評估做空候選，產生至多一筆 SHORT_ENTRY 指令。

        閘門依序：策略模式 → 本週期保證金防禦抑制 → VIX 極端區 (做空乘數 0) →
        六重鐵律 → 價位合法性 → 倉位 >= 1 股。任一不滿足即靜默略過。
        """
        instructions: List[RolloverInstruction] = []
        try:
            ctx = get_full_user_context(user_id)
        except Exception as e:
            logger.warning(f"[SHORT_ENTRY] 讀取使用者 {user_id} 設定失敗，略過: {e}")
            return instructions
        strategy = str(getattr(ctx, "trading_strategy", "") or "")
        if strategy not in _SHORT_ENTRY_STRATEGIES:
            return instructions
        if suppress_reason:
            logger.info(
                f"[SHORT_ENTRY] user {user_id} 本週期有 {suppress_reason} 指令，"
                "抑制新開空單"
            )
            return instructions

        from config import get_short_vix_multiplier

        if get_short_vix_multiplier(vix_spot) <= 0:
            logger.info(
                f"[SHORT_ENTRY] VIX {vix_spot} >= 35 極端區，做空新倉暫停 (user {user_id})"
            )
            return instructions

        seen: set[str] = set()
        unique: List[ShortCandidateInput] = []
        for c in candidates:
            sym = c.symbol.upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            unique.append(c._replace(symbol=sym))
            if len(unique) >= _SHORT_ENTRY_MAX_CANDIDATES:
                break

        capital = float(getattr(ctx, "capital", 0.0) or 0.0)
        risk_limit = float(getattr(ctx, "risk_limit", 15.0) or 0.0)

        scored: List[Tuple[float, RolloverInstruction]] = []
        for candidate in unique:
            try:
                ev, entry_regime, rsi_15m = await self._evaluate_short_candidate(
                    strategy, candidate
                )
            except Exception as e:
                logger.error(f"[SHORT_ENTRY] {candidate.symbol} 做空評估失敗: {e}")
                continue
            if ev is None or not ev.all_passed:
                continue

            levels = build_short_entry_levels(ev)
            if levels is None:
                logger.info(
                    f"[SHORT_ENTRY] {candidate.symbol} 價位不合法 (停損/目標)，fail-closed"
                )
                continue
            sizing = compute_short_entry_sizing(
                levels, capital, risk_limit, vix_spot, rsi_15m
            )
            if sizing.share_qty < 1:
                logger.info(
                    f"[SHORT_ENTRY] {candidate.symbol} 倉位不足 1 股 "
                    f"(binding={sizing.binding_constraint})，略過"
                )
                continue

            plan = build_short_entry_plan(levels, sizing)
            reason = (
                f"🐻 **做空進場訊號 (Short Entry)**｜{levels.sub_mode}\n{ev.reason}"
            )
            if sizing.degrade_reason:
                reason += f"\n⚠️ {sizing.degrade_reason}"
            instruction: RolloverInstruction = {
                "symbol": candidate.symbol,
                "action": "OPEN_SHORT",
                "sell_ratio": 0.0,
                "target_core": candidate.symbol,
                "reason": reason,
                "suggested_strategy": ev.structure_directive or "Short Shares",
                "structure_directive": ev.structure_directive,
                "scenario": RolloverScenario.SHORT_ENTRY.value,
                "direction": "SHORT",
                "instrument_type": "SPOT",
                "limit_price": levels.entry_price,
                "is_manual_override_required": False,
                "cash_impact": None,
                "entry_regime": entry_regime,
                "short_entry_plan": plan,
            }
            rr = levels.reward_risk_ratio
            scored.append((rr if not math.isnan(rr) else 0.0, instruction))

        scored.sort(key=lambda item: item[0], reverse=True)
        instructions.extend(
            ins for _rr, ins in scored[:_SHORT_ENTRY_MAX_INSTRUCTIONS_PER_CYCLE]
        )
        return instructions
