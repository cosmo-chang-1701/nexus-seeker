from datetime import datetime

from cogs.embed_builder import (
    create_holdings_embed,
    create_trades_embed,
    create_portfolio_report_embed,
    build_vtr_stats_embed,
    build_scan_report,
    create_earnings_report_embed,
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
    create_sector_flow_report_embed,
    split_embed_by_fields,
    create_hedge_settlement_embed,
    create_watchlist_overview_embed,
    create_watchlist_signal_embed,
    create_sentiment_scan_embed,
    create_media_sentiment_embed,
    create_active_orders_embed,
    build_radar_scan_embed,
    build_post_market_intelligence_embed,
    create_covered_call_unlock_embed,
)
from models.schemas import WatchlistOptionLeg, WatchlistOptionPlan


def test_create_holdings_embed() -> None:
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
    assert "標的" in desc_field  # type: ignore
    assert "現價" in desc_field  # type: ignore
    assert "AAPL" in desc_field  # type: ignore
    assert "$160.00" in desc_field  # type: ignore


def test_create_trades_embed() -> None:
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
            },
            {
                "id": 2,
                "symbol": "MSFT",
                "opt_type": "put",
                "strike": 400.0,
                "expiry": "2026-06-19",
                "quantity": -1,
                "entry_price": 10.0,
                "current_price": 8.00,
                "unrealized_pnl": 200.0,
                "pnl_pct": 0.2,
            },
        ],
        "total_unrealized_pnl": 350.0,
    }
    embed = create_trades_embed(pnl_data, total_capital=100000.0)
    assert embed.title == "📊 Nexus Seeker | 實單持倉清單 (包含帳面損益)"

    desc_field = embed.fields[0].value
    assert desc_field is not None
    assert "數量" in desc_field  # type: ignore
    assert "現價" in desc_field  # type: ignore
    assert "  6.50" in desc_field  # Visual formatting check  # type: ignore
    assert (
        "  -1" in desc_field
    )  # Visual formatting check for negative quantity  # type: ignore


def test_create_portfolio_report_embed() -> None:
    report_lines = [
        "🔹 **AAPL** ｜ `2026-06-19` ｜ `$150.0` **CALL**\n├─ 💰 成本: `$5.00` ｜ 📈 現價: `$6.50`\n├─ 🟢 損益: **+30.00%**\n├─ ⏳ DTE: `29` 天 ｜ 秤⚖️ SPY Δ: `+32.50`\n└─ 🎯 動作: HOLD",
        "🌐 【宏觀風險與資金水位報告】",
        "Beta-Weighted Delta: +120.0",
    ]

    embed = create_portfolio_report_embed(report_lines, survival_runway=120)
    assert embed.title == "📊 Nexus Seeker 盤後風險結算報告"
    assert "🏁 財務生存跑道" in embed.fields[0].name

    assert embed.fields[1].name == "💰 實質暴露 (Debit Cost)"
    assert "$500.00" in embed.fields[1].value
    assert embed.fields[2].name == "💵 收取權利金 (Credit Cash)"
    assert "$0.00" in embed.fields[2].value
    assert embed.fields[3].name == "📊 未實現損益 (Unrealized PnL)"
    assert "+$150.00" in embed.fields[3].value

    assert "當前持倉明細" in embed.fields[4].name

    positions_value = embed.fields[4].value
    assert "標的" in positions_value
    assert "AAPL" in positions_value
    assert "2026-06-19" in positions_value
    assert "$150.0" in positions_value
    assert "CALL" in positions_value
    assert "+30.00%" in positions_value


def test_create_earnings_report_embed() -> None:
    embed = create_earnings_report_embed(
        "[08:30 UTC+8] 盤前財報與估值調整",
        "1. **📌 核心觀察**\nMU 與 NVDA 進入財報前壓縮區。\n"
        "2. **⚠️ 風險提示**\n避免在事件前擴大裸賣方曝險。",
        {
            "analyzed_symbols": 12,
            "upcoming_earnings": {
                "MU": [{"date": "2026-05-23"}],
                "NVDA": [{"date": "2026-05-24"}],
            },
            "earnings_sentiment_scan": {
                "MU": {
                    "news": "Micron 財報前市場聚焦 HBM 需求延續。",
                    "reddit_sentiment": "社群偏多，但擔心財測落差。",
                }
            },
            "note": "IV and VRP are evaluated dynamically based on recent price action.",
        },
    )

    assert embed.title == "📊 Nexus Seeker 盤前財報與估值調整"
    assert "更新批次" in (embed.description or "")
    assert embed.fields[0].name == "📅 即將發布財報標的"
    assert "MU" in embed.fields[0].value  # type: ignore
    assert embed.fields[1].name == "🧠 情緒 / 估值快照"
    assert "HBM" in embed.fields[1].value  # type: ignore
    assert any(field.name == "📌 核心觀察" for field in embed.fields)


def test_create_sector_flow_report_embed() -> None:
    embed = create_sector_flow_report_embed(
        "[04:15 UTC+8] 收盤資金流向與板塊輪動報告",
        "1. **🔄 板塊主線**\n科技與金融為今日主導。\n"
        "2. **🐋 事件觀察**\nPolymarket 仍聚焦降息與 AI 資本支出。",
        {
            "vix": 19.2,
            "vix_tier_name": "Ready",
            "spy_price": 528.5,
            "sectors": [
                {
                    "symbol": "XLK",
                    "name": "Technology",
                    "pct_change": 1.45,
                    "rel_vol": 1.32,
                    "skew": 4.8,
                    "uoa_count": 2,
                },
                {
                    "symbol": "XLF",
                    "name": "Financials",
                    "pct_change": 0.92,
                    "rel_vol": 1.18,
                    "skew": 1.5,
                    "uoa_count": 1,
                },
            ],
            "poly_events": [{"question": "Will the Fed cut rates by September?"}],
            "spy_max_pain": {"max_pain": 520.0},
        },
    )

    assert embed.title == "📊 Nexus Seeker 收盤資金流向與板塊輪動報告"
    assert "SPY 現價" in (embed.description or "")
    assert embed.fields[0].name == "🌐 收盤市場快照"
    assert "Ready" in embed.fields[0].value  # type: ignore
    assert embed.fields[1].name == "🔄 板塊輪動快照"
    assert "XLK" in embed.fields[1].value  # type: ignore
    assert any(field.name == "🔄 板塊主線" for field in embed.fields)


