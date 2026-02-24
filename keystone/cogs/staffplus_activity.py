from __future__ import annotations

import re
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
from typing import Optional

# =========================
# CONFIG (Skyfall)
# =========================
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

TUPPER_LOG_CHANNEL_ID = 1374922778012160100
STAFF_ROLE_ID = 1374730886490357822
AUDIT_LOG_CHANNEL_ID = 1400633367308800090

ACTIVE_DAYS = 14
NEAR_INACTIVE_DAYS = 21

# =========================
# Helpers
# =========================

MENTION_RE = re.compile(r"<@!?(\d{15,20})>")

CANDIDATE_ID_PATTERNS = [
    # Tupperbox embed field style:
    # "Registered by @Name (1234567890)"
    re.compile(r"Registered by\s+.*?\((\d{15,20})\)", re.IGNORECASE),

    re.compile(r"(?:User|Author|Sender)\s*:\s*<@!?(\d{15,20})>", re.IGNORECASE),
    re.compile(r"(?:User|Author|Sender)\s*:\s*(\d{15,20})", re.IGNORECASE),
    re.compile(r"(?:UserID|AuthorID|SenderID)\s*[:=]\s*(\d{15,20})", re.IGNORECASE),
    re.compile(r"\bID\s*[:=]\s*(\d{15,20})\b", re.IGNORECASE),
]

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def _parse_iso(s: str) -> Optional[datetime.datetime]:
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None

def extract_real_user_id(message: discord.Message) -> Optional[int]:
    """Extract the real Discord user ID from a Tupperbox log message."""
    content = message.content or ""

    # 1) explicit patterns in content
    for pat in CANDIDATE_ID_PATTERNS:
        m = pat.search(content)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    # 2) mention fallback in content
    m2 = MENTION_RE.search(content)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            pass

    # 3) embeds (description/footer/fields)
    for emb in message.embeds:
        if emb.description:
            for pat in CANDIDATE_ID_PATTERNS:
                m = pat.search(emb.description)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        pass
            md = MENTION_RE.search(emb.description)
            if md:
                try:
                    return int(md.group(1))
                except Exception:
                    pass

        if emb.footer and emb.footer.text:
            ft = emb.footer.text
            for pat in CANDIDATE_ID_PATTERNS:
                m = pat.search(ft)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        pass

        for f in emb.fields:
            blob = f"{f.name}\n{f.value}"
            for pat in CANDIDATE_ID_PATTERNS:
                m = pat.search(blob)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        pass
            mf = MENTION_RE.search(blob)
            if mf:
                try:
                    return int(mf.group(1))
                except Exception:
                    pass

    return None


