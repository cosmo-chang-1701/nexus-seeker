#!/usr/bin/env python3
"""audit_macro_market_live.py — 即時執行 /force_macro_update 與 /market 並執行 7 大校驗電池。

本腳本於 Docker 容器環境內執行，完整走訪：
1. Pre-run: 快照 SQLite kv_cache 與 economic_calendar_events 狀態。
2. Phase 1A: 執行 Discord 管理員指令 /force_macro_update 路徑 (AdminCommandsCog)。
3. Phase 1B: 執行 CLI 管理員路徑 (refresh_macro_data(include_vts_and_core=True))。
4. Post-update: 檢查 DB 異動（寫入時間戳、數值變更）。
5. Phase 2: 執行 /market 核心資料管線 (get_macro_overview_data)。
6. Phase 3: 渲染 build_market_macro_overview_embed (Discord Embed 與 ANSI Panels)。
7. Phase 4: 執行 7 大校驗電池 (Validation Batteries)。
8. Phase 5: 產出完整量化校驗分析報告。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Ensure root of nexus_core is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import config
from cogs.embed_builders.market_embeds import build_market_macro_overview_embed
from cogs.trading.admin_commands import AdminCommandsCog
from cogs.unified_terminal.utils import get_macro_overview_data
from database.connection import DatabaseWriteQueue, connect_db
from services.macro_refresh_service import MacroRefreshResult, refresh_macro_data

# 設定 logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("audit_macro_market_live")


@dataclass
class BatteryResult:
    battery_id: str
    name: str
    passed: bool
    details: dict[str, Any]
    errors: list[str]
    warnings: list[str]


def _read_macro_kv_snapshot() -> dict[str, dict[str, Any]]:
    """讀取當前 kv_cache 中所有 macro_% 鍵值的快照。"""
    conn = connect_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, value, updated_at FROM kv_cache WHERE key LIKE 'macro_%' ORDER BY key"
        )
        rows = cursor.fetchall()
        result: dict[str, dict[str, Any]] = {}
        for k, v, updated_at in rows:
            result[str(k)] = {"value": v, "updated_at": updated_at}
        return result
    finally:
        conn.close()


def _read_calendar_events_count() -> int:
    """讀取 economic_calendar_events 目前總筆數。"""
    conn = connect_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM economic_calendar_events")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _make_mock_interaction(admin_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = admin_id
    interaction.user.name = "TestAdmin"
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def run_audit() -> dict[str, Any]:
    logger.info("=================================================================")
    logger.info("🌌 開始執行 Nexus Seeker 宏觀數據強制更新與市場面板深度審計")
    logger.info("=================================================================")

    loop = asyncio.get_running_loop()
    if not DatabaseWriteQueue.is_active():
        DatabaseWriteQueue.initialize(loop)
        logger.info("✅ DatabaseWriteQueue 已初始化")

    audit_summary: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "admin_user_id": config.DISCORD_ADMIN_USER_ID,
        "pre_snapshot": {},
        "post_snapshot": {},
        "discord_force_update": {},
        "cli_force_update": {},
        "macro_overview_data": {},
        "embed_verification": {},
        "batteries": {},
    }

    # -------------------------------------------------------------
    # Phase 0: Pre-run DB 快照
    # -------------------------------------------------------------
    logger.info("\n--- Phase 0: 讀取 Pre-run DB 快照 ---")
    pre_kv = _read_macro_kv_snapshot()
    pre_cal_count = _read_calendar_events_count()
    audit_summary["pre_snapshot"] = {
        "kv_count": len(pre_kv),
        "keys": pre_kv,
        "calendar_events_count": pre_cal_count,
    }
    logger.info(
        f"Pre-run kv_cache macro 鍵數: {len(pre_kv)}, 日曆事件筆數: {pre_cal_count}"
    )

    # -------------------------------------------------------------
    # Phase 1A: 執行 Discord /force_macro_update 路徑
    # -------------------------------------------------------------
    logger.info("\n--- Phase 1A: 執行 Discord /force_macro_update ---")
    mock_bot = MagicMock()
    admin_cog = AdminCommandsCog(mock_bot)
    mock_interaction = _make_mock_interaction(config.DISCORD_ADMIN_USER_ID)

    t0_discord = time.monotonic()
    await admin_cog.force_macro_update.callback(admin_cog, mock_interaction)  # type: ignore
    t_discord_elapsed = time.monotonic() - t0_discord

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert mock_interaction.followup.send.await_count == 1
    sent_kwargs = mock_interaction.followup.send.call_args.kwargs
    discord_embed: discord.Embed = sent_kwargs.get("embed")
    assert discord_embed is not None

    logger.info(f"Discord 回應 Embed 標題: {discord_embed.title}")
    logger.info(f"Discord 回應 Embed 說明:\n{discord_embed.description}")

    audit_summary["discord_force_update"] = {
        "elapsed_seconds": round(t_discord_elapsed, 3),
        "embed_title": discord_embed.title,
        "embed_description": discord_embed.description,
        "embed_color": str(discord_embed.color),
    }

    # -------------------------------------------------------------
    # Phase 1B: 執行 CLI admin force-macro-update 路徑
    # -------------------------------------------------------------
    logger.info("\n--- Phase 1B: 執行 CLI force-macro-update (含 VTS 與 Core) ---")
    t0_cli = time.monotonic()
    cli_result: MacroRefreshResult = await refresh_macro_data(include_vts_and_core=True)
    t_cli_elapsed = time.monotonic() - t0_cli

    cli_steps = [
        {"name": s.name, "ok": s.ok, "message": s.message} for s in cli_result.steps
    ]
    for s in cli_result.steps:
        status_icon = "✅" if s.ok else "❌"
        logger.info(f"  {status_icon} [{s.name}] ok={s.ok}: {s.message}")

    audit_summary["cli_force_update"] = {
        "elapsed_seconds": round(t_cli_elapsed, 3),
        "all_ok": cli_result.all_ok,
        "succeeded_count": len(cli_result.succeeded),
        "failed_count": len(cli_result.failed),
        "steps": cli_steps,
        "gex": asdict(cli_result.gex) if cli_result.gex else None,
        "ted_spread": cli_result.ted_spread,
        "vts_ratio": cli_result.vts_ratio,
        "core_metrics": cli_result.core_metrics,
    }

    # -------------------------------------------------------------
    # Post-update: 檢查 DB 異動
    # -------------------------------------------------------------
    logger.info("\n--- 檢查 DB 快照異動 ---")
    post_kv = _read_macro_kv_snapshot()
    post_cal_count = _read_calendar_events_count()

    kv_mutations: dict[str, dict[str, Any]] = {}
    for k, post_info in post_kv.items():
        pre_info = pre_kv.get(k)
        if pre_info is None:
            kv_mutations[k] = {"type": "INSERTED", "new": post_info}
        elif (
            pre_info["updated_at"] != post_info["updated_at"]
            or pre_info["value"] != post_info["value"]
        ):
            kv_mutations[k] = {
                "type": "UPDATED",
                "old_val": pre_info["value"],
                "new_val": post_info["value"],
                "old_time": pre_info["updated_at"],
                "new_time": post_info["updated_at"],
            }

    audit_summary["post_snapshot"] = {
        "kv_count": len(post_kv),
        "calendar_events_count": post_cal_count,
        "calendar_events_delta": post_cal_count - pre_cal_count,
        "mutations_count": len(kv_mutations),
        "mutations": kv_mutations,
    }
    logger.info(
        f"Post-run kv_cache 異動鍵數: {len(kv_mutations)}, 日曆事件總數: {post_cal_count}"
    )
    for k, m in kv_mutations.items():
        logger.info(f"  Mutation [{k}]: {m}")

    # -------------------------------------------------------------
    # Phase 2: 執行 /market 資料管線
    # -------------------------------------------------------------
    logger.info("\n--- Phase 2: 執行 /market 資料管線 (get_macro_overview_data) ---")
    t0_market = time.monotonic()
    macro_data: dict[str, Any] = await get_macro_overview_data(
        config.DISCORD_ADMIN_USER_ID
    )
    t_market_elapsed = time.monotonic() - t0_market

    audit_summary["macro_overview_data"] = macro_data
    logger.info(f"get_macro_overview_data 耗時: {t_market_elapsed:.3f}s")
    logger.info("核心數據欄位摘要:")
    for k in [
        "spx",
        "spy_spot",
        "vix",
        "us10y",
        "wti",
        "spy_gamma_flip",
        "gamma_flip_line",
        "gex_is_fallback",
        "gex_is_expired",
        "rrp",
        "fed_balance",
        "fear_greed",
        "uer",
        "sahm_rule",
        "short_gamma_critical",
        "recession_warning",
        "escape_win_status",
        "fedwatch_probability",
        "cpi_actual",
        "cpi_expected",
        "cpi_is_fallback",
    ]:
        logger.info(f"  - {k}: {macro_data.get(k)}")

    # -------------------------------------------------------------
    # Phase 3: 渲染 build_market_macro_overview_embed
    # -------------------------------------------------------------
    logger.info("\n--- Phase 3: 渲染 build_market_macro_overview_embed ---")
    market_embed: discord.Embed = build_market_macro_overview_embed(macro_data)
    audit_summary["embed_verification"] = {
        "title": market_embed.title,
        "description": market_embed.description,
        "color": str(market_embed.color),
        "fields_count": len(market_embed.fields),
        "fields": [
            {"name": f.name, "value": f.value, "inline": f.inline}
            for f in market_embed.fields
        ],
    }

    # -------------------------------------------------------------
    # Phase 4: 7 大校驗電池 (Validation Batteries)
    # -------------------------------------------------------------
    logger.info("\n--- Phase 4: 執行 7 大校驗電池 ---")
    batteries: dict[str, BatteryResult] = {}

    # Battery 1: 市場報價合理性與衍生一致性
    b1_errors: list[str] = []
    b1_warnings: list[str] = []
    spx = macro_data.get("spx")
    spy = macro_data.get("spy_spot")
    vix = macro_data.get("vix")
    us10y = macro_data.get("us10y")
    wti = macro_data.get("wti")

    for sym, val in [
        ("SPX", spx),
        ("SPY", spy),
        ("VIX", vix),
        ("US10Y", us10y),
        ("WTI", wti),
    ]:
        if val is None or float(val) <= 0:
            b1_errors.append(f"{sym} 報價缺失或非正數: {val}")

    spx_spy_ratio: float | None = None
    if spx is not None and spy is not None and float(spy) > 0:
        spx_spy_ratio = float(spx) / float(spy)
        if not (9.8 <= spx_spy_ratio <= 10.3):
            b1_errors.append(
                f"SPX/SPY 基差比值 {spx_spy_ratio:.4f} 超出合理區間 [9.8, 10.3]"
            )
    else:
        b1_errors.append("無法計算 SPX/SPY 基差比值（數值缺失）")

    batteries["battery_1"] = BatteryResult(
        battery_id="battery_1",
        name="市場報價合理性與衍生一致性",
        passed=len(b1_errors) == 0,
        details={
            "spx": spx,
            "spy": spy,
            "vix": vix,
            "us10y": us10y,
            "wti": wti,
            "spx_spy_ratio": round(spx_spy_ratio, 4) if spx_spy_ratio else None,
        },
        errors=b1_errors,
        warnings=b1_warnings,
    )

    # Battery 2: GEX 微結構與 Gamma 翻轉線換算
    b2_errors: list[str] = []
    b2_warnings: list[str] = []
    spy_gamma_flip = macro_data.get("spy_gamma_flip")
    gamma_flip_line = macro_data.get("gamma_flip_line")
    gex_fallback = macro_data.get("gex_is_fallback")
    gex_expired = macro_data.get("gex_is_expired")
    gex_age = macro_data.get("gex_cache_age_seconds")

    if spy_gamma_flip is None or float(spy_gamma_flip) <= 0:
        b2_errors.append(f"spy_gamma_flip 缺失或非正數: {spy_gamma_flip}")
    if gamma_flip_line is None or float(gamma_flip_line) <= 0:
        b2_errors.append(f"gamma_flip_line 缺失或非正數: {gamma_flip_line}")

    # 驗證換算邏輯：若 SPX 與 SPY 都在且 ratio 落在 [9.8, 10.3]，應以比值換算
    if (
        spy_gamma_flip
        and gamma_flip_line
        and spx_spy_ratio
        and 9.8 <= spx_spy_ratio <= 10.3
    ):
        expected_line = round(float(spy_gamma_flip) * spx_spy_ratio, 2)
        if abs(float(gamma_flip_line) - expected_line) > 0.05:
            b2_errors.append(
                f"gamma_flip_line ({gamma_flip_line}) 與預期換算值 ({expected_line}) 不一致"
            )

    if gex_fallback:
        b2_warnings.append("GEX 資料標記為備援/快取模式 (gex_is_fallback=True)")
    if gex_expired:
        b2_warnings.append(f"GEX 快取標記為已過期 (age={gex_age}s)")

    batteries["battery_2"] = BatteryResult(
        battery_id="battery_2",
        name="GEX 微結構與 Gamma 翻轉線換算",
        passed=len(b2_errors) == 0,
        details={
            "spy_spot": spy,
            "spy_gamma_flip": spy_gamma_flip,
            "gamma_flip_line": gamma_flip_line,
            "gex_is_fallback": gex_fallback,
            "gex_is_expired": gex_expired,
            "gex_cache_age_seconds": gex_age,
        },
        errors=b2_errors,
        warnings=b2_warnings,
    )

    # Battery 3: 系統級流動性與核心總經指標
    b3_errors: list[str] = []
    b3_warnings: list[str] = []
    ted_spread = cli_result.ted_spread
    rrp = macro_data.get("rrp")
    fed_balance = macro_data.get("fed_balance")
    fear_greed = macro_data.get("fear_greed")
    uer = macro_data.get("uer")
    sahm_rule = macro_data.get("sahm_rule")
    rrp_change_30d = macro_data.get("rrp_change_30d")

    if ted_spread is None:
        b3_warnings.append("TED Spread 即時數據未取得 (None)")
    elif float(ted_spread) < 0:
        b3_errors.append(f"TED Spread 為負值: {ted_spread}")

    if rrp is None or float(rrp) < 0:
        b3_errors.append(f"RRP 逆回購餘額缺失或為負: {rrp}")
    if fed_balance is None or float(fed_balance) <= 0:
        b3_errors.append(f"聯準會資產負債表缺失或非正數: {fed_balance}")
    if fear_greed is None or not (0.0 <= float(fear_greed) <= 100.0):
        b3_errors.append(f"恐懼與貪婪指數超出 [0, 100] 區間: {fear_greed}")
    if uer is None or float(uer) <= 0 or float(uer) > 25.0:
        b3_errors.append(f"失業率 UER 異常: {uer}")
    if sahm_rule is None:
        b3_errors.append("薩姆規則數值缺失")

    batteries["battery_3"] = BatteryResult(
        battery_id="battery_3",
        name="系統級流動性與核心總經指標",
        passed=len(b3_errors) == 0,
        details={
            "ted_spread": ted_spread,
            "rrp": rrp,
            "fed_balance": fed_balance,
            "fear_greed": fear_greed,
            "uer": uer,
            "sahm_rule": sahm_rule,
            "rrp_change_30d": rrp_change_30d,
        },
        errors=b3_errors,
        warnings=b3_warnings,
    )

    # Battery 4: CME FedWatch 利率定價與階梯拆解
    b4_errors: list[str] = []
    b4_warnings: list[str] = []
    fw_prob = macro_data.get("fedwatch_probability")
    fw_details = macro_data.get("fedwatch_details") or {}
    fw_is_fallback = macro_data.get("fedwatch_is_fallback")

    if fw_prob is None:
        b4_errors.append("FedWatch probability 數值缺失")

    prob_m = fw_details.get("prob_maintain")
    prob_h = fw_details.get("prob_hike")
    prob_c = fw_details.get("prob_cut")
    decision = fw_details.get("decision")
    meeting_date = fw_details.get("meeting_date")

    if meeting_date is None or not meeting_date:
        b4_warnings.append("FedWatch 未解析出 meeting_date")

    if prob_m is not None and prob_h is not None and prob_c is not None:
        prob_sum = float(prob_m) + float(prob_h) + float(prob_c)
        if abs(prob_sum - 100.0) > 1.0:
            b4_errors.append(
                f"FedWatch 機率守恆失敗: maintain({prob_m}) + hike({prob_h}) + cut({prob_c}) = {prob_sum:.2f}% != 100%"
            )

        # 決策文字邏輯相符性
        if (
            decision == "maintain"
            and float(prob_m) < float(prob_h)
            and float(prob_m) < float(prob_c)
        ):
            b4_errors.append(f"決策 maintain 但維持機率並非優勢: {fw_details}")
        elif (
            decision == "hike"
            and float(prob_h) <= float(prob_m)
            and float(prob_h) <= float(prob_c)
        ):
            b4_errors.append(f"決策 hike 但加息機率並非優勢: {fw_details}")
        elif (
            decision == "cut"
            and float(prob_c) <= float(prob_m)
            and float(prob_c) <= float(prob_h)
        ):
            b4_errors.append(f"決策 cut 但降息機率並非優勢: {fw_details}")

    if fw_is_fallback:
        b4_warnings.append("FedWatch 使用備援快取")

    batteries["battery_4"] = BatteryResult(
        battery_id="battery_4",
        name="CME FedWatch 利率定價與階梯拆解",
        passed=len(b4_errors) == 0,
        details={
            "probability": fw_prob,
            "meeting_date": meeting_date,
            "decision": decision,
            "prob_maintain": prob_m,
            "prob_hike": prob_h,
            "prob_cut": prob_c,
            "ladder_saturated": fw_details.get("ladder_saturated"),
            "is_fallback": fw_is_fallback,
            "source": fw_details.get("source"),
        },
        errors=b4_errors,
        warnings=b4_warnings,
    )

    # Battery 5: CPI YoY 通膨年增率與偏差計算
    b5_errors: list[str] = []
    b5_warnings: list[str] = []
    cpi_actual = macro_data.get("cpi_actual")
    cpi_expected = macro_data.get("cpi_expected")
    cpi_fallback = macro_data.get("cpi_is_fallback")

    if cpi_actual is None:
        b5_errors.append("cpi_actual 缺失")
    if cpi_expected is None:
        b5_errors.append("cpi_expected 缺失")

    cpi_diff: float | None = None
    if cpi_actual is not None and cpi_expected is not None:
        cpi_diff = round(float(cpi_actual) - float(cpi_expected), 2)
        if not (-5.0 <= cpi_diff <= 5.0):
            b5_warnings.append(f"CPI 預期偏差較大: {cpi_diff}%")

    if cpi_fallback:
        b5_warnings.append("CPI 採用備援值")

    batteries["battery_5"] = BatteryResult(
        battery_id="battery_5",
        name="CPI YoY 通膨年增率與偏差計算",
        passed=len(b5_errors) == 0,
        details={
            "cpi_actual": cpi_actual,
            "cpi_expected": cpi_expected,
            "cpi_diff": cpi_diff,
            "cpi_is_fallback": cpi_fallback,
        },
        errors=b5_errors,
        warnings=b5_warnings,
    )

    # Battery 6: 風控引擎聯動判定狀態機
    b6_errors: list[str] = []
    b6_warnings: list[str] = []
    short_gamma_critical = macro_data.get("short_gamma_critical")
    recession_warning = macro_data.get("recession_warning")
    escape_win_status = macro_data.get("escape_win_status")
    escape_dir = macro_data.get("escape_window_direction")
    escape_shift = macro_data.get("escape_window_shift_days")
    escape_tier = macro_data.get("escape_window_tier")
    payout_threshold = macro_data.get("payout_threshold")

    # 驗證衰退警告邏輯
    expected_recession = (sahm_rule is not None and float(sahm_rule) >= 0.5) or (
        us10y is not None
        and vix is not None
        and float(us10y) > 4.5
        and float(vix) > 20.0
    )
    if recession_warning != expected_recession:
        b6_errors.append(
            f"recession_warning ({recession_warning}) 與預期 ({expected_recession}) 不一致！"
            f" (sahm={sahm_rule}, us10y={us10y}, vix={vix})"
        )

    # 驗證 short_gamma_critical 邏輯
    # SPY vs SPY Flip
    vts_val = cli_result.vts_ratio
    is_backwardation = (
        (vts_val >= 1.0)
        if (vts_val is not None and vts_val > 0.0)
        else (vix is not None and float(vix) > 25.0)
    )
    is_neg_gamma = (
        float(spy) < float(spy_gamma_flip)
        if (spy is not None and spy_gamma_flip is not None and not gex_expired)
        else None
    )
    expected_short_gamma = (
        is_neg_gamma is True
        and (vix is not None and float(vix) > 20.0)
        and is_backwardation
    )
    if short_gamma_critical != expected_short_gamma:
        b6_errors.append(
            f"short_gamma_critical ({short_gamma_critical}) 與預期 ({expected_short_gamma}) 不一致！"
            f" (is_neg_gamma={is_neg_gamma}, vix={vix}, is_backwardation={is_backwardation})"
        )

    if payout_threshold != 13000.0:
        b6_warnings.append(f"安全提領紅線門檻非預設 13,000: {payout_threshold}")

    batteries["battery_6"] = BatteryResult(
        battery_id="battery_6",
        name="風控引擎聯動判定狀態機",
        passed=len(b6_errors) == 0,
        details={
            "short_gamma_critical": short_gamma_critical,
            "expected_short_gamma": expected_short_gamma,
            "recession_warning": recession_warning,
            "expected_recession": expected_recession,
            "escape_win_status": escape_win_status,
            "escape_window_direction": escape_dir,
            "escape_window_shift_days": escape_shift,
            "escape_window_tier": escape_tier,
            "payout_threshold": payout_threshold,
        },
        errors=b6_errors,
        warnings=b6_warnings,
    )

    # Battery 7: Discord Embed 輸出完整性與 ANSI 色碼相容性
    b7_errors: list[str] = []
    b7_warnings: list[str] = []
    field_names = [f.name for f in market_embed.fields]

    expected_panels = [
        "📊 大盤與核心指標 (Market & Core Indices)",
        "🛡️ 聯動風控引擎狀態 (Risk Engine Status)",
        "📈 流動性與總經指標 (Liquidity & Macro)",
        "📅 總經事件公布日程 (Macro Calendar)",
    ]
    for p in expected_panels:
        if p not in field_names:
            b7_errors.append(f"Embed 缺少預期 Field 面板: {p}")

    for f in market_embed.fields:
        f_val = f.value or ""
        f_name = f.name or ""
        if "```ansi" in f_val:
            if not f_val.endswith("```"):
                b7_errors.append(f"Field [{f_name}] 的 ANSI 區塊未正常閉合")
        if "獲取數據失敗" in f_val:
            b7_warnings.append(f"Field [{f_name}] 包含「獲取數據失敗」字串")

    desc_len = len(market_embed.description or "")
    fields_len = sum(
        len(f.name or "") + len(f.value or "") for f in market_embed.fields
    )
    batteries["battery_7"] = BatteryResult(
        battery_id="battery_7",
        name="Discord Embed 輸出完整性與 ANSI 色碼相容性",
        passed=len(b7_errors) == 0,
        details={
            "fields_found": field_names,
            "total_embed_length": desc_len + fields_len,
        },
        errors=b7_errors,
        warnings=b7_warnings,
    )

    audit_summary["batteries"] = {k: asdict(v) for k, v in batteries.items()}

    # -------------------------------------------------------------
    # 輸出總結與報告
    # -------------------------------------------------------------
    logger.info("\n=================================================================")
    logger.info("🏁 7 大校驗電池結果總結:")
    logger.info("=================================================================")
    all_passed = True
    for bid, b_res in batteries.items():
        status_str = "🟢 PASS" if b_res.passed else "🔴 FAIL"
        logger.info(f"[{bid.upper()}] {status_str}: {b_res.name}")
        if b_res.errors:
            for err in b_res.errors:
                logger.error(f"    ❌ Error: {err}")
        if b_res.warnings:
            for warn in b_res.warnings:
                logger.warning(f"    ⚠️ Warning: {warn}")
        if not b_res.passed:
            all_passed = False

    audit_summary["all_batteries_passed"] = all_passed

    # 停止 worker
    await DatabaseWriteQueue.stop_worker()
    logger.info("✅ DatabaseWriteQueue 已安全停止")

    return audit_summary


if __name__ == "__main__":
    result = asyncio.run(run_audit())
    output_path = "data/audit_macro_market_live_report.json"
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"\n完整審計結果已輸出至: {output_path}")
    except Exception as e:
        logger.warning(
            f"寫入 {output_path} 失敗 ({e})，改寫入 /tmp/audit_macro_market_live_report.json"
        )
        output_path = "/tmp/audit_macro_market_live_report.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"\n完整審計結果已輸出至: {output_path}")
    if not result.get("all_batteries_passed"):
        logger.error("❌ 部分校驗電池未通過！")
        sys.exit(1)
    else:
        logger.info("🎯 7 大校驗電池全數通過！")
        sys.exit(0)
