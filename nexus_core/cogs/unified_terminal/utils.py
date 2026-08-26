from typing import Any, cast
import inspect
import logging
import re
from services.llm_service import is_memory_safe
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)

_macro_overview_cache = BoundedCache(max_size=10)


async def get_macro_overview_data(user_id: int) -> dict[str, Any]:
    is_degraded = not is_memory_safe()
    cache_key = f"overview_{user_id}"

    if is_degraded and cache_key in _macro_overview_cache:
        cached_data: dict[str, Any] = cast(
            dict[str, Any], _macro_overview_cache[cache_key].copy()
        )
        cached_data["is_degraded"] = True
        cached_data["served_stale_cache"] = True
        return cached_data

    # Read from SQLite kv_cache
    from database import get_kv_cache, save_kv_cache
    from market_analysis.trading_orchestration import get_safety_payout_threshold
    from services.market_data_service import get_quote
    import asyncio

    try:
        results = await asyncio.gather(
            get_quote("^SPX"),
            get_quote("^VIX"),
            get_quote("^TNX"),
            get_quote("CL=F"),
            return_exceptions=True,
        )

        def _parse(res: Any, key: Any, fallback: Any):  # type: ignore
            if isinstance(res, dict) and res.get("c", 0) > 0:
                val = res["c"]
                asyncio.create_task(save_kv_cache(key, val))
                return val
            return get_kv_cache(key) or fallback

        spx = _parse(results[0], "macro_spx", 5150.0)
        vix = _parse(results[1], "macro_vix", 18.0)
        us10y = _parse(results[2], "macro_us10y", 4.25)
        wti = _parse(results[3], "macro_wti", 75.0)

    except Exception:
        spx = get_kv_cache("macro_spx") or 5150.0
        vix = get_kv_cache("macro_vix") or 18.0
        us10y = get_kv_cache("macro_us10y") or 4.25
        wti = get_kv_cache("macro_wti") or 75.0

    # Normalize US10Y if needed
    if us10y > 10.0:
        us10y = us10y / 10.0

    rrp = get_kv_cache("macro_rrp")
    fed_balance = get_kv_cache("macro_fed_balance")
    from datetime import datetime, timedelta
    from database.calendar_cache import get_macro_events_between
    from services.calendar_service import calendar_service

    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
    events = get_macro_events_between(start_date, end_date)

    if not events:
        try:
            await calendar_service.prefetch_monthly_macro_cache(months_ahead=2)
            events = get_macro_events_between(start_date, end_date)
        except Exception:
            pass

    from market_analysis.macro_calendar_translator import translate_macro_event

    cal_parts = []
    for ev in events[:4]:
        dt_str = ev.get("event_time", "")
        raw_event_name = ev.get("event", "")
        event_name = translate_macro_event(raw_event_name) or raw_event_name
        if len(dt_str) >= 10:
            mm_dd = dt_str[5:10].replace("-", "/")
            cal_parts.append(f"{mm_dd} {event_name}")
        else:
            cal_parts.append(f"{dt_str} {event_name}")

    if cal_parts:
        cpi_nfp_calendar = "\n └─ ".join(cal_parts)
    else:
        cpi_nfp_calendar = "近期無重大數據"

    fear_greed = get_kv_cache("macro_fear_greed")
    gamma_flip_line = get_kv_cache("macro_gamma_flip_line")
    uer = get_kv_cache("macro_uer")
    sahm_rule = get_kv_cache("macro_sahm_rule")
    rrp_change_30d = get_kv_cache("macro_rrp_change_30d")

    if not rrp or not fed_balance or not fear_greed:
        try:
            from market_analysis.index_microstructure import fetch_core_macro_metrics

            core_data = await fetch_core_macro_metrics()
            rrp = core_data.get("rrp") or 420.5
            fed_balance = core_data.get("fed_balance") or 7.25
            fear_greed = core_data.get("fear_greed") or 48.0
            uer = core_data.get("uer") or 4.0
            sahm_rule = core_data.get("sahm_rule") or 0.35
            rrp_change_30d = core_data.get("rrp_change_30d") or 5.0
        except Exception:
            pass

    rrp = rrp or 420.5
    fed_balance = fed_balance or 7.25
    fear_greed = fear_greed or 48.0
    uer = uer or 4.0
    sahm_rule = sahm_rule or 0.35
    rrp_change_30d = rrp_change_30d or 5.0

    if not gamma_flip_line:
        try:
            from market_analysis.index_microstructure import fetch_gex_metrics

            gex_data = await fetch_gex_metrics()
            gamma_flip_line = (gex_data.get("gamma_flip") or 515.0) * 10.0
        except Exception:
            pass

    gamma_flip_line = gamma_flip_line or 5180.0

    # 此處刻意不直接沿用上方 fetch_gex_metrics() 回傳值的 `_is_stale_cache`
    # （若有呼叫的話）：該呼叫只在 macro_gamma_flip_line 快取未命中時才會執行，
    # 多數渲染回合會直接跳過整個 if 區塊，故無法作為穩定訊號。改讀
    # macro_gex_is_fallback —— 此鍵由 index_microstructure.fetch_gex_metrics()
    # 內部每次呼叫（不論在系統何處被觸發）都無條件寫入，是唯一能反映「最近一次
    # 實際 macro GEX 抓取是否降級」的持久跨呼叫訊號。若改為在此無條件呼叫
    # fetch_gex_metrics() 以直接取得新鮮的 `_is_stale_cache`，會重新引入這段
    # 條件式原本刻意避免的每次渲染網路/爬蟲成本。
    gex_fallback_val = get_kv_cache("macro_gex_is_fallback")
    gex_is_fallback = gex_fallback_val is None or int(gex_fallback_val) == 1

    vts_raw = get_kv_cache("macro_vts_ratio")
    try:
        vts_val = float(vts_raw) if vts_raw is not None else None
    except (ValueError, TypeError):
        vts_val = None

    is_backwardation = (
        (vts_val >= 1.0) if (vts_val is not None and vts_val > 0.0) else (vix > 25.0)
    )

    # 零 Gamma 踩踏 Regime 判定
    # SPX 跌破 Gamma Flip Line 且 VIX > 20 且 is_backwardation (倒掛或極端恐慌)
    short_gamma_critical = (spx < gamma_flip_line) and (vix > 20.0) and is_backwardation

    # 衰退警告 RECESSION_WARNING
    recession_warning = (sahm_rule >= 0.5) or (us10y > 4.5 and vix > 20.0)

    payout_threshold = get_safety_payout_threshold()

    fedwatch_prob, fedwatch_is_fallback, fedwatch_details = (
        calendar_service.get_latest_fedwatch_info()
    )

    # 取得 CPI 偏差
    cpi_actual = get_kv_cache("macro_cpi_actual")
    cpi_expected = get_kv_cache("macro_cpi_expected")
    cpi_dev = (
        cpi_actual - cpi_expected
        if (cpi_actual is not None and cpi_expected is not None)
        else (get_kv_cache("macro_cpi_deviation") or 0.0)
    )

    # 多因子宏觀逃頂窗口狀態判定
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    (
        tightening_score,
        easing_score,
        escape_dir,
        escape_shift,
        escape_tier,
        escape_win_status,
    ) = evaluate_escape_window_regime(
        prob=fedwatch_prob,
        cpi_dev=float(cpi_dev),
        wti=float(wti),
        vts_ratio=float(vts_val) if (vts_val is not None and vts_val > 0) else 0.88,
        is_negative_gamma=short_gamma_critical or (spx < gamma_flip_line),
    )

    result_data: dict[str, Any] = {
        "spx": spx,
        "vix": vix,
        "us10y": us10y,
        "wti": wti,
        "rrp": rrp,
        "fed_balance": fed_balance,
        "cpi_nfp_calendar": cpi_nfp_calendar,
        "fear_greed": fear_greed,
        "gamma_flip_line": gamma_flip_line,
        "uer": uer,
        "sahm_rule": sahm_rule,
        "rrp_change_30d": rrp_change_30d,
        "short_gamma_critical": short_gamma_critical,
        "recession_warning": recession_warning,
        "payout_threshold": payout_threshold,
        "fedwatch_probability": fedwatch_prob,
        "fedwatch_is_fallback": fedwatch_is_fallback,
        "fedwatch_details": fedwatch_details,
        "escape_win_status": escape_win_status,
        "escape_window_direction": escape_dir,
        "escape_window_shift_days": escape_shift,
        "escape_window_tier": escape_tier,
        "is_degraded": is_degraded,
        "served_stale_cache": False,
        "gex_is_fallback": gex_is_fallback,
    }

    # Save to memory cache
    _macro_overview_cache[cache_key] = result_data
    return result_data


