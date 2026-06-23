"""Shared business logic for order telemetry pricing and alignment.

Extracts reusable order-related computations from the UI layer (cogs/order_ui.py)
to enable independent unit testing and eliminate ~120 lines of duplication
between DynamicOrderModal.on_submit() and OrderUICog.add_order().
"""

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def fetch_cache_and_live_price(symbol: str) -> tuple[float, float]:
    """Fetch one cached snapshot and one forced-refresh live snapshot for drift checking."""
    from services import market_data_service

    cache_price = 0.0
    live_price = 0.0

    cached_quote = await market_data_service.get_quote(symbol)
    if cached_quote:
        cache_price = float(cached_quote.get("c") or 0.0)

    market_data_service.clear_quote_cache()
    fresh_quote = await market_data_service.get_quote(symbol)
    if fresh_quote:
        live_price = float(fresh_quote.get("c") or 0.0)

    yfinance_quote = await market_data_service.get_yfinance_quote(symbol)
    yfinance_price = float(yfinance_quote.get("c") or 0.0) if yfinance_quote else 0.0
    if yfinance_price > 0.0:
        live_price = yfinance_price

    if live_price <= 0.0:
        hist_df = await market_data_service.get_history_df(symbol, period="2d")
        if not hist_df.empty:
            live_price = float(hist_df["Close"].iloc[-1])

    return cache_price, live_price


def resolve_holding_type_and_rows(
    *, holdings: list[dict], trades: list[tuple]
) -> tuple[str, dict[str, dict]]:
    """Classify holding type and build symbol-keyed holding map."""
    holding_map = {
        str(row.get("symbol", "")).upper(): row
        for row in holdings
        if isinstance(row, dict) and row.get("symbol")
    }
    if (not trades) and holdings:
        return "PURE_STOCK_100X", holding_map
    if any((t[2] is not None) for t in trades):
        return "COMPLEX_OPTIONS", holding_map
    return "LEVERAGED_MARGIN", holding_map


