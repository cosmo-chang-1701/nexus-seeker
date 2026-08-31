"""自選股單一標的完整評估（風控路由、全域防禦閘門、Regime 檢查）。"""

import asyncio
import logging
from typing import Any, Optional

import pandas as pd

from models.schemas import WatchlistEvaluation, WatchlistTacticalPlan
from risk_engine.nro import WatchlistRiskController


logger = logging.getLogger(__name__)


async def evaluate_watchlist_symbol(
    symbol: str,
    *,
    earnings_event: Any | None = None,
    macro_event: Any | None = None,
    df_spy: pd.DataFrame | None = None,
) -> Optional[WatchlistEvaluation]:
    # 延遲匯入：測試以 patch("market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics"/
    # "...build_watchlist_event_context") 掛在套件層屬性上，模組層級 import 會凍結綁定而失效。
    from market_analysis.intraday_pipeline import (
        build_enhanced_watchlist_metrics,
        build_watchlist_event_context,
    )

    metrics, event_context = await asyncio.gather(
        build_enhanced_watchlist_metrics(symbol, df_spy=df_spy),
        build_watchlist_event_context(
            symbol, earnings_event=earnings_event, macro_event=macro_event
        ),
    )
    if metrics is None:
        return None

    # 2. 將避險資產（BOXX/BIL）白名單風控防線下沉至 Ingress 層
    is_hedging = symbol.upper() in ["BOXX", "BIL"]
    if is_hedging:
        metrics.gex_max_put_wall = None
        metrics.oi_pcr = None

    tactical = WatchlistRiskController.process_metrics(metrics)
    symbol_gex = None

    # 🛑 動態轉倉引擎全域防禦閘門 (Fundamental Thesis)
    try:
        from database.market_cache import get_fundamental_cache

        fc = get_fundamental_cache(symbol)
        if fc and fc.get("is_broken"):
            tactical.scenario = "wait"  # Override to wait to block all buys
            tactical.sddm_route = "LIQUIDATE (基本面破滅強制清算)"
            tactical.action_guideline = f"⛔ 【LLM 護城河破滅警告】根據最新基本面分析，護城河已遭結構性破壞。\n> {fc.get('reasoning', '')}\n\n⚠️ 已觸發全域防禦閘門，強制封鎖所有買入與網格建倉策略，建議立即清算並轉倉至 CORE 資產。"
            tactical.alert_level = "red"
    except Exception as e:
        logger.warning(f"全域防禦閘門查詢錯誤: {e}")

    # 零 Gamma 踩踏 Regime 檢查並自動調整網格間距
    try:
        from market_analysis.index_microstructure import (
            get_market_regime,
            fetch_symbol_gex_metrics,
            estimate_symbol_gamma_flip,
        )
        from database.cache import get_kv_cache, save_kv_cache

        regime = await get_market_regime()
        if regime == "SYSTEMIC_LIQUIDITY_CRISIS":
            from database.cache import get_kv_cache

            gex_fb = get_kv_cache("macro_gex_is_fallback")
            is_fb = gex_fb is None or int(gex_fb) == 1
            fb_tag = " [備援估算]" if is_fb else ""

            tactical.scenario = "wait"
            tactical.sddm_route = "SYSTEMIC RISK FREEZE"
            tactical.action_guideline = f"⛔ 【系統性流動性危機】TED Spread 飆升且大盤陷入 Negative Gamma 負螺旋{fb_tag}。已啟動最高層級防火牆：凍結所有網格左側買單，強制保留 BOXX 現金水位以防範系統性衰退。"
            tactical.alert_level = "red"
        elif regime == "SHORT_GAMMA_CRITICAL":
            from database.cache import get_kv_cache

            gex_fb = get_kv_cache("macro_gex_is_fallback")
            is_fb = gex_fb is None or int(gex_fb) == 1
            fb_tag = " [備援估算]" if is_fb else ""

            tactical.dynamic_grid_step = round(tactical.dynamic_grid_step * 1.5, 2)
            tactical.action_guideline += f" (⚠️ 偵測到大盤進入 SHORT_GAMMA_CRITICAL 極端踩踏恐慌軌道{fb_tag}，個股網格單觸發間距已自動放大 1.5 倍以防禦資金被過早抽乾。)"

        # 個股 Net GEX 與牆位解析
        if is_hedging:
            symbol_gex = {}
            net_gex = 0.0
            call_wall = 0.0
            put_wall = 0.0
        else:
            symbol_gex = await fetch_symbol_gex_metrics(symbol)
            net_gex = symbol_gex.get("net_gex", 0.0)
            call_wall = symbol_gex.get("call_wall", 0.0)
            put_wall = symbol_gex.get("put_wall", 0.0)

        spot = metrics.current_price

        if put_wall > 0:
            metrics.gex_max_put_wall = put_wall

        # 軋空 (Squeeze) 判定校正：嚴禁將「現價穿越負 Gamma 履約價」直接定義為軋空。
        # 真正的 Gamma 軋空須同時滿足：(1) 現價向上放量穿越 Gamma Flip 翻轉點，
        # 進入正 Gamma 區間；(2) OI PCR >= 1.0（具備實質空頭籌碼供做市商軋空）；
        # (3) IV 隨價格同步走揚（Call Buying Mania），非單純價格穿越某個履約價牆位。
        gamma_flip_est = 0.0
        if not is_hedging:
            gex_profile = (
                symbol_gex.get("gex_profile", {})
                if isinstance(symbol_gex, dict)
                else {}
            )
            gamma_flip_est = estimate_symbol_gamma_flip(gex_profile, spot)

        iv_rank_prev_key = f"iv_rank_prev_{symbol.upper()}"
        prev_iv_rank = get_kv_cache(iv_rank_prev_key)
        iv_rising_with_price = (
            prev_iv_rank is not None
            and metrics.iv_rank is not None
            and metrics.iv_rank > float(prev_iv_rank)
        )
        if metrics.iv_rank is not None:
            await save_kv_cache(iv_rank_prev_key, metrics.iv_rank)

        if call_wall > 0 and put_wall > 0:
            crossed_gamma_flip_up = (
                gamma_flip_est > 0 and spot > gamma_flip_est and net_gex > 0
            )
            pcr_confirms = metrics.oi_pcr is not None and metrics.oi_pcr >= 1.0
            if crossed_gamma_flip_up and pcr_confirms and iv_rising_with_price:
                tactical.action_guideline += (
                    f"\n🚨 【軋空預警】現價 ({spot:.2f}) 站上 Gamma Flip 估算門檻 "
                    f"(${gamma_flip_est:.2f}) 進入正 Gamma 區間，OI PCR "
                    f"({metrics.oi_pcr:.2f}) 顯示籌碼結構具備實質空頭供軋倉，"
                    f"且 IV 隨價格同步走揚 (Call Buying Mania)，"
                    f"隨時可能觸發造市商被迫回補引發暴漲軋空。"
                )
            elif spot < put_wall:
                tactical.action_guideline += f"\n⚠️ 【流動性枯竭預警】現價 ({spot:.2f}) 跌破 Put Wall ({put_wall:.2f})，期權造市商支撐消失，存在嚴重賣壓與流動性真空風險。"

        if put_wall > 0 and spot > 0:
            distance = (spot - put_wall) / spot
            if distance <= 0.02 and net_gex < 0:
                warning_text = "⚠️ 負 Gamma 踩踏/波動放大區 (做市商 Delta 剛性拋壓風險全面壓倒遠期痛點磁吸，執行路由解鎖已全面受限)"
                tactical.action_guideline = (
                    f"{warning_text}\n{tactical.action_guideline}"
                )
                tactical.alert_level = "red"
                tactical.sddm_route = "SHIELD 網格防禦"
                tactical.scenario = "wait"

    except Exception as e:
        logger.warning(f"評估市場 Regime 與 GEX 時發生錯誤: {e}")

    # Structural divergence check (Skew vs PCR extremes)
    if (
        metrics.skew_percentile is not None
        and metrics.pcr is not None
        and (
            (metrics.skew_percentile > 85.0 and 0.0 < metrics.pcr < 0.4)
            or (metrics.skew_percentile < 15.0 and metrics.pcr > 1.5)
        )
    ):
        tactical = WatchlistTacticalPlan(
            scenario="wait",
            sddm_route="WAIT (觀望 / 待機)",
            action_guideline=(
                "⚠️ 警告：結構性情緒背離｜Skew 分位極端但 PCR 指向相反極端，"
                "可能是機構大幅對沖、散戶追逐買權的結構性分裂。建議停止追價單腿，"
                "僅允許小倉位收租並搭配保護性 Put/Collar 或使用價差結構。"
            ),
            dynamic_grid_step=tactical.dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="red",
        )

    # Skew Divergence Gate (機構避險背離/尾部風險警戒)
    if metrics.skew_percentile is not None and metrics.skew_percentile > 90.0:
        tactical = WatchlistTacticalPlan(
            scenario="wait",
            sddm_route="WAIT (機構避險背離/尾部風險警戒)",
            action_guideline=(
                "⚠️ 機構避險背離/尾部風險警戒｜Skew 分位處於極端高位 (>90%)，顯示真金白銀大量避險。"
                "已自動阻斷任何樂觀評級，建議立即提高現金比重或退守大盤流動性資產。"
            ),
            dynamic_grid_step=tactical.dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="red",
        )

    # Momentum Vector Gate (SQZ MOM + Negative Gamma)
    if (
        symbol_gex
        and symbol_gex.get("net_gex", 0.0) < 0
        and metrics.squeeze_momentum is not None
        and metrics.squeeze_momentum < 0
    ):
        tactical = WatchlistTacticalPlan(
            scenario="wait",
            sddm_route="WAIT (空頭動能發散)",
            action_guideline=(
                "⚠️ 負 Gamma 疊加空頭動能發散 (SQZ MOM < 0)，禁止輸出「區間震盪防守」或買入訊號。"
                "價格極易產生踩踏效應，建議保持觀望。"
            ),
            dynamic_grid_step=tactical.dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="red",
        )

    # 價格暴跌但波動率低壓背離偵測
    try:
        from services import market_data_service

        quote = await market_data_service.get_quote(symbol)
        dp_raw = quote.get("dp") if quote else None
        dp_val = float(dp_raw) if dp_raw is not None else 0.0
        if dp_val < -3.0 and metrics.iv_rank is not None and metrics.iv_rank < 15.0:
            tactical = WatchlistTacticalPlan(
                scenario="wait",
                sddm_route="WAIT (IV 壓抑背離)",
                action_guideline=(
                    "⚠️ WARNING: IV Suppression Divergence｜現價暴跌但波動率低壓，"
                    f"IV Rank 處於極低位階 ({metrics.iv_rank:.1f}%)，與現貨大跌 ({dp_val:+.2f}%) 矛盾。"
                    "可能存在系統快取延遲或異常，建議暫緩單腿長權利金操作，"
                    "僅允許小倉位收租並搭配保護性結構。"
                ),
                dynamic_grid_step=tactical.dynamic_grid_step,
                hidden_delta_risk=0.0,
                hedge_instruction=None,
                hedge_allocation_shares=0,
                alert_level="red",
            )
    except Exception as e:
        logger.warning(
            f"[{symbol}] evaluate_watchlist_symbol 背離比對獲取現價失敗: {e}"
        )

    return WatchlistEvaluation(
        metrics=metrics,
        tactical=tactical,
        event_context=event_context,
        symbol_gex=symbol_gex,
    )