def _strip_redundant_symbol_prefix(question: str, symbol: str) -> str:
    """剝除 Polymarket 個股價格目標市場樣板中的冗餘前綴（如 "Will Palantir Technologies
    Inc. (PLTR) hit "），因為標的代碼已顯示於當前 Embed 情境中，重複資訊只會擠壓截斷預算，
    導致真正有價值的日期/區間資訊被砍掉。若問題不符合此樣板則原樣傳回。"""
    pattern = r"^Will\s+.+?\(" + re.escape(symbol) + r"\)\s+"
    match = re.match(pattern, question, re.IGNORECASE)
    if not match:
        return question
    remainder = question[match.end() :]
    if not remainder:
        return question
    return remainder[0].upper() + remainder[1:]


def _smart_truncate_question(text: str, max_len: int = 75) -> str:
    """在詞界（空白處）截斷過長的 Polymarket 問題文字，避免硬切在數字或單字中間。"""
    if len(text) <= max_len:
        return text
    cutoff = text.rfind(" ", 0, max_len)
    if cutoff <= 0:
        cutoff = max_len
    return text[:cutoff] + "…"


def _format_pool_volume(vol: float) -> str:
    """Format trading volume into human readable string."""
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.2f}M"
    elif vol >= 1_000:
        return f"${vol / 1_000:.0f}k"
    elif vol > 0:
        return f"${vol:.0f}"
    return "$0"


