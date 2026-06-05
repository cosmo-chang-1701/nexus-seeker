from market_analysis.uoa_telemetry import (
    UOATradeInput,
    classify_uoa_trade,
    generate_uoa_ascii_table,
)


def test_classify_uoa_trade_bto_mu():
    """測試案例 1：激進買入 (MU $1050 Call)"""
    trade = UOATradeInput(
        strike_price=1050.0,
        option_type="CALL",
        trade_price=12.50,
        bid_price=12.00,
        ask_price=12.45,
        volume=22348,
        open_interest=4331,
        expiry="2026-06-05",
        symbol="MU",
    )
    # reference_date 為 2026-06-05，DTE = 0 <= 3
    result = classify_uoa_trade(trade, reference_date="2026-06-05")

    assert result.action == "🟢 BUY to OPEN (Ask)"
    assert result.ratio_str == "5.16x"
    assert result.intent == "🔥 機構主動買入：末日 Gamma 逼空"


def test_classify_uoa_trade_sto_nvda():
    """測試案例 2：波動率賣出/壓制 (NVDA $220 Call)"""
    trade = UOATradeInput(
        strike_price=220.0,
        option_type="CALL",
        trade_price=1.10,
        bid_price=1.15,
        ask_price=1.30,
        volume=15000,
        open_interest=2419,
        expiry="2026-06-12",
        symbol="NVDA",
    )
    # reference_date 為 2026-06-05，DTE = 7 > 3
    result = classify_uoa_trade(trade, reference_date="2026-06-05")

    assert result.action == "🔴 SELL to OPEN (Bid)"
    assert result.ratio_str == "6.20x"
    assert result.intent == "🛡️ 做市商/機構開倉賣：鎖死上方天花板"


def test_spacex_intent_and_ascii_table():
    """測試 2026-06-12 $790 Call BTO (SpaceX 週大吸籌) 及 ASCII 表格生成與對齊"""
    trade1 = UOATradeInput(
        strike_price=1050.0,
        option_type="CALL",
        trade_price=12.50,
        bid_price=12.00,
        ask_price=12.45,
        volume=22348,
        open_interest=4331,
        expiry="2026-06-05",
        symbol="MU",
    )
    trade2 = UOATradeInput(
        strike_price=790.0,
        option_type="CALL",
        trade_price=12.50,
        bid_price=12.00,
        ask_price=12.45,
        volume=13741,
        open_interest=693,
        expiry="2026-06-12",
        symbol="SPACEX",
    )
    trade3 = UOATradeInput(
        strike_price=1100.0,
        option_type="CALL",
        trade_price=1.10,
        bid_price=1.15,
        ask_price=1.30,
        volume=15000,
        open_interest=2419,
        expiry="2026-06-12",
        symbol="NVDA",
    )

    r1 = classify_uoa_trade(trade1, reference_date="2026-06-05")
    r2 = classify_uoa_trade(trade2, reference_date="2026-06-05")
    r3 = classify_uoa_trade(trade3, reference_date="2026-06-05")

    assert r2.action == "🟢 BUY to OPEN (Ask)"
    assert r2.ratio_str == "19.82x"
    assert r2.intent == "🚀 跨週深價內建倉：SpaceX 週大吸籌"

    assert r3.action == "🔴 SELL to OPEN (Bid)"
    assert r3.intent == "🛡️ 做市商/機構開倉賣：鎖死上方天花板"

    table = generate_uoa_ascii_table([r1, r2, r3])
    print("\n" + table)

    # 驗證輸出的表格列
    lines = table.split("\n")
    assert len(lines) == 5  # header, sep, 3 data rows
    assert (
        "2026-06-05 | $1050.0 | CALL | 🟢 BUY to OPEN (Ask)  | +22,348  | 5.16x  | 🔥 機構主動買入：末日 Gamma 逼空"
        in lines[2]
    )
    assert (
        "2026-06-12 |  $790.0 | CALL | 🟢 BUY to OPEN (Ask)  | +13,741  | 19.82x | 🚀 跨週深價內建倉：SpaceX 週大吸籌"
        in lines[3]
    )
    assert (
        "2026-06-12 | $1100.0 | CALL | 🔴 SELL to OPEN (Bid) | +15,000  | 6.20x  | 🛡️ 做市商/機構開倉賣：鎖死上方天花板"
        in lines[4]
    )
