import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional

from database.user_settings import get_full_user_context, upsert_user_config
from market_analysis.pro_management import simulate_cc_transition
from services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

class ProInvestorCog(commands.Cog):
    """
    Professional Investor Upgrade Cog.
    Provides advanced transition simulations and financial runway metrics.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mds = MarketDataService()

    @app_commands.command(name="transition_sim", description="Simulate Synthetic Long to Covered Call transition")
    @app_commands.describe(
        symbol="Ticker symbol",
        target_price="Simulated stock exit/entry price",
        cc_strike="Target Covered Call strike price",
        realized_pnl="Optional: Realized PnL from the closed option"
    )
    async def transition_sim(
        self, 
        interaction: discord.Interaction, 
        symbol: str, 
        target_price: float, 
        cc_strike: float,
        realized_pnl: float = 0.0
    ):
        """
        Executes a transition simulation from speculative to core equity.
        """
        await interaction.response.defer(ephemeral=True)
        
        symbol = symbol.upper()
        
        # In a real scenario, we might fetch the current CC premium from MDS
        # For this simulation, we estimate a 2% premium yield if not provided
        est_cc_premium = target_price * 0.02 
        
        try:
            result = simulate_cc_transition(
                current_option_pnl=realized_pnl,
                current_stock_price=target_price,
                target_cc_strike=cc_strike,
                target_cc_premium=est_cc_premium
            )
            
            embed = discord.Embed(
                title=f"📈 Transition Simulation: {symbol}",
                description=f"Synthetic Exit ➔ Core Equity Transition @ ${target_price:.2f}",
                color=discord.Color.blue()
            )
            
            embed.add_field(name="Realized Option PnL", value=f"${result.initial_pnl:,.2f}", inline=True)
            embed.add_field(name="Net Capital Outlay", value=f"${result.net_capital_outlay:,.2f}", inline=True)
            embed.add_field(name="Adjusted Cost Basis", value=f"${result.adjusted_cost_basis:.2f}", inline=True)
            embed.add_field(name="CC Strike / Premium", value=f"${result.cc_strike:.2f} / ${result.cc_premium:.2f}", inline=True)
            embed.add_field(name="Projected AROC", value=f"{result.projected_aroc:.2f}%", inline=True)
            embed.add_field(name="Efficiency Gain", value=f"{result.capital_efficiency_gain:.2f}%", inline=True)
            
            embed.set_footer(text="Calculated using Nexus Position Evolution Engine (30D DTE Projection)")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Transition simulation failed for {symbol}: {e}")
            await interaction.followup.send(f"❌ Simulation failed: {str(e)}", ephemeral=True)

    @app_commands.command(name="runway_check", description="Calculate financial runway based on portfolio yield")
    async def runway_check(self, interaction: discord.Interaction):
        """
        Calculates the user's financial runway in months based on options income.
        """
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        ctx = get_full_user_context(user_id)
        
        if not ctx.is_professional_mode:
            await interaction.followup.send(
                "⚠️ Professional Mode is not enabled. Use `/settings` to upgrade your profile.", 
                ephemeral=True
            )
            return

        if ctx.monthly_expense <= 0:
            await interaction.followup.send(
                "❌ Monthly expense not set. Update your profile in `/settings`.", 
                ephemeral=True
            )
            return

        # Monthly Yield calculation: 
        # total_theta is daily. convert to monthly.
        # Apply tax reserve.
        gross_monthly_yield = ctx.total_theta * 30
        net_monthly_yield = gross_monthly_yield * (1 - ctx.tax_reserve_rate)
        
        runway_months = net_monthly_yield / ctx.monthly_expense if ctx.monthly_expense > 0 else 0
        
        embed = discord.Embed(
            title="🏁 Financial Runway Analysis",
            color=discord.Color.green() if runway_months >= 1 else discord.Color.orange()
        )
        
        embed.add_field(name="Monthly Expense", value=f"${ctx.monthly_expense:,.2f}", inline=True)
        embed.add_field(name="Tax Reserve Rate", value=f"{ctx.tax_reserve_rate*100:.0f}%", inline=True)
        embed.add_field(name="Daily Theta (Portfolio)", value=f"${ctx.total_theta:,.2f}", inline=False)
        embed.add_field(name="Est. Net Monthly Income", value=f"${net_monthly_yield:,.2f}", inline=True)
        
        status_text = "Sustainable" if runway_months >= 1.0 else "Deficit"
        embed.add_field(name="Runway (Months)", value=f"{runway_months:.2f} ({status_text})", inline=True)
        
        embed.set_footer(text="Theta-based yield projection. Does not account for capital gains/losses.")
        
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(ProInvestorCog(bot))