def _is_bearish_market_question(question: str) -> bool:
    """Determine if a prediction market question represents a bearish event."""
    q_lower = question.lower()
    bearish_keywords = [
        "drop",
        "fall",
        "down",
        "below",
        "under",
        "crash",
        "miss",
        "loss",
        "decline",
        "bear",
        "recession",
        "bankruptcy",
    ]
    return any(k in q_lower for k in bearish_keywords)


async def _get_matched_poly_markets(
    symbol: str, poly_markets: list, bot: Any = None
) -> list[dict[str, Any]]:
    from market_analysis.stock_alias_matrix import StockAliasMatrix

    symbol_upper = symbol.upper().strip()
    aliases = await StockAliasMatrix.get_aliases_for_symbol(symbol_upper)

    candidate_markets = list(poly_markets) if poly_markets else []

    # 1. 優先在 candidate_markets 尋找
    matched_markets: list[dict[str, Any]] = []
    seen_questions: set[str] = set()

    for m in candidate_markets:
        if not isinstance(m, dict):
            continue
        question = m.get("question", "")
        desc = m.get("description", "")
        full_text = f"{question} {desc}"
        if StockAliasMatrix.is_text_matching_symbol(full_text, symbol_upper, aliases):
            if question not in seen_questions:
                seen_questions.add(question)
                matched_markets.append(m)

    # 2. 若快照未命中且有 bot 實例，嘗試透過 polymarket_service 進行在線回退搜尋
    if not matched_markets and bot is not None:
        poly_service = getattr(bot, "polymarket_service", None)
        if poly_service and hasattr(poly_service, "get_symbol_markets"):
            try:
                res = poly_service.get_symbol_markets(
                    symbol_upper, limit=5, active_only=True
                )
                if inspect.isawaitable(res):
                    live_markets = await res
                else:
                    live_markets = res
                for m in live_markets or []:
                    if isinstance(m, dict):
                        q = m.get("question", "")
                        if q not in seen_questions:
                            seen_questions.add(q)
                            matched_markets.append(m)
            except Exception as e:
                logger.debug(
                    f"Failed to fetch live polymarket odds for {symbol_upper}: {e}"
                )

    return matched_markets


