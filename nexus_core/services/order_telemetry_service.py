"""Shared business logic for order telemetry pricing and alignment.

Extracts reusable order-related computations from the UI layer (cogs/order_ui.py)
to enable independent unit testing and eliminate ~120 lines of duplication
between DynamicOrderModal.on_submit() and OrderUICog.add_order().
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, TypeGuard

from market_analysis.sentiment.skew_taxonomy import (
    SKEW_NEUTRAL_PERCENTILE,
    ensure_percentile_pct,
)
from market_time import is_market_open

logger = logging.getLogger(__name__)

# 單次快照最多容納的委託單數 (Embed 已會分頁，此上限只為保護外部 API 配額與回應時間)。
MAX_ALIGNMENT_ITEMS = 20


def _is_number(value: Any) -> TypeGuard[float]:
    """數值判斷 (排除 bool)；對 MagicMock 等非數值物件回傳 False。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


async def fetch_cache_and_live_price(
    symbol: str,
) -> tuple[float, float, float | None]:
    """回傳 `(cache_price, live_price, prev_close)`。

    - cache_price：目前快取中的報價 (供「快取 vs 即時」漂移檢查)。
    - live_price：只作廢該標的的快取後重新呼叫 `get_quote`，沿用其優先序
      (Alpaca 串流分鐘價 → Finnhub → yfinance 後備)；不再以日線收盤覆蓋，
      也不再清空全站報價快取。
    - prev_close：報價的 `pc` (前一個正規盤收盤)，缺值時為 None。
    """
    from services import market_data_service

    cache_price = 0.0
    live_price = 0.0
    prev_close: float | None = None

    cached_quote = await market_data_service.get_quote(symbol)
    if cached_quote:
        cache_price = float(cached_quote.get("c") or 0.0)

    market_data_service.invalidate_quote_cache(symbol)
    fresh_quote = await market_data_service.get_quote(symbol)
    if fresh_quote:
        live_price = float(fresh_quote.get("c") or 0.0)
        pc_raw = fresh_quote.get("pc")
        if _is_number(pc_raw) and float(pc_raw) > 0.0:
            prev_close = float(pc_raw)

    if live_price <= 0.0:
        hist_df = await market_data_service.get_history_df(symbol, period="2d")
        if not hist_df.empty:
            live_price = float(hist_df["Close"].iloc[-1])

    return cache_price, live_price, prev_close


def _resolve_hist_iv(iv_metrics: Any, iv: float) -> float:
    """歷史 IV 基準：僅在 LIVE_IV 且未套用事件加載、且 20 日均值為正數時採用。

    HV_PROXY 的「IV」其實是已實現波動率 (禁止與 IV 混比)，STORED_IV／HV_PROXY 遇事件時
    current_iv 會被乘 1.4；兩者拿來和基準比較都會誤觸發 vol-spike／vol-crush，
    故其餘情況一律回傳 `iv` 本身 (中性)。
    """
    if iv_metrics is not None:
        source = getattr(iv_metrics, "iv_source", None)
        loaded = getattr(iv_metrics, "event_loading_applied", None)
        baseline = getattr(iv_metrics, "iv_hist_mean_20d", None)
        if (
            source == "LIVE_IV"
            and loaded is not True
            and _is_number(baseline)
            and float(baseline) > 0.0
        ):
            return float(baseline)
    return iv


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


