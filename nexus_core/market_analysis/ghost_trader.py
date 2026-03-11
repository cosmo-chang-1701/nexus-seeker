import logging
import math
import datetime
import yfinance as yf
import pandas as pd
import asyncio
from py_vollib.black_scholes_merton.greeks.analytical import delta

from config import RISK_FREE_RATE
from services import market_data_service
from database.virtual_trading import add_virtual_trade, get_all_open_virtual_trades, close_virtual_trade, get_all_virtual_trades

logger = logging.getLogger(__name__)

class GhostTrader:
    """虛擬交易室核心邏輯 (Async)"""
    def __init__(self):
        self.today = datetime.datetime.now().date()

    async def get_option_mid_price(self, symbol: str, opt_type: str, strike: float, expiry: str):
        """獲取特定期權合約的 Mid 價格 (用 to_thread)"""
        try:
            ticker = yf.Ticker(symbol)
            chain = await asyncio.to_thread(ticker.option_chain, expiry)
            opts = chain.calls if opt_type == 'call' else chain.puts
            contract = opts[opts['strike'] == strike]
            if contract.empty:
                return None, None
            
            bid, ask, last, iv = contract['bid'].iloc[0], contract['ask'].iloc[0], contract['lastPrice'].iloc[0], contract['impliedVolatility'].iloc[0]
            if pd.isna(bid) or pd.isna(ask) or bid == 0 or ask == 0:
                mid = last
            else:
                mid = (bid + ask) / 2.0
            return mid, iv
        except Exception as e:
            logger.error(f"GhostTrader 獲取 {symbol} 期權價格失敗: {e}")
            return None, None

    async def record_virtual_entry(self, user_id: int, symbol: str, opt_type: str, strike: float, expiry: str, quantity: int, weighted_delta: float = 0.0, theta: float = 0.0, gamma: float = 0.0, tags: list = None, parent_trade_id: int = None, trade_category: str = 'SPECULATIVE'):
        """自動建倉"""
        mid, _ = await self.get_option_mid_price(symbol, opt_type, strike, expiry)
        if mid is None:
            logger.warning(f"VTR 建倉失敗：找不到 {symbol} {expiry} {strike} {opt_type} 的報價")
            return None
            
        slippage_factor = 1.01 if quantity > 0 else 0.99
        entry_price = mid * slippage_factor
        
        trade_id = await asyncio.to_thread(
            add_virtual_trade, user_id=user_id, symbol=symbol, opt_type=opt_type, strike=strike, expiry=expiry, entry_price=entry_price, quantity=quantity, weighted_delta=weighted_delta, theta=theta, gamma=gamma, tags=tags, parent_trade_id=parent_trade_id, trade_category=trade_category
        )
        logger.info(f"🟢 VTR 自動建倉成功 [{trade_id}]: {symbol} {opt_type} {strike} {expiry} QTY:{quantity} Entry:{entry_price:.2f}")
        return trade_id

    async def manage_virtual_positions(self):
        """自動平倉邏輯 (Async)"""
        open_trades = await asyncio.to_thread(get_all_open_virtual_trades)
        for trade in open_trades:
            await self._check_and_exit_trade(trade)

    async def _check_and_exit_trade(self, trade):
        trade_id, symbol, opt_type, strike, expiry, entry_price, quantity = trade['id'], trade['symbol'], trade['opt_type'], trade['strike'], trade['expiry'], trade['entry_price'], trade['quantity']
        exp_date = datetime.datetime.strptime(expiry, '%Y-%m-%d').date()
        dte = (exp_date - self.today).days
        
        if dte <= 21:
            await self._close_position(trade, "DTE <= 21")
            return

        mid, _ = await self.get_option_mid_price(symbol, opt_type, strike, expiry)
        if mid is None: return
            
        if quantity < 0:
            pnl_pct = (entry_price - mid) / entry_price
            if pnl_pct >= 0.50: await self._close_position(trade, "Seller Target Reached (>=50%)", mid)
            elif pnl_pct <= -1.50: await self._close_position(trade, "Seller Stop Loss (>=150%)", mid)
        elif quantity > 0:
            ema21 = await market_data_service.get_ema(symbol, 21)
            quote = await market_data_service.get_quote(symbol)
            current_price = quote.get('c') if quote else None
            
            if ema21 is not None and current_price is not None:
                if (opt_type == 'call' and current_price < ema21) or (opt_type == 'put' and current_price > ema21):
                    await self._close_position(trade, f"🚨 動能平倉警報 ｜ 價格{'跌破' if opt_type=='call' else '突破'} EMA 21", mid)
                    return

            pnl_pct = (mid - entry_price) / entry_price
            if pnl_pct >= 1.00: await self._close_position(trade, "Buyer Target Reached (>=100%)", mid)
            elif pnl_pct <= -0.50: await self._close_position(trade, "Buyer Stop Loss (>=50%)", mid)

    async def _close_position(self, trade, reason: str, exit_price: float = None, status: str = 'CLOSED'):
        """執行平倉"""
        if exit_price is None:
            exit_price, _ = await self.get_option_mid_price(trade['symbol'], trade['opt_type'], trade['strike'], trade['expiry'])
            if exit_price is None: return
                
        actual_exit_price = exit_price * (0.99 if trade['quantity'] > 0 else 1.01)
        pnl = (actual_exit_price - trade['entry_price']) * trade['quantity'] * 100
        success = await asyncio.to_thread(close_virtual_trade, trade['id'], actual_exit_price, status=status, pnl=pnl)
        if success:
            logger.info(f"🔴 VTR 自動平倉 [{trade['id']}] {trade['symbol']} {trade['opt_type']} {trade['strike']} 原因:{reason} Exit:{actual_exit_price:.2f}")

    async def execute_virtual_roll(self):
        """自動轉倉邏輯 (Async)"""
        open_trades = await asyncio.to_thread(get_all_open_virtual_trades)
        for trade in open_trades:
            if trade['quantity'] >= 0: continue
            symbol, opt_type, strike, expiry = trade['symbol'], trade['opt_type'], trade['strike'], trade['expiry']
            
            quote = await market_data_service.get_quote(symbol)
            if not quote or 'c' not in quote: continue
            current_stock_price = quote['c']
            
            mid, iv = await self.get_option_mid_price(symbol, opt_type, strike, expiry)
            if mid is None or iv is None or iv <= 0: continue
                
            exp_date = datetime.datetime.strptime(expiry, '%Y-%m-%d').date()
            t_years = max((exp_date - self.today).days, 1) / 365.0
            
            try:
                opt_delta = delta(('c' if opt_type == 'call' else 'p'), current_stock_price, strike, t_years, RISK_FREE_RATE, iv, 0.0)
                if abs(opt_delta) >= 0.40:
                    logger.info(f"🔄 VTR 觸發自動轉倉 [{trade['id']}] {symbol} Delta: {opt_delta:.2f}")
                    await self._roll_position(trade, current_stock_price)
            except Exception:
                continue

    async def _roll_position(self, old_trade, current_stock_price):
        """平掉舊部位，尋找並開啟新部位 (Async)"""
        await self._close_position(old_trade, reason="Auto-Roll (Delta >= 0.40)", exit_price=None, status='ROLLED')
        new_contract = await self._find_target_contract(old_trade['symbol'], old_trade['opt_type'], current_stock_price, target_dte=(30, 45), target_delta=0.20)
        
        if new_contract:
            await self.record_virtual_entry(
                user_id=old_trade['user_id'], symbol=old_trade['symbol'], opt_type=old_trade['opt_type'], strike=new_contract['strike'], expiry=new_contract['expiry'], quantity=old_trade['quantity'], tags=["rolled_from:" + str(old_trade['id'])], parent_trade_id=old_trade['id']
            )

    async def _find_target_contract(self, symbol, opt_type, current_stock_price, target_dte=(30, 45), target_delta=0.20):
        """尋找符合 DTE 與 Delta 條件的合約 (Async)"""
        ticker = yf.Ticker(symbol)
        expirations = await asyncio.to_thread(lambda: ticker.options)
        
        valid_expiries = []
        for exp in expirations:
            dte = (datetime.datetime.strptime(exp, '%Y-%m-%d').date() - self.today).days
            if target_dte[0] <= dte <= target_dte[1]:
                valid_expiries.append((exp, dte))
        if not valid_expiries: return None
            
        best_exp = min(valid_expiries, key=lambda x: abs(x[1] - (sum(target_dte) / 2.0)))[0]
        chain = await asyncio.to_thread(ticker.option_chain, best_exp)
        opts = chain.calls if opt_type == 'call' else chain.puts
        
        best_strike, min_diff = None, 999
        t_years = max((datetime.datetime.strptime(best_exp, '%Y-%m-%d').date() - self.today).days, 1) / 365.0
        flag = 'c' if opt_type == 'call' else 'p'
        
        for _, row in opts.iterrows():
            strike, iv = row['strike'], row['impliedVolatility']
            if iv <= 0: continue
            try:
                opt_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, 0.0)
                diff = abs(abs(opt_delta) - target_delta)
                if diff < min_diff:
                    min_diff, best_strike = diff, strike
            except: pass
                
        if best_strike: return {'expiry': best_exp, 'strike': best_strike}
        return None

    @staticmethod
    async def get_vtr_performance_stats(user_id: int) -> dict:
        """計算虛擬交易室的績效指標 (Async)"""
        trades = await asyncio.to_thread(get_all_virtual_trades, user_id)
        if not trades: return {'total_trades': 0, 'win_rate': 0.0, 'total_pnl': 0.0, 'avg_pnl': 0.0}
        completed_trades = [t for t in trades if t['status'] in ['CLOSED', 'ROLLED']]
        if not completed_trades: return {'total_trades': 0, 'win_rate': 0.0, 'total_pnl': 0.0, 'avg_pnl': 0.0}
        wins = [t for t in completed_trades if t['pnl'] > 0]
        total_pnl = sum(t['pnl'] for t in completed_trades)
        return {'total_trades': len(completed_trades), 'win_rate': round((len(wins) / len(completed_trades)) * 100, 2), 'total_pnl': round(total_pnl, 2), 'avg_pnl': round(total_pnl / len(completed_trades), 2)}