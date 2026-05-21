from cogs.embed_builder import (
    create_holdings_embed,
    create_trades_embed,
    create_portfolio_report_embed,
    build_vtr_stats_embed,
    build_scan_report,
    create_ddp_embed,
    create_volatility_embed,
)


def test_create_holdings_embed():
    holdings_data = [
        {
            "symbol": "AAPL",
            "quantity": 10,
            "avg_cost": 150.0,
            "current_price": 160.0,
        }
    ]
    embed = create_holdings_embed(holdings_data, total_capital=100000.0)
    assert embed.title == "📊 Nexus Seeker | 現貨持倉清單"

    # Extract lines in code block
    desc_field = embed.fields[0].value
    assert "標的" in desc_field
    assert "現價" in desc_field
    assert "AAPL" in desc_field
    assert "$160.00" in desc_field


def test_create_trades_embed():
    pnl_data = {
        "trades": [
            {
                "id": 1,
                "symbol": "AAPL",
                "opt_type": "call",
                "strike": 150.0,
                "expiry": "2026-06-19",
                "quantity": 1,
                "entry_price": 5.0,
                "current_price": 6.50,
                "unrealized_pnl": 150.0,
                "pnl_pct": 0.3,
            }
        ],
        "total_unrealized_pnl": 150.0,
    }
    embed = create_trades_embed(pnl_data, total_capital=100000.0)
    assert embed.title == "📊 Nexus Seeker | 實單持倉清單 (包含帳面損益)"

    desc_field = embed.fields[0].value
    assert "現價" in desc_field
    assert "  6.50" in desc_field  # Visual formatting check


def test_create_portfolio_report_embed():
    report_lines = [
        "🔹 **AAPL** ｜ `2026-06-19` ｜ `$150.0` **CALL**\n├─ 💰 成本: `$5.00` ｜ 📈 現價: `$6.50`\n├─ 🟢 損益: **+30.00%**\n├─ ⏳ DTE: `29` 天 ｜ 秤⚖️ SPY Δ: `+32.50`\n└─ 🎯 動作: HOLD",
        "🌐 【宏觀風險與資金水位報告】",
        "Beta-Weighted Delta: +120.0",
    ]

    embed = create_portfolio_report_embed(report_lines, survival_runway=120)
    assert embed.title == "📊 Nexus Seeker 盤後風險結算報告"
    assert "🏁 財務生存跑道" in embed.fields[0].name
    assert "當前持倉明細" in embed.fields[1].name

    positions_value = embed.fields[1].value
    assert "標的" in positions_value
    assert "AAPL" in positions_value
    assert "2026-06-19" in positions_value
    assert "$150.0C" in positions_value
    assert "+30.00%" in positions_value


def test_build_vtr_stats_embed():
    stats = {"win_rate": 65, "total_trades": 12, "total_pnl": 1500.0, "avg_pnl": 125.0}
    embed = build_vtr_stats_embed("TestUser", stats, ["對沖效能極佳"])
    assert "VTR" in embed.title and "績效總結" in embed.title
    assert "績效指標" in embed.fields[0].value
    assert "總結算次數" in embed.fields[0].value
    assert "12" in embed.fields[0].value
    assert "勝率" in embed.fields[0].value
    assert "65%" in embed.fields[0].value


def test_build_scan_report():
    result = {
        "symbol": "AAPL",
        "strategy": "Bull Put Spread",
        "strike": "150/145",
        "target_date": "2026-06-19",
        "delta": 0.35,
        "theta": -0.04,
        "gamma": 0.002,
        "iv": 0.32,
        "safe_qty": 2,
        "projected_exposure_pct": 8.5,
        "risk_limit": 15.0,
        "ema_signals": [],
        "macro_vix": 18.0,
        "macro_oil": 75.0,
        "spy_price": 500.0,
    }
    embed = build_scan_report(result)
    assert "量化掃描報告: AAPL" in embed.title

    greeks_val = embed.fields[0].value
    assert "希臘字母" in greeks_val
    assert "Delta" in greeks_val
    assert "+0.350" in greeks_val
    assert "IV (隱含波動率)" in greeks_val

    nro_val = embed.fields[1].value
    assert "風控項目" in nro_val
    assert "建議口數" in nro_val
    assert "2 口" in nro_val
    assert "+8.5%" in nro_val


def test_create_ddp_embed():
    report = {
        "symbol": "AAPL",
        "current_pe": 18.5,
        "pe_mean_3y": 24.0,
        "eps_growth": 0.22,
        "rev_accel": True,
        "confidence_score": 85.0,
        "forward_pe": 16.0,
    }
    embed = create_ddp_embed(report)
    assert "戴維斯雙擊預警: AAPL" in embed.title

    ddp_val = embed.fields[0].value
    assert "DDP 量化指標" in ddp_val
    assert "目前本益比 (TTM P/E)" in ddp_val
    assert "18.50" in ddp_val
    assert "+29.7%" in ddp_val
    assert "85/100" in ddp_val


def test_create_volatility_embed():
    report = {
        "symbol": "AAPL",
        "price": 175.0,
        "iv": 30.0,
        "iv_p": 15,
        "hv": 25.0,
        "status": "波動率極低",
        "strategy": "Long Call",
        "trigger_logic": "IV below 15th percentile",
        "days_to_earnings": 15,
        "stop_loss": 160.0,
        "daily_theta": 0.12,
        "runway_impact": 2,
    }
    embed = create_volatility_embed(report)
    assert "波動率優勢偵測" in embed.title

    eval_val = embed.fields[0].value
    assert "評估指標" in eval_val
    assert "當前價格 (Price)" in eval_val
    assert "$175.00" in eval_val

    catalyst_val = embed.fields[1].value
    assert "建議策略 (Strategy)" in catalyst_val
    assert "Long Call" in catalyst_val

    nro_val = embed.fields[2].value
    assert "風控指標" in nro_val
    assert "建議停損 (Stop Loss)" in nro_val
    assert "$160.00" in nro_val