def test_split_embed_by_fields_creates_one_message_per_block() -> None:
    embed = create_sector_flow_report_embed(
        "[04:15 UTC+8] 收盤資金流向與板塊輪動報告",
        "1. **🔄 板塊主線**\n科技與金融為今日主導。\n"
        "2. **🐋 事件觀察**\nPolymarket 仍聚焦降息與 AI 資本支出。",
        {
            "vix": 19.2,
            "vix_tier_name": "Ready",
            "spy_price": 528.5,
            "sectors": [
                {
                    "symbol": "XLK",
                    "name": "Technology",
                    "pct_change": 1.45,
                    "rel_vol": 1.32,
                    "skew": 4.8,
                    "uoa_count": 2,
                }
            ],
            "poly_events": [{"question": "Will the Fed cut rates by September?"}],
            "spy_max_pain": {"max_pain": 520.0},
        },
    )

    split_embeds = split_embed_by_fields(embed, max_size=1)

    assert len(split_embeds) == len(embed.fields)
    assert split_embeds[0].description
    assert split_embeds[1].description is None
    assert split_embeds[0].fields[0].name == embed.fields[0].name
    assert split_embeds[-1].title.endswith(f"({len(embed.fields)}/{len(embed.fields)})")  # type: ignore


def test_build_vtr_stats_embed() -> None:
    stats = {"win_rate": 65, "total_trades": 12, "total_pnl": 1500.0, "avg_pnl": 125.0}
    embed = build_vtr_stats_embed("TestUser", stats, ["對沖效能極佳"])
    assert "VTR" in embed.title and "績效總結" in embed.title  # type: ignore
    assert "績效指標" in embed.fields[0].value  # type: ignore
    assert "總結算次數" in embed.fields[0].value  # type: ignore
    assert "12" in embed.fields[0].value  # type: ignore
    assert "勝率" in embed.fields[0].value  # type: ignore
    assert "65%" in embed.fields[0].value  # type: ignore


def test_build_scan_report() -> None:
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


def test_create_ddp_embed() -> None:
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
    assert "警報：Nexus 戴維斯雙擊 (DDP) | AAPL" in embed.title  # type: ignore

    ddp_val = embed.fields[0].value
    assert "DDP 量化指標" in ddp_val  # type: ignore
    assert "目前本益比 (TTM P/E)" in ddp_val  # type: ignore
    assert "18.50" in ddp_val  # type: ignore
    assert "+29.7%" in ddp_val  # type: ignore
    assert "85/100" in ddp_val  # type: ignore


def test_create_volatility_embed() -> None:
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
    assert "警報：Nexus 戴維斯雙擊 (波動率優勢) | AAPL" in embed.title  # type: ignore

    eval_val = embed.fields[0].value
    assert "評估指標" in eval_val  # type: ignore
    assert "當前價格 (Price)" in eval_val  # type: ignore
    assert "$175.00" in eval_val  # type: ignore

    catalyst_val = embed.fields[1].value
    assert "建議策略 (Strategy)" in catalyst_val  # type: ignore
    assert "Long Call" in catalyst_val  # type: ignore

    nro_val = embed.fields[2].value
    assert "風控指標" in nro_val  # type: ignore
    assert "建議停損 (Stop Loss)" in nro_val  # type: ignore
    assert "$160.00" in nro_val  # type: ignore


def test_create_hedge_settlement_embed() -> None:
    embed = create_hedge_settlement_embed(12, "SPY", 8)
    assert embed.title == "✅ 對沖結算完成"
    assert "#12" in embed.description  # type: ignore
    assert embed.fields[0].value == "`SPY`"
    assert embed.fields[1].value == "`8`"


def test_create_hedge_list_embed() -> None:
    rows = [
        (1, 22.5, 8, "PENDING", "2026-05-21 10:00:00"),
        (2, 18.0, 5, "EXECUTED", "2026-05-20 09:00:00"),
    ]
    embed = create_hedge_list_embed(rows)
    assert embed.title == "📜 最近對沖警報列表"
    assert "#1" in embed.description  # type: ignore
    assert "22.50" in embed.description  # type: ignore
    assert "⏳" in embed.description  # type: ignore
    assert "✅" in embed.description  # type: ignore


def test_create_hedge_alert_embed() -> None:
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
    assert "Aggressive" in embed.description  # type: ignore
    assert "140.0" in embed.fields[0].value  # type: ignore
    assert "SPY" in embed.fields[3].value  # type: ignore
    assert embed.footer.text == "🌌 Nexus Seeker • Battle Station | Alert ID: 7"


def test_create_proactive_event_alert_embed() -> None:
    events = [
        {
            "name": "🔴 經濟數據: CPI",
            "tte_hours": 12.0,
            "risk_status": "Heat `9.2% / 15.0%` ｜ 賣方偏重 ｜ 短 Gamma ｜ Vanna 敏感中",
            "instruction": "維持 Calendar Guard：提高 Vanna 權重、縮小方向押注，優先保留可快速調整的部位。",
        },
        {
            "name": "📊 財報預警: AAPL",
            "tte_hours": 18.0,
            "risk_status": "Heat `9.2% / 15.0%` ｜ 賣方偏重 ｜ 短 Gamma ｜ Vanna 敏感中",
            "instruction": "財報窗口已開啟；控制口數、避免堆疊裸賣方，若要保留方向觀點優先使用定義風險結構。",
        },
    ]
    embeds = create_proactive_event_alert_embed(events)
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed.title == "🛡️ 【 預警：重大事件即時防護 】"
    assert len(embed.fields) == 2
    assert "CPI" in embed.fields[0].name  # type: ignore
    assert "AAPL" in embed.fields[1].name  # type: ignore
    assert "持倉風險狀態" in embed.fields[0].value  # type: ignore
    assert "NRO 指令" in embed.fields[1].value  # type: ignore

    # 3. Test pagination (30 events)
    many_events = []
    for i in range(30):
        many_events.append(
            {
                "name": f"Event {i}",
                "tte_hours": 10.0,
                "risk_status": "正常",
                "instruction": "測試",
            }
        )
    embeds_many = create_proactive_event_alert_embed(many_events)
    assert len(embeds_many) > 1
    assert sum(len(e.fields) for e in embeds_many) == 30
    assert " (1/" in embeds_many[0].title  # type: ignore


