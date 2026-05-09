from services import market_data_service
from .data import get_option_chain
import pandas as pd
import numpy as np
import logging
import math
import asyncio
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

async def check_portfolio_status_logic(portfolio_rows, user_capital=50000.0):
    """
    [Facade] 盤後動態結算與風險管線編排者 (Orchestrator)
    """
    if not portfolio_rows:
        return []

    analyzer = PortfolioStatusOrchestrator(user_capital)
    return await analyzer.run(portfolio_rows)

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
        
        self.total_beta_delta = 0.0
        self.total_theta = 0.0
        self.total_margin_used = 0.0
        self.total_gamma = 0.0

    async def run(self, portfolio_rows):
        # 1. 預處理：批次下載資料
        await self._prepare_market_data(portfolio_rows)
        
        # 2. 按標的分群處理
        positions_by_symbol = {}
        for row in portfolio_rows:
            positions_by_symbol.setdefault(row[0], []).append(row)
            
        # 3. 遍歷部位計算風險
        for symbol, rows in positions_by_symbol.items():
            await self._process_symbol_positions(symbol, rows)
            
        # 4. 生成宏觀與相關性報告
        await self._append_final_reports(positions_by_symbol)
        
        return self.report_lines

    async def _prepare_market_data(self, portfolio_rows):
        """下載所有必要的行情資料。"""
        unique_symbols = sorted(list(set([row[0] for row in portfolio_rows])))
        all_targets = unique_symbols + ["SPY"]
        
        tasks = {sym: market_data_service.get_history_df(sym, "90d") for sym in all_targets}
        results = await asyncio.gather(*tasks.values())
        
        for sym, df in zip(tasks.keys(), results):
            if df.empty: continue
            if sym == "SPY":
                self.spy_hist = df
                self.spy_price = df['Close'].iloc[-1]
            else:
                self.stock_hist_map[sym] = df

    async def _process_symbol_positions(self, symbol, rows):
        """處理單一標下的所有持倉。"""
        try:
            stock_hist = self.stock_hist_map.get(symbol, pd.DataFrame())
            stock_info = await self._get_stock_info(symbol, stock_hist)
            current_stock_price = stock_info['price']
            dividend_yield = stock_info['dividend_yield']
            beta = stock_info['beta']
            
            option_chains_cache = {}

            for row in rows:
                _, opt_type, strike, expiry, entry_price, quantity, stock_cost, *_ = row
                
                if expiry not in option_chains_cache:
                    option_chains_cache[expiry] = await asyncio.to_thread(get_option_chain, symbol, expiry)
                
                calls, puts = option_chains_cache[expiry]
                chain_data = calls if opt_type == "call" else puts
                
                if chain_data.empty: continue
                contract = chain_data[chain_data['strike'] == strike]
                if contract.empty: continue
                
                current_price = contract['lastPrice'].iloc[0]
                iv = contract['impliedVolatility'].iloc[0]
                
                exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                t_years = max((exp_date - self.today).days, 1) / 365.0 
                
                greeks = calculate_greeks(opt_type, current_stock_price, strike, t_years, iv, dividend_yield)
                margin = calculate_option_margin(opt_type, strike, current_stock_price, current_price, quantity, stock_cost)
                self.total_margin_used += margin

                weight_factor = beta * (current_stock_price / self.spy_price)
                spx_weighted_delta = greeks['delta'] * quantity * 100 * weight_factor
                self.total_beta_delta += spx_weighted_delta
                
                # py_vollib theta is Annual. Convert to Daily.
                daily_theta = (greeks['theta'] * quantity * 100) / 365.0
                self.total_theta += daily_theta
                
                pos_gamma = greeks['gamma'] * quantity * 100
                spx_weighted_gamma = pos_gamma * (weight_factor ** 2)
                self.total_gamma += spx_weighted_gamma

                pnl_pct = (entry_price - current_price) / entry_price if quantity < 0 else (current_price - entry_price) / entry_price
                status = evaluate_defense_status_core(quantity, opt_type, pnl_pct, greeks['delta'], (exp_date - self.today).days)
                cc_tag = " 🛡️(CC)" if (opt_type == 'call' and stock_cost > 0.0) else ""
                
                self.report_lines.append(
                    format_position_report(symbol, expiry, strike, opt_type, cc_tag, 
                                           entry_price, current_price, pnl_pct, (exp_date - self.today).days, 
                                           spx_weighted_delta, status)
                )
        except Exception as e:
            logger.error(f"Symbol {symbol} 處理失敗: {e}", exc_info=True)

    async def _get_stock_info(self, symbol: str, stock_hist):
        """獲取標的價格、Beta 與股息率。"""
        try:
            quote = await market_data_service.get_quote(symbol)
            price = quote.get('c', 0.0) if quote else 0.0
            if price is None or price <= 0:
                price = stock_hist['Close'].iloc[-1] if not stock_hist.empty else 0.0
            
            is_etf_flag = await market_data_service.is_etf(symbol)
            if is_etf_flag:
                dividend_yield = 0.015
            else:
                dividend_yield = await market_data_service.get_dividend_yield(symbol)
            
            if not self.spy_hist.empty and not stock_hist.empty:
                beta_val = calculate_beta(stock_hist, self.spy_hist)
            else:
                beta_val = 1.0
                
        except Exception as e:
            price = stock_hist['Close'].iloc[-1] if not stock_hist.empty else 0.0
            dividend_yield, beta_val = 0.0, 1.0
        
        return {'price': price, 'dividend_yield': dividend_yield, 'beta': beta_val}

    async def _append_final_reports(self, positions_by_symbol):
        """追加宏觀風險與相關性報告。"""
        metrics = get_macro_risk_metrics_core(
            self.total_beta_delta, self.total_theta, self.total_margin_used, 
            self.total_gamma, self.user_capital, self.spy_price
        )
        self.report_lines.extend(format_macro_risk_report_core(metrics, self.spy_price))
        
        symbols = list(positions_by_symbol.keys())
        high_corr_pairs = await analyze_sector_correlation_core(symbols)
        self.report_lines.extend(format_correlation_report_core(high_corr_pairs, len(symbols)))

