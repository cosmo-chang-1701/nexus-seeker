import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from database.cache import save_kv_cache

logger = logging.getLogger(__name__)


class DataContaminationException(RuntimeError):
    """Raised when cached and live prices diverge beyond the hard stale-lock threshold."""


@dataclass(frozen=True)
class AlignmentDecision:
    symbol: str
    order_id: int
    action: str  # 'PRICE_UP', 'NO_ALIGNMENT_NEEDED', 'SUPPRESSED'
    current_order_price: float
    spot_price: float
    suggested_price: float
    original_qty: int
    suggested_qty: int
    is_size_down: bool
    reasons: list[str]
    alert_text: str | None
    system_status_flag: str = ""
    system_instruction_directive: str = ""
    drift_pct: float | None = None
    cache_price: float | None = None
    live_price: float | None = None


def _now_utc() -> datetime:
    # DB timestamps are stored in SQLite CURRENT_TIMESTAMP (UTC-ish). We treat them as UTC-naive.
    return datetime.utcnow()


def _parse_sqlite_timestamp(ts: str) -> Optional[datetime]:
    ts = (ts or "").strip()
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt)
        except Exception:
            continue
    return None


def _is_active_order_in_db(order_id: int, user_id: int) -> bool:
    conn = None
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM active_orders WHERE id = ? AND user_id = ? LIMIT 1",
            (order_id, user_id),
        )
        return cursor.fetchone() is not None
    except Exception as e:
        logger.warning(f"SYSTEM_SWEEP: DB_SYNC_FAILED for order_id={order_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def _recent_clear_position_suppression(
    user_id: int, symbol: str, *, window_hours: int = 24
) -> bool:
    """Return True if symbol should suppress buy/alignment due to recent clear/zero holding."""
    conn = None
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT metadata, updated_at
            FROM assets
            WHERE user_id = ? AND symbol = ? AND context_type = 'HOLDING'
            LIMIT 1
            """,
            (user_id, symbol.upper()),
        )
        row = cursor.fetchone()
        if not row:
            return False

        meta_json, updated_at = row
        meta = json.loads(meta_json) if meta_json else {}
        qty = float(meta.get("quantity", 0.0) or 0.0)
        if qty != 0.0:
            return False

        t = _parse_sqlite_timestamp(str(updated_at or ""))
        if t is None:
            return False

        if _now_utc() - t <= timedelta(hours=window_hours):
            return True
        return False
    except Exception as e:
        logger.warning(f"SYSTEM_SWEEP: HOLDING_SYNC_FAILED for {symbol}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def _expected_move_value(
    spot_price: float, iv: float, days_to_expiration: float
) -> float:
    if spot_price <= 0 or iv <= 0 or days_to_expiration <= 0:
        return 0.0
    return spot_price * iv * math.sqrt(days_to_expiration / 365.0)


def _log_decision_to_sqlite(
    user_id: int, symbol: str, order_id: int, payload: dict[str, Any]
) -> None:
    # Use kv_cache as a lightweight SQLite-backed audit trail without schema changes.
    # We only keep the latest decision per (user, symbol, order).
    try:
        key = f"telemetry:alignment_decision:{user_id}:{symbol.upper()}:{order_id}"
        save_kv_cache(key, payload)
    except Exception as e:
        logger.debug(f"SYSTEM_LOG: kv_cache write skipped: {e}")


def _build_alert_text(
    *,
    symbol: str,
    reason_text: str,
    suggested_price: float,
    sizing_multiplier: float,
) -> str:
    final_action = f"建議對齊修正至 ${suggested_price:.2f}"
    if sizing_multiplier < 0.999:
        final_action += " (打折控倉 75%)"

    return (
        f"🛡️ 標的: {symbol.upper()} | 偵測到現價與掛單偏離，但因 [{reason_text}], "
        f"系統已自動截斷/修正提價建議。 {final_action}"
    )


def _build_suppressed_decision(
    *,
    symbol: str,
    order_id: int,
    current_order_price: float,
    spot_price: float,
    original_qty: int,
    reasons: list[str],
    system_status_flag: str,
    system_instruction_directive: str,
    drift_pct: float | None = None,
    cache_price: float | None = None,
    live_price: float | None = None,
) -> AlignmentDecision:
    return AlignmentDecision(
        symbol=symbol,
        order_id=order_id,
        action="SUPPRESSED",
        current_order_price=current_order_price,
        spot_price=spot_price,
        suggested_price=current_order_price,
        original_qty=original_qty,
        suggested_qty=original_qty,
        is_size_down=False,
        reasons=reasons,
        alert_text=None,
        system_status_flag=system_status_flag,
        system_instruction_directive=system_instruction_directive,
        drift_pct=drift_pct,
        cache_price=cache_price,
        live_price=live_price,
    )


def _classify_uoa_squeeze(
    uoa_array: list[dict[str, Any]] | None,
    macro_event_dates: set[str],
) -> tuple[bool, str]:
    if not uoa_array:
        return False, ""

    for item in uoa_array:
        raw_ratio = item.get("volume_to_oi_ratio", item.get("ratio", 0.0))
        try:
            ratio = float(raw_ratio or 0.0)
        except Exception:
            ratio = 0.0

        expiry = str(item.get("expiration_date", item.get("expiry", "")) or "")
        if ratio > 3.0 and expiry in macro_event_dates:
            strike = float(item.get("strike", 0.0) or 0.0)
            option_type = str(
                item.get("option_type", item.get("type", "")) or ""
            ).upper()
            return (
                True,
                f"[機構鎖定 / 強勢逼空 Gamma Squeeze] {expiry} {option_type} ${strike:.2f} ({ratio:.2f}x)",
            )

    return False, ""


async def generate_alignment_decision(
    *,
    user_id: int,
    order_id: int,
    symbol: str,
    current_order_price: float,
    spot_price: float,
    original_qty: int,
    iv: float,
    hist_iv: float,
    iv_rank: Optional[float],
    max_pain_price: Optional[float],
    prev_max_pain: float = 0.0,
    skew_percentile: float = 0.5,
    put_call_ratio: float = 1.0,
    days_to_expiration: float = 7.0,
    prev_close: float = 0.0,
    cache_price: Optional[float] = None,
    live_price: Optional[float] = None,
    max_drift_pct: float = 1.5,
    deep_sea_gap_lock_pct: float = 5.0,
    order_side: str = "BUY",
    holding_type: str = "",
    holding_shares: float = 0.0,
    uoa_array: Optional[list[dict[str, Any]]] = None,
    macro_event_dates: Optional[set[str]] = None,
    emit_suppressed_decision: bool = False,
) -> AlignmentDecision | None:
    """Central alignment alert pipeline with stale-lock, fortress relock and sovereign gating.

    Returns None when suppressed unless ``emit_suppressed_decision=True``.
    """

    symbol = symbol.upper()
    current_order_price = float(current_order_price or 0.0)
    spot_price = float(spot_price or 0.0)
    original_qty = max(1, int(round(float(original_qty or 1))))
    cache_price = float(cache_price or 0.0)
    live_price = float(live_price or 0.0)
    effective_live_price = live_price if live_price > 0 else spot_price

    # Pillar 4 (Sweeper): hard DB sync
    if not _is_active_order_in_db(order_id, user_id):
        logger.info(
            f"SYSTEM_SWEEP: ORDER_NOT_ACTIVE for {symbol} (order_id={order_id})"
        )
        _log_decision_to_sqlite(
            user_id,
            symbol,
            order_id,
            {
                "ts": _now_utc().isoformat(),
                "action": "SUPPRESSED",
                "reason": "ORDER_NOT_ACTIVE",
            },
        )
        if emit_suppressed_decision:
            return _build_suppressed_decision(
                symbol=symbol,
                order_id=order_id,
                current_order_price=current_order_price,
                spot_price=spot_price,
                original_qty=original_qty,
                reasons=["ORDER_NOT_ACTIVE"],
                system_status_flag="FORTRESS RE-LOCKED",
                system_instruction_directive="委託單已失效，不允許對齊調整。",
            )
        return None

    if _recent_clear_position_suppression(user_id, symbol):
        logger.info(f"SYSTEM_SWEEP: RECENT_CLEAR_POSITION for {symbol}")
        _log_decision_to_sqlite(
            user_id,
            symbol,
            order_id,
            {
                "ts": _now_utc().isoformat(),
                "action": "SUPPRESSED",
                "reason": "RECENT_CLEAR_POSITION",
            },
        )
        if emit_suppressed_decision:
            return _build_suppressed_decision(
                symbol=symbol,
                order_id=order_id,
                current_order_price=current_order_price,
                spot_price=spot_price,
                original_qty=original_qty,
                reasons=["RECENT_CLEAR_POSITION"],
                system_status_flag="FORTRESS RE-LOCKED",
                system_instruction_directive="最近已清倉，暫停追價與自動調整。",
            )
        return None

    if cache_price > 0 and effective_live_price > 0:
        drift_pct = (
            abs(effective_live_price - cache_price) / effective_live_price * 100.0
        )
        if drift_pct > max_drift_pct:
            _log_decision_to_sqlite(
                user_id,
                symbol,
                order_id,
                {
                    "ts": _now_utc().isoformat(),
                    "action": "SUPPRESSED",
                    "reason": "DATA_CONTAMINATION",
                    "cache_price": cache_price,
                    "live_price": effective_live_price,
                    "drift_pct": drift_pct,
                },
            )
            if emit_suppressed_decision:
                return _build_suppressed_decision(
                    symbol=symbol,
                    order_id=order_id,
                    current_order_price=current_order_price,
                    spot_price=spot_price,
                    original_qty=original_qty,
                    reasons=[f"DATA_CONTAMINATION drift={drift_pct:.2f}%"],
                    system_status_flag="FORTRESS RE-LOCKED",
                    system_instruction_directive="快取與即時報價偏離過大，已終止改價建議。",
                    drift_pct=round(drift_pct, 2),
                    cache_price=cache_price,
                    live_price=effective_live_price,
                )
            raise DataContaminationException(
                f"{symbol} data contamination detected: drift={drift_pct:.2f}%"
            )

    side = str(order_side or "BUY").upper()
    if side == "BUY" and current_order_price > 0 and effective_live_price > 0:
        deep_sea_gap_pct = (
            (effective_live_price - current_order_price) / effective_live_price * 100.0
        )
        if deep_sea_gap_pct > deep_sea_gap_lock_pct:
            _log_decision_to_sqlite(
                user_id,
                symbol,
                order_id,
                {
                    "ts": _now_utc().isoformat(),
                    "action": "SUPPRESSED",
                    "reason": "FORTRESS_RELOCK_DEEP_SEA",
                    "gap_pct": deep_sea_gap_pct,
                },
            )
            if emit_suppressed_decision:
                return _build_suppressed_decision(
                    symbol=symbol,
                    order_id=order_id,
                    current_order_price=current_order_price,
                    spot_price=spot_price,
                    original_qty=original_qty,
                    reasons=[f"DEEP_SEA_GAP {deep_sea_gap_pct:.2f}%"],
                    system_status_flag="FORTRESS RE-LOCKED",
                    system_instruction_directive="現價遠離深海買單超過 5%，禁止追價改單。",
                    cache_price=cache_price if cache_price > 0 else None,
                    live_price=effective_live_price
                    if effective_live_price > 0
                    else None,
                )
            return None

    holding_type_norm = str(holding_type or "").upper()
    if holding_type_norm == "PURE_STOCK_100X":
        if holding_shares <= 0.0:
            sovereign_directive = (
                "空倉維持被動深海限價；硬鎖現金擔保/裸賣選擇權觸發，拒絕追價。"
            )
        else:
            sovereign_directive = "純現貨防線啟動，禁止追價改單與槓桿擴張。"
        if emit_suppressed_decision:
            return _build_suppressed_decision(
                symbol=symbol,
                order_id=order_id,
                current_order_price=current_order_price,
                spot_price=spot_price,
                original_qty=original_qty,
                reasons=["PURE_STOCK_100X_SOVEREIGN_GATE"],
                system_status_flag="FORTRESS RE-LOCKED",
                system_instruction_directive=sovereign_directive,
                cache_price=cache_price if cache_price > 0 else None,
                live_price=effective_live_price if effective_live_price > 0 else None,
            )
        return None

    macro_dates = macro_event_dates or set()
    squeeze_flag, squeeze_note = _classify_uoa_squeeze(uoa_array, macro_dates)
    if squeeze_flag:
        if emit_suppressed_decision:
            return _build_suppressed_decision(
                symbol=symbol,
                order_id=order_id,
                current_order_price=current_order_price,
                spot_price=spot_price,
                original_qty=original_qty,
                reasons=[squeeze_note],
                system_status_flag="FORTRESS RE-LOCKED",
                system_instruction_directive="機構籌碼鎖定事件週，策略切換防守持有，抑制改價。",
                cache_price=cache_price if cache_price > 0 else None,
                live_price=effective_live_price if effective_live_price > 0 else None,
            )
        return None

    from services.telemetry_pricing_engine import calculate_telemetry_price

    # Baseline engine suggestion (may be aggressive; we will gate PRICE_UP).
    suggested_price, suggested_qty, logs = await calculate_telemetry_price(
        symbol=symbol,
        base_price=current_order_price,
        spot_price=spot_price,
        iv=iv,
        hist_iv=hist_iv,
        max_pain=float(max_pain_price or 0.0),
        prev_max_pain=prev_max_pain,
        skew_percentile=skew_percentile,
        days_to_expiration=days_to_expiration,
        prev_close=prev_close,
        base_quantity=original_qty,
    )

    is_price_up = suggested_price > current_order_price + 1e-9
    reasons: list[str] = []

    # Default sizing multiplier inferred from baseline output.
    sizing_multiplier = min(1.0, suggested_qty / float(original_qty or 1))

    # Pillar 1: IV Rank Fuse (only for PRICE_UP)
    effective_iv_rank = 1.0 if iv_rank is None else float(iv_rank)
    if is_price_up and effective_iv_rank > 0.70:
        logger.warning(f"SYSTEM_LOCK: IV_TOO_HIGH for {symbol}")
        _log_decision_to_sqlite(
            user_id,
            symbol,
            order_id,
            {
                "ts": _now_utc().isoformat(),
                "action": "SUPPRESSED",
                "reason": "IV_TOO_HIGH",
                "iv_rank": effective_iv_rank,
            },
        )
        return None

    # Pillar 3: Skew & PCR inverse sentiment correction (only meaningful for PRICE_UP)
    if is_price_up and skew_percentile > 0.90 and put_call_ratio > 1.5:
        suggested_price = round(float(suggested_price) * 0.90, 2)
        reasons.append(
            f"Skew/PCR 極端恐慌 (Skew {skew_percentile*100:.1f}%, PCR {put_call_ratio:.2f}) 結構折價 10%"
        )

    if is_price_up and skew_percentile < 0.10:
        sizing_multiplier = min(sizing_multiplier, 0.75)
        reasons.append(
            f"Skew 低位崩壞 (Skew {skew_percentile*100:.1f}%) 觸發尾端風險控倉 75%"
        )

    # Recompute suggested qty after Pillar 3 sizing constraint.
    suggested_qty = max(1, int(math.floor(original_qty * sizing_multiplier)))

    # Pillar 2: Max Pain & Expected Move hard boundaries (only for PRICE_UP)
    if is_price_up:
        em_value = _expected_move_value(spot_price, iv, days_to_expiration)
        expected_move_lower_band = em_value  # Spec: use this scalar in the formula

        mp = float("inf")
        if max_pain_price is not None and float(max_pain_price) > 0:
            mp = float(max_pain_price)
        else:
            reasons.append("Max Pain 未提供：僅以 Expected Move 物理邊界截斷")

        upper_bound = min(mp, spot_price - expected_move_lower_band)
        upper_bound = round(float(upper_bound), 2)

        if suggested_price > upper_bound:
            if math.isfinite(mp) and upper_bound == round(mp, 2):
                pct = ((spot_price - mp) / mp * 100.0) if mp > 0 else 0.0
                reasons.append(f"現價高於最大痛點 {pct:.1f}%：提價被硬截斷")
            else:
                reasons.append("突破預期區間 (Expected Move) 下緣：提價被硬截斷")

            suggested_price = upper_bound

        # Edge case: if after clamp markdown we are not strictly above the current order, no PRICE_UP needed.
        if suggested_price <= current_order_price + 1e-9:
            logger.info(f"SYSTEM_BOUND: NO_ALIGNMENT_NEEDED for {symbol}")
            _log_decision_to_sqlite(
                user_id,
                symbol,
                order_id,
                {
                    "ts": _now_utc().isoformat(),
                    "action": "NO_ALIGNMENT_NEEDED",
                    "current_order_price": current_order_price,
                    "suggested_price": suggested_price,
                },
            )
            return None

    # If baseline suggested price isn't moving up, keep prior UX and do not force suppression.
    action = "PRICE_UP" if is_price_up else "NO_ALIGNMENT_NEEDED"

    alert_text: str | None = None
    if is_price_up:
        reason_text = " / ".join(reasons) if reasons else "衍生品結構風控模組"
        alert_text = _build_alert_text(
            symbol=symbol,
            reason_text=reason_text,
            suggested_price=float(suggested_price),
            sizing_multiplier=sizing_multiplier,
        )

    decision = AlignmentDecision(
        symbol=symbol,
        order_id=order_id,
        action=action,
        current_order_price=current_order_price,
        spot_price=spot_price,
        suggested_price=float(suggested_price),
        original_qty=original_qty,
        suggested_qty=int(suggested_qty),
        is_size_down=int(suggested_qty) < original_qty,
        reasons=reasons,
        alert_text=alert_text,
        cache_price=cache_price if cache_price > 0 else None,
        live_price=effective_live_price if effective_live_price > 0 else None,
    )

    _log_decision_to_sqlite(
        user_id,
        symbol,
        order_id,
        {
            "ts": _now_utc().isoformat(),
            "action": decision.action,
            "current_order_price": decision.current_order_price,
            "spot_price": decision.spot_price,
            "suggested_price": decision.suggested_price,
            "original_qty": decision.original_qty,
            "suggested_qty": decision.suggested_qty,
            "reasons": decision.reasons,
            "iv_rank": effective_iv_rank,
            "max_pain_price": max_pain_price,
            "skew_percentile": skew_percentile,
            "put_call_ratio": put_call_ratio,
        },
    )

    # Also echo detailed baseline logs for console traceability if anything constrained/suppressed.
    if reasons:
        logger.info(
            f"SYSTEM_DEFENSE: {symbol} order_id={order_id} reasons={reasons} baseline_logs={logs[:2]}"
        )

    return decision