def test_create_watchlist_signal_embed() -> None:
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
        skew_commentary="Skew 左偏代表保護性買盤偏多，若事件風險逼近應優先使用定義風險結構。",
        has_position=True,
        holding_quantity=120.0,
        holding_avg_cost=150.0,
        holding_pnl_pct=0.10,
        suitable_sell_price=165.50,
        suitable_sell_shares=30,
        sell_rationale="分批減碼 25%",
    )

    assert embed is not None
    assert (
        embed.title == "標的分析中心 2.0: NVDA 每半小時戰場心跳 [數據未更新/降級模式]"
    )

    assert "物理籌碼牆與邊緣偵測 (Market Footprints)" in embed.description  # type: ignore
    assert "心跳：期權結構與波動率" in embed.description  # type: ignore
    assert "結算與目標 (Target Lock)" in embed.description  # type: ignore
    assert (
        "既有現貨持倉: 120 股 ｜ 平均成本: $150.00 ｜ 當前損益: +10.00%"  # type: ignore
        in embed.description
    )
    assert "操盤執行指南: 可先以 Bull Put Spread 佈局。" in embed.description  # type: ignore


def test_create_watchlist_signal_embed_covered_call() -> None:
    option_plan = WatchlistOptionPlan(
        strategy_name="Covered Call",
        premium_type="credit",
        estimated_net_premium=4.15,
        suggested_contracts=1,
        max_risk_amount=0.0,
        rationale="測試 Covered Call",
        stock_action="拋補看漲期權 / 高位收租",
        legs=[
            WatchlistOptionLeg(
                action="SELL",
                opt_type="CALL",
                strike=115.0,
                expiry="2026-06-26",
                mid_price=4.15,
            )
        ],
    )
    embed = create_watchlist_signal_embed(
        symbol="INTC",
        report_body="```ansi\nwatchlist report\n```",
        option_guidance="Covered Call 鎖利。",
        event_risk_summary="無重大事件",
        skew_state="-5.10% ｜ 右偏 (Call 昂貴)",
        alert_level="yellow",
        option_plan=option_plan,
        skew_commentary="Skew 右偏顯示買權昂貴，適合 Covered Call 收租。",
        has_position=True,
        holding_quantity=100.0,
        holding_avg_cost=113.50,
        holding_pnl_pct=-0.0397,
        suitable_sell_price=115.00,
        suitable_sell_shares=100,
        sell_rationale="全數出清現貨避險",
    )

    assert embed is not None
    assert (
        embed.title == "標的分析中心 2.0: INTC 每半小時戰場心跳 [數據未更新/降級模式]"
    )
    assert (
        "既有現貨持倉: 100 股 ｜ 平均成本: $113.50 ｜ 當前損益: -3.97%"  # type: ignore
        in embed.description
    )
    assert "操盤執行指南: Covered Call 鎖利。" in embed.description  # type: ignore


def test_create_watchlist_overview_embed() -> None:
    embed = create_watchlist_overview_embed(
        [
            {
                "symbol": "NVDA",
                "alert_level": "yellow",
                "skew_state": "+6.20% ｜ ⚠️ 預警性對沖 (Put 昂貴)",
                "scenario": "premium-harvest",
                "event_risk_summary": "CPI 倒數 12.0 小時 ｜ 先縮口數，優先定義風險的 Debit Spread / 保護性部位。",
                "holding_pnl_pct": 0.15,  # type: ignore
            },
            {
                "symbol": "AAPL",
                "alert_level": "green",
                "skew_state": "+1.10% ｜ 正常",
                "scenario": "wait",
                "event_risk_summary": "未偵測到近期需調整參數的重大事件。",
            },
        ],
        llm_overview="本輪先留意 NVDA 的事件風險與偏左 skew，AAPL 維持例行追蹤。",
    )

    assert embed.title == "🧭 本輪 Watchlist 總覽"
    assert "追蹤標的" in (embed.description or "")
    assert embed.fields[0].name == "🎯 本輪焦點"
    assert "NVDA" in embed.fields[0].value  # type: ignore
    assert "權利金佈局" in embed.fields[0].value  # type: ignore
    assert "現貨損益" in embed.fields[0].value  # type: ignore
    assert "+15.00%" in embed.fields[0].value  # type: ignore
    assert embed.fields[1].name == "📋 全標的速覽"
    assert "AAPL" in embed.fields[1].value  # type: ignore
    assert "觀望待機" in embed.fields[1].value  # type: ignore
    assert "NVDA" in embed.fields[1].value  # type: ignore
    assert "+15.00%" in embed.fields[1].value  # type: ignore
    assert embed.fields[2].name == "🤖 LLM 本輪摘要"
    assert "NVDA" in embed.fields[2].value  # type: ignore


def test_create_memory_alert_embed() -> None:
    embed = create_memory_alert_embed(91.2, 512.4, 120, 87)
    assert embed.title == "🆘 【系統緊急警報：記憶體不足】"
    assert "91.2%" in embed.description  # type: ignore
    assert embed.fields[0].value == "`91.2%`"
    assert embed.fields[1].value == "`512.4 MB`"
    assert embed.fields[2].value == "SMA/EMA: `120/87` 筆"


def test_create_polymarket_whale_alert_embed() -> None:
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
    assert "高信心訊號" in embed.title  # type: ignore
    assert "Will NVDA beat earnings?" in embed.description  # type: ignore
    assert "方向性押注" in embed.description  # type: ignore
    assert "預測性對沖建議" in embed.description  # type: ignore
    assert "nvda-earnings" in embed.description  # type: ignore


def test_create_polymarket_status_embed() -> None:
    embed = create_polymarket_status_embed(
        {
            "connected": True,
            "running": True,
            "asset_count": 42,
            "last_message": "2026-05-21 17:00:00",
            "errors": 1,
        }
    )
    assert "Polymarket 服務狀態" in embed.title  # type: ignore
    assert "✅ 運行中" in embed.description  # type: ignore
    assert "`42`" in embed.description  # type: ignore


def test_create_quote_embed() -> None:
    embed = create_quote_embed(
        "AAPL",
        {"c": 150.0, "dp": 1.3, "h": 155.0, "l": 145.0, "pc": 148.0},
    )
    assert "AAPL" in embed.title  # type: ignore
    assert embed.fields[0].value == "**$150.0**"
    assert embed.fields[1].value == "`1.3%`"
    assert "155.0" in embed.fields[2].value  # type: ignore


def test_create_max_pain_embed_with_guidance() -> None:
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
    assert "TSLA" in embed.title  # type: ignore
    assert "收斂中" in (embed.description or "")


def test_create_max_pain_embed_with_short_dte_guidance() -> None:
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


