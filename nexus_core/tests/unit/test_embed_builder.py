from datetime import datetime

from cogs.embed_builder import (
    create_holdings_embed,
    create_trades_embed,
    create_portfolio_report_embed,
    build_vtr_stats_embed,
    build_scan_report,
    create_ddp_embed,
    create_asset_promotion_embed,
    create_ditm_transition_alert_embed,
    create_gamma_fragility_embed,
    create_intraday_execution_guide_embed,
    create_memory_alert_embed,
    create_max_pain_embed,
    create_pre_market_earnings_embed,
    create_polymarket_whale_alert_embed,
    create_polymarket_status_embed,
    create_profit_lock_alert_embed,
    create_quote_embed,
    create_system_health_embed,
    create_transition_simulation_embed,
    create_vtr_settlement_notice_embed,
    create_volatility_embed,
    create_hedge_alert_embed,
    create_hedge_list_embed,
    create_proactive_event_alert_embed,
    create_hedge_settlement_embed,
    create_watchlist_signal_embed,
)
from models.schemas import WatchlistOptionLeg, WatchlistOptionPlan
from types import SimpleNamespace


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


def test_create_hedge_settlement_embed():
    embed = create_hedge_settlement_embed(12, "SPY", 8)
    assert embed.title == "✅ 對沖結算完成"
    assert "#12" in embed.description
    assert embed.fields[0].value == "`SPY`"
    assert embed.fields[1].value == "`8`"


def test_create_hedge_list_embed():
    rows = [
        (1, 22.5, 8, "PENDING", "2026-05-21 10:00:00"),
        (2, 18.0, 5, "EXECUTED", "2026-05-20 09:00:00"),
    ]
    embed = create_hedge_list_embed(rows)
    assert embed.title == "📜 最近對沖警報列表"
    assert "#1" in embed.description
    assert "22.50" in embed.description
    assert "⏳" in embed.description
    assert "✅" in embed.description


def test_create_hedge_alert_embed():
    embed = create_hedge_alert_embed(
        vix=24.5,
        stage_move=2,
        tier_name="Aggressive",
        tier_emoji="🔥",
        color_hex=0xFFAA00,
        total_beta_delta=125.0,
        adjusted_delta=140.0,
        total_vega=-32.5,
        hedge_quantity=140,
        instruction_text="賣出 140 股 SPY",
        narration="VIX 急升導致隱含 Delta 擴張，需先降曝險。",
        alert_id=7,
        poly_snapshot=[
            {
                "question": "Will the Fed cut rates by September?",
                "odds_distribution": [
                    {"outcome": "Yes", "odds": 0.62},
                    {"outcome": "No", "odds": 0.38},
                ],
            }
        ],
    )
    assert embed.title == "🚨 【戰位報告：自動化對沖警報】"
    assert "Aggressive" in embed.description
    assert "140.0" in embed.fields[0].value
    assert "SPY" in embed.fields[3].value
    assert embed.footer.text == "Nexus Seeker Battle Station | Alert ID: 7"


def test_create_proactive_event_alert_embed():
    events = [
        SimpleNamespace(type="ECONOMIC", event="CPI", tte_hours=12),
        SimpleNamespace(type="EARNINGS", symbol="AAPL", tte_hours=18),
    ]
    embed = create_proactive_event_alert_embed(events)
    assert embed.title == "🛡️ 【 預警：重大事件即時防護 】"
    assert len(embed.fields) == 2
    assert "CPI" in embed.fields[0].name
    assert "AAPL" in embed.fields[1].name


def test_create_watchlist_signal_embed():
    option_plan = WatchlistOptionPlan(
        strategy_name="Bull Put Spread",
        premium_type="credit",
        estimated_net_premium=0.35,
        suggested_contracts=2,
        max_risk_amount=330.0,
        rationale="測試用",
        stock_action="測試用",
        legs=[
            WatchlistOptionLeg(
                action="SELL",
                opt_type="PUT",
                strike=120.0,
                expiry="2026-06-19",
                mid_price=1.1,
            ),
            WatchlistOptionLeg(
                action="BUY",
                opt_type="PUT",
                strike=118.0,
                expiry="2026-06-19",
                mid_price=0.75,
            ),
        ],
    )
    embed = create_watchlist_signal_embed(
        symbol="NVDA",
        report_body="```ansi\nwatchlist report\n```",
        option_guidance="可先以 Bull Put Spread 佈局。",
        event_risk_summary="CPI 倒數 12.0 小時 ｜ 先縮口數，優先定義風險的 Debit Spread / 保護性部位。",
        skew_state="+6.20% ｜ ⚠️ 預警性對沖 (Put 昂貴)",
        alert_level="yellow",
        option_plan=option_plan,
    )

    assert embed.title == "📡 Watchlist 半小時戰報：NVDA"
    assert "警報等級" in (embed.description or "")
    assert embed.fields[0].name == "📊 技術 / 期權快照"
    assert "watchlist report" in embed.fields[0].value
    assert embed.fields[1].name == "📐 Skew 與市場判讀"
    assert "測試用" in embed.fields[1].value
    assert embed.fields[2].name == "🗓️ 事件風控"
    assert "CPI" in embed.fields[2].value
    assert "Bull Put Spread" in embed.fields[4].value
    assert "SELL PUT 120.00" in embed.fields[4].value


