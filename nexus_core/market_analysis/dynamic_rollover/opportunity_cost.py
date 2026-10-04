from datetime import datetime
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

import market_time

from . import logger
from .constants import (
    CORE_DEFENSE_ETF_SYMBOLS,
    INDEX_INVERSE_MAP,
    SECTOR_INVERSE_MAP,
    SINGLE_STOCK_INVERSE_MAP,
    _EARNINGS_PRE_EVENT_BUFFER_DAYS,
    _EV_SPREAD_MIN_THRESHOLD,
    _SKEW_DOWNSIDE_PENALTY_FACTOR,
    _SHORT_CANDIDATE_MAX_PSQ,
)
from .models import (
    EntryDirection,
)


async def _confirm_entry_condition5_macro_earnings_gate(
    candidate_symbol: str,
    prior_conditions_passed: bool,
    reasons: list,
    direction: EntryDirection = "LONG",
) -> Tuple[bool, Optional[int]]:
    """條件五：總經負 Gamma 與財報黑天鵝防禦閘門 (前四項通過時才發動判定，避免
    為了一個已經確定會失敗的整體結果，仍去打財報行事曆/總經 Regime 這類真實
    I/O)。未發動時仍在 reasons 補上一行「⏭️ 略過」標記 (不觸發任何額外 I/O)，
    確保「進場鐵律檢核」面板永遠完整列出六項條件，不會因短路優化而讓使用者
    誤以為只有四重鐵律。

    回傳 (是否通過, 距財報天數)。第二個元素純粹是把本函式「為了判定財報緩衝
    而本來就已經查到」的天數一併帶出，供條件六推導建議進場結構的 DTE band 時
    收斂上限用 (不建議抱過財報)，零額外 I/O。查無財報、解析失敗或短路略過時
    為 None；財報已過期時為負值，由消費端自行決定如何處理。

    `direction` 只影響大盤鎖定時的文案，不影響封鎖行為：負 Gamma 踩踏／流動性
    危機期間做空同樣被封鎖 (軋空與流動性斷層風險同樣極端)，但告訴做空使用者
    「嚴禁開倉個股買方」是錯的敘述。"""
    if not prior_conditions_passed:
        reasons.append("條件五⏭️：前四項未全數通過，略過總經/財報安全閥檢查")
        return True, None

    c5_passed = True
    days_to_er: Optional[int] = None
    try:
        from database.calendar_cache import get_cached_earnings

        earn = get_cached_earnings(candidate_symbol)
        if earn and earn.get("earnings_date"):
            earn_date_str = str(earn["earnings_date"])[:10]
            earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
            days_to_er = market_time.days_to_expiry_et(earn_dt)
            if 0 <= days_to_er <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                c5_passed = False
                reasons.append(
                    f"條件五❌：即將於 {days_to_er} 天內發布財報，避開高波事件風險"
                )
    except Exception as e:
        c5_passed = False
        reasons.append(f"條件五❌：財報行事曆資料抓取失敗，安全起見判定未通過: {e}")

    if c5_passed:
        try:
            from market_analysis.index_microstructure import get_market_regime

            regime = await get_market_regime()
            if regime == "UNKNOWN":
                c5_passed = False
                reasons.append(
                    "條件五❌：大盤 Regime 資料不足 (VIX/SPY/Gamma Flip 未知)，"
                    "無法排除危機，安全起見判定未通過"
                )
            elif regime in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS"):
                c5_passed = False
                if direction == "SHORT":
                    reasons.append(
                        f"條件五❌：大盤處於 `{regime}` 負 Gamma 踩踏／流動性危機模式，"
                        "宏觀鎖定期間嚴禁開立個股新空單（軋空與流動性斷層風險同樣極端）"
                    )
                else:
                    reasons.append(
                        f"條件五❌：大盤處於 `{regime}` 負 Gamma 踩踏模式，嚴禁開倉個股買方"
                    )
        except Exception as e:
            c5_passed = False
            reasons.append(
                f"條件五❌：大盤總經風控狀態抓取失敗，安全起見判定未通過: {e}"
            )

    if c5_passed:
        reasons.append("條件五✅：總經環境與財報事件風控安全")

    return c5_passed, days_to_er