def test_create_system_health_embed() -> None:
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
    assert "120/87" in embed.fields[4].value  # type: ignore
    assert "🆘 **極度危險**" in embed.fields[5].value  # type: ignore


def test_create_asset_promotion_embed() -> None:
    embed = create_asset_promotion_embed("AAPL", "2026-06-19", 150.0, "call", 2, 5.5)
    assert embed.title == "🌌 Nexus | 資產晉升成功"
    assert "AAPL" in embed.description  # type: ignore
    assert "2026-06-19" in embed.fields[0].value  # type: ignore
    assert "CALL" in embed.fields[0].value  # type: ignore


def test_create_transition_simulation_embed() -> None:
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
    assert "NVDA" in embed.title  # type: ignore
    assert "$100.00" in embed.fields[0].value  # type: ignore
    assert "7,500.00" in embed.fields[2].value  # type: ignore
    assert "符合 15% 門檻" in embed.fields[3].value  # type: ignore


def test_create_profit_lock_alert_embed() -> None:
    embed = create_profit_lock_alert_embed(
        {"symbol": "AAPL", "pnl_pct": 180, "dte": 5, "reason": "Delta 已接近 1.0"}
    )
    assert "獲利鎖定" in embed.title  # type: ignore
    assert "AAPL" in embed.description  # type: ignore
    assert "180%" in embed.fields[0].value  # type: ignore


def test_create_gamma_fragility_embed() -> None:
    embed = create_gamma_fragility_embed({"net_gamma": -25.5, "threshold": -20})
    assert "🆘 警報：Gamma 脆弱性與斷層" in embed.title  # type: ignore
    assert "`-25.5`" == embed.fields[0].value
    assert "`-20`" == embed.fields[1].value


def test_create_pre_market_earnings_embed_with_alerts() -> None:
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
    assert "盤前財報季雷達預警" in embed.title  # type: ignore
    assert "NVDA" in embed.description  # type: ignore


def test_create_pre_market_earnings_embed_without_alerts() -> None:
    embed = create_pre_market_earnings_embed([], ["AAPL", "MSFT"], 14)
    assert "盤前財報季雷達掃描完畢" in embed.title  # type: ignore
    assert "`AAPL`" in embed.description  # type: ignore


def test_create_ditm_transition_alert_embed() -> None:
    embed = create_ditm_transition_alert_embed(
        symbol="TSLA",
        exit_reason="Delta 接近 1.0",
        action_taken="已平倉 (Closed)",
        pnl=1250.0,
        exposure_pct=12.5,
        hedge={"action": "賣出 10 股 SPY", "gap": 10},
    )
    assert "🚨 警報：DITM 凸性防護與獲利鎖定 | TSLA" in embed.title  # type: ignore
    assert "TSLA" in embed.description  # type: ignore
    assert "12.50%" in embed.fields[3].value  # type: ignore
    assert "賣出 10 股 SPY" in embed.fields[4].value  # type: ignore


def test_create_intraday_execution_guide_embed() -> None:
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
    assert "Phase B" in embed.title  # type: ignore
    assert "Ready" in embed.fields[0].value  # type: ignore
    assert "365.0" in embed.fields[1].value  # type: ignore
    assert "板塊輪動" in embed.fields[2].value  # type: ignore
    assert "12/8" in embed.fields[3].value  # type: ignore


def test_create_intraday_execution_guide_embed_memory_gate() -> None:
    embed = create_intraday_execution_guide_embed(
        phase_name="Phase A",
        vix=15.0,
        memory_percent=90.0,
        is_memory_gated=True,
    )
    assert "Phase A" in embed.title  # type: ignore
    assert "Memory Safety Gate Active" in (embed.description or "")
    assert "90.0%" in embed.fields[0].value  # type: ignore


def test_create_vtr_settlement_notice_embed() -> None:
    embed = create_vtr_settlement_notice_embed(
        status_icon="🔄 [轉倉完成]",
        symbol="TSLA",
        pnl=850.0,
        exposure_pct=9.5,
        regime="Balanced",
        target_delta=12.0,
        hedge={"action": "買入 5 股 SPY", "gap": 5},
    )
    assert "TSLA" in embed.title  # type: ignore
    assert "`9.50%`" in embed.fields[1].value  # type: ignore
    assert "`Balanced`" in embed.fields[2].value  # type: ignore
    assert "買入 5 股 SPY" in embed.fields[4].value  # type: ignore


def test_create_holdings_embed_chunking() -> None:
    # Create 30 holdings to force chunking
    holdings_data = []
    for i in range(30):
        holdings_data.append(
            {
                "symbol": f"SYM{i:02d}",
                "quantity": 100,
                "avg_cost": 10.0,
                "current_price": 12.0,
            }
        )
    embed = create_holdings_embed(holdings_data, total_capital=100000.0)
    # Check that it split into multiple fields
    holding_fields = [f for f in embed.fields if "持倉明細" in f.name]  # type: ignore
    assert len(holding_fields) > 1
    assert "持倉明細 (1/" in holding_fields[0].name  # type: ignore
    for f in holding_fields:
        assert len(f.value) <= 1024  # type: ignore
        assert "SYM" in f.value  # type: ignore
        assert "```ansi" in f.value  # type: ignore
        assert "```" in f.value  # type: ignore


def test_create_trades_embed_chunking() -> None:
    # Create 30 trades to force chunking
    trades = []
    for i in range(30):
        trades.append(
            {
                "id": i + 1,
                "symbol": f"S{i:02d}",
                "opt_type": "call",
                "strike": 100.0,
                "expiry": "2026-06-19",
                "quantity": 1,
                "entry_price": 2.0,
                "current_price": 2.50,
                "unrealized_pnl": 50.0,
                "pnl_pct": 0.25,
            }
        )
    pnl_data = {
        "trades": trades,
        "total_unrealized_pnl": 1500.0,
    }
    embed = create_trades_embed(pnl_data, total_capital=100000.0)
    trade_fields = [f for f in embed.fields if "持倉明細" in f.name]  # type: ignore
    assert len(trade_fields) > 1
    assert "持倉明細 (1/" in trade_fields[0].name  # type: ignore
    for f in trade_fields:
        assert len(f.value) <= 1024  # type: ignore
        assert "```ansi" in f.value  # type: ignore
        assert "```" in f.value  # type: ignore


