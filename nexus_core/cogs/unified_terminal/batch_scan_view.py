import asyncio
from typing import Any, List, Optional
import discord
import logging
from services import market_data_service
from cogs.embed_builder import create_error_embed, chunk_embeds

logger = logging.getLogger(__name__)

# 雷達快照（radar_terminal_{SYM}）超過此秒數即視為過期，可由「更新逾 1 小時標的」重算
RADAR_REFRESH_STALE_SECONDS = 3600
# 更新按鈕：同時重算的標的數與整體時間預算（秒）
_REFRESH_CONCURRENCY = 3
_REFRESH_BUDGET_SECONDS = 540
# Footer 最多列出的抓取失敗標的數（Embed footer 上限 2048 字元）
_FOOTER_MAX_FAILED_SYMBOLS = 10


def _is_stale_result(r: dict) -> bool:
    """雷達資料年齡為 None（從未計算）或超過一小時即視為過期。"""
    age = (r.get("_freshness") or {}).get("radar_age_seconds")
    return age is None or age > RADAR_REFRESH_STALE_SECONDS


class BatchScanWarningButton(discord.ui.Button):
    """
    按鈕：點擊後解析即時聯動警示列出的所有標的並批次執行深入分析。
    """

    def __init__(self, cog: Any, bot: Any):
        super().__init__(
            label="⚡ 批次分析警示標的",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.cog = cog
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> Any:
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "無法讀取當前訊息或 Embed 資料。", title="讀取錯誤"
                ),
                ephemeral=True,
            )
            return

        view = self.view
        if not view:
            return

        # 1. 禁用按鈕與下拉選單以防止重複點擊
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)

        try:
            embed = interaction.message.embeds[0]
            warning_symbols = []

            for field in embed.fields:
                if field.name and "即時聯動警示" in field.name:
                    if field.value:
                        import re

                        # 尋找類似 "• 🚀 TSLA:" 的標的代號
                        symbols = re.findall(r"•.*?([A-Za-z0-9.-]+):", field.value)
                        warning_symbols.extend([s.upper() for s in symbols])
                    break

            if not warning_symbols:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "當前訊息的「即時聯動警示」中沒有列出任何標的，或所有標的皆無異常偏離。",
                        title="無警示標的",
                    ),
                    ephemeral=True,
                )
                return

            # 去重並保持順序
            unique_warnings = []
            for s in warning_symbols:
                if s not in unique_warnings:
                    unique_warnings.append(s)

            user_id = interaction.user.id
            await interaction.followup.send(
                f"🔄 正在批次分析以下 {len(unique_warnings)} 個警示標的: {', '.join(unique_warnings)}...",
                ephemeral=True,
            )

            accumulated_embeds: List[discord.Embed] = []
            analysis_sem = asyncio.Semaphore(3)
            embed_lock = asyncio.Lock()

            async def _analyze_symbol(sym: str) -> None:
                async with analysis_sem:
                    sym_embeds: List[discord.Embed] = []
                    try:
                        await self.cog._run_single_symbol_hub(
                            interaction,
                            sym,
                            user_id,
                            embeds_accumulator=sym_embeds,
                        )
                        async with embed_lock:
                            accumulated_embeds.extend(sym_embeds)
                    except Exception as e:
                        logger.error(f"Batch analysis failed for {sym}: {e}")

            await asyncio.gather(*(_analyze_symbol(s) for s in unique_warnings))

            # Chunk embeds safely by cumulative character length (under 5,500 characters) and size limits (max 10 embeds)
            chunks = chunk_embeds(accumulated_embeds, max_size=5500, max_count=10)
            for chunk in chunks:
                try:
                    await interaction.followup.send(embeds=chunk, ephemeral=True)
                except Exception as send_err:
                    logger.error(
                        f"Failed to send chunk of batch analysis embeds: {send_err}"
                    )
        except Exception as outer_err:
            logger.error(f"Outer Batch Scan Warning Button callback error: {outer_err}")
        finally:
            # 2. 恢復按鈕與下拉選單狀態：先全數啟用，再交由 View 依頁碼／過期標的數
            #    重新套用邊界按鈕（◀ 首頁、▶ 末頁、更新按鈕）的 disabled 規則
            for child in view.children:
                child.disabled = False
            update_states = getattr(view, "_update_button_states", None)
            if callable(update_states):
                update_states()
            try:
                await interaction.edit_original_response(view=view)
            except Exception as final_err:
                logger.error(
                    f"Failed to edit original response in finally block: {final_err}"
                )


