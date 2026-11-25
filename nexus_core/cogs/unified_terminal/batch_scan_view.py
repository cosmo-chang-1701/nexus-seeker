import asyncio
import re
import time
from typing import Any, List, Optional, Union
import discord
import logging
from services import market_data_service
from services.market_data_service import _EDGE_SNAPSHOT_MAX_AGE_SECONDS
from cogs.embed_builder import create_error_embed
from cogs.embed_builders._ansi_utils import _clean_ansi
from cogs.embed_builders._embed_helpers import ANSI_CONTINUATION_FIELD_NAME
from cogs.embed_builders._radar_freshness import assess_radar_row

logger = logging.getLogger(__name__)

# 雷達快照（radar_terminal_{SYM}）達此秒數即視為過期，可由「更新逾 30 分鐘標的」重算；
# 沿用 edge 快照最大年齡。快照不分使用者且會被背景排程更新，故按鈕除快照年齡外，
# 也重算表格標示 🕓 的列（盤前良性 🌙 除外），同一標的在此秒數內不因 🕓 重複重算。
RADAR_REFRESH_STALE_SECONDS = _EDGE_SNAPSHOT_MAX_AGE_SECONDS
_RADAR_REFRESH_STALE_MINUTES = RADAR_REFRESH_STALE_SECONDS // 60
# 更新按鈕：同時重算的標的數與整體時間預算（秒）
_REFRESH_CONCURRENCY = 3
_REFRESH_BUDGET_SECONDS = 540
# Footer 每類最多列出的失敗標的數（Embed footer 上限 2048 字元）
_FOOTER_MAX_FAILED_SYMBOLS = 10


def _is_stale_result(r: dict, elapsed_seconds: float = 0.0) -> bool:
    """雷達快照年齡（加上掃描後經過時間）為 None（從未計算）或達 30 分鐘門檻。"""
    age = (r.get("_freshness") or {}).get("radar_age_seconds")
    return age is None or age + elapsed_seconds >= RADAR_REFRESH_STALE_SECONDS


def _has_refreshable_note(r: dict) -> bool:
    """表格標示 🕓 且非盤前良性過期的列。"""
    freshness = assess_radar_row(r)
    return freshness.is_stale and not freshness.is_benign_premarket


def _needs_live_gex(r: dict, elapsed_seconds: float) -> bool:
    """GEX 標示過期，或其資料年齡（加上掃描後經過時間）達門檻。"""
    if any(source.label == "GEX" for source in assess_radar_row(r).sources):
        return True
    age = (r.get("_freshness") or {}).get("gex_age_seconds")
    return age is not None and age + elapsed_seconds >= RADAR_REFRESH_STALE_SECONDS


def _footer_symbol_note(title: str, symbols: List[str]) -> str:
    if not symbols:
        return ""
    shown = symbols[:_FOOTER_MAX_FAILED_SYMBOLS]
    note = f" ｜ ⚠️ {title}: {', '.join(shown)}"
    if len(symbols) > len(shown):
        note += f" 等 {len(symbols)} 檔"
    return note


_SYMBOL_HEADER_RE = re.compile(r"[A-Za-z0-9.\-]{1,10}")
# 溢出行：「• …其餘 N 則警示略：TSM、NVDA等」
_OVERFLOW_NAMES_RE = re.compile(r"•\s*…其餘\s*\d+\s*則警示略：(.+)")