def test_create_portfolio_report_embed_chunking() -> None:
    # Create a large number of positions to force chunking in create_portfolio_report_embed
    report_lines = []
    for i in range(30):
        report_lines.append(
            f"🔹 **SYM{i:02d}** ｜ `2026-06-19` ｜ `$100.0` **CALL**\n├─ 💰 成本: `$2.00` ｜ 📈 現價: `$2.50`\n├─ 🟢 損益: **+25.00%**\n├─ ⏳ DTE: `29` 天 ｜ 秤⚖️ SPY Δ: `+5.0`\n└─ 🎯 動作: HOLD"
        )
    # Add macro section
    report_lines.append("🌐 【宏觀風險與資金水位報告】")
    report_lines.append("Beta-Weighted Delta: +150.0")

    embed = create_portfolio_report_embed(report_lines, survival_runway=120)
    pos_fields = [f for f in embed.fields if "當前持倉明細" in f.name]
    assert len(pos_fields) > 1
    assert "當前持倉明細 (1/" in pos_fields[0].name
    for f in pos_fields:
        assert len(f.value) <= 1024
        assert "```ansi" not in f.value


def test_create_sentiment_scan_embed_premarket() -> None:
    """Verify that create_sentiment_scan_embed renders correct UI components for pre-market and regular paths."""
    symbol = "AAPL"
    skew_data = {"skew": 2.5, "state": "Bullish"}
    pcr_data = {"pcr": 0.8, "state": "Normal"}
    uoa_data = []  # type: ignore
    max_pain_data = {"max_pain": 150.0, "is_converging": True}

    # Path A: Pre-market Scan with Complete Failure (current_iv = 0.0, is_premarket = True)
    iv_data_degraded = {
        "current_iv": 0.0,
        "iv_rank": 0.0,
        "iv_percentile": 0.0,
        "expected_move_weekly": 0.0,
        "iv_status": "Normal",
        "is_premarket": True,
    }
    embed_degraded = create_sentiment_scan_embed(
        symbol, skew_data, pcr_data, uoa_data, max_pain_data, iv_data_degraded
    )
    assert "[盤前數據未更新]" in embed_degraded.title  # type: ignore
    iv_field_value_degraded = embed_degraded.fields[0].value
    assert "--%" in iv_field_value_degraded  # type: ignore
    assert "等待開盤" in iv_field_value_degraded  # type: ignore

    # Path B: Pre-market Scan with Fallback Success (current_iv = 0.45, is_premarket = True)
    iv_data_fallback = {
        "current_iv": 0.45,
        "iv_rank": 52.0,
        "iv_percentile": 60.0,
        "expected_move_weekly": 5.4,
        "iv_status": "Normal",
        "is_premarket": True,
    }
    embed_fallback = create_sentiment_scan_embed(
        symbol, skew_data, pcr_data, uoa_data, max_pain_data, iv_data_fallback
    )
    assert "[盤前/前日收盤]" in embed_fallback.title  # type: ignore
    iv_field_value_fallback = embed_fallback.fields[0].value
    assert "前日收盤 IV / SQLite 快取" in iv_field_value_fallback  # type: ignore
    assert "45.0%" in iv_field_value_fallback  # type: ignore

    # Path C: Regular Scan (is_premarket = False)
    iv_data_regular = {
        "current_iv": 0.45,
        "iv_rank": 52.0,
        "iv_percentile": 60.0,
        "expected_move_weekly": 5.4,
        "iv_status": "Normal",
        "is_premarket": False,
    }
    embed_regular = create_sentiment_scan_embed(
        symbol, skew_data, pcr_data, uoa_data, max_pain_data, iv_data_regular
    )
    assert "[盤前" not in embed_regular.title  # type: ignore
    iv_field_value_regular = embed_regular.fields[0].value
    assert "當前 30 天平值期權隱含波動率" in iv_field_value_regular  # type: ignore


def test_create_media_sentiment_embed() -> None:
    """Verify that create_media_sentiment_embed renders institutional news and reddit consensus correctly."""
    symbol = "TSLA"
    news_text = "Tesla stock spikes on earnings beat"
    reddit_text = "To the moon! Bullish sentiment on TSLA options"

    embed = create_media_sentiment_embed(symbol, news_text, reddit_text)
    assert embed.title == "🎭 TSLA 輿情與社群大盤掃描 (Media & Social)"
    assert "最新新聞" in embed.fields[0].name
    assert news_text in embed.fields[0].value
    assert "Reddit 討論" in embed.fields[1].name
    assert reddit_text in embed.fields[1].value


def test_create_active_orders_embed() -> None:
    """Verify that create_active_orders_embed correctly renders active orders with premium ANSI card formatting."""
    # 1. Test empty state
    embeds_empty = create_active_orders_embed([])
    assert len(embeds_empty) == 1
    assert "待成交委託單列表" in embeds_empty[0].title  # type: ignore
    assert "目前沒有任何活躍的待成交委託單" in embeds_empty[0].description  # type: ignore

    # 2. Test populated state
    orders = [
        {
            "id": 1,
            "symbol": "AAPL",
            "quantity": 100.0,
            "order_type": "LIMIT",
            "validity": "DAY",
            "limit_price": 175.50,
            "stop_price": 0.0,
            "trailing_value": 0.0,
        },
        {
            "id": 2,
            "symbol": "TSLA",
            "quantity": 50.0,
            "order_type": "STOP",
            "validity": "NIGHT",
            "limit_price": 0.0,
            "stop_price": 180.00,
            "trailing_value": 0.0,
        },
    ]
    embeds = create_active_orders_embed(orders)
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed.title == "📋 Nexus Seeker | 待成交委託單列表"
    assert len(embed.fields) == 2
    assert "委託單 #1" in embed.fields[0].name  # type: ignore
    assert "AAPL" in embed.fields[0].value  # type: ignore
    assert "限價單 (LIMIT)" in embed.fields[0].value  # type: ignore
    assert "175.50" in embed.fields[0].value  # type: ignore

    assert "委託單 #2" in embed.fields[1].name  # type: ignore
    assert "TSLA" in embed.fields[1].value  # type: ignore
    assert "停損單 (STOP)" in embed.fields[1].value  # type: ignore
    assert "180.00" in embed.fields[1].value  # type: ignore

    # 3. Test pagination (30 orders)
    many_orders = []
    for i in range(30):
        many_orders.append(
            {
                "id": i + 1,
                "symbol": f"SYM{i}",
                "quantity": 10.0,
                "order_type": "LIMIT",
                "validity": "DAY",
                "limit_price": 100.0,
                "stop_price": 0.0,
                "trailing_value": 0.0,
            }
        )
    embeds_many = create_active_orders_embed(many_orders)
    assert len(embeds_many) > 1
    assert sum(len(e.fields) for e in embeds_many) == 30
    assert " (1/" in embeds_many[0].title  # type: ignore


