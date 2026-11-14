"""市場批次掃描與盤前財報警報 Mixin。"""

import asyncio
import logging
import math
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, TypedDict
from zoneinfo import ZoneInfo

import database
import market_math
from config import get_vix_tier
from market_analysis import portfolio, hedging
from market_analysis.gap_analysis import GapAnalyzer
from market_analysis.risk_engine import (
    classify_trade_intent,
    optimize_position_risk,
    position_delta_sign,
)
from market_analysis.strategy.indicators import STO_CALL_OVERBOUGHT_RSI
from services import market_data_service, news_service
from models.execution import MarketCondition, Signal

if TYPE_CHECKING:
    from services.execution_router import ExecutionRouter

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


_SHARES_PER_CONTRACT = 100


def _strategy_alignment_reject_reason(
    strategy: str,
    rsi: Any,
    shares: float,
    trading_strategy: str,
    regime_value: Optional[str],
) -> Optional[str]:
    """NRO 掃描策略對齊閘門 (純函式)：回傳拒絕原因，None 表示放行。

    - STO_CALL (Covered Call)：RSI 高於 STO_CALL_OVERBOUGHT_RSI 屬右側追強賣 Call
      (離線比較中最大的績效拖累)；持股不足 100 股則為裸賣 Call。
    - BTO_PUT (方向性做空)：僅 SHORT_SIDE，或 DYNAMIC 且 Regime 為 V 時放行；
      Regime 未知 (None) 一律拒絕 (fail-closed)。
    - 其餘策略 (STO_PUT / BTO_CALL) 不受影響。
    """
    if strategy == "STO_CALL":
        try:
            rsi_val = float(rsi)
        except (TypeError, ValueError):
            return "RSI 缺值，無法確認非右側賣 Call"
        if math.isnan(rsi_val):
            return "RSI 缺值，無法確認非右側賣 Call"
        if rsi_val > STO_CALL_OVERBOUGHT_RSI:
            return (
                f"RSI {rsi_val:.1f} > {STO_CALL_OVERBOUGHT_RSI}，"
                "右側追強賣 Call 不符合現行策略"
            )
        if shares < _SHARES_PER_CONTRACT:
            return f"持股 {shares:g} 股不足 {_SHARES_PER_CONTRACT} 股，不推裸賣 Call"
        return None
    if strategy and classify_trade_intent(strategy) == "DIRECTIONAL_SHORT":
        mode = trading_strategy or "RIGHT_SIDE"
        if mode == "SHORT_SIDE":
            return None
        if mode == "DYNAMIC":
            if regime_value == "REGIME_V_BREAKDOWN_CHASE":
                return None
            return (
                f"DYNAMIC 體制為 {regime_value or '未知'}，非 Regime V，不推方向性做空"
            )
        return f"{mode} 策略不推方向性做空 (BTO_PUT)"
    return None


async def _classify_symbol_regime_value(sym: str, spot: float) -> Optional[str]:
    """以 radar 同源的 GEX 資料分類單一標的 Regime；任何失敗回傳 None (fail-closed)。"""
    try:
        from market_analysis.dynamic_rollover.regime_classifier import (
            classify_dynamic_regime,
        )
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics

        gex = await fetch_symbol_gex_metrics(sym)
        if not isinstance(gex, dict) or not gex:
            return None
        gex_profile_data = {
            "put_wall": gex.get("put_wall"),
            "call_wall": gex.get("call_wall"),
            "net_gex": gex.get("net_gex"),
            "gex_profile": gex.get("gex_profile", {}),
            "gamma_flip": gex.get("gamma_flip"),
            "_is_stale_cache": bool(gex.get("_is_stale_cache", False)),
        }
        regime, _reason, _market_data = await classify_dynamic_regime(
            sym, spot, gex_profile_data, []
        )
        return str(regime.value)
    except Exception as e:
        logger.warning(f"[Strategy Align] {sym} Regime 分類失敗 (fail-closed): {e}")
        return None