def resolve_skew_percentile_pct(skew_metrics: Any) -> float:
    """把 `calculate_skew()` 的回傳值轉成遙測定價用的 Skew 百分位 (0~100)。

    優先順序：
    1. 真實分位 `skew_percentile`：該標的相對於自己歷史的位置 (日級規範母體，
       樣本不足時為高頻回退池)，與對齊警報路徑使用同一個值。
    2. 分位缺失 (樣本不足、抓取失敗) 時退回原始 Skew 絕對值的粗略換算：
       > 5 個百分點視為 98、< -2 視為 2。這是改版前的判定方式，只作為後備，
       避免資料缺失時比改版前更寬鬆。
    3. 兩者皆無時回傳中性 50，不觸發尾端防禦。
    """
    if not isinstance(skew_metrics, dict):
        return SKEW_NEUTRAL_PERCENTILE
    pct = skew_metrics.get("skew_percentile")
    if pct is not None:
        try:
            return ensure_percentile_pct(float(pct), "skew_percentile")
        except (TypeError, ValueError):
            pass
    raw = skew_metrics.get("skew")
    if raw is not None:
        try:
            skew = float(raw)
        except (TypeError, ValueError):
            return SKEW_NEUTRAL_PERCENTILE
        if skew > 5.0:
            return 98.0
        if skew < -2.0:
            return 2.0
    return SKEW_NEUTRAL_PERCENTILE


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
    hist_iv = iv
    try:
        iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
        if (
            iv_metrics
            and iv_metrics.current_iv is not None
            and iv_metrics.current_iv > 0
        ):
            iv = iv_metrics.current_iv
            hist_iv = _resolve_hist_iv(iv_metrics, iv)
    except Exception as e:
        logger.warning(f"Error fetching IV metrics for {symbol}: {e}")

    # 3. Resolve skew (0~100 百分位量綱，見 resolve_skew_percentile_pct)
    skew_val = SKEW_NEUTRAL_PERCENTILE
    try:
        skew_val = resolve_skew_percentile_pct(
            await SentimentEngine.calculate_skew(symbol)
        )
    except Exception as e:
        logger.warning(f"Error calculating skew for {symbol}: {e}")

    # 前收盤未知 (yfinance 只有一根日線時 pc 為 None) 以 0.0 傳入：定價引擎在
    # prev_close <= 0 時跳過跳空缺口錨定，而不是對 None 比較拋 TypeError。
    pc_raw = quote.get("pc")
    prev_close = float(pc_raw) if pc_raw is not None else 0.0

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
        skew_percentile_pct=skew_val,
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
) -> tuple[int, list[dict[str, Any]]]:
    """Core loop for the one-click telemetry apply button.

    只處理 `suggestions` 內的委託單 (即使用者在快照中看到的那些)；不在其中的委託單
    一律不動，也不會重新抓取任何市場資料。移動停損不適用價格對齊；STOP_LIMIT 的
    停損腿平移與限價腿相同的差額，保留原本的停損／限價價差。

    Returns:
        (updated_count, details)：details 為每筆實際更新的委託單一個 dict
        (order_id, symbol, order_type, old_price, new_price, old_qty, new_qty,
        old_stop, new_stop, is_size_down)，呈現交由 embed builder。
    """
    from database.orders import update_active_order_price

    updated_count = 0
    details: list[dict[str, Any]] = []

    for order in orders:
        order_id = int(order["id"])
        if order_id not in suggestions:
            continue

        order_type = str(order.get("order_type") or "").upper()
        # 移動停損的 trailing_value 不是掛單價格，不適用價格對齊
        if order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
            continue

        symbol = str(order["symbol"]).upper()
        limit_p = float(order.get("limit_price") or 0.0)
        stop_p = float(order.get("stop_price") or 0.0)
        current_price = limit_p if limit_p > 0.0 else stop_p
        if current_price <= 0.0:
            continue

        original_qty = max(1, int(round(float(order.get("quantity") or 1.0))))

        optimal_price, optimal_qty_raw = suggestions[order_id]
        optimal_price = float(optimal_price)
        optimal_qty = int(optimal_qty_raw)
        if optimal_price <= 0.0 or optimal_qty <= 0:
            continue
        logger.info(
            f"一鍵套用：使用快照遙測建議 (order_id={order_id}, symbol={symbol}): "
            f"price={optimal_price}, qty={optimal_qty}"
        )

        if abs(optimal_price - current_price) < 0.01 and optimal_qty == original_qty:
            continue

        new_stop: float | None = None
        old_stop: float | None = None
        if order_type == "STOP_LIMIT":
            old_stop = stop_p if stop_p > 0.0 else None
            if limit_p > 0.0 and stop_p > 0.0:
                new_stop = round(stop_p + (optimal_price - limit_p), 2)
            elif old_stop is not None:
                # 只有停損腿有價：資料庫仍會把新價同時寫入兩腿 (舊行為)
                new_stop = optimal_price

        if order_type == "STOP_LIMIT" and limit_p > 0.0 and stop_p > 0.0:
            await asyncio.to_thread(
                update_active_order_price,
                order["id"],
                optimal_price,
                optimal_qty,
                new_stop_price=new_stop,
            )
        else:
            await asyncio.to_thread(
                update_active_order_price, order["id"], optimal_price, optimal_qty
            )
        updated_count += 1
        details.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "order_type": order_type,
                "old_price": float(current_price),
                "new_price": optimal_price,
                "old_qty": original_qty,
                "new_qty": optimal_qty,
                "old_stop": old_stop,
                "new_stop": new_stop,
                "is_size_down": optimal_qty < original_qty,
            }
        )

    return updated_count, details