def test_create_memory_alert_embed():
    embed = create_memory_alert_embed(91.2, 512.4, 120, 87)
    assert embed.title == "🆘 【系統緊急警報：記憶體不足】"
    assert "91.2%" in embed.description
    assert embed.fields[0].value == "`91.2%`"
    assert embed.fields[1].value == "`512.4 MB`"
    assert embed.fields[2].value == "SMA/EMA: `120/87` 筆"


def test_create_polymarket_whale_alert_embed():
    embed = create_polymarket_whale_alert_embed(
        intent_emoji="🟢",
        intent_label="強力看多",
        market_question="Will NVDA beat earnings?",
        usd_value=65000.0,
        dynamic_threshold=10000.0,
        win_rate=78.0,
        is_high_conviction=True,
        is_bullish=True,
        summary="市場預期財報後仍有延續動能。",
        event_slug="nvda-earnings",
        uoa_correlation={
            "uoa": {
                "symbol": "NVDA",
                "expiry": "2026-06-19",
                "strike": 150,
                "type": "CALL",
            },
            "classification": {
                "classification": "方向性押注",
                "confidence": 0.88,
                "explanation": "同步觀察到買權放量。",
            },
        },
    )
    assert "高信心訊號" in embed.title
    assert "Will NVDA beat earnings?" in embed.description
    assert "方向性押注" in embed.description
    assert "預測性對沖建議" in embed.description
    assert "nvda-earnings" in embed.description


def test_create_polymarket_status_embed():
    embed = create_polymarket_status_embed(
        {
            "connected": True,
            "running": True,
            "asset_count": 42,
            "last_message": "2026-05-21 17:00:00",
            "errors": 1,
        }
    )
    assert "Polymarket 服務狀態" in embed.title
    assert "✅ 運行中" in embed.description
    assert "`42`" in embed.description


def test_create_quote_embed():
    embed = create_quote_embed(
        "AAPL",
        {"c": 150.0, "dp": 1.3, "h": 155.0, "l": 145.0, "pc": 148.0},
    )
    assert "AAPL" in embed.title
    assert embed.fields[0].value == "**$150.0**"
    assert embed.fields[1].value == "`1.3%`"
    assert "155.0" in embed.fields[2].value


def test_create_max_pain_embed_with_guidance():
    embed = create_max_pain_embed(
        "TSLA",
        {
            "expiry": "2099-01-02",
            "max_pain": 200,
            "current_price": 198.5,
            "distance_pct": -0.75,
            "is_converging": True,
        },
    )
    assert "TSLA" in embed.title
    assert "收斂中" in (embed.description or "")


def test_create_max_pain_embed_with_short_dte_guidance():
    near_expiry = datetime.now().strftime("%Y-%m-%d")
    embed = create_max_pain_embed(
        "SPY",
        {
            "expiry": near_expiry,
            "max_pain": 500,
            "current_price": 501.2,
            "distance_pct": 0.24,
            "is_converging": False,
        },
    )
    assert any(field.name == "🚀 執行建議" for field in embed.fields)


def test_create_system_health_embed():
    embed = create_system_health_embed(
        memory_percent=96.0,
        memory_available_mb=256.0,
        cpu_percent=33.0,
        process_memory_mb=512.0,
        disk_percent=97.0,
        disk_free_gb=1.5,
        sma_cache_size=120,
        ema_cache_size=87,
        poly_cache_size=10,
        orderbook_size=5,
    )
    assert embed.title == "🖥️ Nexus Seeker 系統健康診斷"
    assert "120/87" in embed.fields[4].value
    assert "🆘 **極度危險**" in embed.fields[5].value


