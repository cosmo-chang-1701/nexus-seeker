"""批次量化雷達掃描（/x scan_type: 或 Unified Radar Panel 執行按鈕）。"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import discord

from services import market_data_service

from cogs.embed_builder import create_error_embed, build_radar_scan_embed
from .batch_scan_view import BatchScanPaginatedView

logger = logging.getLogger(__name__)

# 限制 /x 批次雷達掃描的併發標的數，適度提高至 15 以加速大清單處理
_RADAR_SCAN_SEM = asyncio.Semaphore(15)


class BatchScanMixin:
    if TYPE_CHECKING:
        bot: Any

        async def _fetch_sym_radar_data_fast(self, sym: str) -> Any: ...

    @market_data_service.interactive
    async def execute_unified_scan(
        self, interaction: discord.Interaction, state: dict, user_id: int
    ) -> Any:
        scan_value = state.get("scope", "WATCHLIST")
        tag = state.get("selected_tag")
        quant_filters = set(state.get("quant_filters", []))
        params = state.get("params", {})

        target_symbols = set()

        try:
            if scan_value in ("HOLDINGS", "ALL"):
                from services.asset_manager import AssetManager
                from models.asset import ContextType

                manager = AssetManager()
                holding_assets = manager.get_assets(user_id, ContextType.HOLDING)
                for a in holding_assets:
                    target_symbols.add(a.symbol.upper())

            if scan_value in ("ORDERS", "ALL"):
                from database.orders import get_user_active_orders

                active_orders = await asyncio.to_thread(get_user_active_orders, user_id)
                for o in active_orders:
                    target_symbols.add(o["symbol"].upper())

            if scan_value in ("OPTIONS", "ALL"):
                from database.portfolio import get_user_portfolio

                portfolio_rows = await asyncio.to_thread(get_user_portfolio, user_id)
                for row in portfolio_rows:
                    target_symbols.add(row[1].upper())

            if scan_value == "WATCHLIST":
                import database
                from database.watchlist_tags import get_watchlist_tags

                watchlist_items = await asyncio.to_thread(
                    database.get_user_watchlist, user_id
                )
                for item in watchlist_items:
                    sym = item[0].upper()
                    if tag:
                        tags = await asyncio.to_thread(
                            get_watchlist_tags, str(user_id), sym
                        )
                        if tag.upper() not in tags:
                            continue
                    target_symbols.add(sym)

            unique_symbols = sorted(list(target_symbols))

            if not unique_symbols:
                scan_names = {
                    "HOLDINGS": "現貨持倉",
                    "ORDERS": "待成交掛單",
                    "OPTIONS": "期權持倉",
                    "WATCHLIST": "自選標的",
                    "ALL": "持倉、掛單或期權",
                }
                return await interaction.followup.send(
                    embed=create_error_embed(
                        f"您目前沒有任何{scan_names.get(scan_value, '相關')}標的，無法進行批次掃描。",
                        title="無標的資料",
                    ),
                    ephemeral=True,
                )

            # 並行獲取所有標的的雷達數據 (Cache-Aside)
            # 使用 _RADAR_SCAN_SEM 限制併發數，避免大清單 (ALL) 造成請求洪峰。
            async def _throttled_fetch(sym: str) -> Any:
                async with _RADAR_SCAN_SEM:
                    return await self._fetch_sym_radar_data_fast(sym)

            scan_results = await asyncio.gather(
                *(_throttled_fetch(s) for s in unique_symbols),
                return_exceptions=True,
            )
            # 過濾 Exception 並確保是 dict 類型以滿足 mypy
            valid_results = [r for r in scan_results if isinstance(r, dict)]

            # 根據 Unified Radar Panel 的量化過濾條件進行篩選
            filtered_results = []
            max_pain_threshold = params.get("max_pain_threshold", 10.0) / 100.0

            from models.schemas import ScanParams
            from market_analysis.intraday_pipeline import evaluate_advanced_filters
            import types

            scan_params_kwargs: dict[str, Any] = {}
            if "tdp_mode" in quant_filters or "require_tdp_signal" in quant_filters:
                scan_params_kwargs["require_tdp_signal"] = True
            if "squeeze_mode" in quant_filters:
                scan_params_kwargs["require_squeeze_firing"] = True
            if "uoa_mode" in quant_filters:
                scan_params_kwargs["min_net_uoa_delta"] = 1.0

            advanced_active = bool(scan_params_kwargs)
            adv_params = ScanParams(**scan_params_kwargs)

            for r in valid_results:
                passed = True

                # 1. exclude_martial_law (排除底牆破位 / 負 Gamma / 痛點極端偏離)
                if "exclude_martial_law" in quant_filters:
                    gex_data = r.get("gex_profile_data", {}) or r.get("gex_metrics", {})
                    pw_val = gex_data.get("put_wall") if gex_data else None
                    put_wall_val = float(pw_val) if pw_val is not None else 0.0
                    net_gex_val = (
                        float(gex_data.get("net_gex", 0.0) or 0.0) if gex_data else 0.0
                    )

                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    mp_data = r.get("max_pain")
                    dist = (
                        mp_data.get("distance_pct", 0.0)
                        if isinstance(mp_data, dict)
                        else 0.0
                    )
                    if (
                        (
                            put_wall_val > 0
                            and current_price > 0
                            and current_price < put_wall_val
                        )
                        or net_gex_val < 0
                        or abs(dist) > max_pain_threshold
                    ):
                        passed = False

                # 2. avoid_silent_period (規避財報/總經靜默期)
                if "avoid_silent_period" in quant_filters:
                    iv_data = r.get("iv_data")
                    if iv_data:
                        earnings_loading = getattr(
                            iv_data, "has_earnings_event", False
                        ) or (
                            isinstance(iv_data, dict)
                            and iv_data.get("has_earnings_event", False)
                        )
                        macro_loading = getattr(iv_data, "has_macro_event", False) or (
                            isinstance(iv_data, dict)
                            and iv_data.get("has_macro_event", False)
                        )
                        if earnings_loading or macro_loading:
                            passed = False

                # 3. magnetic_filters (高階磁吸過濾)
                if "magnetic_filters" in quant_filters:
                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    mp_data = r.get("max_pain")
                    mp_val = (
                        mp_data.get("max_pain") if isinstance(mp_data, dict) else 0.0
                    )
                    max_pain_val = float(mp_val) if mp_val is not None else 0.0

                    gex_data = r.get("gex_profile_data", {})
                    pw_val = gex_data.get("put_wall") if gex_data else 0.0
                    putwall = float(pw_val) if pw_val is not None else 0.0

                    dp_val = r.get("dp_poc")
                    dp_poc = float(dp_val) if dp_val is not None else 0.0

                    min_dev = params.get("min_max_pain_dev", 0.10)
                    tolerance = params.get("abs_support_tolerance", 1.0) / 100.0

                    if current_price > 0 and max_pain_val > 0:
                        if abs(current_price - max_pain_val) / max_pain_val <= min_dev:
                            passed = False
                    else:
                        passed = False

                    if current_price > 0 and putwall > 0 and current_price < putwall:
                        passed = False

                    if dp_poc > 0 and putwall > 0:
                        if abs(dp_poc - putwall) / putwall >= tolerance:
                            passed = False
                    else:
                        passed = False

                # 4. Advanced Filters (ScanParams)
                if passed and advanced_active:
                    quote = r.get("quote", {})
                    c_val = quote.get("c") if quote else 0.0
                    current_price = float(c_val) if c_val is not None else 0.0

                    psq_res = r.get("psq_result", {})
                    gex_data = r.get("gex_profile_data", {})

                    pw_val = gex_data.get("put_wall") if gex_data else None
                    put_wall = float(pw_val) if pw_val is not None else None

                    mp_data = r.get("max_pain")
                    mp_val = (
                        mp_data.get("max_pain") if isinstance(mp_data, dict) else 0.0
                    )
                    max_pain_val = float(mp_val) if mp_val is not None else 0.0

                    pseudo_metrics = types.SimpleNamespace(
                        squeeze_status=psq_res.get("is_squeezing", False),
                        squeeze_momentum=psq_res.get(
                            "momentum_value", psq_res.get("momentum", 0.0)
                        ),
                        current_price=current_price,
                        volume_poc=None,  # volume profile may not be fully available in batch scan
                        gex_max_put_wall=put_wall,
                        ma20=r.get("ma20"),
                        max_pain=max_pain_val,
                        dp_poc=r.get("dp_poc", 0.0),
                    )

                    is_adv_passed, adv_tags = evaluate_advanced_filters(
                        metrics=pseudo_metrics,
                        symbol_gex=gex_data,
                        uoa_data=r.get("uoa", []),
                        params=adv_params,
                    )
                    if not is_adv_passed:
                        passed = False
                    else:
                        r["advanced_tags"] = adv_tags

                if passed:
                    filtered_results.append(r)

            if not filtered_results:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "掃描完成，但無符合條件的標的。", title="無結果"
                    ),
                    ephemeral=True,
                )

            embeds = build_radar_scan_embed(filtered_results, scan_value, user_id)
            if not isinstance(embeds, list):
                embeds = [embeds]

            # 多頁結果一律封裝進單一則訊息的換頁 View（BatchScanPaginatedView），
            # 只送出一次 interaction.followup.send()，翻頁改由使用者點擊 ◀/▶
            # 就地編輯同一則訊息。無論結果有幾頁，都不會再逐頁呼叫 followup.send()
            # 而撞上 Discord 互動的隱性 followup 訊息數量上限（錯誤碼 40094）。
            pager_view = BatchScanPaginatedView(
                embeds, self, self.bot, total_items=len(filtered_results)
            )
            await interaction.followup.send(
                embed=embeds[0], view=pager_view, ephemeral=True
            )

        except Exception as e:
            logger.error(f"Batch Scan Error for {scan_value}: {e}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(f"執行批次掃描時發生錯誤: {e}"),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"Failed to send batch scan error followup: {follow_err}")
