import asyncio
import logging
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
            e_date = await asyncio.to_thread(market_math.get_next_earnings_date, sym)
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
            spy_task = asyncio.to_thread(market_data_service.get_history_df, "SPY", "1y")
            macro_task = asyncio.to_thread(market_data_service.get_macro_environment)
            
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            
            spy_price = df_spy['Close'].iloc[-1] if not df_spy.empty else 670.0
            macro_data = MacroContext(
                vix=macro_raw.get('vix', 18.0),
                oil_price=macro_raw.get('oil', 75.0)
            )
        except Exception as e:
            logger.error(f"全域基準資料獲取失敗: {e}")
            df_spy, spy_price, macro_data = None, 670.0, MacroContext(vix=20.0, oil_price=85.0)

        # 2. 提取不重複標的進行「批次掃描」
        unique_targets = set((sym, stock_cost, use_llm) for uid, sym, stock_cost, use_llm in all_watchlists)
        scan_results = {}
        news_cache = {}
        reddit_cache = {}

        for sym, stock_cost, use_llm in unique_targets:
            try:
                # 使用 to_thread 確保 EMA/SMA 計算不阻塞主線程
                res = await asyncio.to_thread(market_math.analyze_symbol, sym, stock_cost, 0.0, df_spy, spy_price)
                
                if res:
                    # 併行獲取新聞與 Reddit (活用快取)
                    if sym not in news_cache:
                        news_cache[sym] = await news_service.fetch_recent_news(sym)
                    if sym not in reddit_cache:
                        reddit_cache[sym] = await reddit_service.get_reddit_context(sym)

                    news_text = news_cache[sym]
                    reddit_text = reddit_cache[sym]

                    # 語意風控判定
                    if use_llm:
                        ai_verdict = await llm_service.evaluate_trade_risk(sym, res['strategy'], news_text, reddit_text)
                        res['ai_decision'] = ai_verdict.get('decision', 'APPROVE')
                        res['ai_reasoning'] = ai_verdict.get('reasoning', '無資料')
                    else:
                        res['ai_decision'] = 'SKIP'
                        res['ai_reasoning'] = '未啟用 LLM 語意風控'
                    
                    res.update({'news_text': news_text, 'reddit_text': reddit_text})
                    scan_results[(sym, stock_cost, use_llm)] = res
                    
            except Exception as e:
                logger.error(f"掃描標的 {sym} 失敗: {e}")
            
            # 短暫休眠防止速率限制 (Rate Limit)
            await asyncio.sleep(0.1)

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
                    projected_exposure_pct = (projected_total_delta * spy_price / user_capital) * 100

                    data.update({
                        'safe_qty': safe_qty,
                        'hedge_spy': hedge_spy,
                        'projected_exposure_pct': round(projected_exposure_pct, 2),
                        'spy_price': spy_price,
                        'macro_vix': macro_data.vix,
                        'macro_oil': macro_data.oil_price,
                        'uid': uid
                    })
                    
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
                    tags=["auto_scan"]
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
                # 獲取全站最近紀錄 (修正原代碼傳 None 的問題，改用 get_virtual_trades)
                all_history = await asyncio.to_thread(get_virtual_trades, user_id=None)
                spy_quote = market_data_service.get_quote("SPY")
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

    async def get_after_market_report_data(self) -> Dict[int, List[str]]:
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
            user_capital = database.get_full_user_context(uid).capital
            report_lines = await asyncio.to_thread(
                portfolio.check_portfolio_status_logic, 
                rows, 
                user_capital
            )
            if report_lines:
                results[uid] = report_lines
        return results
