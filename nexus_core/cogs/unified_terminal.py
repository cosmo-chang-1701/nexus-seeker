import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Dict, Any, Optional, List
import psutil
from services.market_data_service import BoundedCache

from services import market_data_service, news_service, reddit_service
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.risk_engine import MacroContext
import market_math
import database
from cogs.embed_builder import (
    create_error_embed,
    create_financial_runway_embed,
    create_info_embed,
    create_iv_risk_scan_embed,
    create_market_calendar_embed,
    create_media_sentiment_embed,
    create_trades_embed,
    create_strategic_dash_embed,
    create_tactical_symbol_embed,
    create_tactical_hedge_embed,
    create_holdings_embed,
    build_vtr_stats_embed,
    create_polymarket_list_embed,
    build_radar_scan_embed,
    build_market_macro_overview_embed,
)

logger = logging.getLogger(__name__)


def find_matching_polymarket_odds(symbol: str, poly_markets: list) -> str:
    import re

    symbol = symbol.upper()
    ticker_map = {
        "MU": ["micron"],
        "NVDA": ["nvidia"],
        "AAPL": ["apple"],
        "TSLA": ["tesla"],
        "MSFT": ["microsoft"],
        "GOOG": ["google", "alphabet"],
        "GOOGL": ["google", "alphabet"],
        "AMZN": ["amazon"],
        "META": ["meta", "facebook"],
        "NFLX": ["netflix"],
    }
    alts = ticker_map.get(symbol, [])

    for m in poly_markets or []:
        if not isinstance(m, dict):
            continue
        question = m.get("question", "")
        question_lower = question.lower()

        matches_ticker = False
        if re.search(rf"\b{re.escape(symbol.lower())}\b", question_lower):
            matches_ticker = True
        else:
            for alt in alts:
                if alt in question_lower:
                    matches_ticker = True
                    break

        if not matches_ticker and symbol == "MU":
            if "micron" in question_lower and (
                "eps" in question_lower
                or "revenue" in question_lower
                or "earnings" in question_lower
            ):
                matches_ticker = True

        if matches_ticker:
            tokens = m.get("tokens", [])
            if tokens:
                yes_token = None
                for t in tokens:
                    if str(t.get("outcome", "")).strip().lower() == "yes":
                        yes_token = t
                        break
                target_token = yes_token if yes_token else tokens[0]
                outcome = target_token.get("outcome", "Yes")
                price_val = target_token.get("price", 0)
                try:
                    price_float = float(price_val)
                    odds_pct = price_float * 100.0
                    return f"{outcome}: {odds_pct:.1f}%"
                except Exception:
                    pass
                return f"{outcome}: {price_val}"

    return "N/A"