def _extract_warning_symbols(field_value: str) -> list[str]:
    """從「即時聯動警示」欄位解析標的：先讀新版分組標頭，無標頭才退回舊版 `• … SYM:`。"""
    cleaned = _clean_ansi(field_value)
    headers: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line or line.startswith("```") or line.startswith("•"):
            continue
        if _SYMBOL_HEADER_RE.fullmatch(line):
            headers.append(line)
    if headers:
        result = [h.upper() for h in headers]
        for line in cleaned.splitlines():
            m = _OVERFLOW_NAMES_RE.match(line.strip())
            if not m:
                continue
            for name in m.group(1).strip().removesuffix("等").split("、"):
                name = name.strip().upper()
                if _SYMBOL_HEADER_RE.fullmatch(name) and name not in result:
                    result.append(name)
        return result
    # 舊格式（訊息仍在畫面上）："• 🚀 TSLA: ..."
    return [s.upper() for s in re.findall(r"•.*?([A-Za-z0-9.-]+):", cleaned)]


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

        # 換畫面中或已停用：僅回應互動，避免分頁按鈕貼回其他畫面（須在任何 await 之前判斷）
        if isinstance(view, BatchScanPaginatedView) and view._is_transitioning():
            await interaction.response.defer()
            return

        # 1. 禁用按鈕與下拉選單以防止重複點擊
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)
        setattr(view, "last_interaction", interaction)

        try:
            embed = interaction.message.embeds[0]
            warning_symbols = []

            in_insights = False
            for field in embed.fields:
                if not in_insights:
                    if field.name and "即時聯動警示" in field.name:
                        in_insights = True
                        if field.value:
                            warning_symbols.extend(
                                _extract_warning_symbols(field.value)
                            )
                elif field.name == ANSI_CONTINUATION_FIELD_NAME:
                    if field.value:
                        warning_symbols.extend(_extract_warning_symbols(field.value))
                else:
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

            # 以單一 followup 訊息逐檔換頁，不逐檔／逐塊 followup（會撞 Discord 40094）
            await self.cog._run_warning_symbols_hub(
                interaction, unique_warnings, interaction.user.id
            )
        except Exception as outer_err:
            logger.error(f"Outer Batch Scan Warning Button callback error: {outer_err}")
        finally:
            # 2. 恢復按鈕與下拉選單狀態：先全數啟用，再交由 View 依頁碼／過期標的數
            #    重新套用邊界按鈕（◀ 首頁、▶ 末頁、更新按鈕）的 disabled 規則。
            #    View 已被其他畫面取代者不得再編輯訊息；已逾時者僅移除按鈕。
            #    返回面板／更新的換畫面編輯在途時，先等結果：成功即屬已取代，失敗才恢復。
            if isinstance(view, BatchScanPaginatedView):
                await view.transition_settled.wait()
            finished = view.is_finished()
            if not (finished and getattr(view, "superseded", False)):
                if not finished:
                    for child in view.children:
                        child.disabled = False
                    update_states = getattr(view, "_update_button_states", None)
                    if callable(update_states):
                        update_states()
                try:
                    await interaction.edit_original_response(
                        view=None if finished else view
                    )
                except Exception as final_err:
                    logger.error(
                        f"Failed to edit original response in finally block: {final_err}"
                    )


