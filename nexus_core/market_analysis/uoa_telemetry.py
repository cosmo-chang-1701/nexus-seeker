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
    delta: float = 0.0
    dte: int = 0


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


# 履約價距現價不足此比例一律視為平價 (ATM)：末日／週選在這個距離內的 Delta 約
# 0.4~0.65，是高槓桿方向性博弈，不是鎖定高 Delta 的長線深價內吸籌。
UOA_ATM_BAND_PCT = 0.025
# 「深價內機構吸籌」要求的最低 |Delta|。
UOA_DEEP_ITM_MIN_DELTA = 0.80


def check_uoa_moneyness(
    is_call: bool,
    strike: float,
    current_price: float,
    delta: Optional[float] = None,
) -> str:
    """判定 UOA 合約的價內外屬性。

    回傳值：
      * ``ATM``：距現價 < UOA_ATM_BAND_PCT；
      * ``OTM_Speculation``：價外；
      * ``ITM_Whale_Accumulation``：深價內（|Δ| >= UOA_DEEP_ITM_MIN_DELTA；無 Delta
        可用時沿用舊定義，凡價內且落在 ATM 帶外即屬之）；
      * ``ITM_Directional``：價內但 |Δ| 未達深價內門檻——方向性押注，非吸籌。
    """
    if current_price <= 0 or strike == current_price:
        return "ATM"
    if abs(strike - current_price) / current_price < UOA_ATM_BAND_PCT:
        return "ATM"
    is_itm = strike < current_price if is_call else strike > current_price
    if not is_itm:
        return "OTM_Speculation"
    if delta is not None and delta != 0.0 and abs(delta) < UOA_DEEP_ITM_MIN_DELTA:
        return "ITM_Directional"
    return "ITM_Whale_Accumulation"


