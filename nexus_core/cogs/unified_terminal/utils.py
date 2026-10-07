from typing import Any, cast
import inspect
import logging
import re
import time
from market_analysis.sentiment.skew_taxonomy import (
    POLYMARKET_BEARISH_PCT,
    POLYMARKET_BULLISH_PCT,
    POLYMARKET_MIN_POOL_USD,
)
from services.llm_service import is_memory_safe
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)

# 值為 (寫入時的 time.monotonic(), result_data)
_macro_overview_cache = BoundedCache(max_size=10)

# 記憶體降級時可回退使用的快取最長年齡。過去沒有任何年齡上限，記憶體壓力若持續
# 數小時，/market 會把數小時前的 VIX / SPX / 零 Gamma 踩踏判定當成現況顯示（Embed
# 時間戳卻是當下），誤導風控判讀。超過此年齡即視同冷快取，照常完整重算。
_MACRO_OVERVIEW_STALE_MAX_AGE_SECONDS = 900.0

# 核心總經指標（edge core_metrics 一次寫入）的 kv 鍵
_CORE_MACRO_KV_KEYS: tuple[str, ...] = (
    "macro_rrp",
    "macro_fed_balance",
    "macro_fear_greed",
    "macro_uer",
    "macro_sahm_rule",
)
# 需追蹤快取年齡的 kv 鍵（一次查詢取得）
_MACRO_AGE_TRACKED_KEYS: tuple[str, ...] = _CORE_MACRO_KV_KEYS + (
    "macro_spy_gamma_flip",
    "macro_vts_ratio",
)


def _cache_clock() -> float:
    """快取年齡用的單調時鐘（獨立成函式以便測試替換，不必 patch 全域 time）。"""
    return time.monotonic()


