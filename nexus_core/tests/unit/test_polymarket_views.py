"""PolymarketPaginatedView 翻頁互動 View 單元測試。"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.unified_terminal.polymarket_views import PolymarketPaginatedView


def _make_embed(title: str) -> discord.Embed:
    """建立帶標題的簡易測試 Embed。"""
    return discord.Embed(title=title)


# ---------------------------------------------------------------------------
# 初始化狀態測試
# ---------------------------------------------------------------------------


class TestPolymarketPaginatedViewInit:
    """初始化與按鈕狀態驗證。"""

    def test_single_page_both_buttons_disabled(self) -> None:
        """單頁時 ◀ 和 ▶ 都應 disabled。"""
        embeds = [_make_embed("Page 1")]
        view = PolymarketPaginatedView(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is True
        assert view.btn_page_indicator.label == "1 / 1"

    def test_multi_page_initial_state(self) -> None:
        """多頁時初始在第一頁：◀ disabled，▶ enabled。"""
        embeds = [_make_embed(f"Page {i}") for i in range(1, 4)]
        view = PolymarketPaginatedView(embeds)

        assert view.current_page == 0
        assert view.btn_prev.disabled is True
        assert view.btn_next.disabled is False
        assert view.btn_page_indicator.label == "1 / 3"

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
        assert view.btn_page_indicator.label == "2 / 3"
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
        assert view.btn_page_indicator.label == "2 / 2"

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
        assert view.btn_page_indicator.label == "1 / 3"
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