def test_create_asset_promotion_embed():
    embed = create_asset_promotion_embed("AAPL", "2026-06-19", 150.0, "call", 2, 5.5)
    assert embed.title == "🌌 Nexus | 資產晉升成功"
    assert "AAPL" in embed.description
    assert "2026-06-19" in embed.fields[0].value
    assert "CALL" in embed.fields[0].value


def test_create_transition_simulation_embed():
    embed = create_transition_simulation_embed(
        symbol="NVDA",
        current_price=100.0,
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        target_cc_strike=110.0,
        target_cc_premium=2.5,
        projected_aroc=18.0,
        capital_efficiency_gain=2.7,
    )
    assert "NVDA" in embed.title
    assert "$100.00" in embed.fields[0].value
    assert "7,500.00" in embed.fields[2].value
    assert "符合 15% 門檻" in embed.fields[3].value


def test_create_profit_lock_alert_embed():
    embed = create_profit_lock_alert_embed(
        {"symbol": "AAPL", "pnl_pct": 180, "dte": 5, "reason": "Delta 已接近 1.0"}
    )
    assert "獲利鎖定" in embed.title
    assert "AAPL" in embed.description
    assert "180%" in embed.fields[0].value


def test_create_gamma_fragility_embed():
    embed = create_gamma_fragility_embed({"net_gamma": -25.5, "threshold": -20})
    assert "Gamma 脆弱性警告" in embed.title
    assert "`-25.5`" == embed.fields[0].value
    assert "`-20`" == embed.fields[1].value


def test_create_pre_market_earnings_embed_with_alerts():
    embed = create_pre_market_earnings_embed(
        [
            {
                "symbol": "NVDA",
                "is_portfolio": True,
                "earnings_date": "2026-06-01",
                "days_left": 3,
            }
        ],
        ["NVDA"],
        14,
    )
    assert "盤前財報季雷達預警" in embed.title
    assert "NVDA" in embed.description


def test_create_pre_market_earnings_embed_without_alerts():
    embed = create_pre_market_earnings_embed([], ["AAPL", "MSFT"], 14)
    assert "盤前財報季雷達掃描完畢" in embed.title
    assert "`AAPL`" in embed.description


def test_create_ditm_transition_alert_embed():
    embed = create_ditm_transition_alert_embed(
        symbol="TSLA",
        exit_reason="Delta 接近 1.0",
        action_taken="已平倉 (Closed)",
        pnl=1250.0,
        exposure_pct=12.5,
        hedge={"action": "賣出 10 股 SPY", "gap": 10},
    )
    assert "DITM 凸性防禦" in embed.title
    assert "TSLA" in embed.description
    assert "12.50%" in embed.fields[3].value
    assert "賣出 10 股 SPY" in embed.fields[4].value


def test_create_intraday_execution_guide_embed():
    embed = create_intraday_execution_guide_embed(
        phase_name="Phase B",
        vix=18.5,
        memory_percent=50.0,
        is_memory_gated=False,
        vix_level_name="Ready",
        greeks_status="Δ: `100.00` | 隱含 Δ (Vanna): `110.00`",
        runway_days=365.0,
        theta_cov=150.0,
        active_signal_content="**板塊輪動:** 關注科技與金融板塊資金流向。",
        sma_cache_size=12,
        ema_cache_size=8,
    )
    assert "Phase B" in embed.title
    assert "Ready" in embed.fields[0].value
    assert "365.0" in embed.fields[1].value
    assert "板塊輪動" in embed.fields[2].value
    assert "12/8" in embed.fields[3].value


def test_create_intraday_execution_guide_embed_memory_gate():
    embed = create_intraday_execution_guide_embed(
        phase_name="Phase A",
        vix=15.0,
        memory_percent=90.0,
        is_memory_gated=True,
    )
    assert "Phase A" in embed.title
    assert "Memory Safety Gate Active" in (embed.description or "")
    assert "90.0%" in embed.fields[0].value


def test_create_vtr_settlement_notice_embed():
    embed = create_vtr_settlement_notice_embed(
        status_icon="🔄 [轉倉完成]",
        symbol="TSLA",
        pnl=850.0,
        exposure_pct=9.5,
        regime="Balanced",
        target_delta=12.0,
        hedge={"action": "買入 5 股 SPY", "gap": 5},
    )
    assert "TSLA" in embed.title
    assert "`9.50%`" in embed.fields[1].value
    assert "`Balanced`" in embed.fields[2].value
    assert "買入 5 股 SPY" in embed.fields[4].value