async def collect_macro_event_dates(days: int = 14) -> set[str]:
    """近期高影響宏觀事件的日期 (ISO)，供對齊決策的事件窗口防護使用。"""
    from services.calendar_service import calendar_service

    dates: set[str] = set()
    try:
        events = await calendar_service.get_high_impact_events(days=days)
    except Exception as e:
        logger.warning(f"Error fetching macro events for telemetry: {e}")
        return dates
    for event in events:
        try:
            event_date = datetime.fromisoformat(
                str(event.time).replace("Z", "+00:00")
            ).date()
            dates.add(event_date.isoformat())
        except ValueError:
            continue
    return dates


def _gather_value(result: Any, label: str, symbol: str) -> Any:
    """`gather(return_exceptions=True)` 的結果：例外視為該項資料缺失 (None)。"""
    if isinstance(result, BaseException):
        logger.warning(f"[{symbol}] 對齊輸入 {label} 取得失敗，視為缺值: {result}")
        return None
    return result


async def _fetch_alignment_market_inputs(
    symbol: str, macro_event_dates: set[str]
) -> dict[str, Any]:
    """抓齊一檔標的的即時市場資料，組成 `generate_alignment_decision` 的參數。

    任何單一輸入拋出例外都只視為該項缺值，不會讓整個指令失敗。財報日會被加入
    `macro_event_dates` (就地修改)。

    回傳 `{"decision_kwargs": {...}, "iv_metrics", "skew_metrics",
    "max_pain_price", "put_wall", "put_wall_is_stale"}`；原始物件供呼叫端渲染
    embed 使用。`put_wall` 是真實 GEX Put Wall (未知為 None)，與 Max Pain 是不同的量。
    """
    from market_analysis import index_microstructure
    from market_analysis.sentiment_engine import SentimentEngine
    from services.calendar_service import calendar_service

    results = await asyncio.gather(
        SentimentEngine.fetch_and_calculate_iv_metrics(symbol),
        SentimentEngine.calculate_skew(symbol),
        SentimentEngine.calculate_max_pain(symbol),
        SentimentEngine.detect_uoa(symbol),
        SentimentEngine.calculate_pcr(symbol),
        calendar_service.get_symbol_earnings(symbol),
        index_microstructure.fetch_symbol_gex_metrics(symbol),
        return_exceptions=True,
    )
    iv_metrics = _gather_value(results[0], "IV", symbol)
    skew_metrics = _gather_value(results[1], "Skew", symbol)
    max_pain_metrics = _gather_value(results[2], "Max Pain", symbol)
    uoa_list = _gather_value(results[3], "UOA", symbol)
    pcr_metrics = _gather_value(results[4], "PCR", symbol)
    earnings_event = _gather_value(results[5], "財報日", symbol)
    gex_metrics = _gather_value(results[6], "GEX", symbol)

    if earnings_event is not None and getattr(earnings_event, "date", None):
        macro_event_dates.add(earnings_event.date)

    # Safeguard: If Max Pain is marked as stale, avoid using it in critical pricing calculations
    max_pain_metrics = max_pain_metrics if isinstance(max_pain_metrics, dict) else {}
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

    # 真實 GEX Put Wall (0.0 = 未知)；`_is_stale_cache` 表示服務的是過期快取
    put_wall: float | None = None
    put_wall_is_stale = False
    if isinstance(gex_metrics, dict):
        try:
            raw_wall = float(gex_metrics.get("put_wall") or 0.0)
        except (TypeError, ValueError):
            raw_wall = 0.0
        if raw_wall > 0.0:
            put_wall = raw_wall
        put_wall_is_stale = gex_metrics.get("_is_stale_cache") is True

    uoa_payload = [
        {
            "expiration_date": str(item.get("expiry", "")),
            "strike": float(item.get("strike", 0.0) or 0.0),
            "option_type": str(item.get("type", "")),
            "volume_to_oi_ratio": float(item.get("ratio", 0.0) or 0.0),
        }
        for item in (uoa_list if isinstance(uoa_list, list) else [])
        if isinstance(item, dict)
    ]

    current_iv = getattr(iv_metrics, "current_iv", None) if iv_metrics else None
    iv_val = float(current_iv) if _is_number(current_iv) else 0.35
    raw_iv_rank = getattr(iv_metrics, "iv_rank", None) if iv_metrics else None
    iv_rank = float(raw_iv_rank / 100.0) if _is_number(raw_iv_rank) else None
    # 成交量 PCR；缺值時以中性 1.0 代替 (不觸發 Skew/PCR 恐慌折價)
    put_call_ratio = 1.0
    if isinstance(pcr_metrics, dict):
        raw_pcr = pcr_metrics.get("volume_pcr")
        try:
            if raw_pcr is not None and float(raw_pcr) > 0:
                put_call_ratio = float(raw_pcr)
        except (TypeError, ValueError):
            pass

    return {
        "decision_kwargs": {
            "iv": iv_val,
            "hist_iv": max(_resolve_hist_iv(iv_metrics, iv_val), 0.0001),
            "iv_rank": iv_rank,
            "max_pain_price": max_pain_price if max_pain_price > 0.0 else None,
            "prev_max_pain": max_pain_price if max_pain_price > 0.0 else 0.0,
            "skew_percentile_pct": resolve_skew_percentile_pct(skew_metrics),
            "put_call_ratio": put_call_ratio,
            "uoa_array": uoa_payload,
        },
        "iv_metrics": iv_metrics,
        "skew_metrics": skew_metrics,
        "max_pain_price": max_pain_price,
        "put_wall": put_wall,
        "put_wall_is_stale": put_wall_is_stale,
    }


