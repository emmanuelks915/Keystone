from __future__ import annotations

import datetime
from typing import Optional, Union

import discord
from discord.ext import commands

UserLike = Union[discord.Member, discord.User, None]

# 🔹 Your main server (Skyfall) guild ID
SKYFALL_GUILD_ID = 1374730886234374235

# 🔹 Audit log channel in Skyfall
AUDIT_CHANNEL_ID = 1400633367308800090


def dispatch_audit(
    bot: commands.Bot,
    *,
    guild_id: int,
    action: str,
    user: UserLike = None,
    details: Optional[str] = None,
    severity: str = "INFO",
):
    """
    Helper to fire an audit event from anywhere.

    Example:
        dispatch_audit(
            self.bot,
            guild_id=interaction.guild_id,
            action="giveaway_end",
            user=interaction.user,
            details=f"Ended giveaway {message.id} with 3 winners.",
        )
    """
    # Only dispatch for your main guild
    if guild_id != SKYFALL_GUILD_ID:
        return

    bot.dispatch(
        "tangerine_audit_log",
        guild_id,
        action,
        user,
        details or "",
        severity,
    )


class AuditLog(commands.Cog):
    """
    Central audit logger for Tangerine.

    - Logs every successful slash command
    - Logs slash command errors
    - Supports custom audit events
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Locked to your Skyfall audit channel
        self.audit_channel_id: int = AUDIT_CHANNEL_ID

    def get_audit_channel(
        self,
        guild: Optional[discord.Guild],
    ) -> Optional[discord.TextChannel]:
        # Only log for your main guild
        if guild is None or guild.id != SKYFALL_GUILD_ID:
            return None

        channel = guild.get_channel(self.audit_channel_id)
        if channel is None:
            channel = self.bot.get_channel(self.audit_channel_id)  # type: ignore

        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def send_audit_embed(
        self,
        *,
        guild: Optional[discord.Guild],
        action: str,
        user: UserLike,
        details: str,
        severity: str = "INFO",
    ):
        channel = self.get_audit_channel(guild)
        if channel is None:
            return

        now = discord.utils.utcnow()

        embed = discord.Embed(
            title=f"[{severity}] {action}",
            description=details or "No details provided.",
            color=(
                discord.Color.orange()
                if severity.upper() == "WARN"
                else discord.Color.blurple()
            ),
            timestamp=now,
        )

        if user is not None:
            embed.add_field(
                name="Actor",
                value=f"{getattr(user, 'mention', user)} (`{user.id}`)",
                inline=False,
            )

        if guild is not None:
            embed.set_footer(text=f"Guild: {guild.name} • ID: {guild.id}")
        else:
            embed.set_footer(text="DM Context")

        await channel.send(embed=embed)

    # --------- automatic command logging ---------

    @commands.Cog.listener()
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: discord.app_commands.Command
        | discord.app_commands.ContextMenu,
    ):
        """Logs every successful slash command (Skyfall only)."""
        guild = interaction.guild
        if guild is None or guild.id != SKYFALL_GUILD_ID:
            return

        user = interaction.user
        cmd_name = command.qualified_name

        options_str = ""
        data = getattr(interaction, "data", None)
        if isinstance(data, dict):
            options = data.get("options", [])
            if options:
                parts = []
                for opt in options:
                    name = opt.get("name")
                    value = opt.get("value")
                    if name is not None:
                        parts.append(f"{name}={value}")
                if parts:
                    options_str = ", ".join(parts)

        details_lines = [
            f"Command: `/{cmd_name}`",
            f"User: {user} (`{user.id}`)",
            f"Guild: {guild.name} (`{guild.id}`)",
        ]
        if interaction.channel:
            details_lines.append(f"Channel: {interaction.channel.mention}")  # type: ignore
        if options_str:
            details_lines.append(f"Options: `{options_str}`")

        details = "\n".join(details_lines)

        await self.send_audit_embed(
            guild=guild,
            action="command_used",
            user=user,
            details=details,
            severity="INFO",
        )

    @commands.Cog.listener()
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        """Logs slash command errors (Skyfall only)."""
        guild = interaction.guild
        if guild is None or guild.id != SKYFALL_GUILD_ID:
            return

        user = interaction.user
        command = interaction.command
        cmd_name = command.qualified_name if command else "unknown"

        details_lines = [
            f"Command: `/{cmd_name}`",
            f"User: {user} (`{user.id}`)",
            f"Error: `{error.__class__.__name__}` - {error}",
            f"Guild: {guild.name} (`{guild.id}`)",
        ]
        if interaction.channel:
            details_lines.append(f"Channel: {interaction.channel.mention}")  # type: ignore

        details = "\n".join(details_lines)

        await self.send_audit_embed(
            guild=guild,
            action="command_error",
            user=user,
            details=details,
            severity="WARN",
        )

    # --------- custom audit event listener ---------

    @commands.Cog.listener()
    async def on_tangerine_audit_log(
        self,
        guild_id: int,
        action: str,
        user: UserLike,
        details: str,
        severity: str,
    ):
        # Only care about events from your main guild
        if guild_id != SKYFALL_GUILD_ID:
            return

        guild = self.bot.get_guild(guild_id)
        await self.send_audit_embed(
            guild=guild,
            action=action,
            user=user,
            details=details,
            severity=severity,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLog(bot))
