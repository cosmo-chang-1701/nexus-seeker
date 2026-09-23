"""evaluation_recorder.py — Regime 分類與進場鐵律評估的前向蒐集記錄器。

為什麼需要：GEX 相關門檻 (Put Wall、Gamma Flip、次級負 GEX 節點、2.2×Risk)
**無法回測**——edge scraper 的 `gex_snapshot` 與 core 的 `kv_cache gex_metrics_*`
全是 upsert、只留最新值，Regime 分類與進場評估也從未被記錄。唯一的校準資料
來源是從現在開始，把每次評估「當下實際使用的數值」存下來，再由離峰排程回填
事後走勢 (services/regime_outcome_labeler.py)。

熱路徑成本設計：
* 記錄呼叫只是從既有區域變數組一個 dict 並 append 到有界 deque (O(1))，不做 I/O。
* 只有在 `evaluation_source()` 脈絡內才記錄——來源未標記的呼叫 (單元測試、臨時
  腳本) 一律略過，不會污染資料。
* `flush_evaluations()` 由週期結尾一次批次寫入 (單一交易)。
* deque 有 maxlen：若 flush 長期失敗，丟棄最舊紀錄而非無限成長 (1GB VPS)。
* `config.ENABLE_REGIME_EVALUATION_LOG=false` 時完全 no-op。
"""

import contextvars
import json
import logging
import math
import re
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_BUFFER_MAXLEN = 512
_REASON_DIGEST_MAX = 512
_FEATURES_JSON_MAX = 2048
_NY_TZ = ZoneInfo("America/New_York")

_BUFFER: deque[dict[str, Any]] = deque(maxlen=_BUFFER_MAXLEN)
_SOURCE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "regime_evaluation_source", default=None
)
_CYCLE_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "regime_evaluation_cycle_context", default={}
)

_CONDITION_DIGITS = "一二三四五六"
_CONDITION_RE = re.compile(r"條件([一二三四五六])(✅|❌|⏭️)")


def _enabled() -> bool:
    try:
        import config

        return bool(getattr(config, "ENABLE_REGIME_EVALUATION_LOG", True))
    except Exception:
        return False


@contextmanager
def evaluation_source(source: str) -> Iterator[None]:
    """標記本脈絡內的評估來源 (PORTFOLIO_MONITOR / SYMBOL_VIEW)。"""
    token = _SOURCE.set(source)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def set_evaluation_source(source: Optional[str]) -> contextvars.Token:
    """不便使用 with 區塊的呼叫端用；務必以回傳的 token 呼叫 reset_evaluation_source。"""
    return _SOURCE.set(source)


def reset_evaluation_source(token: contextvars.Token) -> None:
    _SOURCE.reset(token)


def set_cycle_context(
    vix_spot: Optional[float] = None, macro_regime: Optional[str] = None
) -> None:
    _CYCLE_CONTEXT.set({"vix_spot": vix_spot, "macro_regime": macro_regime})


def current_bar_ts(now: Optional[datetime] = None) -> str:
    """美東時間向下取整至 15 分鐘 (去重鍵)。"""
    moment = (now or datetime.now(_NY_TZ)).astimezone(_NY_TZ)
    floored = moment.replace(
        minute=moment.minute - moment.minute % 15, second=0, microsecond=0
    )
    return floored.isoformat()


def parse_condition_masks(reason: str) -> tuple[Optional[int], Optional[int]]:
    """從鐵律 reason 字串解析 (通過遮罩, 已評估遮罩)。bit i = 條件 i+1。

    best-effort：只認得 `條件X✅/❌/⏭️` 標記，無任何標記時回傳 (None, None)。
    """
    passed = 0
    evaluated = 0
    found = False
    for digit, mark in _CONDITION_RE.findall(reason or ""):
        found = True
        bit = 1 << _CONDITION_DIGITS.index(digit)
        if mark == "⏭️":
            continue
        evaluated |= bit
        if mark == "✅":
            passed |= bit
    if not found:
        return None, None
    return passed, evaluated


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f != 0.0 else None


def _append(row: dict[str, Any]) -> None:
    source = _SOURCE.get()
    if source is None or not _enabled():
        return
    cycle = _CYCLE_CONTEXT.get()
    row.setdefault("vix_spot", cycle.get("vix_spot"))
    row.setdefault("macro_regime", cycle.get("macro_regime"))
    row["source"] = source
    row.setdefault("bar_ts", current_bar_ts())
    reason = row.get("reason_digest")
    if isinstance(reason, str):
        row["reason_digest"] = reason[:_REASON_DIGEST_MAX]
    features = row.get("features_json")
    if isinstance(features, Mapping):
        try:
            text = json.dumps(
                {k: _clean(v) for k, v in features.items()}, ensure_ascii=False
            )
            row["features_json"] = text if len(text) <= _FEATURES_JSON_MAX else None
        except (TypeError, ValueError):
            row["features_json"] = None
    _BUFFER.append({k: _clean(v) for k, v in row.items()})


