import asyncio
import logging

import database
from database.user_settings import get_full_user_context
from services import market_data_service
from config import get_vix_tier, VIX_LADDER_CONFIG
from market_analysis.risk_engine import (
    get_macro_risk_metrics,
    calculate_vega_adjusted_delta,
    calculate_hedge_instruction,
)
import sqlite3
import config

logger = logging.getLogger(__name__)


class HedgeMonitorService:
    """
    Automated Hedging & Alert Pipeline.
    Monitors VIX/IV spikes and pushes actionable hedge instructions.
    """

    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self._monitor_task = None
        self._last_vix_level = None
        self._last_vix_stage = None
        self._check_interval = 300  # 5 minutes

    def start(self):
        if self.running:
            return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("🛡️ Hedge Monitor Service started.")

    def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("🛑 Hedge Monitor Service stopped.")

    async def _monitor_loop(self):
        while self.running:
            try:
                await self._check_spikes_and_alerts()
            except Exception as e:
                logger.error(f"Hedge Monitor loop error: {e}", exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _check_spikes_and_alerts(self):
        # 1. Fetch current VIX
        macro = await market_data_service.get_macro_environment()
        current_vix = macro.get("vix", 18.0)
        current_tier = get_vix_tier(current_vix)
        current_stage_idx = next(
            (
                i
                for i, t in enumerate(VIX_LADDER_CONFIG)
                if t["name"] == current_tier["name"]
            ),
            2,
        )

        # 2. Check for VIX stage moves or spikes
        is_spike = False
        stage_move = 0

        if self._last_vix_level is not None:
            vix_change_pct = (current_vix - self._last_vix_level) / self._last_vix_level
            if vix_change_pct >= 0.10:
                is_spike = True

            if self._last_vix_stage is not None:
                stage_move = current_stage_idx - self._last_vix_stage
                if stage_move >= 2:
                    is_spike = True

        # Update state
        self._last_vix_level = current_vix
        self._last_vix_stage = current_stage_idx

        if is_spike:
            logger.warning(
                f"🚨 VIX Spike detected! Current: {current_vix:.2f}, Move: {stage_move} stages."
            )
            await self._trigger_global_hedge_assessment(current_vix, stage_move)

    async def _trigger_global_hedge_assessment(self, vix_level: float, stage_move: int):
        user_ids = database.get_all_user_ids()
        for uid in user_ids:
            try:
                await self._assess_and_alert_user(uid, vix_level, stage_move)
            except Exception as e:
                logger.error(f"Failed to assess hedge for user {uid}: {e}")

    async def _assess_and_alert_user(
        self, user_id: int, vix_level: float, stage_move: int
    ):
        # 1. Refresh Greeks to get latest Vega/Vanna
        from market_analysis.portfolio import refresh_portfolio_greeks

        await refresh_portfolio_greeks(user_id)

        # 2. Calculate Portfolio Risk from Assets table
        user_context = get_full_user_context(user_id)
        from services.asset_manager import AssetManager
        from models.asset import ContextType, TradeMetadata, HoldingMetadata
        import json

        manager = AssetManager()
        total_delta = 0.0
        total_vega = 0.0
        total_vanna = 0.0
        total_theta = 0.0
        total_gamma = 0.0

        spy_df = await market_data_service.get_history_df("SPY", "2d")
        spy_price = spy_df["Close"].iloc[-1] if not spy_df.empty else 670.0

        with manager._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT context_type, metadata FROM assets WHERE user_id = ? AND context_type IN ('TRADE', 'HOLDING')",
                (user_id,),
            )
            for row in cursor.fetchall():
                c_type, meta_str = row
                meta = json.loads(meta_str)

                if c_type == ContextType.TRADE:
                    t_meta = TradeMetadata(**meta)
                    total_delta += t_meta.weighted_delta
                    total_vega += t_meta.vega
                    total_vanna += t_meta.vanna
                    total_theta += t_meta.theta
                    total_gamma += t_meta.gamma
                    # Margin is not in metadata yet, but we can approximate or ignore for spike alert
                elif c_type == ContextType.HOLDING:
                    h_meta = HoldingMetadata(**meta)
                    total_delta += h_meta.weighted_delta

        metrics = get_macro_risk_metrics(
            total_delta,
            total_theta,
            0.0,  # Margin used 0.0 for now
            total_gamma,
            user_context.capital,
            spy_price,
            vix_spot=vix_level,
            total_vega=total_vega,
            total_vanna=total_vanna,
        )

        # 3. Calculate Hedge Instruction
        # Account for Hidden Delta: Delta_adj = Delta + Vanna * Delta_Vol
        # Assume Delta_Vol is 10% (0.10) for the spike
        adj_delta = calculate_vega_adjusted_delta(total_delta, total_vanna, 0.10)

        # Hedge using SPY shorting (Delta -1.0)
        hedge_qty = calculate_hedge_instruction(adj_delta, -1.0)
        if abs(hedge_qty) < 5:
            return  # Ignore small hedges

        instr_text = f"建議對沖：{'賣出' if hedge_qty > 0 else '買入'} {abs(hedge_qty)} 股 SPY 以中和當前 {adj_delta:+.1f} 的調整後 Delta 曝險。"
        if abs(adj_delta) > 50:
            instr_text = "⚠️ [緊急對沖指令] " + instr_text

        # 4. LLM Narration
        narration = await self._generate_narration(
            user_id, metrics, adj_delta, vix_level
        )

        # 5. Polymarket Snapshot mechanism
        poly_snapshot = None
        try:
            if hasattr(self.bot, "polymarket_service"):
                # [Snapshot Mechanism] 獲取目前活躍市場的即時快照
                poly_snapshot = await self.bot.polymarket_service.get_market_snapshot(
                    limit=3
                )
        except Exception as e:
            logger.debug(f"Failed to capture poly snapshot: {e}")

        # 6. VTR Logging & Real Persistence
        from market_analysis.attribution import AttributionEngine

        pre_hedge_greeks = {
            "delta": round(metrics["total_beta_delta"], 2),
            "vega": round(metrics["total_vega"], 2),
            "vanna": round(metrics["total_vanna"], 2),
            "gamma": round(metrics["total_gamma"], 4),
        }
        # 將快照序列化存入 vtr_hedge_logs
        await AttributionEngine.log_vtr_hedge(
            user_id, "VIX_SPIKE_HEDGE", pre_hedge_greeks, poly_snapshot
        )

        alert_id = self._save_alert(
            user_id,
            vix_level,
            stage_move,
            total_delta,
            total_vega,
            hedge_qty,
            instr_text,
            narration,
        )

        # 7. Discord Alert
        await self._send_discord_alert(
            user_id,
            vix_level,
            stage_move,
            metrics,
            adj_delta,
            hedge_qty,
            instr_text,
            narration,
            alert_id,
            poly_snapshot,
        )

    async def _generate_narration(
        self, user_id: int, metrics: dict, adj_delta: float, vix: float
    ) -> str:
        prompt = f"""
        當前市場 VIX 急升至 {vix:.2f}。
        用戶組合數據：
        - 淨 Delta 曝險: {metrics['total_beta_delta']:.2f}
        - 調整後 Delta (考慮 Vanna): {adj_delta:.2f}
        - Vega 曝險: {metrics['total_vega']:.2f}
        - Vanna 曝險: {metrics['total_vanna']:.2f}

        請以資深風險控管主管 (CRO) 的口吻，用繁體中文解釋為什麼需要對沖。
        說明 IV 上升對當前部位的具體威脅（特別是隱含 Delta 的擴張）。
        字數控制在 80 字以內，語氣冷靜專業。
        """
        # We can use a simplified call to llm_service
        try:
            from services.llm_service import client, LLM_MODEL_NAME

            response = await client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[
                    {"role": "system", "content": "You are a Quant Risk Manager."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            return "市場波動劇烈，組合 Delta 已偏離中性。建議執行對沖以鎖定風險。"

    def _save_alert(
        self, user_id, vix, stage_move, delta, vega, hedge_qty, instr, narration
    ):
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO hedge_alerts (user_id, vix_level, vix_stage_move, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, narration)
            VALUES (?, ?, ?, ?, ?, 'SPY', ?, ?, ?)
        """,
            (user_id, vix, stage_move, delta, vega, hedge_qty, instr, narration),
        )
        alert_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return alert_id

    async def _send_discord_alert(
        self,
        user_id,
        vix,
        stage_move,
        metrics,
        adj_delta,
        hedge_qty,
        instr,
        narration,
        alert_id,
        poly_snapshot=None,
    ):
        import discord
        from config import get_vix_tier

        tier = get_vix_tier(vix)
        color = discord.Color(tier.get("color_hex", 0xFF0000))

        embed = discord.Embed(
            title="🚨 【戰位報告：自動化對沖警報】",
            description=f"**警報等級：** {tier['emoji']} {tier['name']} (移動 `{stage_move:+} 階`)",
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="📊 風險指標",
            value=(
                f"• **即時 VIX:** `{vix:.2f}`\n"
                f"• **淨 Delta:** `{metrics['total_beta_delta']:+.1f}`\n"
                f"• **調整後 Delta:** `{adj_delta:+.1f}` (Hidden Delta)\n"
                f"• **Vega 脆弱性:** `{metrics['total_vega']:+.2f}`"
            ),
            inline=False,
        )

        if poly_snapshot:
            snapshot_text = ""
            for event in poly_snapshot:
                q = event.get("question")[:40] + "..."
                odds = event.get("odds_distribution", [])
                odds_str = " | ".join(
                    [
                        f"{o.get('outcome')}: `{o.get('odds')*100:.0f}%`"
                        for o in odds[:2]
                    ]
                )
                snapshot_text += f"• **{q}**\n  └ {odds_str}\n"

            if snapshot_text:
                embed.add_field(
                    name="🌐 [快取快照] Polymarket 即時機率",
                    value=snapshot_text,
                    inline=False,
                )

        embed.add_field(name="🤖 AI 風險敘述", value=f"*{narration}*", inline=False)

        embed.add_field(
            name="🛡️ 對沖建議指令", value=f"```fix\n{instr}\n```", inline=False
        )

        embed.add_field(
            name="📈 預期效果",
            value=(
                f"執行後淨 Delta 將回歸至 `{adj_delta + (hedge_qty * -1.0):+.1f}` 附近，"
                f"顯著降低系統性回撤風險。"
            ),
            inline=False,
        )

        embed.set_footer(text=f"Nexus Seeker Battle Station | Alert ID: {alert_id}")

        await self.bot.queue_dm(user_id, embed=embed)