async def refresh_portfolio_greeks(user_id: int = None):
    """
    [Unified Asset Lifecycle] 重新整理 Assets 表中所有資產的希臘字母數據。
    包含：TRADE (期權) 與 HOLDING (現貨)。
    """
    try:
        from services.asset_manager import AssetManager
        from models.asset import ContextType, TradeMetadata, HoldingMetadata
        from py_vollib.black_scholes_merton.implied_volatility import implied_volatility
        import json
        
        manager = AssetManager()
        # 取得所有非 WATCH 的資產
        query = "SELECT * FROM assets WHERE context_type IN ('TRADE', 'HOLDING')"
        params = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
            
        assets_to_update = []
        unique_symbols = set()
        
        with manager._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                data = dict(row)
                data['metadata'] = json.loads(data['metadata']) if data['metadata'] else {}
                from models.asset import Asset
                asset = Asset(**data)
                assets_to_update.append(asset)
                unique_symbols.add(asset.symbol)
        
        if not unique_symbols: return
            
        spy_df = await market_data_service.get_history_df("SPY", "5d")
        spy_price = spy_df['Close'].iloc[-1] if not spy_df.empty else 670.0
        
        stock_data = {}
        for sym in unique_symbols:
            df = await market_data_service.get_history_df(sym, "5d")
            quote = await market_data_service.get_quote(sym)
            stock_data[sym] = {
                'price': quote.get('c', df['Close'].iloc[-1] if not df.empty else 0.0),
                'beta': calculate_beta(df, spy_df) if not df.empty and not spy_df.empty else 1.0,
                'div_yield': await market_data_service.get_dividend_yield(sym)
            }

        with manager._get_conn() as conn:
            cursor = conn.cursor()
            for asset in assets_to_update:
                s_info = stock_data.get(asset.symbol)
                if not s_info or s_info['price'] <= 0: continue

                weight_factor = s_info['beta'] * (s_info['price'] / spy_price)
                
                if asset.context_type == ContextType.TRADE:
                    meta = TradeMetadata(**asset.metadata)
                    mid, iv_raw = await asyncio.to_thread(get_option_chain_mid_iv, asset.symbol, meta.expiry, meta.strike, meta.opt_type)
                    
                    iv = iv_raw
                    if iv <= 0.001 and mid > 0:
                        try:
                            exp_date = datetime.strptime(meta.expiry, '%Y-%m-%d').date()
                            t_years = max((exp_date - datetime.now().date()).days, 1) / 365.0
                            from config import RISK_FREE_RATE
                            iv = implied_volatility(mid, s_info['price'], meta.strike, t_years, RISK_FREE_RATE, meta.opt_type[0])
                        except Exception: iv = iv_raw

                    if iv <= 0: continue
                    
                    t_years = max((datetime.strptime(meta.expiry, '%Y-%m-%d').date() - datetime.now().date()).days, 1) / 365.0
                    greeks = calculate_greeks(meta.opt_type, s_info['price'], meta.strike, t_years, iv, s_info['div_yield'])
                    
                    meta.weighted_delta = round(greeks['delta'] * meta.quantity * 100 * weight_factor, 4)
                    meta.theta = round(greeks['theta'] * meta.quantity * 100, 4)
                    meta.gamma = round(greeks['gamma'] * meta.quantity * 100 * (weight_factor**2), 6)
                    
                    cursor.execute("UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (meta.model_dump_json(), asset.id))

                elif asset.context_type == ContextType.HOLDING:
                    meta = HoldingMetadata(**asset.metadata)
                    # 現貨 Delta 為 1.0
                    meta.weighted_delta = round(1.0 * meta.quantity * weight_factor, 4)
                    cursor.execute("UPDATE assets SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (meta.model_dump_json(), asset.id))
            
            conn.commit()

    except Exception as e:
        logger.error(f"refresh_portfolio_greeks 失敗: {e}", exc_info=True)

def get_option_chain_mid_iv(symbol, expiry, strike, opt_type):
    try:
        calls, puts = get_option_chain(symbol, expiry)
        chain = calls if opt_type == 'call' else puts
        # 彈性匹配：尋找最接近的履約價 (防止浮點數誤差)
        contract = chain[(chain['strike'] - strike).abs() < 0.01]
        
        if not contract.empty:
            c = contract.iloc[0]
            bid = c.get('bid', 0.0)
            ask = c.get('ask', 0.0)
            last = c.get('lastPrice', 0.0)
            
            # 優先使用 Mid，若無報價使用 Last
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            iv = c.get('impliedVolatility', 0.0)
            return mid, iv
    except Exception as e:
        logger.debug(f"get_option_chain_mid_iv 異常: {e}")
    return 0.0, 0.0
