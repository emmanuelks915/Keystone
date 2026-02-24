# cogs/postwindow.py
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

UTC = timezone.utc

# ---------- Guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)


class WindowState:
    __slots__ = (
        "channel_id",
        "ends_at",
        "warn_at",
        "locked",
        "closed",
        "excused_until",
        "extensions",
        "last_post",
        "ping_here",
    )

    def __init__(
        self,
        channel_id: int,
        ends_at: datetime,
        warn_at: Optional[datetime],
        locked: bool,
        ping_here: bool,
    ):
        self.channel_id = channel_id
        self.ends_at = ends_at
        self.warn_at = warn_at
        self.locked = locked
        self.closed = False
        self.excused_until: Dict[int, datetime] = {}  # user_id -> until
        self.extensions: Dict[int, int] = {}  # user_id -> extra seconds for this window
        self.last_post: Dict[int, datetime] = {}  # user_id -> last message time (informational)
        self.ping_here = ping_here


def parse_duration(s: str) -> int:
    """Parse '24h', '90m', '1h30m', '30m15s' into seconds."""
    s = s.strip().lower()
    if not s:
        raise ValueError("Provide a duration, e.g., 24h, 90m, 30m15s")
    total = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
            continue
        if ch in "hms" and num:
            n = int(num)
            if ch == "h":
                total += n * 3600
            elif ch == "m":
                total += n * 60
            elif ch == "s":
                total += n
            num = ""
        elif ch.isspace():
            continue
        else:
            raise ValueError("Use formats like 24h, 90m, 1h30m, 30m15s")
    if num:
        total += int(num)
    if total <= 0:
        raise ValueError("Duration must be > 0")
    return total


def fmt_human_left(seconds: int) -> str:
    """Return 'about X hours Y minutes' style text for a remaining seconds value."""
    if seconds <= 0:
        return "no time"
    mins_total = (seconds + 59) // 60  # round up to the next minute
    hours = mins_total // 60
    mins = mins_total % 60
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins:
        parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    if not parts:  # < 1 minute
        return "less than a minute"
    return "about " + " ".join(parts)


def fmt_rel(dt: datetime) -> str:
    secs = int((dt - datetime.now(UTC)).total_seconds())
    m = abs(secs) // 60
    s = abs(secs) % 60
    sign = "" if secs >= 0 else "-"
    if m:
        return f"{sign}{m}m {s}s"
    return f"{sign}{s}s"


