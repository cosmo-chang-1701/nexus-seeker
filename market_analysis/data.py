import yfinance as yf
from datetime import datetime

def get_next_earnings_date(ticker):
    """
    取得下一次財報發布日期。

    Args:
        ticker (yf.Ticker): yfinance Ticker 物件。

    Returns:
        datetime.date or None: 下一次財報日期，若無資料則回傳 None。
    """
    try:
        # 避免重複建立 ticker 物件，直接使用傳入的實例
        cal = ticker.calendar
        if cal is not None and not cal.empty and 'Earnings Date' in cal:
            earning_dates = cal['Earnings Date']
            if len(earning_dates) > 0:
                next_date = earning_dates[0]
                return next_date.date() if hasattr(next_date, 'date') else next_date
    except Exception:
        pass
    return None
