import yfinance as yf
import pandas as pd
import numpy as np
import logging
import math
from datetime import datetime
from py_vollib.black_scholes_merton.greeks.analytical import delta, theta, gamma

from config import RISK_FREE_RATE
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
        """下載所有必要的行情資料。"""
        unique_symbols = sorted(list(set([row[0] for row in portfolio_rows])))
        all_targets = unique_symbols + ["SPY"]
        
        try:
            hists = yf.download(all_targets, period="90d", progress=False)
            if not hists.empty:
                # 取得 SPY 基準
                if "SPY" in hists['Close']:
                    spy_series = hists['Close']['SPY']
                    self.spy_hist = pd.DataFrame({'Close': spy_series})
                    self.spy_price = spy_series.iloc[-1]
                
                # 將其他標的存入 Map
                for sym in unique_symbols:
                    if sym in hists['Close']:
                        self.stock_hist_map[sym] = pd.DataFrame({'Close': hists['Close'][sym]})
        except Exception as e:
            logger.warning(f"批次歷史資料下載失敗: {e}")

    def _process_symbol_positions(self, symbol, rows):
        """處理單一標下的所有持倉。"""
        try:
            ticker = yf.Ticker(symbol)
            stock_hist = self.stock_hist_map.get(symbol, pd.DataFrame())
            
            # 獲取標的資訊 (ETF 防護)
            stock_info = self._get_stock_info(ticker, stock_hist)
            current_stock_price = stock_info['price']
            dividend_yield = stock_info['dividend_yield']
            beta = stock_info['beta']
            
            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity, stock_cost = row
                
                # 獲取選擇權資料
                if expiry not in option_chains_cache:
                    option_chains_cache[expiry] = ticker.option_chain(expiry)
                
                chain_data = option_chains_cache[expiry].calls if opt_type == "call" else option_chains_cache[expiry].puts
                contract = chain_data[chain_data['strike'] == strike]
                if contract.empty: continue
                
                current_price = contract['lastPrice'].iloc[0]
                iv = contract['impliedVolatility'].iloc[0]
                
                # 計算時間參數
                exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                dte = (exp_date - self.today).days
                t_years = max(dte, 1) / 365.0 
                
                # 計算 Greeks
                greeks = self._calculate_greeks(opt_type, current_stock_price, strike, t_years, iv, dividend_yield)
                
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

    def _get_stock_info(self, ticker, stock_hist):
        """獲取標的價格、Beta 與股息率。"""
        try:
            f_info = ticker.fast_info
            price = f_info.get('last_price') or (stock_hist['Close'].iloc[-1] if not stock_hist.empty else ticker.history(period="1d")['Close'].iloc[-1])
            is_etf = f_info.get('quoteType') == 'ETF'
            dividend_yield = 0.015 if is_etf else (f_info.get('dividendYield', 0.0) or 0.0)
            
            if not self.spy_hist.empty and not stock_hist.empty:
                beta_val = calculate_beta(stock_hist, self.spy_hist)
            else:
                beta_val = ticker.info.get('beta', 1.0) if not is_etf else 1.0
        except:
            price = stock_hist['Close'].iloc[-1] if not stock_hist.empty else ticker.history(period="1d")['Close'].iloc[-1]
            dividend_yield, beta_val = 0.0, 1.0
            
        return {'price': price, 'dividend_yield': dividend_yield, 'beta': beta_val}

    def _calculate_greeks(self, opt_type, stock_price, strike, t_years, iv, q):
        """計算單一選擇權的 Greeks。"""
        flag = 'c' if opt_type == 'call' else 'p'
        try:
            return {
                'delta': delta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
                'theta': theta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
                'gamma': gamma(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q)
            }
        except:
            return {'delta': 0.0, 'theta': 0.0, 'gamma': 0.0}

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