def test_build_radar_scan_embed() -> None:
    """Verify that build_radar_scan_embed correctly renders batch scan results with premium ANSI card formatting."""
    scan_results = [
        {
            "symbol": "AMD",
            "quote": {"c": 466.38, "dp": -10.8},
            "iv_metrics": {
                "iv_rank": 0.1,
                "expected_move_weekly": 17.05,
            },
            "skew": 1.1,
            "max_pain": {"max_pain": 492.50},
        },
        {
            "symbol": "MRVL",
            "quote": {"c": 263.47, "dp": -16.7},
            "iv_metrics": {
                "iv_rank": 63.5,
                "expected_move_weekly": 44.21,
            },
            "skew": 1.1,
            "max_pain": {"max_pain": 225.00},
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed.title == "🌌 交易員終端: 核心 AI 暨持倉批次量化雷達 (ALL)"
    assert "**🧠 核心 AI 暨持倉量化雷達**" in embed.description  # type: ignore
    assert "AMD" in embed.description  # type: ignore
    assert "MRVL" in embed.description  # type: ignore
    assert "超跌磁吸" in embed.description  # type: ignore
    assert "籌碼斷層" in embed.description  # type: ignore


def test_build_radar_scan_embed_with_none_values() -> None:
    """Verify that build_radar_scan_embed handles None values in dictionaries gracefully."""
    scan_results = [
        {
            "symbol": "CRASH",
            "quote": {"c": 100.0, "dp": 0.0},
            "iv_metrics": {
                "iv_rank": None,
                "expected_move_weekly": None,
            },
            "skew": None,
            "max_pain": {"max_pain": None},
            "gex_metrics": {
                "put_wall": None,
                "call_wall": None,
                "zero_gamma": None,
                "net_gex": None,
            },
            "psq_result": {"momentum": None},
        },
    ]

    # Should not raise TypeError: float() argument must be a string or a real number, not 'NoneType'
    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    assert len(embeds) == 1
    embed = embeds[0]
    assert "CRASH" in embed.description  # type: ignore


def test_build_post_market_intelligence_embed_empty() -> None:
    """Verify that build_post_market_intelligence_embed correctly renders when there are no report lines."""
    embeds = build_post_market_intelligence_embed(
        report_lines=[],
        hedge_analysis={},
        survival_runway=9999.0,
        sectors_data=[],
        ai_commentary="Test AI commentary",
    )
    embed = embeds[0]
    assert embed.title == "📋 報告：盤後綜合風險與 AI 策略"

    assert "🏁 財務生存跑道 (Financial Runway)" in embed.description  # type: ignore

    # Section titles are now in Field names (semantic), not in ANSI block values
    field_names = [f.name for f in embed.fields]

    assert any("持倉明細" in name for name in field_names)  # type: ignore
    assert any("宏觀風險" in name for name in field_names)  # type: ignore
    assert any("對沖績效歸因" in name for name in field_names)  # type: ignore
    assert any("🧠 AI 損益歸因與次日策略點評" in name for name in field_names)  # type: ignore

    # Verify empty-state placeholder text in field values
    positions_val = next(
        f.value
        for f in embed.fields
        if "持倉明細" in f.name  # type: ignore
    )
    assert "目前無持倉部位。" in positions_val  # type: ignore

    macro_val = next(
        f.value
        for f in embed.fields
        if "宏觀風險" in f.name  # type: ignore
    )
    assert "目前無宏觀風險數據。" in macro_val  # type: ignore


def test_build_post_market_intelligence_embed_parsed_ai_commentary() -> None:
    """Verify that build_post_market_intelligence_embed correctly parses and formats the AI commentary into three fields in Target Center style."""
    ai_commentary = (
        "1. 📊 多空大盤交叉驗證解讀\n"
        "- 第一點分析\n"
        "- 第二點分析\n"
        "2. ⚠️ 潛在陷阱與風險提示\n"
        "- 第一個風險\n"
        "- 第二個風險\n"
        "3. 🛡️ 高勝率交易策略推薦\n"
        "- 第一個策略\n"
        "- 第二個策略\n"
    )
    embeds = build_post_market_intelligence_embed(
        report_lines=[],
        hedge_analysis={},
        survival_runway=9999.0,
        sectors_data=[],
        ai_commentary=ai_commentary,
    )
    embed = embeds[0]
    field_names = [f.name for f in embed.fields]

    # AI section titles should now be in Field names
    assert any("📊 AI 多空大盤交叉驗證解讀" in name for name in field_names)  # type: ignore
    assert any("⚠️ AI 潛在陷阱與風險提示" in name for name in field_names)  # type: ignore
    assert any("🛡️ AI 高勝率交易策略推薦" in name for name in field_names)  # type: ignore
    assert not any("🧠 AI 損益歸因與次日策略點評" in name for name in field_names)  # type: ignore

    # Verify content in the corresponding field value
    market_field_val = next(
        f.value
        for f in embed.fields
        if "📊 AI 多空大盤交叉驗證解讀" in f.name  # type: ignore
    )
    assert "```ansi" in market_field_val  # type: ignore
    assert "第一點分析" in market_field_val  # type: ignore
    assert "第二點分析" in market_field_val  # type: ignore


def test_build_post_market_intelligence_embed_hedge_attribution() -> None:
    """Verify hedge_analysis data is correctly consumed and rendered in the Hedge Attribution field."""
    hedge_data = {
        "net_pnl": -150.50,
        "alpha_contribution": 200.00,
        "hedge_contribution": -350.50,
        "hedge_ratio": 0.85,
        "effectiveness": 0.75,
        "status": "OPTIMAL",
        "dynamic_tau": 0.0312,
    }
    embeds = build_post_market_intelligence_embed(
        report_lines=[],
        hedge_analysis=hedge_data,
        survival_runway=9999.0,
        sectors_data=[],
        ai_commentary="Test",
    )
    all_field_names: list[str] = []
    all_field_values: list[str] = []
    for emb in embeds:
        all_field_names.extend(f.name for f in emb.fields)  # type: ignore
        all_field_values.extend((f.value or "") for f in emb.fields)

    # Hedge Attribution section must exist
    assert any("對沖績效歸因" in name for name in all_field_names)  # type: ignore

    hedge_field_val = next(
        val
        for name, val in zip(all_field_names, all_field_values)
        if "對沖績效歸因" in name  # type: ignore
    )
    # Verify all key hedge metrics are rendered
    assert "+200.00" in hedge_field_val
    assert "-350.50" in hedge_field_val
    assert "-150.50" in hedge_field_val
    assert "OPTIMAL" in hedge_field_val
    assert "0.0312" in hedge_field_val
    assert "85.00%" in hedge_field_val
    assert "75.0%" in hedge_field_val


def test_create_covered_call_unlock_embed() -> None:
    """Verify that create_covered_call_unlock_embed correctly renders deadlock release embed."""
    # 1. With recommendations
    data_with_recs = {
        "symbol": "NVDA",
        "current_shares": 100.0,
        "current_cost": 120.0,
        "new_cost_basis": 115.0,
        "current_price": 110.0,
        "recommendations": [
            {
                "expiration": "2026-07-17",
                "strike": 125.0,
                "delta": 0.12,
                "premium": 2.50,
                "annualized_yield": 18.5,
            }
        ],
    }
    embed_with_recs = create_covered_call_unlock_embed(data_with_recs)
    assert embed_with_recs.title == "🔓 警報：物理死鎖解除與備兌建單 | NVDA"
    assert len(embed_with_recs.fields) == 3
    assert "現貨與吸籌模擬" in embed_with_recs.fields[0].name  # type: ignore
    assert "100 股" in embed_with_recs.fields[0].value  # type: ignore
    assert "$120.00" in embed_with_recs.fields[0].value  # type: ignore
    assert "$115.00" in embed_with_recs.fields[0].value  # type: ignore
    assert "推薦 Covered Call 備兌合約" in embed_with_recs.fields[1].name  # type: ignore
    assert "2026-07-17" in embed_with_recs.fields[1].value  # type: ignore
    assert "$125.00" in embed_with_recs.fields[1].value  # type: ignore
    assert "18.50%" in embed_with_recs.fields[1].value  # type: ignore

    # 2. Without recommendations
    data_no_recs = {
        "symbol": "AAPL",
        "current_shares": 50.0,
        "current_cost": 180.0,
        "new_cost_basis": 178.0,
        "current_price": 170.0,
        "recommendations": [],
    }
    embed_no_recs = create_covered_call_unlock_embed(data_no_recs)
    assert embed_no_recs.title == "🔓 警報：物理死鎖解除與備兌建單 | AAPL"
    assert len(embed_no_recs.fields) == 2
    assert "解鎖狀態與策略建議" in embed_no_recs.fields[1].name  # type: ignore
    assert "未尋獲符合條件之極虛值" in embed_no_recs.fields[1].value  # type: ignore


def test_create_watchlist_signal_embed_event_loading() -> None:
    """Verify that event loading formatting displays status and expected move note correctly."""
    from models.schemas import EnhancedWatchlistMetrics

    metrics = EnhancedWatchlistMetrics(
        symbol="MU",
        exchange="NASDAQ",
        current_price=130.0,
        buy_zone_status="🟢 買點支撐",
        buy_price_phase1=120.0,
        buy_price_phase2=115.0,
        buy_price_phase3=110.0,
        sell_zone_status="🟢 賣點壓力",
        sell_price_phase1=140.0,
        sell_price_phase2=145.0,
        sell_price_phase3=150.0,
        pe_ratio=15.0,
        rsi_14=55.0,
        atr_14=4.5,
        beta=1.2,
        ma20=128.0,
        ma50=125.0,
        ma200=115.0,
        iv_rank=85.0,
        iv_percentile=88.0,
        option_skew=5.0,
        skew_percentile=85.0,
        option_skew_state="正常",
        pcr=0.78,
        volume_poc=125.0,
        gex_max_put_wall=110.0,
        vanna_sensitivity=0.1,
        relative_strength_spy=1.05,
        iv_source="STORED_IV",
        is_premarket=False,
        volume_pcr=0.78,
        oi_pcr=1.55,
        has_earnings_event=True,
        has_macro_event=False,
    )

    embed = create_watchlist_signal_embed(
        symbol="MU",
        metrics=metrics,
        alert_level="yellow",
    )

    assert "(狀態: ⚠️ 臨近財報/快取波動率可能低估)" in embed.description  # type: ignore
    assert "備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。" in embed.description  # type: ignore
    assert "Volume PCR (即時情緒): 0.78" in embed.description  # type: ignore
    assert "OI PCR (結構防禦): 1.55" in embed.description  # type: ignore


def test_create_watchlist_signal_embed_non_degraded() -> None:
    from models.schemas import EnhancedWatchlistMetrics
    from models.quant import IVMetrics

    metrics = EnhancedWatchlistMetrics(
        symbol="AAPL",
        exchange="NASDAQ",
        current_price=150.0,
        buy_zone_status="🟢 買點支撐",
        buy_price_phase1=140.0,
        buy_price_phase2=135.0,
        buy_price_phase3=130.0,
        sell_zone_status="🟢 賣點壓力",
        sell_price_phase1=160.0,
        sell_price_phase2=165.0,
        sell_price_phase3=170.0,
        pe_ratio=30.0,
        rsi_14=50.0,
        atr_14=3.0,
        beta=1.0,
        ma20=148.0,
        ma50=145.0,
        ma200=140.0,
        iv_rank=25.0,
        iv_percentile=30.0,
        option_skew=2.5,
        skew_percentile=60.0,
        option_skew_state="正常",
        pcr=0.8,
        volume_poc=145.0,
        gex_max_put_wall=130.0,
        vanna_sensitivity=0.05,
        relative_strength_spy=1.0,
        iv_source="LIVE_IV",
        is_premarket=False,
        volume_pcr=0.8,
        oi_pcr=0.9,
    )

    iv_metrics = IVMetrics(
        symbol="AAPL",
        current_iv=0.35,
        iv_rank=25.0,
        iv_percentile=30.0,
        expected_move_weekly=5.0,
        iv_status="Normal",
        is_premarket=False,
        iv_source="LIVE_IV",
        reference_spot_price=150.0,
    )

    embed = create_watchlist_signal_embed(
        symbol="AAPL",
        metrics=metrics,
        iv_metrics=iv_metrics,
        alert_level="green",
    )

    assert embed is not None
    assert embed.title == "標的分析中心 2.0: AAPL 每半小時戰場心跳"

    desc = embed.description or ""
    # Verify exact numeric formatting

    assert "GEX PutWall (做市商底牆): $130.00 (當前價差: +15.38%)" in desc
    assert "Vol POC (籌碼控制中心): $145.00" in desc
    assert "Option Skew (期權偏斜): +2.50% (分位點: 60.0%)" in desc
    assert (
        "Implied Volatility (IV): 35.0% ｜ IV Rank: 25.0% (狀態: 正常 / 公允)" in desc
    )
    assert "本週預期波幅 (Expected Move): ±$5.00" in desc
    assert "Volume PCR (即時情緒): 0.80" in desc
    assert "OI PCR (結構防禦): 0.90" in desc


def test_create_telemetry_alignment_embeds() -> None:
    from cogs.embed_builders.order_embeds import create_telemetry_alignment_embeds

    # Define a normal item
    item = {
        "symbol": "AAPL",
        "order_id": 123,
        "order_type": "LIMIT",
        "price_label": "掛單限價",
        "current_price": 145.0,  # limit price
        "original_qty": 10.0,
        "suggested_price": 142.0,
        "suggested_qty": 10,
        "is_size_down": False,
        "holding_type_label": "LEVERAGED",
        "holding_shares": 50,
        "holding_status": "持倉中",
        "avg_cost": 140.0,
        "live_price": 146.5,
        "gain_loss_pct": 4.64,
        "put_wall": 130.0,
        "wall_dist_pct": 11.26,
        "wall_status": "上方緩衝",
        "skew_val": 2.5,
        "skew_pct": 60.0,
        "skew_status": "平穩",
        "iv_val": 35.0,
        "iv_rank": 25.0,
        "iv_status": "Normal",
        "proximity_pct": 1.02,
        "radar_status": "雷達鎖定中",
        "system_status_flag": "TELEMETRY ACTIVE",
        "system_instruction_directive": "通過實時防線，維持紀律掛單。",
        "is_premarket": False,
        "iv_source": "LIVE_IV",
        "side": "BUY",
    }

    # 1. Test single normal item
    embeds = create_telemetry_alignment_embeds([item])
    assert len(embeds) == 1
    val = embeds[0].fields[0].value
    assert "```ansi" in val  # type: ignore
    assert "\u001b[1;36m【物理防線 (The Shield)】\u001b[0m" in val  # type: ignore
    assert "選擇權偏斜 (Option Skew)" in val  # type: ignore
    assert "隱含波動率 (IV)" in val  # type: ignore

    # 2. Test pre-market degraded (no IV/Option data)
    pm_degraded_item = dict(
        item, is_premarket=True, iv_val=0.0, iv_source="UNAVAILABLE"
    )
    embeds_pm_degraded = create_telemetry_alignment_embeds([pm_degraded_item])
    assert len(embeds_pm_degraded) == 1
    val_pm_degraded = embeds_pm_degraded[0].fields[0].value
    assert "[盤前數據未更新]" in val_pm_degraded  # type: ignore
    assert "等待開盤" in val_pm_degraded  # type: ignore
    assert "--%" in val_pm_degraded  # type: ignore

    # 3. Test pre-market with STORED_IV fallback
    pm_stored_item = dict(item, is_premarket=True, iv_val=35.0, iv_source="STORED_IV")
    embeds_pm_stored = create_telemetry_alignment_embeds([pm_stored_item])
    assert len(embeds_pm_stored) == 1
    val_pm_stored = embeds_pm_stored[0].fields[0].value
    assert "[盤前/前日收盤]" in val_pm_stored  # type: ignore
    assert "(前日收盤)" in val_pm_stored  # type: ignore

    # 4. Test pre-market with HV_PROXY fallback
    pm_hv_item = dict(item, is_premarket=True, iv_val=42.0, iv_source="HV_PROXY")
    embeds_pm_hv = create_telemetry_alignment_embeds([pm_hv_item])
    assert len(embeds_pm_hv) == 1
    val_pm_hv = embeds_pm_hv[0].fields[0].value
    assert "[盤前/HV代理]" in val_pm_hv  # type: ignore
    assert "(歷史波動率代理)" in val_pm_hv  # type: ignore

    # 5. Test pagination (16 items -> should produce at least 2 embeds if split or chunked)
    items_16 = [dict(item, order_id=i) for i in range(16)]
    embeds_16 = create_telemetry_alignment_embeds(items_16)
    assert len(embeds_16) >= 2
    assert "(第 1/" in embeds_16[0].title  # type: ignore
    assert "頁)" in embeds_16[0].title  # type: ignore


def test_create_tactical_symbol_embed_string_expected_move() -> None:
    # 測試傳入字串型別的 expected_move_weekly 不會導致 __round__ 錯誤
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
    import pytest

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": "--",  # Invalid type for round()
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 100.0},
    }

    try:
        embed = create_tactical_symbol_embed(data)
        assert embed is not None
        assert isinstance(embed.title, str)
        assert "NVDA" in embed.title
    except TypeError as e:
        pytest.fail(f"Embed creation failed with type error: {e}")


