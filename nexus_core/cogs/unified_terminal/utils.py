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
            get_quote("SPY"),
            return_exceptions=True,
        )

        def _parse(res: Any, key: Any, fallback: Any = None):  # type: ignore
            if isinstance(res, dict) and res.get("c", 0) > 0:
                val = res["c"]
                asyncio.create_task(save_kv_cache(key, val))
                return val
            cached = get_kv_cache(key)
            if cached is not None:
                return cached
            return fallback

        spx = _parse(results[0], "macro_spx")
        vix = _parse(results[1], "macro_vix")
        us10y = _parse(results[2], "macro_us10y")
        wti = _parse(results[3], "macro_wti")
        spy_spot = _parse(results[4], "macro_spy_spot")

        # 數值 cross-check 與衍生：
        # 若 SPX 抓取失敗但 SPY 現貨即時取得，使用 SPY * 10 衍生 SPX；反之亦然
        if spx is None and spy_spot is not None and float(spy_spot) > 0:
            spx = round(float(spy_spot) * 10.0, 2)
            asyncio.create_task(save_kv_cache("macro_spx", spx))
        elif spy_spot is None and spx is not None and float(spx) > 0:
            spy_spot = round(float(spx) / 10.0, 2)
            asyncio.create_task(save_kv_cache("macro_spy_spot", spy_spot))

    except Exception:
        spx = get_kv_cache("macro_spx")
        vix = get_kv_cache("macro_vix")
        us10y = get_kv_cache("macro_us10y")
        wti = get_kv_cache("macro_wti")
        spy_spot = get_kv_cache("macro_spy_spot")
        if spx is None and spy_spot is not None and float(spy_spot) > 0:
            spx = round(float(spy_spot) * 10.0, 2)
        elif spy_spot is None and spx is not None and float(spx) > 0:
            spy_spot = round(float(spx) / 10.0, 2)

    # Normalize US10Y if needed
    if us10y is not None and float(us10y) > 10.0:
        us10y = float(us10y) / 10.0

    rrp = get_kv_cache("macro_rrp")
    fed_balance = get_kv_cache("macro_fed_balance")
    from datetime import datetime, timedelta
    from database.calendar_cache import get_macro_events_between
    from services.calendar_service import calendar_service

    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
    events = await asyncio.to_thread(get_macro_events_between, start_date, end_date)

    if not events:
        try:
            await calendar_service.prefetch_monthly_macro_cache(months_ahead=2)
            events = await asyncio.to_thread(
                get_macro_events_between, start_date, end_date
            )
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
    spy_gamma_flip = get_kv_cache("macro_spy_gamma_flip")
    uer = get_kv_cache("macro_uer")
    sahm_rule = get_kv_cache("macro_sahm_rule")
    rrp_change_30d = get_kv_cache("macro_rrp_change_30d")

    if not rrp or not fed_balance or not fear_greed:
        try:
            from market_analysis.index_microstructure import fetch_core_macro_metrics

            core_data = await fetch_core_macro_metrics()
            rrp = rrp or core_data.get("rrp")
            fed_balance = fed_balance or core_data.get("fed_balance")
            fear_greed = fear_greed or core_data.get("fear_greed")
            uer = uer or core_data.get("uer")
            sahm_rule = sahm_rule or core_data.get("sahm_rule")
            rrp_change_30d = rrp_change_30d or core_data.get("rrp_change_30d")
        except Exception:
            pass

    if not gamma_flip_line or not spy_gamma_flip:
        try:
            from market_analysis.index_microstructure import (
                fetch_gex_metrics,
                fetch_symbol_gex_metrics,
                estimate_symbol_gamma_flip,
            )

            gex_data = await fetch_gex_metrics(allow_empty=True)
            raw_flip = (
                gex_data.get("gamma_flip") if isinstance(gex_data, dict) else None
            )

            # 若 macro GEX 無法取得或為空，嘗試透過 SPY 即時個股期權計算
            if not raw_flip or (
                gex_data.get("spy_spot") == 510.0 and raw_flip == 515.0
            ):
                try:
                    spy_gex = await fetch_symbol_gex_metrics("SPY", force_live=False)
                    spot_calc = float(spy_gex.get("spot", 0.0) or 0.0)
                    if spot_calc > 0:
                        calc_flip = estimate_symbol_gamma_flip(
                            spy_gex.get("gex_profile", {}), spot_calc
                        )
                        if calc_flip > 0:
                            raw_flip = calc_flip
                            if not spy_spot:
                                spy_spot = spot_calc
                except Exception:
                    pass

            if raw_flip and float(raw_flip) > 0 and float(raw_flip) != 515.0:
                raw_flip_val = float(raw_flip)
                if not spy_gamma_flip:
                    spy_gamma_flip = raw_flip_val
                if not gamma_flip_line:
                    gamma_flip_line = raw_flip_val * 10.0
        except Exception:
            pass

    gamma_flip_line = (
        float(gamma_flip_line)
        if gamma_flip_line is not None and float(gamma_flip_line) > 0
        else (
            float(spy_gamma_flip) * 10.0
            if spy_gamma_flip is not None and float(spy_gamma_flip) > 0
            else None
        )
    )
    spy_gamma_flip = (
        float(spy_gamma_flip)
        if spy_gamma_flip is not None and float(spy_gamma_flip) > 0
        else (
            float(gamma_flip_line) / 10.0
            if gamma_flip_line is not None and float(gamma_flip_line) > 0
            else None
        )
    )

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

    vix_val_safe = float(vix) if vix is not None else 18.0
    is_backwardation = (
        (vts_val >= 1.0)
        if (vts_val is not None and vts_val > 0.0)
        else (vix_val_safe > 25.0)
    )

    # 零 Gamma 踩踏 Regime 判定
    # 評估 SPY 現貨價相對於 SPY Gamma Flip（與 index_microstructure.get_market_regime 一致，消除 10x basis 失真）
    is_negative_gamma_spy = (
        (float(spy_spot) < float(spy_gamma_flip))
        if (
            spy_spot is not None
            and spy_gamma_flip is not None
            and float(spy_spot) > 0.0
            and float(spy_gamma_flip) > 0.0
        )
        else (
            float(spx) < float(gamma_flip_line)
            if (
                spx is not None
                and gamma_flip_line is not None
                and float(spx) > 0.0
                and float(gamma_flip_line) > 0.0
            )
            else False
        )
    )
    short_gamma_critical = (
        is_negative_gamma_spy
        and (vix is not None and float(vix) > 20.0)
        and is_backwardation
    )

    # 衰退警告 RECESSION_WARNING
    recession_warning = (sahm_rule is not None and float(sahm_rule) >= 0.5) or (
        us10y is not None
        and vix is not None
        and float(us10y) > 4.5
        and float(vix) > 20.0
    )

    payout_threshold = get_safety_payout_threshold()

    fedwatch_prob, fedwatch_is_fallback, fedwatch_details = (
        calendar_service.get_latest_fedwatch_info()
    )

    # 取得 CPI 實際值 / 預期值與偏差
    cpi_actual = get_kv_cache("macro_cpi_actual")
    cpi_expected = get_kv_cache("macro_cpi_expected")
    cpi_is_fallback_val = get_kv_cache("macro_cpi_is_fallback")
    cpi_is_fallback = cpi_is_fallback_val is None or int(cpi_is_fallback_val) == 1
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
        cpi_dev=float(cpi_dev) if cpi_dev is not None else 0.0,
        wti=float(wti) if (wti is not None and float(wti) > 0) else 75.0,
        vts_ratio=float(vts_val) if (vts_val is not None and vts_val > 0) else 0.88,
        is_negative_gamma=short_gamma_critical or is_negative_gamma_spy,
    )

    result_data: dict[str, Any] = {
        "spx": spx,
        "spy_spot": spy_spot,
        "spy_gamma_flip": spy_gamma_flip,
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
        "cpi_actual": cpi_actual,
        "cpi_expected": cpi_expected,
        "cpi_is_fallback": cpi_is_fallback,
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


# Alias for backward compatibility / explicit context building
build_macro_terminal_embed_context = get_macro_overview_data


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
