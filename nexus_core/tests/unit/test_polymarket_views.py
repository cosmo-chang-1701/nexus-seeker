"""PolymarketPaginatedView 翻頁互動 View 單元測試。"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.embed_builders._core import NexusEmbed
from cogs.unified_terminal.polymarket_views import PolymarketPaginatedView


def _make_embed(title: str) -> discord.Embed:
    """建立帶標題的簡易測試 Embed。

    使用 NexusEmbed（而非原生 discord.Embed）以精確重現生產環境行為：
    NexusEmbed.set_footer() 會自動附上 "🌌 Nexus Seeker • " 品牌前綴。
    """
    return NexusEmbed(title=title)


# ---------------------------------------------------------------------------
# 初始化狀態測試
# ---------------------------------------------------------------------------


class TestPolymarketPaginatedViewInit:
    """初始化與按鈕狀態驗證。"""

    def test_single_page_both_buttons_disabled(self) -> None:
        """單頁時 ◀ 和 ▶ 都應 disabled，Footer 標示頁次 1/1。"""
        embeds = [_make_embed("Page 1")]
        view = PolymarketPaginatedView(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is True
        assert embeds[0].footer.text == "🌌 Nexus Seeker • 頁次: 1/1 ｜ 📊 總項目: 1"

    def test_multi_page_initial_state(self) -> None:
        """多頁時初始在第一頁：◀ disabled，▶ enabled，各頁 Footer 頁碼正確。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = PolymarketPaginatedView(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is False
        assert embeds[0].footer.text == "🌌 Nexus Seeker • 頁次: 1/3 ｜ 📊 總項目: 3"
        assert embeds[1].footer.text == "🌌 Nexus Seeker • 頁次: 2/3 ｜ 📊 總項目: 3"
        assert embeds[2].footer.text == "🌌 Nexus Seeker • 頁次: 3/3 ｜ 📊 總項目: 3"

    def test_total_items_overrides_page_count(self) -> None:
        """明確傳入 total_items 時，Footer 應顯示實際項目數而非頁數。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds, total_items=37)

        assert view.total_items == 37
        footer_text = embeds[0].footer.text
        assert footer_text is not None
        assert "📊 總項目: 37" in footer_text

    def test_button_labels_and_style_match_list_watch(self) -> None:
        """按鈕文案與樣式應比照 /list_watch 的 WatchlistPagination。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)

        assert view.btn_prev.label == "◀ 上一頁"
        assert view.btn_next.label == "下一頁 ▶"
        assert view.btn_prev.style == discord.ButtonStyle.primary
        assert view.btn_next.style == discord.ButtonStyle.primary

    def test_custom_timeout(self) -> None:
        """自定義 timeout 生效。"""
        embeds = [_make_embed("Page 1")]
        view = PolymarketPaginatedView(embeds, timeout=60.0)
        assert view.timeout == 60.0


# ---------------------------------------------------------------------------
# 翻頁行為測試
# ---------------------------------------------------------------------------


class TestPolymarketPaginatedViewNavigation:
    """翻頁按鈕的狀態轉換驗證。"""

    @pytest.mark.asyncio
    async def test_next_page(self) -> None:
        """點擊 ▶ 翻到第 2 頁，狀態正確更新。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = PolymarketPaginatedView(embeds)

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_next.callback(mock_interaction)

        assert view.current_page == 1
        assert view.btn_prev.disabled is False
        assert view.btn_next.disabled is False
        mock_interaction.response.edit_message.assert_called_once_with(
            embed=embeds[1], view=view
        )

    @pytest.mark.asyncio
    async def test_next_to_last_page(self) -> None:
        """連續翻到最後一頁，▶ disabled。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_next.callback(mock_interaction)

        assert view.current_page == 1
        assert view.btn_prev.disabled is False
        assert view.btn_next.disabled is True

    @pytest.mark.asyncio
    async def test_prev_page(self) -> None:
        """從第 2 頁翻回第 1 頁，◀ disabled。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = PolymarketPaginatedView(embeds)
        view.current_page = 1  # 模擬已在第 2 頁
        view._update_button_states()

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_prev.callback(mock_interaction)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is False
        mock_interaction.response.edit_message.assert_called_once_with(
            embed=embeds[0], view=view
        )

    @pytest.mark.asyncio
    async def test_prev_does_not_go_negative(self) -> None:
        """在第 1 頁點 ◀ 不會產生負索引。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_prev.callback(mock_interaction)

        assert view.current_page == 0

    @pytest.mark.asyncio
    async def test_next_does_not_exceed_max(self) -> None:
        """在最後一頁點 ▶ 不會超出索引。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)
        view.current_page = 1
        view._update_button_states()

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_next.callback(mock_interaction)

        assert view.current_page == 1


# ---------------------------------------------------------------------------
# Timeout 行為測試
# ---------------------------------------------------------------------------


class TestPolymarketPaginatedViewTimeout:
    """Timeout 清理驗證。"""

    @pytest.mark.asyncio
    async def test_on_timeout_clears_items(self) -> None:
        """Timeout 後所有按鈕應被移除。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)

        assert len(view.children) > 0
        await view.on_timeout()
        assert len(view.children) == 0

    @pytest.mark.asyncio
    async def test_on_timeout_edits_message_to_remove_buttons(self) -> None:
        """尚未翻頁即逾時：須透過回填的 message 實際移除 Discord 上的按鈕。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.on_timeout()

        view.message.edit.assert_awaited_once_with(view=None)

    @pytest.mark.asyncio
    async def test_on_timeout_prefers_last_page_interaction(self) -> None:
        """翻頁過後逾時：優先用最後一次翻頁互動編輯（token 較新），不再動 message。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await view.btn_next.callback(interaction)
        await view.on_timeout()

        interaction.edit_original_response.assert_awaited_once_with(view=None)
        view.message.edit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_timeout_swallows_edit_failure(self) -> None:
        """訊息已被關閉或 token 過期時，逾時處理不得拋出例外。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = PolymarketPaginatedView(embeds)
        view.message = MagicMock()
        view.message.edit = AsyncMock(side_effect=Exception("Unknown Message"))

        await view.on_timeout()

        assert len(view.children) == 0
