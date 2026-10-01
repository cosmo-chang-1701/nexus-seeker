#!/usr/bin/env python3
"""
Nexus Seeker - Dynamic Rollover Engine Real-Time Simulation Drill (動態轉倉演練)
=============================================================================
This script simulates and prints step-by-step telemetry, decision matrices,
and resulting Discord Embed notifications for:
  - Scenario 1: NVDA turns weak, SPCX turns strong and passes all 6 entry gates.
  - Scenario 2: NVDA turns weak, no candidate meets criteria (Hold / Blocked / VOO / BOXX).
  - Scenario 3: TSLA breaks below the Put Wall (Regime V) -> standalone SHORT_ENTRY
    instruction with levels and sizing; long-only downstream paths stay silent.
"""

import argparse
import asyncio
import os
import sys
from typing import Any

# Ensure root of nexus_core is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from cogs.embed_builders.rollover_embeds import create_dynamic_rollover_embed
from market_analysis.dynamic_rollover import (
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


async def run_scenario_3() -> None:
    print_banner("情境三演練：TSLA 跌破 Put Wall，Regime V 破位追空 (Short Side)")

    print_section("1. 盤勢分類與 Regime 路由 (TSLA)")
    print(f" • 標的: {C_YELLOW}TSLA{C_RESET} | 交易策略: 動態調整 (DYNAMIC)")
    print(" • 即時現價: $92.00 | Session VWAP: $99.00 | Gamma Flip: $100.00")
    print(" • 做市商結構: PutWall $95.00 (已跌破) | ResistanceWall $97.00 (+3.0M GEX)")
    print(" • 全鏈 Net GEX: -2.5M (負 Gamma 順勢助跌) | 15m RSI: 38.0")
    print(" • ATR₁₅ₘ: $1.00 | ATR₁D: $5.00 | VIX: 20.0 (摩拳擦掌)")
    print(
        f" • 分類結果: {C_RED}Regime V 破位追空態{C_RESET} "
        "(跌破 Gamma Flip / VWAP / Put Wall，放量陰線，RSI < 45)"
    )

    print_section("2. 做空進場訊號六重嚴格過濾鐵律檢驗")
    gates = [
        (
            "條件一",
            "結構性放量破位",
            "15m 實體陰線收盤 $92.00 < Gamma Flip $100.00，量能 2.0x，跌破 VWAP",
        ),
        (
            "條件二",
            "做市商負 Gamma 頂牆",
            "停損 = $97.00 + 0.5×ATR₁₅ₘ = $97.50，停損距離 5.98% 落在 [2.72%, 8%] 內",
        ),
        (
            "條件三",
            "下行空間 + 無接刀",
            "[破位追空] 至次級負 Gamma 節點 $80.00 空間 13.04% >= 10.87% (2.0×ATR₁D)",
        ),
        (
            "條件四",
            "主力跨週期賣壓",
            "$90P BTO 主力方向性押注，DTE 25、ratio 1.50x、權利金 $400,000",
        ),
        ("條件五", "總經與財報安全閥", "距財報 45 天，大盤 NORMAL"),
        (
            "條件六",
            "效期與 IVR 分流",
            "最近效期 DTE 25 (>= 14)，IVR 20% -> Long Put (輕度 OTM)",
        ),
    ]
    for g_num, g_name, g_desc in gates:
        print(f" • {g_num}【{g_name}】: {g_desc} ➔ {C_GREEN}通過 ✅{C_RESET}")

    from cogs.embed_builders.rollover_embeds import create_short_entry_embed
    from market_analysis.dynamic_rollover.models import ShortEntryEvaluation
    from market_analysis.dynamic_rollover.short_entry_sizing import (
        build_short_entry_levels,
        build_short_entry_plan,
        compute_short_entry_sizing,
    )

    ev = ShortEntryEvaluation(
        all_passed=True,
        reason=" | ".join(f"做空{g[0]}✅：{g[2]}" for g in gates),
        structure_directive="Long Put (輕度 OTM)",
        sub_mode="破位追空",
        conditions=(True, True, True, True, True, True),
        spot=92.0,
        resistance_wall=97.0,
        call_wall=97.0,
        put_wall=95.0,
        gamma_flip=100.0,
        next_negative_node=80.0,
        net_gex=-2_500_000.0,
        session_vwap=99.0,
        atr_15m=1.0,
        atr_1d=5.0,
        ivr=20.0,
    )

    print_section("3. 下游隔離：做空確認只走 SHORT_ENTRY")
    print(
        f" • 做空確認 ➔ {C_GREEN}僅由 SHORT_ENTRY 情境產生獨立的做空進場訊號{C_RESET}"
        "（不進入任何多頭買進路徑）"
    )

    print_section("4. SHORT_ENTRY 價位與倉位計算")
    levels = build_short_entry_levels(ev)
    if levels is None:
        print(f" {C_RED}價位不合法，fail-closed{C_RESET}")
        return
    sizing = compute_short_entry_sizing(
        levels, 100_000.0, 15.0, vix_spot=20.0, rsi_15m=38.0
    )
    print(f" • 進場 (限價放空): ${levels.entry_price:.2f}")
    print(
        f" • 停損: 結構 ${levels.stop_price_structural:.2f} ／ 出場引擎 "
        f"${levels.stop_price_exit_engine:.2f} ➔ 取較遠者 {C_YELLOW}${levels.stop_price:.2f}{C_RESET}"
    )
    print(
        f" • 目標: 次級負 Gamma 節點 ${levels.target_price:.2f} | R:R {levels.reward_risk_ratio:.2f}"
    )
    print(
        f" • 風險預算: $100,000 × min(0.5%, 凱利 {sizing.kelly_fraction:.2%}) × VIX 乘數 "
        f"{sizing.short_vix_multiplier:.2f} = {C_CYAN}${sizing.risk_budget_usd:,.2f}{C_RESET}"
    )
    print(
        f" • 建議股數: {C_GREEN}{sizing.share_qty}{C_RESET} 股 (約束: {sizing.binding_constraint})"
    )

    print_section("5. 做空部位出場矩陣 (登錄負股數後接管)")
    print(" • SL-結構失效: Call Wall $97.00 + 0.5 × ATR₁₅ₘ = $97.50 (現價升穿即觸發)")
    print(" • SL-狀態翻轉: Net GEX >= 0 -> 強制 100% 回補")
    print(
        " • TP1/TP2 目標牆：現價已跌破 Put Wall，改以次級負 Gamma 節點 $80.00 為目標"
        "（避免部位一登錄就落在「跌破 Put Wall 1.5%」的 TP2 內）"
    )

    embed = create_short_entry_embed(
        "TSLA",
        f"🐻 **做空進場訊號 (Short Entry)**｜{levels.sub_mode}\n{ev.reason}",
        build_short_entry_plan(levels, sizing),
        ev.structure_directive,
        "REGIME_V_BREAKDOWN_CHASE",
    )
    print_embed_preview(embed)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus Seeker 動態轉倉演練執行工具")
    parser.add_argument(
        "--scenario",
        choices=["2", "3", "all"],
        default="all",
        help="指定演練情境 (2: 無標的符合, 3: TSLA破位追空, all: 全部)",
    )
    args = parser.parse_args()

    # 逐情境隔離：本演練腳本會對真實市場資料源發動請求，而情境一/二使用的是
    # 示範用代號 (SPCX)，在真實資料源上取不到 K 線就會拋例外。早期版本沒有
    # 隔離，任何一個情境的資料層失敗都會中止整個 main()，讓後續情境**完全
    # 不會執行**——演練工具的價值正在於一次跑完所有情境並比對，故改為逐個
    # 捕捉、印出失敗原因後繼續。
    scenarios = (
        ("2", run_scenario_2),
        ("3", run_scenario_3),
    )
    for key, fn in scenarios:
        if args.scenario not in (key, "all"):
            continue
        try:
            await fn()
        except Exception as e:
            print(
                f"\n{C_RED}⚠️ 情境 {key} 演練中止：{type(e).__name__}: {e}{C_RESET}\n"
                f"{C_YELLOW}   (本腳本會對真實資料源發動請求；示範用代號取不到 "
                f"K 線屬預期情形，不影響其餘情境){C_RESET}"
            )


if __name__ == "__main__":
    asyncio.run(main())
