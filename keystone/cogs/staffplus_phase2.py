from __future__ import annotations

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

STAFF_ROLE_ID = 1374730886490357822
AUDIT_LOG_CHANNEL_ID = 1400633367308800090  # staff pings + logs

# Deadline behavior
DEFAULT_OC_SHEET_DUE_DAYS = 3
REMINDER_LOOP_MINUTES = 5

# Reminder schedule before due
REMIND_OFFSETS = [
    datetime.timedelta(days=2),
    datetime.timedelta(days=1),
    datetime.timedelta(hours=6),
    datetime.timedelta(hours=2),
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

def _discord_ts(dt: datetime.datetime) -> str:
    u = int(dt.timestamp())
    return f"<t:{u}:F> (<t:{u}:R>)"

def _next_remind_at(due_at: datetime.datetime, stage: int) -> Optional[datetime.datetime]:
    if stage < len(REMIND_OFFSETS):
        return due_at - REMIND_OFFSETS[stage]
    return None


class StaffPlusPhase2(commands.Cog):
    """
    Tangerine Staff+ Phase 2:
    - Deadlines + reminders
    - Extension requests
    - Overdue enforcement for grimoire claims (downgrade to Simple)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.deadline_loop.start()

    def cog_unload(self):
        self.deadline_loop.cancel()

    async def _audit(self, content: str):
        ch = self.bot.get_channel(AUDIT_LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(content)
        else:
            print(content)

    # =========================
    # HOOK: called by grimoire cog on CLAIM
    # =========================
    async def hook_grimoire_claim_deadline(
        self,
        interaction: discord.Interaction,
        claim_id: str,
        grimoire_type: str,
        oc_slot: int,
        due_days: int = DEFAULT_OC_SHEET_DUE_DAYS,
    ):
        """
        Creates a 3-day deadline + staff queue item for any non-Simple claimed grimoire.
        Stores source_type/source_id so we can enforce downgrade later.
        """
        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            await self._audit("❌ **Staff+ Phase 2:** Supabase not attached to bot.")
            return

        now = _utcnow()
        due_at = now + datetime.timedelta(days=due_days)
        next_remind = _next_remind_at(due_at, 0)

        # deadline row
        try:
            supabase.table("staffplus_deadlines").insert({
                "guild_id": interaction.guild_id or SKYFALL_GUILD_ID,
                "user_id": interaction.user.id,
                "created_by": interaction.user.id,
                "title": "OC Sheet Submission Required",
                "details": f"Claimed **{grimoire_type}** grimoire (OC Slot {oc_slot}). Must submit OC sheet within {due_days} days or it downgrades to **Simple**.",
                "due_at": due_at.isoformat(),
                "status": "open",
                "remind_stage": 0,
                "next_remind_at": next_remind.isoformat() if next_remind else None,
                "source_type": "grimoire_claim",
                "source_id": claim_id,
                "updated_at": now.isoformat(),
            }).execute()
        except Exception as e:
            await self._audit(f"❌ **Staff+ Phase 2:** Failed to create deadline: `{type(e).__name__}: {e}`")
            return

        # queue row
        try:
            supabase.table("staffplus_queue").insert({
                "guild_id": interaction.guild_id or SKYFALL_GUILD_ID,
                "type": "grimoire_oc_sheet",
                "subject_user_id": interaction.user.id,
                "created_by": interaction.user.id,
                "assigned_role_id": STAFF_ROLE_ID,
                "title": f"OC sheet due for {interaction.user.display_name} (Slot {oc_slot}) - {grimoire_type}",
                "link": interaction.message.jump_url if interaction.message else None,
                "status": "pending",
                "nag_stage": 0,
                "next_nag_at": (now + datetime.timedelta(hours=72)).isoformat(),
                "updated_at": now.isoformat(),
            }).execute()
        except Exception as e:
            await self._audit(f"⚠️ **Staff+ Phase 2:** Queue insert failed (deadline still created): `{type(e).__name__}: {e}`")

        # staff ping (one-time, informational)
        await self._audit(
            f"📘 **OC Sheet Deadline Created** <@&{STAFF_ROLE_ID}>\n"
            f"Player: {interaction.user.mention}\n"
            f"Claim: **{grimoire_type}** (Slot {oc_slot})\n"
            f"Due: {_discord_ts(due_at)}\n"
            f"Claim ID: `{claim_id}`"
        )

        # player message (ephemeral since claim is ephemeral)
        try:
            await interaction.followup.send(
                f"⏳ **OC Sheet Deadline Started**\n"
                f"You claimed **{grimoire_type}**.\n"
                f"Submit your OC sheet by {_discord_ts(due_at)} or your grimoire will be downgraded to **Simple**.\n"
                f"If you need more time, use `/deadline_extension_request`.",
                ephemeral=True,
            )
        except Exception:
            pass

    # =========================
    # STAFF COMMANDS (PUBLIC, staff-only)
    # =========================
    @app_commands.command(name="deadline_extend", description="Extend a deadline by X days/hours (staff).")
    @app_commands.checks.has_role(STAFF_ROLE_ID)
    async def deadline_extend(self, interaction: discord.Interaction, deadline_id: str, add_days: int, reason: Optional[str] = None):
        await interaction.response.defer()  # public
        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            return await interaction.followup.send("❌ Supabase is not attached.")

        now = _utcnow()
        try:
            row = supabase.table("staffplus_deadlines").select("*").eq("id", deadline_id).limit(1).execute()
            if not row.data:
                return await interaction.followup.send("❌ Deadline not found.")

            d = row.data[0]
            due_at = _parse_iso(d["due_at"])
            if not due_at:
                return await interaction.followup.send("❌ Deadline has invalid due date.")

            new_due = due_at + datetime.timedelta(days=add_days)
            next_remind = _next_remind_at(new_due, int(d.get("remind_stage") or 0))  # keep current stage
            supabase.table("staffplus_deadlines").update({
                "due_at": new_due.isoformat(),
                "next_remind_at": next_remind.isoformat() if next_remind else None,
                "updated_at": now.isoformat(),
                "details": (d.get("details") or "") + (f"\nExtension: +{add_days}d by <@{interaction.user.id}>. {reason or ''}".strip()),
            }).eq("id", deadline_id).execute()

            await interaction.followup.send(f"✅ Extended deadline `{deadline_id}` to {_discord_ts(new_due)}.")
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: `{type(e).__name__}: {e}`")

    @app_commands.command(name="ocsheet_submit", description="Mark OC sheet submitted (completes deadline + resolves queue).")
    @app_commands.checks.has_role(STAFF_ROLE_ID)
    async def ocsheet_submit(self, interaction: discord.Interaction, deadline_id: str, link: Optional[str] = None):
        await interaction.response.defer()  # public
        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            return await interaction.followup.send("❌ Supabase is not attached.")

        now = _utcnow()
        try:
            row = supabase.table("staffplus_deadlines").select("*").eq("id", deadline_id).limit(1).execute()
            if not row.data:
                return await interaction.followup.send("❌ Deadline not found.")

            d = row.data[0]
            if d.get("status") != "open":
                return await interaction.followup.send("⚠️ Deadline is not open anymore.")

            supabase.table("staffplus_deadlines").update({
                "status": "completed",
                "completed_at": now.isoformat(),
                "completion_link": link,
                "updated_at": now.isoformat(),
            }).eq("id", deadline_id).execute()

            # resolve matching queue items for this user/type
            uid = int(d["user_id"])
            supabase.table("staffplus_queue").update({
                "status": "resolved",
                "resolved_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }).eq("status", "pending").eq("type", "grimoire_oc_sheet").eq("subject_user_id", uid).execute()

            await interaction.followup.send(
                f"✅ OC sheet marked submitted for <@{uid}>.\n"
                + (f"Link: {link}" if link else "")
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: `{type(e).__name__}: {e}`")

    # =========================
    # PLAYER COMMAND: Extension request (PUBLIC)
    # =========================
    @app_commands.command(name="deadline_extension_request", description="Request an extension on your OC sheet deadline.")
    @app_commands.guilds(SKYFALL_GUILD)
    async def deadline_extension_request(self, interaction: discord.Interaction, reason: str):
        await interaction.response.defer()  # public

        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            return await interaction.followup.send("❌ Bot is missing Supabase connection.")

        now = _utcnow()
        # find latest OPEN oc-sheet deadline for this user
        try:
            res = (
                supabase.table("staffplus_deadlines")
                .select("*")
                .eq("status", "open")
                .eq("user_id", interaction.user.id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if not res.data:
                return await interaction.followup.send("ℹ️ You don’t have an active OC-sheet deadline right now.")

            d = res.data[0]
            did = d["id"]
            due = _parse_iso(d["due_at"])
            due_str = _discord_ts(due) if due else d["due_at"]

            # queue item for staff
            supabase.table("staffplus_queue").insert({
                "guild_id": interaction.guild_id or SKYFALL_GUILD_ID,
                "type": "extension_request",
                "subject_user_id": interaction.user.id,
                "created_by": interaction.user.id,
                "assigned_role_id": STAFF_ROLE_ID,
                "title": f"Extension request for OC sheet deadline ({interaction.user.display_name})",
                "link": None,
                "status": "pending",
                "nag_stage": 0,
                "next_nag_at": (now + datetime.timedelta(hours=24)).isoformat(),
                "updated_at": now.isoformat(),
            }).execute()

            await self._audit(
                f"🕒 **Extension Requested** <@&{STAFF_ROLE_ID}>\n"
                f"Player: {interaction.user.mention}\n"
                f"Deadline ID: `{did}`\n"
                f"Current Due: {due_str}\n"
                f"Reason: {reason}"
            )

            await interaction.followup.send(
                f"✅ Extension request sent to staff.\n"
                f"Your current due date is {due_str}.\n"
                f"Deadline ID: `{did}`"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: `{type(e).__name__}: {e}`")

    # =========================
    # BACKGROUND: Reminders + enforcement
    # =========================
    @tasks.loop(minutes=REMINDER_LOOP_MINUTES)
    async def deadline_loop(self):
        if not self.bot.is_ready():
            return

        supabase = getattr(self.bot, "supabase", None)
        if not supabase:
            return

        now = _utcnow()

        try:
            res = (
                supabase.table("staffplus_deadlines")
                .select("*")
                .eq("status", "open")
                .order("due_at", desc=False)
                .limit(2000)
                .execute()
            )
            rows = res.data or []
        except Exception:
            return

        for d in rows:
            did = d.get("id")
            uid = d.get("user_id")
            due_at = _parse_iso(d.get("due_at") or "")
            if not did or not uid or not due_at:
                continue

            stage = int(d.get("remind_stage") or 0)
            next_remind_at = _parse_iso(d.get("next_remind_at") or "")
            is_overdue = now >= due_at

            # --- Reminders ---
            should_remind = False
            if next_remind_at and now >= next_remind_at:
                should_remind = True
            elif (next_remind_at is None) and is_overdue:
                # overdue: remind daily
                last = _parse_iso(d.get("last_reminded_at") or "")
                if not last or (now - last) >= datetime.timedelta(hours=24):
                    should_remind = True

            if should_remind:
                # DM first; if DMs closed, we just ping staff instead (keeps player from being spammed publicly)
                user = self.bot.get_user(int(uid))
                msg = (
                    f"⏳ **Deadline Reminder**\n"
                    f"**{d.get('title','Deadline')}**\n"
                    f"Due: {_discord_ts(due_at)}\n"
                    f"{d.get('details','')}"
                )

                dm_ok = False
                if user:
                    try:
                        await user.send(msg)
                        dm_ok = True
                    except Exception:
                        dm_ok = False

                if not dm_ok:
                    await self._audit(
                        f"⚠️ Could not DM <@{uid}> a deadline reminder (DMs closed?).\n"
                        f"Deadline `{did}` due {_discord_ts(due_at)}"
                    )

                new_stage = stage + (0 if is_overdue else 1)
                new_next = None if is_overdue else _next_remind_at(due_at, new_stage)

                try:
                    supabase.table("staffplus_deadlines").update({
                        "remind_stage": new_stage if not is_overdue else stage,
                        "last_reminded_at": now.isoformat(),
                        "next_remind_at": new_next.isoformat() if new_next else None,
                        "updated_at": now.isoformat(),
                    }).eq("id", did).execute()
                except Exception:
                    pass

            # --- Enforcement: downgrade grimoire if overdue and linked to grimoire_claim ---
            if is_overdue and not d.get("escalated_at") and d.get("source_type") == "grimoire_claim" and d.get("source_id"):
                claim_id = d["source_id"]

                try:
                    # downgrade claim row to Simple
                    supabase.table("grimoire_claims").update({
                        "selected_grimoire": "Simple",
                        "updated_at": now.isoformat()  # if column doesn't exist, harmless; supabase will error, caught
                    }).eq("id", claim_id).execute()
                except Exception:
                    # some schemas won't have updated_at; that's fine, try without it
                    try:
                        supabase.table("grimoire_claims").update({
                            "selected_grimoire": "Simple"
                        }).eq("id", claim_id).execute()
                    except Exception as e:
                        await self._audit(f"❌ Downgrade failed for claim `{claim_id}`: `{type(e).__name__}: {e}`")

                # mark escalated so we don't repeatedly downgrade
                try:
                    supabase.table("staffplus_deadlines").update({
                        "escalated_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                        "details": (d.get("details") or "") + "\n⚠️ Auto-enforced: deadline missed, grimoire downgraded to Simple."
                    }).eq("id", did).execute()
                except Exception:
                    pass

                await self._audit(
                    f"🚨 **OC Sheet Deadline Missed — Auto Downgrade Applied** <@&{STAFF_ROLE_ID}>\n"
                    f"Player: <@{uid}>\n"
                    f"Deadline `{did}` was due {_discord_ts(due_at)}\n"
                    f"Claim ID: `{claim_id}`\n"
                    f"Action: `grimoire_claims.selected_grimoire` → **Simple**"
                )

    @deadline_loop.before_loop
    async def before_deadline_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffPlusPhase2(bot), guild=SKYFALL_GUILD)