class BatchScanRefreshButton(discord.ui.Button):
    """
    按鈕：僅重算（慢速路徑）雷達資料超過 1 小時的標的，再就地重跑同一次掃描。

    只會重算標的自身的快照（UOA、Max Pain、IV、PSQ 等）；GEX 仍跟隨 edge 快照
    （約 30 分鐘更新），不會因此重算。
    """

    def __init__(self, stale_count: int):
        if stale_count > 0:
            label = f"🔄 更新逾 1 小時標的 ({stale_count})"
        else:
            label = "✅ 資料皆在 1 小時內"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=stale_count == 0,
        )

    async def _refresh_stale_symbols(self, cog: Any, symbols: List[str]) -> None:
        """以 Semaphore(3) 與整體時間預算重算過期標的；失敗與逾時只記錄警告。"""
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)

        async def _refresh_one(sym: str) -> None:
            async with sem:
                await cog._fetch_sym_radar_data_slow(sym, force_refresh=True)

        tasks = {asyncio.create_task(_refresh_one(s)): s for s in symbols}
        done, pending = await asyncio.wait(tasks, timeout=_REFRESH_BUDGET_SECONDS)
        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.warning(f"Radar refresh failed for {tasks[task]}: {exc}")
        # 逾時未完成者不取消（SingleFlight 會保護底層計算），僅記錄並吞掉後續例外
        for task in pending:
            logger.warning(f"Radar refresh timed out for {tasks[task]}")
            task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def callback(self, interaction: discord.Interaction) -> Any:
        view = self.view
        if not isinstance(view, BatchScanPaginatedView):
            return

        from services.llm_service import is_memory_safe

        if not is_memory_safe():
            await interaction.response.send_message(
                embed=create_error_embed(
                    "系統記憶體吃緊，請稍後再試", title="系統忙碌"
                ),
                ephemeral=True,
            )
            return

        stale = view.stale_symbols()
        # 整個更新流程（慢速重算與重新掃描）皆視為使用者互動來源
        with market_data_service.mark_interactive_request():
            for child in view.children:
                setattr(child, "disabled", True)
            self.label = f"⏳ 更新中 ({len(stale)} 檔)…"
            await interaction.response.edit_message(view=view)

            try:
                await self._refresh_stale_symbols(view.cog, stale)

                outcome = await view.cog._run_unified_scan(
                    view.state or {}, view.user_id
                )
                if outcome.error_embed is not None:
                    await interaction.edit_original_response(
                        embed=outcome.error_embed, view=None
                    )
                    return

                new_view = BatchScanPaginatedView(
                    outcome.embeds,
                    view.cog,
                    view.bot,
                    total_items=len(outcome.results),
                    scan_results=outcome.results,
                    state=view.state,
                    user_id=view.user_id,
                    failed_symbols=outcome.failed_symbols,
                )
                await interaction.edit_original_response(
                    embed=outcome.embeds[0], view=new_view
                )
            except Exception as e:
                logger.exception(f"Batch scan refresh error: {e}")
                # 舊 View 的 300 秒 timeout 可能已在長時間更新中到期，故重建全新的 View
                fresh_view = BatchScanPaginatedView(
                    view.embeds,
                    view.cog,
                    view.bot,
                    total_items=view.total_items,
                    scan_results=view.scan_results,
                    state=view.state,
                    user_id=view.user_id,
                    failed_symbols=view.failed_symbols,
                )
                fresh_view.current_page = view.current_page
                fresh_view._update_button_states()
                try:
                    await interaction.edit_original_response(
                        embed=view.embeds[view.current_page], view=fresh_view
                    )
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "更新標的資料時發生錯誤，請稍後再試。"
                        ),
                        ephemeral=True,
                    )
                except Exception as final_err:
                    logger.error(f"Failed to restore batch scan view: {final_err}")


