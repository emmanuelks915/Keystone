import discord
from discord import app_commands
from discord.ext import commands

class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check if Keystone is online.")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = int(self.bot.latency * 1000)
        await interaction.response.send_message(f"🧱 Keystone operational. ({latency_ms}ms)", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
