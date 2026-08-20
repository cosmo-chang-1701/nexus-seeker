"""BatchScanPaginatedView 翻頁互動 View 單元測試。"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.embed_builders._core import NexusEmbed
from cogs.unified_terminal.batch_scan_view import (
    BatchScanPaginatedView,
    BatchScanWarningButton,
)


def _make_embed(title: str) -> discord.Embed:
    """建立帶標題的簡易測試 Embed。

    使用 NexusEmbed（而非原生 discord.Embed）以精確重現生產環境行為：
    NexusEmbed.set_footer() 會自動附上 "🌌 Nexus Seeker • " 品牌前綴。
    """
    return NexusEmbed(title=title)


def _make_view(embeds: list[discord.Embed], **kwargs: object) -> BatchScanPaginatedView:
    return BatchScanPaginatedView(embeds, MagicMock(), MagicMock(), **kwargs)  # type: ignore[arg-type]


class TestBatchScanPaginatedViewInit:
    """初始化、Footer 與按鈕狀態驗證。"""

    def test_single_page_both_buttons_disabled(self) -> None:
        embeds = [_make_embed("Page 1")]
        view = _make_view(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is True
        assert embeds[0].footer.text == "🌌 Nexus Seeker • 頁次: 1/1 ｜ 📊 總項目: 1"

    def test_multi_page_footers_and_initial_state(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = _make_view(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is False
        assert embeds[0].footer.text == "🌌 Nexus Seeker • 頁次: 1/3 ｜ 📊 總項目: 3"
        assert embeds[1].footer.text == "🌌 Nexus Seeker • 頁次: 2/3 ｜ 📊 總項目: 3"
        assert embeds[2].footer.text == "🌌 Nexus Seeker • 頁次: 3/3 ｜ 📊 總項目: 3"

    def test_total_items_overrides_page_count(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = _make_view(embeds, total_items=55)

        assert view.total_items == 55
        footer_text = embeds[0].footer.text
        assert footer_text is not None
        assert "📊 總項目: 55" in footer_text

    def test_button_labels_and_style_match_list_watch(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = _make_view(embeds)

        assert view.btn_prev.label == "◀ 上一頁"
        assert view.btn_next.label == "下一頁 ▶"
        assert view.btn_prev.style == discord.ButtonStyle.primary
        assert view.btn_next.style == discord.ButtonStyle.primary

    def test_warning_button_attached_on_separate_row(self) -> None:
        """批次分析警示標的按鈕應存在，且與換頁按鈕分屬不同 row 以免擠壓。"""
        embeds = [_make_embed("Page 1")]
        view = _make_view(embeds)

        warning_buttons = [
            c for c in view.children if isinstance(c, BatchScanWarningButton)
        ]
        assert len(warning_buttons) == 1
        assert warning_buttons[0].row == 1
        assert view.btn_prev.row == 0
        assert view.btn_next.row == 0


class TestBatchScanPaginatedViewNavigation:
    """翻頁按鈕的狀態轉換驗證。"""

    @pytest.mark.asyncio
    async def test_next_page(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = _make_view(embeds)

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
    async def test_prev_does_not_go_negative(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = _make_view(embeds)

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_prev.callback(mock_interaction)

        assert view.current_page == 0

    @pytest.mark.asyncio
    async def test_next_does_not_exceed_max(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = _make_view(embeds)
        view.current_page = 1
        view._update_button_states()

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.response = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()

        await view.btn_next.callback(mock_interaction)

        assert view.current_page == 1


class TestBatchScanPaginatedViewTimeout:
    """Timeout 清理驗證。"""

    @pytest.mark.asyncio
    async def test_on_timeout_clears_items(self) -> None:
        embeds = [_make_embed(f"Page {i}") for i in range(1, 3)]
        view = _make_view(embeds)

        assert len(view.children) > 0
        await view.on_timeout()
        assert len(view.children) == 0