def _resolve_iv_fields(iv_metrics: Any) -> dict[str, Any]:
    """把 IVMetrics 轉成快照欄位；以防禦式讀取避免 MagicMock 等物件的假屬性。"""
    if iv_metrics is None:
        return {
            "iv_val": None,
            "iv_rank": None,
            "iv_status": "UNAVAILABLE",
            "iv_source": "UNAVAILABLE",
            "iv_event_loading_applied": False,
            "iv_premarket_flag": False,
        }

    raw_source = getattr(iv_metrics, "iv_source", None)
    iv_source = raw_source if isinstance(raw_source, str) else "LIVE_IV"
    current_iv = getattr(iv_metrics, "current_iv", None)
    raw_rank = getattr(iv_metrics, "iv_rank", None)
    iv_val = float(current_iv) * 100.0 if _is_number(current_iv) else None
    iv_rank = float(raw_rank) if _is_number(raw_rank) else None

    if iv_source == "UNAVAILABLE" or iv_val is None:
        iv_status = "UNAVAILABLE"
    elif iv_rank is None:
        count = getattr(iv_metrics, "iv_history_count", 0)
        required = getattr(iv_metrics, "iv_history_required", 60)
        count_i = int(count) if _is_number(count) else 0
        required_i = int(required) if _is_number(required) else 60
        iv_status = f"樣本累積 {count_i}/{required_i}"
    else:
        raw_status = getattr(iv_metrics, "iv_status", None)
        iv_status = "UNAVAILABLE" if raw_status is None else str(raw_status)

    return {
        "iv_val": iv_val,
        "iv_rank": iv_rank,
        "iv_status": iv_status,
        "iv_source": iv_source,
        "iv_event_loading_applied": getattr(iv_metrics, "event_loading_applied", None)
        is True,
        "iv_premarket_flag": getattr(iv_metrics, "is_premarket", None) is True,
    }