async def calculate_polymarket_weighted_odds(
    symbol: str, poly_markets: list, bot: Any = None
) -> str:
    """計算標的在 Polymarket 上所有相關合約之成交量加權綜合看多勝率 (Volume-Weighted Bullish Probability)"""
    matched_markets = await _get_matched_poly_markets(symbol, poly_markets, bot=bot)
    if not matched_markets:
        return "N/A"

    total_weighted_bullish = 0.0
    total_weight = 0.0
    actual_total_vol = 0.0
    valid_contracts = 0

    for m in matched_markets:
        question = m.get("question", "")
        tokens = m.get("tokens", [])
        if not tokens:
            tokens = m.get("odds_distribution", [])
        if not tokens:
            continue

        yes_token = None
        for t in tokens:
            if str(t.get("outcome", "")).strip().lower() == "yes":
                yes_token = t
                break
        target_token = yes_token if yes_token else tokens[0]
        price_val = target_token.get("price")
        if price_val is None:
            price_val = target_token.get("odds", 0)

        try:
            price_float = float(price_val)
        except Exception:
            continue

        # 方向性標準化：看跌事件 Yes 視為看空 (1 - P(Yes))
        if _is_bearish_market_question(question):
            bullish_prob = 1.0 - price_float
        else:
            bullish_prob = price_float

        vol = float(m.get("volumeNum") or m.get("volume") or 0.0)
        # 基底名義流動性權重（防止 0 成交量合約被完全忽略）
        w = max(vol, 1000.0)

        total_weighted_bullish += bullish_prob * w
        total_weight += w
        actual_total_vol += vol
        valid_contracts += 1

    if valid_contracts == 0 or total_weight <= 0.0:
        return "N/A"

    agg_prob = total_weighted_bullish / total_weight
    pct = agg_prob * 100.0
    vol_tag = _format_pool_volume(actual_total_vol)

    if pct >= 55.0:
        tag = f"🟢 {pct:.1f}% 巨鯨看多"
    elif pct <= 45.0:
        tag = f"🔴 {pct:.1f}% 巨鯨偏空"
    else:
        tag = f"⚖️ {pct:.1f}% 中性分歧"

    if actual_total_vol > 0:
        return f"{tag} ({valid_contracts}檔加權 · 池量 {vol_tag})"
    else:
        return f"{tag} ({valid_contracts}檔加權)"


async def find_matching_polymarket_odds(
    symbol: str, poly_markets: list, bot: Any = None
) -> str:
    symbol_upper = symbol.upper().strip()
    matched_markets = await _get_matched_poly_markets(symbol, poly_markets, bot=bot)

    results = []
    for m in matched_markets:
        question = m.get("question", "")
        tokens = m.get("tokens", [])
        if not tokens:
            tokens = m.get("odds_distribution", [])
        if tokens:
            yes_token = None
            for t in tokens:
                if str(t.get("outcome", "")).strip().lower() == "yes":
                    yes_token = t
                    break
            target_token = yes_token if yes_token else tokens[0]
            outcome = target_token.get("outcome", "Yes")
            price_val = target_token.get("price")
            if price_val is None:
                price_val = target_token.get("odds", 0)

            val_str = ""
            try:
                price_float = float(price_val)
                odds_pct = price_float * 100.0
                val_str = f"{outcome}: {odds_pct:.1f}%"
            except Exception:
                val_str = f"{outcome}: {price_val}"

            # Format to a compact string with markdown hyperlink and contract volume
            stripped_q = _strip_redundant_symbol_prefix(question, symbol_upper)
            short_q = _smart_truncate_question(stripped_q)
            event_slug = m.get("event_slug") or m.get("slug")
            market_url = m.get("url") or (
                f"https://polymarket.com/event/{event_slug}"
                if event_slug
                else "https://polymarket.com"
            )
            vol = float(m.get("volumeNum") or m.get("volume") or 0.0)
            vol_str = f" · 池量 {_format_pool_volume(vol)}" if vol > 0 else ""
            results.append((f"[{short_q}]({market_url}) ({val_str}{vol_str})", vol))

    if results:
        # Sort by volume descending and take top 3
        results.sort(key=lambda x: x[1], reverse=True)
        top_results = results[:3]
        return "\n • ".join(r[0] for r in top_results)
    return "N/A"
