from __future__ import annotations

import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

UTC = timezone.utc

DEFAULT_CLOSE_MESSAGE = "⛔ **Posting window is over. No more edits or new posts are allowed.**"

PING_MODE_CHOICES = [
    app_commands.Choice(name="Ping missing only", value="missing"),
    app_commands.Choice(name="Ping @here", value="here"),
    app_commands.Choice(name="No ping", value="none"),
]

TRACKING_MODE_CHOICES = [
    app_commands.Choice(name="Per OC", value="oc"),
    app_commands.Choice(name="Per player", value="user"),
]

ROSTER_MODE_CHOICES = [
    app_commands.Choice(name="Manual roster", value="manual"),
    app_commands.Choice(name="Role roster", value="role"),
    app_commands.Choice(name="Thread member roster", value="thread_members"),
]


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Supabase/PostgREST usually returns ISO. This fallback keeps a bad row
        # from killing the watchdog loop.
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_duration(s: str) -> int:
    """Parse '24h', '90m', '1h30m', '30m15s', or '2d' into seconds."""
    s = (s or "").strip().lower()
    if not s:
        raise ValueError("Provide a duration, e.g., `24h`, `90m`, `1h30m`, `30m15s`, `2d`.")

    total = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
            continue
        if ch in "dhms" and num:
            n = int(num)
            if ch == "d":
                total += n * 86400
            elif ch == "h":
                total += n * 3600
            elif ch == "m":
                total += n * 60
            elif ch == "s":
                total += n
            num = ""
        elif ch.isspace():
            continue
        else:
            raise ValueError("Use formats like `24h`, `90m`, `1h30m`, `30m15s`, or `2d`.")

    if num:
        # Bare number = seconds. This keeps the old behavior.
        total += int(num)

    if total <= 0:
        raise ValueError("Duration must be greater than 0.")
    return total


def fmt_human_left(seconds: int) -> str:
    if seconds <= 0:
        return "no time"
    mins_total = (seconds + 59) // 60
    days = mins_total // (60 * 24)
    rem = mins_total % (60 * 24)
    hours = rem // 60
    mins = rem % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins and not days:
        parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    if not parts:
        return "less than a minute"
    return "about " + " ".join(parts)


def chunk_lines(lines: list[str], *, max_chars: int = 950, max_lines: int = 20) -> str:
    if not lines:
        return "None."

    out: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + 1
        if len(out) >= max_lines or used + extra > max_chars:
            remaining = len(lines) - len(out)
            out.append(f"…and {remaining} more.")
            break
        out.append(line)
        used += extra
    return "\n".join(out)


