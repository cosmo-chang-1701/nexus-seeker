import discord
from typing import Any, Dict, List
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from cogs.embed_builder import (
    get_embed_length,
    chunk_embeds,
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
    create_memory_alert_embed,
    create_max_pain_embed,
    create_polymarket_whale_alert_embed,
    create_polymarket_status_embed,
    create_polymarket_list_embed,
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
    build_pre_market_briefing_embed,
    create_macro_scan_embed,
    create_fomc_escape_window_embed,
    create_stress_test_embed,
)
from models.schemas import WatchlistOptionLeg, WatchlistOptionPlan


def get_embed_text(embed: Any) -> str:
    if embed is None:
        return ""
    parts = [str(embed.description or "")]
    for field in getattr(embed, "fields", []):
        parts.append(str(field.name))
        parts.append(str(field.value))
    return "\n".join(parts)


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
    assert embed.title == "💰 Nexus Seeker | 現貨持倉清單"

    # Extract lines in code block
    desc_field = embed.fields[0].value
    assert "標的" in desc_field  # type: ignore
    assert "現價" in desc_field  # type: ignore
    assert "AAPL" in desc_field  # type: ignore
    assert "$160.00" in desc_field  # type: ignore


def test_create_holdings_embed_shows_boxx_allocation_pct() -> None:
    """boxx_allocation_pct 有設定時應顯示於配置欄位；未設定時不應顯示 🧱 標記。"""
    holdings_data = [
        {
            "symbol": "VOO",
            "quantity": 10,
            "avg_cost": 400.0,
            "current_price": 420.0,
            "asset_class": "CORE",
            "target_allocation_pct": 0.5,
            "boxx_allocation_pct": 0.7,
        },
        {
            "symbol": "AAPL",
            "quantity": 5,
            "avg_cost": 150.0,
            "current_price": 160.0,
        },
    ]
    embed = create_holdings_embed(holdings_data, total_capital=100000.0)
    desc_field = embed.fields[0].value
    assert "🧱70%" in desc_field  # type: ignore


def test_create_holdings_embed_shows_target_allocation_suggestion_hint() -> None:
    """target_allocation_pct 未設定但帶有 suggested_target_allocation_pct 時，
    應以獨立提示欄位顯示建議值（僅供參考，不進入配置表格本身，避免與已生效的
    數值混淆）。已設定 target_allocation_pct 的持倉則不應觸發提示。"""
    holdings_data = [
        {
            "symbol": "VOO",
            "quantity": 10,
            "avg_cost": 400.0,
            "current_price": 420.0,
            "asset_class": "CORE",
            "suggested_target_allocation_pct": 50.0,
        },
        {
            "symbol": "QQQ",
            "quantity": 5,
            "avg_cost": 300.0,
            "current_price": 310.0,
            "asset_class": "CORE",
            "target_allocation_pct": 0.6,
        },
    ]
    embed = create_holdings_embed(holdings_data, total_capital=100000.0)
    hint_fields = [f for f in embed.fields if "核心資金部署建議" in f.name]  # type: ignore
    assert len(hint_fields) == 1
    assert "VOO" in hint_fields[0].value  # type: ignore
    assert "50%" in hint_fields[0].value  # type: ignore
    assert "QQQ" not in hint_fields[0].value  # type: ignore


def test_create_holdings_embed_shows_acquired_at() -> None:
    """acquired_at 有設定時應顯示建倉日期；未設定時應顯示佔位符號「—」。"""
    holdings_data = [
        {
            "symbol": "AAPL",
            "quantity": 10,
            "avg_cost": 150.0,
            "current_price": 160.0,
            "acquired_at": "2023-05-01",
        },
        {
            "symbol": "VOO",
            "quantity": 5,
            "avg_cost": 400.0,
            "current_price": 420.0,
        },
    ]
    embed = create_holdings_embed(holdings_data, total_capital=100000.0)
    desc_field = embed.fields[0].value
    assert "建倉日" in desc_field  # type: ignore
    assert "2023-05-01" in desc_field  # type: ignore
    assert "—" in desc_field  # type: ignore


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
    assert embed.title == "💰 Nexus Seeker | 實單持倉清單 (包含帳面損益)"

    desc_field = embed.fields[0].value
    assert desc_field is not None
    assert "數量" in desc_field
    assert "現價" in desc_field
    assert "  6.50" in desc_field  # Visual formatting check
    assert "  -1" in desc_field  # Visual formatting check for negative quantity


def test_create_portfolio_report_embed() -> None:
    report_lines = [
        "🔹 **AAPL** ｜ `2026-06-19` ｜ `$150.0` **CALL**\n├─ 💰 成本: `$5.00` ｜ 📈 現價: `$6.50`\n├─ 🟢 損益: **+30.00%**\n├─ ⏳ DTE: `29` 天 ｜ 秤⚖️ SPY Δ: `+32.50`\n└─ 🎯 動作: HOLD",
        "🌐 【宏觀風險與資金水位報告】",
        "Beta-Weighted Delta: +120.0",
    ]

    embed = create_portfolio_report_embed(report_lines, survival_runway=120)
    assert embed.title == "📊 Nexus Seeker 盤後風險結算報告"
    assert "🏁 財務生存跑道 (Financial Runway)" in (get_embed_text(embed) or "")
    assert "Debit Cost" in (get_embed_text(embed) or "")
    assert "Credit Cash" in (get_embed_text(embed) or "")
    assert "Unrealized PnL" in (get_embed_text(embed) or "")
    assert "$500.00 USD" in (get_embed_text(embed) or "")

    field_names = [f.name for f in embed.fields]
    assert any("資金與實質暴露 (Financial Summary)" in name for name in field_names)
    assert any("持倉明細 (Positions)" in name for name in field_names)
    assert any("【宏觀風險與資金水位報告】" in name for name in field_names)

    positions_value = next(
        f.value for f in embed.fields if "持倉明細 (Positions)" in f.name
    )
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
    assert "更新批次" in (get_embed_text(embed) or "")
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
    assert "SPY 現價" in (get_embed_text(embed) or "")
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
    assert "警報：Nexus 波動率優勢 (廉價選擇權) | AAPL" in embed.title  # type: ignore

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
    assert "#12" in get_embed_text(embed)
    assert embed.fields[0].value == "`SPY`"
    assert embed.fields[1].value == "`8`"


def test_create_hedge_list_embed() -> None:
    rows = [
        (1, 22.5, 8, "PENDING", "2026-05-21 10:00:00"),
        (2, 18.0, 5, "EXECUTED", "2026-05-20 09:00:00"),
    ]
    embed = create_hedge_list_embed(rows)
    assert embed.title == "📜 最近對沖警報列表"
    assert "#1" in get_embed_text(embed)
    assert "22.50" in get_embed_text(embed)
    assert "⏳" in get_embed_text(embed)
    assert "✅" in get_embed_text(embed)


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
    assert "Aggressive" in get_embed_text(embed)
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
        embed.title
        == "📊 標的分析中心 2.0: NVDA 每半小時戰場心跳 [數據未更新/降級模式]"
    )

    assert "物理籌碼牆與邊緣偵測 (Market Footprints)" in get_embed_text(embed)
    assert "心跳：期權結構與波動率" in get_embed_text(embed)
    assert "結算與目標 (Target Lock)" in get_embed_text(embed)
    assert (
        "既有現貨持倉: 120 股 ｜ 平均成本: $150.00 ｜ 當前損益: +10.00%"
        in get_embed_text(embed)
    )
    assert "操盤執行指南: 可先以 Bull Put Spread 佈局。" in get_embed_text(embed)
    assert "**⚙️ 量化 Skew 解析**" in get_embed_text(embed)
    assert "Skew: +6.20% ｜ ⚠️ 預警性對沖 (Put 昂貴)" in get_embed_text(embed)


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
        embed.title
        == "📊 標的分析中心 2.0: INTC 每半小時戰場心跳 [數據未更新/降級模式]"
    )
    assert (
        "既有現貨持倉: 100 股 ｜ 平均成本: $113.50 ｜ 當前損益: -3.97%"
        in get_embed_text(embed)
    )
    assert "操盤執行指南: Covered Call 鎖利。" in get_embed_text(embed)
    assert "**⚙️ 量化 Skew 解析**" in get_embed_text(embed)
    assert "Skew: -5.10% ｜ 右偏 (Call 昂貴)" in get_embed_text(embed)


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
    assert "追蹤標的" in (get_embed_text(embed) or "")
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
    assert embed.title == "🆘 【系統緊急警報：記憶體不足】 - Droplet (主節點)"
    assert "91.2%" in get_embed_text(embed)
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
    assert "Will NVDA beat earnings?" in get_embed_text(embed)
    assert "方向性押注" in get_embed_text(embed)
    assert "預測性對沖建議" in get_embed_text(embed)
    assert "nvda-earnings" in get_embed_text(embed)


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
    assert "✅ 運行中" in get_embed_text(embed)


def test_create_polymarket_list_embed_empty() -> None:
    embeds = create_polymarket_list_embed([])
    assert len(embeds) == 1
    assert "Polymarket 巨鯨意圖圖譜" in embeds[0].title  # type: ignore
    assert "目前沒有監控中的市場。" in embeds[0].description  # type: ignore


def test_create_polymarket_list_embed_full_text_and_links() -> None:
    long_question = (
        "Will the Federal Open Market Committee (FOMC) announce a decrease of at least "
        "25 basis points in the target range for the federal funds rate at the March meeting?"
    )
    markets = [
        {
            "question": long_question,
            "event_slug": "fed-interest-rate-march-2026",
            "tokens": [
                {"outcome": "Yes", "price": 0.65},
                {"outcome": "No", "price": 0.35},
            ],
        }
    ]
    embeds = create_polymarket_list_embed(markets)
    assert len(embeds) == 1
    desc = embeds[0].description
    assert desc is not None
    # 驗證沒有 55 字元截斷，包含完整長問題
    assert long_question in desc
    # 驗證 Markdown 超連結
    assert "https://polymarket.com/event/fed-interest-rate-march-2026" in desc
    # 驗證勝率與價格格式化
    assert "**Yes**: `65%` ($0.65)" in desc
    assert "**No**: `35%` ($0.35)" in desc


