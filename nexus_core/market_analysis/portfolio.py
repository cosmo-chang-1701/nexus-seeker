from services import market_data_service
from .data import get_option_chain
import pandas as pd
import numpy as np
import logging
import math
from datetime import datetime
from .greeks import calculate_greeks

from .risk_engine import (
    evaluate_defense_status as evaluate_defense_status_core, 
    calculate_beta, 
    get_macro_risk_metrics as get_macro_risk_metrics_core, 
    analyze_sector_correlation as analyze_sector_correlation_core,
    simulate_exposure_impact,
    optimize_position_risk
)
from .margin import calculate_option_margin
from .report_formatter import (
    format_position_report, 
    format_macro_risk_report as format_macro_risk_report_core, 
    format_correlation_report as format_correlation_report_core
)

logger = logging.getLogger(__name__)

def check_portfolio_status_logic(portfolio_rows, user_capital=50000.0):
    """
    [Facade] 盤後動態結算與風險管線編排者 (Orchestrator)
    整合了 ETF 404 防護、Beta-Weighted Greeks 與二階風險評估。
    """
    if not portfolio_rows:
        return []

    analyzer = PortfolioStatusOrchestrator(user_capital)
    return analyzer.run(portfolio_rows)

class PortfolioStatusOrchestrator:
    """
    負責協調資料獲取、風險計算與報告生成的編排類。
    """
    def __init__(self, user_capital: float):
        self.user_capital = user_capital
        self.today = datetime.now().date()
        self.spy_price = 500.0
        self.spy_hist = pd.DataFrame()
        self.stock_hist_map = {}
        self.report_lines = []
        
        # 聚合數據
        self.total_beta_delta = 0.0
        self.total_theta = 0.0
        self.total_margin_used = 0.0
        self.total_gamma = 0.0

    def run(self, portfolio_rows):
        # 1. 預處理：批次下載資料
        self._prepare_market_data(portfolio_rows)
        
        # 2. 按標的分群處理
        positions_by_symbol = {}
        for row in portfolio_rows:
            positions_by_symbol.setdefault(row[0], []).append(row)
            
        # 3. 遍歷部位計算風險
        for symbol, rows in positions_by_symbol.items():
            self._process_symbol_positions(symbol, rows)
            
        # 4. 生成宏觀與相關性報告
        self._append_final_reports(positions_by_symbol)
        
        return self.report_lines

    def _prepare_market_data(self, portfolio_rows):
        """透過 Finnhub 下載所有必要的行情資料。"""
        unique_symbols = sorted(list(set([row[0] for row in portfolio_rows])))
        all_targets = unique_symbols + ["SPY"]
        
        try:
            for sym in all_targets:
                df = market_data_service.get_history_df(sym, "90d")
                if df.empty:
                    continue
                if sym == "SPY":
                    self.spy_hist = df
                    self.spy_price = df['Close'].iloc[-1]
                else:
                    self.stock_hist_map[sym] = df
        except Exception as e:
            logger.warning(f"批次歷史資料下載失敗: {e}")

    def _process_symbol_positions(self, symbol, rows):
        """處理單一標下的所有持倉。"""
        try:
            stock_hist = self.stock_hist_map.get(symbol, pd.DataFrame())
            
            # 獲取標的資訊 (透過 Finnhub)
            stock_info = self._get_stock_info(symbol, stock_hist)
            current_stock_price = stock_info['price']
            dividend_yield = stock_info['dividend_yield']
            beta = stock_info['beta']
            
            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity, stock_cost = row
                
                # 獲取選擇權資料
                if expiry not in option_chains_cache:
                    option_chains_cache[expiry] = get_option_chain(symbol, expiry)
                
                calls, puts = option_chains_cache[expiry]
                chain_data = calls if opt_type == "call" else puts
                
                if chain_data.empty: continue
                contract = chain_data[chain_data['strike'] == strike]
                if contract.empty: continue
                
                current_price = contract['lastPrice'].iloc[0]
                iv = contract['impliedVolatility'].iloc[0]
                
                # 計算時間參數
                exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                dte = (exp_date - self.today).days
                t_years = max(dte, 1) / 365.0 
                
                # 計算 Greeks
                greeks = calculate_greeks(opt_type, current_stock_price, strike, t_years, iv, dividend_yield)
                
                # 計算保證金
                margin = calculate_option_margin(opt_type, strike, current_stock_price, current_price, quantity, stock_cost)
                self.total_margin_used += margin

                # Beta-Weighting 聚合
                weight_factor = beta * (current_stock_price / self.spy_price)
                
                pos_delta = greeks['delta'] * quantity * 100
                spx_weighted_delta = pos_delta * weight_factor
                self.total_beta_delta += spx_weighted_delta
                
                self.total_theta += greeks['theta'] * quantity * 100
                
                pos_gamma = greeks['gamma'] * quantity * 100
                spx_weighted_gamma = pos_gamma * (weight_factor ** 2)
                self.total_gamma += spx_weighted_gamma

                # 生成單筆報告
                pnl_pct = (entry_price - current_price) / entry_price if quantity < 0 else (current_price - entry_price) / entry_price
                status = evaluate_defense_status_core(quantity, opt_type, pnl_pct, greeks['delta'], dte)
                cc_tag = " 🛡️(CC)" if (opt_type == 'call' and stock_cost > 0.0) else ""
                
                self.report_lines.append(
                    format_position_report(symbol, expiry, strike, opt_type, cc_tag, 
                                           entry_price, current_price, pnl_pct, dte, 
                                           spx_weighted_delta, status)
                )
        except Exception as e:
            logger.error(f"Symbol {symbol} 處理失敗: {e}", exc_info=True)

    def _get_stock_info(self, symbol: str, stock_hist):
        """
        透過 Finnhub 獲取標的價格、Beta 與股息率。
        不再依賴 yfinance 的 fast_info / info，避免 ETF 404 問題。
        """
        try:
            # 1. 價格取得邏輯 (優先級: Finnhub quote > history_cache)
            quote = market_data_service.get_quote(symbol)
            price = quote.get('c', 0.0) if quote else 0.0
            if price is None or price <= 0:
                if not stock_hist.empty:
                    price = stock_hist['Close'].iloc[-1]
                else:
                    price = 0.0
            
            # 2. 股息率估算 (透過 Finnhub basic financials)
            is_etf_flag = market_data_service.is_etf(symbol)
            if is_etf_flag:
                dividend_yield = 0.015  # ETF 預設值
            else:
                dividend_yield = market_data_service.get_dividend_yield(symbol)
            
            # 3. Beta 值計算邏輯
            # 優先使用動態回歸計算 (Regression Beta)
            if not self.spy_hist.empty and not stock_hist.empty:
                beta_val = calculate_beta(stock_hist, self.spy_hist)
            else:
                beta_val = 1.0
                
        except Exception as e:
            # 發生異常時的 Fallback 處理
            price = stock_hist['Close'].iloc[-1] if not stock_hist.empty else 0.0
            dividend_yield, beta_val = 0.0, 1.0
        
        return {'price': price, 'dividend_yield': dividend_yield, 'beta': beta_val}


    def _append_final_reports(self, positions_by_symbol):
        """追加宏觀風險與相關性報告。"""
        metrics = get_macro_risk_metrics_core(
            self.total_beta_delta, self.total_theta, self.total_margin_used, 
            self.total_gamma, self.user_capital, self.spy_price
        )
        self.report_lines.extend(format_macro_risk_report_core(metrics, self.spy_price))
        
        symbols = list(positions_by_symbol.keys())
        high_corr_pairs = analyze_sector_correlation_core(symbols)
        self.report_lines.extend(format_correlation_report_core(high_corr_pairs, len(symbols)))