def _resolve_holding_type_label(holding_type: str, holding_map: dict[str, dict]) -> str:
    if holding_type == "PURE_STOCK_100X":
        return "1.00x 純現貨 (0 槓桿)"
    if holding_type == "COMPLEX_OPTIONS":
        return "現貨＋選擇權"
    return "保證金槓桿 (推定)" if holding_map else "無持倉紀錄"


async def build_telemetry_alignment_items(
    user_id: int,
    orders: list[dict],
    holding_type: str,
    holding_map: dict[str, dict],
    macro_event_dates: set[str],
) -> tuple[list[dict], bool]:
    """Core loop for building telemetry alignment items for /telemetry_alert.

    每筆符合條件的委託單都會產生一個 item (不會因決策被抑制而消失)；沒有即時現價時
    產生降級 item (不執行決策、維持原價原量)。同一標的在一次呼叫內只抓一次報價與
    市場輸入；item 數達 `MAX_ALIGNMENT_ITEMS` 時，在抓取下一筆資料之前就截斷。

    Returns:
        (alignment_items, truncated)
    """
    from market_analysis.telemetry_pricing_engine import (
        DataContaminationException,
        generate_alignment_decision,
    )

    alignment_items: list[dict] = []
    truncated = False
    quote_memo: dict[str, tuple[float, float, float | None]] = {}
    inputs_memo: dict[str, dict[str, Any]] = {}
    market_open = bool(is_market_open())

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

        # 上限檢查必須在抓取下一筆市場資料「之前」，避免白抓用不到的資料與決策副作用
        if len(alignment_items) >= MAX_ALIGNMENT_ITEMS:
            truncated = True
            break

        original_qty = max(1, int(round(float(o.get("quantity") or 1.0))))
        side = str(o.get("side") or "BUY").upper()

        symbol = str(o["symbol"]).upper()
        if symbol not in quote_memo:
            try:
                quote_memo[symbol] = await fetch_cache_and_live_price(symbol)
            except Exception as e:
                logger.warning(f"[{symbol}] 對齊快照取得即時報價失敗: {e}")
                quote_memo[symbol] = (0.0, 0.0, None)
        cache_price, live_raw, prev_close = quote_memo[symbol]
        live_price: float | None = live_raw if live_raw > 0.0 else None

        if symbol not in inputs_memo:
            inputs_memo[symbol] = await _fetch_alignment_market_inputs(
                symbol, macro_event_dates
            )
        inputs = inputs_memo[symbol]
        iv_metrics = inputs["iv_metrics"]
        skew_metrics = inputs["skew_metrics"]
        max_pain_price = float(inputs["max_pain_price"] or 0.0)
        put_wall = inputs["put_wall"]

        holding_row = holding_map.get(symbol, {})
        holding_shares = float(holding_row.get("quantity", 0.0) or 0.0)

        # 決策欄位預設 = 維持原價原量
        action = "NO_LIVE_PRICE"
        suggested_price = float(current_price)
        suggested_qty = original_qty
        alert_text: str | None = None
        system_status_flag = "DATA DEGRADED"
        directive = "無法取得即時現價，暫停對齊建議（維持原價原量）。"

        if live_price is not None:
            decision = None
            try:
                decision = await generate_alignment_decision(
                    user_id=user_id,
                    order_id=int(o["id"]),
                    symbol=symbol,
                    current_order_price=float(current_price),
                    spot_price=float(live_price),
                    original_qty=original_qty,
                    prev_close=float(prev_close) if prev_close is not None else 0.0,
                    cache_price=cache_price,
                    live_price=live_price,
                    order_side=side,
                    holding_type=holding_type,
                    holding_shares=holding_shares,
                    macro_event_dates=set(macro_event_dates),
                    emit_suppressed_decision=True,
                    **inputs["decision_kwargs"],
                )
            except DataContaminationException:
                decision = None
            except Exception as e:
                logger.warning(f"[{symbol}] 對齊決策失敗，維持原價原量: {e}")
                decision = None

            if decision is None:
                # 不得靜默丟掉委託單：以抑制狀態保留在快照中
                action = "SUPPRESSED"
                system_status_flag = "FORTRESS RE-LOCKED"
                directive = "對齊決策無法產生，維持原價原量。"
            else:
                action = str(decision.action)
                suggested_price = float(decision.suggested_price)
                suggested_qty = int(decision.suggested_qty)
                alert_text = getattr(decision, "alert_text", None)
                system_status_flag = (
                    decision.system_status_flag
                    if decision.system_status_flag
                    else (
                        "FORTRESS RE-LOCKED"
                        if action == "SUPPRESSED"
                        else "TELEMETRY ACTIVE"
                    )
                )
                directive = (
                    decision.system_instruction_directive
                    if decision.system_instruction_directive
                    else (alert_text or "通過實時對齊檢查，僅在防線內調整掛單。")
                )

        suggestion_changed = (
            abs(suggested_price - current_price) >= 0.01
            or suggested_qty != original_qty
        )
        is_size_down = suggested_qty < original_qty

        # 持倉與損益 (負數量 = 空頭持倉)
        raw_avg_cost = float(holding_row.get("avg_cost", 0.0) or 0.0)
        avg_cost: float | None = raw_avg_cost if raw_avg_cost > 0.0 else None
        gain_loss_pct: float | None = None
        if avg_cost is not None and live_price is not None and holding_shares != 0:
            if holding_shares > 0:
                gain_loss_pct = (live_price - avg_cost) / avg_cost * 100.0
            else:
                gain_loss_pct = (avg_cost - live_price) / avg_cost * 100.0

        if holding_shares > 0:
            holding_status = "持倉中"
        elif holding_shares < 0:
            holding_status = "空頭持倉"
        else:
            holding_status = "空倉待命"

        wall_dist_pct = (
            (live_price - put_wall) / live_price * 100.0
            if put_wall is not None and live_price is not None
            else None
        )
        wall_status = (
            "待確認"
            if wall_dist_pct is None
            else ("上方緩衝" if wall_dist_pct >= 0 else "跌破支撐")
        )

        skew_val = None
        skew_pct = None
        skew_status = "N/A"
        if isinstance(skew_metrics, dict):
            raw_skew = skew_metrics.get("skew")
            raw_skew_pct = skew_metrics.get("skew_percentile")
            skew_val = float(raw_skew) if _is_number(raw_skew) else None
            skew_pct = float(raw_skew_pct) if _is_number(raw_skew_pct) else None
            skew_status = str(skew_metrics.get("state", "N/A"))

        iv_fields = _resolve_iv_fields(iv_metrics)

        proximity_pct = (
            abs(live_price - current_price) / live_price * 100.0
            if live_price is not None
            else None
        )
        if action == "SUPPRESSED":
            radar_status = "FORTRESS RE-LOCKED"
        elif proximity_pct is None:
            radar_status = "現價缺失"
        elif proximity_pct <= 2.0:
            radar_status = "雷達鎖定中"
        else:
            radar_status = "偏離擴大"

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
                "suggestion_changed": suggestion_changed,
                "is_size_down": is_size_down,
                "action": action,
                "alignment_available": live_price is not None,
                "alert_text": alert_text,
                "holding_type_label": _resolve_holding_type_label(
                    holding_type, holding_map
                ),
                "holding_shares": int(round(holding_shares)),
                "holding_status": holding_status,
                "avg_cost": avg_cost,
                "live_price": live_price,
                "prev_close": prev_close,
                "gain_loss_pct": gain_loss_pct,
                "put_wall": put_wall,
                "put_wall_is_stale": bool(inputs["put_wall_is_stale"]),
                "max_pain_price": max_pain_price if max_pain_price > 0.0 else None,
                "wall_dist_pct": wall_dist_pct,
                "wall_status": wall_status,
                "skew_val": skew_val,
                "skew_pct": skew_pct,
                "skew_status": skew_status,
                "iv_val": iv_fields["iv_val"],
                "iv_rank": iv_fields["iv_rank"],
                "iv_status": iv_fields["iv_status"],
                "iv_source": iv_fields["iv_source"],
                "iv_event_loading_applied": iv_fields["iv_event_loading_applied"],
                "proximity_pct": proximity_pct,
                "radar_status": radar_status,
                "system_status_flag": system_status_flag,
                "system_instruction_directive": directive,
                "is_premarket": (not market_open) or iv_fields["iv_premarket_flag"],
                "side": side,
            }
        )

    return alignment_items, truncated