class EarningsAlert(TypedDict):
    symbol: str
    is_portfolio: bool
    earnings_date: date
    days_left: int


class MarketScanMixin:
    if TYPE_CHECKING:
        execution_router: ExecutionRouter

        def _clean_market_condition_inputs(
            self, price: float, ma20: Any, atr: Any, rsi: Any
        ) -> Tuple[float, float, float]: ...

        def _validate_trade_pipeline(
            self, user_context: Any, data: Dict[str, Any]
        ) -> Tuple[bool, str]: ...

    async def get_pre_market_alerts_data(
        self, warning_days: int
    ) -> Dict[int, Dict[str, Any]]:
        """
        取得盤前財報警報數據。
        """
        from services.calendar_service import calendar_service

        today = datetime.now(ny_tz).date()
        all_portfolios = database.get_all_portfolio()
        all_watchlists = database.get_all_watchlist()

        user_symbols: Dict[int, Dict[str, Set[str]]] = {}
        unique_symbols = set()

        for row in all_portfolios:
            uid, sym = row[0], row[2]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["port"].add(
                sym
            )
            unique_symbols.add(sym)

        for row in all_watchlists:
            uid, sym = row[0], row[1]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["watch"].add(
                sym
            )
            unique_symbols.add(sym)

        earnings_infos = await calendar_service.get_symbol_earnings_batch(
            list(unique_symbols)
        )
        earnings_cache: Dict[str, date] = {}
        for sym, earnings_info in earnings_infos.items():
            if earnings_info is None:
                continue
            e_date = datetime.strptime(earnings_info.date, "%Y-%m-%d").date()
            earnings_cache[sym] = e_date

        results = {}
        for uid, symbols_data in user_symbols.items():
            alerts: List[EarningsAlert] = []
            combined_symbols = symbols_data["port"].union(symbols_data["watch"])

            for sym in combined_symbols:
                cached_earnings_date: date | None = earnings_cache.get(sym)
                if cached_earnings_date:
                    days_left = (cached_earnings_date - today).days
                    if 0 <= days_left <= warning_days:
                        item: EarningsAlert = {
                            "symbol": sym,
                            "is_portfolio": sym in symbols_data["port"],
                            "earnings_date": cached_earnings_date,
                            "days_left": days_left,
                        }
                        alerts.append(item)

            # 🚀 根據距離財報天數升冪排序 (0天優先)
            alerts.sort(key=lambda x: x["days_left"])

            results[uid] = {
                "alerts": alerts,
                "scanned_symbols": sorted(combined_symbols),
            }
        return results

    async def run_market_scan(
        self, is_auto: bool = True, triggered_by_id: Optional[int] = None
    ) -> Dict[int, List[Dict[str, Any]]]:
        """
        執行全站市場掃描 (整合 EMA, Macro Stress Matrix 與 VIX/Oil 監控)
        """
        all_watchlists = database.get_all_watchlist()
        if not all_watchlists:
            return {}

        # 🚀 獲取所有用戶的現貨持倉，用於動態帶入成本
        from database.holdings import get_all_holdings

        all_holdings = await asyncio.to_thread(get_all_holdings)
        holding_map = {(h["user_id"], h["symbol"]): h["avg_cost"] for h in all_holdings}
        # 現貨股數：NRO 策略對齊閘門 (Covered Call 需持股) 使用；缺值視為 0 (fail-closed)
        holding_qty_map: Dict[Tuple[int, str], float] = {}
        for h in all_holdings:
            try:
                qty_val = float(h.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                qty_val = 0.0
            holding_qty_map[(h["user_id"], h["symbol"])] = qty_val
        # DYNAMIC 使用者的 Regime 分類：每輪每標的至多一次，跨使用者共用
        regime_cache: Dict[str, Optional[str]] = {}

        from market_analysis.risk_engine import MacroContext, build_macro_context

        # 1. 🚀 獲取全域基準資料。SPY/VIX 未知時一律為 None（不補 670/18.0
        # 備援常數）：SPY 未知則本輪不推選擇權倉位建議 (無法換算曝險)；VIX
        # 未知則 optimize_position_risk 以 vix_unknown=True fail-closed。
        spy_price: Optional[float] = None
        vix_spot: Optional[float] = None
        macro_data: Optional[MacroContext] = None
        df_spy: Any = None
        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = (
                float(df_spy["Close"].iloc[-1])
                if df_spy is not None and not df_spy.empty
                else None
            )
            macro_data = build_macro_context(macro_raw)
            vix_spot = macro_data.vix if macro_data is not None else None
        except Exception as e:
            logger.warning(f"市場掃描基準資料 (SPY/VIX) 抓取失敗，視為未知: {e}")
            df_spy = None

        vix_tier = get_vix_tier(vix_spot)

        # 2. 提取不重複標的進行「併行批次掃描」
        # 標的聚合鍵：(代號, 成本)
        scan_targets = []
        for uid, sym, _ in all_watchlists:
            cost = holding_map.get((uid, sym), 0.0)
            scan_targets.append((sym, cost))

        unique_targets = list(set(scan_targets))

        # 🚀 併行執行所有標的分量 (分批執行以防止觸發 API Rate Limit)
        results_list = []
        batch_size = 10
        for i in range(0, len(unique_targets), batch_size):
            chunk = unique_targets[i : i + batch_size]
            tasks = [
                self._scan_single_target(t, df_spy, spy_price, vix_spot) for t in chunk
            ]
            batch_results = await asyncio.gather(*tasks)
            results_list.extend(batch_results)
            if i + batch_size < len(unique_targets):
                await asyncio.sleep(0.5)  # 給 API 一點緩衝，並釋放池空間

        scan_results = {target: res for target, res in results_list if res is not None}

        if not scan_results:
            return {}

        # 3. 準備使用者分發與「個人化 NRO 優化」
        user_alerts_results = {}
        user_watchlists: Dict[int, List[Tuple[str, float]]] = {}
        for uid, sym, _ in all_watchlists:
            stock_cost = holding_map.get((uid, sym), 0.0)
            user_watchlists.setdefault(uid, []).append((sym, stock_cost))

        # 🚀 標的層級批次預先獲取 Skew, PCR 與財報事件，徹底消除多使用者迴圈內的 O(U x S) 重複呼叫
        from market_analysis.sentiment_engine import SentimentEngine
        from services.calendar_service import calendar_service

        unique_scan_symbols: set[str] = {
            sym
            for sym, _ in scan_results.keys()
            if scan_results[(sym, _)].get("is_option_valid")
        }
        symbol_sentiment_cache: dict[str, dict[str, Any]] = {}
        for sym in unique_scan_symbols:
            cached_item = next(
                (
                    scan_results[(s, c)]
                    for (s, c) in scan_results
                    if s == sym and "skew_data" in scan_results[(s, c)]
                ),
                None,
            )
            skew_data = (
                cached_item.get("skew_data")
                if cached_item and cached_item.get("skew_data")
                else await SentimentEngine.calculate_skew(sym)
            )
            pcr_data = await SentimentEngine.calculate_pcr(sym)
            earnings_info = await calendar_service.get_symbol_earnings(sym)
            symbol_sentiment_cache[sym] = {
                "skew_val": skew_data.get("skew") or 0.0,
                "pcr_val": pcr_data.get("pcr"),  # 缺值為 None，不補 0.8
                "tte_hours": earnings_info.tte_hours if earnings_info else None,
            }

        for uid, watchlist_items in user_watchlists.items():
            valid_user_alerts = []

            # 獲取該使用者的動態風險參數與目前持倉統計
            # 🚀 [Resource Isolation] 確保 Greeks 數據最新，避免使用舊 Delta 判斷避險
            await portfolio.refresh_portfolio_greeks(uid)

            user_context = database.get_full_user_context(uid)
            if getattr(user_context, "load_failed", False) is True:
                logger.error(f"[Scan] 使用者 {uid} 帳戶資料讀取失敗，本輪略過該使用者")
                continue
            user_capital = user_context.capital
            current_total_delta = user_context.total_weighted_delta
            # 現金擔保上限所需的可動用現金：每位使用者每輪掃描只算一次。
            from market_analysis.risk_engine import (
                is_cash_secured_put_strategy,
                load_available_collateral,
            )

            _cash_reserve = float(getattr(user_context, "cash_reserve", 0.0) or 0.0)
            (
                available_collateral,
                collateral_warning,
            ) = await load_available_collateral(uid, _cash_reserve)

            for sym, stock_cost in watchlist_items:
                if (sym, stock_cost) in scan_results:
                    base_data = scan_results[(sym, stock_cost)].copy()
                    base_data["uid"] = uid
                    base_data["spy_price"] = spy_price
                    base_data["macro_vix"] = (
                        macro_data.vix if macro_data is not None else None
                    )
                    base_data["macro_vix_change"] = (
                        macro_data.vix_change if macro_data is not None else None
                    )
                    base_data["macro_oil"] = (
                        macro_data.oil_price if macro_data is not None else None
                    )
                    # VIX 戰情階梯狀態注入 (供 UI 層渲染)
                    base_data["vix_spot"] = vix_spot
                    base_data["vix_battle_status"] = {
                        "name": vix_tier.get("name", "N/A")
                        if vix_spot is not None
                        else "VIX 資料不足",
                        "emoji": vix_tier.get("emoji", ""),
                        "color_hex": vix_tier.get("color_hex", 0x99AAB5),
                        "vix_spot": vix_spot,
                        "sto_delta_cap": vix_tier.get("sto_delta_cap", 0.0),
                        "sizing_multiplier": vix_tier.get("sizing_multiplier", 1.0),
                    }
                    # 倉位乘數依交易意圖分流 (analyze_symbol 已算好的意圖專屬值)：
                    # BTO_PUT 顯示的應是做空倒 U 形乘數，不是賣方階梯乘數。
                    if base_data.get("vix_sizing_multiplier") is not None:
                        base_data["vix_battle_status"]["sizing_multiplier"] = base_data[
                            "vix_sizing_multiplier"
                        ]
                        base_data["vix_battle_status"]["trade_intent"] = base_data.get(
                            "trade_intent"
                        )

                    is_option_valid = base_data.get("is_option_valid", False)

                    if not is_option_valid:
                        continue  # 此標的沒有任何觸發訊號

                    # === 1. 選擇權策略分支 ===
                    # SPY 現價未知時無法把 Beta-Delta 換算成曝險 (過去補 670)，
                    # fail-closed：本輪不推選擇權倉位建議。
                    if (
                        user_context.option_alert_mode != 0
                        and is_option_valid
                        and spy_price is None
                    ):
                        logger.warning(
                            f"🚫 [{sym}] SPY 現價未知，略過選擇權倉位建議 (fail-closed)"
                        )
                    if (
                        user_context.option_alert_mode != 0
                        and is_option_valid
                        and spy_price is not None
                    ):
                        opt_data = base_data.copy()
                        opt_data["alert_type"] = "OPTION"

                        # 🚀 整合核心：讀取標的層級快取，確保 0 重複計算與數據一致性
                        cached_sent = symbol_sentiment_cache.get(sym)
                        if cached_sent is None:
                            skew_data = await SentimentEngine.calculate_skew(sym)
                            pcr_data = await SentimentEngine.calculate_pcr(sym)
                            earnings_info = await calendar_service.get_symbol_earnings(
                                sym
                            )
                            cached_sent = {
                                "skew_val": skew_data.get("skew") or 0.0,
                                "pcr_val": pcr_data.get("pcr"),  # 缺值為 None，不補 0.8
                                "tte_hours": earnings_info.tte_hours
                                if earnings_info
                                else None,
                            }
                            symbol_sentiment_cache[sym] = cached_sent

                        pcr_val = cached_sent["pcr_val"]
                        skew_val = cached_sent["skew_val"]
                        tte_hours = cached_sent["tte_hours"]

                        strategy = opt_data.get("strategy", "")

                        # 🚫 策略對齊閘門：必須在任何副作用 (倉位優化、re-hedge 寫入、
                        # 警報加入) 之前執行。
                        _raw_ts = getattr(user_context, "trading_strategy", "")
                        trading_strategy = _raw_ts if isinstance(_raw_ts, str) else ""
                        shares_held = holding_qty_map.get((uid, sym), 0.0)
                        regime_value: Optional[str] = None
                        if (
                            strategy
                            and classify_trade_intent(strategy) == "DIRECTIONAL_SHORT"
                            and trading_strategy == "DYNAMIC"
                        ):
                            if sym not in regime_cache:
                                regime_cache[sym] = await _classify_symbol_regime_value(
                                    sym, float(opt_data.get("price") or 0.0)
                                )
                            regime_value = regime_cache[sym]
                        align_reason = _strategy_alignment_reject_reason(
                            strategy,
                            opt_data.get("rsi"),
                            shares_held,
                            trading_strategy,
                            regime_value,
                        )
                        if align_reason is not None:
                            logger.info(
                                f"🚫 [Strategy Align] {sym} {strategy}: {align_reason}"
                            )
                            continue

                        _strike = float(opt_data.get("strike") or 0.0)
                        _ref_px = float(opt_data.get("price") or 0.0)
                        collateral_per_contract = (
                            _strike * 100.0 if _strike > 0 else _ref_px * 100.0
                        )
                        opt_res = optimize_position_risk(
                            current_delta=current_total_delta,
                            unit_weighted_delta=opt_data.get("weighted_delta", 0.0),
                            user_capital=user_capital,
                            spy_price=spy_price,
                            stock_iv=opt_data.get("iv", 0.15),
                            strategy=strategy,
                            macro_data=macro_data,
                            risk_limit=user_context.risk_limit,
                            vix_spot=vix_spot,
                            pcr=pcr_val,
                            skew=skew_val,
                            event_tte_hours=tte_hours,
                            vix_unknown=macro_data is None,
                            collateral_per_contract=(
                                collateral_per_contract
                                if collateral_per_contract > 0
                                else None
                            ),
                            available_collateral=available_collateral,
                        )
                        if collateral_warning and is_cash_secured_put_strategy(
                            strategy
                        ):
                            opt_res.warnings.append(collateral_warning)
                        safe_qty = opt_res.suggested_contracts
                        if strategy == "STO_CALL":
                            # Covered Call 張數不得超過持股可覆蓋的整張數。
                            # (程式庫無可重用的「已開空 Call 張數」helper，未扣除既有空單。)
                            safe_qty = min(
                                safe_qty, int(shares_held // _SHARES_PER_CONTRACT)
                            )
                            if safe_qty <= 0:
                                logger.info(
                                    f"🚫 [Strategy Align] {sym} {strategy}: "
                                    "持股可覆蓋張數為 0"
                                )
                                continue
                        hedge_spy = opt_res.suggested_hedge_spy

                        if opt_res.warnings:
                            opt_data["nro_warnings"] = opt_res.warnings

                        # 模擬成交後的衝擊
                        side_multiplier = position_delta_sign(strategy)
                        new_trade_impact = (
                            opt_data.get("weighted_delta", 0.0)
                            * side_multiplier
                            * safe_qty
                        )
                        projected_total_delta = current_total_delta + new_trade_impact
                        projected_exposure_pct = (
                            (projected_total_delta * spy_price / user_capital) * 100
                            if user_capital > 0
                            else 0.0
                        )

                        opt_data.update(
                            {
                                "safe_qty": safe_qty,
                                "hedge_spy": hedge_spy,
                                "projected_exposure_pct": round(
                                    projected_exposure_pct, 2
                                ),
                                "pcr": pcr_val,
                                "skew": skew_val,
                                "risk_limit": user_context.risk_limit,
                            }
                        )

                        # 🚀 執行集中化決策管線 (Stage 1-4)
                        is_approved, reason = self._validate_trade_pipeline(
                            user_context, opt_data
                        )
                        if not is_approved:
                            logger.info(
                                f"🚫 [Pipeline Reject] {sym} {strategy}: {reason}"
                            )
                            continue

                        # 🚀 對沖解除建議 (Hedge Unlocking)
                        ema_signals = opt_data.get("ema_signals", [])
                        for sig in ema_signals:
                            if (
                                sig.get("type") == "CROSSOVER"
                                and sig.get("direction") == "BULLISH"
                            ):
                                from services.alert_filter import validate_mtf_trend

                                mtf = await validate_mtf_trend(sym, sig)
                                unlock_advice = hedging.suggest_hedge_unlock(
                                    user_context, opt_data, mtf
                                )
                                if unlock_advice:
                                    opt_data["hedge_unlock"] = unlock_advice
                                break

                        # 🚀 自動回補避險 (Auto Re-Hedging)
                        now_ts = int(time.time())
                        if now_ts - user_context.last_rehedge_alert_time > 3600:
                            rehedge_advice = hedging.evaluate_rehedge_necessity(
                                user_context, opt_data
                            )
                            if rehedge_advice:
                                rehedge_advice = hedging.get_tuned_risk_advice(
                                    uid, rehedge_advice
                                )
                                opt_data["rehedge_info"] = rehedge_advice
                                await asyncio.to_thread(
                                    database.upsert_user_config,
                                    uid,
                                    last_rehedge_alert_time=now_ts,
                                )
                                user_context.last_rehedge_alert_time = now_ts

                        valid_user_alerts.append(opt_data)

            if valid_user_alerts:
                user_alerts_results[uid] = valid_user_alerts

        return user_alerts_results

    async def _scan_single_target(
        self, target: Any, df_spy: Any, spy_price: Any, vix_spot: Any
    ) -> tuple[Any, Any]:
        sym, stock_cost = target
        # ... (rest of scan logic)
        try:
            # analyze_symbol 已經是 async，若沒有 Option 訊號，res 會是 None
            res = await market_math.analyze_symbol(
                sym, stock_cost, df_spy, spy_price, vix_spot=vix_spot
            )
            is_option_valid = bool(res)
            if not res:
                res = {"symbol": sym, "stock_cost": stock_cost, "strategy": ""}

            res["is_option_valid"] = is_option_valid

            # 🚀 新增 Gap & Fill 跳空分析 (僅在開盤初期 2 小時內執行更精確，但這裡常態掃描)
            try:
                df_gap = await market_data_service.get_history_df(
                    sym, period="5d", interval="1d"
                )
                if not df_gap.empty and len(df_gap) >= 2:
                    gap_status = GapAnalyzer.analyze_gap(df_gap)
                    if gap_status:
                        res["gap_status"] = gap_status
            except Exception as gap_e:
                logger.warning(f"Gap 分析失敗 for {sym}: {gap_e}")

            # 🚀 新增 EMA 訊號偵測 (Crossover & Test)
            # 為確保 EMA 準確性，獲取至少 60 天歷史數據 (1-Hour 時框作為小週期觸發)
            df_hist_1h = await market_data_service.get_history_df(
                sym, period="60d", interval="1h"
            )
            if not df_hist_1h.empty:
                ema_8_sig = market_math.detect_ema_signals(df_hist_1h, window=8)
                ema_21_sig = market_math.detect_ema_signals(df_hist_1h, window=21)

                # 整合訊號至結果字典
                res["ema_signals"] = [sig for sig in [ema_8_sig, ema_21_sig] if sig]

                # 如果有 EMA 訊號，強制標註為「高價值追蹤」
                if res["ema_signals"]:
                    res["is_priority_alert"] = True

            # 日 K (價格備援與 ExecutionRouter 指標)
            df_hist_1d = await market_data_service.get_history_df(
                sym, period="1y", interval="1d"
            )
            if not df_hist_1d.empty:
                # ExecutionRouter 備援路徑 (下方) 需要價格
                res["price"] = float(df_hist_1d["Close"].iloc[-1])

            # 🚀 整合核心：Execution Router 執行決策 (SDDM)
            try:
                import pandas_ta as ta

                # 使用 df_hist_1d (日 K) 計算指標
                if not df_hist_1d.empty:
                    df_hist_1d["SMA20"] = ta.sma(df_hist_1d["Close"], length=20)
                    df_hist_1d["ATR14"] = ta.atr(
                        df_hist_1d["High"],
                        df_hist_1d["Low"],
                        df_hist_1d["Close"],
                        length=14,
                    )
                    df_hist_1d["RSI14"] = ta.rsi(df_hist_1d["Close"], length=14)

                    last_row = df_hist_1d.iloc[-1]

                    from market_analysis.sentiment_engine import SentimentEngine

                    skew_res = await SentimentEngine.calculate_skew(sym)
                    res["skew_data"] = skew_res
                    skew_val = (skew_res.get("skew") or 0.0) / 100.0

                    uoa_detected = bool(
                        res.get("uoa_list")
                    )  # 這裡假設 analyze_symbol 已處理 uoa_list

                    price = last_row["Close"]
                    ma20 = last_row["SMA20"]
                    atr = last_row["ATR14"]
                    rsi = last_row["RSI14"]

                    # 清理指標防範空值/NaN
                    clean_ma20, clean_atr, clean_rsi = (
                        self._clean_market_condition_inputs(price, ma20, atr, rsi)
                    )

                    # 計算相對強度 (Relative Strength)
                    from market_analysis.risk_engine import (
                        get_sector_benchmark,
                        calculate_relative_strength_index,
                    )

                    benchmark_symbol = get_sector_benchmark(sym)
                    df_bench = await market_data_service.get_history_df(
                        benchmark_symbol, period="1y", interval="1d"
                    )
                    relative_strength = calculate_relative_strength_index(
                        df_hist_1d, df_bench, n=20
                    )

                    try:
                        condition = MarketCondition(
                            vix=vix_spot,
                            skew_percent=skew_val,
                            asset_price=price,
                            ma20=clean_ma20,
                            atr_14=clean_atr,
                            rsi_14=clean_rsi,
                            uoa_detected=uoa_detected,
                            relative_strength=relative_strength,
                        )
                        res["execution_decision"] = (
                            self.execution_router.evaluate_market(condition)
                        )
                    except Exception as ex_router_e:
                        logger.debug(
                            f"ExecutionRouter 評估失敗 for {sym}: {ex_router_e}"
                        )
                        res["execution_decision"] = Signal.SKIP
            except Exception as ex_router_e:
                logger.warning(f"ExecutionRouter 評估失敗 for {sym}: {ex_router_e}")
                if not res.get("price") or res.get("price") <= 0:
                    res["price"] = (
                        df_hist_1d["Close"].iloc[-1] if not df_hist_1d.empty else 0.0
                    )

            # 語意風控判定: 只在有任何訊號觸發時執行以節省成本
            if is_option_valid:
                # 併行獲取新聞 (Finnhub) 與 Reddit (從 KV 快取讀取)
                news_task = news_service.fetch_recent_news(sym)

                from database.cache import get_kv_cache

                reddit_text = (
                    get_kv_cache(f"reddit_sentiment_{sym}")
                    or "暫無快取情緒資料 (等待每日更新)。"
                )

                news_text = await news_task

                # 直接略過 AI 判斷，確保 0 延遲與低成本
                res["ai_decision"] = "SKIP"
                res["ai_reasoning"] = (
                    "系統已全面升級為量化規則引擎，停用舊版 LLM 語意風控"
                )

                res.update({"news_text": news_text, "reddit_text": reddit_text})

            return target, res
        except Exception as e:
            logger.error(f"掃描標的 {sym} 失敗: {e}")
            return target, None
