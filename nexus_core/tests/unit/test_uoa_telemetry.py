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
    # Dynamic intent now includes ticker, strike, volume, OI, DTE
    assert "🔥" in result.intent
    assert "[MU]" in result.intent
    assert "$1050.00" in result.intent
    assert "22,348" in result.intent
    assert "Gamma" in result.intent


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
    # Dynamic intent now includes ticker, strike, volume, OI
    assert "🛡️" in result.intent
    assert "[NVDA]" in result.intent
    assert "$220.00" in result.intent
    assert "15,000" in result.intent
    assert "天花板" in result.intent


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
    # Dynamic intent: no more hardcoded SpaceX string, now uses data binding
    assert "🚀" in r2.intent
    assert "[SPACEX]" in r2.intent
    assert "$790.00" in r2.intent
    assert "13,741" in r2.intent

    assert r3.action == "🔴 SELL to OPEN (Bid)"
    assert "🛡️" in r3.intent
    assert "[NVDA]" in r3.intent
    assert "$1100.00" in r3.intent

    table = generate_uoa_ascii_table([r1, r2, r3])
    print("\n" + table)

    # 驗證輸出的表格列結構
    lines = table.split("\n")
    assert len(lines) == 5  # header, sep, 3 data rows
    # Verify structural presence of key data in each row
    assert "2026-06-05" in lines[2] and "$1050.0" in lines[2] and "🟢" in lines[2]
    assert "2026-06-12" in lines[3] and "$790.0" in lines[3] and "🟢" in lines[3]
    assert "2026-06-12" in lines[4] and "$1100.0" in lines[4] and "🔴" in lines[4]
