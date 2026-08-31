"""單一標的核心分析管線（analyze_symbol）。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from services import market_data_service
from market_analysis.risk_engine import calculate_beta
from market_analysis.greeks import calculate_greeks

from market_analysis.strategy.indicators import _determine_strategy_signal
from market_analysis.strategy.liquidity_risk import (
    apply_vix_ladder,
    _validate_risk_and_liquidity,
    _calculate_sizing,
)
from market_analysis.strategy.contract_selection import (
    _find_target_expiry,
    _calculate_vertical_skew,
)

logger = logging.getLogger(__name__)


async def _as_awaitable(value: Any) -> Any:
    """把一個已知值包成 coroutine，方便與其他真正的 I/O coroutine 一起 gather。"""
    return value


async def analyze_symbol(
    symbol: Any,
    stock_cost: Any = 0.0,
    df_spy: Any = None,
    spy_price: Any = None,
    vix_spot: Optional[float] = None,
) -> Any:
    """掃描技術指標、波動率、偏態、Greeks 等進行核心分析。

    Args:
        vix_spot: VIX 即時價格。用於 VIX 戰情階梯判定（Delta 上限、倉位縮放、訊號閘門）。
    """
    # 延遲匯入：測試以 patch("market_analysis.strategy.<name>") 掛在套件層屬性上，
    # 模組層級 import 會凍結綁定而失效，故在此改為函式內延遲匯入。
    from market_analysis.strategy import (
        _calculate_technical_indicators,
        _fetch_opt_chain_and_best_contract,
        evaluate_ema_trend,
        _calculate_mmm,
        _calculate_term_structure,
    )

    try:
        df_spy_needs_fetch = df_spy is None
        quote, is_etf, df, df_spy_fetched = await asyncio.gather(
            market_data_service.get_quote(symbol),
            market_data_service.is_etf(symbol),
            market_data_service.get_history_df(symbol, "1y"),
            market_data_service.get_history_df("SPY", "1y")
            if df_spy_needs_fetch
            else _as_awaitable(df_spy),
        )
        if df_spy_needs_fetch:
            df_spy = df_spy_fetched

        price = quote.get("c", 0.0) if quote else None
        if df.empty:
            return None
        if price is None or price <= 0:
            price = df["Close"].iloc[-1]

        if df_spy.empty:
            logger.warning(f"無法取得 SPY 基準資料，{symbol} 改用 beta=1.0 fallback")
            spy_price_val = (
                spy_price if spy_price is not None and spy_price > 0 else price
            )
            beta = 1.0
        else:
            spy_price_val = (
                spy_price if spy_price is not None else df_spy["Close"].iloc[-1]
            )
            if symbol.upper() == "BOXX":
                beta = 0.0
            else:
                beta = calculate_beta(df, df_spy) if symbol != "SPY" else 1.0

        dividend_yield, indicators = await asyncio.gather(
            _as_awaitable(0.015)
            if is_etf
            else market_data_service.get_dividend_yield(symbol),
            asyncio.to_thread(_calculate_technical_indicators, df),
        )
        if indicators is None:
            return None
        price = indicators["price"]

        strategy, opt_type, target_delta, min_dte, max_dte = _determine_strategy_signal(
            indicators, ivr=0.0
        )
        if not strategy:
            return None

        # ---------- VIX 戰情階梯閘門 (VIX Battle Ladder Gate) ----------
        vix_tier = apply_vix_ladder(vix_spot)
        vix_sizing_multiplier = vix_tier.get("sizing_multiplier", 1.0)
        vix_kelly_override = vix_tier.get("kelly_fraction_override")

        # VIX 戰情階梯資訊注入，供 Service 層進行 Macro 階段判定
        vix_allow_signal = vix_tier.get("allow_signal", True)
        if strategy in ["STO_PUT", "STO_CALL"]:
            # Delta 上限鉗制：sto_delta_cap 為負數，max() 取較小絕對值（更保守）
            sto_cap = vix_tier.get("sto_delta_cap", -0.20)
            if sto_cap != 0.0 and strategy == "STO_PUT" and target_delta < sto_cap:
                logger.info(
                    f"[{symbol}] VIX 階梯 Delta 鉗制: {target_delta:.2f} -> {sto_cap:.2f}"
                )
                target_delta = sto_cap
            elif (
                sto_cap != 0.0
                and strategy == "STO_CALL"
                and target_delta > abs(sto_cap)
            ):
                logger.info(
                    f"[{symbol}] VIX 階梯 Delta 鉗制: {target_delta:.2f} -> {abs(sto_cap):.2f}"
                )
                target_delta = abs(sto_cap)
        # ----------------------------------------------------------------

        expirations = await market_data_service.get_all_option_expiries(symbol)
        if not expirations:
            return None
        today = datetime.now().date()

        target_expiry_date, days_to_expiry = _find_target_expiry(
            expirations, today, min_dte, max_dte
        )
        if not target_expiry_date:
            return None

        # 以下四項彼此互相獨立（皆僅依賴上方已解析的 price/expirations/is_etf 等值），
        # 併發抓取取代原本的序列瀑布。注意：這代表即使最終因 best_contract 為 None
        # 或 EMA 空頭強勢而提前 return，ema_eval/mmm/term_structure 仍會先執行完才被捨棄
        # ——屬於刻意接受的 tradeoff（用少量早退路徑的多餘網路請求換取常見路徑的低延遲）。
        (
            (best_contract, opt_chain),
            ema_eval,
            (mmm_pct, safe_lower, safe_upper, days_to_earnings),
            (ts_ratio, ts_state),
        ) = await asyncio.gather(
            _fetch_opt_chain_and_best_contract(
                symbol,
                target_expiry_date,
                opt_type,
                target_delta,
                price,
                days_to_expiry,
                dividend_yield,
            ),
            evaluate_ema_trend(symbol, price)
            if days_to_expiry <= 90
            else _as_awaitable(
                {
                    "trend": "N/A",
                    "ema_8": 0.0,
                    "ema_21": 0.0,
                    "distance_from_21": 0.0,
                }
            ),
            _calculate_mmm(symbol, price, today, is_etf),
            _calculate_term_structure(symbol, expirations, price, today),
        )
        if best_contract is None:
            return None

        if days_to_expiry <= 90:
            if ema_eval.get("trend") == "BEARISH_STRONG" and strategy in [
                "BTO_CALL",
                "STO_PUT",
            ]:
                logger.info(f"[{symbol}] 剔除: 動態趨勢濾網偵測到空頭強勢 ({strategy})")
                return None
            trend_state = ema_eval.get("trend", "UNKNOWN")
            ema_8, ema_21, dist_21 = (
                ema_eval.get("ema_8", 0.0),
                ema_eval.get("ema_21", 0.0),
                ema_eval.get("distance_from_21", 0.0),
            )
        else:
            trend_state = "N/A"
            ema_8, ema_21, dist_21 = 0.0, 0.0, 0.0

        # ------------------ VIX306 Advanced Volatility Filters ------------------
        vix_vts_data = await market_data_service.get_vix_term_structure()
        vix_zscores = await market_data_service.get_vix_zscores()

        vts_ratio = vix_vts_data.get("vts_ratio", 0.0)
        is_vts_valid = (
            vix_vts_data.get("is_valid", False)
            and vix_vts_data.get("vts_state") != "UNKNOWN"
        )
        z30 = vix_zscores.get("zscore_30", 0.0)
        z60 = vix_zscores.get("zscore_60", 0.0)

        # Filter #12: VTS Filter
        if strategy == "STO_PUT" and is_vts_valid and vts_ratio >= 1.0:
            logger.info(
                f"[{symbol}] 剔除: VIX 目前處於逆價差 Backwardation (VTS >= 1.0)，市場風險極高"
            )
            return None

        # Filter #14: Regime Alignment
        vix_trending_up = z30 > 0.5 and z60 > 0.0
        is_bullish_strategy = strategy in ["BTO_CALL", "STO_PUT"]
        if vix_trending_up and is_bullish_strategy:
            logger.info(
                f"[{symbol}] 剔除: VIX 30/60 雙重指標看漲中(波動率放大)，拒絕作多"
            )
            return None

        # Filter: SPX/NDX Alignment
        if is_bullish_strategy:
            spy_sma20 = await market_data_service.get_sma("SPY", 20)
            if spy_sma20 and spy_price_val and spy_price_val < spy_sma20:
                logger.info(f"[{symbol}] 剔除: SPY 跌破 20MA，大盤弱勢拒絕作多")
                return None
        # -------------------------------------------------------------------------

        if opt_chain is not None:
            vertical_skew, skew_state, is_high_tail_risk = _calculate_vertical_skew(
                opt_chain, price, days_to_expiry, strategy, symbol, dividend_yield
            )
            if vertical_skew is None:
                return None
        else:
            vertical_skew, skew_state, is_high_tail_risk = 1.0, "N/A", False

        risk_metrics = _validate_risk_and_liquidity(
            strategy,
            best_contract,
            price,
            indicators.get("hv_current", 0.0),
            days_to_expiry,
            symbol,
        )
        if not risk_metrics:
            return None

        # Filter #13: Tail Risk Filter (1/4-Kelly Adjustment)
        kelly_fraction = 0.25 if is_high_tail_risk else 0.50

        aroc, alloc_pct, margin_per_contract = _calculate_sizing(
            strategy,
            best_contract,
            days_to_expiry,
            expected_move=risk_metrics.get("expected_move", 0.0),
            price=price,
            stock_cost=stock_cost,
            kelly_fraction=kelly_fraction,
            kelly_fraction_override=vix_kelly_override,
        )

        # VIX 倉位縮放：將階梯乘數套用至 alloc_pct
        if vix_sizing_multiplier != 1.0:
            alloc_pct *= vix_sizing_multiplier

        # AROC 驗證邏輯移交給 TradingService 驗證管線 (Validation Pipeline) 處理
        # 此處僅保留基礎數據計算

        raw_delta = best_contract.get("bs_delta", 0.0)
        safe_spy_price = spy_price_val if spy_price_val > 0 else 1.0
        weighted_delta = round(raw_delta * beta * (price / safe_spy_price) * 100, 2)

        greeks = calculate_greeks(
            opt_type,
            price,
            best_contract.get("strike", 0.0),
            max(days_to_expiry, 1) / 365.0,
            best_contract.get("impliedVolatility", 0.0),
            dividend_yield,
        )

        return {
            "symbol": symbol,
            "price": price,
            "beta": beta,
            "weighted_delta": weighted_delta,
            "stock_cost": stock_cost,
            "rsi": indicators.get("rsi", 0.0),
            "sma20": indicators.get("sma20", 0.0),
            "hv_rank": indicators.get("hv_rank", 0.0),
            "ts_ratio": ts_ratio,
            "ts_state": ts_state,
            "v_skew": vertical_skew,
            "v_skew_state": skew_state,
            "vix_vts_ratio": vts_ratio,
            "vix_regime": vix_vts_data.get("vts_state", "UNKNOWN"),
            "vix_z30": z30,
            "vix_z60": z60,
            "is_high_tail_risk": is_high_tail_risk,
            "earnings_days": days_to_earnings,
            "mmm_pct": mmm_pct,
            "safe_lower": safe_lower,
            "safe_upper": safe_upper,
            "expected_move": risk_metrics.get("expected_move", 0.0),
            "em_lower": risk_metrics.get("em_lower", 0.0),
            "em_upper": risk_metrics.get("em_upper", 0.0),
            "strategy": strategy,
            "target_date": target_expiry_date,
            "dte": days_to_expiry,
            "strike": best_contract.get("strike", 0.0),
            "bid": risk_metrics.get("bid", 0.0),
            "ask": risk_metrics.get("ask", 0.0),
            "spread": risk_metrics.get("spread", 0.0),
            "spread_ratio": risk_metrics.get("spread_ratio", 0.0),
            "delta": raw_delta,
            "iv": best_contract.get("impliedVolatility", 0.0),
            "aroc": aroc,
            "alloc_pct": alloc_pct,
            "margin_per_contract": margin_per_contract,
            "vrp": risk_metrics.get("vrp", 0.0),
            "theta": round(greeks.get("theta", 0.0), 4),
            "gamma": round(greeks.get("gamma", 0.0), 6),
            "mid_price": risk_metrics.get("mid_price", 0.0),
            "suggested_hedge_strike": risk_metrics.get("suggested_hedge_strike"),
            "liq_status": risk_metrics.get("liq_status", "N/A"),
            "liq_msg": risk_metrics.get("liq_msg", ""),
            "spy_price": safe_spy_price,
            "ema_8": ema_8,
            "ema_21": ema_21,
            "trend": trend_state,
            "distance_from_21": dist_21,
            # VIX 戰情階梯元資料
            "vix_spot": vix_spot,
            "vix_tier_name": vix_tier.get("name", "N/A"),
            "vix_tier_emoji": vix_tier.get("emoji", ""),
            "vix_tier_color": vix_tier.get("color_hex", 0x808080),
            "vix_sizing_multiplier": vix_sizing_multiplier,
            "vix_sto_delta_cap": vix_tier.get("sto_delta_cap", 0.0),
            "vix_allow_signal": vix_allow_signal,
        }
    except Exception as e:
        logger.error(f"分析 {symbol} 錯誤: {e}")
        return None
