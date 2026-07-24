import psutil
from services.market_data_service import BoundedCache

_macro_overview_cache = BoundedCache(max_size=10)


async def get_macro_overview_data(user_id: int) -> dict:
    ram_usage = psutil.virtual_memory().percent
    is_degraded = ram_usage > 85.0
    cache_key = f"overview_{user_id}"

    if is_degraded and cache_key in _macro_overview_cache:
        data = _macro_overview_cache[cache_key].copy()
        data["is_degraded"] = True
        return data

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

        def _parse(res, key, fallback):
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

    cal_parts = []
    for ev in events[:4]:
        dt_str = ev.get("event_time", "")
        event_name = ev.get("event", "")
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

    gex_fallback_val = get_kv_cache("macro_gex_is_fallback")
    gex_is_fallback = gex_fallback_val is None or int(gex_fallback_val) == 1

    vts_raw = get_kv_cache("macro_vts_ratio")
    is_backwardation = (float(vts_raw) >= 1.0) if vts_raw else (vix > 25.0)

    # 零 Gamma 踩踏 Regime 判定
    # SPX 跌破 Gamma Flip Line 且 VIX > 20 且 is_backwardation (倒掛或極端恐慌)
    short_gamma_critical = (spx < gamma_flip_line) and (vix > 20.0) and is_backwardation

    # 衰退警告 RECESSION_WARNING
    recession_warning = (sahm_rule >= 0.5) or (us10y > 4.5 and vix > 20.0)

    payout_threshold = get_safety_payout_threshold()

    data = {
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
        "is_degraded": is_degraded,
        "gex_is_fallback": gex_is_fallback,
    }

    # Save to memory cache
    _macro_overview_cache[cache_key] = data
    return data


async def find_matching_polymarket_odds(symbol: str, poly_markets: list) -> str:
    import re
    from services.market_data_service import get_company_profile

    symbol = symbol.upper()
    alts = []

    try:
        profile = await get_company_profile(symbol)
        if profile and "name" in profile:
            company_name = profile.get("name", "")
            if company_name:
                full_clean = (
                    company_name.lower()
                    .replace(" inc.", "")
                    .replace(" corp.", "")
                    .replace(" ltd.", "")
                    .replace(" corporation", "")
                    .replace(" company", "")
                    .strip()
                )
                if full_clean and full_clean not in alts:
                    alts.append(full_clean)
                first_word = company_name.split()[0].lower()
                first_word = re.sub(r"[^a-z0-9]", "", first_word)
                if first_word and first_word not in alts:
                    alts.append(first_word)
    except Exception:
        pass

    ticker_map = {
        "MU": ["micron"],
        "NVDA": ["nvidia"],
        "AAPL": ["apple"],
        "TSLA": ["tesla"],
        "MSFT": ["microsoft"],
        "GOOG": ["google", "alphabet"],
        "GOOGL": ["google", "alphabet"],
        "AMZN": ["amazon"],
        "META": ["meta", "facebook"],
        "NFLX": ["netflix"],
    }

    for fallback in ticker_map.get(symbol, []):
        if fallback not in alts:
            alts.append(fallback)

    for m in poly_markets or []:
        if not isinstance(m, dict):
            continue
        question = m.get("question", "")
        question_lower = question.lower()

        matches_ticker = False
        if re.search(rf"\b{re.escape(symbol.lower())}\b", question_lower):
            matches_ticker = True
        else:
            for alt in alts:
                if alt in question_lower:
                    matches_ticker = True
                    break

        if not matches_ticker and symbol == "MU":
            if "micron" in question_lower and (
                "eps" in question_lower
                or "revenue" in question_lower
                or "earnings" in question_lower
            ):
                matches_ticker = True

        if matches_ticker:
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
                try:
                    price_float = float(price_val)
                    odds_pct = price_float * 100.0
                    return f"{outcome}: {odds_pct:.1f}%"
                except Exception:
                    pass
                return f"{outcome}: {price_val}"

    return "N/A"
