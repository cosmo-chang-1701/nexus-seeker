"""自選股量化指標建構（Vol POC、GEX PutWall、Beta、RSI/MA 等）。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from models.schemas import EnhancedWatchlistMetrics
from services.market_data_service import BoundedCache

from market_analysis.signal_calculator import (
    _derive_buy_levels,
    _derive_sell_levels,
    _buy_zone_status,
    _sell_zone_status,
    _extract_pe_ratio,
)


logger = logging.getLogger(__name__)

_WATCHLIST_METRICS_CACHE = BoundedCache(max_size=128)
_WATCHLIST_METRICS_TTL = 20 * 60


def _quote_price(quote: Dict[str, Any] | None, fallback: float = 0.0) -> float:
    if not quote:
        return fallback
    for key in ("c", "current_price", "price"):
        value = quote.get(key)
        if value and float(value) > 0.0:
            return float(value)
    return fallback


def get_cached_volume_poc(symbol: str) -> float | None:
    from database.cache import get_kv_cache

    val = get_kv_cache(f"volume_poc_{symbol.upper()}")
    return float(val) if val is not None else None


async def save_cached_volume_poc(symbol: str, poc: float) -> None:
    from database.cache import save_kv_cache

    await save_kv_cache(f"volume_poc_{symbol.upper()}", poc)


def get_cached_gex_putwall(symbol: str) -> float | None:
    from database.cache import get_kv_cache

    val = get_kv_cache(f"gex_putwall_{symbol.upper()}")
    return float(val) if val is not None else None


async def save_cached_gex_putwall(symbol: str, wall: float) -> None:
    from database.cache import save_kv_cache

    await save_kv_cache(f"gex_putwall_{symbol.upper()}", wall)


def _estimate_volume_poc(df: pd.DataFrame, bins: int = 24) -> float:
    recent = df.tail(60).copy()
    if recent.empty:
        return 0.0

    grouped = recent.groupby(
        pd.cut(
            recent["Close"], bins=min(bins, max(8, len(recent) // 2)), duplicates="drop"
        ),
        observed=False,
    )["Volume"].sum()
    if grouped.empty:
        return float(recent["Close"].iloc[-1])

    poc_bucket = grouped.idxmax()
    return float(round((float(poc_bucket.left) + float(poc_bucket.right)) / 2.0, 4))


def _relative_strength_vs_spy(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    from market_analysis.risk_engine import calculate_relative_strength_index

    return round(calculate_relative_strength_index(df_stock, df_spy, n=20), 4)


async def _estimate_options_wall_metrics(
    symbol: str,
    current_price: float,
    dividend_yield: float,
) -> tuple[float | None, float | None]:
    from market_analysis.greeks import calculate_greeks, calculate_vanna
    from services import market_data_service

    expiries = await market_data_service.get_all_option_expiries(symbol)
    if not expiries:
        return None, None

    expiry = expiries[0]
    chain = await market_data_service.get_option_chain(symbol, expiry)
    if chain is None or chain.puts.empty:
        return None, None

    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    t_years = max((expiry_dt - datetime.now()).days / 365.0, 7.0 / 365.0)

    puts = chain.puts.copy()
    puts = puts.dropna(subset=["strike", "openInterest", "impliedVolatility"])
    if puts.empty:
        return None, None

    max_wall_score = -1.0
    max_wall_strike = current_price
    for _, row in puts.iterrows():
        greeks = calculate_greeks(
            "put",
            current_price,
            float(row["strike"]),
            t_years,
            float(row["impliedVolatility"]),
            dividend_yield,
        )
        gamma_score = abs(float(greeks["gamma"])) * float(row["openInterest"]) * 100.0
        if gamma_score > max_wall_score:
            max_wall_score = gamma_score
            max_wall_strike = float(row["strike"])

    call_vanna = 0.0
    put_vanna = 0.0
    try:
        atm_call_idx = (chain.calls["strike"] - current_price).abs().idxmin()
        atm_call = chain.calls.loc[atm_call_idx]
        call_vanna = float(
            calculate_vanna(
                "c",
                current_price,
                float(atm_call["strike"]),
                t_years,
                float(atm_call["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    try:
        atm_put_idx = (puts["strike"] - current_price).abs().idxmin()
        atm_put = puts.loc[atm_put_idx]
        put_vanna = float(
            calculate_vanna(
                "p",
                current_price,
                float(atm_put["strike"]),
                t_years,
                float(atm_put["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    avg_vanna = (abs(call_vanna) + abs(put_vanna)) / (
        2.0 if call_vanna or put_vanna else 1.0
    )
    return round(max_wall_strike, 4), round(avg_vanna, 4)


async def build_enhanced_watchlist_metrics(
    symbol: str,
    *,
    df_spy: pd.DataFrame | None = None,
) -> Optional[EnhancedWatchlistMetrics]:
    from market_analysis.risk_engine import calculate_beta
    from market_analysis.sentiment_engine import SentimentEngine
    from services import market_data_service

    symbol = symbol.upper()
    now_ts = datetime.now().timestamp()
    if symbol in _WATCHLIST_METRICS_CACHE:
        cached_metrics, expiry = _WATCHLIST_METRICS_CACHE[symbol]
        if now_ts < expiry:
            return cached_metrics  # type: ignore

    quote_task = market_data_service.get_quote(symbol)
    stock_history_task = market_data_service.get_history_df(symbol, period="1y")

    if df_spy is None:
        spy_history_task = market_data_service.get_spy_history_df(period="1y")
    else:

        async def _get_provided_spy() -> Any:
            return df_spy

        spy_history_task = _get_provided_spy()

    financials_task = market_data_service.get_basic_financials(symbol)
    profile_task = market_data_service.get_company_profile(symbol)
    iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
    skew_task = SentimentEngine.calculate_skew(symbol)
    pcr_task = SentimentEngine.calculate_pcr(symbol)
    dividend_yield_task = market_data_service.get_dividend_yield(symbol)

    (
        quote,
        df_stock,
        df_spy,
        financials,
        profile,
        iv_metrics,
        skew_metrics,
        pcr_metrics,
        dividend_yield,
    ) = await asyncio.gather(
        quote_task,
        stock_history_task,
        spy_history_task,
        financials_task,
        profile_task,
        iv_task,
        skew_task,
        pcr_task,
        dividend_yield_task,
    )

    if df_stock.empty or len(df_stock) < 60:
        return None
    if df_spy.empty or len(df_spy) < 60:
        return None

    last_close = 0.0
    if quote:
        last_close = float(quote.get("pc", 0.0) or quote.get("c", 0.0) or 0.0)
    if last_close <= 0.0 and not df_stock.empty:
        last_close = float(df_stock["Close"].iloc[-1])

    current_price = _quote_price(quote, fallback=last_close)
    if current_price <= 0.0 or current_price is None:
        current_price = last_close
    if current_price <= 0.0 and not df_stock.empty:
        current_price = float(df_stock["Close"].iloc[-1])

    # 1. Vol POC (Volume Point of Control) via SQLite cache fallback
    volume_poc = 0.0
    if not df_stock.empty and len(df_stock) >= 60:
        try:
            volume_poc = max(_estimate_volume_poc(df_stock), 0.01)
            await save_cached_volume_poc(symbol, volume_poc)
        except Exception as e:
            logger.warning(f"Error calculating Vol POC for {symbol}: {e}")
    if volume_poc <= 0.0:
        cached_poc = get_cached_volume_poc(symbol)
        volume_poc = cached_poc if cached_poc else current_price

    # 2. GEX PutWall via SQLite cache fallback
    gex_max_put_wall = None
    vanna_sensitivity = None
    try:
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics

        gex_data = await fetch_symbol_gex_metrics(symbol)
        if gex_data:
            gex_max_put_wall = gex_data.get("put_wall", 0.0)
            vanna_sensitivity = 0.0
        if gex_max_put_wall is not None and gex_max_put_wall > 0.0:
            await save_cached_gex_putwall(symbol, gex_max_put_wall)
    except Exception as e:
        logger.warning(f"Error calculating GEX PutWall for {symbol}: {e}")
    if gex_max_put_wall is None or gex_max_put_wall <= 0.0:
        cached_wall = get_cached_gex_putwall(symbol)
        gex_max_put_wall = cached_wall if cached_wall else None

    # Restore essential indicators for pricing engine (AGENTS.md)
    from market_analysis.strategy import _calculate_technical_indicators

    indicators = await asyncio.to_thread(_calculate_technical_indicators, df_stock)
    rsi_14 = indicators.get("rsi", 50.0) if indicators else 50.0
    atr_14 = 0.01
    ma20 = indicators.get("sma20", current_price) if indicators else current_price
    ma50 = current_price
    ma200 = current_price
    beta = (
        0.0
        if symbol.upper() == "BOXX"
        else calculate_beta(df_stock, df_spy)
        if not df_stock.empty and not df_spy.empty
        else 1.0
    )
    relative_strength_spy = (
        _relative_strength_vs_spy(df_stock, df_spy)
        if not df_stock.empty and not df_spy.empty
        else 0.0
    )

    gex_max_put_wall_for_calc = (
        gex_max_put_wall
        if gex_max_put_wall is not None and gex_max_put_wall > 0.0
        else current_price
    )
    buy_phase1, buy_phase2, buy_phase3 = _derive_buy_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        volume_poc,
        max(gex_max_put_wall_for_calc, 0.01),
        0.0,
    )
    sell_phase1, sell_phase2, sell_phase3 = _derive_sell_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        0.0,
        volume_poc=volume_poc,
        gex_max_put_wall=max(gex_max_put_wall_for_calc, 0.01),
    )

    pe_raw = _extract_pe_ratio(financials)
    pe_outlier_warning = None
    if pe_raw is not None and pe_raw > 500.0:
        pe_outlier_warning = "【⚠️ 季度 EPS 驟降導致之數據雜訊預警】"
        pe_ratio = None
    else:
        pe_ratio = pe_raw

    from database.squeeze_cache import get_squeeze_cache, save_squeeze_cache
    from market_analysis.psq_engine import analyze_psq

    squeeze_cache = get_squeeze_cache(symbol)
    if squeeze_cache:
        squeeze_status = squeeze_cache.get("is_squeezing", False)
        squeeze_momentum = squeeze_cache.get("momentum", 0.0)
        squeeze_direction = squeeze_cache.get("direction", "⚪")
    else:
        psq_obj = analyze_psq(df_stock, vix_spot=18.0)
        if psq_obj:
            squeeze_status = psq_obj.is_squeezing
            squeeze_momentum = psq_obj.momentum_value
            squeeze_direction = (
                "🟢"
                if psq_obj.signal_direction == "Long"
                else ("🔴" if psq_obj.signal_direction == "Short" else "⚪")
            )
        else:
            squeeze_status = False
            squeeze_momentum = 0.0
            squeeze_direction = "⚪"
        save_squeeze_cache(symbol, squeeze_status, squeeze_momentum, squeeze_direction)

    metrics = EnhancedWatchlistMetrics(
        symbol=symbol,
        exchange=str(
            profile.get("exchange") or profile.get("exchangeCode") or "UNKNOWN"
        ),
        current_price=current_price,
        buy_zone_status=_buy_zone_status(current_price, buy_phase1, buy_phase2),
        buy_price_phase1=buy_phase1,
        buy_price_phase2=buy_phase2,
        buy_price_phase3=buy_phase3,
        sell_zone_status=_sell_zone_status(current_price, sell_phase1, sell_phase2),
        sell_price_phase1=sell_phase1,
        sell_price_phase2=sell_phase2,
        sell_price_phase3=sell_phase3,
        pe_ratio=pe_ratio,
        pe_outlier_warning=pe_outlier_warning,
        rsi_14=rsi_14,
        atr_14=atr_14,
        beta=beta,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        iv_rank=iv_metrics.iv_rank if iv_metrics else None,
        iv_percentile=iv_metrics.iv_percentile if iv_metrics else None,
        option_skew=float(skew_val)
        if skew_metrics and (skew_val := skew_metrics.get("skew")) is not None
        else None,
        skew_percentile=float(skew_per)
        if skew_metrics
        and (skew_per := skew_metrics.get("skew_percentile")) is not None
        else None,
        option_skew_state=str(skew_metrics.get("state") or "N/A")
        if skew_metrics
        else "N/A",
        pcr=float(pcr_val)
        if pcr_metrics and (pcr_val := pcr_metrics.get("pcr")) is not None
        else None,
        volume_poc=volume_poc,
        gex_max_put_wall=gex_max_put_wall,
        vanna_sensitivity=vanna_sensitivity,
        relative_strength_spy=relative_strength_spy,
        iv_source=iv_metrics.iv_source if iv_metrics else "UNAVAILABLE",
        is_premarket=getattr(iv_metrics, "is_premarket", False)
        if iv_metrics
        else False,
        volume_pcr=pcr_metrics.get("volume_pcr") if pcr_metrics else None,
        oi_pcr=pcr_metrics.get("oi_pcr") if pcr_metrics else None,
        has_earnings_event=getattr(iv_metrics, "has_earnings_event", False)
        if iv_metrics
        else False,
        has_macro_event=getattr(iv_metrics, "has_macro_event", False)
        if iv_metrics
        else False,
        iv_term_structure_status=getattr(iv_metrics, "iv_term_structure_status", None)
        if iv_metrics
        else None,
        term_structure_ratio=getattr(iv_metrics, "term_structure_ratio", None)
        if iv_metrics
        else None,
        squeeze_status=squeeze_status,
        squeeze_momentum=squeeze_momentum,
        squeeze_direction=squeeze_direction,
    )
    _WATCHLIST_METRICS_CACHE[symbol] = (metrics, now_ts + _WATCHLIST_METRICS_TTL)
    return metrics