def test_create_polymarket_list_embed_pagination() -> None:
    markets = [
        {
            "question": f"Market Question Number {i}",
            "slug": f"market-question-{i}",
            "tokens": [{"outcome": "Yes", "price": 0.5}],
        }
        for i in range(1, 11)
    ]
    # 設定每頁 4 個，總共 10 個市場應產生 3 頁
    embeds = create_polymarket_list_embed(markets, chunk_size=4)
    assert len(embeds) == 3
    assert "(第 1/3 頁)" in embeds[0].title  # type: ignore
    assert "(第 2/3 頁)" in embeds[1].title  # type: ignore
    assert "(第 3/3 頁)" in embeds[2].title  # type: ignore
    assert "Market Question Number 1" in embeds[0].description  # type: ignore
    assert "Market Question Number 5" in embeds[1].description  # type: ignore
    assert "Market Question Number 9" in embeds[2].description  # type: ignore


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
    assert "收斂中" in (get_embed_text(embed) or "")


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
    # 1. 主節點 + 邊緣節點在線測試 (極度危險)
    embed_danger = create_system_health_embed(
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
        edge_stats={
            "os_system": "Darwin",
            "memory_percent": 45.0,
            "memory_available_mb": 8192.0,
            "cpu_percent": 12.0,
            "process_memory_mb": 110.0,
            "disk_percent": 60.0,
            "disk_free_gb": 150.0,
            "swap_percent": 0.0,
            "battery": {"percent": 90.0, "power_plugged": True},
        },
    )
    assert embed_danger.title == "🖥️ Nexus Seeker 分散式系統健康診斷"
    main_val = embed_danger.fields[0].value
    assert "🧠 **記憶體 (RAM)**: `96.0%`" in main_val  # type: ignore
    assert "⚡ **CPU 負載**: `33.0%`" in main_val  # type: ignore
    assert "📌 **程序占用 (RSS)**: `512.0 MB`" in main_val  # type: ignore
    assert "💿 **硬碟空間**: `97.0%`" in main_val  # type: ignore
    assert "🔄 **Swap 占用**: `0.0%`" in main_val  # type: ignore
    assert "📦 **快取統計**:" in main_val  # type: ignore
    assert "🔹 SMA/EMA 快取: `120/87`" in main_val  # type: ignore

    edge_val = embed_danger.fields[1].value
    assert "🧠 **記憶體 (RAM)**: `45.0%`" in edge_val  # type: ignore
    assert "🔌 **電力狀態**: `90.0%` (插電中)" in edge_val  # type: ignore
    assert "🆘 **極度危險 (OOM 警告)**" in embed_danger.fields[-1].value  # type: ignore

    # 2. 邊緣節點離線 + 健康狀態優良測試
    embed_healthy = create_system_health_embed(
        memory_percent=50.0,
        memory_available_mb=1024.0,
        cpu_percent=15.0,
        process_memory_mb=150.0,
        disk_percent=40.0,
        disk_free_gb=20.0,
        sma_cache_size=10,
        ema_cache_size=10,
        edge_stats=None,
    )
    assert "⚠️ **連線狀態**: `離線或無法連線`" in embed_healthy.fields[1].value  # type: ignore
    assert "✅ **狀態優良**" in embed_healthy.fields[-1].value  # type: ignore


def test_create_asset_promotion_embed() -> None:
    embed = create_asset_promotion_embed("AAPL", "2026-06-19", 150.0, "call", 2, 5.5)
    assert embed.title == "🌌 Nexus | 資產晉升成功"
    assert "AAPL" in get_embed_text(embed)
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
    assert "AAPL" in get_embed_text(embed)
    assert "180%" in embed.fields[0].value  # type: ignore


def test_create_gamma_fragility_embed() -> None:
    embed = create_gamma_fragility_embed({"net_gamma": -25.5, "threshold": -20})
    assert "🆘 警報：Gamma 脆弱性與斷層" in embed.title  # type: ignore
    assert "`-25.5`" == embed.fields[0].value
    assert "`-20`" == embed.fields[1].value


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
    assert "TSLA" in get_embed_text(embed)
    assert "12.50%" in embed.fields[3].value  # type: ignore
    assert "賣出 10 股 SPY" in embed.fields[4].value  # type: ignore


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
    pos_fields = [f for f in embed.fields if "持倉明細 (Positions)" in f.name]
    assert len(pos_fields) > 1
    assert "持倉明細 (Positions) (1/" in pos_fields[0].name
    for f in pos_fields:
        assert len(f.value) <= 1024
        assert "```ansi" in f.value


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
    """Verify that create_media_sentiment_embed renders institutional news, resonance radar, and reddit consensus correctly."""
    symbol = "TSLA"
    news_text = "Tesla stock spikes on earnings beat"
    reddit_text = "To the moon! Bullish sentiment on TSLA options"

    embed = create_media_sentiment_embed(symbol, news_text, reddit_text)
    assert embed.title == "🎭 TSLA 輿情與社群大盤掃描 (Media & Social)"
    fields = {f.name: str(f.value) for f in embed.fields}
    assert "📊 輿情與期權共振雷達" in fields
    assert "🔥 Reddit 社群熱門討論" in fields
    assert reddit_text in fields["🔥 Reddit 社群熱門討論"]
    assert "📰 即時市場新聞與權威報導" in fields
    assert news_text in fields["📰 即時市場新聞與權威報導"]


def test_create_media_sentiment_embed_untruncated_and_line_breaks() -> None:
    """Verify that create_media_sentiment_embed does not truncate long text and adds line breaks below each block."""
    symbol = "NVDA"
    long_headline = "NVIDIA Announces Groundbreaking Next-Generation Quantum AI Architecture with Unprecedented Computing Efficiency Across Major Cloud Providers Worldwide"
    long_reddit_title = "Deep Dive Analysis: Why NVIDIA's Massive Free Cash Flow Generation and Software Moat Will Outperform Consensus Estimates Over the Next Five Years"
    long_poly_summary = "🟢 78.5% 巨鯨看多 (NVIDIA will achieve record revenue in upcoming fiscal quarter and maintain market leadership)"

    news_items = [
        {
            "source": "Reuters",
            "headline": long_headline,
            "url": "https://reuters.com/nvda-news",
            "time_tag": "10分鐘前",
        }
    ]
    reddit_posts = [
        {
            "subreddit": "wallstreetbets",
            "title": long_reddit_title,
            "url": "https://reddit.com/r/wallstreetbets/nvda_post",
        }
    ]
    poly_odds = f"[{long_poly_summary}](https://polymarket.com/nvda) (Yes: 78.5%)"

    embed = create_media_sentiment_embed(
        symbol,
        news_items=news_items,
        reddit_posts=reddit_posts,
        polymarket_odds=poly_odds,
        polymarket_summary=long_poly_summary,
    )

    fields = {f.name: str(f.value) for f in embed.fields}

    # 1. Check ANSI panel contains full Polymarket summary without 55-char truncation and ends with \n\u200b
    radar_val = fields["📊 輿情與期權共振雷達"]
    assert "NVIDIA will achieve record revenue" in radar_val
    assert radar_val.endswith("\n\u200b")

    # 2. Check Polymarket field has full text and ends with \n\u200b
    poly_val = fields["🐋 Polymarket 預測事件"]
    assert long_poly_summary in poly_val
    assert poly_val.endswith("\n\u200b")

    # 3. Check Reddit field has full long title without 65-char truncation and ends with \n\u200b
    reddit_val = fields["🔥 Reddit 社群熱門討論"]
    assert long_reddit_title in reddit_val
    assert "…" not in reddit_val
    assert reddit_val.endswith("\n\u200b")

    # 4. Check News field has full long headline without 65-char truncation and ends with \n\u200b
    news_val = fields["📰 即時市場新聞與權威報導"]
    assert long_headline in news_val
    assert "…" not in news_val
    assert news_val.endswith("\n\u200b")


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
    assert "🧠 核心 AI 暨持倉量化雷達" in get_embed_text(embed)
    assert "AMD" in get_embed_text(embed)
    assert "MRVL" in get_embed_text(embed)
    assert "超跌磁吸" in get_embed_text(embed)
    assert "籌碼斷層" in get_embed_text(embed)


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
    assert "CRASH" in get_embed_text(embed)