# 回溯相容的輔助函數 (保留原名稱，移除 legacy 前綴)
def calculate_macro_risk(total_beta_delta, total_theta, total_margin_used, total_gamma, user_capital, spy_price=500.0):
    """回溯相容封裝。"""
    metrics = get_macro_risk_metrics_core(
        total_beta_delta, total_theta, total_margin_used, 
        total_gamma, user_capital, spy_price
    )
    return format_macro_risk_report_core(metrics, spy_price)

def analyze_correlation(positions_by_symbol):
    """回溯相容封裝。"""
    symbols = list(positions_by_symbol.keys())
    pairs = analyze_sector_correlation_core(symbols)
    return format_correlation_report_core(pairs, len(symbols))

def evaluate_defense_status(quantity, opt_type, pnl_pct, current_delta, dte):
    """回溯相容封裝。"""
    return evaluate_defense_status_core(quantity, opt_type, pnl_pct, current_delta, dte)
def refresh_portfolio_greeks(user_id: int = None):
    """
    [Worker] 重新整理投資組合的希臘字母數據並寫回資料庫。
    若 user_id 為 None，則處理全站所有使用者。
    """
    try:
        from database.portfolio import get_all_portfolio, get_user_portfolio, update_portfolio_greeks
        from database.virtual_trading import get_all_open_virtual_trades, get_open_virtual_trades, update_virtual_trade_greeks
        
        # 1. 取得持倉資料
        if user_id:
            real_positions = get_user_portfolio(user_id)
            virtual_positions = get_open_virtual_trades(user_id)
        else:
            real_positions = get_all_portfolio()
            virtual_positions = get_all_open_virtual_trades()

        # 2. 準備市場資料批次下載
        symbols = set()
        for row in real_positions:
            # get_all_portfolio 回傳 [uid, id, sym, ...] 
            # get_user_portfolio 回傳 [id, sym, ...]
            # 這裡需要小心索引，或者是統一回傳格式。
            # 觀察 database/portfolio.py:
            # get_user_portfolio: SELECT id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost
            # get_all_portfolio: SELECT user_id, id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost
            sym = row[2] if len(row) > 8 else row[1]
            symbols.add(sym)
        for row in virtual_positions:
            symbols.add(row['symbol'])
        
        if not symbols:
            return
            
        spy_df = market_data_service.get_history_df("SPY", "5d")
        spy_price = spy_df['Close'].iloc[-1] if not spy_df.empty else 670.0
        
        stock_data = {}
        for sym in symbols:
            df = market_data_service.get_history_df(sym, "5d")
            quote = market_data_service.get_quote(sym)
            stock_data[sym] = {
                'price': quote.get('c', df['Close'].iloc[-1] if not df.empty else 0.0),
                'beta': calculate_beta(df, spy_df) if not df.empty and not spy_df.empty else 1.0,
                'div_yield': market_data_service.get_dividend_yield(sym)
            }

        # 3. 更新真實持倉
        for row in real_positions:
            # 索引偏移處理
            offset = 1 if len(row) > 8 else 0
            trade_id = row[offset]
            sym = row[offset+1]
            opt_type = row[offset+2]
            strike = row[offset+3]
            expiry = row[offset+4]
            qty = row[offset+6]
            
            s_info = stock_data.get(sym)
            if not s_info or s_info['price'] <= 0: continue
            
            # 計算 Greeks (簡化 iv 獲取)
            mid, iv = get_option_chain_mid_iv(sym, expiry, strike, opt_type)
            if iv <= 0: continue
            
            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            t_years = max((exp_date - datetime.now().date()).days, 1) / 365.0
            
            greeks = calculate_greeks(opt_type, s_info['price'], strike, t_years, iv, s_info['div_yield'])
            
            # Beta-Weighting
            weight_factor = s_info['beta'] * (s_info['price'] / spy_price)
            weighted_delta = greeks['delta'] * qty * 100 * weight_factor
            
            update_portfolio_greeks(trade_id, round(weighted_delta, 4), round(greeks['theta'] * qty * 100, 4), round(greeks['gamma'] * qty * 100 * (weight_factor**2), 6))

        # 4. 更新虛擬持倉
        for row in virtual_positions:
            trade_id = row['id']
            sym = row['symbol']
            opt_type = row['opt_type']
            strike = row['strike']
            expiry = row['expiry']
            qty = row['quantity']
            
            s_info = stock_data.get(sym)
            if not s_info or s_info['price'] <= 0: continue
            
            mid, iv = get_option_chain_mid_iv(sym, expiry, strike, opt_type)
            if iv <= 0: continue
            
            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            t_years = max((exp_date - datetime.now().date()).days, 1) / 365.0
            
            greeks = calculate_greeks(opt_type, s_info['price'], strike, t_years, iv, s_info['div_yield'])
            
            weight_factor = s_info['beta'] * (s_info['price'] / spy_price)
            weighted_delta = greeks['delta'] * qty * 100 * weight_factor
            
            update_virtual_trade_greeks(trade_id, round(weighted_delta, 4), round(greeks['theta'] * qty * 100, 4), round(greeks['gamma'] * qty * 100 * (weight_factor**2), 6))

    except Exception as e:
        logger.error(f"refresh_portfolio_greeks 失敗: {e}", exc_info=True)

def get_option_chain_mid_iv(symbol, expiry, strike, opt_type):
    """內部輔助：獲取合約的 Mid 價格與 IV"""
    try:
        calls, puts = get_option_chain(symbol, expiry)
        chain = calls if opt_type == 'call' else puts
        contract = chain[chain['strike'] == strike]
        if not contract.empty:
            mid = (contract['bid'].iloc[0] + contract['ask'].iloc[0]) / 2
            iv = contract['impliedVolatility'].iloc[0]
            return mid, iv
    except:
        pass
    return 0.0, 0.0
