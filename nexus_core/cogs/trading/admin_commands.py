"""
cogs/trading/admin_commands.py

[Admin] 管理員手動觸發指令：force_scan、force_after_report、force_macro_update。
"""

from typing import Any
import asyncio
import logging
import time

import discord
from discord.ext import commands
from discord import app_commands

from config import DISCORD_ADMIN_USER_ID
from cogs.embed_builder import (
    create_info_embed,
    create_error_embed,
)

logger = logging.getLogger(__name__)


class AdminCommandsCog(commands.Cog):
    """管理員手動觸發指令集合。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="force_scan", description="[Admin] 立即手動執行全站掃描 (不論開盤時間)"
    )
    async def force_scan(self, interaction: discord.Interaction) -> Any:
        if not getattr(self.bot, "_is_leader_instance", True):
            await interaction.response.send_message(
                embed=create_info_embed(
                    "系統控制",
                    "⚠️ 目前此實例為 follower（藍綠部署中）。請稍候或重新觸發指令。",
                ),
                ephemeral=True,
            )
            return
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_scan attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan"
        )
        await interaction.response.send_message(
            embed=create_info_embed("系統控制", "🚀 強制啟動全站掃描中..."),
            ephemeral=True,
        )
        scan_cog = self.bot.get_cog("MarketScanCog")
        if scan_cog:
            asyncio.create_task(
                scan_cog._run_market_scan_logic(
                    is_auto=False, triggered_by=interaction.user
                )
            )
        else:
            logger.error("MarketScanCog not found, cannot execute force_scan.")

    @app_commands.command(
        name="force_after_report",
        description="[Admin] 立即手動執行盤後結算報告 (可選 dry-run)",
    )
    @app_commands.describe(dry_run="true=只做計算與建構，不發送 DM")
    async def force_after_report(
        self, interaction: discord.Interaction, dry_run: bool = True
    ) -> Any:
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_after_report attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        mode = "DRY-RUN" if dry_run else "SEND"
        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_after_report mode={mode}"
        )
        await interaction.response.send_message(
            embed=create_info_embed(
                "系統控制", f"🧪 盤後結算報告手動執行中 (`{mode}`)..."
            ),
            ephemeral=True,
        )

        after_market_cog = self.bot.get_cog("AfterMarketCog")
        if after_market_cog:
            stats = await after_market_cog._run_after_market_report_pipeline(
                dry_run=dry_run, triggered_by=interaction.user
            )
            await interaction.followup.send(
                embed=create_info_embed(
                    "盤後結算報告完成",
                    (
                        f"mode: `{mode}`\n"
                        f"users_total: `{stats['users_total']}`\n"
                        f"users_queued: `{stats['users_queued']}`\n"
                        f"users_skipped: `{stats['users_skipped']}`\n"
                        f"users_failed: `{stats['users_failed']}`"
                    ),
                ),
                ephemeral=True,
            )
        else:
            logger.error("AfterMarketCog not found, cannot execute force_after_report.")

    @app_commands.command(
        name="force_macro_update",
        description="[Admin] 立即手動更新大盤總經數據 (GEX & FedWatch)",
    )
    async def force_macro_update(self, interaction: discord.Interaction) -> Any:
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_macro_update attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_macro_update"
        )
        await interaction.response.defer(ephemeral=True)

        from services.calendar_service import calendar_service

        errors: list[str] = []
        updated_lines: list[str] = []

        # 1. GEX 與流動性
        gex_info = await _refresh_macro_gex_and_liquidity(errors)
        if gex_info is not None:
            updated_lines.append(f"- **GEX**: {gex_info}")

        # 2. 總經日曆 (TradingView)。必須先於 FedWatch 執行：強制刷新日曆會經由
        # replace_macro_month_events() 以 DELETE + 重新 INSERT 覆寫整月事件，
        # 新列的 fedwatch_probability 為 NULL；若先寫入 FedWatch 再刷新日曆，
        # 剛寫進 FOMC 事件列的定價會被清空，/calendar 將顯示不到 FedWatch。
        try:
            calendar_ok = await calendar_service.prefetch_monthly_macro_cache(
                months_ahead=1, force_fetch=True
            )
            if calendar_ok:
                updated_lines.append("- **總經日曆**: 已重新抓取並寫入快取")
            else:
                errors.append("總經日曆更新失敗：邊緣爬蟲無有效回應，沿用既有快取")
        except Exception as e:
            logger.warning(f"force_macro_update 總經日曆更新失敗: {e}")
            errors.append(f"總經日曆更新失敗：{e}")

        # 3. FedWatch（失敗時不拋例外，以回傳值表示是否成功寫入）
        try:
            fedwatch_ok = await calendar_service.update_fedwatch_probability()
            if fedwatch_ok:
                updated_lines.append("- **FedWatch**: 最新利率定價已寫入資料庫")
            else:
                errors.append(
                    "FedWatch 更新失敗：無法取得有效利率定價（爬取失敗或數據未通過合理性檢查），沿用備援快取"
                )
        except Exception as e:
            logger.warning(f"force_macro_update FedWatch 更新失敗: {e}")
            errors.append(f"FedWatch 更新失敗：{e}")

        updated_block = "\n".join(updated_lines)
        if errors:
            err_msg = "\n".join(f"- {err}" for err in errors)
            desc = f"⚠️ 部分大盤總經數據更新失敗：\n{err_msg}"
            if updated_block:
                desc += f"\n\n已成功更新：\n{updated_block}"
            await interaction.followup.send(
                embed=create_error_embed(desc, title="更新部分失敗"),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_info_embed(
                    "系統控制",
                    f"✅ 大盤與總經數據更新成功！\n{updated_block}",
                ),
                ephemeral=True,
            )


def _positive_float(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num if num > 0 else 0.0


async def _fetch_spy_live_gex_fallback() -> dict[str, float] | None:
    """大盤 GEX 端點完全無資料（連 last-known-good 快取都沒有）時，改以 SPY
    個股期權鏈即時估算 Gamma Flip，並以單一交易寫回大盤 GEX 快取。

    回傳寫入成功的 GEX 數據；任何一步失敗回傳 None（不寫入任何鍵）。
    """
    from database.cache import save_kv_cache_many
    from market_analysis.index_microstructure import (
        estimate_symbol_gamma_flip,
        fetch_symbol_gex_metrics,
    )

    try:
        spy_gex = await fetch_symbol_gex_metrics("SPY", force_live=True)
    except Exception as e:
        logger.warning(f"SPY 個股端點即時計算 GEX 失敗: {e}")
        return None

    # force_live 即時抓取失敗時，fetch_symbol_gex_metrics 會優雅降級回傳帶
    # `_is_stale_cache` 標記的過期個股快取。過期資料不可當作即時數據寫回大盤
    # 快取（否則會以新時間戳覆寫 last-known-good，並把 macro_gex_is_fallback
    # 清為 0，讓下游誤判大盤 GEX 為新鮮數據）。
    if not isinstance(spy_gex, dict) or spy_gex.get("_is_stale_cache"):
        logger.warning("SPY 即時 GEX 抓取失敗（僅取得過期快取），不寫回大盤 GEX 快取")
        return None

    spot = _positive_float(spy_gex.get("spot"))
    if spot <= 0:
        return None
    gex_profile = spy_gex.get("gex_profile") or {}
    flip = estimate_symbol_gamma_flip(gex_profile, spot)
    if flip <= 0:
        return None

    gex_data = {
        "spy_spot": round(spot, 2),
        "gamma_flip": round(flip, 2),
        "put_wall": round(_positive_float(spy_gex.get("put_wall")), 2),
    }
    saved = await save_kv_cache_many(
        {
            "macro_spy_spot": gex_data["spy_spot"],
            "macro_spy_gamma_flip": gex_data["gamma_flip"],
            "macro_gamma_flip_line": gex_data["gamma_flip"] * 10.0,
            "macro_gex_is_fallback": 0,
            "macro_gex_metrics_cache": {"data": gex_data, "timestamp": time.time()},
        }
    )
    if not saved:
        logger.warning("SPY 即時估算 GEX 寫入大盤 GEX 快取失敗")
        return None
    return gex_data


async def _refresh_macro_gex_and_liquidity(errors: list[str]) -> str | None:
    """強制刷新大盤 GEX 與流動性指標。成功取得 GEX 時回傳顯示用摘要字串，
    失敗時回傳 None；各項失敗原因附加至 errors。"""
    try:
        from market_analysis.index_microstructure import (
            fetch_gex_metrics,
            fetch_liquidity_metrics,
            invalidate_market_regime_cache,
        )

        results: list[Any] = list(
            await asyncio.gather(
                fetch_gex_metrics(allow_empty=True),
                fetch_liquidity_metrics(),
                return_exceptions=True,
            )
        )
        gex_res, liq_res = results[0], results[1]

        # fetch_gex_metrics(allow_empty=True) 只會回傳三種形態：即時數據、
        # 帶 `_is_stale_cache` 標記的 last-known-good 快取，或無任何可用數據時
        # 的空 dict（不會回傳 510/515 靜態常數，該常數僅在 allow_empty=False
        # 時出現）。因此以「是否有有效 spy_spot」判斷是否需要 SPY 即時備援。
        gex_data: dict[str, Any] = {}
        if isinstance(gex_res, dict):
            gex_data = gex_res
        elif isinstance(gex_res, BaseException):
            logger.warning(f"force_macro_update 大盤 GEX 抓取失敗: {gex_res}")

        if _positive_float(gex_data.get("spy_spot")) <= 0:
            gex_data = await _fetch_spy_live_gex_fallback() or {}

        ted_spread: float | None = None
        if isinstance(liq_res, dict) and not liq_res.get("_is_fallback"):
            ted_raw = liq_res.get("ted_spread")
            if ted_raw is not None:
                try:
                    ted_spread = float(ted_raw)
                except (TypeError, ValueError):
                    ted_spread = None
        if ted_spread is None:
            errors.append("流動性指標 (TED Spread) 更新失敗：無法取得即時數據")

        # get_market_regime() 快取的組成輸入 (GEX/流動性) 剛被強制刷新，
        # 需一併清除其記憶體快取，避免管理員手動刷新後市況判讀仍停留在
        # 舊資料直到 TTL 到期才更新。
        invalidate_market_regime_cache()

        spy_spot = _positive_float(gex_data.get("spy_spot"))
        if spy_spot <= 0:
            errors.append("GEX 更新失敗：大盤端點與 SPY 即時估算皆無有效數據")
            return None

        # TED Spread 失敗已列於 errors，此處僅在取得即時值時附上，避免重複顯示。
        ted_part = f" / TED Spread: {ted_spread:.2f}" if ted_spread is not None else ""
        stale_tag = " ⚠️ [使用快取資料]" if gex_data.get("_is_stale_cache") else ""
        gamma_flip = _positive_float(gex_data.get("gamma_flip"))
        return (
            f"SPY: ${spy_spot:.2f} / Gamma Flip: {gamma_flip:.2f}{ted_part}{stale_tag}"
        )
    except Exception as e:
        logger.warning(f"force_macro_update GEX 與流動性更新失敗: {e}")
        errors.append(f"GEX 與流動性更新失敗：{e}")
        return None


async def setup(bot: Any) -> None:
    await bot.add_cog(AdminCommandsCog(bot))