class BatchScanPaginatedView(discord.ui.View):
    """
    批次掃描結果的單一訊息換頁 View。

    按鈕樣式與頁碼 Footer 格式沿用 `/list_watch` 的 `WatchlistPagination`
    (`ui/watchlist.py`)，維持跨模組換頁介面的視覺一致性；換頁機制則是
    `interaction.response.edit_message()` 就地換頁，並整合「批次分析警示
    標的」與「返回控制面板」按鈕。多頁掃描結果只需送出一則 followup 訊息即可完整呈現，
    避免逐頁分別呼叫 `interaction.followup.send()` 撞上 Discord 互動的隱性
    followup 訊息數量上限（錯誤碼 40094）。
    """

    def __init__(
        self,
        embeds: List[discord.Embed],
        cog: Any,
        bot: Any,
        *,
        timeout: float = 300.0,
        total_items: Optional[int] = None,
        scan_results: Optional[List[dict]] = None,
        state: Optional[dict] = None,
        user_id: Optional[int] = None,
        failed_symbols: Optional[List[str]] = None,
    ):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.cog = cog
        self.bot = bot
        self.current_page = 0
        self.total_items = total_items if total_items is not None else len(embeds)
        self.scan_results = scan_results
        self.state = state
        self.user_id = user_id
        self.failed_symbols: List[str] = list(failed_symbols or [])
        self.refresh_button: Optional["BatchScanRefreshButton"] = None

        # 警示標的分析按鈕獨立一列，避免與換頁按鈕擠在同一排
        warning_button = BatchScanWarningButton(cog, bot)
        warning_button.row = 1
        self.add_item(warning_button)

        # 「更新逾 1 小時標的」按鈕：需帶入掃描結果、state 與 user_id 才能重跑同一次掃描
        if scan_results is not None and state is not None and user_id is not None:
            self.refresh_button = BatchScanRefreshButton(len(self.stale_symbols()))
            self.add_item(self.refresh_button)

        self._apply_footers()
        self._update_button_states()

    def stale_symbols(self) -> List[str]:
        """回傳雷達資料過期（>1 小時或從未計算）的標的代號（去重、保持順序）。"""
        symbols: List[str] = []
        for r in self.scan_results or []:
            sym = str(r.get("symbol") or "").upper()
            if sym and sym not in symbols and _is_stale_result(r):
                symbols.append(sym)
        return symbols

    def _apply_footers(self) -> None:
        """比照 /list_watch：頁碼與總項目數寫入 Footer，而非另立指示按鈕。"""
        total_pages = len(self.embeds)
        failed_note = ""
        if self.failed_symbols:
            shown = self.failed_symbols[:_FOOTER_MAX_FAILED_SYMBOLS]
            failed_note = f" ｜ ⚠️ 抓取失敗: {', '.join(shown)}"
            if len(self.failed_symbols) > len(shown):
                failed_note += f" 等 {len(self.failed_symbols)} 檔"
        for idx, emb in enumerate(self.embeds):
            emb.set_footer(
                text=f"頁次: {idx + 1}/{total_pages} ｜ 📊 總項目: {self.total_items}"
                f"{failed_note}"
            )

    def _update_button_states(self) -> None:
        """根據當前頁碼動態切換換頁按鈕 disabled 狀態。"""
        self.btn_prev.disabled = self.current_page <= 0
        self.btn_next.disabled = self.current_page >= len(self.embeds) - 1
        if self.refresh_button is not None:
            self.refresh_button.disabled = not self.stale_symbols()

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary, row=0)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至上一頁。"""
        self.current_page = max(0, self.current_page - 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary, row=0)
    async def btn_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至下一頁。"""
        self.current_page = min(len(self.embeds) - 1, self.current_page + 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(
        label="🔄 返回控制面板", style=discord.ButtonStyle.secondary, row=0
    )
    async def btn_return_panel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """切換回 UnifiedRadarView 控制面板，允許使用者調整過濾條件或掃描範圍。"""
        from .radar_view import UnifiedRadarView
        from cogs.embed_builders.scan_embeds import (
            build_unified_radar_panel_embed,
        )

        # 帶入本次掃描的 state，讓控制面板還原範圍／過濾條件／參數／標籤
        view = UnifiedRadarView(self.cog, interaction.user.id, initial_state=self.state)
        embed = build_unified_radar_panel_embed(view.get_state_dict())
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self) -> None:
        """Timeout 後移除所有按鈕，避免殭屍互動元件殘留。"""
        self.clear_items()