async def resolve_telemetry_pricing(
    symbol: str,
    order_type: str,
    base_quantity: int | float,
) -> tuple[float, float, float, int, list[str]]:
    """Consolidated telemetry auto-pricing logic.

    Shared by DynamicOrderModal.on_submit() and OrderUICog.add_order().
    Eliminates ~120 lines of duplicated code.

    Returns:
        (limit_val, stop_val, trailing_val, final_qty, telemetry_logs)

    Raises:
        ValueError: if spot price cannot be resolved.
    """
    from services import market_data_service
    from market_analysis.sentiment_engine import SentimentEngine
    from services.telemetry_pricing_engine import calculate_telemetry_price

    # 1. Resolve spot price
    try:
        quote = await market_data_service.get_quote(symbol)
        spot_price = quote.get("c", 0.0)
        if spot_price <= 0.0:
            df_temp = await market_data_service.get_history_df(symbol, period="2d")
            if not df_temp.empty:
                spot_price = float(df_temp["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"Error fetching quote for telemetry fallback: {e}")
        spot_price = 0.0

    if spot_price <= 0.0:
        raise ValueError(
            f"無法獲取標的 {symbol} 的現有價格以進行遙測定價。請手動輸入價格。"
        )

    # 2. Resolve IV metrics
    iv = 0.35
    hist_iv = 0.30
    try:
        iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        if (
            iv_metrics
            and iv_metrics.current_iv is not None
            and iv_metrics.current_iv > 0
        ):
            iv = iv_metrics.current_iv
            hist_iv = iv / 1.1
    except Exception as e:
        logger.warning(f"Error fetching IV metrics for {symbol}: {e}")

    # 3. Resolve skew
    skew_val = 0.5
    try:
        skew_metrics = await SentimentEngine.calculate_skew(symbol)
        if skew_metrics and "skew" in skew_metrics:
            skew = float(skew_metrics["skew"])
            if skew > 5.0:
                skew_val = 0.98
            elif skew < -2.0:
                skew_val = 0.02
    except Exception as e:
        logger.warning(f"Error calculating skew for {symbol}: {e}")

    prev_close = quote.get("pc", spot_price)

    # 4. Calculate base_price based on order_type
    if order_type in ("LIMIT", "STOP_LIMIT"):
        base_price = spot_price
    elif order_type == "STOP":
        base_price = spot_price
    elif order_type == "TRAILING_STOP_USD":
        base_price = spot_price * 0.05
    elif order_type == "TRAILING_STOP_PCT":
        base_price = 5.0
    else:
        base_price = spot_price

    # 5. Call telemetry pricing engine
    (
        resolved_price,
        resolved_qty,
        telemetry_logs,
    ) = await calculate_telemetry_price(
        symbol=symbol,
        base_price=base_price,
        spot_price=spot_price,
        iv=iv,
        hist_iv=hist_iv,
        max_pain=spot_price,
        prev_max_pain=spot_price,
        skew_percentile=skew_val,
        prev_close=prev_close,
        base_quantity=base_quantity,
    )

    # 6. Map resolved price to order type fields
    limit_val = 0.0
    stop_val = 0.0
    trailing_val = 0.0

    if order_type == "LIMIT":
        limit_val = resolved_price
    elif order_type == "STOP":
        stop_val = resolved_price
    elif order_type == "STOP_LIMIT":
        limit_val = resolved_price
        stop_val = resolved_price * 0.98
    elif order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
        trailing_val = resolved_price

    final_qty = max(1, int(resolved_qty))

    return limit_val, stop_val, trailing_val, final_qty, telemetry_logs


async def apply_telemetry_to_orders(
    user_id: int,
    orders: list[dict],
    suggestions: dict[int, tuple[float, int]],
    holding_type: str,
    holding_map: dict[str, dict],
) -> tuple[int, list[str]]:
    """Core loop for the one-click telemetry apply button.

    Extracted from ApplyTelemetryView.apply_telemetry_button().

    Returns:
        (updated_count, detail_lines)
    """
    from market_analysis.telemetry_pricing_engine import (
        DataContaminationException,
        generate_alignment_decision,
    )
    from database.cache import get_kv_cache
    from database.orders import update_active_order_price

    updated_count = 0
    details: list[str] = []

    for order in orders:
        order_id = int(order["id"])
        symbol = order["symbol"]
        current_price = (
            order["limit_price"]
            if order["limit_price"] > 0
            else (
                order["stop_price"]
                if order["stop_price"] > 0
                else order["trailing_value"]
            )
        )
        original_qty = max(1, int(round(float(order.get("quantity") or 1.0))))

        optimal_price = None
        optimal_qty = None

        # 1. 優先使用 View 記憶體中攜帶的即時對齊建議價格
        if order_id in suggestions:
            optimal_price, optimal_qty = suggestions[order_id]
            logger.info(
                f"一鍵套用：使用內存遙測建議 (order_id={order_id}, symbol={symbol}): "
                f"price={optimal_price}, qty={optimal_qty}"
            )
        else:
            # 2. 次之使用資料庫 kv_cache 中快取的對齊建議 (10 分鐘內有效)
            key = f"telemetry:alignment_decision:{user_id}:{symbol.upper()}:{order_id}"
            cached_data = get_kv_cache(key)
            if cached_data and isinstance(cached_data, dict):
                ts_str = cached_data.get("ts")
                is_fresh = False
                if ts_str:
                    try:
                        ts_dt = datetime.fromisoformat(ts_str)
                        if (datetime.utcnow() - ts_dt).total_seconds() < 600:
                            is_fresh = True
                    except Exception:
                        pass
                if is_fresh:
                    optimal_price = cached_data.get("suggested_price")
                    optimal_qty = cached_data.get("suggested_qty")
                    logger.info(
                        f"一鍵套用：使用快取遙測建議 (order_id={order_id}, symbol={symbol}): "
                        f"price={optimal_price}, qty={optimal_qty}"
                    )

        # 3. 若以上皆無，則後備使用原有的 Mock 參數重算以維持基本行為
        if optimal_price is None or optimal_qty is None:
            cache_price, live_price = await fetch_cache_and_live_price(symbol)
            if live_price <= 0.0:
                continue

            holding_row = holding_map.get(symbol.upper(), {})
            holding_shares = float(holding_row.get("quantity", 0.0) or 0.0)

            try:
                decision = await generate_alignment_decision(
                    user_id=user_id,
                    order_id=order_id,
                    symbol=symbol,
                    current_order_price=float(current_price),
                    spot_price=float(live_price),
                    original_qty=original_qty,
                    iv=0.55,
                    hist_iv=0.35,
                    iv_rank=0.50,
                    max_pain_price=100.0,
                    prev_max_pain=100.0,
                    skew_percentile=0.98,
                    put_call_ratio=1.0,
                    prev_close=float(
                        cache_price if cache_price > 0.0 else current_price
                    ),
                    cache_price=cache_price,
                    live_price=live_price,
                    order_side=str(order.get("side") or "BUY"),
                    holding_type=holding_type,
                    holding_shares=holding_shares,
                )
            except DataContaminationException:
                continue

            if decision is None:
                continue

            optimal_price = float(decision.suggested_price)
            optimal_qty = int(decision.suggested_qty)
            logger.info(
                f"一鍵套用：重新計算後備建議 (order_id={order_id}, symbol={symbol}): "
                f"price={optimal_price}, qty={optimal_qty}"
            )

        if abs(optimal_price - current_price) >= 0.01 or optimal_qty != original_qty:
            update_active_order_price(order["id"], optimal_price, int(optimal_qty))
            updated_count += 1
            qty_change_msg = ""
            if optimal_qty < original_qty:
                qty_change_msg = f" (數量自 {original_qty} 調降至 {int(optimal_qty)} 股 [⚠️ 尾端風險防禦])"
            details.append(
                f"• **委託單 ID `{order['id']}` ({symbol})**:\n"
                f"  - 原有價格: `${current_price:.2f}`\n"
                f"  - 調整後安全建議價: `${optimal_price:.2f}`{qty_change_msg}\n"
            )

    return updated_count, details


async def build_telemetry_alignment_items(
    user_id: int,
    orders: list[dict],
    holding_type: str,
    holding_map: dict[str, dict],
    macro_event_dates: set[str],
) -> tuple[list[dict], bool]:
    """Core loop for building telemetry alignment items for /telemetry_alert.

    Extracted from OrderUICog.telemetry_alert().

    Returns:
        (alignment_items, truncated)
    """
    from market_analysis.telemetry_pricing_engine import (
        DataContaminationException,
        generate_alignment_decision,
    )
    from market_analysis.sentiment_engine import SentimentEngine
    from services.calendar_service import calendar_service

    alignment_items: list[dict] = []
    truncated = False

    for o in orders:
        order_type = str(o.get("order_type") or "").upper()

        # Trailing stop 的 trailing_value 並非「固定掛單價格」，不適用價格對齊警報。
        if order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
            continue

        limit_p = float(o.get("limit_price") or 0)
        stop_p = float(o.get("stop_price") or 0)

        price_label = "掛單價格"
        if order_type in ("LIMIT", "STOP_LIMIT") and limit_p > 0:
            current_price = limit_p
            price_label = "掛單限價"
        elif order_type in ("STOP", "STOP_LIMIT") and stop_p > 0:
            current_price = stop_p
            price_label = "掛單停損價"
        else:
            current_price = limit_p if limit_p > 0 else stop_p

        if current_price <= 0:
            continue

        original_qty = max(1, int(round(float(o.get("quantity") or 1.0))))

        symbol = str(o["symbol"]).upper()
        cache_price, live_price = await fetch_cache_and_live_price(symbol)
        if live_price <= 0.0:
            live_price = float(current_price)

        (
            iv_metrics,
            skew_metrics,
            max_pain_metrics,
            uoa_list,
            earnings_event,
        ) = await asyncio.gather(
            SentimentEngine.fetch_and_calculate_iv_metrics(symbol),
            SentimentEngine.calculate_skew(symbol),
            SentimentEngine.calculate_max_pain(symbol),
            SentimentEngine.detect_uoa(symbol),
            calendar_service.get_symbol_earnings(symbol),
        )
        if earnings_event is not None and earnings_event.date:
            macro_event_dates.add(earnings_event.date)

        # Safeguard: If Max Pain is marked as stale, avoid using it in critical pricing calculations
        is_mp_stale = (
            max_pain_metrics.get("is_stale", False)
            or (max_pain_metrics.get("data_status") == "Stale")
            or max_pain_metrics.get("max_pain") is None
            or max_pain_metrics.get("circuit_breaker_triggered", False)
        )
        if is_mp_stale:
            logger.warning(
                f"[{symbol}] Max Pain data is stale or invalid, resetting max_pain_price to 0.0 to prevent pricing calculation errors."
            )
            max_pain_price = 0.0
        else:
            max_pain_price = float(max_pain_metrics.get("max_pain", 0.0) or 0.0)

        uoa_payload = [
            {
                "expiration_date": str(item.get("expiry", "")),
                "strike": float(item.get("strike", 0.0) or 0.0),
                "option_type": str(item.get("type", "")),
                "volume_to_oi_ratio": float(item.get("ratio", 0.0) or 0.0),
            }
            for item in uoa_list
        ]

        holding_row = holding_map.get(symbol, {})
        holding_shares = float(holding_row.get("quantity", 0.0) or 0.0)

        try:
            iv_val_for_decision = (
                float(iv_metrics.current_iv or 0.0)
                if iv_metrics and iv_metrics.current_iv is not None
                else 0.35
            )
            iv_rank_for_decision = (
                float(iv_metrics.iv_rank / 100.0)
                if iv_metrics and iv_metrics.iv_rank is not None
                else None
            )
            skew_per_for_decision = (
                float(skew_per / 100.0)
                if skew_metrics
                and (skew_per := skew_metrics.get("skew_percentile")) is not None
                else 0.50
            )

            decision = await generate_alignment_decision(
                user_id=user_id,
                order_id=int(o["id"]),
                symbol=symbol,
                current_order_price=float(current_price),
                spot_price=float(live_price),
                original_qty=original_qty,
                iv=iv_val_for_decision,
                hist_iv=max(iv_val_for_decision / 1.1, 0.0001),
                iv_rank=iv_rank_for_decision,
                max_pain_price=max_pain_price if max_pain_price > 0.0 else None,
                prev_max_pain=max_pain_price if max_pain_price > 0.0 else 0.0,
                skew_percentile=skew_per_for_decision,
                put_call_ratio=1.0,
                prev_close=float(cache_price if cache_price > 0.0 else current_price),
                cache_price=cache_price,
                live_price=live_price,
                order_side=str(o.get("side") or "BUY"),
                holding_type=holding_type,
                holding_shares=holding_shares,
                uoa_array=uoa_payload,
                macro_event_dates=set(macro_event_dates),
                emit_suppressed_decision=True,
            )
        except DataContaminationException:
            decision = None

        if decision is None:
            continue

        suggested_price = float(decision.suggested_price)
        suggested_qty = int(decision.suggested_qty)

        is_size_down = suggested_qty < original_qty

        avg_cost = float(holding_row.get("avg_cost", 0.0) or 0.0)
        gain_loss_pct = (
            (live_price - avg_cost) / avg_cost * 100.0 if avg_cost > 0.0 else 0.0
        )
        put_wall = max_pain_price if max_pain_price > 0.0 else None
        wall_dist_pct = (
            (live_price - put_wall) / live_price * 100.0
            if put_wall is not None and live_price > 0.0
            else None
        )

        skew_val = (
            float(skew_val)
            if skew_metrics and (skew_val := skew_metrics.get("skew")) is not None
            else None
        )
        skew_pct = (
            float(skew_per)
            if skew_metrics
            and (skew_per := skew_metrics.get("skew_percentile")) is not None
            else None
        )
        skew_status = str(skew_metrics.get("state", "N/A")) if skew_metrics else "N/A"
        iv_val = (
            float(iv_metrics.current_iv * 100.0)
            if iv_metrics and iv_metrics.current_iv is not None
            else None
        )
        iv_rank = (
            float(iv_metrics.iv_rank)
            if iv_metrics and iv_metrics.iv_rank is not None
            else None
        )
        iv_status = str(iv_metrics.iv_status) if iv_metrics else "UNAVAILABLE"

        proximity = (
            abs(live_price - current_price) / live_price * 100.0
            if live_price > 0.0
            else 999.0
        )
        radar_status = (
            "FORTRESS RE-LOCKED"
            if decision.action == "SUPPRESSED"
            else ("雷達鎖定中" if proximity <= 2.0 else "偏離擴大")
        )

        if holding_type == "PURE_STOCK_100X":
            holding_type_label = "1.00x 純現貨 (0 槓桿)"
        else:
            holding_type_label = "LEVERAGED"

        holding_status = "空倉待命" if holding_shares <= 0 else "持倉中"
        wall_status = (
            "上方緩衝"
            if wall_dist_pct is not None and wall_dist_pct >= 0
            else "跌破支撐"
            if wall_dist_pct is not None
            else "待確認"
        )
        system_status_flag = (
            decision.system_status_flag
            if decision.system_status_flag
            else (
                "FORTRESS RE-LOCKED"
                if decision.action == "SUPPRESSED"
                else "TELEMETRY ACTIVE"
            )
        )
        directive = (
            decision.system_instruction_directive
            if decision.system_instruction_directive
            else (decision.alert_text or "通過實時對齊檢查，僅在防線內調整掛單。")
        )

        # 限制每個 Embed 擁有的標的數量以防字元數超限 (預估每個標的卡片佔用約 500 字元)
        if len(alignment_items) * 500 > 3500:
            truncated = True
            break

        alignment_items.append(
            {
                "symbol": symbol,
                "order_id": o["id"],
                "order_type": order_type,
                "price_label": price_label,
                "current_price": current_price,
                "original_qty": original_qty,
                "suggested_price": suggested_price,
                "suggested_qty": suggested_qty,
                "is_size_down": is_size_down,
                "alert_text": getattr(decision, "alert_text", None),
                "holding_type_label": holding_type_label,
                "holding_shares": int(round(holding_shares)),
                "holding_status": holding_status,
                "avg_cost": avg_cost,
                "live_price": live_price,
                "gain_loss_pct": gain_loss_pct,
                "put_wall": put_wall,
                "wall_dist_pct": wall_dist_pct,
                "wall_status": wall_status,
                "skew_val": skew_val,
                "skew_pct": skew_pct,
                "skew_status": skew_status,
                "iv_val": iv_val,
                "iv_rank": iv_rank,
                "iv_status": iv_status,
                "proximity_pct": proximity,
                "radar_status": radar_status,
                "system_status_flag": system_status_flag,
                "system_instruction_directive": directive,
                "is_premarket": iv_metrics.is_premarket
                if (iv_metrics and hasattr(iv_metrics, "is_premarket"))
                else False,
                "iv_source": iv_metrics.iv_source
                if (iv_metrics and hasattr(iv_metrics, "iv_source"))
                else "UNAVAILABLE",
                "side": o.get("side", "BUY"),
            }
        )

    return alignment_items, truncated