# 會放行「多頭新開倉」的 Regime。新增這類 Regime 時**必須**同步加進來，
# 否則它會被記成 direction=None / decision=0——前向蒐集資料裡看起來像「評估過
# 但拒絕」，而 `calibration forward-report` 正是依 decision 分組統計勝率的
# (forward_log.py::build_forward_report)。漏加等於讓新路徑的校準資料靜默歸零。
_LONG_ENTRY_REGIMES: frozenset[str] = frozenset(
    {
        "REGIME_I_LEFT_CATCH",
        "REGIME_III_RIGHT_MOMENTUM",
        "REGIME_III_B_TREND_CONTINUATION",
    }
)


def _walls(gex_profile_data: Any) -> dict[str, Optional[float]]:
    if not isinstance(gex_profile_data, Mapping):
        return {"call_wall": None, "put_wall": None, "net_gex": None}
    return {
        "call_wall": _num(gex_profile_data.get("call_wall")),
        "put_wall": _num(gex_profile_data.get("put_wall")),
        "net_gex": _num(gex_profile_data.get("net_gex")),
    }


def record_regime_classification(
    symbol: str,
    spot: float,
    regime: str,
    reason: str,
    gex_profile_data: Any,
    session_vwap: float = 0.0,
    atr_15m: float = 0.0,
    rsi_15m: float = float("nan"),
) -> None:
    try:
        row: dict[str, Any] = {
            "symbol": symbol.upper(),
            "evaluator": "REGIME_CLASSIFIER",
            "regime": regime,
            "direction": "SHORT"
            if regime == "REGIME_V_BREAKDOWN_CHASE"
            else ("LONG" if regime in _LONG_ENTRY_REGIMES else None),
            "decision": 1
            if regime in _LONG_ENTRY_REGIMES or regime == "REGIME_V_BREAKDOWN_CHASE"
            else 0,
            "spot": _num(spot),
            "session_vwap": _num(session_vwap),
            "atr_15m": _num(atr_15m),
            "rsi_15m": _clean(float(rsi_15m)) if rsi_15m is not None else None,
            "reason_digest": reason,
        }
        row.update(_walls(gex_profile_data))
        _append(row)
    except Exception as e:  # 記錄器永不影響交易路徑
        logger.debug(f"[EvalRecorder] regime 記錄失敗: {e}")


def record_short_evaluation(ev: Any, symbol: str, regime: Optional[str] = None) -> None:
    """記錄 `ShortEntryEvaluation` (以 duck typing 讀取，避免反向依賴引擎模型)。"""
    try:
        conditions = tuple(getattr(ev, "conditions", ()) or ())
        passed = 0
        evaluated = 0
        for i, c in enumerate(conditions):
            if c is None:
                continue
            evaluated |= 1 << i
            if c:
                passed |= 1 << i
        _append(
            {
                "symbol": symbol.upper(),
                "evaluator": "ENTRY_SHORT",
                "regime": regime,
                "direction": "SHORT",
                "sub_mode": getattr(ev, "sub_mode", None),
                "decision": 1 if getattr(ev, "all_passed", False) else 0,
                "conditions_mask": passed,
                "conditions_evaluated_mask": evaluated,
                "spot": _num(getattr(ev, "spot", None)),
                "gamma_flip": _num(getattr(ev, "gamma_flip", None)),
                "call_wall": _num(getattr(ev, "call_wall", None)),
                "put_wall": _num(getattr(ev, "put_wall", None)),
                "resistance_wall": _num(getattr(ev, "resistance_wall", None)),
                "next_negative_node": _num(getattr(ev, "next_negative_node", None)),
                "net_gex": _num(getattr(ev, "net_gex", None)),
                "session_vwap": _num(getattr(ev, "session_vwap", None)),
                "atr_15m": _num(getattr(ev, "atr_15m", None)),
                "atr_1d": _num(getattr(ev, "atr_1d", None)),
                "ivr": _num(getattr(ev, "ivr", None)),
                "reason_digest": getattr(ev, "reason", None),
            }
        )
    except Exception as e:
        logger.debug(f"[EvalRecorder] short 記錄失敗: {e}")


def record_gate_reason(
    evaluator: str,
    symbol: str,
    spot: float,
    passed: bool,
    reason: str,
    direction: str = "LONG",
    candidate_radar: Optional[Mapping[str, Any]] = None,
) -> None:
    """記錄右側／左側六重鐵律結果 (條件遮罩以正則解析 reason，best-effort)。"""
    try:
        mask, evaluated = parse_condition_masks(reason)
        row: dict[str, Any] = {
            "symbol": symbol.upper(),
            "evaluator": evaluator,
            "direction": direction,
            "decision": 1 if passed else 0,
            "conditions_mask": mask,
            "conditions_evaluated_mask": evaluated,
            "spot": _num(spot),
            "reason_digest": reason,
        }
        if candidate_radar:
            row.update(_walls(candidate_radar.get("gex_profile_data")))
            iv = candidate_radar.get("iv_metrics")
            if isinstance(iv, Mapping):
                row["ivr"] = _num(iv.get("iv_rank"))
        _append(row)
    except Exception as e:
        logger.debug(f"[EvalRecorder] gate 記錄失敗: {e}")


