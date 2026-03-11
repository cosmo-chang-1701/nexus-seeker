import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional

import database
import market_math
import market_time
from market_analysis import portfolio, hedging
from market_analysis.ghost_trader import GhostTrader
from services import market_data_service, news_service, llm_service, reddit_service

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")

class TradingService:
    """
    提供核心交易業務邏輯，將 Discord 機器人的介面與底層計算/資料處理分離。
    """

    def __init__(self, bot):
        self.bot = bot
        self.vtr_engine = GhostTrader()

    async def get_pre_market_alerts_data(self, warning_days: int) -> Dict[int, Dict[str, Any]]:
        """
        取得盤前財報警報數據。
        """
        today = datetime.now(ny_tz).date()
        all_portfolios = database.get_all_portfolio()
        all_watchlists = database.get_all_watchlist()

        user_symbols = {}
        unique_symbols = set()

        for row in all_portfolios:
            uid, sym = row[0], row[2]
            user_symbols.setdefault(uid, {'port': set(), 'watch': set()})['port'].add(sym)
            unique_symbols.add(sym)

        for row in all_watchlists:
            uid, sym = row[0], row[1]
            user_symbols.setdefault(uid, {'port': set(), 'watch': set()})['watch'].add(sym)
            unique_symbols.add(sym)

        # 批次快取財報日期
        earnings_cache = {}
        for sym in unique_symbols:
            e_date = await market_math.get_next_earnings_date(sym)
            if e_date:
                if isinstance(e_date, datetime): e_date = e_date.date()
                earnings_cache[sym] = e_date

        results = {}
        for uid, symbols_data in user_symbols.items():
            alerts = []
            combined_symbols = symbols_data['port'].union(symbols_data['watch'])
            
            for sym in combined_symbols:
                e_date = earnings_cache.get(sym)
                if e_date:
                    days_left = (e_date - today).days
                    if 0 <= days_left <= warning_days:
                        item = {
                            'symbol': sym,
                            'is_portfolio': sym in symbols_data['port'],
                            'earnings_date': e_date,
                            'days_left': days_left
                        }
                        alerts.append(item)
            results[uid] = {
                'alerts': alerts,
                'scanned_symbols': sorted(combined_symbols)
            }
        return results

    async def run_market_scan(self, is_auto: bool = True, triggered_by_id: Optional[int] = None) -> Dict[int, List[Dict[str, Any]]]:
        """
        執行全站市場掃描 (整合 EMA, Macro Stress Matrix 與 VIX/Oil 監控)
        """
        all_watchlists = database.get_all_watchlist()
        if not all_watchlists:
            return {}

        from market_analysis.risk_engine import MacroContext

        # 1. 🚀 獲取全域基準資料 (僅抓取一次，減少 API 消耗)
        try:
            spy_task = market_data_service.get_history_df("SPY", "1y")
            macro_task = market_data_service.get_macro_environment()
            
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            
            spy_price = df_spy['Close'].iloc[-1] if not df_spy.empty else 670.0
            macro_data = MacroContext(
                vix=macro_raw.get('vix', 18.0),
                oil_price=macro_raw.get('oil', 75.0),
                vix_change=macro_raw.get('vix_change', 0.0)
            )
        except Exception as e:
            logger.error(f"全域基準資料獲取失敗: {e}")
            df_spy, spy_price, macro_data = None, 670.0, MacroContext(vix=20.0, oil_price=85.0, vix_change=0.0)

        # 2. 提取不重複標的進行「併行批次掃描」
        unique_targets = list(set((sym, stock_cost, use_llm) for uid, sym, stock_cost, use_llm in all_watchlists))
        
        async def _scan_single_target(target):
            sym, stock_cost, use_llm = target
            try:
                # analyze_symbol 已經是 async
                res = await market_math.analyze_symbol(sym, stock_cost, df_spy, spy_price)
                if not res:
                    return target, None

                # 🚀 新增 EMA 訊號偵測 (Crossover & Test)
                # 為確保 EMA 準確性，獲取至少 60 天歷史數據 (1-Hour 時框作為小週期觸發)
                df_hist_1h = await market_data_service.get_history_df(sym, period="60d", interval="1h")
                if not df_hist_1h.empty:
                    ema_8_sig = market_math.detect_ema_signals(df_hist_1h, window=8)
                    ema_21_sig = market_math.detect_ema_signals(df_hist_1h, window=21)
                    
                    # 整合訊號至結果字典
                    res['ema_signals'] = [sig for sig in [ema_8_sig, ema_21_sig] if sig]
                    
                    # 如果有 EMA 訊號，強制標註為「高價值追蹤」
                    if res['ema_signals']:
                        res['is_priority_alert'] = True

                # 併行獲取新聞與 Reddit
                news_task = news_service.fetch_recent_news(sym)
                reddit_task = reddit_service.get_reddit_context(sym)
                news_text, reddit_text = await asyncio.gather(news_task, reddit_task)

                # 語意風控判定
                if use_llm:
                    ai_verdict = await llm_service.evaluate_trade_risk(sym, res['strategy'], news_text, reddit_text)
                    res['ai_decision'] = ai_verdict.get('decision', 'APPROVE')
                    res['ai_reasoning'] = ai_verdict.get('reasoning', '無資料')
                else:
                    res['ai_decision'] = 'SKIP'
                    res['ai_reasoning'] = '未啟用 LLM 語意風控'
                
                res.update({'news_text': news_text, 'reddit_text': reddit_text})
                return target, res
            except Exception as e:
                logger.error(f"掃描標的 {sym} 失敗: {e}")
                return target, None

        # 🚀 併行執行所有標的分量 (分批執行以防止觸發 API Rate Limit)
        results_list = []
        batch_size = 10
        for i in range(0, len(unique_targets), batch_size):
            chunk = unique_targets[i:i + batch_size]
            tasks = [_scan_single_target(t) for t in chunk]
            batch_results = await asyncio.gather(*tasks)
            results_list.extend(batch_results)
            if i + batch_size < len(unique_targets):
                await asyncio.sleep(0.5)  # 給 API 一點緩衝，並釋放池空間
        
        scan_results = {target: res for target, res in results_list if res is not None}

        if not scan_results:
            return {}

        # 3. 準備使用者分發與「個人化 NRO 優化」
        user_alerts_results = {}
        user_watchlists = {}
        for uid, sym, stock_cost, use_llm in all_watchlists:
            user_watchlists.setdefault(uid, []).append((sym, stock_cost, use_llm))

        for uid, watchlist_items in user_watchlists.items():
            valid_user_alerts = []
            
            # 獲取該使用者的動態風險參數與目前持倉統計
            # 🚀 [Resource Isolation] 確保 Greeks 數據最新，避免使用舊 Delta 判斷避險
            await asyncio.to_thread(portfolio.refresh_portfolio_greeks, uid)
            
            user_context = database.get_full_user_context(uid)
            user_capital = user_context.capital
            user_risk_pref = user_context.risk_limit_base
            current_total_delta = user_context.total_weighted_delta

            for sym, stock_cost, use_llm in watchlist_items:
                if (sym, stock_cost, use_llm) in scan_results:
                    data = scan_results[(sym, stock_cost, use_llm)].copy()
                    
                    # 🚀 整合核心：注入宏觀背景進行風險優化
                    strategy = data.get('strategy', '')
                    safe_qty, hedge_spy = portfolio.optimize_position_risk(
                        current_delta=current_total_delta,
                        unit_weighted_delta=data.get('weighted_delta', 0.0),
                        user_capital=user_capital,
                        spy_price=spy_price,
                        stock_iv=data.get('iv', 0.15),
                        strategy=strategy,
                        macro_data=macro_data, # 注入全域宏觀數據
                        base_risk_limit_pct=user_risk_pref
                    )

                    # 模擬成交後的衝擊
                    side_multiplier = -1 if "STO" in strategy else 1
                    # 這裡使用 safe_qty 進行模擬，反映系統真實建議
                    new_trade_impact = data.get('weighted_delta', 0.0) * side_multiplier * safe_qty
                    projected_total_delta = current_total_delta + new_trade_impact
                    projected_exposure_pct = (projected_total_delta * spy_price / user_capital) * 100 if user_capital > 0 else 0.0

                    data.update({
                        'safe_qty': safe_qty,
                        'hedge_spy': hedge_spy,
                        'projected_exposure_pct': round(projected_exposure_pct, 2),
                        'spy_price': spy_price,
                        'macro_vix': macro_data.vix,
                        'macro_vix_change': macro_data.vix_change,
                        'macro_oil': macro_data.oil_price,
                        'uid': uid
                    })

                    # 🚀 新增：對沖解除建議 (Hedge Unlocking)
                    # 只有在偵測到 EMA CROSSOVER 且為多頭時才評估
                    ema_signals = data.get('ema_signals', [])
                    for sig in ema_signals:
                        if sig.get('type') == 'CROSSOVER' and sig.get('direction') == 'BULLISH':
                            from services.alert_filter import validate_mtf_trend
                            mtf = await asyncio.to_thread(validate_mtf_trend, sym, sig)
                            unlock_advice = hedging.suggest_hedge_unlock(user_context, data, mtf)
                            if unlock_advice:
                                data['hedge_unlock'] = unlock_advice
                            break
                    
                    # 🚀 3. 新增：自動回補避險 (Auto Re-Hedging)
                    # 條件：任一維度觸發 (技術/宏觀/曝險) 且符合 State Lock (1hr)
                    now_ts = int(time.time())
                    if now_ts - user_context.last_rehedge_alert_time > 3600:
                        rehedge_advice = hedging.evaluate_rehedge_necessity(user_context, data)
                        if rehedge_advice:
                            # 🚀 應用 STHE 動態 Tau 校正
                            rehedge_advice = hedging.get_tuned_risk_advice(uid, rehedge_advice)
                            data['rehedge_info'] = rehedge_advice
                            # 更新資料庫中的 last_rehedge_alert_time 以防止重複發送
                            database.upsert_user_config(uid, last_rehedge_alert_time=now_ts)
                            # 為了讓同一次 Scan 中的其他 Symbol 不重複觸發，
                            # 手動更新 user_context 的值 (雖然下一個 UserLoop 會重新抓，但同一個 UserLoop 會繼續跑剩餘 symbol)
                            user_context.last_rehedge_alert_time = now_ts
                    
                    valid_user_alerts.append(data)
            
            if valid_user_alerts:
                user_alerts_results[uid] = valid_user_alerts

        return user_alerts_results

    async def execute_vtr_auto_entry(self, data: Dict[str, Any]):
        """
        執行 VTR 自動建倉。
        """
        uid = data['uid']
        sym = data['symbol']
        strategy = data.get('strategy', '')
        safe_qty = data.get('safe_qty', 0)
        
        if safe_qty > 0:
            try:
                opt_t = 'put' if 'PUT' in strategy else 'call'
                qty = -safe_qty if 'STO' in strategy else safe_qty
                
                # 自動判定類別：Short SPY 或 BTO SPY Put 為 HEDGE
                trade_category = 'SPECULATIVE'
                if sym == 'SPY':
                    if qty < 0 or (opt_t == 'put' and qty > 0):
                        trade_category = 'HEDGE'
                        
                await asyncio.to_thread(
                    self.vtr_engine.record_virtual_entry,
                    user_id=uid,
                    symbol=sym,
                    opt_type=opt_t,
                    strike=data['strike'],
                    expiry=data['target_date'],
                    quantity=qty,
                    weighted_delta=data.get('weighted_delta', 0.0),
                    theta=data.get('theta', 0.0),
                    gamma=data.get('gamma', 0.0),
                    tags=["auto_scan"],
                    trade_category=trade_category
                )
            except Exception as e:
                logger.error(f"VTR Entry failed: {e}")

    async def monitor_vtr_and_calculate_hedging(self) -> List[Dict[str, Any]]:
        """
        監控 VTR 持倉，執行管理與轉倉，並計算對沖建議。
        返回需要發送給使用者的結算與對沖訊息數據。
        """
        results = []
        try:
            from database.virtual_trading import get_all_open_virtual_trades, get_virtual_trades
            before_trades = await asyncio.to_thread(get_all_open_virtual_trades)
            before_ids = {t['id'] for t in before_trades}

            # 執行管理與轉倉
            await asyncio.to_thread(self.vtr_engine.manage_virtual_positions)
            await asyncio.to_thread(self.vtr_engine.execute_virtual_roll)
            
            # 重新檢查交易列表
            after_trades = await asyncio.to_thread(get_all_open_virtual_trades)
            after_ids = {t['id'] for t in after_trades}
            closed_ids = before_ids - after_ids

            if closed_ids:
                # 獲獲取全站最近紀錄
                all_history = await asyncio.to_thread(get_virtual_trades, user_id=None)
                spy_quote = await market_data_service.get_quote("SPY")
                spy_price = spy_quote.get('c', 500.0) if spy_quote else 500.0

                for tid in closed_ids:
                    trade_info = next((t for t in all_history if t['id'] == tid), None)
                    if not trade_info: continue
                    
                    uid = trade_info['user_id']
                    user_context = database.get_full_user_context(uid)
                    current_total_delta = user_context.total_weighted_delta
                    user_capital = user_context.capital

                    # 位階判斷
                    target_delta, regime = hedging.get_market_regime_target(spy_price, user_capital)
                    hedge = hedging.calculate_autonomous_hedge(current_total_delta, target_delta, spy_price)

                    results.append({
                        'uid': uid,
                        'trade_info': trade_info,
                        'current_total_delta': current_total_delta,
                        'user_capital': user_capital,
                        'spy_price': spy_price,
                        'regime': regime,
                        'target_delta': target_delta,
                        'hedge': hedge
                    })
        except Exception as e:
            logger.error(f"VTR monitoring service error: {e}")
        
        return results

    async def get_after_market_report_data(self) -> Dict[int, Dict[str, Any]]:
        """
        取得盤後結算報告數據。
        """
        all_portfolios = database.get_all_portfolio()
        if not all_portfolios:
            return {}
        
        user_ports = {}
        for row in all_portfolios:
            uid = row[0]
            user_ports.setdefault(uid, []).append(row[2:])

        results = {}
        for uid, rows in user_ports.items():
            user_ctx = database.get_full_user_context(uid)
            user_capital = user_ctx.capital
            
            # 1. 執行標準持倉報告邏輯
            report_lines = await asyncio.to_thread(
                portfolio.check_portfolio_status_logic, 
                rows, 
                user_capital
            )
            
            # 2. 執行對沖績效分析
            hedge_analysis = await asyncio.to_thread(
                hedging.analyze_hedge_performance,
                uid
            )
            
            if report_lines:
                # 🚀 執行 STHE 每日自動優化排程
                # 1. 結算今日有效性
                await asyncio.to_thread(hedging.calculate_daily_effectiveness, uid)
                # 2. 滾動更新 Tau 係數
                new_tau = await asyncio.to_thread(hedging.calculate_dynamic_tau, uid)
                
                # 將 Tau 注入分析字典
                hedge_analysis['dynamic_tau'] = new_tau
                
                results[uid] = {
                    "report_lines": report_lines,
                    "hedge_analysis": hedge_analysis
                }
        return results