async def get_macro_overview_data(user_id: int) -> dict[str, Any]:
    is_degraded = not is_memory_safe()
    cache_key = f"overview_{user_id}"

    if is_degraded and cache_key in _macro_overview_cache:
        cached_at, cached_payload = cast(
            tuple[float, dict[str, Any]], _macro_overview_cache[cache_key]
        )
        cache_age = _cache_clock() - cached_at
        if 0.0 <= cache_age <= _MACRO_OVERVIEW_STALE_MAX_AGE_SECONDS:
            cached_data: dict[str, Any] = cached_payload.copy()
            cached_data["is_degraded"] = True
            cached_data["served_stale_cache"] = True
            cached_data["stale_cache_age_seconds"] = cache_age
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

    # 各 kv 快取的年齡（秒）：快取值本身不帶時效，edge 或排程停擺時舊值會被
    # 當成現況顯示（曾出現恐懼與貪婪 65 vs 實際 28、GEX Flip 停在 24 天前）。
    from database.cache import get_kv_cache_many
    from market_analysis.index_microstructure import (
        MACRO_GEX_STALE_MAX_AGE_SECONDS,
        is_macro_gamma_flip_outlier,
    )

    kv_ages = {
        key: age
        for key, (_value, age) in get_kv_cache_many(_MACRO_AGE_TRACKED_KEYS).items()
    }

    def _max_known_age(keys: tuple[str, ...]) -> float | None:
        known = [kv_ages[k] for k in keys if kv_ages.get(k) is not None]
        return max(cast(list[float], known)) if known else None

    core_cache_age = _max_known_age(_CORE_MACRO_KV_KEYS)
    core_missing = rrp is None or fed_balance is None or fear_greed is None
    core_expired = (
        core_cache_age is not None and core_cache_age > MACRO_GEX_STALE_MAX_AGE_SECONDS
    )
    if core_missing or core_expired:
        try:
            from market_analysis.index_microstructure import fetch_core_macro_metrics

            core_data = await fetch_core_macro_metrics()
            if isinstance(core_data, dict) and not core_data.get("_is_fallback"):

                def _pick(field: str, old: Any) -> Any:
                    new = core_data.get(field)
                    return new if new is not None else old

                rrp = _pick("rrp", rrp)
                fed_balance = _pick("fed_balance", fed_balance)
                fear_greed = _pick("fear_greed", fear_greed)
                uer = _pick("uer", uer)
                sahm_rule = _pick("sahm_rule", sahm_rule)
                rrp_change_30d = _pick("rrp_change_30d", rrp_change_30d)
                if any(
                    core_data.get(f) is not None
                    for f in ("rrp", "fed_balance", "fear_greed", "uer", "sahm_rule")
                ):
                    core_cache_age = 0.0
        except Exception:
            pass
    core_is_expired = (
        core_cache_age is not None and core_cache_age > MACRO_GEX_STALE_MAX_AGE_SECONDS
    )

    # KV 讀取端合理性閘門：修正前寫入的離群 Flip（例如 SPY 774.8 時的 948.9）
    # 不可沿用，丟棄後走下方自癒路徑重抓（與 fetch_gex_metrics() 同一判定函式）。
    kv_flip_is_outlier = is_macro_gamma_flip_outlier(
        spy_gamma_flip, spy_spot
    ) or is_macro_gamma_flip_outlier(gamma_flip_line, spx)
    if kv_flip_is_outlier:
        logger.warning(
            f"KV 快取的 Gamma Flip (SPY {spy_gamma_flip} / SPX {gamma_flip_line}) "
            f"偏離現貨 (SPY {spy_spot} / SPX {spx}) 超出合理區間，丟棄並改走自癒路徑"
        )
        spy_gamma_flip = None
        gamma_flip_line = None

    # 大盤 GEX 快取超過時效：數值仍顯示，但不納入零 Gamma 與逃頂窗口判定
    # （與 get_market_regime() 的 MACRO_GEX_STALE_MAX_AGE_SECONDS 一致）。
    # 此處不重抓：GEX 走 edge Playwright 爬蟲，edge 失效時每次渲染都會白等。
    gex_cache_age = kv_ages.get("macro_spy_gamma_flip")
    gex_is_expired = (
        spy_gamma_flip is not None
        and gex_cache_age is not None
        and gex_cache_age > MACRO_GEX_STALE_MAX_AGE_SECONDS
    )

    # 自癒：KV 缺值或離群（離群時上方已清為 None）
    if kv_flip_is_outlier or not gamma_flip_line or not spy_gamma_flip:
        try:
            from market_analysis.index_microstructure import (
                estimate_macro_spy_gamma_flip,
                fetch_gex_metrics,
                fetch_symbol_gex_metrics,
            )

            gex_data = await fetch_gex_metrics(allow_empty=True)
            if not isinstance(gex_data, dict):
                gex_data = {}
            raw_flip: Any = gex_data.get("gamma_flip")
            try:
                raw_flip_val = float(raw_flip) if raw_flip is not None else 0.0
            except (TypeError, ValueError):
                raw_flip_val = 0.0
            ref_spot = spy_spot if spy_spot else gex_data.get("spy_spot")

            # 大盤 GEX 缺值、標記為備援，或相對現貨離群時，改以 SPY 個股期權鏈
            # 即時估算（fetch_gex_metrics(allow_empty=True) 不會回傳靜態常數，
            # 備援一律以 is_fallback 旗標辨識，不比對魔術數字）。
            if (
                raw_flip_val <= 0
                or bool(gex_data.get("is_fallback"))
                or is_macro_gamma_flip_outlier(raw_flip_val, ref_spot)
            ):
                raw_flip_val = 0.0
                try:
                    spy_gex = await fetch_symbol_gex_metrics("SPY", force_live=False)
                    spot_calc = float(spy_gex.get("spot", 0.0) or 0.0)
                    if spot_calc > 0:
                        calc_flip = estimate_macro_spy_gamma_flip(
                            spy_gex.get("gex_profile", {}), spot_calc
                        )
                        if calc_flip > 0:
                            raw_flip_val = calc_flip
                            if not spy_spot:
                                spy_spot = spot_calc
                except Exception:
                    pass

            if raw_flip_val > 0:
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
    # SPX/SPY 實際比值約 10.03（SPY 配息與費用率造成的基差），固定 ×10 會讓
    # SPX 尺度的翻轉線低估約 25–30 點。兩者報價皆在時改用實際比值換算；
    # 比值落在合理區間外（報價時點不一致等）則維持 ×10。
    if (
        spy_gamma_flip is not None
        and spx is not None
        and spy_spot is not None
        and float(spy_spot) > 0
    ):
        spx_spy_ratio = float(spx) / float(spy_spot)
        if 9.8 <= spx_spy_ratio <= 10.3:
            gamma_flip_line = round(float(spy_gamma_flip) * spx_spy_ratio, 2)

    # ── 大盤 Gamma Flip 合理性閘門（最終把關）──────────────────────────
    # 自癒取得的值或 SPX 尺度換算後的翻轉線仍可能離群；與上方 KV 讀取端共用
    # is_macro_gamma_flip_outlier()（非對稱區間，見 index_microstructure）。
    if is_macro_gamma_flip_outlier(spy_gamma_flip, spy_spot):
        logger.warning(
            f"SPY Gamma Flip ({spy_gamma_flip}) 偏離現貨 ({spy_spot}) 超出合理區間，"
            "判定為異常雜訊，降級為無效"
        )
        spy_gamma_flip = None
        gamma_flip_line = None

    if is_macro_gamma_flip_outlier(gamma_flip_line, spx):
        logger.warning(
            f"SPX Gamma Flip Line ({gamma_flip_line}) 偏離現貨 ({spx}) 超出合理區間，"
            "判定為異常雜訊，降級為無效"
        )
        gamma_flip_line = None
        spy_gamma_flip = None

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
    vts_cache_age = kv_ages.get("macro_vts_ratio")
    if vts_cache_age is not None and vts_cache_age > MACRO_GEX_STALE_MAX_AGE_SECONDS:
        # 過期的期限結構視為未知：backwardation 改依 VIX 判定、逃頂窗口不計分
        vts_val = None

    vix_val_safe = float(vix) if vix is not None else 18.0
    is_backwardation = (
        (vts_val >= 1.0)
        if (vts_val is not None and vts_val > 0.0)
        else (vix_val_safe > 25.0)
    )

    # 零 Gamma 踩踏 Regime 判定
    # 評估 SPY 現貨價相對於 SPY Gamma Flip（與 index_microstructure.get_market_regime 一致，消除 10x basis 失真）
    # None = 未知（Flip 缺值或快取過期），不可當成「正 Gamma」計入寬鬆
    is_negative_gamma_spy: bool | None
    if gex_is_expired:
        is_negative_gamma_spy = None
    elif (
        spy_spot is not None
        and spy_gamma_flip is not None
        and float(spy_spot) > 0.0
        and float(spy_gamma_flip) > 0.0
    ):
        is_negative_gamma_spy = float(spy_spot) < float(spy_gamma_flip)
    elif (
        spx is not None
        and gamma_flip_line is not None
        and float(spx) > 0.0
        and float(gamma_flip_line) > 0.0
    ):
        is_negative_gamma_spy = float(spx) < float(gamma_flip_line)
    else:
        is_negative_gamma_spy = None
    short_gamma_critical = (
        is_negative_gamma_spy is True
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
        else get_kv_cache("macro_cpi_deviation")
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
        cpi_dev=cpi_dev,
        wti=wti,
        vts_ratio=vts_val,
        is_negative_gamma=is_negative_gamma_spy,
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
        "gex_is_expired": gex_is_expired,
        "gex_cache_age_seconds": gex_cache_age,
        # 面板實際用於逃頂窗口與 backwardation 判定的 VTS（逾時效為 None）
        "vts_ratio": vts_val,
        "core_is_expired": core_is_expired,
        "core_cache_age_seconds": core_cache_age,
    }

    # Save to memory cache
    _macro_overview_cache[cache_key] = (_cache_clock(), result_data)
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
        if vol <= 0.0:
            # 0 成交量合約沒有價格發現，不納入加權
            continue
        w = vol

        total_weighted_bullish += bullish_prob * w
        total_weight += w
        actual_total_vol += vol
        valid_contracts += 1

    if valid_contracts == 0 or total_weight <= 0.0:
        return "N/A"

    vol_tag = _format_pool_volume(actual_total_vol)
    if actual_total_vol < POLYMARKET_MIN_POOL_USD:
        # 刻意不含「N% 巨鯨…」標籤，背離檢查的 regex 因此視為缺值
        return f"⚪ 池量不足（{vol_tag}，{valid_contracts}檔），不判讀"

    agg_prob = total_weighted_bullish / total_weight
    pct = agg_prob * 100.0

    if pct >= POLYMARKET_BULLISH_PCT:
        tag = f"🟢 {pct:.1f}% 巨鯨看多"
    elif pct <= POLYMARKET_BEARISH_PCT:
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