class PostWindow(commands.Cog):
    """Persistent per-OC post windows with targeted missing-poster reminders."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watchdog.start()

    def cog_unload(self):
        self.watchdog.cancel()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    group = app_commands.Group(name="postwindow", description="RP post windows and missing-poster reminders")

    # ------------------------------------------------------------------
    # Interaction helpers
    # ------------------------------------------------------------------
    async def _send(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        return await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    async def _defer(self, interaction: discord.Interaction, *, ephemeral: bool = False):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)

    # ------------------------------------------------------------------
    # Character helpers
    # ------------------------------------------------------------------
    def _get_user_characters(self, user_id: int) -> list[dict[str, Any]]:
        res = (
            self.sb()
            .table("characters")
            .select("character_id,name,is_active,user_id")
            .eq("user_id", int(user_id))
            .order("name")
            .execute()
        )
        return res.data or []

    def _get_active_character(self, user_id: int) -> dict[str, Any] | None:
        rows = self._get_user_characters(user_id)
        active = [r for r in rows if bool(r.get("is_active"))]
        if active:
            return active[0]
        if len(rows) == 1:
            return rows[0]
        return None

    def _find_character_for_user(self, user_id: int, oc_name: str | None) -> tuple[dict[str, Any] | None, str | None]:
        rows = self._get_user_characters(user_id)
        if not rows:
            return None, "That player does not have any OCs in the `characters` table yet."

        raw = (oc_name or "").strip()
        if not raw:
            active = [r for r in rows if bool(r.get("is_active"))]
            if active:
                return active[0], None
            if len(rows) == 1:
                return rows[0], None
            names = ", ".join(str(r.get("name")) for r in rows[:10])
            return None, f"That player has multiple OCs. Please provide `oc_name`. Options: {names}"

        exact = [r for r in rows if str(r.get("name") or "").casefold() == raw.casefold()]
        if exact:
            return exact[0], None

        starts = [r for r in rows if str(r.get("name") or "").casefold().startswith(raw.casefold())]
        if len(starts) == 1:
            return starts[0], None
        if starts:
            names = ", ".join(str(r.get("name")) for r in starts[:10])
            return None, f"More than one OC matched `{raw}`. Try one of: {names}"

        names = ", ".join(str(r.get("name")) for r in rows[:10])
        return None, f"I couldn’t find `{raw}` for that player. Their OCs: {names}"

    def _autocomplete_target_user_id(self, interaction: discord.Interaction) -> int:
        """
        Discord autocomplete does not always hydrate the selected `user` option
        into a discord.Member object. Sometimes it is a raw snowflake/string,
        and sometimes it only appears inside interaction.data/resolved.

        This helper makes `/postwindow add user:@Someone oc_name:...` show
        @Someone's OCs instead of defaulting back to the staff member's OCs.
        """
        target_user = getattr(interaction.namespace, "user", None)

        if isinstance(target_user, (discord.Member, discord.User)):
            return int(target_user.id)

        if target_user is not None:
            raw = str(target_user).strip()
            if raw.isdigit():
                return int(raw)

        try:
            data = interaction.data or {}
            options = data.get("options") or []

            def walk(opts: list[dict[str, Any]]) -> int | None:
                for opt in opts:
                    if not isinstance(opt, dict):
                        continue
                    if opt.get("name") == "user" and opt.get("value") is not None:
                        raw_value = str(opt.get("value")).strip()
                        if raw_value.isdigit():
                            return int(raw_value)
                    nested = opt.get("options")
                    if isinstance(nested, list):
                        found = walk(nested)
                        if found is not None:
                            return found
                return None

            found_id = walk(options)
            if found_id is not None:
                return found_id
        except Exception:
            pass

        return int(interaction.user.id)

    async def oc_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            user_id = self._autocomplete_target_user_id(interaction)
            q = (current or "").strip().casefold()
            rows = self._get_user_characters(user_id)
            out: list[app_commands.Choice[str]] = []
            for row in rows:
                name = str(row.get("name") or "")
                if q and q not in name.casefold():
                    continue
                label = f"{name}{' ⭐' if row.get('is_active') else ''}"
                out.append(app_commands.Choice(name=label[:100], value=name[:100]))
                if len(out) >= 25:
                    break
            return out
        except Exception:
            traceback.print_exc()
            return []

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _get_open_session(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        res = (
            self.sb()
            .table("postwindow_sessions")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("channel_id", int(channel_id))
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    def _get_latest_session(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        res = (
            self.sb()
            .table("postwindow_sessions")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("channel_id", int(channel_id))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    def _get_participants(self, session_id: str) -> list[dict[str, Any]]:
        res = (
            self.sb()
            .table("postwindow_participants")
            .select("*")
            .eq("session_id", session_id)
            .order("created_at")
            .execute()
        )
        return res.data or []

    def _participant_exists(self, session_id: str, user_id: int, character_id: str | None) -> bool:
        rows = (
            self.sb()
            .table("postwindow_participants")
            .select("participant_id,character_id")
            .eq("session_id", session_id)
            .eq("user_id", int(user_id))
            .execute()
            .data
            or []
        )
        wanted = str(character_id) if character_id else None
        for row in rows:
            got = row.get("character_id")
            got_s = str(got) if got else None
            if got_s == wanted:
                return True
        return False

    def _add_participant(
        self,
        *,
        session: dict[str, Any],
        user_id: int,
        character_id: str | None,
        character_name: str | None,
        added_by: int | None = None,
    ) -> bool:
        session_id = str(session["session_id"])
        if self._participant_exists(session_id, user_id, character_id):
            # If they were removed before, bring them back.
            update = {"status": "pending", "updated_at": to_iso(now_utc())}
            q = (
                self.sb()
                .table("postwindow_participants")
                .update(update)
                .eq("session_id", session_id)
                .eq("user_id", int(user_id))
            )
            if character_id:
                q = q.eq("character_id", character_id)
            else:
                q = q.is_("character_id", "null")
            q.execute()
            return False

        self.sb().table("postwindow_participants").insert(
            {
                "session_id": session_id,
                "guild_id": int(session["guild_id"]),
                "user_id": int(user_id),
                "character_id": character_id,
                "character_name": character_name,
                "status": "pending",
                "added_by": int(added_by) if added_by else None,
            }
        ).execute()
        return True

    def _participant_deadline(self, session: dict[str, Any], participant: dict[str, Any]) -> datetime:
        base = parse_ts(session.get("ends_at")) or now_utc()
        extra = int(participant.get("extension_seconds") or 0)
        return base + timedelta(seconds=extra)

    def _missing_participants(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        now = now_utc()
        out: list[dict[str, Any]] = []
        for p in self._get_participants(str(session["session_id"])):
            status = str(p.get("status") or "pending")
            if status != "pending":
                continue
            excused_until = parse_ts(p.get("excused_until"))
            if excused_until and excused_until > now:
                continue
            out.append(p)
        return out

    async def _seed_members(
        self,
        *,
        session: dict[str, Any],
        members: Iterable[discord.Member],
        added_by: int,
    ) -> tuple[int, list[str]]:
        added = 0
        skipped: list[str] = []
        tracking_mode = str(session.get("tracking_mode") or "oc")

        for member in members:
            if member.bot:
                continue

            character_id: str | None = None
            character_name: str | None = None

            if tracking_mode == "oc":
                char = self._get_active_character(int(member.id))
                if not char:
                    skipped.append(member.mention)
                    continue
                character_id = str(char["character_id"])
                character_name = str(char.get("name") or "Unknown OC")

            created = self._add_participant(
                session=session,
                user_id=int(member.id),
                character_id=character_id,
                character_name=character_name,
                added_by=added_by,
            )
            if created:
                added += 1

        return added, skipped

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    def _participant_label(self, participant: dict[str, Any], *, include_status: bool = False) -> str:
        status = str(participant.get("status") or "pending")
        icon = {
            "pending": "⏳",
            "posted": "✅",
            "excused": "📝",
            "removed": "➖",
        }.get(status, "•")
        user = f"<@{int(participant['user_id'])}>"
        oc = participant.get("character_name")
        label = f"{user}"
        if oc:
            label += f" — **{oc}**"
        if include_status:
            label = f"{icon} {label}"
        return label

    def _build_status_embed(self, session: dict[str, Any], channel: discord.abc.GuildChannel | None = None) -> discord.Embed:
        ends_at = parse_ts(session.get("ends_at"))
        warn_at = parse_ts(session.get("warn_at"))
        participants = self._get_participants(str(session["session_id"]))

        posted = [p for p in participants if p.get("status") == "posted"]
        pending = [p for p in participants if p.get("status") == "pending"]
        excused = [p for p in participants if p.get("status") == "excused"]
        removed = [p for p in participants if p.get("status") == "removed"]

        emb = discord.Embed(title="Post Window Status", color=discord.Color.dark_teal(), timestamp=now_utc())
        if channel:
            emb.description = f"Channel: {channel.mention if hasattr(channel, 'mention') else channel.name}"
        if ends_at:
            emb.add_field(
                name="Ends",
                value=f"<t:{int(ends_at.timestamp())}:F> (<t:{int(ends_at.timestamp())}:R>)",
                inline=False,
            )
        if warn_at and not session.get("warned_at"):
            emb.add_field(name="Reminder", value=f"<t:{int(warn_at.timestamp())}:R>", inline=True)

        emb.add_field(name="Tracking", value=str(session.get("tracking_mode") or "oc"), inline=True)
        emb.add_field(name="Ping mode", value=str(session.get("ping_mode") or "missing"), inline=True)
        emb.add_field(name="Lock at end", value=str(bool(session.get("lock_on_close"))), inline=True)

        emb.add_field(
            name=f"Still Needed ({len(pending)})",
            value=chunk_lines([self._participant_label(p, include_status=True) for p in pending]),
            inline=False,
        )
        emb.add_field(
            name=f"Posted ({len(posted)})",
            value=chunk_lines([self._participant_label(p, include_status=True) for p in posted]),
            inline=False,
        )
        if excused:
            emb.add_field(
                name=f"Excused ({len(excused)})",
                value=chunk_lines([self._participant_label(p, include_status=True) for p in excused]),
                inline=False,
            )
        if removed:
            emb.set_footer(text=f"{len(removed)} participant(s) removed from the roster.")
        return emb

    def _build_missing_message(self, session: dict[str, Any], *, manual: bool = False) -> tuple[str, discord.AllowedMentions]:
        missing = self._missing_participants(session)
        ends_at = parse_ts(session.get("ends_at"))
        remaining = fmt_human_left(int(((ends_at or now_utc()) - now_utc()).total_seconds()))
        ping_mode = str(session.get("ping_mode") or "missing")

        if not missing:
            return "✅ Everyone on the post-window roster has posted or been excused.", discord.AllowedMentions.none()

        lines = [self._participant_label(p) for p in missing]
        header = "⚠️ **Healthy posting reminder!**" if manual else "⚠️ **Post-window reminder!**"
        mention_prefix = ""
        allowed = discord.AllowedMentions.none()

        if ping_mode == "here":
            mention_prefix = "@here "
            allowed = discord.AllowedMentions(everyone=True, users=True)
        elif ping_mode == "missing":
            allowed = discord.AllowedMentions(users=True)

        msg = (
            f"{mention_prefix}{header}\n"
            f"Time left: **{remaining}**"
        )
        if ends_at:
            msg += f" — closes <t:{int(ends_at.timestamp())}:R>"
        msg += "\n\nStill waiting on:\n" + "\n".join(f"• {line}" for line in lines[:25])
        if len(lines) > 25:
            msg += f"\n…and {len(lines) - 25} more."
        return msg, allowed

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @group.command(name="start", description="Start a persistent RP post window in this channel.")
    @app_commands.describe(
        limit="How long people have to post, e.g. 24h, 3d, 90m",
        warn_before="Optional reminder time before the end, e.g. 2h, 30m",
        lock="Lock the channel/thread when the window fully ends",
        message="Closure message when the window ends",
        unlock_before_start="Unlock this channel/thread before starting",
        ping_mode="Who to ping on reminders",
        tracking_mode="Track posts per OC or per player",
        roster_mode="How to fill the starting roster",
        role="Required only for role roster mode",
    )
    @app_commands.choices(
        ping_mode=PING_MODE_CHOICES,
        tracking_mode=TRACKING_MODE_CHOICES,
        roster_mode=ROSTER_MODE_CHOICES,
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def start(
        self,
        interaction: discord.Interaction,
        limit: str,
        warn_before: str | None = None,
        lock: bool = True,
        message: str = DEFAULT_CLOSE_MESSAGE,
        unlock_before_start: bool = True,
        ping_mode: str = "missing",
        tracking_mode: str = "oc",
        roster_mode: str = "manual",
        role: discord.Role | None = None,
    ):
        await self._defer(interaction)

        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in a server text channel or thread.", ephemeral=True)

        if ping_mode not in {"missing", "here", "none"}:
            ping_mode = "missing"
        if tracking_mode not in {"oc", "user"}:
            tracking_mode = "oc"
        if roster_mode not in {"manual", "role", "thread_members"}:
            roster_mode = "manual"

        ch = interaction.channel
        guild_id = int(interaction.guild.id)
        channel_id = int(ch.id)

        existing = self._get_open_session(guild_id, channel_id)
        if existing:
            return await self._send(interaction, "A post window is already open here. Use `/postwindow status`, `/postwindow stop`, or `/postwindow extendall`.", ephemeral=True)

        try:
            limit_s = parse_duration(limit)
            warn_s = parse_duration(warn_before) if warn_before else None
        except ValueError as e:
            return await self._send(interaction, f"❌ {e}", ephemeral=True)

        if warn_s and warn_s >= limit_s:
            return await self._send(interaction, "❌ `warn_before` must be shorter than the full window limit.", ephemeral=True)

        if roster_mode == "role" and role is None:
            return await self._send(interaction, "❌ Role roster mode needs a `role`.", ephemeral=True)

        if roster_mode == "thread_members" and not isinstance(ch, discord.Thread):
            return await self._send(interaction, "❌ Thread member roster mode only works inside a Discord thread.", ephemeral=True)

        if unlock_before_start:
            await self._unlock_channel(ch, reason="Auto-unlock before post window start")

        start_time = now_utc()
        ends_at = start_time + timedelta(seconds=limit_s)
        warn_at = (ends_at - timedelta(seconds=warn_s)) if warn_s else None

        try:
            ins = self.sb().table("postwindow_sessions").insert(
                {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "starter_user_id": int(interaction.user.id),
                    "status": "open",
                    "starts_at": to_iso(start_time),
                    "ends_at": to_iso(ends_at),
                    "warn_at": to_iso(warn_at),
                    "warned_at": None,
                    "closed_at": None,
                    "close_message": message or DEFAULT_CLOSE_MESSAGE,
                    "lock_on_close": bool(lock),
                    "unlock_before_start": bool(unlock_before_start),
                    "ping_mode": ping_mode,
                    "tracking_mode": tracking_mode,
                    "roster_mode": roster_mode,
                    "target_role_id": int(role.id) if role else None,
                }
            ).execute()
            session = (ins.data or [None])[0]
            if not session:
                return await self._send(interaction, "❌ Could not create the post window row.", ephemeral=True)
        except Exception as e:
            print(f"[postwindow start] error: {e}")
            traceback.print_exc()
            return await self._send(interaction, "❌ Database error starting the post window. Did you run the SQL migration?", ephemeral=True)

        added = 0
        skipped: list[str] = []

        if roster_mode == "role" and role is not None:
            added, skipped = await self._seed_members(session=session, members=role.members, added_by=int(interaction.user.id))
        elif roster_mode == "thread_members" and isinstance(ch, discord.Thread):
            members: list[discord.Member] = []
            try:
                thread_members = await ch.fetch_members()
                for tm in thread_members:
                    member = interaction.guild.get_member(tm.id)
                    if member:
                        members.append(member)
            except Exception:
                members = [m for m in getattr(ch, "members", []) if isinstance(m, discord.Member)]
            added, skipped = await self._seed_members(session=session, members=members, added_by=int(interaction.user.id))

        msg = (
            f"✅ Started a **per-{tracking_mode.upper()} post window**.\n"
            f"• Ends: <t:{int(ends_at.timestamp())}:F> (<t:{int(ends_at.timestamp())}:R>)\n"
            + (f"• Reminder: <t:{int(warn_at.timestamp())}:R>\n" if warn_at else "")
            + f"• Roster mode: **{roster_mode}**\n"
            + f"• Ping mode: **{ping_mode}**\n"
            + f"• Lock at end: **{bool(lock)}**\n"
            + f"• Starting roster entries added: **{added}**"
        )
        if skipped:
            msg += "\n\n⚠️ Skipped because no active/single OC was found:\n" + "\n".join(f"• {s}" for s in skipped[:15])
            if len(skipped) > 15:
                msg += f"\n…and {len(skipped) - 15} more."
            msg += "\nUse `/postwindow add` to add their exact OC manually."

        return await self._send(interaction, msg)

    @group.command(name="add", description="Add one player/OC to the current post-window roster.")
    @app_commands.describe(user="Player to add", oc_name="OC name. Leave blank to use their active/single OC.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def add(self, interaction: discord.Interaction, user: discord.Member, oc_name: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)

        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        character_id = None
        character_name = None
        if str(session.get("tracking_mode") or "oc") == "oc":
            char, err = self._find_character_for_user(int(user.id), oc_name)
            if err:
                return await self._send(interaction, f"❌ {err}", ephemeral=True)
            if char:
                character_id = str(char["character_id"])
                character_name = str(char.get("name") or "Unknown OC")

        created = self._add_participant(
            session=session,
            user_id=int(user.id),
            character_id=character_id,
            character_name=character_name,
            added_by=int(interaction.user.id),
        )
        action = "Added" if created else "Restored/already had"
        label = user.mention + (f" — **{character_name}**" if character_name else "")
        return await self._send(interaction, f"✅ {action} {label} to the roster.", ephemeral=True)

    @group.command(name="addrole", description="Add everyone in a role to the current post-window roster.")
    @app_commands.describe(role="Role to add. In per-OC mode, their active/single OC is used.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def addrole(self, interaction: discord.Interaction, role: discord.Role):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        added, skipped = await self._seed_members(session=session, members=role.members, added_by=int(interaction.user.id))
        msg = f"✅ Added **{added}** roster entr{'y' if added == 1 else 'ies'} from {role.mention}."
        if skipped:
            msg += "\n\n⚠️ Skipped because no active/single OC was found:\n" + "\n".join(f"• {s}" for s in skipped[:15])
            if len(skipped) > 15:
                msg += f"\n…and {len(skipped) - 15} more."
        return await self._send(interaction, msg, ephemeral=True)

    @group.command(name="addthread", description="Add current thread members to the post-window roster.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def addthread(self, interaction: discord.Interaction):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not isinstance(interaction.channel, discord.Thread):
            return await self._send(interaction, "Run this inside a Discord thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        members: list[discord.Member] = []
        try:
            thread_members = await interaction.channel.fetch_members()
            for tm in thread_members:
                member = interaction.guild.get_member(tm.id)
                if member:
                    members.append(member)
        except Exception:
            members = [m for m in getattr(interaction.channel, "members", []) if isinstance(m, discord.Member)]

        added, skipped = await self._seed_members(session=session, members=members, added_by=int(interaction.user.id))
        msg = f"✅ Added **{added}** thread roster entr{'y' if added == 1 else 'ies'}."
        if skipped:
            msg += "\n\n⚠️ Skipped because no active/single OC was found:\n" + "\n".join(f"• {s}" for s in skipped[:15])
        return await self._send(interaction, msg, ephemeral=True)

    @group.command(name="remove", description="Remove a player/OC from the current post-window roster.")
    @app_commands.describe(user="Player to remove", oc_name="OC name if this user has multiple OCs in the window")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def remove(self, interaction: discord.Interaction, user: discord.Member, oc_name: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        rows = [p for p in self._get_participants(str(session["session_id"])) if int(p["user_id"]) == int(user.id) and p.get("status") != "removed"]
        if oc_name:
            rows = [p for p in rows if str(p.get("character_name") or "").casefold() == oc_name.strip().casefold()]
        if not rows:
            return await self._send(interaction, "I couldn’t find that roster entry.", ephemeral=True)
        if len(rows) > 1:
            names = ", ".join(str(r.get("character_name") or "Player entry") for r in rows)
            return await self._send(interaction, f"That player has multiple entries. Please provide `oc_name`. Options: {names}", ephemeral=True)

        p = rows[0]
        self.sb().table("postwindow_participants").update({"status": "removed"}).eq("participant_id", p["participant_id"]).execute()
        return await self._send(interaction, f"➖ Removed {self._participant_label(p)} from the roster.", ephemeral=True)

    @group.command(name="status", description="Show who posted, who is missing, and when the window ends.")
    async def status(self, interaction: discord.Interaction):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in a server channel/thread.", ephemeral=True)

        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            latest = self._get_latest_session(int(interaction.guild.id), int(interaction.channel.id))
            state = str(latest.get("status")) if latest else "none"
            return await self._send(interaction, f"No active post window here. Latest status: **{state}**.", ephemeral=True)

        return await self._send(interaction, embed=self._build_status_embed(session, interaction.channel), ephemeral=True)

    @group.command(name="missing", description="Post a targeted reminder for whoever has not posted yet.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def missing(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        msg, allowed = self._build_missing_message(session, manual=True)
        if interaction.response.is_done():
            return await interaction.followup.send(msg, allowed_mentions=allowed)
        return await interaction.response.send_message(msg, allowed_mentions=allowed)

    @group.command(name="posted", description="Staff: manually mark a player/OC as posted.")
    @app_commands.describe(user="Player to mark", oc_name="OC name. Leave blank to use active/single OC.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def posted(self, interaction: discord.Interaction, user: discord.Member, oc_name: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        target = self._resolve_participant_for_user(session, int(user.id), oc_name=oc_name, include_posted=True)
        if isinstance(target, str):
            return await self._send(interaction, f"❌ {target}", ephemeral=True)
        if not target:
            return await self._send(interaction, "I couldn’t find that roster entry.", ephemeral=True)

        self.sb().table("postwindow_participants").update(
            {
                "status": "posted",
                "first_post_at": target.get("first_post_at") or to_iso(now_utc()),
                "last_post_at": to_iso(now_utc()),
            }
        ).eq("participant_id", target["participant_id"]).execute()
        return await self._send(interaction, f"✅ Marked {self._participant_label(target)} as posted.", ephemeral=True)

    @group.command(name="claim", description="Player: mark one of your own rostered OCs as posted if automark could not tell.")
    @app_commands.describe(oc_name="Your OC name. Leave blank to use active/single OC.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def claim(self, interaction: discord.Interaction, oc_name: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)

        target = self._resolve_participant_for_user(session, int(interaction.user.id), oc_name=oc_name, include_posted=True)
        if isinstance(target, str):
            return await self._send(interaction, f"❌ {target}", ephemeral=True)
        if not target:
            return await self._send(interaction, "I couldn’t find you on this roster.", ephemeral=True)

        self.sb().table("postwindow_participants").update(
            {
                "status": "posted",
                "first_post_at": target.get("first_post_at") or to_iso(now_utc()),
                "last_post_at": to_iso(now_utc()),
            }
        ).eq("participant_id", target["participant_id"]).execute()
        return await self._send(interaction, f"✅ Marked **{target.get('character_name') or 'your entry'}** as posted.", ephemeral=True)

    @group.command(name="excuse", description="Excuse a player/OC from needing to post in this window.")
    @app_commands.describe(user="Player to excuse", until="How long, e.g. 12h, 2d", oc_name="OC name if needed", reason="Optional reason")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def excuse(self, interaction: discord.Interaction, user: discord.Member, until: str = "7d", oc_name: str | None = None, reason: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)
        try:
            secs = parse_duration(until)
        except ValueError as e:
            return await self._send(interaction, f"❌ {e}", ephemeral=True)

        target = self._resolve_participant_for_user(session, int(user.id), oc_name=oc_name, include_posted=True)
        if isinstance(target, str):
            return await self._send(interaction, f"❌ {target}", ephemeral=True)
        if not target:
            return await self._send(interaction, "I couldn’t find that roster entry.", ephemeral=True)

        excused_until = now_utc() + timedelta(seconds=secs)
        self.sb().table("postwindow_participants").update(
            {
                "status": "excused",
                "excused_until": to_iso(excused_until),
                "excuse_reason": reason,
            }
        ).eq("participant_id", target["participant_id"]).execute()
        return await self._send(interaction, f"📝 Excused {self._participant_label(target)} until <t:{int(excused_until.timestamp())}:R>.", ephemeral=True)

    @group.command(name="unexcuse", description="Put an excused player/OC back into pending.")
    @app_commands.describe(user="Player to unexcuse", oc_name="OC name if needed")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def unexcuse(self, interaction: discord.Interaction, user: discord.Member, oc_name: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)
        target = self._resolve_participant_for_user(session, int(user.id), oc_name=oc_name, include_posted=True)
        if isinstance(target, str):
            return await self._send(interaction, f"❌ {target}", ephemeral=True)
        if not target:
            return await self._send(interaction, "I couldn’t find that roster entry.", ephemeral=True)
        self.sb().table("postwindow_participants").update(
            {"status": "pending", "excused_until": None, "excuse_reason": None}
        ).eq("participant_id", target["participant_id"]).execute()
        return await self._send(interaction, f"⏳ Put {self._participant_label(target)} back into pending.", ephemeral=True)

    @group.command(name="extend", description="Give extra time to one player/OC.")
    @app_commands.describe(user="Player to extend", extra="Extra time, e.g. 30m, 1h", oc_name="OC name if needed", reason="Optional reason")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def extend(self, interaction: discord.Interaction, user: discord.Member, extra: str, oc_name: str | None = None, reason: str | None = None):
        await self._defer(interaction, ephemeral=True)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)
        try:
            secs = parse_duration(extra)
        except ValueError as e:
            return await self._send(interaction, f"❌ {e}", ephemeral=True)

        target = self._resolve_participant_for_user(session, int(user.id), oc_name=oc_name, include_posted=True)
        if isinstance(target, str):
            return await self._send(interaction, f"❌ {target}", ephemeral=True)
        if not target:
            return await self._send(interaction, "I couldn’t find that roster entry.", ephemeral=True)

        new_extra = int(target.get("extension_seconds") or 0) + secs
        self.sb().table("postwindow_participants").update(
            {"extension_seconds": new_extra, "extension_reason": reason}
        ).eq("participant_id", target["participant_id"]).execute()

        deadline = (parse_ts(session.get("ends_at")) or now_utc()) + timedelta(seconds=new_extra)
        return await self._send(interaction, f"⏱️ Extended {self._participant_label(target)} by **{extra}**. Personal deadline: <t:{int(deadline.timestamp())}:R>.", ephemeral=True)

    @group.command(name="extendall", description="Extend the whole post window for everyone.")
    @app_commands.describe(extra="Extra time, e.g. 30m, 1h, 2h30m", reason="Optional reason")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def extendall(self, interaction: discord.Interaction, extra: str, reason: str | None = None):
        await self._defer(interaction)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)
        try:
            secs = parse_duration(extra)
        except ValueError as e:
            return await self._send(interaction, f"❌ {e}", ephemeral=True)

        old_end = parse_ts(session.get("ends_at")) or now_utc()
        new_end = old_end + timedelta(seconds=secs)
        update: dict[str, Any] = {"ends_at": to_iso(new_end), "extension_reason": reason}
        warn_at = parse_ts(session.get("warn_at"))
        if warn_at and not session.get("warned_at"):
            update["warn_at"] = to_iso(warn_at + timedelta(seconds=secs))
        self.sb().table("postwindow_sessions").update(update).eq("session_id", session["session_id"]).execute()
        msg = f"⏩ **Window extended** by **{extra}**. New end: <t:{int(new_end.timestamp())}:F> (<t:{int(new_end.timestamp())}:R>)."
        if reason:
            msg += f"\nReason: _{reason}_"
        return await self._send(interaction, msg)

    @group.command(name="stop", description="Close the current post window immediately.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def stop(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in the post window channel/thread.", ephemeral=True)
        session = self._get_open_session(int(interaction.guild.id), int(interaction.channel.id))
        if not session:
            return await self._send(interaction, "No active post window here.", ephemeral=True)
        await self._close_session(interaction.channel, session, manual=True)
        return await self._send(interaction, "⛔ Post window closed.")

    @group.command(name="unlock", description="Unlock this channel/thread and stop blocking new posts after closure.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        await self._defer(interaction)
        if not interaction.guild or not interaction.channel:
            return await self._send(interaction, "Run this in a text channel or thread.", ephemeral=True)

        await self._unlock_channel(interaction.channel, reason="Manual post-window unlock")
        latest = self._get_latest_session(int(interaction.guild.id), int(interaction.channel.id))
        if latest and latest.get("status") == "closed":
            self.sb().table("postwindow_sessions").update({"status": "unlocked"}).eq("session_id", latest["session_id"]).execute()

        return await self._send(interaction, "🔓 Channel/thread unlocked. New posts will no longer be blocked by the closed window.")

    # ------------------------------------------------------------------
    # Participant resolution + listeners
    # ------------------------------------------------------------------
    def _resolve_participant_for_user(
        self,
        session: dict[str, Any],
        user_id: int,
        *,
        oc_name: str | None = None,
        include_posted: bool = False,
    ) -> dict[str, Any] | str | None:
        statuses = {"pending", "excused"}
        if include_posted:
            statuses.add("posted")

        rows = [
            p
            for p in self._get_participants(str(session["session_id"]))
            if int(p["user_id"]) == int(user_id) and str(p.get("status") or "pending") in statuses
        ]
        if not rows:
            return None

        raw = (oc_name or "").strip()
        if raw:
            matches = [p for p in rows if str(p.get("character_name") or "").casefold() == raw.casefold()]
            if len(matches) == 1:
                return matches[0]
            starts = [p for p in rows if str(p.get("character_name") or "").casefold().startswith(raw.casefold())]
            if len(starts) == 1:
                return starts[0]
            if starts:
                return "More than one rostered OC matched that name. Use the full OC name."
            return "That OC is not on this post-window roster for that player."

        pending = [p for p in rows if p.get("status") == "pending"] or rows
        if len(pending) == 1:
            return pending[0]

        # Per-OC auto-resolution: try active OC if the player has multiple entries.
        active = self._get_active_character(user_id)
        if active:
            active_id = str(active.get("character_id"))
            active_matches = [p for p in pending if str(p.get("character_id") or "") == active_id]
            if len(active_matches) == 1:
                return active_matches[0]

        names = ", ".join(str(p.get("character_name") or "Player entry") for p in pending)
        return f"That player has multiple pending roster entries. Please specify `oc_name`. Options: {names}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        try:
            session = self._get_open_session(int(message.guild.id), int(message.channel.id))
            if session:
                await self._mark_posted_from_message(message, session)
                return

            latest = self._get_latest_session(int(message.guild.id), int(message.channel.id))
            if latest and latest.get("status") == "closed":
                perms = message.channel.permissions_for(message.author)
                if not (perms.manage_messages or perms.manage_channels):
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.HTTPException):
                        pass
        except Exception as e:
            print(f"[postwindow on_message] error: {e}")
            traceback.print_exc()

    async def _mark_posted_from_message(self, message: discord.Message, session: dict[str, Any]) -> None:
        target = self._resolve_participant_for_user(session, int(message.author.id), include_posted=False)
        if not target or isinstance(target, str):
            return

        deadline = self._participant_deadline(session, target)
        if now_utc() > deadline:
            # Their personal deadline passed. Staff can still override with /postwindow posted.
            return

        now = now_utc()
        first_post_at = target.get("first_post_at") or to_iso(now)
        update = {
            "status": "posted",
            "first_post_at": first_post_at,
            "last_post_at": to_iso(now),
            "last_message_id": int(message.id),
        }
        self.sb().table("postwindow_participants").update(update).eq("participant_id", target["participant_id"]).execute()

        self.sb().table("postwindow_posts").upsert(
            {
                "session_id": str(session["session_id"]),
                "guild_id": int(message.guild.id),
                "channel_id": int(message.channel.id),
                "user_id": int(message.author.id),
                "character_id": target.get("character_id"),
                "message_id": int(message.id),
                "posted_at": to_iso(now),
            },
            on_conflict="session_id,message_id",
        ).execute()

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild:
            return
        try:
            latest = self._get_latest_session(int(after.guild.id), int(after.channel.id))
            if not latest or latest.get("status") != "closed":
                return
            perms = after.channel.permissions_for(after.author)
            if perms.manage_messages or perms.manage_channels:
                return
            try:
                await after.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
        except Exception as e:
            print(f"[postwindow on_message_edit] error: {e}")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Watchdog + close/unlock
    # ------------------------------------------------------------------
    @tasks.loop(seconds=20)
    async def watchdog(self):
        await self.bot.wait_until_ready()
        try:
            res = self.sb().table("postwindow_sessions").select("*").eq("status", "open").execute()
            sessions = res.data or []
        except Exception as e:
            print(f"[postwindow watchdog] DB error: {e}")
            return

        now = now_utc()
        for session in sessions:
            try:
                guild = self.bot.get_guild(int(session["guild_id"]))
                if not guild:
                    continue

                ch = self.bot.get_channel(int(session["channel_id"]))
                if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                    try:
                        ch = await self.bot.fetch_channel(int(session["channel_id"]))
                    except Exception:
                        ch = None
                if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                    continue

                warn_at = parse_ts(session.get("warn_at"))
                ends_at = parse_ts(session.get("ends_at"))
                if warn_at and not session.get("warned_at") and now >= warn_at and (not ends_at or now < ends_at):
                    msg, allowed = self._build_missing_message(session)
                    try:
                        await ch.send(msg, allowed_mentions=allowed)
                    except discord.HTTPException:
                        pass
                    self.sb().table("postwindow_sessions").update({"warned_at": to_iso(now)}).eq("session_id", session["session_id"]).execute()

                if ends_at and now >= self._latest_needed_deadline(session):
                    await self._close_session(ch, session)

            except Exception as e:
                print(f"[postwindow watchdog] session error: {e}")
                traceback.print_exc()

    def _latest_needed_deadline(self, session: dict[str, Any]) -> datetime:
        base = parse_ts(session.get("ends_at")) or now_utc()
        latest = base
        for p in self._missing_participants(session):
            deadline = self._participant_deadline(session, p)
            if deadline > latest:
                latest = deadline
        return latest

    async def _close_session(self, ch: discord.TextChannel | discord.Thread, session: dict[str, Any], *, manual: bool = False):
        close_msg = session.get("close_message") or DEFAULT_CLOSE_MESSAGE
        missing = self._missing_participants(session)
        if missing:
            close_msg += "\n\nStill missing:\n" + "\n".join(f"• {self._participant_label(p)}" for p in missing[:25])
            if len(missing) > 25:
                close_msg += f"\n…and {len(missing) - 25} more."

        try:
            await ch.send(close_msg, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

        self.sb().table("postwindow_sessions").update(
            {
                "status": "closed",
                "closed_at": to_iso(now_utc()),
                "closed_by_user_id": None,
            }
        ).eq("session_id", session["session_id"]).execute()

        if bool(session.get("lock_on_close")):
            try:
                if isinstance(ch, discord.TextChannel):
                    overwrites = ch.overwrites_for(ch.guild.default_role)
                    overwrites.send_messages = False
                    await ch.set_permissions(ch.guild.default_role, overwrite=overwrites, reason="Post window closed")
                elif isinstance(ch, discord.Thread):
                    await ch.edit(locked=True, reason="Post window closed")
            except discord.Forbidden:
                try:
                    await ch.send("⚠️ I couldn't lock this channel/thread. I need **Manage Channels** permission.")
                except discord.HTTPException:
                    pass

    async def _unlock_channel(self, ch: discord.abc.GuildChannel, *, reason: str):
        try:
            if isinstance(ch, discord.TextChannel):
                overwrites = ch.overwrites_for(ch.guild.default_role)
                if overwrites.send_messages is False:
                    overwrites.send_messages = None
                    await ch.set_permissions(ch.guild.default_role, overwrite=overwrites, reason=reason)
            elif isinstance(ch, discord.Thread):
                if ch.locked:
                    await ch.edit(locked=False, reason=reason)
        except discord.Forbidden:
            # Command caller gets a success-ish response elsewhere. This keeps start from hard-crashing.
            pass

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PostWindow(bot))
