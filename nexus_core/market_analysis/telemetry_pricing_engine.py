import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import config
from database.cache import save_kv_cache

logger = logging.getLogger(__name__)


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
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM active_orders WHERE id = ? AND user_id = ? LIMIT 1",
                (order_id, user_id),
            )
            return cursor.fetchone() is not None
    except Exception as e:
        logger.warning(f"SYSTEM_SWEEP: DB_SYNC_FAILED for order_id={order_id}: {e}")
        return False


def _recent_clear_position_suppression(
    user_id: int, symbol: str, *, window_hours: int = 24
) -> bool:
    """Return True if symbol should suppress buy/alignment due to recent clear/zero holding."""
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
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
) -> AlignmentDecision | None:
    """Central alignment alert pipeline with 4 defensive pillars.

    Returns None when suppressed (ghost order / clear-position / IV fuse / no alignment needed).
    """

    symbol = symbol.upper()
    current_order_price = float(current_order_price or 0.0)
    spot_price = float(spot_price or 0.0)
    original_qty = max(1, int(round(float(original_qty or 1))))

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