class BatchScanRefreshButton(discord.ui.Button):
    """
    按鈕：重算（慢速路徑）快照達 30 分鐘門檻、標示非即時或抓取失敗的標的，
    再就地重跑同一次掃描。

    GEX 過期的標的會先嘗試即時抓取並等待完成再重算；即時抓取失敗或沒有新資料
    （盤後常見）時沿用舊資料，列上仍會標示 GEX 年齡。
    """

    def __init__(self, stale_count: int):
        super().__init__(style=discord.ButtonStyle.secondary, row=1)
        self.set_stale_count(stale_count, False)

    def set_stale_count(self, stale_count: int, has_stale_rows: bool) -> None:
        """依可重算標的數與是否仍有非即時列同步按鈕文字與 disabled 狀態。"""
        if stale_count > 0:
            self.label = (
                f"🔄 更新逾 {_RADAR_REFRESH_STALE_MINUTES} 分鐘標的 ({stale_count})"
            )
        elif has_stale_rows:
            self.label = "☑️ 暫無可更新項目"
        else:
            self.label = f"✅ 資料皆在 {_RADAR_REFRESH_STALE_MINUTES} 分鐘內"
        self.disabled = stale_count == 0

    async def _refresh_stale_symbols(
        self,
        cog: Any,
        symbols: List[str],
        live_gex_symbols: Optional[set[str]] = None,
    ) -> List[str]:
        """以 Semaphore(3) 與整體時間預算重算標的；回傳失敗與逾時的標的。"""
        if not symbols:
            return []
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
        gex_targets: set[str] = live_gex_symbols or set()

        async def _refresh_one(sym: str) -> None:
            async with sem:
                if sym in gex_targets:
                    # 先等即時 GEX 寫入快取，慢速路徑才讀得到新資料
                    try:
                        from market_analysis.index_microstructure import (
                            fetch_symbol_gex_metrics,
                        )

                        await fetch_symbol_gex_metrics(sym, force_live=True)
                    except Exception as e:
                        logger.warning(f"Radar GEX live fetch failed for {sym}: {e}")
                await cog._fetch_sym_radar_data_slow(sym, force_refresh=True)

        tasks = {asyncio.create_task(_refresh_one(s)): s for s in symbols}
        done, pending = await asyncio.wait(tasks, timeout=_REFRESH_BUDGET_SECONDS)
        failed: List[str] = []
        for task, sym in tasks.items():
            if task in done:
                if task.cancelled():
                    # 已取消的 task 呼叫 exception() 會拋 CancelledError
                    logger.warning(f"雷達重算任務被取消: {sym}")
                    failed.append(sym)
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.warning(f"Radar refresh failed for {sym}: {exc}")
                    failed.append(sym)
            else:
                # 逾時未完成者不取消（SingleFlight 會保護底層計算），僅記錄並吞掉後續例外
                logger.warning(f"Radar refresh timed out for {sym}")
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
                failed.append(sym)
        return failed

    async def callback(self, interaction: discord.Interaction) -> Any:
        view = self.view
        if not isinstance(view, BatchScanPaginatedView):
            return

        # 防連點：須在任何 await 之前判斷；僅回應互動，不重複重算
        if view._is_transitioning():
            await interaction.response.defer()
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
        live_gex_symbols = view._live_gex_symbols(stale)
        refresh_failed = list(view.refresh_failed_symbols)
        recently = view._active_recent_refreshes()
        # 檢查到此處之間沒有 await，同一 View 的第二次點擊必定看得到旗標
        view.refresh_in_progress = True
        view.transition_settled.clear()
        # 整個更新流程（慢速重算與重新掃描）皆視為使用者互動來源
        with market_data_service.mark_interactive_request():
            previous_disabled = [
                (child, getattr(child, "disabled", False)) for child in view.children
            ]
            for child in view.children:
                setattr(child, "disabled", True)
            self.label = f"⏳ 更新中 ({len(stale)} 檔)…"
            try:
                await interaction.response.edit_message(view=view)
            except Exception:
                # 畫面未換成「更新中」：復原旗標與按鈕狀態，讓使用者可再次點擊
                view.refresh_in_progress = False
                for child, was_disabled in previous_disabled:
                    setattr(child, "disabled", was_disabled)
                view._update_button_states()
                raise
            else:
                view.last_interaction = interaction
                # 之後本訊息一律換上新 View；舊 View 停用，避免其逾時拆掉新按鈕
                view._retire()
            finally:
                view.transition_settled.set()

            try:
                refresh_failed = await self._refresh_stale_symbols(
                    view.cog, stale, live_gex_symbols
                )
                refreshed_at = time.monotonic()
                recently.update(
                    {s: refreshed_at for s in stale if s not in refresh_failed}
                )

                outcome = await view.cog._run_unified_scan(
                    view.state or {}, view.user_id
                )
                if outcome.error_embed is not None:
                    # 無掃描結果可換頁：保留「返回控制面板」，不提供更新按鈕
                    error_view = BatchScanPaginatedView(
                        [outcome.error_embed],
                        view.cog,
                        view.bot,
                        total_items=0,
                        state=view.state,
                        user_id=view.user_id,
                        failed_symbols=outcome.failed_symbols,
                        refresh_failed_symbols=refresh_failed,
                    )
                    error_view.last_interaction = interaction
                    await interaction.edit_original_response(
                        embed=outcome.error_embed, view=error_view
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
                    recently_refreshed=recently,
                    refresh_failed_symbols=refresh_failed,
                )
                new_view.current_page = min(view.current_page, len(outcome.embeds) - 1)
                new_view._update_button_states()
                new_view.last_interaction = interaction
                await interaction.edit_original_response(
                    embed=outcome.embeds[new_view.current_page], view=new_view
                )
            except Exception as e:
                logger.exception(f"Batch scan refresh error: {e}")
                # 舊 View 已停用，故重建全新的 View
                fresh_view = BatchScanPaginatedView(
                    view.embeds,
                    view.cog,
                    view.bot,
                    total_items=view.total_items,
                    scan_results=view.scan_results,
                    state=view.state,
                    user_id=view.user_id,
                    failed_symbols=view.failed_symbols,
                    recently_refreshed=recently,
                    refresh_failed_symbols=refresh_failed,
                )
                # 沿用舊掃描結果，資料年齡仍自原掃描時間起算
                fresh_view._created_at = view._created_at
                fresh_view.current_page = view.current_page
                fresh_view._update_button_states()
                fresh_view.last_interaction = interaction
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
        recently_refreshed: Optional[dict[str, float]] = None,
        refresh_failed_symbols: Optional[List[str]] = None,
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
        # 掃描結果的資料年齡以建立當下為準，之後以經過時間累加
        self._created_at = time.monotonic()
        # 本按鈕成功重算過的標的 → time.monotonic()
        self.recently_refreshed: dict[str, float] = dict(recently_refreshed or {})
        self.refresh_failed_symbols: List[str] = list(refresh_failed_symbols or [])
        # on_timeout 移除按鈕用：優先使用最後一次互動，否則編輯初始訊息
        self.message: Optional[Union[discord.WebhookMessage, discord.Message]] = None
        self.last_interaction: Optional[discord.Interaction] = None
        # True 代表同一則訊息已換上其他畫面，本 View 不得再編輯訊息
        self.superseded = False
        self.refresh_button: Optional["BatchScanRefreshButton"] = None
        # True 代表更新按鈕的重算流程已啟動，後續點擊只回應互動
        self.refresh_in_progress = False
        # True 代表返回面板的編輯在途，其他按鈕只回應互動
        self.returning_to_panel = False
        # 換畫面的編輯（返回面板、更新的第一次編輯）有結果前為 clear，供警示按鈕收尾等待
        self.transition_settled = asyncio.Event()
        self.transition_settled.set()

        # 警示標的分析按鈕獨立一列，避免與換頁按鈕擠在同一排
        warning_button = BatchScanWarningButton(cog, bot)
        warning_button.row = 1
        self.add_item(warning_button)

        # 「更新逾 30 分鐘標的」按鈕：需帶入掃描結果、state 與 user_id 才能重跑同一次掃描
        if scan_results is not None and state is not None and user_id is not None:
            self.refresh_button = BatchScanRefreshButton(len(self.stale_symbols()))
            self.add_item(self.refresh_button)

        self._apply_footers()
        self._update_button_states()

    def stale_symbols(self) -> List[str]:
        """回傳需重算的標的代號（去重、保持順序）。

        依序為：快速路徑抓取失敗者；快照年齡（含掃描後經過時間）達 30 分鐘或從未
        計算者；標示 🕓（盤前良性除外）且 30 分鐘內未被本按鈕重算過者。
        """
        now = time.monotonic()
        elapsed = now - self._created_at
        recent = self._active_recent_refreshes()
        symbols: List[str] = []
        for failed in self.failed_symbols:
            sym = failed.upper()
            if sym and sym not in symbols:
                symbols.append(sym)
        for r in self.scan_results or []:
            sym = str(r.get("symbol") or "").upper()
            if not sym or sym in symbols:
                continue
            if _is_stale_result(r, elapsed) or (
                sym not in recent and _has_refreshable_note(r)
            ):
                symbols.append(sym)
        return symbols

    def _active_recent_refreshes(self) -> dict[str, float]:
        """30 分鐘內由本按鈕成功重算過的標的（副本）。"""
        now = time.monotonic()
        return {
            sym: at
            for sym, at in self.recently_refreshed.items()
            if now - at < RADAR_REFRESH_STALE_SECONDS
        }

    def _live_gex_symbols(self, symbols: List[str]) -> set[str]:
        """重算清單中需先即時抓取 GEX 的標的（僅限有掃描結果列者）。"""
        elapsed = time.monotonic() - self._created_at
        targets: set[str] = set()
        for r in self.scan_results or []:
            sym = str(r.get("symbol") or "").upper()
            if sym in symbols and _needs_live_gex(r, elapsed):
                targets.add(sym)
        return targets

    def _is_transitioning(self) -> bool:
        """更新中、返回面板在途或本 View 已停用：按鈕點擊只回應互動。"""
        return self.refresh_in_progress or self.returning_to_panel or self.is_finished()

    def _retire(self) -> None:
        """同一則訊息即將換上其他畫面：停止本 View，使其逾時不再編輯訊息。"""
        self.superseded = True
        self.stop()

    def _apply_footers(self) -> None:
        """比照 /list_watch：頁碼與總項目數寫入 Footer，而非另立指示按鈕。"""
        total_pages = len(self.embeds)
        failed_note = _footer_symbol_note(
            "抓取失敗", self.failed_symbols
        ) + _footer_symbol_note("更新失敗", self.refresh_failed_symbols)
        for idx, emb in enumerate(self.embeds):
            emb.set_footer(
                text=f"頁次: {idx + 1}/{total_pages} ｜ 📊 總項目: {self.total_items}"
                f"{failed_note}"
            )

    def _update_button_states(self) -> None:
        """根據當前頁碼與資料時效動態切換按鈕狀態。"""
        self.btn_prev.disabled = self.current_page <= 0
        self.btn_next.disabled = self.current_page >= len(self.embeds) - 1
        if self.refresh_button is not None:
            self.refresh_button.set_stale_count(
                len(self.stale_symbols()),
                any(assess_radar_row(r).is_stale for r in self.scan_results or []),
            )

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary, row=0)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至上一頁。"""
        if self._is_transitioning():
            await interaction.response.defer()
            return
        self.current_page = max(0, self.current_page - 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )
        self.last_interaction = interaction

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary, row=0)
    async def btn_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        """翻至下一頁。"""
        if self._is_transitioning():
            await interaction.response.defer()
            return
        self.current_page = min(len(self.embeds) - 1, self.current_page + 1)
        self._update_button_states()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )
        self.last_interaction = interaction

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

        # 防連點與併發：須在任何 await 之前判斷
        if self._is_transitioning():
            await interaction.response.defer()
            return
        self.returning_to_panel = True
        self.transition_settled.clear()
        try:
            # 帶入本次掃描的 state，讓控制面板還原範圍／過濾條件／參數／標籤
            view = UnifiedRadarView(
                self.cog, interaction.user.id, initial_state=self.state
            )
            embed = build_unified_radar_panel_embed(view.get_state_dict())
            await interaction.response.edit_message(embed=embed, view=view)
            # 編輯成功才停用本 View；失敗時畫面仍是原按鈕，須保持可用且逾時仍會移除
            self._retire()
        finally:
            # 失敗時復原旗標讓按鈕可再用；成功時 View 已停用，仍不會再處理點擊
            self.returning_to_panel = False
            self.transition_settled.set()

    async def on_timeout(self) -> None:
        """Timeout 後移除所有按鈕，避免殭屍互動元件殘留。

        clear_items() 只改記憶體中的 View，須實際編輯訊息才能移除畫面上的按鈕。
        """
        self.clear_items()
        try:
            if self.last_interaction is not None:
                await self.last_interaction.edit_original_response(view=None)
            elif self.message is not None:
                await self.message.edit(view=None)
        except Exception as e:
            logger.debug(f"BatchScanPaginatedView 逾時移除按鈕失敗: {e}")