def classify_uoa_trade(
    trade: UOATradeInput,
    reference_date: Optional[Union[datetime, date, str]] = None,
    current_price: Optional[float] = None,
    delta: Optional[float] = None,
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

    # 規則 A (🟢 買入開倉 / Ask Side)
    if trade.trade_price >= trade.ask_price or (
        trade.trade_price > midpoint and trade.trade_price < trade.ask_price
    ):
        action = "🟢 買入開倉 (BTO - Ask)"
    # 規則 B (🔴 賣出開倉 / Bid Side)
    elif trade.trade_price <= trade.bid_price or (
        trade.trade_price < midpoint and trade.trade_price > trade.bid_price
    ):
        action = "🔴 賣出開倉 (STO - Bid)"
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

    # 4. 動態戰略意圖映射 (Dynamic Intent Mapping)
    # 所有文字動態綁定真實交易數據，禁止硬編碼罐頭字串
    opt_type_upper = trade.option_type.upper()
    ticker_tag = f"[{trade.symbol}] " if trade.symbol else ""
    volume_str = f"{trade.volume:,}"
    strike_str = f"${trade.strike_price:.2f}"
    oi_str = f"{trade.open_interest:,}"

    is_call = opt_type_upper == "CALL"

    # 判斷是否可用現價做 ITM/OTM 精確分類
    use_moneyness = current_price is not None and current_price > 0
    moneyness = "UNKNOWN"
    if use_moneyness and current_price is not None:
        moneyness = check_uoa_moneyness(
            is_call, trade.strike_price, current_price, delta
        )
    delta_tag = f", Δ={delta:+.2f}" if delta is not None and delta != 0.0 else ""

    if action == "🟢 買入開倉 (BTO - Ask)":
        if is_call:
            if use_moneyness and moneyness == "OTM_Speculation":
                intent = (
                    f"🔥 {ticker_tag}在 {strike_str} 買入 {volume_str} 口價外 CALL "
                    f"(DTE={dte}, OI={oi_str})，屬 OTM 投機性看漲買盤 (OTM_Speculation)"
                )
            elif use_moneyness and moneyness == "ITM_Whale_Accumulation":
                intent = (
                    f"🚀 {ticker_tag}大額資金在 {strike_str} 買入 {volume_str} 口深價內 CALL "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬 ITM 機構主力吸籌建倉 (ITM_Whale_Accumulation)"
                )
            elif use_moneyness and moneyness == "ATM":
                intent = (
                    f"🔥 {ticker_tag}在 {strike_str} 買入 {volume_str} 口平價 CALL "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬 ATM 高槓桿方向性看漲博弈"
                )
            elif use_moneyness and moneyness == "ITM_Directional":
                intent = (
                    f"🔥 {ticker_tag}在 {strike_str} 買入 {volume_str} 口淺價內 CALL "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬方向性看漲買盤（Delta 未達深價內吸籌門檻）"
                )
            else:
                if dte <= 3:
                    intent = (
                        f"🔥 {ticker_tag}機構在 {strike_str} 主動買入 {volume_str} 口"
                        f" 末日 CALL (DTE={dte}, OI={oi_str})，Gamma 逼空火力集中"
                    )
                else:
                    intent = (
                        f"🚀 {ticker_tag}大額資金在 {strike_str} 買入 {volume_str} 口"
                        f" 跨週 CALL (DTE={dte}, OI={oi_str})，機構主力吸籌建倉"
                    )
        else:  # PUT
            if use_moneyness and moneyness == "OTM_Speculation":
                intent = (
                    f"⚠️ {ticker_tag}在 {strike_str} 買入 {volume_str} 口價外 PUT "
                    f"(DTE={dte}, OI={oi_str})，屬 OTM 投機避險買盤 (OTM_Speculation)"
                )
            elif use_moneyness and moneyness == "ITM_Whale_Accumulation":
                intent = (
                    f"📉 {ticker_tag}大額資金在 {strike_str} 買入 {volume_str} 口深價內 PUT "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬 ITM 機構主力避險建倉 (ITM_Whale_Accumulation)"
                )
            elif use_moneyness and moneyness == "ATM":
                intent = (
                    f"⚠️ {ticker_tag}在 {strike_str} 買入 {volume_str} 口平價 PUT "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬 ATM 高槓桿方向性看跌博弈"
                )
            elif use_moneyness and moneyness == "ITM_Directional":
                intent = (
                    f"📉 {ticker_tag}在 {strike_str} 買入 {volume_str} 口淺價內 PUT "
                    f"(DTE={dte}, OI={oi_str}{delta_tag})，屬方向性看跌買盤（Delta 未達深價內門檻）"
                )
            else:
                if dte <= 3:
                    intent = (
                        f"⚠️ {ticker_tag}機構在 {strike_str} 急買 {volume_str} 口"
                        f" 末日 PUT (DTE={dte}, OI={oi_str})，恐慌性避險避雷"
                    )
                else:
                    intent = (
                        f"📉 {ticker_tag}空頭在 {strike_str} 主動買入 {volume_str} 口"
                        f" PUT (DTE={dte}, OI={oi_str})，加碼下行防護"
                    )

    elif action == "🔴 賣出開倉 (STO - Bid)":
        # 「物理天花板／地板」只適用於價外賣單：在現價下方賣出價內 CALL 是備兌
        # 鎖利或多頭平倉，不可能阻礙股價上漲；PUT 同理。
        is_itm_sto = use_moneyness and moneyness in (
            "ITM_Whale_Accumulation",
            "ITM_Directional",
        )
        is_atm_sto = use_moneyness and moneyness == "ATM"
        if is_call and is_itm_sto:
            intent = (
                f"🔒 {ticker_tag}在 {strike_str} 賣出 {volume_str} 口價內 CALL"
                f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，屬現貨多頭備兌鎖利／多方平倉，"
                "不構成上方天花板"
            )
        elif is_call and is_atm_sto:
            intent = (
                f"🛡️ {ticker_tag}在 {strike_str} 賣出 {volume_str} 口平價 CALL"
                f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，短線壓制現價附近波動，非遠端封頂"
            )
        elif not is_call and is_itm_sto:
            intent = (
                f"🔓 {ticker_tag}在 {strike_str} 賣出 {volume_str} 口價內 PUT"
                f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，屬空方平倉或合成部位，"
                "不構成下方支撐地板"
            )
        elif not is_call and is_atm_sto:
            intent = (
                f"🛡️ {ticker_tag}在 {strike_str} 賣出 {volume_str} 口平價 PUT"
                f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，短線承接現價附近賣壓，非遠端地板"
            )
        elif is_call:
            if ratio >= 2.0:
                intent = (
                    f"🛡️ {ticker_tag}機構在 {strike_str} 巨額開倉賣出 {volume_str} 口 CALL"
                    f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，機構高位 STO 築頂收租，物理鎖死上方天花板"
                )
            else:
                intent = (
                    f"🛡️ {ticker_tag}機構在 {strike_str} 開倉賣出 {volume_str} 口 CALL"
                    f" (OI={oi_str}, 佔比={ratio_str})，物理封頂鎖死上方天花板"
                )
        else:  # PUT
            if ratio >= 2.0:
                intent = (
                    f"🛡️ {ticker_tag}機構在 {strike_str} 巨額開倉賣出 {volume_str} 口 PUT"
                    f" (DTE={dte}, OI={oi_str}, 佔比={ratio_str})，機構低位 STO 築底收租，強力構築下行支撐地板"
                )
            else:
                intent = (
                    f"🛡️ {ticker_tag}機構在 {strike_str} 開倉賣出 {volume_str} 口 PUT"
                    f" (OI={oi_str}, 佔比={ratio_str})，強力構築下行支撐地板"
                )

    else:  # ⚖️ MIDPOINT (Cross)
        intent = (
            f"⚖️ {ticker_tag}大宗 Crossing 在 {strike_str} 對倒 {volume_str} 口"
            f" {opt_type_upper} (OI={oi_str})，中性策略組合或機構調倉"
        )

    # 5. Whale_Hedge 分類：買入深價內 Put (Delta < -0.65) 屬巨鯨避險部位，
    # 嚴禁計入多頭動能分數 (見 intraday_pipeline.py::evaluate_advanced_filters)
    if (
        not is_call
        and action == "🟢 買入開倉 (BTO - Ask)"
        and delta is not None
        and delta < -0.65
    ):
        intent += "｜Whale_Hedge (巨鯨避險)"

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
        delta=delta if delta is not None else 0.0,
        dte=dte,
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
        "權利金",
        "戰略意圖映射",
    ]
    header_alignments = ["left", "left", "left", "left", "left", "left", "left", "left"]
    col_alignments = ["left", "right", "left", "left", "left", "left", "right", "left"]
    min_widths = [10, 7, 4, 21, 8, 6, 8, 0]

    # 格式化每一行數據的儲存格
    rows_cells: List[List[str]] = []
    for trade in trades:
        notional_val = trade.trade_price * trade.volume * 100
        if notional_val >= 1_000_000:
            notional_str = f"${notional_val / 1_000_000:.2f}M"
        else:
            notional_str = f"${notional_val / 1_000:.1f}k"
        cells = [
            trade.expiry,
            f"${trade.strike_price:.1f}",
            trade.option_type.upper(),
            trade.action,
            f"+{trade.volume:,}",
            trade.ratio_str,
            notional_str,
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


# 兩腿成交量相差在此比例內才視為同一組價差（1:1 配對）。
SPREAD_VOLUME_RATIO_TOL = 0.25


def _leg_volume(entry: dict) -> float:
    try:
        return float(entry.get("volume", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _match_side(short_leg: dict, side: list[dict]) -> list[dict]:
    """在單側 (履約價較低或較高) 找出與賣出腿 1:1 對應的買入腿。

    先找成交量相當的單一腿（取最近履約價），找不到再由近而遠累加多腿，
    例如賣出 $1100 CALL 5.4 萬口對應 $1070~$1080 三檔合計 5.7 萬口。
    """
    vs = _leg_volume(short_leg)
    if vs <= 0 or not side:
        return []
    k = float(short_leg.get("strike", 0.0))
    by_distance = sorted(side, key=lambda e: abs(float(e.get("strike", 0.0)) - k))
    for leg in by_distance:
        if abs(_leg_volume(leg) / vs - 1.0) <= SPREAD_VOLUME_RATIO_TOL:
            return [leg]
    # 由近而遠的前綴中，取成交量最接近 1:1 且落在容差內者
    best: list[dict] = []
    best_err = SPREAD_VOLUME_RATIO_TOL
    total = 0.0
    for i, leg in enumerate(by_distance):
        total += _leg_volume(leg)
        err = abs(total / vs - 1.0)
        if err <= best_err:
            best, best_err = by_distance[: i + 1], err
        if total > vs * (1.0 + SPREAD_VOLUME_RATIO_TOL):
            break
    return best


def _strikes_label(legs: list[dict]) -> str:
    strikes = sorted(float(e.get("strike", 0.0)) for e in legs)
    if len(strikes) == 1:
        return f"${strikes[0]:g}"
    return f"${strikes[0]:g}~${strikes[-1]:g}"


def annotate_spread_structures(entries: list[dict]) -> None:
    """把同到期日、同類型、成交量 1:1 對應的 BTO／STO 腿標記為價差組合（就地修改）。

    逐合約分類會把「買 $1800C / 賣 $1850C」拆成一筆吸籌加一筆封頂，但兩腿合起來
    是一張牛市價差：賣出腿代表價差的獲利上限（多方目標價），不是機構獨立封頂。
    被配對的賣出腿標記 ``spread_role="SHORT_LEG"``，`detect_uoa_sto_call_physical_cap`
    據此不再把它當成物理封頂。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = (str(e.get("expiry", "")), str(e.get("type", "")).upper())
        groups.setdefault(key, []).append(e)

    for (_, opt_type), legs in groups.items():
        is_call = opt_type == "CALL"
        shorts = sorted(
            (e for e in legs if "STO" in str(e.get("action", ""))),
            key=_leg_volume,
            reverse=True,
        )
        for short_leg in shorts:
            longs = [
                e
                for e in legs
                if "BTO" in str(e.get("action", "")) and not e.get("spread_role")
            ]
            k = float(short_leg.get("strike", 0.0))
            below = _match_side(
                short_leg, [e for e in longs if float(e.get("strike", 0.0)) < k]
            )
            above = _match_side(
                short_leg, [e for e in longs if float(e.get("strike", 0.0)) > k]
            )
            if not below and not above:
                continue

            short_tag = f"${k:g}"
            if below and above:
                label = (
                    f"多腿組合（買 {_strikes_label(below)}、{_strikes_label(above)}"
                    f" ／賣 {short_tag} {opt_type}）"
                )
                matched = below + above
            elif below:
                # 買低賣高：CALL 為牛市借方價差；PUT 為牛市貸方價差
                name = (
                    "牛市價差 (Bull Call Spread)"
                    if is_call
                    else "牛市價差 (Bull Put Spread)"
                )
                label = f"{name} {_strikes_label(below)}/{short_tag}"
                matched = below
            else:
                name = (
                    "熊市價差 (Bear Call Spread)"
                    if is_call
                    else "熊市價差 (Bear Put Spread)"
                )
                label = f"{name} {short_tag}/{_strikes_label(above)}"
                matched = above

            short_leg["spread_role"] = "SHORT_LEG"
            short_leg["spread_label"] = label
            intent = str(short_leg.get("intent", ""))
            head = intent.rsplit("，", 1)[0] if "，" in intent else intent
            short_leg["intent"] = (
                f"{head}，🔗 屬{label}的賣出腿，代表價差獲利上限而非機構獨立封頂"
            )
            for leg in matched:
                leg["spread_role"] = "LONG_LEG"
                leg["spread_label"] = label
                leg["intent"] = f"{leg.get('intent', '')}｜🔗 屬{label}的買入腿"
