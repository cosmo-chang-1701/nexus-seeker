"""自選股單一標的完整評估（風控路由、全域防禦閘門、Regime 檢查）。"""

import asyncio
import logging
from typing import Any, Optional

import pandas as pd

from models.schemas import WatchlistEvaluation, WatchlistTacticalPlan
from risk_engine.nro import WatchlistRiskController


logger = logging.getLogger(__name__)


def _apply_tactical_gate(
    tactical: WatchlistTacticalPlan,
    *,
    locked: bool,
    sddm_route: str,
    action_guideline: str,
    capital_retreat_required: bool = False,
) -> WatchlistTacticalPlan:
    """套用一道戰術閘門。

    `locked=True` 代表已有更高優先級的鎖定指令在位（基本面護城河破滅強制清算、
    或系統性流動性危機凍結）。這種情況下只把警語追加到 `action_guideline`，
    **不覆寫既有路由**——過去這幾道閘門一律 `tactical = WatchlistTacticalPlan(...)`
    整包重建，會把「立即清算」降級成一般的「機構避險背離」觀望文案，等於在最需要
    清算指令的情境下把它靜默丟掉。
    """
    if locked:
        if action_guideline not in tactical.action_guideline:
            tactical.action_guideline = (
                f"{tactical.action_guideline}\n{action_guideline}".strip()
            )
        tactical.alert_level = "red"
        if capital_retreat_required:
            tactical.capital_retreat_required = True
        return tactical

    # locked=False: 若既有 guideline 已包含具體警報/預警（如 ⚠️, 🚨, ⛔, 【軋空預警】, 負 Gamma 等），
    # 採警語追加機制（Guideline Append），杜絕後續條件（如動能發散、IV壓抑）抹除前置高危警報（修復 ISSUE-01 / Top 3）。
    existing_guideline = (
        tactical.action_guideline.strip() if tactical.action_guideline else ""
    )
    if existing_guideline and (
        tactical.alert_level == "red"
        or tactical.scenario == "wait"
        or any(
            marker in existing_guideline
            for marker in [
                "⚠️",
                "🚨",
                "⛔",
                "【軋空預警】",
                "【流動性枯竭預警】",
                "負 Gamma",
            ]
        )
    ):
        if action_guideline not in existing_guideline:
            combined_guideline = f"{existing_guideline}\n{action_guideline}".strip()
        else:
            combined_guideline = existing_guideline
    else:
        combined_guideline = action_guideline

    return WatchlistTacticalPlan(
        scenario="wait",
        sddm_route=sddm_route,
        action_guideline=combined_guideline,
        dynamic_grid_step=tactical.dynamic_grid_step,
        hidden_delta_risk=0.0,
        hedge_instruction=None,
        hedge_allocation_shares=0,
        alert_level="red",
        # 旗標必須是 sticky 的：這些閘門會依序評估，未設此旗標的閘門
        # （結構性背離、IV 壓抑背離）若把 plan 整個重建成 False，會清掉前面
        # 由 Skew>90 或負 Gamma 設好的退守要求。
        capital_retreat_required=(
            capital_retreat_required or tactical.capital_retreat_required
        ),
    )


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
    # 更高優先級的鎖定指令是否已成立（基本面破滅強制清算 / 系統性流動性危機凍結）。
    # 後續的 Skew / 動能 / IV 背離閘門只能追加警語，不得整包覆寫。
    higher_priority_lock = False

    # 🛑 動態轉倉引擎全域防禦閘門 (Fundamental Thesis)
    try:
        from database.market_cache import get_fundamental_cache

        fc = get_fundamental_cache(symbol)
        fc_confidence = float(fc.get("confidence", 0.0) or 0.0) if fc else 0.0
        # [ISS-05] 必須同時滿足 is_broken 且置信度 >= 0.75 始觸發基本面清算，防範低置信度幻覺清算
        if fc and fc.get("is_broken") and fc_confidence >= 0.75:
            tactical.scenario = "wait"  # Override to wait to block all buys
            tactical.sddm_route = "LIQUIDATE (基本面破滅強制清算)"
            tactical.action_guideline = f"⛔ 【LLM 護城河破滅警告】根據最新基本面分析，護城河已遭結構性破壞（置信度: {fc_confidence:.0%}）。\n> {fc.get('reasoning', '')}\n\n⚠️ 已觸發全域防禦閘門，強制封鎖所有買入與網格建倉策略，建議立即清算並轉倉至 CORE 資產。"
            tactical.alert_level = "red"
            tactical.capital_retreat_required = True
            higher_priority_lock = True
        elif fc and fc.get("is_broken") and fc_confidence < 0.75:
            logger.info(
                f"[{symbol}] 基本面護城河破滅警告因置信度不足 ({fc_confidence:.2f} < 0.75) 遭防禦閘門過濾，防止低置信度幻覺清算"
            )
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
            tactical.capital_retreat_required = True
            higher_priority_lock = True
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
            # 軋空物理條件 (ISSUE-2.5)：造市商處於負 Gamma (net_gex < 0) 助漲追買泥淖，
            # 且 Call 買盤壓倒性主導 (oi_pcr <= 0.60)，搭配 IV 隨價格同步飆升 (Call Buying Mania)。
            in_squeeze_gamma_regime = net_gex < 0 or (
                gamma_flip_est > 0 and spot >= gamma_flip_est and net_gex <= 0
            )
            pcr_confirms = metrics.oi_pcr is not None and metrics.oi_pcr <= 0.60
            if in_squeeze_gamma_regime and pcr_confirms and iv_rising_with_price:
                flip_text = (
                    f"（站上 Gamma Flip ${gamma_flip_est:.2f}）"
                    if gamma_flip_est > 0
                    else ""
                )
                tactical.action_guideline += (
                    f"\n🚨 【軋空預警】現價 (${spot:.2f}){flip_text} 處於負 Gamma 助漲區間 (Net GEX: {net_gex:+.0f})，"
                    f"OI PCR ({metrics.oi_pcr:.2f}) 顯示 Call 買盤壓倒性主導，"
                    f"且 IV 隨價格同步走揚 (Call Buying Mania)，"
                    f"造市商空頭 Delta 避險追買隨時引發 Gamma Squeeze 暴漲軋空。"
                )
            elif spot < put_wall:
                # [ISS-12]: 引入多棒實體確認機制與防洗盤緩衝，避免盤中微觀插針 (下影線) 假刺穿引發恐慌預警。
                atr_15m_val = getattr(metrics, "atr_15m", None) or (
                    metrics.atr_14 / 5.099
                    if getattr(metrics, "atr_14", 0.0) > 0
                    else 0.0
                )
                anti_washout_line = (
                    put_wall - 1.5 * atr_15m_val if atr_15m_val > 0 else put_wall
                )

                # 若現價已深幅跌破防洗盤絕對防守線 (PutWall - 1.5*ATR15m)，或經由 15 分鐘多棒實體收盤貫穿確認
                from market_analysis.gamma_cliff_confirmation import (
                    is_gamma_cliff_confirmed,
                )

                is_confirmed = False
                if spot < anti_washout_line:
                    is_confirmed = True
                else:
                    try:
                        is_confirmed = await is_gamma_cliff_confirmed(symbol, put_wall)
                    except Exception as err:
                        logger.warning(
                            f"[{symbol}] Gamma cliff confirmation check failed: {err}"
                        )
                        is_confirmed = False

                if is_confirmed:
                    tactical.action_guideline += f"\n⚠️ 【流動性枯竭預警】現價 ({spot:.2f}) 實體貫穿確認跌破 Put Wall ({put_wall:.2f})，期權造市商支撐消失，存在嚴重賣壓與流動性真空風險。"
                else:
                    logger.info(
                        f"[{symbol}] 現價 ({spot:.2f}) 雖低於 Put Wall ({put_wall:.2f})，但在防洗盤緩衝區 (${anti_washout_line:.2f}) 內且未獲 15m 實體收盤確認，過濾下影線假破位"
                    )

        if put_wall > 0 and spot > 0:
            distance = (spot - put_wall) / spot
            if distance <= 0.02 and net_gex < 0:
                warning_text = "⚠️ 負 Gamma 踩踏/波動放大區 (做市商 Delta 剛性拋壓風險全面壓倒遠期痛點磁吸，執行路由解鎖已全面受限)"
                tactical.action_guideline = (
                    f"{warning_text}\n{tactical.action_guideline}"
                )
                tactical.alert_level = "red"
                tactical.scenario = "wait"
                # 資金藍圖閘門改讀顯式旗標。過去它比對 sddm_route 是否含
                # "負 Gamma"，但這裡設的字串是 "SHIELD 網格防禦"，從不匹配。
                tactical.capital_retreat_required = True
                if not higher_priority_lock:
                    tactical.sddm_route = "SHIELD 網格防禦 (負 Gamma 踩踏)"

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
        tactical = _apply_tactical_gate(
            tactical,
            locked=higher_priority_lock,
            sddm_route="WAIT (觀望 / 待機)",
            action_guideline=(
                "⚠️ 警告：結構性情緒背離｜Skew 分位極端但 PCR 指向相反極端，"
                "可能是機構大幅對沖、散戶追逐買權的結構性分裂。建議停止追價單腿，"
                "僅允許小倉位收租並搭配保護性 Put/Collar 或使用價差結構。"
            ),
        )

    # Skew Divergence Gate (機構避險背離/尾部風險警戒)
    if metrics.skew_percentile is not None and metrics.skew_percentile > 90.0:
        # 冷啟動保護 (ISSUE-2.2)：若樣本數不足 60 筆，抑制最高等級資金撤退，避免新標的誤觸帳戶清算
        skew_samples = getattr(metrics, "skew_sample_size", None)
        is_sample_mature = skew_samples is None or skew_samples >= 60
        tactical = _apply_tactical_gate(
            tactical,
            locked=higher_priority_lock,
            sddm_route="WAIT (機構避險背離/尾部風險警戒)",
            action_guideline=(
                "⚠️ 機構避險背離/尾部風險警戒｜Skew 分位處於極端高位 (>90%)，顯示真金白銀大量避險。"
                "已自動阻斷任何樂觀評級，建議立即提高現金比重或退守大盤流動性資產。"
            ),
            capital_retreat_required=is_sample_mature,
        )

    # Momentum Vector Gate (SQZ MOM + Negative Gamma)
    if (
        symbol_gex
        and symbol_gex.get("net_gex", 0.0) < 0
        and metrics.squeeze_momentum is not None
        and metrics.squeeze_momentum < 0
    ):
        tactical = _apply_tactical_gate(
            tactical,
            locked=higher_priority_lock,
            sddm_route="WAIT (空頭動能發散)",
            action_guideline=(
                "⚠️ 負 Gamma 疊加空頭動能發散 (SQZ MOM < 0)，禁止輸出「區間震盪防守」或買入訊號。"
                "價格極易產生踩踏效應，建議保持觀望。"
            ),
            capital_retreat_required=True,
        )

    # 價格暴跌但波動率低壓背離偵測
    try:
        from services import market_data_service

        quote = await market_data_service.get_quote(symbol)
        dp_raw = quote.get("dp") if quote else None
        dp_val = float(dp_raw) if dp_raw is not None else 0.0
        if dp_val < -3.0 and metrics.iv_rank is not None and metrics.iv_rank < 15.0:
            tactical = _apply_tactical_gate(
                tactical,
                locked=higher_priority_lock,
                sddm_route="WAIT (IV 壓抑背離)",
                action_guideline=(
                    "⚠️ WARNING: IV Suppression Divergence｜現價暴跌但波動率低壓，"
                    f"IV Rank 處於極低位階 ({metrics.iv_rank:.1f}%)，與現貨大跌 ({dp_val:+.2f}%) 矛盾。"
                    "可能存在系統快取延遲或異常，建議暫緩單腿長權利金操作，"
                    "僅允許小倉位收租並搭配保護性結構。"
                ),
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