class StaffPlusActivity(commands.Cog):
    """
    Tangerine Staff+ Phase 1:
    - Listen to Tupperbox log channel
    - Update rp_activity table with last_rp_at for real users
    - Weekly staff report + manual report command
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_activity_report.start()

    def cog_unload(self):
        self.weekly_activity_report.cancel()

    # -------------------------
    # Listener
    # -------------------------
    @commands.Cog.listener("on_message")
    async def on_message_activity(self, message: discord.Message):
        if not message.guild:
            return
        if message.guild.id != SKYFALL_GUILD_ID:
            return
        if message.channel.id != TUPPER_LOG_CHANNEL_ID:
            return

        # Only ignore OUR bot messages — NOT Tupperbox logs
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        real_user_id = extract_real_user_id(message)
        if not real_user_id:
            await self._audit(
                f"⚠️ **Staff+ Activity:** Could not extract real user from a Tupper log message.\n"
                f"Message: {message.jump_url}"
            )
            return

        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            await self._audit("❌ **Staff+ Activity:** Supabase client not attached to bot.")
            return

        now = _utcnow()

        try:
            supabase.table("rp_activity").upsert({
                "discord_id": real_user_id,
                "last_rp_at": now.isoformat(),
                "last_source_message_id": message.id,
                "last_source_channel_id": message.channel.id,
                "updated_at": now.isoformat()
            }).execute()
        except Exception as e:
            await self._audit(f"❌ **Staff+ Activity:** Supabase write failed: `{type(e).__name__}: {e}`")

    # -------------------------
    # Staff Commands (PUBLIC)
    # -------------------------
    @app_commands.command(name="activity_report", description="Post a staff RP activity report (based on Tupper logs).")
    @app_commands.checks.has_role(STAFF_ROLE_ID)
    async def activity_report(self, interaction: discord.Interaction):
        await interaction.response.defer()  # public
        text = await self._build_activity_report()
        await interaction.followup.send(text)

    @app_commands.command(name="activity_lookup", description="Look up a user's last recorded RP activity.")
    @app_commands.checks.has_role(STAFF_ROLE_ID)
    async def activity_lookup(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()  # public

        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            await interaction.followup.send("❌ Supabase is not attached to the bot.")
            return

        try:
            res = supabase.table("rp_activity").select("*").eq("discord_id", user.id).limit(1).execute()
            if not res.data:
                await interaction.followup.send(f"ℹ️ No RP activity recorded yet for {user.mention}.")
                return

            row = res.data[0]
            last_rp_at = row.get("last_rp_at")
            msg_id = row.get("last_source_message_id")
            ch_id = row.get("last_source_channel_id")

            details = f"🍊 **RP Activity Lookup**\nPlayer: {user.mention}\n"

            if last_rp_at:
                dt = _parse_iso(last_rp_at)
                if dt:
                    unix_ts = int(dt.timestamp())
                    details += f"Last RP Activity: <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
                else:
                    details += f"Last RP Activity: {last_rp_at}\n"
            else:
                details += "Last RP Activity: _Unknown_\n"

            if ch_id and msg_id:
                details += f"Source: <#{ch_id}> (message id `{msg_id}`)\n"

            await interaction.followup.send(details)

        except Exception as e:
            await interaction.followup.send(f"❌ Lookup failed: `{type(e).__name__}: {e}`")

    # -------------------------
    # Scheduled weekly report
    # -------------------------
    @tasks.loop(hours=24)
    async def weekly_activity_report(self):
        if not self.bot.is_ready():
            return

        now = _utcnow()

        # Monday only
        if now.weekday() != 0:
            return

        # Soft time gate: 14:00 UTC
        if now.hour != 14:
            return

        text = await self._build_activity_report()
        await self._audit(text)

    @weekly_activity_report.before_loop
    async def before_weekly_report(self):
        await self.bot.wait_until_ready()

    # -------------------------
    # Report builder
    # -------------------------
    async def _build_activity_report(self) -> str:
        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            return "❌ **Staff+ Activity Report:** Supabase client not attached."

        now = _utcnow()
        active_cutoff = now - datetime.timedelta(days=ACTIVE_DAYS)
        near_cutoff = now - datetime.timedelta(days=NEAR_INACTIVE_DAYS)

        try:
            res = (
                supabase.table("rp_activity")
                .select("discord_id,last_rp_at")
                .order("last_rp_at", desc=True)
                .limit(2000)
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            return f"❌ **Staff+ Activity Report:** Failed to fetch: `{type(e).__name__}: {e}`"

        active, near, inactive = [], [], []

        for r in rows:
            uid = r.get("discord_id")
            ts = r.get("last_rp_at")
            if not uid or not ts:
                continue

            dt = _parse_iso(ts)
            if not dt:
                continue

            if dt >= active_cutoff:
                active.append(uid)
            elif dt >= near_cutoff:
                near.append(uid)
            else:
                inactive.append(uid)

        def fmt_list(uids, max_show=25):
            if not uids:
                return "_None_"
            shown = uids[:max_show]
            more = len(uids) - len(shown)
            base = " ".join(f"<@{u}>" for u in shown)
            if more > 0:
                base += f" … (+{more} more)"
            return base

        return (
            f"🍊 **Tangerine Staff+ | RP Activity Report**\n"
            f"Window: **Active ≤ {ACTIVE_DAYS}d**, Near-inactive: **{ACTIVE_DAYS+1}–{NEAR_INACTIVE_DAYS}d**, Inactive: **>{NEAR_INACTIVE_DAYS}d**\n"
            f"Generated: <t:{int(now.timestamp())}:F>\n\n"
            f"✅ **Active ({len(active)}):** {fmt_list(active)}\n\n"
            f"⚠️ **Near Inactive ({len(near)}):** {fmt_list(near)}\n\n"
            f"❌ **Inactive ({len(inactive)}):** {fmt_list(inactive)}"
        )

    # -------------------------
    # Audit log helper
    # -------------------------
    async def _audit(self, content: str):
        ch = self.bot.get_channel(AUDIT_LOG_CHANNEL_ID)
        if ch and isinstance(ch, discord.TextChannel):
            await ch.send(content)
        else:
            print(content)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffPlusActivity(bot), guild=SKYFALL_GUILD)
