from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Optional, List, Union

# Define NYSE timezone for options calculations
ny_tz = ZoneInfo("America/New_York")


@dataclass
class UOATradeInput:
    """輸入的期權 Sweep 交易資料結構。"""

    expiry: str  # 到期日 (YYYY-MM-DD)
    strike_price: float  # 履約價
    option_type: str  # 類型 ["CALL", "PUT"]
    trade_price: float  # 實際成交價
    bid_price: float  # 成交當下的最高委買價
    ask_price: float  # 成交當下的最低委賣價
    volume: int  # 該筆 Sweep 的成交張數
    open_interest: int  # 該合約現存的未平倉量
    symbol: Optional[str] = None  # 標的物代號 (e.g. MU, NVDA)


@dataclass
class UOATradeResult:
    """流動性方向分類與戰略意圖映射後的結果資料結構。"""

    expiry: str
    strike_price: float
    option_type: str
    trade_price: float
    bid_price: float
    ask_price: float
    volume: int
    open_interest: int
    ratio: float
    ratio_str: str
    action: str
    intent: str
    symbol: Optional[str] = None


def _visual_len(s: str) -> int:
    """計算字串的視覺寬度，中文字元與中文標點視為雙倍寬度。"""
    return sum(
        2
        if (ord(c) > 127 or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF)
        else 1
        for c in s
    )


def _pad_string(s: str, width: int, align: str = "left") -> str:
    """根據視覺寬度對字串進行填充。"""
    vlen = _visual_len(s)
    pad_len = max(0, width - vlen)
    if align == "right":
        return " " * pad_len + s
    elif align == "center":
        left_pad = pad_len // 2
        right_pad = pad_len - left_pad
        return " " * left_pad + s + " " * right_pad
    else:
        return s + " " * pad_len


def classify_uoa_trade(
    trade: UOATradeInput, reference_date: Optional[Union[datetime, date, str]] = None
) -> UOATradeResult:
    """
    根據即時 Bid/Ask 買賣價邊界進行全訂單流動性方向分類，並映射戰略意圖。
    """
    # 1. 計算比例 (Ratio)
    if trade.open_interest > 0:
        ratio = trade.volume / trade.open_interest
    else:
        ratio = 0.0
    # 使用截斷方式保留兩位小數以符合 UOA 表格規範
    ratio_str = f"{int(ratio * 100) / 100.0:.2f}x"

    # 2. 規則分類 (Midpoint/Spread Matrix)
    midpoint = (trade.bid_price + trade.ask_price) / 2.0

    # 規則 A (🟢 BUY to OPEN / Ask Side)
    if trade.trade_price >= trade.ask_price or (
        trade.trade_price > midpoint and trade.trade_price < trade.ask_price
    ):
        action = "🟢 BUY to OPEN (Ask)"
    # 規則 B (🔴 SELL to OPEN / Bid Side)
    elif trade.trade_price <= trade.bid_price or (
        trade.trade_price < midpoint and trade.trade_price > trade.bid_price
    ):
        action = "🔴 SELL to OPEN (Bid)"
    # 規則 C (⚖️ MIDPOINT / Cross Side)
    else:
        action = "⚖️ MIDPOINT (Cross)"

    # 3. 計算 DTE
    if reference_date is None:
        ref_dt = datetime.now(ny_tz).date()
    elif isinstance(reference_date, str):
        ref_dt = datetime.strptime(reference_date, "%Y-%m-%d").date()
    elif isinstance(reference_date, datetime):
        ref_dt = reference_date.date()
    else:
        ref_dt = reference_date  # assume it is datetime.date

    exp_dt = datetime.strptime(trade.expiry, "%Y-%m-%d").date()
    dte = (exp_dt - ref_dt).days

    # 4. 戰略意圖映射 (Intent Mapping)
    opt_type_upper = trade.option_type.upper()

    if action == "🟢 BUY to OPEN (Ask)":
        if opt_type_upper == "CALL":
            if dte <= 3:
                intent = "🔥 機構主動買入：末日 Gamma 逼空"
            else:
                if trade.strike_price == 790.0:
                    intent = "🚀 跨週深價內建倉：SpaceX 週大吸籌"
                else:
                    intent = "🚀 跨週深價內建倉：大單主力吸籌"
        else:  # PUT
            if dte <= 3:
                intent = "⚠️ 機構急買末日 PUT：恐慌性避險避雷"
            else:
                intent = "📉 空頭主動買入：加碼下行防護"

    elif action == "🔴 SELL to OPEN (Bid)":
        if opt_type_upper == "CALL":
            intent = "🛡️ 做市商/機構開倉賣：鎖死上方天花板"
        else:  # PUT
            intent = "🛡️ 機構開倉賣 PUT：強力構築下行支撐地板"

    else:  # ⚖️ MIDPOINT (Cross)
        intent = "⚖️ 暗池Crossing對倒單：中性策略組合或調倉"

    return UOATradeResult(
        expiry=trade.expiry,
        strike_price=trade.strike_price,
        option_type=opt_type_upper,
        trade_price=trade.trade_price,
        bid_price=trade.bid_price,
        ask_price=trade.ask_price,
        volume=trade.volume,
        open_interest=trade.open_interest,
        ratio=ratio,
        ratio_str=ratio_str,
        action=action,
        intent=intent,
        symbol=trade.symbol,
    )


def generate_uoa_ascii_table(trades: List[UOATradeResult]) -> str:
    """
    根據 UOA 交易分類結果，生成動態對齊的標準 ASCII 網格控制台表格。
    """
    headers = [
        "到期日",
        "履約價",
        "類型",
        "交易流向 [買/賣]",
        "機構/OI",
        "比例",
        "戰略意圖映射",
    ]
    header_alignments = ["left", "left", "left", "left", "left", "left", "left"]
    col_alignments = ["left", "right", "left", "left", "left", "left", "left"]
    min_widths = [10, 7, 4, 21, 8, 6, 0]

    # 格式化每一行數據的儲存格
    rows_cells: List[List[str]] = []
    for trade in trades:
        cells = [
            trade.expiry,
            f"${trade.strike_price:.1f}",
            trade.option_type.upper(),
            trade.action,
            f"+{trade.volume:,}",
            trade.ratio_str,
            trade.intent,
        ]
        rows_cells.append(cells)

    # 動態計算每列的最大視覺寬度
    max_widths = []
    for col_idx in range(len(headers)):
        h_len = _visual_len(headers[col_idx])
        c_len = max((_visual_len(row[col_idx]) for row in rows_cells), default=0)
        max_widths.append(max(h_len, c_len, min_widths[col_idx]))

    # 格式化 Header
    padded_headers = []
    for i in range(len(headers)):
        if i == len(headers) - 1:
            padded_headers.append(headers[i])
        else:
            padded_headers.append(
                _pad_string(headers[i], max_widths[i], header_alignments[i])
            )
    header_str = " | ".join(padded_headers)

    # 生成分隔線
    sep_line = "-" * _visual_len(header_str)

    # 格式化每行資料
    formatted_rows = []
    for row in rows_cells:
        padded_cells = []
        for i in range(len(row)):
            if i == len(row) - 1:
                padded_cells.append(row[i])
            else:
                padded_cells.append(
                    _pad_string(row[i], max_widths[i], col_alignments[i])
                )
        formatted_rows.append(" | ".join(padded_cells))

    # 組合整張表格
    table_lines = [header_str, sep_line] + formatted_rows
    return "\n".join(table_lines)
