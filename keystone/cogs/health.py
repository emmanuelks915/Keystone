from __future__ import annotations

import os
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ----------------- CONFIG -----------------

# Health status channel (defaults to your #tangerine-systems)
HEALTH_STATUS_CHANNEL_ID = int(
    os.getenv("HEALTH_STATUS_CHANNEL_ID", "1448335152584593551") or 0
)

# Role to ping whenever something is NOT healthy (Supabase down, etc.)
HEALTH_ALERT_ROLE_ID = int(
    os.getenv("HEALTH_ALERT_ROLE_ID", "1374730886507139072") or 0
)

# 🔹 Guild-scope (Skyfall only)
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)


class Health(commands.Cog):
    """
    Health / status monitoring.

    - Sends a "bot is online" message on startup (with DB status).
    - Periodically checks:
        • Discord latency
        • Supabase connectivity (via your `ocs` table)
      and reports changes to a dedicated health channel.
    - `/bot_health` slash command for on-demand status.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        # tracks last True/False for Supabase health (None = unknown)
        self.last_supabase_ok: Optional[bool] = None
        self.health_loop.start()

    # ----------------- helpers -----------------

    def _supabase(self):
        """Grab the Supabase client from the bot (set in bot.py), if any."""
        return getattr(self.bot, "supabase", None)

    async def _check_supabase(self) -> Optional[bool]:
        """
        Return:
          True  -> Supabase reachable
          False -> Supabase configured but failing
          None  -> Supabase not configured on the bot
        """
        supabase = self._supabase()
        if supabase is None:
            return None

        try:
            # Light, generic ping: your `ocs` table is used everywhere anyway.
            res = (
                supabase.table("ocs")
                .select("oc_id")
                .limit(1)
                .execute()
            )
            _ = getattr(res, "data", None)
            return True
        except Exception as e:
            print(f"[Health] Supabase health check failed: {e}")
            return False

    def _format_uptime(self) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        delta: datetime.timedelta = now - self.started_at
        days = delta.days
        seconds = delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    async def _send_health_event(
        self,
        title: str,
        description: str,
        color: discord.Color,
        alert: bool = False,
    ):
        """
        Post a health event embed to the status channel.

        If `alert` is True, it will mention HEALTH_ALERT_ROLE_ID (staff).
        """
        if not HEALTH_STATUS_CHANNEL_ID:
            return

        channel = self.bot.get_channel(HEALTH_STATUS_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )
        embed.set_footer(text=f"Uptime: {self._format_uptime()}")

        content = None
        if alert and HEALTH_ALERT_ROLE_ID:
            content = f"<@&{HEALTH_ALERT_ROLE_ID}>"

        try:
            await channel.send(content=content, embed=embed)
        except Exception as e:
            print(f"[Health] Failed to send health event: {e}")

    # ----------------- listeners -----------------

    @commands.Cog.listener()
    async def on_ready(self):
        # Fire once per process boot; nice little "I'm back" ping with DB status.
        if not HEALTH_STATUS_CHANNEL_ID:
            return

        supabase_status = await self._check_supabase()
        if supabase_status is True:
            db_line = "Supabase: 🟢 Connected (startup check OK)"
        elif supabase_status is False:
            db_line = "Supabase: 🔴 Failing on startup (check Railway/Supabase)."
        else:
            db_line = "Supabase: ⚪ Not configured on this bot."

        desc = (
            "Bot process started and connected to Discord.\n\n"
            f"{db_line}"
        )

        # On startup we don't ping staff; if it's bad, the loop will also catch it.
        await self._send_health_event(
            title="✅ Tangerine is Online",
            description=desc,
            color=discord.Color.green(),
            alert=False,
        )

    # ----------------- background loop -----------------

    @tasks.loop(minutes=5)
    async def health_loop(self):
        """
        Runs every 5 minutes:
        - Checks Supabase status.
        - If Supabase status changed (OK -> FAIL or FAIL -> OK), logs to health channel.
        """
        await self.bot.wait_until_ready()

        supabase_status = await self._check_supabase()
        latency_ms = int(self.bot.latency * 1000) if self.bot.latency is not None else -1

        # Only shout when Supabase state actually changes (to avoid spam).
        if supabase_status != self.last_supabase_ok:
            # record new state
            self.last_supabase_ok = supabase_status

            if supabase_status is True:
                desc = (
                    f"Supabase connectivity **restored**.\n"
                    f"- Latency: **{latency_ms} ms**\n"
                    f"- DB ping via `ocs` table succeeded."
                )
                await self._send_health_event(
                    title="🟢 Supabase Healthy",
                    description=desc,
                    color=discord.Color.green(),
                    alert=False,  # no staff ping for recovery, just info
                )
            elif supabase_status is False:
                desc = (
                    "Supabase connectivity **failed**.\n"
                    "- DB calls may error or time out.\n"
                    "- Check Railway/Supabase dashboards.\n\n"
                    f"Current bot latency: **{latency_ms} ms**"
                )
                await self._send_health_event(
                    title="🔴 Supabase Unreachable",
                    description=desc,
                    color=discord.Color.red(),
                    alert=True,  # ping staff here, this is Not Green™
                )
            else:
                # supabase_status is None -> no supabase on bot; log once as info.
                await self._send_health_event(
                    title="⚪ Supabase Not Configured",
                    description=(
                        "Bot is running without a configured Supabase client on `bot.supabase`.\n"
                        "This is fine if intentional; otherwise check your bot startup."
                    ),
                    color=discord.Color.light_grey(),
                    alert=False,
                )

    @health_loop.before_loop
    async def before_health_loop(self):
        print("[Health] Waiting for bot to be ready...")
        await self.bot.wait_until_ready()
        print("[Health] Health loop started.")

    # ----------------- slash command -----------------

    @app_commands.command(
        name="bot_health",
        description="Show Tangerine's current health status (latency, DB, uptime).",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def health_status(self, interaction: discord.Interaction):
        """
        Slash command handler for /bot_health.
        Note: method name must NOT start with 'bot_' or 'cog_'.
        """
        await interaction.response.defer(ephemeral=False)

        latency_ms = int(self.bot.latency * 1000) if self.bot.latency is not None else -1
        supabase_status = await self._check_supabase()
        uptime_str = self._format_uptime()

        if supabase_status is True:
            db_text = "🟢 Connected (health check OK)"
        elif supabase_status is False:
            db_text = "🔴 Failing (health check error)"
        else:
            db_text = "⚪ Not configured on this bot"

        embed = discord.Embed(
            title="📊 Tangerine Bot Health",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Discord Latency", value=f"{latency_ms} ms", inline=True)
        embed.add_field(name="Supabase", value=db_text, inline=True)
        embed.add_field(name="Uptime", value=uptime_str, inline=False)

        guild_count = len(self.bot.guilds)
        user_count = sum(g.member_count or 0 for g in self.bot.guilds)
        embed.add_field(name="Guilds", value=str(guild_count), inline=True)
        embed.add_field(name="Approx. Users", value=str(user_count), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Health(bot))