def test_build_radar_scan_embed_marks_stale_max_pain_with_freshness_prefix() -> None:
    """market_cache.is_stale=True 應在標的列渲染 🕓 新鮮度前綴，並於 Real-time
    Insights 附上提示，且不與代表結構性風險的 ⚠️ 前綴混用。"""
    scan_results = [
        {
            "symbol": "STALESYM",
            "quote": {"c": 100.0, "dp": 0.0},
            "iv_metrics": {"iv_rank": 40.0, "expected_move_weekly": 3.0},
            "skew": 0.0,
            "max_pain": {
                "max_pain": 100.0,
                "distance_pct": 0.0,
                "is_stale": True,
                "calculation_mode": "OI",
                "is_degraded": False,
                "circuit_breaker_triggered": False,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🕓" in text
    assert "STALESYM" in text
    assert "本列數據" in text


def test_build_radar_scan_embed_marks_volume_degraded_max_pain() -> None:
    """market_cache.calculation_mode == 'Volume' (OI 資料不可用降級) 應同樣觸發
    🕓 新鮮度標記，即使 is_stale 本身為 False。"""
    scan_results = [
        {
            "symbol": "VOLDEG",
            "quote": {"c": 50.0, "dp": 0.0},
            "iv_metrics": {"iv_rank": 30.0, "expected_move_weekly": 1.5},
            "skew": 0.0,
            "max_pain": {
                "max_pain": 50.0,
                "distance_pct": 0.0,
                "is_stale": False,
                "calculation_mode": "Volume",
                "is_degraded": True,
                "circuit_breaker_triggered": False,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🕓" in text
    assert "VOLDEG" in text


def test_build_radar_scan_embed_marks_stale_gex_cache() -> None:
    """gex_metrics._is_stale_cache=True（GEX edge scraper 快取過期回退）應觸發
    同一套 🕓 新鮮度標記，即使 Max Pain 本身是新鮮的。"""
    scan_results = [
        {
            "symbol": "GEXSTALE",
            "quote": {"c": 75.0, "dp": 0.0},
            "iv_metrics": {"iv_rank": 20.0, "expected_move_weekly": 2.0},
            "skew": 0.0,
            "max_pain": {
                "max_pain": 75.0,
                "distance_pct": 0.0,
                "is_stale": False,
                "calculation_mode": "OI",
                "is_degraded": False,
                "circuit_breaker_triggered": False,
            },
            "gex_metrics": {
                "put_wall": 70.0,
                "call_wall": 80.0,
                "net_gex": 1_000_000.0,
                "_is_stale_cache": True,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🕓" in text
    assert "GEXSTALE" in text


def test_build_radar_scan_embed_marks_premarket_stale_gex_as_benign() -> None:
    """盤前（iv_metrics.is_premarket=True）且僅因年齡過期的 gex_is_stale/
    uoa_is_stale 觸發時，選擇權市場尚未開盤本就不會有更新數據，應改用
    🌙「前日收盤」的中性提示文字，而非暗示資料異常的「請謹慎採信灰階建議」。"""
    scan_results = [
        {
            "symbol": "PMSYM",
            "quote": {"c": 75.0, "dp": 0.0},
            "iv_metrics": {
                "iv_rank": 20.0,
                "expected_move_weekly": 2.0,
                "is_premarket": True,
            },
            "skew": 0.0,
            "max_pain": {
                "max_pain": 75.0,
                "distance_pct": 0.0,
                "is_stale": False,
                "calculation_mode": "OI",
                "is_degraded": False,
                "circuit_breaker_triggered": False,
            },
            "gex_metrics": {
                "put_wall": 70.0,
                "call_wall": 80.0,
                "net_gex": 1_000_000.0,
                "_is_stale_cache": True,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🌙" in text
    assert "PMSYM" in text
    assert "盤前/前日收盤" in text
    assert "請謹慎採信灰階建議" not in text


def test_build_radar_scan_embed_premarket_genuine_degradation_keeps_strong_warning() -> (
    None
):
    """即使處於盤前，若 market_cache.is_stale=True（SWR 重算失敗回退舊值）等
    代表真正資料品質問題的旗標成立，仍應維持原本較強烈的警語，不得被盤前的
    「屬正常現象」中性提示掩蓋。"""
    scan_results = [
        {
            "symbol": "PMBAD",
            "quote": {"c": 75.0, "dp": 0.0},
            "iv_metrics": {
                "iv_rank": 20.0,
                "expected_move_weekly": 2.0,
                "is_premarket": True,
            },
            "skew": 0.0,
            "max_pain": {
                "max_pain": 75.0,
                "distance_pct": 0.0,
                "is_stale": True,
                "calculation_mode": "OI",
                "is_degraded": False,
                "circuit_breaker_triggered": False,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🕓" in text
    assert "PMBAD" in text
    assert "請謹慎採信灰階建議" in text


def test_build_radar_scan_embed_no_freshness_marker_when_data_is_fresh() -> None:
    """基準案例：所有新鮮度旗標皆為 False/None 時，該標的列本身不應被標上 🕓
    前綴，也不應觸發即時聯動警示提示（表格頁尾的固定圖例說明文字本身一律存在，
    不在本測試驗證範圍內）。"""
    scan_results = [
        {
            "symbol": "FRESHSYM",
            "quote": {"c": 60.0, "dp": 0.0},
            "iv_metrics": {"iv_rank": 45.0, "expected_move_weekly": 2.0},
            "skew": 0.0,
            "max_pain": {
                "max_pain": 60.0,
                "distance_pct": 0.0,
                "is_stale": False,
                "calculation_mode": "OI",
                "is_degraded": False,
                "circuit_breaker_triggered": False,
            },
            "gex_metrics": {
                "put_wall": 55.0,
                "call_wall": 65.0,
                "net_gex": 500_000.0,
                "_is_stale_cache": False,
            },
        },
    ]

    embeds = build_radar_scan_embed(scan_results, "ALL", 12345)
    text = get_embed_text(embeds[0])
    assert "🕓 FRESHSYM" not in text
    assert "本列數據" not in text


def test_build_radar_scan_embed_renders_field_formulas_consistently() -> None:
    """驗算雷達主欄位算式：EM Pos%、D-MP%、DTE/Event Prem、最近結構牆位距離。"""
    scan_results = [
        {
            "symbol": "AAPL",
            "quote": {"c": 105.0, "dp": 1.5},
            "iv_metrics": {
                "iv_rank": 60.0,
                "expected_move_weekly": 10.0,
                "expected_move_lower": 95.0,
                "expected_move_upper": 115.0,
                "iv_term_structure_status": "Backwardation",
                "term_structure_ratio": 1.2,
            },
            "dte_er": 7,
            "skew": 1.1,
            "skew_percentile": 80.0,
            "max_pain": {"max_pain": 100.0},
            "uoa": [],
            "gex_profile_data": {
                "put_wall": 104.0,
                "call_wall": 130.0,
                "zero_gamma": 102.0,
                "net_gex": 20_000_000.0,
            },
            "psq_result": {
                "is_squeezing": True,
                "momentum": 2.5,
                "momentum_value": 2.5,
                "signal_direction": "🟢",
            },
            "dp_poc": 104.0,
            "month_max_pains": [],
        }
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    assert len(embeds) == 1
    desc = get_embed_text(embeds[0])

    # Z-Score = (50 - 50) / 50 = +0.00σ
    assert "+0.00σ" in desc


def test_build_radar_scan_embed_rebuilds_expected_move_bounds_from_reference_price() -> (
    None
):
    """當 EM 上下緣缺失或無效時，應以 reference_price ± expected_move_weekly 回推。"""
    scan_results = [
        {
            "symbol": "MSFT",
            "quote": {"c": 100.0, "dp": 0.5},
            "iv_metrics": {
                "iv_rank": 40.0,
                "expected_move_weekly": 8.0,
                "expected_move_lower": 0.0,
                "expected_move_upper": 0.0,
                "reference_price": 100.0,
                "iv_term_structure_status": "Normal",
                "term_structure_ratio": 1.0,
            },
            "dte_er": 5,
            "skew": -0.8,
            "skew_percentile": 30.0,
            "max_pain": {"max_pain": 100.0},
            "uoa": [],
            "gex_profile_data": {
                "put_wall": 95.0,
                "call_wall": 120.0,
                "zero_gamma": 98.0,
                "net_gex": 5_000_000.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 0.0,
                "momentum_value": 0.0,
                "signal_direction": "⚪",
            },
            "dp_poc": 96.0,
            "month_max_pains": [],
        }
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    desc = get_embed_text(embeds[0])
    # 由 reference_price=100 和 EM=8 回推上下緣 92/108，EM Pos% 應為 50%，對應 Z-Score +0.00σ
    assert "+0.00σ" in desc


def test_build_radar_scan_embed_gp_wall_polarity_dynamics() -> None:
    """驗證 G/P-Wall(±) 欄位極性、N/A Fallback、IV 策略負 Gamma 熔斷與 ⚠️ 標記（以 SPCX 與 CRWV 為驗證標的）。"""
    # 案例 A: SPCX 現價 $106.29 實體跌破 PutWall $108.0，無 CallWall
    scan_results_below_pw = [
        {
            "symbol": "SPCX",
            "quote": {"c": 106.29, "dp": -1.5},
            "iv_metrics": {
                "iv_rank": 35.0,
                "expected_move_weekly": 5.0,
                "expected_move_lower": 102.0,
                "expected_move_upper": 112.0,
            },
            "dte_er": 5,
            "skew": 0.5,
            "skew_percentile": 60.0,
            "max_pain": {"max_pain": 110.0},
            "uoa": [],
            "gex_metrics": {
                "put_wall": 108.0,
                "call_wall": 0.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": -1.2,
                "signal_direction": "🔴",
            },
        }
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds_a = build_radar_scan_embed(scan_results_below_pw, "ALL", 12345)

    desc_a = get_embed_text(embeds_a[0])
    # 1. 現價跌破 PutWall，G/P-Wall 應切換為 (-) 負 Gamma 極性，且 CallWall 無數據時顯示 N/A
    assert "(-) N/A / $108.0" in desc_a
    # 2. Neg-GEX 欄位為 -1.6%
    assert "-1.6%" in desc_a
    # 3. 負 Gamma 踩踏區 IV 策略啟動風控熔斷，強制顯示「🔴賣方禁售」
    assert "🔴賣方禁售" in desc_a
    # 4. 觸發負 Gamma 踩踏/異常，標的欄位動態渲染為「⚠️ SPCX」
    assert "⚠️ SPCX" in desc_a

    # 案例 B: SPCX 現價 $110.0 高於 PutWall $108.0 且 net_gex > 0
    scan_results_above_pw = [
        {
            "symbol": "SPCX",
            "quote": {"c": 110.0, "dp": 1.2},
            "iv_metrics": {
                "iv_rank": 35.0,
                "expected_move_weekly": 5.0,
                "expected_move_lower": 105.0,
                "expected_move_upper": 115.0,
            },
            "dte_er": 5,
            "skew": 0.5,
            "skew_percentile": 40.0,
            "max_pain": {"max_pain": 110.0},
            "uoa": [],
            "gex_profile_data": {
                "put_wall": 108.0,
                "call_wall": 120.0,
                "net_gex": 10_000_000.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 1.5,
                "signal_direction": "🟢",
            },
        }
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds_b = build_radar_scan_embed(scan_results_above_pw, "ALL", 12345)

    desc_b = get_embed_text(embeds_b[0])
    assert "(+) $120.0 / $108.0" in desc_b
    assert "+1.9%" in desc_b
    assert "🟢適宜賣方" in desc_b

    # 案例 C: CRWV 實盤極端偏離與跌破底牆場景驗證
    scan_results_crwv = [
        {
            "symbol": "CRWV",
            "quote": {"c": 106.29, "dp": -1.34},
            "iv_metrics": {
                "iv_rank": 35.0,
                "expected_move_weekly": 6.0,
                "expected_move_lower": 100.0,
                "expected_move_upper": 112.0,
            },
            "dte_er": 5,
            "skew": 0.0,
            "skew_percentile": 50.0,
            "max_pain": {"max_pain": 139.15},  # 偏離 > 20%
            "uoa": [],
            "gex_metrics": {
                "put_wall": 108.0,
                "call_wall": 0.0,
                "net_gex": 0.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 0.0,
                "signal_direction": "⚪",
            },
        }
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds_crwv = build_radar_scan_embed(scan_results_crwv, "ALL", 12345)

    desc_crwv = get_embed_text(embeds_crwv[0])
    assert "⚠️ CRWV" in desc_crwv
    assert "(-) N/A / $108.0" in desc_crwv
    assert "🔴賣方禁售" in desc_crwv
    assert "-1.6%" in desc_crwv
    assert "現貨續抱" in desc_crwv
    assert "嚴守15分K收盤" in desc_crwv


@contextmanager
def _triple_confluence_patches() -> Any:
    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        yield


def _triple_confluence_base_result(
    *,
    skew_percentile: float,
    month_max_pains: List[Dict[str, Any]],
    uoa: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """三重結構性風險合流測試共用的最小可運作 scan_result：
    price=110、put_wall=90、call_wall=130，確保不會誤觸其他更高優先序的
    灰階戰術建議分支（跌破底牆/CallWall/薄弱紙牆/跌穿LVN），僅留下待測變因
    （skew_percentile / month_max_pains / uoa）。"""
    return {
        "symbol": "TSTX",
        "quote": {"c": 110.0, "dp": 0.5},
        "iv_metrics": {
            "iv_rank": 50.0,
            "expected_move_weekly": 5.0,
            "expected_move_lower": 105.0,
            "expected_move_upper": 115.0,
            "term_structure_ratio": 0.95,
            "iv_term_structure_status": "Contango",
        },
        "dte_er": 40,
        "skew": 1.0,
        "skew_percentile": skew_percentile,
        "max_pain": {"max_pain": 100.0},
        "uoa": uoa,
        "gex_metrics": {
            "put_wall": 90.0,
            "call_wall": 130.0,
            "net_gex": 3_000_000.0,
        },
        "psq_result": {
            "is_squeezing": False,
            "momentum": -0.5,
            "momentum_value": -0.5,
            "signal_direction": "⚪",
        },
        "month_max_pains": month_max_pains,
    }


def test_build_radar_scan_embed_triple_structural_risk_confluence() -> None:
    """正向案例：Skew 98% 極端避險背離 + 次週 Max Pain 向下引力 + 無 DTE≥7 機構買盤支撐，應合流觸發複合警示。"""
    far_expiry = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    scan_results = [
        _triple_confluence_base_result(
            skew_percentile=98.0,
            month_max_pains=[
                {"expiry": far_expiry, "max_pain": 95.0}
            ],  # dev ≈ +15.8%, dte=10
            uoa=[],
        )
    ]

    with _triple_confluence_patches():
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    desc = get_embed_text(embeds[0])
    assert "🚨 三重結構性風險合流：避險背離+痛點引力+機構真空，嚴禁抄底" in desc
    assert "結構性風險合流" in desc


def test_build_radar_scan_embed_triple_confluence_requires_all_three_conditions() -> (
    None
):
    """回歸案例 A：僅 Skew 98% 極端分位成立，缺乏 Max Pain 引力與 UOA 條件，應落回既有的 skew>90 防洗盤分支。"""
    scan_results = [
        _triple_confluence_base_result(
            skew_percentile=98.0,
            month_max_pains=[],
            uoa=[],
        )
    ]

    with _triple_confluence_patches():
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    desc = get_embed_text(embeds[0])
    assert "三重結構性風險合流" not in desc
    assert "🛑 防洗盤處置，嚴守 15 分鐘實體 K 線撤退線" in desc


def test_build_radar_scan_embed_triple_confluence_blocked_by_institutional_buy_support() -> (
    None
):
    """回歸案例 B：Skew 與 Max Pain 引力皆成立，但存在 DTE≥7 的 BTO CALL 機構買盤，應視為有支撐而不觸發複合警示。"""
    far_expiry = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    scan_results = [
        _triple_confluence_base_result(
            skew_percentile=98.0,
            month_max_pains=[{"expiry": far_expiry, "max_pain": 95.0}],
            uoa=[
                {
                    "symbol": "TSTX",
                    "expiry": far_expiry,
                    "strike": 115.0,
                    "type": "CALL",
                    "action": "BTO",
                    "volume": 5000,
                    "oi": 1000,
                }
            ],
        )
    ]

    with _triple_confluence_patches():
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    desc = get_embed_text(embeds[0])
    assert "三重結構性風險合流" not in desc
    assert "🛑 防洗盤處置，嚴守 15 分鐘實體 K 線撤退線" in desc


def test_build_radar_scan_embed_triple_confluence_yields_to_higher_priority_breakdown() -> (
    None
):
    """優先序案例：同時符合複合條件與既有更高優先序的「破位殺盤」條件時，應仍輸出破位殺盤訊息。"""
    far_expiry = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    result = _triple_confluence_base_result(
        skew_percentile=98.0,
        month_max_pains=[
            {"expiry": far_expiry, "max_pain": 70.0}
        ],  # dev ≈ +21.4%, dte=10
        uoa=[],
    )
    # 觸發「破位殺盤」：price < put_wall 且 volume_pcr >= 1.2
    result["quote"] = {"c": 85.0, "dp": -1.0}
    result["volume_pcr"] = 1.5

    scan_results = [result]

    with _triple_confluence_patches():
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    desc = get_embed_text(embeds[0])
    assert "🚨 破位殺盤 (PCR 1.50)，嚴防下殺" in desc
    assert "三重結構性風險合流" not in desc


def test_build_radar_scan_embed_all_enhanced_fields() -> None:
    """全面驗證交易員終端雷達補足之欄位：GEX Call/Put Wall、Top UOA、STO 鎖死、暗池水泥牆、真實 Skew、SQZ 向量、防洗盤防守位。"""
    scan_results = [
        {
            "symbol": "NVDA",
            "quote": {"c": 224.50, "dp": 2.35},
            "iv_metrics": {
                "iv_rank": 10.1,
                "expected_move_weekly": 12.0,
                "expected_move_lower": 212.5,
                "expected_move_upper": 236.5,
                "term_structure_ratio": 0.94,
                "iv_term_structure_status": "Contango",
            },
            "dte_er": 12,
            "skew": -0.29,
            "skew_percentile": 51.0,
            "max_pain": {"max_pain": 215.0},
            "uoa": [
                {
                    "symbol": "NVDA",
                    "expiry": "2026-08-15",
                    "strike": 227.5,
                    "type": "CALL",
                    "action": "STO",
                    "volume": 261000,
                    "oi": 15000,
                },
                {
                    "symbol": "NVDA",
                    "expiry": "2026-08-15",
                    "strike": 237.5,
                    "type": "PUT",
                    "action": "STO",
                    "volume": 7014,
                    "oi": 2000,
                },
            ],
            "gex_metrics": {
                "put_wall": 220.0,
                "call_wall": 227.5,
                "net_gex": 281300.0,
            },
            "gex_profile_data": {
                "put_wall": 220.0,
                "call_wall": 227.5,
                "net_gex": 281300.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 12.69,
                "signal_direction": "🟢",
            },
            "atr_14": 3.5,
            "darkpool": {
                "prints": [
                    {
                        "price": 220.50,
                        "premium": 48_850_000.0,
                        "volume": 220000,
                    }
                ]
            },
            "dp_poc": 220.50,
        },
        {
            "symbol": "CRWV",
            "quote": {"c": 106.29, "dp": -1.34},
            "iv_metrics": {
                "iv_rank": 51.3,
                "expected_move_weekly": 6.0,
                "expected_move_lower": 100.0,
                "expected_move_upper": 112.0,
                "term_structure_ratio": 0.94,
                "iv_term_structure_status": "Contango",
            },
            "dte_er": 5,
            "skew": -3.06,
            "skew_percentile": 62.0,
            "max_pain": {"max_pain": 95.0},
            "uoa": [
                {
                    "symbol": "CRWV",
                    "expiry": "2026-08-28",
                    "strike": 110.0,
                    "type": "PUT",
                    "action": "STO",
                    "volume": 955,
                    "oi": 120,
                }
            ],
            "gex_metrics": {
                "put_wall": 108.0,
                "call_wall": 109.0,
                "net_gex": 9144000.0,
            },
            "gex_profile_data": {
                "put_wall": 108.0,
                "call_wall": 109.0,
                "net_gex": 9144000.0,
                "gex_profile": {"104.0": 3000000, "105.0": 3000000, "106.0": 3144000},
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 19.62,
                "signal_direction": "🟢",
            },
            "atr_14": 2.8,
            "darkpool": {
                "prints": [
                    {
                        "price": 101.68,
                        "premium": 48_850_000.0,
                        "volume": 480000,
                    }
                ]
            },
            "dp_poc": 101.68,
        },
        {
            "symbol": "AAPL",
            "quote": {"c": 230.00, "dp": 0.50},
            "iv_metrics": {
                "iv_rank": 45.0,
                "expected_move_weekly": 5.0,
                "expected_move_lower": 225.0,
                "expected_move_upper": 235.0,
                "term_structure_ratio": 0.98,
                "iv_term_structure_status": "Contango",
            },
            "dte_er": 40,
            "skew": 0.1,
            "skew_percentile": 50.0,
            "max_pain": {"max_pain": 230.0},
            "uoa": [],
            "gex_metrics": {
                "put_wall": 220.0,
                "call_wall": 240.0,
                "net_gex": 5000000.0,
            },
            "psq_result": {
                "is_squeezing": False,
                "momentum": 1.0,
                "signal_direction": "🟢",
            },
        },
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    assert len(embeds) == 1
    desc = get_embed_text(embeds[0])

    # 1. 頂部 Gamma 牆驗證
    assert "(+) $227.5 / $220.0" in desc
    assert "(-) $109.0 / $108.0" in desc
    assert "(+) $240.0 / $220.0" in desc

    # 2. Top UOA 巨鯨開倉驗證
    assert "$227.5C (STO 261k)" in desc
    assert "$110.0P (STO 955)" in desc

    # 3. STO 鎖死履約價驗證
    assert "C$227.5 / P$237.5" in desc
    assert "P$110.0" in desc

    # 4. 真實 Skew 與分位點驗證
    assert "51% (-0.29%)" in desc
    assert "62% (-3.06%)" in desc

    # 5. SQZ 動能向量驗證 (NVDA 觸發上方 227.5 硬封頂降級為 ⚪，CRWV 維持 🟢)
    assert "+12.7" in desc
    assert "🟢+19.6" in desc
    assert "偵測到上方 $227.50 存在實質硬封頂" in desc

    # 6. IV 策略驗證
    assert "🔴CSP 禁售" in desc
    assert "🔴賣方禁售" in desc
    assert "🟢適宜賣方" in desc

    # 7. 防洗盤絕對防守位與離場判定鐵律驗證
    # CRWV PutWall 108.0 - 1.5 * 2.8 = $103.80
    assert "$103.80" in desc
    assert "嚴守 15 分鐘實體 K 線收盤撤退線" in desc


def test_build_radar_scan_embed_10_symbols_no_field_overflow() -> None:
    """驗證當輸入 10 檔自選標的 (Watchlist) 且包含完整 UOA、暗池與長文字戰術建議時，所有 Field Value 均嚴格 <= 1024 字元。"""
    symbols = [
        "NVDA",
        "AAPL",
        "MSFT",
        "TSLA",
        "AMZN",
        "GOOGL",
        "META",
        "AMD",
        "CRWV",
        "SPCX",
    ]
    scan_results: List[Dict[str, Any]] = []

    for i, sym in enumerate(symbols):
        scan_results.append(
            {
                "symbol": sym,
                "quote": {"c": 150.0 + i * 10.0, "dp": -2.5 + i * 0.5},
                "iv_metrics": {
                    "iv_rank": 45.0 + i * 2.0,
                    "expected_move_weekly": 8.0 + i * 0.5,
                    "expected_move_lower": 140.0 + i * 10.0,
                    "expected_move_upper": 160.0 + i * 10.0,
                    "term_structure_ratio": 1.05,
                    "iv_term_structure_status": "Contango",
                },
                "dte_er": 10 + i,
                "skew": -0.5 - i * 0.1,
                "skew_percentile": 55.0 + i * 3.0,
                "max_pain": {"max_pain": 145.0 + i * 10.0},
                "uoa": [
                    {
                        "symbol": sym,
                        "expiry": "2026-08-28",
                        "strike": 160.0 + i * 10.0,
                        "type": "CALL",
                        "action": "STO",
                        "volume": 5000 + i * 500,
                        "oi": 1000,
                    }
                ],
                "gex_metrics": {
                    "put_wall": 145.0 + i * 10.0,
                    "call_wall": 165.0 + i * 10.0,
                    "net_gex": 15000000.0,
                },
                "gex_profile_data": {
                    "put_wall": 145.0 + i * 10.0,
                    "call_wall": 165.0 + i * 10.0,
                    "net_gex": 15000000.0,
                },
                "psq_result": {
                    "is_squeezing": i % 2 == 0,
                    "momentum": 5.0 + i * 1.5,
                    "signal_direction": "🟢",
                },
                "atr_14": 2.5 + i * 0.2,
                "darkpool": {
                    "prints": [
                        {
                            "price": 148.0 + i * 10.0,
                            "premium": 60000000.0,
                            "volume": 400000,
                        }
                    ]
                },
                "dp_poc": 148.0 + i * 10.0,
            }
        )

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "defense_macro_tail_risk": True,
                "alpha_market_signals": True,
                "defense_portfolio_risk": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
        patch(
            "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
            return_value=(None, None, None),
        ),
    ):
        embeds = build_radar_scan_embed(scan_results, "WATCHLIST", 12345)

    assert len(embeds) == 1
    embed = embeds[0]

    # 1. 嚴格驗證每個欄位的 value <= 1024, name <= 256
    for idx, field in enumerate(embed.fields):
        name_len = len(field.name or "")
        val_len = len(field.value or "")
        assert (
            name_len <= 256
        ), f"Field {idx} name exceeds 256 chars: {name_len} ({field.name})"
        assert (
            val_len <= 1024
        ), f"Field {idx} value exceeds 1024 chars: {val_len} ({field.name})"

    # 2. 驗證 to_dict 輸出符合 Discord API 規範
    embed_dict = embed.to_dict()
    for idx, f in enumerate(embed_dict.get("fields", [])):
        assert (
            len(f.get("value", "")) <= 1024
        ), f"to_dict Field {idx} value exceeds 1024: {len(f.get('value', ''))}"
        assert (
            len(f.get("name", "")) <= 256
        ), f"to_dict Field {idx} name exceeds 256: {len(f.get('name', ''))}"

    # 3. 驗證所有 10 檔標的均有完整出現在文字中
    full_text = get_embed_text(embed)
    for sym in symbols:
        assert sym in full_text, f"Symbol {sym} missing from embed output"

    # 4. 驗證風控鐵律與即時警示完整保留
    assert "嚴守 15 分鐘實體 K 線收盤撤退線" in full_text
    assert "💡 即時聯動警示" in full_text
    assert "📋 雷達圖例與風控指引" in full_text


def test_nexus_embed_clamps_oversized_fields_safely() -> None:
    """驗證 NexusEmbed 能在超出 1024/256 字元時進行防禦性截斷並維持 codeblock 閉合。"""
    from cogs.embed_builders._core import NexusEmbed

    embed = NexusEmbed(title="Test Clamp")

    # 測試超長純文字
    long_name = "N" * 300
    long_value = "V" * 2000
    embed.add_field(name=long_name, value=long_value)

    assert len(embed.fields[0].name) == 256  # type: ignore
    assert embed.fields[0].name.endswith("...")  # type: ignore
    assert len(embed.fields[0].value) == 1024  # type: ignore
    assert embed.fields[0].value.endswith("...")  # type: ignore

    # 測試超長 codeblock 安全閉合
    long_code = "```ansi\n" + "A" * 2000 + "\n```"
    embed.add_field(name="Code Block", value=long_code)

    code_val = str(embed.fields[1].value)
    assert len(code_val) <= 1024
    assert code_val.startswith("```ansi\n")
    assert code_val.endswith("\n```")


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

    field_names: list[str] = [str(f.name or "") for f in embed.fields]

    assert any("持倉明細" in name for name in field_names)
    assert any("宏觀風險" in name for name in field_names)
    assert not any("對沖績效歸因" in name for name in field_names)
    assert any("🧠 AI 損益歸因與次日策略點評" in name for name in field_names)

    # Verify empty-state placeholder text in field values
    positions_val = next(
        str(f.value or "") for f in embed.fields if "持倉明細" in str(f.name or "")
    )
    assert "100% 現金防禦/觀望狀態" in positions_val

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
        all_field_names.extend(str(f.name) for f in emb.fields)
        all_field_values.extend((f.value or "") for f in emb.fields)

    # Hedge Attribution section must exist
    assert any("對沖績效歸因" in name for name in all_field_names)

    hedge_field_val = next(
        val
        for name, val in zip(all_field_names, all_field_values)
        if "對沖績效歸因" in name
    )
    # Verify all key hedge metrics are rendered
    assert "+200.00" in hedge_field_val
    assert "-350.50" in hedge_field_val
    assert "-150.50" in hedge_field_val
    assert "OPTIMAL" in hedge_field_val
    assert "0.0312" in hedge_field_val
    assert "75.0%" in hedge_field_val


def test_build_post_market_intelligence_embed_markdown_headers_and_independent_colors() -> (
    None
):
    """Verify that build_post_market_intelligence_embed correctly strips markdown ### headers and renders independent PnL colors."""
    ai_commentary = (
        "### 1. 📊 多空大盤交叉驗證解讀\n"
        "- 多空交叉解讀分析點 A\n"
        "- 多空交叉解讀分析點 B\n\n"
        "### 2. ⚠️ 潛在陷阱與風險提示\n"
        "- 風險提示點 A\n\n"
        "### 3. 🛡️ 高勝率交易策略推薦\n"
        "- 推薦策略 A\n"
    )
    hedge_data = {
        "net_pnl": 100.0,
        "alpha_contribution": 300.0,
        "hedge_contribution": -200.0,
        "hedge_ratio": 0.6667,
        "effectiveness": 0.8,
        "status": "OPTIMAL",
        "dynamic_tau": None,
    }
    embeds = build_post_market_intelligence_embed(
        report_lines=[],
        hedge_analysis=hedge_data,
        survival_runway=120.0,
        sectors_data=[
            {
                "symbol": "XLK",
                "name": "Technology",
                "pct_change": 1.5,
                "rel_vol": 1.2,
                "skew": 0.5,
                "uoa_count": 3,
            }
        ],
        ai_commentary=ai_commentary,
    )
    embed = embeds[0]
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}

    # 1. AI Sections must not contain ###
    market_val = field_dict.get("📊 AI 多空大盤交叉驗證解讀", "")
    assert "###" not in market_val
    assert "多空交叉解讀分析點 A" in market_val

    risk_val = field_dict.get("⚠️ AI 潛在陷阱與風險提示", "")
    assert "###" not in risk_val
    assert "風險提示點 A" in risk_val

    strat_val = field_dict.get("🛡️ AI 高勝率交易策略推薦", "")
    assert "###" not in strat_val
    assert "推薦策略 A" in strat_val

    # 2. Hedge Attribution ANSI colors
    hedge_val = field_dict.get("🛡️ 對沖績效歸因 (Hedge Attribution)", "")
    # Alpha (+300.00) is green (32m), Hedge (-200.00) is red (31m), Net (+100.00) is green (32m)
    assert "\033[1;32m$+300.00\033[0m" in hedge_val
    assert "\033[1;31m$-200.00\033[0m" in hedge_val
    assert "\033[1;32m$+100.00\033[0m" in hedge_val

    # 3. Sector rotation
    sector_val = field_dict.get("🔄 板塊輪動 (Sector Rotation)", "")
    assert "XLK" in sector_val
    assert "+1.50%" in sector_val


def test_create_covered_call_unlock_embed() -> None:
    """Verify that create_covered_call_unlock_embed correctly renders deadlock release embed."""
    # 1. With recommendations
    data_with_recs = {
        "symbol": "NVDA",
        "current_shares": 100.0,
        "current_cost": 120.0,
        "new_cost_basis": 115.0,
        "current_price": 110.0,
        "covered_shares": 100.0,
        "uncovered_shares": 100.0,
        "max_new_contracts": 1,
        "existing_calls": [
            {
                "strike": 140.0,
                "expiry": "2026-06-19",
                "quantity": -1,
                "shares_covered": 100.0,
            }
        ],
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
    assert len(embed_with_recs.fields) == 4
    assert "現貨與吸籌模擬" in embed_with_recs.fields[0].name  # type: ignore
    assert "100 股" in embed_with_recs.fields[0].value  # type: ignore
    assert "$120.00" in embed_with_recs.fields[0].value  # type: ignore
    assert "$115.00" in embed_with_recs.fields[0].value  # type: ignore
    assert "既有備兌覆蓋狀態" in embed_with_recs.fields[1].name  # type: ignore
    assert "100 股" in embed_with_recs.fields[1].value  # type: ignore
    assert "$140.00 Call @ 2026-06-19" in embed_with_recs.fields[1].value  # type: ignore
    assert "推薦 Covered Call 備兌合約" in embed_with_recs.fields[2].name  # type: ignore
    assert "2026-07-17" in embed_with_recs.fields[2].value  # type: ignore
    assert "$125.00" in embed_with_recs.fields[2].value  # type: ignore
    assert "18.50%" in embed_with_recs.fields[2].value  # type: ignore

    # 2. Without recommendations, and without any existing covered call coverage
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
    assert len(embed_no_recs.fields) == 3
    assert "既有備兌覆蓋狀態" in embed_no_recs.fields[1].name  # type: ignore
    assert "目前無既有備兌部位" in embed_no_recs.fields[1].value  # type: ignore
    assert "解鎖狀態與策略建議" in embed_no_recs.fields[2].name  # type: ignore
    assert "未尋獲符合條件之極虛值" in embed_no_recs.fields[2].value  # type: ignore


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

    assert "(狀態: ⚠️ 臨近財報/快取波動率可能低估)" in get_embed_text(embed)
    assert "備註: 實盤請預留 1.4x 波動邊界以防範 IV Crush。" in get_embed_text(embed)
    assert "Volume PCR (即時情緒): 0.78" in get_embed_text(embed)
    assert "OI PCR (結構防禦): 1.55" in get_embed_text(embed)


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
    assert embed.title == "📊 標的分析中心 2.0: AAPL 每半小時戰場心跳"

    desc = get_embed_text(embed) or ""
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


def test_create_watchlist_signal_embed_marks_stale_max_pain_with_age() -> None:
    """max_pain_data.is_stale=True 搭配 updated_at 應在心跳的 Max Pain 行同時
    顯示 [快取 / API 降級] 標記與人類可讀的資料年齡（回應使用者要求：不只顯示
    是否降級，也要能看到快取資料實際的日期時間）。"""
    from models.schemas import EnhancedWatchlistMetrics
    from models.quant import IVMetrics
    from datetime import datetime, timedelta, timezone

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

    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=42)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    embed = create_watchlist_signal_embed(
        symbol="AAPL",
        metrics=metrics,
        iv_metrics=iv_metrics,
        alert_level="green",
        max_pain_data={
            "max_pain": 145.0,
            "distance_pct": 3.45,
            "is_stale": True,
            "calculation_mode": "OI",
            "is_degraded": False,
            "circuit_breaker_triggered": False,
            "updated_at": stale_ts,
        },
    )

    desc = get_embed_text(embed) or ""
    assert "[快取 / API 降級" in desc
    assert "分鐘前" in desc


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
    name_pm_degraded = embeds_pm_degraded[0].fields[0].name
    assert "[盤前數據未更新]" in name_pm_degraded  # type: ignore
    assert "等待開盤" in val_pm_degraded  # type: ignore
    assert "--%" in val_pm_degraded  # type: ignore

    # 3. Test pre-market with STORED_IV fallback
    pm_stored_item = dict(item, is_premarket=True, iv_val=35.0, iv_source="STORED_IV")
    embeds_pm_stored = create_telemetry_alignment_embeds([pm_stored_item])
    assert len(embeds_pm_stored) == 1
    val_pm_stored = embeds_pm_stored[0].fields[0].value
    name_pm_stored = embeds_pm_stored[0].fields[0].name
    assert "[盤前/前日收盤]" in name_pm_stored  # type: ignore
    assert "(前日收盤)" in val_pm_stored  # type: ignore

    # 4. Test pre-market with HV_PROXY fallback
    pm_hv_item = dict(item, is_premarket=True, iv_val=42.0, iv_source="HV_PROXY")
    embeds_pm_hv = create_telemetry_alignment_embeds([pm_hv_item])
    assert len(embeds_pm_hv) == 1
    val_pm_hv = embeds_pm_hv[0].fields[0].value
    name_pm_hv = embeds_pm_hv[0].fields[0].name
    assert "[盤前/HV代理]" in name_pm_hv  # type: ignore
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


def test_create_tactical_symbol_embed_shows_anti_washout_stop_with_atr_15m() -> None:
    """atr_15m 有值且成功抓到 PutWall 時，GEX 欄位應附上
    PutWall - 1.5×ATR_15m 的防洗盤停損參考行。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": 10.0,
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 100.0},
        "gex_profile_data": {
            "put_wall": 100.0,
            "gex_profile": {"98.0": 2_000_000, "100.0": 3_000_000, "102.0": -1_000_000},
        },
        "atr_15m": 2.0,
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)
    # PutWall(100.0) - 1.5 * ATR_15m(2.0) = 97.0
    assert "防洗盤停損參考 (PutWall - 1.5×ATR_15m): $97.00" in desc


def test_create_tactical_symbol_embed_omits_anti_washout_stop_without_atr_15m() -> None:
    """atr_15m 缺失或為 0（抓取失敗）時，不應顯示防洗盤停損參考行，
    以免印出誤導性的 $0.00 或以現價當作 ATR 計算基礎。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": 10.0,
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 100.0},
        "gex_profile_data": {
            "put_wall": 100.0,
            "gex_profile": {"98.0": 2_000_000, "100.0": 3_000_000, "102.0": -1_000_000},
        },
        # atr_15m 未提供，模擬抓取失敗降級為 0.0
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)
    assert "防洗盤停損參考" not in desc
    assert "GEX PutWall (做市商底牆): $100.00" in desc


def test_create_tactical_symbol_embed_shows_net_gex_flip_and_callwall() -> None:
    """Net GEX Regime、個股 GEX Flip 線、GEX CallWall 水位/深度/距現價% 應正確接線顯示。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "price": 100.0,
        "gex_profile_data": {
            "put_wall": 90.0,
            "call_wall": 108.0,
            "net_gex": 20_000_000.0,
            "gex_profile": {
                "90.0": -5_000_000,
                "95.0": -2_000_000,
                "100.0": 10_000_000,
                "108.0": 4_000_000,
            },
        },
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)

    assert (
        "做市商淨曝險 (Net GEX Regime): +20000K (🟢 LONG_GAMMA (自穩定壓制波動))"
        in desc
    )
    assert "個股零 Gamma 翻轉線 (Stock GEX Flip): $100.00 (現價緩衝: +0.00%)" in desc
    assert (
        "GEX CallWall (做市商頂牆): $108.00 (深度: +4000K | 距現價空間: +8.00%)" in desc
    )


def test_create_tactical_symbol_embed_flags_callwall_insufficient_space() -> None:
    """GEX CallWall 距現價空間 < 5% 時應附註「❌ 不足5%」警示。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "price": 100.0,
        "gex_profile_data": {
            "put_wall": 90.0,
            "call_wall": 103.0,
            "net_gex": -5_000_000.0,
            "gex_profile": {
                "90.0": 1_000_000,
                "100.0": 2_000_000,
                "103.0": 1_000_000,
            },
        },
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)

    assert "做市商淨曝險 (Net GEX Regime): -5000K (🔴 SHORT_GAMMA (助漲助跌))" in desc
    assert "距現價空間: +3.00% ❌ 不足5%" in desc


def test_create_tactical_symbol_embed_shows_flip_placeholder_when_no_crossing() -> None:
    """gex_profile 全數同號、沒有 zero-cross 時，GEX Flip 應顯示 -- 而非誤導性的 $0.00。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "price": 100.0,
        "gex_profile_data": {
            "put_wall": 90.0,
            "gex_profile": {"98.0": 2_000_000, "100.0": 3_000_000, "102.0": 1_000_000},
        },
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)

    assert "個股零 Gamma 翻轉線 (Stock GEX Flip): -- (無法估算)" in desc


def test_create_tactical_symbol_embed_marks_live_iv_realtime() -> None:
    """iv_source == "LIVE_IV"（force_refresh 即時反解）時，IV 標題應標示
    🟢即時，讓使用者能分辨看到的 IV 是即時反解還是快取值。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": 10.0,
            "iv_status": "Normal",
            "iv_source": "LIVE_IV",
            "is_premarket": False,
        },
        "expected_move_context": {"reference_price": 100.0},
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)
    assert "Implied Volatility (IV) 🟢即時" in desc
    assert "當前 30 天平值期權隱含波動率（每次開啟強制刷新，非快取）" in desc


def test_create_tactical_symbol_embed_stored_iv_has_no_realtime_marker() -> None:
    """iv_source == "STORED_IV" 時（非本次請求即時反解），不應顯示 🟢即時標記，
    維持既有的快取提示文字不變。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": 10.0,
            "iv_status": "Normal",
            "iv_source": "STORED_IV",
            "is_premarket": False,
        },
        "expected_move_context": {"reference_price": 100.0},
    }

    embed = create_tactical_symbol_embed(data)
    desc = get_embed_text(embed)
    assert "🟢即時" not in desc
    assert "SQLite 快取 IV（非即時）" in desc


def test_create_tactical_symbol_embed_marks_stale_uoa_field() -> None:
    """uoa_age_seconds 超過 30 分鐘門檻時，UOA 欄位名稱應附上資料年齡標記
    （回應使用者要求：能看到快取資料實際的日期時間，而不只是布林警告）。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "NVDA",
        "iv_data": {
            "current_iv": 0.5,
            "iv_rank": 50.0,
            "iv_percentile": 60.0,
            "expected_move_weekly": 10.0,
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 100.0},
        "uoa": [],
        "uoa_age_seconds": 3600.0,  # 1 小時前，超過 1800 秒門檻
    }

    embed = create_tactical_symbol_embed(data)
    field_names = [f.name for f in embed.fields if f.name is not None]
    matching = [n for n in field_names if "異常活動 (UOA)" in n]
    assert matching, f"找不到 UOA 欄位，現有欄位: {field_names}"
    assert "小時前" in matching[0]


def test_create_tactical_symbol_embed_fresh_uoa_shows_just_fetched_marker() -> None:
    """uoa_age_seconds=0.0（即時抓取，如背景排程 slow path）應顯示「剛剛」
    而非降級警告字樣 —— 年齡標記本身永遠附上（回應使用者要求可看到實際資料
    時間），但不應與 [快取 / API 降級] 的警告語氣混淆。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "AMD",
        "iv_data": {
            "current_iv": 0.4,
            "iv_rank": 40.0,
            "iv_percentile": 50.0,
            "expected_move_weekly": 8.0,
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 90.0},
        "uoa": [],
        "uoa_age_seconds": 0.0,
    }

    embed = create_tactical_symbol_embed(data)
    field_names = [f.name for f in embed.fields if f.name is not None]
    matching = [n for n in field_names if "異常活動 (UOA)" in n]
    assert matching, f"找不到 UOA 欄位，現有欄位: {field_names}"
    assert "剛剛" in matching[0]
    assert "降級" not in matching[0]


def test_create_tactical_symbol_embed_no_uoa_age_marker_when_age_unknown() -> None:
    """uoa_age_seconds 完全未提供（None）時，無法判斷資料年齡，不應顯示任何
    年齡標記，以免誤導使用者資料是新鮮的。"""
    from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed

    data = {
        "symbol": "AMD",
        "iv_data": {
            "current_iv": 0.4,
            "iv_rank": 40.0,
            "iv_percentile": 50.0,
            "expected_move_weekly": 8.0,
            "iv_status": "Normal",
        },
        "expected_move_context": {"reference_price": 90.0},
        "uoa": [],
    }

    embed = create_tactical_symbol_embed(data)
    field_names = [f.name for f in embed.fields if f.name is not None]
    matching = [n for n in field_names if "異常活動 (UOA)" in n]
    assert matching, f"找不到 UOA 欄位，現有欄位: {field_names}"
    assert matching[0] == "🐋 異常活動 (UOA)"


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


# ─────────────────────────────────────────────────────────────────────────────
# 欄位數據驗算測試 (Field Accuracy Audit)
# ─────────────────────────────────────────────────────────────────────────────


def test_holding_pnl_pct_displayed_correctly() -> None:
    """holding_pnl_pct 應正確計算並顯示損益百分比（而非永遠 0.00%）。"""
    embed = create_watchlist_signal_embed(
        symbol="AAPL",
        has_position=True,
        holding_quantity=100.0,
        holding_avg_cost=150.0,
        holding_pnl_pct=0.1333,  # +13.33% = (170 - 150) / 150 * 100
        suitable_sell_price=170.0,
        suitable_sell_shares=25,
        sell_rationale="分批減碼 25%",
    )
    assert embed is not None
    assert get_embed_text(embed)
    assert "+13.33%" in get_embed_text(embed)


def test_holding_pnl_pct_negative_displayed_correctly() -> None:
    """持倉虧損時，損益應顯示負號。"""
    embed = create_watchlist_signal_embed(
        symbol="TSLA",
        has_position=True,
        holding_quantity=50.0,
        holding_avg_cost=200.0,
        holding_pnl_pct=-0.10,  # -10.00%
        suitable_sell_price=185.0,
        suitable_sell_shares=12,
        sell_rationale="止損",
    )
    assert embed is not None
    assert get_embed_text(embed)
    assert "-10.00%" in get_embed_text(embed)


def test_holding_pnl_pct_none_shows_zero() -> None:
    """holding_pnl_pct=None 時，顯示為 0.00%（向後相容的 fallback）。"""
    embed = create_watchlist_signal_embed(
        symbol="NVDA",
        has_position=True,
        holding_quantity=10.0,
        holding_avg_cost=100.0,
        holding_pnl_pct=None,
        suitable_sell_price=110.0,
        suitable_sell_shares=2,
        sell_rationale="觀望",
    )
    assert embed is not None
    assert get_embed_text(embed)
    assert "0.00%" in get_embed_text(embed)


def test_build_post_market_intelligence_embed_with_stock_holdings() -> None:
    """Verify that build_post_market_intelligence_embed correctly parses and renders STOCK holdings."""
    stock_report_line = (
        "🔹 **NVDA** ｜ `PERPETUAL` ｜ `$120.00` **STOCK**\n"
        "├─ 💰 成本: `$120.00` ｜ 📈 現價: `$130.00`\n"
        "├─ 🟢 損益: **+8.33%**\n"
        "├─ ⏳ DTE: `0` 天 ｜ 秤⚖️ SPY Δ: `+5.20`\n"
        "├─ ⚙️ 方向: `BTO` ｜ 📦 數量: `10.0`\n"
        "├─ 📊 IV/IVR: `0.0%/35.0%`\n"
        "└─ 🎯 動作: HOLD\n"
    )
    macro_report_line = (
        "🌐 **【宏觀風險與資金水位報告】**\n"
        "✅ **風險中性** (`5.2%` 內)\n"
        "   👉 目前系統性曝險在安全範圍，無需執行對沖。\n"
    )
    embeds = build_post_market_intelligence_embed(
        report_lines=[stock_report_line, macro_report_line],
        hedge_analysis={"net_pnl": 100.0, "status": "OPTIMAL"},
        survival_runway=500.0,
        sectors_data=[],
        ai_commentary="1. 📊 多空大盤交叉驗證解讀\n無異常",
    )
    embed = embeds[0]
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}

    pos_val = field_dict.get("📊 持倉明細 (Positions)", "")
    assert "NVDA" in pos_val
    assert "現貨 (HOLDING)" in pos_val
    assert "10 股" in pos_val
    assert "$120.00" in pos_val
    assert "$130.00" in pos_val

    macro_val = field_dict.get("🌐 【宏觀風險與資金水位報告】", "")
    assert "風險中性" in macro_val

    fin_val = field_dict.get("💰 資金與實質暴露 (Financial Summary)", "")
    # Debit cost for 10 shares of $120.00 should be $1,200.00 USD
    assert "$1,200.00 USD" in fin_val


def test_pcr_state_empty_string_falls_back_to_numeric_logic() -> None:
    """pcr_dict 中 volume_pcr_state 為空字串時，應 fallback 至數值閾值判斷，不顯示空字串。"""
    from models.quant import IVMetrics

    iv_m = IVMetrics(
        symbol="AAPL",
        current_iv=0.25,
        iv_rank=45.0,
        iv_percentile=50.0,
        expected_move_weekly=4.5,
        iv_status="Normal",
        iv_source="LIVE_IV",
    )
    pcr_data_with_empty_state = {
        "volume_pcr": 0.75,  # < 0.90 → should resolve to 🐂 中性偏多/看漲主導
        "volume_pcr_state": "",  # empty string — should NOT be used
        "oi_pcr": 1.05,  # between 0.90–1.20 → ⚖️ 籌碼結構中性
        "oi_pcr_state": "",  # empty string — should NOT be used
    }

    embed = create_watchlist_signal_embed(
        symbol="AAPL",
        iv_metrics=iv_m,
        pcr_data=pcr_data_with_empty_state,
    )
    assert embed is not None
    assert get_embed_text(embed)
    # volume_pcr_state="" should not appear; numeric branch should activate
    assert "🐂 中性偏多/看漲主導" in get_embed_text(embed)
    assert "⚖️ 籌碼結構中性" in get_embed_text(embed)


def test_pcr_state_valid_string_is_used() -> None:
    """pcr_dict 中有效的 volume_pcr_state 應直接使用。"""
    from models.quant import IVMetrics

    iv_m = IVMetrics(
        symbol="MSFT",
        current_iv=0.20,
        iv_rank=55.0,
        iv_percentile=60.0,
        expected_move_weekly=3.0,
        iv_status="Normal",
        iv_source="LIVE_IV",
    )
    pcr_data_with_custom_state = {
        "volume_pcr": 0.95,
        "volume_pcr_state": "🔵 自訂狀態標籤",
        "oi_pcr": 1.10,
        "oi_pcr_state": "🟣 OI 自訂狀態",
    }

    embed = create_watchlist_signal_embed(
        symbol="MSFT",
        iv_metrics=iv_m,
        pcr_data=pcr_data_with_custom_state,
    )
    assert embed is not None
    assert get_embed_text(embed)
    assert "🔵 自訂狀態標籤" in get_embed_text(embed)
    assert "🟣 OI 自訂狀態" in get_embed_text(embed)


def test_build_post_market_intelligence_embed_target_center_styling_and_sector_matrix() -> (
    None
):
    """Verify Target Center 2.0 aesthetics, tree structures, and focus sector matrix in post-market embed."""
    stock_line = (
        "🔹 **NVDA** ｜ `PERPETUAL` ｜ `$120.00` **STOCK**\n"
        "├─ 💰 成本: `$120.00` ｜ 📈 現價: `$130.00`\n"
        "├─ 🟢 損益: **+8.33%**\n"
        "├─ ⏳ DTE: `0` 天 ｜ 秤⚖️ SPY Δ: `+5.20`\n"
        "├─ ⚙️ 方向: `BTO` ｜ 📦 數量: `10.0`\n"
        "├─ 📊 IV/IVR: `0.0%/35.0%`\n"
        "└─ 🎯 動作: HOLD\n"
    )
    option_line = (
        "🔹 **AAPL** ｜ `2026-09-18` ｜ `$220.00` **CALL**\n"
        "├─ 💰 成本: `$5.00` ｜ 📈 現價: `$6.50`\n"
        "├─ 🟢 損益: **+30.00%**\n"
        "├─ ⏳ DTE: `35` 天 ｜ 秤⚖️ SPY Δ: `+32.50`\n"
        "├─ ⚙️ 方向: `BTO` ｜ 📦 數量: `1.0`\n"
        "├─ 📊 IV/IVR: `28.5%/45.0%`\n"
        "└─ 🎯 動作: HOLD\n"
    )
    macro_line = (
        "🌐 **【宏觀風險與資金水位報告】**\n"
        "🚨 **多頭曝險過高** (`25.0%` > `15.0%`)\n"
        "   👉 目前系統性曝險偏高，建議執行對沖保護。\n"
        "📊 **資產指標概覽**\n"
        "   👉 組合 Delta: +37.70\n"
    )
    hedge_data = {
        "net_pnl": 150.00,
        "alpha_contribution": 350.00,
        "hedge_contribution": -200.00,
        "hedge_ratio": 0.5714,
        "effectiveness": 0.85,
        "status": "OPTIMAL",
        "dynamic_tau": 0.0312,
    }
    sectors_data = [
        {
            "symbol": "XLK",
            "name": "科技",
            "pct_change": 1.52,
            "rel_vol": 1.45,
            "skew": 0.8,
            "uoa_count": 4,
        },
        {
            "symbol": "XLY",
            "name": "非必",
            "pct_change": 0.95,
            "rel_vol": 1.10,
            "skew": 0.3,
            "uoa_count": 1,
        },
        {
            "symbol": "XLE",
            "name": "能源",
            "pct_change": -1.85,
            "rel_vol": 1.60,
            "skew": -0.9,
            "uoa_count": 3,
        },
        {
            "symbol": "XLF",
            "name": "金融",
            "pct_change": -0.65,
            "rel_vol": 0.90,
            "skew": -0.4,
            "uoa_count": 1,
        },
    ]
    ai_commentary = (
        "1. 📊 多空大盤交叉驗證解讀\n"
        "- 大盤處於高檔震盪整理\n"
        "2. ⚠️ 潛在陷阱與風險提示\n"
        "- 留意科技股獲利了結賣壓\n"
        "3. 🛡️ 高勝率交易策略推薦\n"
        "- 建議逢高佈置 Bear Call Spread\n"
    )
    embeds = build_post_market_intelligence_embed(
        report_lines=[stock_line, option_line, macro_line],
        hedge_analysis=hedge_data,
        survival_runway=500.0,
        sectors_data=sectors_data,
        ai_commentary=ai_commentary,
    )
    embed = embeds[0]
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}

    # 1. Description contains runway (without timestamp or pnl)
    desc = embed.description or ""
    assert "🏁 財務生存跑道" in desc
    assert "500.0 天" in desc

    fin_val = field_dict.get("💰 資金與實質暴露 (Financial Summary)", "")
    assert "Debit Cost" in fin_val
    assert "Credit Cash" in fin_val
    assert "Unrealized" in fin_val

    # 2. Positions clean bullet styling
    pos_val = field_dict.get("📊 持倉明細 (Positions)", "")
    assert "NVDA" in pos_val
    assert "AAPL" in pos_val
    assert "•" in pos_val

    # 3. Hedge attribution
    hedge_val = field_dict.get("🛡️ 對沖績效歸因 (Hedge Attribution)", "")
    assert "OPTIMAL (對沖結構健康)" in hedge_val
    assert "0.0312" in hedge_val
    assert "85.0%" in hedge_val

    # 4. Macro risks
    macro_val = field_dict.get("🌐 【宏觀風險與資金水位報告】", "")
    assert "多頭曝險過高" in macro_val

    # 5. Sector rotation focus matrix
    sector_val = field_dict.get("🔄 板塊輪動 (Sector Rotation)", "")
    assert "領漲板塊 (Top Inflows)" in sector_val
    assert "領跌板塊 (Top Outflows)" in sector_val
    assert "XLK" in sector_val
    assert "XLE" in sector_val


def test_build_pre_market_briefing_embed_vix_ansi_and_macro_status() -> None:
    macro_data = {
        "vix": 14.53,
        "vix_change": -0.10,
        "dxy": 104.25,
        "tnx": 4.25,
        "tnx_change_bps": 2.1,
        "us2y": 4.60,
    }

    embed = build_pre_market_briefing_embed(
        macro_data=macro_data,
        alerts=[],
        earnings_alerts=[],
        scanned_symbols=["AAPL", "NVDA"],
        warning_days=14,
    )

    assert embed.title == "🌅 報告：盤前綜合宏觀與自選股 [✅ 總經平穩・無即期財報]"
    assert embed.description is None
    assert embed.color == discord.Color.blue()
    field_dict = {str(f.name): str(f.value or "") for f in embed.fields}

    # 1. 驗證巨觀數據指標 ANSI 代碼無文字亂碼殘留
    macro_field = field_dict.get("🌍 巨觀數據指標", "")
    assert "VIX 恐慌指數" in macro_field
    assert "14.53" in macro_field
    assert " [0;31m" not in macro_field
    assert " [0;32m" not in macro_field
    assert " [0;33m" not in macro_field
    assert " [0m" not in macro_field
    assert "\x1b[0;32m" in macro_field
    assert "\x1b[0m" in macro_field
    assert "(🟢)" in macro_field

    # 2. 驗證四維度宏觀狀態解讀 (ANSI 包裹)
    status_field = field_dict.get("✅ 宏觀狀態", "")
    assert status_field.startswith("```ansi")
    assert "低波動沉睡區間" in status_field
    assert "10Y 4.25%" in status_field
    assert "DXY 104.25" in status_field
    assert "指標全數合規" in status_field

    # 3. 驗證自選股無財報風險安全欄位 (ANSI 包裹)
    safe_field = field_dict.get("✅ 自選股財報季雷達", "")
    assert safe_field.startswith("```ansi")
    assert "AAPL, NVDA" in safe_field
    assert "近 14 日內無財報發布風險" in safe_field

    # 4. 驗證僅宏觀警報狀態分支 (Red)
    embed_with_alerts = build_pre_market_briefing_embed(
        macro_data=macro_data,
        alerts=["恐慌指數急遽上升，市場避險情緒發酵"],
        earnings_alerts=[],
    )
    assert (
        embed_with_alerts.title == "🌅 報告：盤前綜合宏觀與自選股 [⚠️ 宏觀風控警報觸發]"
    )
    assert embed_with_alerts.color == discord.Color.red()
    alert_field_dict = {
        str(f.name): str(f.value or "") for f in embed_with_alerts.fields
    }
    assert "🚨 宏觀風險警示 (Macro Alerts)" in alert_field_dict
    assert "恐慌指數急遽上升" in alert_field_dict["🚨 宏觀風險警示 (Macro Alerts)"]
    assert alert_field_dict["🚨 宏觀風險警示 (Macro Alerts)"].startswith("```ansi")


def test_build_pre_market_briefing_embed_10_earnings_alerts() -> None:
    macro_data = {
        "vix": 16.5,
        "vix_change": 0.5,
        "dxy": 102.0,
        "tnx": 4.1,
        "tnx_change_bps": 0.0,
        "us2y": 4.2,
    }

    # 測試正好 10 檔標的 (包含持倉高風險 -> 觸發紅色高危標題)
    ten_alerts = [
        {
            "symbol": f"SYM{i}",
            "is_portfolio": (i % 2 == 0),
            "earnings_date": f"2026-08-{20+i}",
            "days_left": i,
        }
        for i in range(1, 11)
    ]
    embed_10 = build_pre_market_briefing_embed(
        macro_data=macro_data,
        earnings_alerts=ten_alerts,
    )
    assert embed_10.title == "🌅 報告：盤前綜合宏觀與自選股 [⚠️ 持倉標的財報高危]"
    assert embed_10.color == discord.Color.red()
    field_dict_10 = {str(f.name): str(f.value or "") for f in embed_10.fields}
    earnings_val_10 = field_dict_10.get("🚨 自選股財報季雷達預警 (Earnings Radar)", "")
    assert earnings_val_10.startswith("```ansi")
    for i in range(1, 11):
        assert f"SYM{i}" in earnings_val_10
    assert "...等共" not in earnings_val_10

    # 測試超過 10 檔標的 (例如 12 檔，純觀察名單 -> 觸發橙色觀察標題)：應拆成兩個分批 field
    twelve_alerts = [
        {
            "symbol": f"TICK{i}",
            "is_portfolio": False,
            "earnings_date": f"2026-08-{20+i}",
            "days_left": i,
        }
        for i in range(1, 13)
    ]
    embed_12 = build_pre_market_briefing_embed(
        macro_data=macro_data,
        earnings_alerts=twelve_alerts,
    )
    assert embed_12.title == "🌅 報告：盤前綜合宏觀與自選股 [👀 自選清單財報預警]"
    assert embed_12.color == discord.Color(0xF39C12)
    field_dict_12 = {str(f.name): str(f.value or "") for f in embed_12.fields}
    earnings_val_page1 = field_dict_12.get(
        "🚨 自選股財報季雷達預警 (Earnings Radar) (第 1/2 批)", ""
    )
    earnings_val_page2 = field_dict_12.get(
        "🚨 自選股財報季雷達預警 (Earnings Radar) (第 2/2 批)", ""
    )
    assert earnings_val_page1.startswith("```ansi")
    assert earnings_val_page2.startswith("```ansi")
    for i in range(1, 11):
        assert f"TICK{i}" in earnings_val_page1
    for i in range(11, 13):
        assert f"TICK{i}" in earnings_val_page2
    combined_val_12 = earnings_val_page1 + earnings_val_page2
    assert "...等共" not in combined_val_12

    # 測試雙重高危 (宏觀警報 + 持倉財報高危)
    embed_dual_risk = build_pre_market_briefing_embed(
        macro_data=macro_data,
        alerts=["殖利率曲線深度倒掛"],
        earnings_alerts=[
            {
                "symbol": "NVDA",
                "is_portfolio": True,
                "earnings_date": "2026-08-25",
                "days_left": 4,
            }
        ],
    )
    assert (
        embed_dual_risk.title == "🌅 報告：盤前綜合宏觀與自選股 [🚨 雙重高危風控警戒]"
    )
    assert embed_dual_risk.color == discord.Color.red()


def test_create_macro_scan_embed_vix_ansi_and_healthy_status() -> None:
    macro_data = {
        "vix": 18.20,
        "vix_change": 0.80,
        "dxy": 102.50,
        "tnx": 4.15,
        "tnx_change_bps": -1.5,
        "us2y": 4.30,
    }
    embed = create_macro_scan_embed(macro_data=macro_data, alerts=[])
    field_dict = {str(f.name): str(f.value or "") for f in embed.fields}

    # 驗證 ANSI 無字面殘留碼
    macro_field = field_dict.get("🌍 巨觀數據指標", "")
    assert " [0;32m" not in macro_field
    assert " [0m" not in macro_field
    assert "\x1b[0;32m" in macro_field
    assert "\x1b[0m" in macro_field

    # 驗證巨觀狀態動態解讀
    status_field = field_dict.get("✅ 巨觀狀態", "")
    assert "📈 **波動率環境**" in status_field
    assert "常態健康位階" in status_field


def test_macro_report_ansi_no_fake_heading_from_zero_width_space() -> None:
    """report_formatter 用零寬空格 (\u200b) 分隔段落，_format_macro_report_ansi 不應誤判
    出一個空洞的「🔹 宏觀指標」假標題。"""
    from market_analysis.report_formatter import format_macro_risk_report

    metrics = {
        "exposure_pct": 5.2,
        "net_exposure_dollars": 1000.0,
        "total_beta_delta": 10.0,
        "total_gamma": 0.10,
        "gamma_threshold": 0.5,
        "theta_yield": 0.10,
        "total_theta": 25.0,
        "portfolio_heat": 12.0,
        "total_margin_used": 5000.0,
        "total_vega": 3.0,
        "total_vanna": 1.0,
    }
    report_lines = format_macro_risk_report(metrics, spy_price=450.0)

    embeds = build_post_market_intelligence_embed(report_lines=report_lines)
    embed = embeds[0]
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}

    macro_val = field_dict.get("🌐 【宏觀風險與資金水位報告】", "")
    assert "宏觀指標" not in macro_val
    assert "淨 SPY Delta 曝險" in macro_val


def test_macro_and_correlation_report_split_into_separate_fields() -> None:
    """宏觀風險與板塊相關性應各自渲染為獨立的 Embed Field（Field-based Modularization）。"""
    from market_analysis.report_formatter import (
        format_correlation_report,
        format_macro_risk_report,
    )

    metrics = {
        "exposure_pct": 5.2,
        "net_exposure_dollars": 1000.0,
        "total_beta_delta": 10.0,
        "total_gamma": 0.10,
        "gamma_threshold": 0.5,
        "theta_yield": 0.10,
        "total_theta": 25.0,
        "portfolio_heat": 12.0,
        "total_margin_used": 5000.0,
    }
    report_lines = format_macro_risk_report(metrics, spy_price=450.0)
    report_lines += format_correlation_report(
        high_corr_pairs=[("NVDA", "AMD", 0.85)], symbol_count=5
    )

    embeds = build_post_market_intelligence_embed(report_lines=report_lines)
    embed = embeds[0]
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}

    macro_val = field_dict.get("🌐 【宏觀風險與資金水位報告】", "")
    correlation_val = field_dict.get("🕸️ 【非系統性集中風險 (板塊連動性)】", "")

    assert macro_val, "應存在獨立的宏觀風險欄位"
    assert correlation_val, "應存在獨立的板塊相關性欄位"

    assert "淨 SPY Delta 曝險" in macro_val
    assert "板塊相關性掃描" not in macro_val

    assert "板塊相關性掃描" in correlation_val
    assert "NVDA" in correlation_val and "AMD" in correlation_val
    assert "淨 SPY Delta 曝險" not in correlation_val


def test_nexus_embed_overflow_no_longer_shows_warning_text() -> None:
    """移除全域截斷防護警告文字後，超量欄位仍會被安全截斷，但不再附加提示文字。"""
    from cogs.embed_builders._core import NexusEmbed

    embed = NexusEmbed(title="測試", description="")
    for i in range(30):
        embed.add_field(name=f"欄位 {i}", value=f"內容 {i}", inline=False)

    result = embed.to_dict()

    assert len(result["fields"]) <= 25
    desc = result.get("description") or ""
    assert "自選標的過多" not in desc
    assert "自動截斷防護" not in desc


def test_create_fomc_escape_window_embed_title_and_layout() -> None:
    """驗證宏觀逃頂推演矩陣 Embed 標題與版面格式"""
    embed = create_fomc_escape_window_embed(
        prob=0.85,
        direction="前移",
        shift_days=5,
        adjusted_start="10月中旬 (10-15)",
        adjusted_end="10月下旬 (10-25)",
        reason="緊縮警戒測試",
        is_fallback=False,
        tier_title="🚨 收縮警戒 (Tightening Contraction)",
        tactical_directive="提前防禦撤退指引",
        factors_summary=[("FOMC 利率定價 (FedWatch)", "🚨 鷹派加息")],
        was_auto_rolled=False,
    )

    assert embed.title == "📅 宏觀逃頂：總經流動性撤退推演矩陣 (Macro Escape Matrix)"
    field_dict: dict[str, str] = {str(f.name): str(f.value or "") for f in embed.fields}
    assert "🧭 宏觀流動性狀態" in field_dict
    assert "🚨 收縮警戒" in field_dict["🧭 宏觀流動性狀態"]
    assert "🔄 逃頂窗口調整方向" in field_dict
    assert "前移 5 個交易日" in field_dict["🔄 逃頂窗口調整方向"]
    assert "📆 調整後逃頂窗口預期" in field_dict
    assert "10月中旬 (10-15)" in field_dict["📆 調整後逃頂窗口預期"]
    assert "🎯 戰術行動指引" in field_dict
    assert "```ansi" in field_dict["🎯 戰術行動指引"]
    assert "💡 推演邏輯與風控分析" in field_dict
    assert "```ansi" in field_dict["💡 推演邏輯與風控分析"]


def test_create_stress_test_embed_ansi_layout() -> None:
    """驗證現金赤字壓力測試 Embed ANSI 排版與警報狀態"""
    # 1. Critical Scenario
    critical_data = {
        "is_critical": True,
        "total_deficit": 22500.0,
        "cash_reserve": 150.0,
        "boxx_shares": 213.0,
        "boxx_cash": 21000.0,
        "net_deficit": -1350.0,
        "gtc_buy_orders_count": 18,
    }
    embed_crit = create_stress_test_embed(critical_data)
    assert embed_crit.title == "🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)"
    assert embed_crit.color == discord.Color.red()
    field_dict = {str(f.name): str(f.value or "") for f in embed_crit.fields}
    assert "📊 壓測摘要" in field_dict
    assert "```ansi" in field_dict["📊 壓測摘要"]
    assert "$22,500.00" in field_dict["📊 壓測摘要"]
    assert (
        "-$1350.00" in field_dict["📊 壓測摘要"]
        or "-$1,350.00" in field_dict["📊 壓測摘要"]
    )
    assert "🔥 CRITICAL WARNING" in field_dict
    assert "```ansi" in field_dict["🔥 CRITICAL WARNING"]

    # 2. Safe Scenario
    safe_data = {
        "is_critical": False,
        "total_deficit": 5000.0,
        "cash_reserve": 10000.0,
        "boxx_shares": 100.0,
        "boxx_cash": 11600.0,
        "net_deficit": 16600.0,
        "gtc_buy_orders_count": 4,
    }
    embed_safe = create_stress_test_embed(safe_data)
    assert embed_safe.color == discord.Color.green()
    field_dict_safe = {str(f.name): str(f.value or "") for f in embed_safe.fields}
    assert "✅ 系統安全狀態" in field_dict_safe
    assert "```ansi" in field_dict_safe["✅ 系統安全狀態"]


def test_get_embed_length() -> None:
    # 1. Create an embed with title, description, footer, author, and fields
    embed = discord.Embed(title="Hello", description="World")
    embed.set_author(name="AuthorName")
    embed.set_footer(text="FooterText")
    embed.add_field(name="Field1", value="Value1")
    embed.add_field(name="Field2", value="Value2")

    # Sum of lengths:
    # Hello: 5
    # World: 5
    # AuthorName: 10
    # FooterText: 10
    # Field1: 6, Value1: 6 -> 12
    # Field2: 6, Value2: 6 -> 12
    # Total = 5 + 5 + 10 + 10 + 12 + 12 = 54
    assert get_embed_length(embed) == 54

    # 2. Test empty embed
    empty_embed = discord.Embed()
    assert get_embed_length(empty_embed) == 0


def test_chunk_embeds_by_size() -> None:
    embeds = [
        discord.Embed(description="A" * 2000),  # 2000
        discord.Embed(description="B" * 2000),  # 2000
        discord.Embed(description="C" * 2000),  # 2000
    ]
    # Under max_size=3000, 2000 + 2000 = 4000 > 3000, so each should be in its own chunk
    chunks = chunk_embeds(embeds, max_size=3000)
    assert len(chunks) == 3
    assert len(chunks[0]) == 1
    assert len(chunks[1]) == 1
    assert len(chunks[2]) == 1

    # Under max_size=5000, first two (4000) fit in one chunk, the third (2000) in the next
    chunks2 = chunk_embeds(embeds, max_size=5000)
    assert len(chunks2) == 2
    assert len(chunks2[0]) == 2
    assert len(chunks2[1]) == 1


def test_chunk_embeds_by_count() -> None:
    embeds = [discord.Embed(description="A") for _ in range(15)]
    # Under max_count=5, 15 embeds should be split into 3 chunks of 5
    chunks = chunk_embeds(embeds, max_count=5)
    assert len(chunks) == 3
    assert all(len(c) == 5 for c in chunks)


def test_single_oversized_embed() -> None:
    embeds = [
        discord.Embed(description="A" * 6000),  # 6000 (oversized)
        discord.Embed(description="B" * 100),  # 100
    ]
    # An oversized embed should still be chunked safely, and the next fits in the subsequent chunk
    chunks = chunk_embeds(embeds, max_size=5000)
    assert len(chunks) == 2
    assert len(chunks[0]) == 1
    assert len(chunks[1]) == 1