class PostWindow(commands.Cog):
    """Freeform post timer: start, warn, hard close, optional channel/thread lock."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: Dict[int, WindowState] = {}  # channel_id -> state
        self.watchdog.start()

    def cog_unload(self):
        self.watchdog.cancel()

    # Group – now global; guild scoping is handled by /sync
    group = app_commands.Group(
        name="postwindow",
        description="Freeform post window",
    )

    @group.command(name="start", description="Start a post window in this channel.")
    @app_commands.describe(
        limit="How long people have to post (e.g., 24h, 6h, 90m)",
        warn_before="Optional: warn this long before it ends (e.g., 1h, 15m)",
        lock="After time is up, make channel read-only for @everyone (or lock thread)",
        message="Closure message to post when the window ends",
        unlock_before_start="If locked from a prior window, unlock before starting",
        ping_here="Mention @here in the warning notice (default: true)",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def start(
        self,
        interaction: discord.Interaction,
        limit: str,
        warn_before: Optional[str] = None,
        lock: Optional[bool] = True,
        message: Optional[str] = "⛔ **Posting window is over. No more edits or new posts are allowed.**",
        unlock_before_start: Optional[bool] = True,
        ping_here: Optional[bool] = True,
    ):
        if not interaction.channel:
            return await interaction.response.send_message(
                "Run this in a text channel or thread.", ephemeral=True
            )
        ch = interaction.channel

        if ch.id in self.sessions and not self.sessions[ch.id].closed:
            return await interaction.response.send_message(
                "A window is already running here. Use `/postwindow stop` first.",
                ephemeral=True,
            )

        # Optionally unlock before starting (restore posting or unlock thread)
        if unlock_before_start:
            try:
                if isinstance(ch, discord.TextChannel):
                    overwrites = ch.overwrites_for(ch.guild.default_role)
                    if overwrites.send_messages is False:
                        overwrites.send_messages = None  # reset to category/server default
                        await ch.set_permissions(
                            ch.guild.default_role,
                            overwrite=overwrites,
                            reason="Auto-unlock before post window start",
                        )
                elif isinstance(ch, discord.Thread):
                    if ch.locked:
                        await ch.edit(
                            locked=False,
                            reason="Auto-unlock before post window start",
                        )
            except discord.Forbidden:
                pass

        try:
            limit_s = parse_duration(limit)
            warn_s = parse_duration(warn_before) if warn_before else None
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        now = datetime.now(UTC)
        ends = now + timedelta(seconds=limit_s)
        warn_at = (ends - timedelta(seconds=warn_s)) if warn_s and warn_s < limit_s else None

        st = WindowState(ch.id, ends, warn_at, bool(lock), bool(ping_here))
        self.sessions[ch.id] = st
        setattr(self, f"closemsg_{ch.id}", message)  # remember custom close text

        await interaction.response.send_message(
            f"✅ Started a **freeform** post window.\n"
            f"• Ends: <t:{int(ends.timestamp())}:F> (<t:{int(ends.timestamp())}:R>)\n"
            + (f"• Warn: <t:{int(warn_at.timestamp())}:R>\n" if warn_at else "")
            + (f"• Lock at end: **{st.locked}**\n")
            + (f"• Warn pings @here: **{st.ping_here}**\n")
            + (f"• Auto-unlocked before start: **{bool(unlock_before_start)}**\n")
            + f"• Close message: {message}",
        )

    @group.command(name="status", description="Show the remaining time and lock state.")
    async def status(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not ch:
            return await interaction.response.send_message(
                "Run this in a text channel or thread.", ephemeral=True
            )

        # derive live lock state regardless of session
        lock_state = "unknown"
        if isinstance(ch, discord.TextChannel):
            overw = ch.overwrites_for(ch.guild.default_role)
            lock_state = "locked" if overw.send_messages is False else "unlocked"
        elif isinstance(ch, discord.Thread):
            lock_state = "locked" if ch.locked else "unlocked"

        if ch.id not in self.sessions:
            return await interaction.response.send_message(
                f"No active post window here. (Channel is **{lock_state}**.)",
                ephemeral=True,
            )

        st = self.sessions[ch.id]
        if st.closed:
            return await interaction.response.send_message(
                f"This window is already closed. (Channel is **{lock_state}**.)",
                ephemeral=True,
            )

        emb = discord.Embed(title="Post Window Status", timestamp=datetime.now(UTC))
        emb.add_field(
            name="Ends",
            value=f"<t:{int(st.ends_at.timestamp())}:F> (<t:{int(st.ends_at.timestamp())}:R>)",
            inline=False,
        )
        if st.warn_at:
            emb.add_field(
                name="Warns", value=f"<t:{int(st.warn_at.timestamp())}:R>", inline=True
            )
        emb.add_field(name="Lock at end", value=str(st.locked), inline=True)
        emb.add_field(name="Warn pings @here", value=str(st.ping_here), inline=True)
        emb.add_field(name="Currently", value=lock_state, inline=True)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @group.command(
        name="extend",
        description="Give extra time to a specific user for this window.",
    )
    @app_commands.describe(
        user="Who gets extra time", extra="e.g., 30m, 1h", reason="Optional reason"
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def extend(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        extra: str,
        reason: Optional[str] = None,
    ):
        ch = interaction.channel
        if not ch or ch.id not in self.sessions:
            return await interaction.response.send_message(
                "No active post window here.", ephemeral=True
            )
        st = self.sessions[ch.id]
        if st.closed:
            return await interaction.response.send_message(
                "Window already closed.", ephemeral=True
            )
        try:
            secs = parse_duration(extra)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        st.extensions[user.id] = st.extensions.get(user.id, 0) + secs
        await interaction.response.send_message(
            f"⏱️ Extended <@{user.id}> by **{extra}**{f' — {reason}' if reason else ''}."
        )

    @group.command(
        name="extendall", description="Extend the current post window for everyone."
    )
    @app_commands.describe(
        extra="How much extra time to add (e.g., 30m, 1h, 2h30m)",
        reason="Optional reason to include in the notice",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def extendall(
        self, interaction: discord.Interaction, extra: str, reason: Optional[str] = None
    ):
        ch = interaction.channel
        if not ch or ch.id not in self.sessions:
            return await interaction.response.send_message(
                "No active post window here.", ephemeral=True
            )

        st = self.sessions[ch.id]
        if st.closed:
            return await interaction.response.send_message(
                "Window already closed.", ephemeral=True
            )

        try:
            secs = parse_duration(extra)
            if secs <= 0:
                raise ValueError
        except Exception:
            return await interaction.response.send_message(
                "❌ Bad duration. Use formats like `30m`, `1h`, `2h30m`.",
                ephemeral=True,
            )

        st.ends_at = st.ends_at + timedelta(seconds=secs)
        now = datetime.now(UTC)
        if st.warn_at and st.warn_at > now:
            st.warn_at = st.warn_at + timedelta(seconds=secs)

        new_end_unix = int(st.ends_at.timestamp())
        msg = f"⏩ **Window extended** by **{extra}**"
        if reason:
            msg += f" — _{reason}_"
        msg += f". New end: <t:{new_end_unix}:F> (<t:{new_end_unix}:R>)."
        await interaction.response.send_message(msg)

    @group.command(
        name="excuse",
        description="Excuse a user from needing to post this window.",
    )
    @app_commands.describe(
        user="Who is excused", until="Duration to excuse (e.g., 12h, 2d)", reason="Optional reason"
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def excuse(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        until: str,
        reason: Optional[str] = None,
    ):
        ch = interaction.channel
        if not ch or ch.id not in self.sessions:
            return await interaction.response.send_message(
                "No active post window here.", ephemeral=True
            )
        st = self.sessions[ch.id]
        if st.closed:
            return await interaction.response.send_message(
                "Window already closed.", ephemeral=True
            )
        try:
            secs = parse_duration(until)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        st.excused_until[user.id] = datetime.now(UTC) + timedelta(seconds=secs)
        await interaction.response.send_message(
            f"📝 Excused <@{user.id}> for **{until}**{f' — {reason}' if reason else ''}."
        )

    @group.command(
        name="stop", description="Stop (close) the current post window immediately."
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def stop(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not ch or ch.id not in self.sessions:
            return await interaction.response.send_message(
                "No active post window here.", ephemeral=True
            )
        await interaction.response.send_message("Stopping window now…")
        await self._close_window(ch)

    @group.command(
        name="unlock", description="Unlock this channel/thread for posting again."
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not ch:
            return await interaction.response.send_message(
                "Run this in a text channel or thread.", ephemeral=True
            )

        try:
            if isinstance(ch, discord.TextChannel):
                overwrites = ch.overwrites_for(ch.guild.default_role)
                if overwrites.send_messages is False:
                    overwrites.send_messages = None  # reset to category/server default
                    await ch.set_permissions(
                        ch.guild.default_role,
                        overwrite=overwrites,
                        reason="Manual unlock",
                    )
            elif isinstance(ch, discord.Thread):
                if ch.locked:
                    await ch.edit(locked=False, reason="Manual unlock")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⚠️ I don’t have permission to manage channel settings.",
                ephemeral=True,
            )

        await interaction.response.send_message("🔓 Channel unlocked for posting.")

    # ---------- listeners ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        st = self.sessions.get(message.channel.id)
        if not st:
            return
        now = datetime.now(UTC)

        # already closed: delete new messages from non-staff
        if st.closed:
            perms = message.channel.permissions_for(message.author)
            if not (perms.manage_messages or perms.manage_channels):
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
                return

        # still open: track last post time (informational), respect excuses
        uid = message.author.id
        if uid in st.excused_until and st.excused_until[uid] > now:
            return
        st.last_post[uid] = now

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # After closure: block edits by deleting edited message (non-staff)
        st = self.sessions.get(after.channel.id)
        if not st or not st.closed:
            return
        perms = after.channel.permissions_for(after.author)
        if perms.manage_messages or perms.manage_channels:
            return
        try:
            await after.delete()
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    # ---------- watchdog ----------
    @tasks.loop(seconds=20)
    async def watchdog(self):
        await self.bot.wait_until_ready()
        now = datetime.now(UTC)
        for ch_id, st in list(self.sessions.items()):
            if st.closed:
                continue
            ch = self.bot.get_channel(ch_id)
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                continue

            # warn once (human-friendly + optional @here)
            if st.warn_at and (now >= st.warn_at) and (now < st.ends_at):
                try:
                    remaining = max(0, int((st.ends_at - now).total_seconds()))
                    text = fmt_human_left(remaining)
                    mention = "@here " if st.ping_here else ""
                    await ch.send(
                        f"⚠️ {mention}Heads up: {text} left to post.",
                        allowed_mentions=discord.AllowedMentions(
                            everyone=True
                        ) if st.ping_here else discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
                st.warn_at = None

            # close at deadline
            if now >= st.ends_at:
                await self._close_window(ch)

    async def _close_window(self, ch: discord.abc.GuildChannel):
        st = self.sessions.get(ch.id)
        if not st or st.closed:
            return
        st.closed = True

        # 1) PR-style closure message
        close_msg = getattr(
            self,
            f"closemsg_{ch.id}",
            "⛔ **Posting window is over. No more edits or new posts are allowed.**",
        )
        try:
            await ch.send(close_msg)
        except discord.HTTPException:
            pass

        # 2) Optionally lock channel/thread (NO archiving)
        if st.locked:
            try:
                if isinstance(ch, discord.TextChannel):
                    overwrites = ch.overwrites_for(ch.guild.default_role)
                    overwrites.send_messages = False
                    await ch.set_permissions(
                        ch.guild.default_role,
                        overwrite=overwrites,
                        reason="Post window closed",
                    )
                elif isinstance(ch, discord.Thread):
                    await ch.edit(locked=True, reason="Post window closed")
            except discord.Forbidden:
                try:
                    await ch.send(
                        "⚠️ I couldn't lock this channel/thread. I need **Manage Channels** permission."
                    )
                except discord.HTTPException:
                    pass

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PostWindow(bot))
