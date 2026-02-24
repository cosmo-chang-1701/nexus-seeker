import logging
from datetime import date

logger = logging.getLogger(__name__)


def get_next_earnings_date(ticker):
    """
    取得下一次財報發布日期。

    Args:
        ticker: yfinance Ticker 物件。

    Returns:
        datetime.date or None: 下一次財報日期，若無資料則回傳 None。
    """
    try:
        cal = ticker.calendar

        # yfinance 不同版本回傳 DataFrame 或 dict，分開處理
        if cal is None:
            return None

        if isinstance(cal, dict):
            earning_dates = cal.get('Earnings Date', [])
        else:
            # DataFrame 路徑
            if cal.empty or 'Earnings Date' not in cal:
                return None
            earning_dates = cal['Earnings Date']

        if len(earning_dates) == 0:
            return None

        today = date.today()
        for d in earning_dates:
            d_date = d.date() if hasattr(d, 'date') else d
            if d_date >= today:
                return d_date

        # 所有日期皆已過期，仍回傳最近一筆供呼叫端參考
        last = earning_dates[-1] if hasattr(earning_dates, '__getitem__') else list(earning_dates)[-1]
        return last.date() if hasattr(last, 'date') else last

    except Exception as e:
        logger.warning("取得財報日期失敗: %s", e)
        return None