# 本輪「不出場、改抬棘輪停損」的分層：訊號押注的是**續抱**，方向與部位相同。
# 其餘分層 (SL 平倉、TP 減碼、極端熔斷、IV 驟降) 押注的是**離場**，方向與部位相反。
HOLD_EXIT_TIERS: frozenset[str] = frozenset(
    {"SL_TRAILING_BREAKEVEN", "TP1_TREND_EXEMPT"}
)


def exit_evaluator_name(tier: str, position_side: str) -> str:
    """出場分層的 evaluator 名稱。

    每個分層、每個部位方向各自獨立：去重鍵 (symbol, evaluator, source, bar_ts)
    不含 user_id，若共用單一 evaluator，同一根 K 棒上不同使用者的不同分層
    (例如 A 觸發 SL-結構失效、B 觸發 SL-動態保本) 會互相覆蓋。分開之後，
    被合併的只剩「同一標的、同一分層、同一方向」——那是同一個事實。
    """
    suffix = "_SHORT" if position_side == "SHORT" else ""
    return f"EXIT_{tier}{suffix}"


def record_exit_signal(
    symbol: str,
    tier: str,
    position_side: str,
    metrics: Mapping[str, Any],
    stop_level: Optional[float] = None,
    advisory: bool = False,
    asset_class: Optional[str] = None,
) -> None:
    """記錄微觀結構出場決策矩陣的分層觸發 (handoff §5.4 SL 分層檢討的資料來源)。

    記錄的是引擎的**原始訊號**，而非推播結果：顧問模式 (B&H) 會把多數分層
    丟棄或改寫為位階告知，但要評估的是「這一層的訊號本身準不準」，所以轉換
    之前就記錄，並以 `advisory` 旗標讓報告端分開統計。

    `direction` 是訊號押注的方向 (見 `HOLD_EXIT_TIERS`)，讓前向報告沿用
    「win = 訊號方向先觸及有利帶」的單一語意：多頭部位的 SL 平倉 direction=SHORT，
    其 win 代表出場後價格確實下跌 (出場正確)，loss 即為被洗盤掃出。
    停損價刻意不放進 `stop_price` 欄位——那一欄由 labeler 的 `plan_outcome`
    依 direction 解讀為進場計畫的停損，對出場訊號語意不符。
    """
    try:
        side = "SHORT" if position_side == "SHORT" else "LONG"
        opposite = "LONG" if side == "SHORT" else "SHORT"
        _append(
            {
                "symbol": symbol.upper(),
                "evaluator": exit_evaluator_name(tier, side),
                "direction": side if tier in HOLD_EXIT_TIERS else opposite,
                "sub_mode": tier,
                "decision": 1,
                "spot": _num(metrics.get("spot_price")),
                "gamma_flip": _num(metrics.get("gamma_flip")),
                "call_wall": _num(metrics.get("call_wall")),
                "put_wall": _num(metrics.get("put_wall")),
                "resistance_wall": _num(metrics.get("resistance_wall")),
                "net_gex": _num(metrics.get("net_gex")),
                "session_vwap": _num(metrics.get("session_vwap")),
                "atr_15m": _num(metrics.get("atr_15m")),
                "ivr": _num(metrics.get("ivr")),
                "features_json": {
                    "position_side": side,
                    "advisory": bool(advisory),
                    "asset_class": asset_class,
                    "avg_cost": _num(metrics.get("avg_cost")),
                    "stop_level": _num(stop_level),
                    "ratchet_stop": _num(metrics.get("ratchet_stop")),
                    "support_wall": _num(metrics.get("support_wall")),
                },
            }
        )
    except Exception as e:
        logger.debug(f"[EvalRecorder] exit 記錄失敗: {e}")


def pending_count() -> int:
    return len(_BUFFER)


def clear_buffer() -> None:
    _BUFFER.clear()


async def flush_evaluations() -> int:
    """批次寫入緩衝區；記憶體內先依去重鍵合併 (同一 bar 內後寫覆蓋前寫)。"""
    if not _BUFFER:
        return 0
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    while _BUFFER:
        row = _BUFFER.popleft()
        key = (
            row.get("symbol"),
            row.get("evaluator"),
            row.get("source"),
            row.get("bar_ts"),
        )
        rows[key] = row
    try:
        from database.regime_evaluation_log import insert_regime_evaluations

        return await insert_regime_evaluations(list(rows.values()))
    except Exception as e:
        logger.error(f"[EvalRecorder] 評估紀錄批次寫入失敗 ({len(rows)} 列): {e}")
        return 0
