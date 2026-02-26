import logging
import math
import datetime
import yfinance as yf
import pandas as pd
from py_vollib.black_scholes_merton.greeks.analytical import delta

from config import RISK_FREE_RATE
from services import market_data_service
from database.virtual_trading import add_virtual_trade, get_all_open_virtual_trades, close_virtual_trade, get_all_virtual_trades

logger = logging.getLogger(__name__)

class GhostTrader:
    """虛擬交易室核心邏輯"""
    def __init__(self):
        self.today = datetime.datetime.now().date()

    def get_option_mid_price(self, symbol: str, opt_type: str, strike: float, expiry: str):
        """獲取特定期權合約的 Mid 價格"""
        try:
            ticker = yf.Ticker(symbol)
            chain = ticker.option_chain(expiry)
            opts = chain.calls if opt_type == 'call' else chain.puts
            contract = opts[opts['strike'] == strike]
            if contract.empty:
                return None, None
            
            bid = contract['bid'].iloc[0]
            ask = contract['ask'].iloc[0]
            last = contract['lastPrice'].iloc[0]
            iv = contract['impliedVolatility'].iloc[0]
            
            # 若 bid/ask 異常，以 last_price 回退
            if pd.isna(bid) or pd.isna(ask) or bid == 0 or ask == 0:
                mid = last
            else:
                mid = (bid + ask) / 2.0
                
            return mid, iv
        except Exception as e:
            logger.error(f"GhostTrader 獲取 {symbol} 期權價格失敗: {e}")
            return None, None

    def record_virtual_entry(self, user_id: int, symbol: str, opt_type: str, strike: float, expiry: str, quantity: int, tags: list = None, parent_trade_id: int = None):
        """自動建倉：以當前 Mid 價格（考慮 1% 滑點）寫入 virtual_trades"""
        mid, _ = self.get_option_mid_price(symbol, opt_type, strike, expiry)
        if mid is None:
            logger.warning(f"VTR 建倉失敗：找不到 {symbol} {expiry} {strike} {opt_type} 的報價")
            return None
            
        # 考慮 1% 滑點
        # 買方(quantity > 0)會買貴1%，賣方(quantity < 0)會賣便宜1%
        slippage_factor = 1.01 if quantity > 0 else 0.99
        entry_price = mid * slippage_factor
        
        trade_id = add_virtual_trade(
            user_id=user_id,
            symbol=symbol,
            opt_type=opt_type,
            strike=strike,
            expiry=expiry,
            entry_price=entry_price,
            quantity=quantity,
            tags=tags,
            parent_trade_id=parent_trade_id
        )
        logger.info(f"🟢 VTR 自動建倉成功 [{trade_id}]: {symbol} {opt_type} {strike} {expiry} QTY:{quantity} Entry:{entry_price:.2f}")
        return trade_id

    def manage_virtual_positions(self):
        """自動平倉邏輯"""
        open_trades = get_all_open_virtual_trades()
        for trade in open_trades:
            self._check_and_exit_trade(trade)

    def _check_and_exit_trade(self, trade):
        trade_id = trade['id']
        symbol = trade['symbol']
        opt_type = trade['opt_type']
        strike = trade['strike']
        expiry = trade['expiry']
        entry_price = trade['entry_price']
        quantity = trade['quantity']
        
        # 1. 計算 DTE
        exp_date = datetime.datetime.strptime(expiry, '%Y-%m-%d').date()
        dte = (exp_date - self.today).days
        
        # 若 DTE <= 21，自動平倉
        if dte <= 21:
            self._close_position(trade, "DTE <= 21")
            return

        # 2. 獲取報價計算 PnL
        mid, _ = self.get_option_mid_price(symbol, opt_type, strike, expiry)
        if mid is None:
            return
            
        # 計算 PnL 百分比
        if quantity < 0:
            # 賣方：獲利為正值，虧損為負值。(例如賣出5.0，現價2.0，獲利 3/5 = 60%)
            # (entry - current) / entry
            pnl_pct = (entry_price - mid) / entry_price
            
            if pnl_pct >= 0.50:
                self._close_position(trade, "Seller Target Reached (>=50%)", mid)
            elif pnl_pct <= -1.50:
                self._close_position(trade, "Seller Stop Loss (>=150%)", mid)
                
        elif quantity > 0:
            # 買方：獲利為正值，虧損為負值。(例如買入2.0，現價4.0，獲利 2/2 = 100%)
            # (current - entry) / entry
            pnl_pct = (mid - entry_price) / entry_price
            
            if pnl_pct >= 1.00:
                self._close_position(trade, "Buyer Target Reached (>=100%)", mid)
            elif pnl_pct <= -0.50:
                self._close_position(trade, "Buyer Stop Loss (>=50%)", mid)

    def _close_position(self, trade, reason: str, exit_price: float = None, status: str = 'CLOSED'):
        """執行平倉"""
        if exit_price is None:
            exit_price, _ = self.get_option_mid_price(trade['symbol'], trade['opt_type'], trade['strike'], trade['expiry'])
            if exit_price is None:
                # 拿不到報價，直接放棄此次平倉
                return
                
        # 考慮滑點: 買方平倉是賣出(低賣 1%)，賣方平倉是買回(高買 1%)
        slippage_factor = 0.99 if trade['quantity'] > 0 else 1.01
        actual_exit_price = exit_price * slippage_factor
        
        # 計算 PnL (關注點分離: 移至業務邏輯層)
        pnl = (actual_exit_price - trade['entry_price']) * trade['quantity'] * 100
        
        success = close_virtual_trade(trade['id'], actual_exit_price, status=status, pnl=pnl)
        if success:
            logger.info(f"🔴 VTR 自動平倉 [{trade['id']}] {trade['symbol']} {trade['opt_type']} {trade['strike']} 原因:{reason} Exit:{actual_exit_price:.2f}")

    def execute_virtual_roll(self):
        """自動轉倉邏輯：賣方 Delta 擴張至 ±0.40 時，平舊開新 (30-45 DTE, Delta 0.20)"""
        open_trades = get_all_open_virtual_trades()
        for trade in open_trades:
            # 只有賣方執行轉倉
            if trade['quantity'] >= 0:
                continue
                
            symbol = trade['symbol']
            opt_type = trade['opt_type']
            strike = trade['strike']
            expiry = trade['expiry']
            
            # 獲取標的現價
            stock_info = market_data_service.get_quote(symbol)
            if not stock_info or 'c' not in stock_info:
                continue
            current_stock_price = stock_info['c']
            
            # 獲取期權 IV
            mid, iv = self.get_option_mid_price(symbol, opt_type, strike, expiry)
            if mid is None or iv is None or iv <= 0:
                continue
                
            # 計算 Delta
            exp_date = datetime.datetime.strptime(expiry, '%Y-%m-%d').date()
            dte = (exp_date - self.today).days
            t_years = max(dte, 1) / 365.0
            
            try:
                # 這裡不考慮股息，因為是概算
                flag = 'c' if opt_type == 'call' else 'p'
                opt_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, 0.0)
            except Exception:
                continue
                
            # 檢查是否擴張至 0.40
            if abs(opt_delta) >= 0.40:
                logger.info(f"🔄 VTR 觸發自動轉倉 [{trade['id']}] {symbol} Delta: {opt_delta:.2f}")
                self._roll_position(trade, current_stock_price)

    def _roll_position(self, old_trade, current_stock_price):
        """平掉舊部位，尋找並開啟新部位"""
        symbol = old_trade['symbol']
        opt_type = old_trade['opt_type']
        
        # 1. 平倉舊部位
        self._close_position(old_trade, reason="Auto-Roll (Delta >= 0.40)", exit_price=None, status='ROLLED')
        
        # 更新舊部位狀態為 ROLLED
        # 但 _close_position 固定寫死 CLOSED，這裡我們直接用 SQL 更新或是在 close_virtual_trade 傳狀態。
        # 因為前面寫死了，我就手動再 UPDATE 狀態吧，或者去改 source...
        # 為了簡化，我在前面已將 close_virtual_trade 加上 status 參數。
        # let's call with status='ROLLED' if possible. Oops, _close_position doesn't pass status.
        # I'll just change _close_position to accept status and pass it to close_virtual_trade.

        # 2. 尋找新合約: 30-45 DTE, Delta ~ 0.20
        new_contract = self._find_target_contract(symbol, opt_type, current_stock_price, target_dte=(30, 45), target_delta=0.20)
        
        if new_contract:
            # 3. 建倉新部位
            self.record_virtual_entry(
                user_id=old_trade['user_id'],
                symbol=symbol,
                opt_type=opt_type,
                strike=new_contract['strike'],
                expiry=new_contract['expiry'],
                quantity=old_trade['quantity'], # 維持相同的賣方數量
                tags=["rolled_from:" + str(old_trade['id'])],
                parent_trade_id=old_trade['id']
            )

    def _find_target_contract(self, symbol, opt_type, current_stock_price, target_dte=(30, 45), target_delta=0.20):
        """尋找符合 DTE 與 Delta 條件的合約"""
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        
        valid_expiries = []
        for exp in expirations:
            exp_date = datetime.datetime.strptime(exp, '%Y-%m-%d').date()
            dte = (exp_date - self.today).days
            if target_dte[0] <= dte <= target_dte[1]:
                valid_expiries.append((exp, dte))
                
        if not valid_expiries:
            return None
            
        # 偏好最靠近 target_dte 平均值的日期
        target_mid = sum(target_dte) / 2.0
        best_exp = min(valid_expiries, key=lambda x: abs(x[1] - target_mid))[0]
        
        chain = ticker.option_chain(best_exp)
        opts = chain.calls if opt_type == 'call' else chain.puts
        
        # 我們需要找 delta 最接近 0.20 的。因為 yfinance 不直接提供 delta，需要自行算或從 impliedVolatility 估計。
        # 為了簡單，我們可以遍歷計算每個履約價的 Delta
        best_strike = None
        min_diff = 999
        
        t_years = max((datetime.datetime.strptime(best_exp, '%Y-%m-%d').date() - self.today).days, 1) / 365.0
        flag = 'c' if opt_type == 'call' else 'p'
        
        for _, row in opts.iterrows():
            strike = row['strike']
            iv = row['impliedVolatility']
            if iv <= 0: continue
            
            try:
                opt_delta = delta(flag, current_stock_price, strike, t_years, RISK_FREE_RATE, iv, 0.0)
                diff = abs(abs(opt_delta) - target_delta)
                if diff < min_diff:
                    min_diff = diff
                    best_strike = strike
            except:
                pass
                
        if best_strike:
            return {'expiry': best_exp, 'strike': best_strike}
        return None

    @staticmethod
    def get_vtr_performance_stats(user_id: int) -> dict:
        """
        計算虛擬交易室的績效指標
        """
        trades = get_all_virtual_trades(user_id)
        
        if not trades:
            return {
                'total_trades': 0, 'win_rate': 0.0, 
                'total_pnl': 0.0, 'avg_pnl': 0.0
            }

        # 1. 僅統計已結算或已轉倉的部位 (排除 OPEN)
        completed_trades = [t for t in trades if t['status'] in ['CLOSED', 'ROLLED']]
        
        if not completed_trades:
            return {'total_trades': 0, 'win_rate': 0.0, 'total_pnl': 0.0, 'avg_pnl': 0.0}

        # 2. 計算勝率 (PnL > 0 為贏)
        wins = [t for t in completed_trades if t['pnl'] > 0]
        total_pnl = sum(t['pnl'] for t in completed_trades)
        
        return {
            'total_trades': len(completed_trades),
            'win_rate': round((len(wins) / len(completed_trades)) * 100, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl': round(total_pnl / len(completed_trades), 2)
        }