def test_create_tactical_symbol_embed_string_reference_price() -> None:
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
    import pytest

    data = {
        "symbol": "DDOG",
        "iv_data": {
            "current_iv": 0.45,
            "iv_rank": 35.0,
            "iv_percentile": 40.0,
            "expected_move_weekly": "5.2",
            "iv_status": "Normal",
        },
        "expected_move_context": {
            "reference_price": "--",
        },
    }

    try:
        embed = create_tactical_symbol_embed(data)
        assert embed is not None
        assert isinstance(embed.title, str)
        assert "DDOG" in embed.title
    except (TypeError, ValueError) as e:
        pytest.fail(f"Embed creation failed with malformed reference price: {e}")


def test_create_tactical_symbol_embed_tolerates_string_iv_and_macro_tte() -> None:
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
    import pytest
    import types

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": "--",
            "iv_rank": "--",
            "iv_percentile": "--",
            "expected_move_weekly": "--",
            "iv_status": "Normal",
        },
        "expected_move_context": "--",
        "catalysts": [
            types.SimpleNamespace(
                time="2026-08-10T12:30:00Z",
                event="Nonfarm Payrolls",
                tte_hours="--",
            )
        ],
    }

    try:
        embed = create_tactical_symbol_embed(data)
        assert embed is not None
        assert isinstance(embed.title, str)
        assert "NVDA" in embed.title
    except Exception as e:
        pytest.fail(f"Embed creation failed with degraded string payloads: {e}")
