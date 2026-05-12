import os
import traceback
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
    # Safeguard if somehow used outside a guild
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


def _can_dev(interaction: discord.Interaction) -> bool:
    return _has_admin(interaction) or _is_dev(interaction.user.id)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Dev/Admin: sync slash commands (guild)")
    async def sync(self, interaction: discord.Interaction):
        if not _can_dev(interaction):
            return await interaction.response.send_message("❌ Dev/Admin only.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        guild = discord.Object(id=interaction.guild.id)
        try:
            # Helps refresh signatures when globals changed
            self.bot.tree.copy_global_to(guild=guild)
            synced = await self.bot.tree.sync(guild=guild)
            await interaction.followup.send(f"✅ Synced {len(synced)} commands.", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ Sync failed: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="wipe", description="Dev/Admin: wipe guild commands then re-sync (fix signature mismatches)")
    async def wipe(self, interaction: discord.Interaction):
        """
        Use when Discord has a cached/old command signature and /sync can't fix it.
        This clears ONLY this guild's commands and re-registers from current code.
        """
        if not _can_dev(interaction):
            return await interaction.response.send_message("❌ Dev/Admin only.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        guild = discord.Object(id=interaction.guild.id)
        try:
            # 1) Clear guild commands on the bot-side tree
            self.bot.tree.clear_commands(guild=guild)
            # 2) Push the empty set to Discord (this is the actual wipe)
            await self.bot.tree.sync(guild=guild)

            # 3) Re-copy globals and sync again to rebuild from current code
            self.bot.tree.copy_global_to(guild=guild)
            rebuilt = await self.bot.tree.sync(guild=guild)

            await interaction.followup.send(
                f"🧨 Wiped + rebuilt guild commands. Now registered: {len(rebuilt)}",
                ephemeral=True
            )
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ Wipe failed: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="reload", description="Dev/Admin: reload a cog")
    @app_commands.describe(cog="Cog name like 'oc' or 'cogs.oc'")
    async def reload(self, interaction: discord.Interaction, cog: str):
        if not _can_dev(interaction):
            return await interaction.response.send_message("❌ Dev/Admin only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        name = cog if cog.startswith("cogs.") else f"cogs.{cog}"

        try:
            await self.bot.reload_extension(name)
            await interaction.followup.send(f"✅ Reloaded `{name}`", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))