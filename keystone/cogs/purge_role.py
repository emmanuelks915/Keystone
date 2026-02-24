from __future__ import annotations

import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)


def parse_duration(s: str) -> timedelta:
    """
    Accepts: 30m, 12h, 14d, 2w, 45s
    """
    m = _DURATION_RE.match(s or "")
    if not m:
        raise ValueError("Invalid duration. Use like 30m, 12h, 14d, 2w.")
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    raise ValueError("Invalid duration unit.")


def parse_role_mentions(s: Optional[str]) -> Set[int]:
    """
    Parse a string like: "<@&123> <@&456>" or "123 456" into role IDs.
    """
    if not s:
        return set()
    return set(int(x) for x in re.findall(r"\d{10,20}", s))


def _tracked_role_ids() -> Set[int]:
    raw = (os.getenv("TRACK_ROLE_IDS") or "").strip()
    out: Set[int] = set()
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _log_channel_id() -> Optional[int]:
    raw = (os.getenv("PURGE_LOG_CHANNEL_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _rejoin_invite_url() -> str:
    return (os.getenv("REJOIN_INVITE_URL") or "").strip()


def build_kick_dm(*, guild_name: str, role_name: str, older_than: str, invite_url: str) -> str:
    msg = (
        "Hey there! 👋\n\n"
        f"You were removed from **{guild_name}** because your **{role_name}** role exceeded the allowed time window "
        f"(**{older_than}**).\n\n"
        "This isn’t a ban, and there’s no hard feelings — we’d genuinely love to see you back once you’re ready to jump in.\n\n"
    )
    if invite_url:
        msg += f"When you’re ready, you can rejoin using this invite:\n{invite_url}\n\n"
    msg += "Hope to see you again soon 💛"
    return msg


@dataclass
class PurgePlan:
    guild_id: int
    role_id: int
    cutoff: datetime
    exception_role_ids: Set[int]
    mode: str  # "remove_role" or "kick"
    targets: List[int]  # user_ids
    older_than: str  # for DM text


class ConfirmPurgeView(discord.ui.View):
    def __init__(
        self,
        *,
        invoker_id: int,
        plan: PurgePlan,
        bot: commands.Bot,
        max_actions: int,
        force: bool,
        timeout: float = 60.0,
    ):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.plan = plan
        self.bot = bot
        self.max_actions = max_actions
        self.force = force

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the staff member who ran this command can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="✅ Confirm Purge", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable immediately so it can’t be double-clicked
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(view=self)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        # Safety cap
        targets = self.plan.targets
        if not self.force and len(targets) > self.max_actions:
            targets = targets[: self.max_actions]

        role = guild.get_role(self.plan.role_id)
        if role is None:
            await interaction.followup.send("Target role no longer exists.", ephemeral=True)
            return

        removed = 0
        kicked = 0
        skipped = 0
        dm_sent = 0
        dm_failed = 0
        failed: List[Tuple[int, str]] = []

        invite_url = _rejoin_invite_url()

        for user_id in targets:
            try:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None

            if member is None:
                skipped += 1
                continue

            # still has role?
            if role not in member.roles:
                skipped += 1
                continue

            # exception roles?
            if any(r.id in self.plan.exception_role_ids for r in member.roles):
                skipped += 1
                continue

            try:
                if self.plan.mode == "kick":
                    # DM before kicking (best-effort)
                    dm_text = build_kick_dm(
                        guild_name=guild.name,
                        role_name=role.name,
                        older_than=self.plan.older_than,
                        invite_url=invite_url,
                    )
                    try:
                        await member.send(dm_text)
                        dm_sent += 1
                    except discord.Forbidden:
                        dm_failed += 1
                    except discord.HTTPException:
                        dm_failed += 1

                    await member.kick(reason=f"Purge: had role {role.name} older than cutoff")
                    kicked += 1
                else:
                    await member.remove_roles(role, reason=f"Purge: role older than cutoff")
                    removed += 1

                # Optional: clean up role_assignments row after successful action
                sb = getattr(self.bot, "sb", None) or getattr(self.bot, "supabase", None)
                if sb:
                    try:
                        sb.table("role_assignments").delete().match(
                            {"guild_id": guild.id, "user_id": user_id, "role_id": role.id}
                        ).execute()
                    except Exception:
                        pass

            except discord.Forbidden:
                failed.append((user_id, "Forbidden (role hierarchy / missing perms)"))
            except discord.HTTPException as e:
                failed.append((user_id, f"HTTP error: {e}"))
            except Exception as e:
                failed.append((user_id, f"Error: {e}"))

            await asyncio.sleep(0.5)  # be polite to rate limits

        summary_lines = [
            "**Purge completed**",
            f"Role: <@&{role.id}>",
            f"Mode: `{self.plan.mode}`",
            f"Cutoff: <t:{int(self.plan.cutoff.timestamp())}:f>",
            f"Processed: **{len(targets)}**",
            (f"Kicked: **{kicked}**" if self.plan.mode == "kick" else f"Removed role: **{removed}**"),
            f"Skipped: **{skipped}**",
            f"Failed: **{len(failed)}**",
        ]

        if self.plan.mode == "kick":
            summary_lines.append(f"DM sent: **{dm_sent}**")
            summary_lines.append(f"DM failed: **{dm_failed}**")
            if not invite_url:
                summary_lines.append("⚠️ `REJOIN_INVITE_URL` is not set, so the DM had no invite link.")

        if failed:
            preview = "\n".join([f"- <@{uid}> — {why}" for uid, why in failed[:10]])
            summary_lines.append("\n**Failures (first 10):**\n" + preview)

        summary = "\n".join(summary_lines)

        await interaction.followup.send(summary, ephemeral=True)

        log_id = _log_channel_id()
        if log_id:
            ch = guild.get_channel(log_id)
            if isinstance(ch, discord.TextChannel):
                await ch.send(f"🧹 **Purge executed by {interaction.user.mention}**\n{summary}")

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="Canceled.", view=self)


class PurgeRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tracked = _tracked_role_ids()

    # ---------- role-age tracker ----------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not self.tracked:
            return

        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}

        added = (after_ids - before_ids) & self.tracked
        removed = (before_ids - after_ids) & self.tracked

        if not added and not removed:
            return

        sb = getattr(self.bot, "sb", None) or getattr(self.bot, "supabase", None)
        if not sb:
            return

        guild_id = after.guild.id
        user_id = after.id

        for role_id in added:
            payload = {
                "guild_id": guild_id,
                "user_id": user_id,
                "role_id": role_id,
                "assigned_at": _utcnow().isoformat(),
            }
            try:
                sb.table("role_assignments").upsert(payload).execute()
            except Exception:
                pass

        for role_id in removed:
            try:
                sb.table("role_assignments").delete().match(
                    {"guild_id": guild_id, "user_id": user_id, "role_id": role_id}
                ).execute()
            except Exception:
                pass

    # ---------- slash command ----------
    @app_commands.command(
        name="purge_role",
        description="Purge members with a role older than X, with exceptions + confirm step.",
    )
    @app_commands.describe(
        role="Role to target (ex: @No OC)",
        older_than="How old the role assignment must be (ex: 14d, 12h, 30m)",
        exceptions="Roles to EXCLUDE (paste mentions or IDs, separated by spaces)",
        mode="remove_role (default) or kick",
        dry_run="If true, shows preview + confirmation buttons (recommended)",
        max_actions="Safety cap per run (default 25)",
        force="If true, ignores max_actions cap",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="remove_role", value="remove_role"),
            app_commands.Choice(name="kick", value="kick"),
        ]
    )
    async def purge_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        older_than: str,
        exceptions: Optional[str] = None,
        mode: app_commands.Choice[str] = None,
        dry_run: bool = True,
        max_actions: int = 25,
        force: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        perms = interaction.user.guild_permissions if interaction.guild else None
        chosen_mode = (mode.value if mode else "remove_role")

        if not perms or not (perms.manage_roles or perms.administrator):
            await interaction.followup.send("You need **Manage Roles** to use this command.", ephemeral=True)
            return

        if chosen_mode == "kick" and not (perms.kick_members or perms.administrator):
            await interaction.followup.send("You need **Kick Members** to use `mode=kick`.", ephemeral=True)
            return

        try:
            delta = parse_duration(older_than)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        cutoff = _utcnow() - delta
        exception_ids = parse_role_mentions(exceptions)

        if role.id not in self.tracked:
            await interaction.followup.send(
                "That role isn’t being tracked for age.\n"
                "Add it to `TRACK_ROLE_IDS` and re-deploy.\n"
                f"(Role ID: `{role.id}`)",
                ephemeral=True,
            )
            return

        sb = getattr(self.bot, "sb", None) or getattr(self.bot, "supabase", None)
        if not sb:
            await interaction.followup.send("Supabase client not found on bot (bot.sb or bot.supabase).", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        try:
            res = (
                sb.table("role_assignments")
                .select("user_id, assigned_at")
                .eq("guild_id", guild.id)
                .eq("role_id", role.id)
                .lte("assigned_at", cutoff.isoformat())
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            await interaction.followup.send(f"Supabase query failed: `{e}`", ephemeral=True)
            return

        target_ids: List[int] = []
        for row in rows:
            try:
                uid = int(row["user_id"])
            except Exception:
                continue

            member = guild.get_member(uid)
            if member is None:
                target_ids.append(uid)
                continue

            if role not in member.roles:
                continue

            if any(r.id in exception_ids for r in member.roles):
                continue

            target_ids.append(uid)

        effective_ids = target_ids if (force or len(target_ids) <= max_actions) else target_ids[:max_actions]
        capped_note = ""
        if not force and len(target_ids) > max_actions:
            capped_note = (
                f"\n⚠️ Safety cap: showing first **{max_actions}** of **{len(target_ids)}**. "
                "Use `force=true` to run all."
            )

        preview_count = min(20, len(effective_ids))
        preview_mentions = " ".join([f"<@{uid}>" for uid in effective_ids[:preview_count]])
        more = ""
        if len(effective_ids) > preview_count:
            more = f"\n…and **{len(effective_ids) - preview_count}** more."

        exception_text = " ".join([f"<@&{rid}>" for rid in exception_ids]) if exception_ids else "None"

        msg = (
            "**Purge Preview**\n"
            f"Role: <@&{role.id}>\n"
            f"Older than: `{older_than}` (cutoff <t:{int(cutoff.timestamp())}:f>)\n"
            f"Exceptions: {exception_text}\n"
            f"Mode: `{chosen_mode}`\n"
            f"Candidates found: **{len(target_ids)}**\n"
            f"Will process: **{len(effective_ids)}**"
            f"{capped_note}\n\n"
            f"**Preview (first {preview_count}):**\n{preview_mentions}{more}"
        )

        if not target_ids:
            await interaction.followup.send(msg + "\n\nNo one matches — nothing to do.", ephemeral=True)
            return

        plan = PurgePlan(
            guild_id=guild.id,
            role_id=role.id,
            cutoff=cutoff,
            exception_role_ids=exception_ids,
            mode=chosen_mode,
            targets=target_ids,
            older_than=older_than,
        )

        view = ConfirmPurgeView(
            invoker_id=interaction.user.id,
            plan=plan,
            bot=self.bot,
            max_actions=max_actions,
            force=force,
            timeout=60.0,
        )

        # Always require the confirm UI for safety (even if dry_run=false)
        if dry_run:
            await interaction.followup.send(msg + "\n\n**ARE YOU SURE?** Confirm below:", ephemeral=True, view=view)
        else:
            await interaction.followup.send(
                msg + "\n\nConfirmation was disabled, but you still must click confirm to proceed:",
                ephemeral=True,
                view=view,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(PurgeRoleCog(bot))