class _OpportunityCostMixin:
    """候選標的篩選（多空候選挑選）。

    原「機會成本換股」情境 (Scenario 2) 已移除；右側六重鐵律已由多時間框架擠壓
    規則取代（`market_analysis/squeeze_entry/`，docs/strategies/10）。僅保留做空
    情境與 Scenario 3 TP 分層輪動目標共用的候選挑選，以及左側／做空鐵律共用的
    條件五（財報／總經安全閥）。
    """

    def _calculate_ev_proxy(
        self, symbol: str, skew_percentile: Optional[float] = None
    ) -> float:
        """
        Skew-Adjusted EV 期望值模型：
        以快取的 expected_move_upper 相對現貨的正規化上緣空間為基礎，
        並結合 Skew 偏斜度進行下行風險調整：
        Adjusted EV = Base EV * (1.0 - Downside Risk Penalty)
        當 Skew Percentile < 50% (偏恐慌/偏空) 時施加懲罰，避免單純因為波動大而誤判為高期望值。
        僅使用 market_cache（Cache-Aside），零額外 API 呼叫。
        is_stale 或 is_degraded 的快取視為不可信，回傳 0.0。
        """
        from database.market_cache import get_market_cache

        row = get_market_cache(symbol)
        if not row or row.get("is_stale") or row.get("is_degraded"):
            return 0.0
        spot = float(row.get("reference_spot_price") or 0.0)
        upper = float(row.get("expected_move_upper") or 0.0)
        if spot <= 0.0:
            return 0.0
        base_ev = (upper - spot) / spot

        # 若未提供 skew_percentile，嘗試從快取讀取
        if skew_percentile is None:
            try:
                from database.cache import get_kv_cache

                cached_sp = get_kv_cache(f"skew_percentile_{symbol.upper()}")
                if cached_sp is not None:
                    skew_percentile = float(cached_sp)
            except Exception:
                pass

        if skew_percentile is not None and skew_percentile < 50.0:
            downside_penalty = (
                (50.0 - skew_percentile) / 50.0
            ) * _SKEW_DOWNSIDE_PENALTY_FACTOR
            return float(max(0.0, base_ev * (1.0 - downside_penalty)))

        return float(base_ev)

    def _iter_rollover_candidates(
        self, user_id: int, exclude_symbols: Optional[set] = None
    ) -> Iterator[str]:
        """依序產出使用者 Watchlist 中可評估的候選標的代號。

        共用的排除規則 (多空候選來源一致)：已持有／呼叫端指定排除、核心防禦
        ETF (CORE_DEFENSE_ETF_SYMBOLS)、即將在財報緩衝期內發布財報的高波事件
        標的 (機構風控：避開二元事件黑天鵝)。Watchlist 讀取失敗時不產出任何值。
        """
        from database.calendar_cache import get_cached_earnings
        from database.watchlist import get_user_watchlist

        exclude = {
            s.upper() for s in (exclude_symbols or set())
        } | CORE_DEFENSE_ETF_SYMBOLS
        try:
            watchlist = get_user_watchlist(user_id)
        except Exception as e:
            logger.error(f"取得 user {user_id} watchlist 失敗: {e}")
            return

        today_dt = datetime.now().date()
        for sym, _ in watchlist:
            sym_u = str(sym).upper()
            if sym_u in exclude:
                continue

            try:
                earn = get_cached_earnings(sym_u)
                if earn and earn.get("earnings_date"):
                    earn_date_str = str(earn["earnings_date"])[:10]
                    earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                    diff_days = (earn_dt - today_dt).days
                    if 0 <= diff_days <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                        continue
            except Exception:
                pass

            yield sym_u

    def _find_best_rollover_target(
        self, user_id: int, exclude_symbols: Optional[set] = None
    ) -> str:
        """掃描使用者 Watchlist 與 market_cache 快取尋找下一個高 EV 衛星標的，若無則回傳 VOO。
        自動避開即將在 3 天內發布財報的高波事件標的。"""
        best_symbol = "VOO"
        best_ev = _EV_SPREAD_MIN_THRESHOLD  # 門檻 EV > _EV_SPREAD_MIN_THRESHOLD
        for sym_u in self._iter_rollover_candidates(user_id, exclude_symbols):
            ev = self._calculate_ev_proxy(sym_u)
            if ev > best_ev:
                best_ev = ev
                best_symbol = sym_u
        return best_symbol

    def _calculate_short_ev_proxy(
        self, symbol: str, radar: Optional[Mapping[str, Any]]
    ) -> float:
        """做空候選的期望值代理：下行預期波幅 × 空頭動能權重。

            base = (spot − expected_move_lower) / spot
            score = base × (100 − PSQ) / 100

        預期波幅本身是對稱的，真正讓排序具有方向性的是 PSQ 權重。門檻判定
        只看 base (與多頭 `_calculate_ev_proxy` 同一個 `_EV_SPREAD_MIN_THRESHOLD`
        口徑)，PSQ 權重只用於排序——否則做空候選的空間門檻會被權重暗中抬高。
        PSQ 高於 `_SHORT_CANDIDATE_MAX_PSQ` (動能不夠弱)、base 未達門檻、或
        market_cache 過期／降級時回傳 0.0。
        """
        from database.market_cache import get_market_cache

        if not radar:
            return 0.0
        psq = self._normalize_power_squeeze(radar.get("psq_result") or {})
        if psq > _SHORT_CANDIDATE_MAX_PSQ:
            return 0.0
        row = get_market_cache(symbol)
        if not row or row.get("is_stale") or row.get("is_degraded"):
            return 0.0
        spot = float(row.get("reference_spot_price") or 0.0)
        lower = float(row.get("expected_move_lower") or 0.0)
        if spot <= 0.0 or lower <= 0.0 or lower >= spot:
            return 0.0
        base = (spot - lower) / spot
        if base <= _EV_SPREAD_MIN_THRESHOLD:
            return 0.0
        return float(base * (100.0 - psq) / 100.0)

    def _find_best_short_target(
        self,
        user_id: int,
        exclude_symbols: Optional[set],
        radar_snapshot: Mapping[str, Mapping[str, Any]],
    ) -> Optional[str]:
        """尋找最佳做空候選 (SHORT_ENTRY 情境)，無合格者回傳 None。

        與 `_find_best_rollover_target` 共用 watchlist 迭代與排除規則，另外排除
        反向 ETF (做空反向 ETF 等於做多大盤，方向相反)。零網路 I/O：只讀
        market_cache 與呼叫端傳入的共享雷達快取快照 (`bot._latest_radar_data_cache`)；
        快照中沒有資料的標的直接略過，不為了挑候選而逐檔抓取雷達。
        """
        inverse_symbols = (
            set(INDEX_INVERSE_MAP.values())
            | set(SECTOR_INVERSE_MAP.values())
            | {
                v
                for variants in SINGLE_STOCK_INVERSE_MAP.values()
                for v in variants.values()
            }
        )
        best_symbol: Optional[str] = None
        best_score = 0.0
        for sym_u in self._iter_rollover_candidates(user_id, exclude_symbols):
            if sym_u in inverse_symbols:
                continue
            score = self._calculate_short_ev_proxy(sym_u, radar_snapshot.get(sym_u))
            if score > best_score:
                best_score = score
                best_symbol = sym_u
        return best_symbol

    def _normalize_power_squeeze(self, psq: Dict[str, Any]) -> float:
        """
        將 analyze_psq() 產生的 PSQResult (dict 形式，如 radar cache 中的 psq_result)
        正規化為 0-100 的 PowerSqueeze 分數，供 evaluate_opportunity_cost() 使用。
        重用既有的 squeeze_level / signal_direction / momentum_color / is_breakout_long/short
        分級，而非發明新的量化門檻。
        """
        level = str(psq.get("squeeze_level", "Normal"))
        direction = str(psq.get("signal_direction", "Neutral"))
        mom_color = str(psq.get("momentum_color", "Neutral"))
        is_bullish = direction == "Long" or mom_color in ("LightBlue", "Golden")
        is_bearish = direction == "Short" or mom_color in ("Red", "DarkBlue")

        table = {
            "Release": {"neutral": 10.0, "bull": 75.0, "bear": 5.0},
            "Normal": {"neutral": 30.0, "bull": 40.0, "bear": 20.0},
            "Mid": {"neutral": 60.0, "bull": 70.0, "bear": 45.0},
            "High": {"neutral": 50.0, "bull": 90.0, "bear": 10.0},
        }
        bucket = table.get(level, table["Normal"])
        score = (
            bucket["bull"]
            if is_bullish
            else (bucket["bear"] if is_bearish else bucket["neutral"])
        )

        if psq.get("is_breakout_long"):
            score = max(score, 95.0)
        elif psq.get("is_breakout_short"):
            score = min(score, 5.0)

        return float(max(0.0, min(100.0, score)))