class BatchScanWarningButton(discord.ui.Button):
    """
    按鈕：點擊後解析即時聯動警示列出的所有標的並批次執行深入分析。
    """

    def __init__(self, cog, bot):
        super().__init__(
            label="⚡ 批次分析警示標的",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.cog = cog
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
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

                        # 尋找雙星號包裹的粗體標的代號，例如 **AAPL**
                        symbols = re.findall(r"\*\*([A-Za-z0-9.-]+)\*\*", field.value)
                        warning_symbols = [s.upper() for s in symbols]
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

            for symbol in unique_warnings:
                try:
                    await self.cog._run_single_symbol_hub(interaction, symbol, user_id)
                except Exception as e:
                    logger.error(f"Batch analysis failed for {symbol}: {e}")
                    await interaction.followup.send(
                        embed=create_error_embed(
                            f"分析 `{symbol}` 時發生錯誤: {e}", title="分析錯誤"
                        ),
                        ephemeral=True,
                    )
        finally:
            # 2. 恢復按鈕與下拉選單狀態
            for child in view.children:
                child.disabled = False
            await interaction.edit_original_response(view=view)


class BatchScanView(discord.ui.View):
    """
    批次掃描總覽面板的互動 View。
    已移除「選擇單一標的深入分析」下拉選單。
    """

    def __init__(self, symbols: List[str], cog, bot):
        super().__init__(timeout=300)
        self.add_item(BatchScanWarningButton(cog, bot))


class SymbolHubView(discord.ui.View):
    """
    Interactive view for the Unified Symbol Hub (/x).
    Updates the original message in-place and provides loading feedback.
    """

    def __init__(self, symbol: str, user_id: int, bot):
        super().__init__(timeout=300)
        self.symbol = symbol.upper()
        self.user_id = user_id
        self.bot = bot
        self.base_data: Dict[str, Any] = {}

    async def _set_loading(self, interaction: discord.Interaction):
        """將所有按鈕設為禁用狀態以表示讀取中"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        """恢復按鈕狀態並更新內容"""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🏠 核心指標", style=discord.ButtonStyle.success, custom_id="btn_home"
    )
    async def btn_home(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            embed = create_tactical_symbol_embed(self.base_data)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"恢復主頁失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🎭 輿情社群",
        style=discord.ButtonStyle.primary,
        custom_id="btn_media",
    )
    async def btn_media(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            news_task = news_service.fetch_recent_news(self.symbol)
            ctx = database.get_full_user_context(self.user_id)
            reddit_task = reddit_service.get_reddit_context(
                self.symbol, enable_tunnel=ctx.enable_local_tunnel
            )
            news_text, reddit_text = await asyncio.gather(news_task, reddit_task)
            embed = create_media_sentiment_embed(self.symbol, news_text, reddit_text)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取輿情社群失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🔄 即時整理",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_refresh",
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            # 清除該 symbol 在 sentiment_engine 中的 BoundedCache 快取
            from market_analysis.sentiment_engine import _iv_cache

            if self.symbol in _iv_cache:
                del _iv_cache[self.symbol]
                logger.info(f"[{self.symbol}] 按鈕觸發：已清除 IV 數據快取")

            # 獲取 stock_cost
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            stock_cost = next(
                (
                    a.metadata.get("avg_cost", 0.0)
                    for a in assets
                    if a.symbol == self.symbol
                ),
                0.0,
            )

            # 用於 DDP 與 Polymarket 等服務
            from market_analysis.ddp_inspector import DDPInspector
            from services.polymarket_service import PolymarketService

            ddp_inspector = DDPInspector(self.bot)
            poly_service = PolymarketService(self.bot)

            # 並行抓取所有數據
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            quote_task = market_data_service.get_quote(self.symbol)
            skew_task = SentimentEngine.calculate_skew(self.symbol)
            pcr_task = SentimentEngine.calculate_pcr(self.symbol)
            uoa_task = SentimentEngine.detect_uoa(self.symbol)
            mp_task = SentimentEngine.calculate_max_pain(self.symbol)
            iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(self.symbol)
            ctx = database.get_full_user_context(self.user_id)
            reddit_task = reddit_service.get_reddit_context(
                self.symbol, enable_tunnel=ctx.enable_local_tunnel
            )
            poly_task = poly_service.get_market_snapshot(limit=10)
            ddp_task = ddp_inspector.inspect_symbol(self.symbol)
            df_hist_task = market_data_service.get_history_df(
                self.symbol, period="1y", interval="1d"
            )

            (
                df_spy,
                macro_raw,
                quote,
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_metrics,
                reddit_text,
                poly_markets,
                ddp_report,
                df_hist_1d,
            ) = await asyncio.gather(
                spy_task,
                macro_task,
                quote_task,
                skew_task,
                pcr_task,
                uoa_task,
                mp_task,
                iv_task,
                reddit_task,
                poly_task,
                ddp_task,
                df_hist_task,
            )

            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            safe_macro = macro_raw or {}
            macro_data = MacroContext(
                vix=safe_macro.get("vix", 18.0),
                oil_price=safe_macro.get("oil", 75.0),
                vix_change=safe_macro.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                self.symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": self.symbol, "stock_cost": stock_cost, "price": 0.0}

            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if is_df_valid
                    else result.get("price", 0.0)
                )

            result["quote"] = quote

            safe_skew = skew_data or {}
            result["skew"] = safe_skew.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                self.symbol, "SKEW", result["skew"]
            )

            result["pcr"] = pcr_data if pcr_data is not None else {}
            result["uoa"] = uoa_data if uoa_data is not None else []

            result["iv_data"] = iv_metrics
            result["iv_rank"] = iv_metrics.iv_rank if iv_metrics else 0.0

            safe_mp = max_pain_data or {}
            result["max_pain"] = safe_mp.get("max_pain", 0.0)

            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score
            safe_reddit_text = reddit_text or ""
            if "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            # Polymarket odds
            poly_odds = find_matching_polymarket_odds(self.symbol, poly_markets)
            result["polymarket_odds"] = poly_odds

            self.base_data = result
            embed = create_tactical_symbol_embed(self.base_data)
            await interaction.followup.send(
                embed=create_info_embed(
                    title="更新成功",
                    message=f"✨ `{self.symbol}` 最新數據已重整並更新！",
                ),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"重整數據失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(
        label="🛡️ 一鍵對沖",
        style=discord.ButtonStyle.danger,
        custom_id="btn_hedge",
    )
    async def btn_hedge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        try:
            # 根據目前波動率與情緒自動引導對沖操作
            ivr = self.base_data.get("iv_rank", 50.0)
            rec_strategy = (
                "Bull Put Spread (賣出認沽價差策略)"
                if ivr > 50.0
                else "Bear Debits / Put Protection (買入保護性認沽)"
            )

            embed_hedge = create_tactical_hedge_embed(self.symbol, ivr, rec_strategy)
            await interaction.followup.send(embed=embed_hedge, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"開啟對沖中心失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction)


class PortfolioHubView(discord.ui.View):
    """
    Interactive view for the Portfolio Hub (/dash).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🏠 戰略看板",
        style=discord.ButtonStyle.success,
        custom_id="btn_home_port",
    )
    async def btn_home(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)

            macro_raw = await market_data_service.get_macro_environment()
            vix_spot = macro_raw.get("vix", 18.0)

            embed = create_strategic_dash_embed(ctx, pnl_data, vix_spot=vix_spot)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"恢復戰略看板失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📋 實單持倉", style=discord.ButtonStyle.primary)
    async def btn_trades(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.trading_service import TradingService

            trading_service = TradingService(self.bot)
            pnl_data = await trading_service.get_portfolio_pnl(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            embed = create_trades_embed(pnl_data, ctx.capital)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取持倉失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📦 現貨持倉", style=discord.ButtonStyle.primary)
    async def btn_holdings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(self.user_id, ContextType.HOLDING)
            holdings = []
            for a in assets:
                quote = await market_data_service.get_quote(a.symbol)
                h_data = {
                    "symbol": a.symbol,
                    "quantity": a.metadata.get("quantity", 0.0),
                    "avg_cost": a.metadata.get("avg_cost", 0.0),
                    "current_price": quote.get("c", 0.0) if quote else 0.0,
                }
                holdings.append(h_data)
            ctx = database.get_full_user_context(self.user_id)
            embed = create_holdings_embed(holdings, ctx.capital)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取現貨失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🏁 財務跑道", style=discord.ButtonStyle.secondary)
    async def btn_runway(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.portfolio import refresh_portfolio_greeks
            from market_analysis.pro_management import calculate_financial_runway
            from services.asset_manager import AssetManager
            from models.asset import ContextType, HoldingMetadata

            await refresh_portfolio_greeks(self.user_id)
            ctx = database.get_full_user_context(self.user_id)
            runway_days = calculate_financial_runway(
                ctx.cash_reserve, ctx.monthly_expense, ctx.total_theta
            )
            manager = AssetManager()
            holdings = manager.get_assets(self.user_id, ContextType.HOLDING)
            total_holding_value = 0.0
            for h in holdings:
                meta = HoldingMetadata(**h.metadata)
                quote = await market_data_service.get_quote(h.symbol)
                total_holding_value += (
                    quote.get("c", 0.0) if quote else 0.0
                ) * meta.quantity
            backup_liq = total_holding_value * 0.8
            ext_runway = calculate_financial_runway(
                ctx.cash_reserve + backup_liq, ctx.monthly_expense, ctx.total_theta
            )
            embed = create_financial_runway_embed(
                cash_reserve=ctx.cash_reserve,
                monthly_expense=ctx.monthly_expense,
                total_theta=ctx.total_theta,
                runway_days=runway_days,
                backup_liquidity=backup_liq,
                extended_runway=ext_runway,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"計算跑道失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="👻 VTR 績效", style=discord.ButtonStyle.secondary)
    async def btn_vtr(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.ghost_trader import GhostTrader
            from market_analysis.attribution import AttributionEngine

            await AttributionEngine.finalize_vtr_attribution(self.user_id)
            stats = await GhostTrader.get_vtr_performance_stats(self.user_id)
            attr_lines = AttributionEngine.format_attribution_report(self.user_id)
            embed = build_vtr_stats_embed(
                interaction.user.display_name, stats, attr_lines
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取 VTR 績效失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🚨 壓力測試", style=discord.ButtonStyle.danger)
    async def btn_stress_test(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from database.orders import get_user_active_orders

            orders = get_user_active_orders(self.user_id)
            total_deficit = 0.0
            gtc_buy_orders = []
            for o in orders:
                validity = o.get("validity", "").upper()
                side = o.get("side", "").upper()
                if "GTC" in validity and side == "BUY":
                    price = o.get("limit_price", 0.0)
                    if price <= 0.0:
                        price = o.get("stop_price", 0.0)
                    qty = o.get("quantity", 0.0)
                    total_deficit += price * qty
                    gtc_buy_orders.append(o)
            ctx = database.get_full_user_context(self.user_id)
            cash_reserve = ctx.cash_reserve if ctx else 0.0

            from database.holdings import get_user_holdings

            holdings = get_user_holdings(self.user_id)
            boxx_shares = 0.0
            for h in holdings:
                if h.get("symbol", "").upper() == "BOXX":
                    boxx_shares = h.get("quantity", 0.0)
                    break
            boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
            net_deficit = cash_reserve + boxx_cash - total_deficit
            is_critical = total_deficit > (cash_reserve + boxx_cash)

            results = {
                "total_deficit": total_deficit,
                "cash_reserve": cash_reserve,
                "boxx_shares": boxx_shares,
                "boxx_cash": boxx_cash,
                "net_deficit": net_deficit,
                "is_critical": is_critical,
                "gtc_buy_orders_count": len(gtc_buy_orders),
            }
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)


# LRU Bounded Cache for macro overview (max 10 entries)
_macro_overview_cache = BoundedCache(max_size=10)


def get_macro_overview_data(user_id: int) -> dict:
    ram_usage = psutil.virtual_memory().percent
    is_degraded = ram_usage > 85.0
    cache_key = f"overview_{user_id}"

    if is_degraded and cache_key in _macro_overview_cache:
        data = _macro_overview_cache[cache_key].copy()
        data["is_degraded"] = True
        return data

    # Read from SQLite kv_cache
    from database import get_kv_cache
    from market_analysis.trading_orchestration import get_safety_payout_threshold

    spx = get_kv_cache("macro_spx") or 5150.0
    vix = get_kv_cache("macro_vix") or 18.0
    us10y = get_kv_cache("macro_us10y") or 4.25
    # Normalize US10Y if needed
    if us10y > 10.0:
        us10y = us10y / 10.0

    wti = get_kv_cache("macro_wti") or 75.0
    rrp = get_kv_cache("macro_rrp") or 420.5
    fed_balance = get_kv_cache("macro_fed_balance") or 7.25
    cpi_nfp_calendar = (
        get_kv_cache("macro_cpi_nfp_calendar") or "2026-06-18 (CPI), 2026-07-03 (NFP)"
    )
    fear_greed = get_kv_cache("macro_fear_greed") or 48.0
    gamma_flip_line = get_kv_cache("macro_gamma_flip_line") or 5180.0
    uer = get_kv_cache("macro_uer") or 4.0
    sahm_rule = get_kv_cache("macro_sahm_rule") or 0.35
    rrp_change_30d = get_kv_cache("macro_rrp_change_30d") or 5.0

    gex_fallback_val = get_kv_cache("macro_gex_is_fallback")
    gex_is_fallback = gex_fallback_val is None or int(gex_fallback_val) == 1

    # 零 Gamma 踩踏 Regime 判定
    # SPX 跌破 Gamma Flip Line 且 VIX > 20
    short_gamma_critical = (spx < gamma_flip_line) and (vix > 20.0)

    # 衰退警告 RECESSION_WARNING
    recession_warning = (sahm_rule >= 0.5) or (us10y > 4.5 and vix > 20.0)

    payout_threshold = get_safety_payout_threshold()

    data = {
        "spx": spx,
        "vix": vix,
        "us10y": us10y,
        "wti": wti,
        "rrp": rrp,
        "fed_balance": fed_balance,
        "cpi_nfp_calendar": cpi_nfp_calendar,
        "fear_greed": fear_greed,
        "gamma_flip_line": gamma_flip_line,
        "uer": uer,
        "sahm_rule": sahm_rule,
        "rrp_change_30d": rrp_change_30d,
        "short_gamma_critical": short_gamma_critical,
        "recession_warning": recession_warning,
        "payout_threshold": payout_threshold,
        "is_degraded": is_degraded,
        "gex_is_fallback": gex_is_fallback,
    }

    # Save to memory cache
    _macro_overview_cache[cache_key] = data
    return data


class PulseHubView(discord.ui.View):
    """
    Interactive view for the Pulse Hub (/market).
    """

    def __init__(self, user_id: int, bot):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.bot = bot

    async def _set_loading(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.edit_original_response(view=self)

    async def _reset_loading(self, interaction: discord.Interaction, embed=None):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="📊 總經風控", style=discord.ButtonStyle.success)
    async def btn_macro_overview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from cogs.embed_builder import build_market_macro_overview_embed

            macro_data = get_macro_overview_data(self.user_id)
            embed = build_market_macro_overview_embed(macro_data)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取總經數據失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="📅 市場日曆", style=discord.ButtonStyle.primary)
    async def btn_calendar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from services.calendar_service import calendar_service

            events = await calendar_service.get_portfolio_events(self.user_id)
            embed = create_market_calendar_embed(
                events,
                max_items=15,
                empty_message="📭 未來 7 日內無影響持倉標的的重大事件或財報。",
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取日曆失敗: {e}"), ephemeral=True
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🐋 預測市場", style=discord.ButtonStyle.primary)
    async def btn_poly(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            if not hasattr(self.bot, "polymarket_service"):
                embed = create_error_embed(
                    "Polymarket 服務未初始化。", title="系統錯誤"
                )
            else:
                markets = self.bot.polymarket_service.get_active_markets(limit=20)
                embed = create_polymarket_list_embed(markets)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"獲取預測市場失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)

    @discord.ui.button(label="🔥 高波動掃描", style=discord.ButtonStyle.secondary)
    async def btn_iv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._set_loading(interaction)
        embed = None
        try:
            from market_analysis.volatility_inspector import VolatilityInspector

            all_watchlists = database.get_all_watchlist()
            user_watch = [row[1] for row in all_watchlists if row[0] == self.user_id]
            if not user_watch:
                embed = create_info_embed(
                    "查無資料", "📭 觀察清單為空，無法執行 IV 掃描。"
                )
            else:
                inspector = VolatilityInspector(self.bot)
                results = await inspector.run_scan(user_watch, self.user_id)
                high_iv = [
                    r
                    for r in results
                    if r.get("iv_rank", 0) > 80 or r.get("is_high_risk_vol")
                ]
                embed = create_iv_risk_scan_embed(high_iv)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"執行 IV 掃描失敗: {e}"),
                ephemeral=True,
            )
        finally:
            await self._reset_loading(interaction, embed=embed)


class UnifiedTerminalCog(commands.Cog):
    """
    Unified Hubs for Nexus Seeker.
    Consolidates 20+ commands into 3 core hubs: /x, /dash, /market.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("UnifiedTerminalCog loaded.")

    @app_commands.command(
        name="x", description="🌌 標體分析中心：一站式獲取報價、量化掃描與情緒分析"
    )
    @app_commands.describe(
        symbol="股票代號 (如 NVDA，與 scan_type 二擇一)",
        scan_type="批次掃描類型 (HOLDINGS:持倉, ORDERS:掛單, OPTIONS:持有期權, WATCHLIST:自選, ALL:四者全部)",
    )
    @app_commands.choices(
        scan_type=[
            app_commands.Choice(name="💼 掃描持倉標的 (Holdings)", value="HOLDINGS"),
            app_commands.Choice(
                name="⏳ 掃描掛單標的 (Pending Orders)", value="ORDERS"
            ),
            app_commands.Choice(
                name="📜 掃描期權持倉標的 (Option Holdings)", value="OPTIONS"
            ),
            app_commands.Choice(name="🌟 掃描自選標的 (Watchlist)", value="WATCHLIST"),
            app_commands.Choice(name="🌀 掃描全部 (持倉+掛單+期權標的)", value="ALL"),
        ]
    )
    async def symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: Optional[str] = None,
        scan_type: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = interaction.user.id

            # 🚀 Task 2 Hook: Proactive Warmup during pre-market window (08:30 - 09:30 ET)
            if hasattr(self.bot, "memory_manager"):
                coro = self.bot.memory_manager.proactive_warmup()
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)

            # 1. 參數驗證
            if not symbol and not scan_type:
                return await interaction.followup.send(
                    embed=create_error_embed(
                        "請輸入 `symbol` 參數，或選擇 `scan_type` 進行批次掃描。",
                        title="輸入錯誤",
                    ),
                    ephemeral=True,
                )

            # 2. 單一標的深度分析
            if symbol:
                symbol = symbol.upper()
                await self._run_single_symbol_hub(interaction, symbol, user_id)
                return

            # 3. 批次掃描邏輯
            if not scan_type:
                return
            scan_value = scan_type.value
            target_symbols = set()

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

                watchlist_items = await asyncio.to_thread(
                    database.get_user_watchlist, user_id
                )
                for item in watchlist_items:
                    target_symbols.add(item[0].upper())

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

            try:
                # 並行獲取所有標的的雷達數據 (Cache-Aside)
                scan_results = await asyncio.gather(
                    *(self._fetch_sym_radar_data(s) for s in unique_symbols),
                    return_exceptions=True,
                )
                # 過濾 Exception 並確保是 dict 類型以滿足 mypy
                valid_results = [r for r in scan_results if isinstance(r, dict)]

                embeds = build_radar_scan_embed(valid_results, scan_value, user_id)
                if not isinstance(embeds, list):
                    embeds = [embeds]

                chunk_size = 15
                for idx, emb in enumerate(embeds):
                    chunk_results = valid_results[
                        idx * chunk_size : (idx + 1) * chunk_size
                    ]
                    chunk_symbols = [r["symbol"].upper() for r in chunk_results]
                    page_view = BatchScanView(chunk_symbols, self, self.bot)
                    await interaction.followup.send(
                        embed=emb, view=page_view, ephemeral=True
                    )

            except Exception as e:
                logger.error(f"Batch Scan Error for {scan_value}: {e}")
                await interaction.followup.send(
                    embed=create_error_embed(f"執行批次掃描時發生錯誤: {e}"),
                    ephemeral=True,
                )
        except Exception as outer_err:
            logger.error(f"Outer Symbol Hub Error: {outer_err}")
            try:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"執行 `/x` 指令時發生未預期錯誤: {outer_err}"
                    ),
                    ephemeral=True,
                )
            except Exception as follow_err:
                logger.error(f"Failed to send outer error followup: {follow_err}")

    async def _run_single_symbol_hub(
        self, interaction: discord.Interaction, symbol: str, user_id: int
    ):
        symbol = symbol.upper()
        if not await market_data_service.validate_symbol(symbol):
            return await interaction.followup.send(
                embed=create_error_embed(
                    f"無效的標的代號: `{symbol}`", title="輸入錯誤"
                ),
                ephemeral=True,
            )

        try:
            from services.asset_manager import AssetManager
            from models.asset import ContextType

            manager = AssetManager()
            assets = manager.get_assets(user_id, ContextType.HOLDING)
            stock_cost = next(
                (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
                0.0,
            )

            # 用於 DDP 與 Polymarket 等服務
            from market_analysis.ddp_inspector import DDPInspector
            from services.polymarket_service import PolymarketService

            ddp_inspector = DDPInspector(self.bot)
            poly_service = PolymarketService(self.bot)

            # 並行抓取所有數據
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            quote_task = market_data_service.get_quote(symbol)
            skew_task = SentimentEngine.calculate_skew(symbol)
            pcr_task = SentimentEngine.calculate_pcr(symbol)
            uoa_task = SentimentEngine.detect_uoa(symbol)
            mp_task = SentimentEngine.calculate_max_pain(symbol)
            iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
            ctx = database.get_full_user_context(user_id)
            reddit_task = reddit_service.get_reddit_context(
                symbol, enable_tunnel=ctx.enable_local_tunnel
            )
            poly_task = poly_service.get_market_snapshot(limit=10)
            ddp_task = ddp_inspector.inspect_symbol(symbol)
            df_hist_task = market_data_service.get_history_df(
                symbol, period="1y", interval="1d"
            )

            (
                df_spy,
                macro_raw,
                quote,
                skew_data,
                pcr_data,
                uoa_data,
                max_pain_data,
                iv_metrics,
                reddit_text,
                poly_markets,
                ddp_report,
                df_hist_1d,
            ) = await asyncio.gather(
                spy_task,
                macro_task,
                quote_task,
                skew_task,
                pcr_task,
                uoa_task,
                mp_task,
                iv_task,
                reddit_task,
                poly_task,
                ddp_task,
                df_hist_task,
            )

            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            safe_macro = macro_raw or {}
            macro_data = MacroContext(
                vix=safe_macro.get("vix", 18.0),
                oil_price=safe_macro.get("oil", 75.0),
                vix_change=safe_macro.get("vix_change", 0.0),
            )

            result = await market_math.analyze_symbol(
                symbol, stock_cost, df_spy, spy_price, vix_spot=macro_data.vix
            )
            if not result:
                result = {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}

            psq_result = analyze_psq(df_hist_1d, vix_spot=macro_data.vix)
            if psq_result:
                result["psq_result"] = psq_result
                is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
                result["price"] = (
                    df_hist_1d["Close"].iloc[-1]
                    if is_df_valid
                    else result.get("price", 0.0)
                )

            result["quote"] = quote

            safe_skew = skew_data or {}
            result["skew"] = safe_skew.get("skew", 0.0)
            result["skew_percentile"] = SentimentEngine.get_indicator_percentile(
                symbol, "SKEW", result["skew"]
            )

            result["pcr"] = pcr_data if pcr_data is not None else {}
            result["uoa"] = uoa_data if uoa_data is not None else []

            result["iv_data"] = iv_metrics
            result["iv_rank"] = iv_metrics.iv_rank if iv_metrics else 0.0

            safe_mp = max_pain_data or {}
            result["max_pain"] = safe_mp.get("max_pain", 0.0)

            result["is_ddp"] = ddp_report.get("is_ddp", False) if ddp_report else False
            result["vix"] = macro_data.vix
            result["spy_price"] = spy_price

            # Reddit sentiment score
            safe_reddit_text = reddit_text or ""
            if "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
            elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
                result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
            else:
                result["reddit_sentiment_score"] = "⚖️ 中性"

            # Polymarket odds
            poly_odds = find_matching_polymarket_odds(symbol, poly_markets)
            result["polymarket_odds"] = poly_odds

            main_embed = create_tactical_symbol_embed(result)

            view = SymbolHubView(symbol, user_id, self.bot)
            view.base_data = result

            await interaction.followup.send(embed=main_embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Symbol Hub Error for {symbol}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"載入 `{symbol}` 資料時發生錯誤: {e}"),
                ephemeral=True,
            )

    async def _async_revalidate_market_cache(self, sym: str, price: float):
        try:
            from database import save_market_cache
            from market_analysis.sentiment_engine import SentimentEngine

            logger.info(f"🔄 [SWR] Background revalidating market cache for {sym}...")
            iv_m = await SentimentEngine.fetch_and_calculate_iv_metrics(sym)
            mp_d = await SentimentEngine.calculate_max_pain(sym)

            em_weekly = iv_m.expected_move_weekly if iv_m else 0.0
            max_pain = (
                mp_d.get("max_pain", 0.0) if (mp_d and isinstance(mp_d, dict)) else 0.0
            )

            em_lower = price - em_weekly if price > 0 else 0.0
            em_upper = price + em_weekly if price > 0 else 0.0

            await asyncio.to_thread(
                save_market_cache, sym, max_pain, em_lower, em_upper, price, 0
            )
            logger.info(f"✅ [SWR] Background revalidation complete for {sym}")
        except Exception as e:
            logger.error(f"❌ [SWR] Background revalidation failed for {sym}: {e}")

    async def _fetch_sym_radar_data(self, sym: str):
        from services.single_flight import SingleFlightManager

        return await SingleFlightManager.run(
            f"analyze_{sym}", self._fetch_sym_radar_data_raw, sym
        )

    async def _fetch_sym_radar_data_raw(self, sym: str):
        """
        獲取單一標的的雷達量化數據。
        採用 Cache-Aside 設計，直接物理性從 SQLite 中的 market_cache 讀取，快取未命中則進行退級即時計算。
        """
        from database import (
            get_market_cache,
            save_market_cache,
            mark_market_cache_stale,
        )
        from market_analysis.sentiment_engine import SentimentEngine
        from services import market_data_service

        # 1. 取得 quote (必須即時，因為是價格)
        quote = await market_data_service.get_quote(sym)
        price = quote.get("c", 0.0) if quote else 0.0

        # 2. 取得 Skew (情緒)
        skew_data = await SentimentEngine.calculate_skew(sym)
        skew_val = skew_data.get("skew", 0.0) if isinstance(skew_data, dict) else 0.0
        skew_percentile = SentimentEngine.get_indicator_percentile(
            sym, "SKEW", skew_val
        )

        # 取得 UOA (異常期權活動) 資料
        uoa_data = []
        try:
            uoa_data = await SentimentEngine.detect_uoa(sym)
        except Exception as e:
            logger.error(f"[{sym}] Batch Scan 獲取 UOA 失敗: {e}")

        # 3. 讀取 market_cache 快取
        cache_data = await asyncio.to_thread(get_market_cache, sym)
        if cache_data and price > 0:
            ref_price = cache_data.get("reference_spot_price")
            if ref_price and ref_price > 0:
                deviation = abs(price - ref_price) / ref_price

                # 平滑快取防護（強制冷卻機制）
                is_cooldown = False
                updated_str = cache_data.get("updated_at")
                if updated_str:
                    try:
                        from datetime import datetime, timezone

                        updated_dt = datetime.strptime(
                            updated_str, "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        elapsed = (
                            datetime.now(timezone.utc) - updated_dt
                        ).total_seconds()
                        if elapsed < 30.0:
                            is_cooldown = True
                            logger.info(
                                f"[{sym}] 快取更新距今 {elapsed:.1f} 秒 (小於 MIN_TTL=30秒)，觸發平滑快取防護，強制判定快取依然可用。"
                            )
                    except Exception as ts_err:
                        logger.error(f"[{sym}] 解析快取時間戳記失敗: {ts_err}")

                if is_cooldown:
                    cache_data["is_stale"] = False
                elif deviation > 0.03:  # 3% 偏離度閾值
                    logger.warning(
                        f"[{sym}] Spot price shifted from {ref_price} to {price} "
                        f"(dev={deviation:.2%}), marking stale & triggering revalidation."
                    )
                    cache_data["is_stale"] = True
                    await asyncio.to_thread(mark_market_cache_stale, sym)
                    asyncio.create_task(self._async_revalidate_market_cache(sym, price))

        iv_rank_val = 0.0
        em_weekly = 0.0
        max_pain = 0.0

        if cache_data and not cache_data.get("is_stale", False):
            max_pain = cache_data.get("max_pain", 0.0)
            em_lower = cache_data.get("expected_move_lower", 0.0)
            em_upper = cache_data.get("expected_move_upper", 0.0)
            # 從上下緣反推 em_weekly
            if em_upper > em_lower and price > 0:
                em_weekly = (em_upper - em_lower) / 2.0
            # IV Rank 仍可以從 fetch_and_calculate_iv_metrics 快速取（因為它有快取）
            iv_m = await SentimentEngine.fetch_and_calculate_iv_metrics(sym)
            if iv_m:
                iv_rank_val = iv_m.iv_rank
        else:
            # Cache-Aside: 快取不存在或已過期，進行即時計算並存回 SQLite
            iv_m = await SentimentEngine.fetch_and_calculate_iv_metrics(sym)
            mp_d = await SentimentEngine.calculate_max_pain(sym)

            if iv_m:
                iv_rank_val = iv_m.iv_rank
                em_weekly = iv_m.expected_move_weekly
            if mp_d and isinstance(mp_d, dict):
                max_pain = mp_d.get("max_pain", 0.0)

            em_lower = price - em_weekly if price > 0 else 0.0
            em_upper = price + em_weekly if price > 0 else 0.0

            # 寫回快取
            await asyncio.to_thread(
                save_market_cache, sym, max_pain, em_lower, em_upper, price
            )

        # 將模擬資料庫回傳包裝成可以傳給 build_radar_scan_embed 的字典
        # 以便不管是否快取，都具有相同的 iv_metrics 結構
        mock_iv = {
            "iv_rank": iv_rank_val,
            "expected_move_weekly": em_weekly,
        }

        return {
            "symbol": sym,
            "quote": quote,
            "iv_metrics": mock_iv,
            "skew": skew_val,
            "skew_percentile": skew_percentile,
            "max_pain": {
                "max_pain": max_pain,
            },
            "uoa": uoa_data,
        }

    @app_commands.command(
        name="dash", description="📊 交易員看板：一站式監控持倉、跑道與 VTR 績效"
    )
    async def portfolio_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        from services.trading_service import TradingService

        trading_service = TradingService(self.bot)
        pnl_data = await trading_service.get_portfolio_pnl(user_id)
        ctx = database.get_full_user_context(user_id)

        # 獲取 VIX 資訊
        macro_raw = await market_data_service.get_macro_environment()
        vix_spot = macro_raw.get("vix", 18.0)

        embed = create_strategic_dash_embed(ctx, pnl_data, vix_spot=vix_spot)

        view = PortfolioHubView(user_id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="market", description="🌌 市場情報中心：監控日曆、預測市場與高波動標的"
    )
    async def pulse_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 🚀 Task 2 Hook: Proactive Warmup during pre-market window
        if hasattr(self.bot, "memory_manager"):
            coro = self.bot.memory_manager.proactive_warmup()
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

        macro_data = get_macro_overview_data(interaction.user.id)
        embed = build_market_macro_overview_embed(macro_data)

        view = PulseHubView(interaction.user.id, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="stress_test",
        description="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)",
    )
    async def stress_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        try:
            from database.orders import get_user_active_orders

            orders = get_user_active_orders(user_id)
            total_deficit = 0.0
            gtc_buy_orders = []
            for o in orders:
                validity = o.get("validity", "").upper()
                side = o.get("side", "").upper()
                if "GTC" in validity and side == "BUY":
                    price = o.get("limit_price", 0.0)
                    if price <= 0.0:
                        price = o.get("stop_price", 0.0)
                    qty = o.get("quantity", 0.0)
                    total_deficit += price * qty
                    gtc_buy_orders.append(o)
            ctx = database.get_full_user_context(user_id)
            cash_reserve = ctx.cash_reserve if ctx else 0.0

            from database.holdings import get_user_holdings

            holdings = get_user_holdings(user_id)
            boxx_shares = 0.0
            for h in holdings:
                if h.get("symbol", "").upper() == "BOXX":
                    boxx_shares = h.get("quantity", 0.0)
                    break
            boxx_cash = min(boxx_shares, 180.0) * (21000.0 / 180.0)
            net_deficit = cash_reserve + boxx_cash - total_deficit
            is_critical = total_deficit > (cash_reserve + boxx_cash)

            results = {
                "total_deficit": total_deficit,
                "cash_reserve": cash_reserve,
                "boxx_shares": boxx_shares,
                "boxx_cash": boxx_cash,
                "net_deficit": net_deficit,
                "is_critical": is_critical,
                "gtc_buy_orders_count": len(gtc_buy_orders),
            }
            from cogs.embed_builder import create_stress_test_embed

            embed = create_stress_test_embed(results)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed(f"壓力測試失敗: {e}"), ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(UnifiedTerminalCog(bot))
