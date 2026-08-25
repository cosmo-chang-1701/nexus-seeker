#!/usr/bin/env python3
"""
Nexus Seeker - Dynamic Rollover Engine Real-Time Simulation Drill (動態轉倉演練)
=============================================================================
This script simulates and prints step-by-step telemetry, decision matrices,
and resulting Discord Embed notifications for:
  - Scenario 1: NVDA turns weak, SPCX turns strong and passes all 6 entry gates.
  - Scenario 2: NVDA turns weak, no candidate meets criteria (Hold / Blocked / VOO / BOXX).
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

# Ensure root of nexus_core is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from cogs.embed_builders.rollover_embeds import create_dynamic_rollover_embed
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    RolloverScenario,
)

# ANSI Color Codes for Terminal Output
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[1;31m"
C_GREEN = "\033[1;32m"
C_YELLOW = "\033[1;33m"
C_BLUE = "\033[1;34m"
C_MAGENTA = "\033[1;35m"
C_CYAN = "\033[1;36m"


def print_banner(title: str) -> None:
    line = "═" * 70
    print(f"\n{C_CYAN}{line}{C_RESET}")
    print(f"{C_BOLD}{C_YELLOW} 🚀 {title}{C_RESET}")
    print(f"{C_CYAN}{line}{C_RESET}\n")


def print_section(title: str) -> None:
    print(f"\n{C_BOLD}{C_BLUE}--- [{title}] ---{C_RESET}")


def print_embed_preview(embed: Any) -> None:
    print(f"\n{C_BOLD}{C_MAGENTA}📱 [Discord Embed 推播預覽]{C_RESET}")
    print(f"{C_BOLD}標題:{C_RESET} {embed.title}")
    print(f"{C_BOLD}顏色代碼:{C_RESET} {embed.color}")
    print(f"{C_BOLD}描述 (Description):{C_RESET}\n{embed.description}\n")
    for f in embed.fields:
        print(f"{C_BOLD}【{f.name}】{C_RESET}\n{f.value}\n")


async def run_scenario_1() -> None:
    print_banner("情境一演練：NVDA 轉弱，SPCX 轉強符合轉倉條件 (Opportunity Cost)")

    engine = DynamicRolloverEngine()

    print_section("1. 持倉與市場微結構遙測 (NVDA)")
    print(f" • 標的: {C_YELLOW}NVDA{C_RESET} (SATELLITE 現貨持倉)")
    print(
        " • 持倉現值: $15,000.00 | 股數: 77 股 | 成本: $180.00 | 現價: $195.00 (+8.3%)"
    )
    print(f" • PowerSqueeze: {C_RED}10.0 (Release / Neutral - 動能衰退 ⚠️){C_RESET}")
    print(" • Skew-Adjusted EV: Base 1.54% - 40% Skew 懲罰 = 1.23%")
    print(" • 做市商結構: PutWall $190.00 | CallWall $210.00 | 15m K 線未破位")

    print_section("2. 候選標的掃描與突破評估 (SPCX)")
    print(f" • 候選標的: {C_GREEN}SPCX{C_RESET} (Watchlist 最佳高 EV 現貨標的)")
    print(" • 即時現價: $85.00 | Expected Move Upper: $105.00")
    print(
        f" • PowerSqueeze: {C_GREEN}95.0 (High Squeeze + Long + Breakout Long - 突破待發 🚀){C_RESET}"
    )
    print(" • Skew-Adjusted EV: (105-85)/85 = 23.50%")
    print(f" • 期望值利差 (EV Spread): {C_GREEN}+22.27%{C_RESET} (大幅超越 5.5% 門檻)")

    print_section("3. 防洗盤進場訊號六重嚴格過濾鐵律檢驗")
    gates = [
        (
            "條件一",
            "結構性右側突破",
            "15m 實體收盤 $85.50 > Gamma Flip $80.00，量能 2.5x 均量",
            "通過 ✅",
        ),
        (
            "條件二",
            "做市商正 Gamma 底牆",
            "Support GEX Wall $80.00 (+1.5M GEX 支撐彈簧床)",
            "通過 ✅",
        ),
        (
            "條件三",
            "UOA 無實質物理封頂",
            "Call Wall $95.00 (空間 11.8% > 5%)，無 STO 蓋頂",
            "通過 ✅",
        ),
        (
            "條件四",
            "主力 UOA 買盤",
            "偵測到 $90C BTO 主力買盤，DTE = 21 天 (>= 7 天)",
            "通過 ✅",
        ),
        ("條件五", "總經與財報風控", "距財報 45 天，大盤處於 NORMAL 模式", "通過 ✅"),
        ("條件六", "效期雜訊過濾", "最近效期選擇權 DTE = 5 天 (> 1 天)", "通過 ✅"),
    ]
    for g_num, g_name, g_desc, g_res in gates:
        print(f" • {g_num}【{g_name}】: {g_desc} ➔ {C_GREEN}{g_res}{C_RESET}")

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 195.0,
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 15000.0,
            "skew_percentile": 40.0,
            "psq_result": {"squeeze_level": "Release", "signal_direction": "Neutral"},
        }
    ]

    candidate_radar: Dict[str, Any] = {
        "quote": {"c": 85.0},
        "iv_metrics": {"iv_rank": 25.0},
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "gex_profile_data": {
            "gex_profile": {"75.0": -500000.0, "80.0": 1500000.0, "95.0": -500000.0},
            "call_wall": 95.0,
            "put_wall": 80.0,
        },
        "uoa": [
            {
                "type": "CALL",
                "action": "BTO",
                "strike": 90.0,
                "expiry": (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d"),
                "ratio": 1.5,
            }
        ],
    }

    def cache_side_effect(
        symbol: str, expiry: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if symbol.upper() == "NVDA":
            return {
                "reference_spot_price": 195.0,
                "expected_move_upper": 198.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        if symbol.upper() == "SPCX":
            return {
                "reference_spot_price": 85.0,
                "expected_move_upper": 105.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        return None

    df_data: Dict[str, List[float]] = {
        "Close": [80.0] * 20 + [85.50],
        "Volume": [10000.0] * 20 + [25000.0],
    }

    with patch(
        "database.market_cache.get_market_cache", side_effect=cache_side_effect
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=pd.DataFrame(df_data),
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[(datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")],
    ), patch(
        "database.calendar_cache.get_cached_earnings",
        return_value={
            "earnings_date": (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
        },
    ), patch(
        "market_analysis.index_microstructure.get_market_regime",
        new_callable=AsyncMock,
        return_value="NORMAL",
    ):
        (
            instructions,
            _entry_confirmation,
        ) = await engine.evaluate_opportunity_cost_for_satellites(
            user_id=101,
            portfolio_assets=portfolio_assets,
            already_flagged_symbols=set(),
            candidate_symbol="SPCX",
            candidate_radar=candidate_radar,
        )

    print_section("4. 轉倉決策輸出")
    ins = instructions[0]
    print(f" • 觸發情境: {C_YELLOW}{ins['scenario']}{C_RESET}")
    print(
        f" • 執行動作: {C_GREEN}{ins['action']}{C_RESET} (減碼 {ins['sell_ratio']:.0%})"
    )
    print(f" • 轉入資產: {C_GREEN}{ins['target_core']}{C_RESET}")
    print(f" • 建議限價: ${ins['limit_price']:.2f}")
    print(f" • 回收資金: {ins['cash_impact']}")

    embed = create_dynamic_rollover_embed(
        rollover_type="機會成本轉倉",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price=f"${ins['limit_price']:.2f} (限價)",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )
    print_embed_preview(embed)


async def run_scenario_2() -> None:
    print_banner("情境二演練：NVDA 轉弱，沒有標的符合轉倉條件")

    print_section("子情境 2A：Watchlist 無候選標的 ➔ S2 早退，S3 安心防守 (HOLD)")
    print(" • Watchlist 所有標的 EV <= 0.05 或 3 天內有財報，目標鎖定 VOO")
    print(" • S2 機會成本引擎: 直接早退，不發送無效雜訊")
    print(
        " • S3 持倉檢驗: NVDA 現價 $195.00 > Stop Loss $185.50 (PutWall $190 - 1.5x ATR $3.0)"
    )
    print(" • 決策: 產出安心防守卡，嚴守 15 分鐘實體 K 線收盤撤退線")

    embed_2a = create_dynamic_rollover_embed(
        rollover_type="持倉防守 (核心衛星再平衡)",
        sell_symbol="NVDA",
        sell_ratio=0.0,
        buy_symbol="NVDA",
        reason="微結構判定: GEX Wall $190.00 護城河完好，阻力天花板 $210.00\n防守機制: 建議設置防守委託單 停損: $185.50",
        suggested_strategy="HOLD (維持現狀續抱)",
        suggested_price="N/A (維持現狀)",
        strike="N/A",
        expiry="N/A",
        direction="HOLD",
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        asset_class="SPOT",
    )
    print_embed_preview(embed_2a)

    print_section("子情境 2B：候選標的被六重進場鐵律攔截 ➔ 系統靜默早退")
    print(
        " • Watchlist 有 SPCX，但在 $88.00 爆出單筆 ratio=4.0x OI 的 STO Call 巨量壓頂"
    )
    print(f" • 條件三判定: {C_RED}❌ 偵測到 STO Call 物理封頂 @ $88.00{C_RESET}")
    print(" • 系統行為: 靜默早退，阻擋追高與踩入主力出貨陷阱")

    print_section("子情境 2C：NVDA 實體跌破防守線 (結構破位) ➔ 強制 100% 撤退回防 VOO")
    print(" • NVDA 15m 實體收盤 $184.00 跌破防守線 $185.50，Gamma Cliff 確認崩塌")
    print(
        " • 機構風控鐵律: 強制 100% 清倉 (LIQUIDATE)，撤退回防大盤核心 VOO，嚴禁追逐高波衛星標的"
    )

    embed_2c = create_dynamic_rollover_embed(
        rollover_type="核心衛星再平衡",
        sell_symbol="NVDA",
        sell_ratio=1.0,
        buy_symbol="VOO",
        reason="1. 盤勢定調: 現價 $184.00 | IV 位階: 45.0%\n2. 主力意圖: GEX Wall $190.00 失守\n3. 建議: 🚨 15m 實體破位確認：15 分鐘實體收盤跌破 $185.50，負 Gamma 助跌啟動，強制 100% 轉入 VOO 防禦。",
        suggested_strategy="100% LIQUIDATE (轉入 VOO)",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
        cash_impact="$14,168",
        asset_class="SPOT",
    )
    print_embed_preview(embed_2c)

    print_section("子情境 2D：大盤負 Gamma 踩踏 + 保證金危機 ➔ 強制 100% 轉入 BOXX")
    print(" • 大盤進入 SHORT_GAMMA_CRITICAL 負 Gamma 踩踏模式，帳戶存在保證金赤字")
    print(" • NVDA 判定無邊際優勢 (No-Edge)，觸發 Scenario 4 保證金防禦")
    print(
        " • 機構風控鐵律: 強制 100% 清倉轉入純現金等價物 BOXX 鎖定無風險利息 (絕非 VOO)"
    )

    embed_2d = create_dynamic_rollover_embed(
        rollover_type="槓桿與保證金防禦",
        sell_symbol="NVDA",
        sell_ratio=1.0,
        buy_symbol="BOXX",
        reason="🚨 大盤處於 SHORT_GAMMA_CRITICAL 負 Gamma 踩踏模式且帳戶存在維持率壓力。NVDA 結構破位無邊際優勢，強制平倉轉入 BOXX 鎖定無風險利息。",
        suggested_strategy="100% LIQUIDATE (轉入 BOXX 鎖定無風險利息)",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        buy_action_label="轉入 BOXX（鎖定無風險利息）",
        scenario=RolloverScenario.MARGIN_DEFENSE.value,
        cash_impact="$14,168",
        asset_class="SPOT",
    )
    print_embed_preview(embed_2d)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus Seeker 動態轉倉演練執行工具")
    parser.add_argument(
        "--scenario",
        choices=["1", "2", "all"],
        default="all",
        help="指定演練情境 (1: SPCX轉強, 2: 無標的符合, all: 全部)",
    )
    args = parser.parse_args()

    if args.scenario in ("1", "all"):
        await run_scenario_1()
    if args.scenario in ("2", "all"):
        await run_scenario_2()


if __name__ == "__main__":
    asyncio.run(main())
