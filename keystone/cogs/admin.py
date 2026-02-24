import os
import discord
from discord import app_commands
from discord.ext import commands

def _parse_dev_ids() -> set[int]:
    raw = (os.getenv("DEV_USER_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out

def _is_dev(user_id: int) -> bool:
    return user_id in _parse_dev_ids()

def _has_admin(interaction: discord.Interaction) -> bool:
    # Safe guard if somehow used outside a guild
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Dev/Admin: sync slash commands (guild)")
    async def sync(self, interaction: discord.Interaction):
        if not (_has_admin(interaction) or _is_dev(interaction.user.id)):
            return await interaction.response.send_message("❌ Dev/Admin only.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        guild = discord.Object(id=interaction.guild.id)
        self.bot.tree.copy_global_to(guild=guild)  # helps refresh signatures
        synced = await self.bot.tree.sync(guild=guild)

        await interaction.followup.send(f"✅ Synced {len(synced)} commands.", ephemeral=True)

    @app_commands.command(name="reload", description="Dev/Admin: reload a cog")
    @app_commands.describe(cog="Cog name like 'oc' or 'cogs.oc'")
    async def reload(self, interaction: discord.Interaction, cog: str):
        if not (_has_admin(interaction) or _is_dev(interaction.user.id)):
            return await interaction.response.send_message("❌ Dev/Admin only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        name = cog if cog.startswith("cogs.") else f"cogs.{cog}"

        try:
            await self.bot.reload_extension(name)
            await interaction.followup.send(f"✅ Reloaded `{name}`", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))