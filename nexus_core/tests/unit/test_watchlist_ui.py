import pytest
import sys
import os
import discord
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from ui.watchlist import WatchlistPagination


@pytest.mark.asyncio
@patch("services.asset_manager.AssetManager")
async def test_watchlist_pagination_ephemeral_edit(mock_asset_manager_class):
    """
    Test that WatchlistPagination handles ephemeral message limitations correctly.
    Specifically checks that HTTPExceptions are caught and original_interaction is prioritized.
    """
    # 1. Setup Mock AssetManager to prevent DB queries
    mock_manager = MagicMock()
    mock_manager.get_assets.return_value = []
    mock_asset_manager_class.return_value = mock_manager

    # 2. Setup original interaction (the webhook that created the ephemeral list)
    mock_original_interaction = AsyncMock(spec=discord.Interaction)

    # 3. Setup button click interaction
    mock_button_interaction = AsyncMock(spec=discord.Interaction)
    mock_button_interaction.response.send_message = AsyncMock()
    mock_original_message = AsyncMock(spec=discord.Message)
    # Simulate Discord raising an error if message.edit is called on an ephemeral message
    mock_original_message.edit.side_effect = discord.errors.NotFound(
        response=MagicMock(status=404), message="Unknown Message"
    )
    mock_button_interaction.message = mock_original_message

    # 4. Setup modal submission interaction
    mock_modal_interaction = AsyncMock(spec=discord.Interaction)
    mock_modal_interaction.response.edit_message = AsyncMock()
    mock_modal_interaction.user = MagicMock()
    mock_modal_interaction.user.id = 12345

    # --- Scenario 1: Using original_interaction (The Fix) ---
    pagination_view = WatchlistPagination(
        data=[], original_interaction=mock_original_interaction
    )

    # Simulate the user clicking "Edit Tags" button
    with patch("ui.watchlist_tags.WatchlistTagSelectView") as MockSelectView:
        edit_tags_btn = [
            x for x in pagination_view.children if x.custom_id == "edit_tags"
        ][0]
        await edit_tags_btn.callback(mock_button_interaction)

        # Verify the Select menu was sent
        mock_button_interaction.response.send_message.assert_called_once()

        # Extract the on_success callback that was dynamically created
        kwargs = MockSelectView.call_args.kwargs
        on_success = kwargs.get("on_success_callback")
        assert on_success is not None

        # Execute the on_success callback (simulating a successful tag update from modal)
        await on_success(mock_modal_interaction)

        # Verify that original_interaction.edit_original_response was used!
        mock_original_interaction.edit_original_response.assert_called_once()
        # Ensure message.edit was NOT called, avoiding the 404 error entirely
        mock_original_message.edit.assert_not_called()
        # Verify the smooth transition info message was shown
        mock_modal_interaction.response.edit_message.assert_called_once()

    # --- Scenario 2: original_interaction is missing, fallback triggers 404 but is caught ---
    pagination_view_fallback = WatchlistPagination(data=[])  # No original_interaction

    mock_button_interaction_fallback = AsyncMock(spec=discord.Interaction)
    mock_button_interaction_fallback.response.send_message = AsyncMock()
    mock_button_interaction_fallback.message = mock_original_message
    mock_modal_interaction_fallback = AsyncMock(spec=discord.Interaction)
    mock_modal_interaction_fallback.response.edit_message = AsyncMock()
    mock_modal_interaction_fallback.user = MagicMock()

    with patch("ui.watchlist_tags.WatchlistTagSelectView") as MockSelectViewFallback:
        edit_tags_btn_fallback = [
            x for x in pagination_view_fallback.children if x.custom_id == "edit_tags"
        ][0]
        await edit_tags_btn_fallback.callback(mock_button_interaction_fallback)

        kwargs = MockSelectViewFallback.call_args.kwargs
        on_success_fallback = kwargs.get("on_success_callback")

        # This will trigger mock_original_message.edit() which raises discord.NotFound
        # The test verifies that the exception is swallowed and execution continues
        await on_success_fallback(mock_modal_interaction_fallback)

        mock_original_message.edit.assert_called_once()
        # If the exception wasn't caught, the following line would never execute and the test would crash
        mock_modal_interaction_fallback.response.edit_message.assert_called_once()
