import discord
from typing import Optional, Set
import asyncio

from cogs.embed_builders.scan_embeds import build_unified_radar_panel_embed


class FilterParamsModal(discord.ui.Modal, title="微調進階量化參數"):
    max_pain_threshold: discord.ui.TextInput = discord.ui.TextInput(
        label="Max Pain 閾值 (%)", default="10", placeholder="例如: 10"
    )
    abs_support_tolerance: discord.ui.TextInput = discord.ui.TextInput(
        label="絕對支撐容錯率 (%)", default="1", placeholder="例如: 1"
    )
    silent_period_days: discord.ui.TextInput = discord.ui.TextInput(
        label="靜默期規避 (天)", default="5", placeholder="例如: 5"
    )

    def __init__(self, view: "UnifiedRadarView"):
        super().__init__()
        self.radar_view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.radar_view.params["max_pain_threshold"] = float(
                self.max_pain_threshold.value
            )
            self.radar_view.params["abs_support_tolerance"] = float(
                self.abs_support_tolerance.value
            )
            self.radar_view.params["silent_period_days"] = int(
                self.silent_period_days.value
            )
        except ValueError:
            return await interaction.response.send_message(
                "❌ 參數格式錯誤，請輸入有效的數字。", ephemeral=True
            )

        await self.radar_view.update_state_message(interaction)


class UnifiedRadarView(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

        # State tracking
        self.scope = "WATCHLIST"
        self.quant_filters: Set[str] = set()
        self.params = {
            "max_pain_threshold": 10.0,
            "abs_support_tolerance": 1.0,
            "silent_period_days": 5,
        }
        self.selected_tag: Optional[str] = None
        self.tag_selector_added = False
        self.tag_select: Optional[discord.ui.Select] = None

        self._add_static_components()

    def get_state_dict(self) -> dict:
        return {
            "scope": self.scope,
            "quant_filters": list(self.quant_filters),
            "params": self.params,
            "selected_tag": self.selected_tag,
        }

    def _add_static_components(self):
        # 1. Scope Selector
        scope_options = [
            discord.SelectOption(
                label="🌟 掃描自選標的 (Watchlist)", value="WATCHLIST", default=True
            ),
            discord.SelectOption(label="🌀 掃描全部 (持倉+掛單+期權標的)", value="ALL"),
            discord.SelectOption(label="💼 掃描持倉標的 (Holdings)", value="HOLDINGS"),
            discord.SelectOption(
                label="⏳ 掃描掛單標的 (Pending Orders)", value="ORDERS"
            ),
            discord.SelectOption(
                label="📜 掃描期權持倉標的 (Option Holdings)", value="OPTIONS"
            ),
        ]
        self.scope_select = discord.ui.Select(
            placeholder="請選擇掃描範圍...",
            min_values=1,
            max_values=1,
            options=scope_options,
            row=0,
        )
        self.scope_select.callback = self.on_scope_change
        self.add_item(self.scope_select)

        # 2. Quant Filters Selector
        filter_options = [
            discord.SelectOption(
                label="排除底牆破位 / 戒嚴", value="exclude_martial_law"
            ),
            discord.SelectOption(label="要求 TDP 三擊共振", value="require_tdp_signal"),
            discord.SelectOption(
                label="暗池派發防護 (Skew < -0.3)", value="dp_skew_defense"
            ),
            discord.SelectOption(
                label="嚴格流動性閘門 (點差比率 < 15%)", value="strict_liquidity"
            ),
            discord.SelectOption(
                label="避開財報/總經靜默期", value="avoid_silent_period"
            ),
        ]
        self.filter_select = discord.ui.Select(
            placeholder="請選擇進階量化過濾條件 (可多選)...",
            min_values=0,
            max_values=5,
            options=filter_options,
            row=1,
        )
        self.filter_select.callback = self.on_filter_change
        self.add_item(self.filter_select)

        # 3. Action Buttons
        self.adjust_params_btn = discord.ui.Button(
            label="微調參數", style=discord.ButtonStyle.secondary, row=3
        )
        self.adjust_params_btn.callback = self.on_adjust_params
        self.add_item(self.adjust_params_btn)

        self.execute_scan_btn = discord.ui.Button(
            label="🚀 執行量化雷達", style=discord.ButtonStyle.primary, row=3
        )
        self.execute_scan_btn.callback = self.on_execute_scan
        self.add_item(self.execute_scan_btn)

    async def on_scope_change(self, interaction: discord.Interaction):
        self.scope = self.scope_select.values[0]
        for opt in self.scope_select.options:
            opt.default = opt.value == self.scope

        if self.scope == "WATCHLIST":
            await interaction.response.defer(ephemeral=True)
            from database.watchlist_tags import get_user_unique_tags

            user_id_str = str(self.user_id)
            try:
                tags = await asyncio.to_thread(get_user_unique_tags, user_id_str)
            except Exception:
                tags = []

            if tags:
                if not self.tag_selector_added:
                    tag_options = [
                        discord.SelectOption(label=t, value=t) for t in tags[:25]
                    ]
                    tag_options.insert(
                        0,
                        discord.SelectOption(
                            label="所有標籤 (不過濾)", value="ALL_TAGS"
                        ),
                    )

                    self.tag_select = discord.ui.Select(
                        placeholder="請選擇 Watchlist 標籤...",
                        min_values=1,
                        max_values=1,
                        options=tag_options,
                        row=2,
                    )
                    self.tag_select.callback = self.on_tag_change  # type: ignore[method-assign]
                    self.add_item(self.tag_select)
                    self.tag_selector_added = True
            else:
                self.selected_tag = None
                if self.tag_selector_added and self.tag_select is not None:
                    self.remove_item(self.tag_select)
                    self.tag_selector_added = False

            await self.update_state_message_deferred(interaction)
        else:
            self.selected_tag = None
            if self.tag_selector_added and self.tag_select is not None:
                self.remove_item(self.tag_select)
                self.tag_selector_added = False
            await self.update_state_message(interaction)

    async def on_filter_change(self, interaction: discord.Interaction):
        self.quant_filters = set(self.filter_select.values)
        for opt in self.filter_select.options:
            opt.default = opt.value in self.quant_filters
        await self.update_state_message(interaction)

    async def on_tag_change(self, interaction: discord.Interaction):
        if not self.tag_select:
            return
        val = self.tag_select.values[0]
        if val == "ALL_TAGS":
            self.selected_tag = None
        else:
            self.selected_tag = val

        for opt in self.tag_select.options:
            opt.default = opt.value == val

        await self.update_state_message(interaction)

    async def on_adjust_params(self, interaction: discord.Interaction):
        modal = FilterParamsModal(self)
        modal.max_pain_threshold.default = str(self.params["max_pain_threshold"])
        modal.abs_support_tolerance.default = str(self.params["abs_support_tolerance"])
        modal.silent_period_days.default = str(self.params["silent_period_days"])
        await interaction.response.send_modal(modal)

    async def update_state_message(self, interaction: discord.Interaction):
        embed = build_unified_radar_panel_embed(self.get_state_dict())
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            pass

    async def update_state_message_deferred(self, interaction: discord.Interaction):
        embed = build_unified_radar_panel_embed(self.get_state_dict())
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            pass

    async def on_execute_scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        for item in self.children:
            if hasattr(item, "disabled"):
                setattr(item, "disabled", True)
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass

        await self.cog.execute_unified_scan(
            interaction, self.get_state_dict(), self.user_id
        )
