from __future__ import annotations

import json
import os
import re
import traceback
from datetime import timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.autocomplete_service import oc_name_autocomplete
from services.xp_service import (
    XPDuplicateAwardError,
    XPService,
    XPServiceError,
    XPValidationError,
)


WORD_RE = re.compile(r"\b[\w'’-]+\b")

RP_XP_APPROVAL_CHANNEL_ID = int(os.getenv("RP_XP_APPROVAL_CHANNEL_ID", "0") or 0)
RP_XP_AUDIT_CHANNEL_ID = int(os.getenv("RP_XP_AUDIT_CHANNEL_ID", "1473718234174718109") or 0)


SCENE_TYPES = [
    app_commands.Choice(name="Social", value="social"),
    app_commands.Choice(name="Downtime", value="downtime"),
    app_commands.Choice(name="Travel", value="travel"),
    app_commands.Choice(name="Event", value="event"),
    app_commands.Choice(name="Mission - no RP XP", value="mission"),
    app_commands.Choice(name="Combat - no RP XP", value="combat"),
]


def normalize_oc_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def get_message_display_name(message: discord.Message) -> str:
    return (
        getattr(message.author, "display_name", None)
        or getattr(message.author, "name", None)
        or ""
    ).strip()


def count_words(content: str) -> int:
    return len(WORD_RE.findall(content or ""))


def is_valid_rp_message(message: discord.Message) -> bool:
    if message.author.bot and message.webhook_id is None:
        return False

    content = (message.content or "").strip()
    if not content:
        return False

    ignored_prefixes = ("//", "((", "[[", "ooc:", "OOC:", "!")
    if content.startswith(ignored_prefixes):
        return False

    return count_words(content) >= 5


def xp_from_words(words: int) -> int:
    paragraphs = words // 100
    return paragraphs * 3


def get_rp_location_payload(message: discord.Message) -> dict[str, Any]:
    """Build durable Discord location fields for a tracked RP post.

    The tracker only saves posts from threads, but this safely handles normal
    channels too. For threads:
    - thread_id/thread_name = the RP thread
    - channel_id/channel_name = the parent channel, which is usually the broader location/category
    - location_name = parent channel name first, then thread name
    """
    channel = message.channel
    parent = getattr(channel, "parent", None)

    is_thread = isinstance(channel, discord.Thread)

    thread_id = int(channel.id) if is_thread else None
    thread_name = str(getattr(channel, "name", "") or "") if is_thread else None

    parent_channel_id = None
    parent_channel_name = None

    if parent is not None:
        parent_channel_id = int(getattr(parent, "id", 0) or 0) or None
        parent_channel_name = str(getattr(parent, "name", "") or "") or None

    channel_id = parent_channel_id if parent_channel_id is not None else int(getattr(channel, "id", 0) or 0)
    channel_name = parent_channel_name or str(getattr(channel, "name", "") or "") or None

    location_name = parent_channel_name or thread_name or channel_name

    return {
        "thread_id": int(thread_id or channel.id),
        "thread_name": thread_name,
        "channel_id": int(channel_id) if channel_id is not None else None,
        "channel_name": channel_name,
        "parent_channel_id": parent_channel_id,
        "parent_channel_name": parent_channel_name,
        "location_name": location_name,
        "jump_url": getattr(message, "jump_url", None),
    }

def thread_jump_url(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def message_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def safe_locations(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []

    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]

    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                return [x for x in loaded if isinstance(x, dict)]
        except Exception:
            return []

    return []


class RPXPAdjustModal(discord.ui.Modal):
    def __init__(self, cog: "RPTools", claim_id: str):
        super().__init__(title="Adjust RP XP")
        self.cog = cog
        self.claim_id = claim_id

        self.approved_xp = discord.ui.TextInput(
            label="Approved XP",
            placeholder="Example: 24",
            required=True,
            max_length=8,
        )
        self.reason = discord.ui.TextInput(
            label="Reason",
            placeholder="Example: Adjusted for duplicate text / partial eligibility.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )

        self.add_item(self.approved_xp)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            raw_xp = str(self.approved_xp.value or "").strip()
            if not raw_xp.isdigit():
                return await interaction.response.send_message(
                    "Approved XP must be a whole number.",
                    ephemeral=True,
                )

            xp_value = int(raw_xp)
            await self.cog.review_rp_xp_claim(
                interaction=interaction,
                claim_id=self.claim_id,
                status="approved",
                approved_xp=xp_value,
                reason=str(self.reason.value or "").strip(),
            )

        except Exception as e:
            print(f"[rp xp adjust modal] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error adjusting RP XP claim.",
                    ephemeral=True,
                )


class RPXPDenyModal(discord.ui.Modal):
    def __init__(self, cog: "RPTools", claim_id: str):
        super().__init__(title="Deny RP XP")
        self.cog = cog
        self.claim_id = claim_id

        self.reason = discord.ui.TextInput(
            label="Denial reason",
            placeholder="Example: Scene was not complete yet.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )

        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.cog.review_rp_xp_claim(
                interaction=interaction,
                claim_id=self.claim_id,
                status="denied",
                approved_xp=None,
                reason=str(self.reason.value or "").strip(),
            )

        except Exception as e:
            print(f"[rp xp deny modal] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error denying RP XP claim.",
                    ephemeral=True,
                )


class RPTools:
    bot: commands.Bot

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    async def get_owned_oc_by_name(self, user_id: int, name: str):
        raw = normalize_oc_name(name)

        res = (
            self.sb()
            .table("characters")
            .select("character_id, name, user_id")
            .eq("user_id", user_id)
            .execute()
        )

        rows = getattr(res, "data", None) or []

        matches = [
            r for r in rows
            if normalize_oc_name(r.get("name") or "") == raw
        ]

        return matches[0] if matches else None

    async def get_global_oc_by_name(self, name: str):
        target = normalize_oc_name(name)
        if not target:
            return None

        res = (
            self.sb()
            .table("characters")
            .select("character_id, name, user_id")
            .execute()
        )

        rows = getattr(res, "data", None) or []

        matches = [
            r for r in rows
            if normalize_oc_name(r.get("name") or "") == target
        ]

        if len(matches) != 1:
            return None

        return matches[0]

    async def get_active_scene_by_thread(self, thread_id: int):
        res = (
            self.sb()
            .table("rp_scenes")
            .select("*")
            .eq("thread_id", thread_id)
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def get_scene_by_thread(self, thread_id: int):
        res = (
            self.sb()
            .table("rp_scenes")
            .select("*")
            .eq("thread_id", thread_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def get_participant(self, scene_id: str, user_id: int):
        res = (
            self.sb()
            .table("rp_scene_participants")
            .select("*")
            .eq("scene_id", scene_id)
            .eq("user_id", user_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def get_participants_for_user(self, scene_id: str, user_id: int):
        res = (
            self.sb()
            .table("rp_scene_participants")
            .select("*")
            .eq("scene_id", scene_id)
            .eq("user_id", user_id)
            .execute()
        )
        return getattr(res, "data", None) or []

    async def get_participant_by_oc_name(self, scene_id: str, oc_name: str):
        target = normalize_oc_name(oc_name)

        if not target:
            return None

        res = (
            self.sb()
            .table("rp_scene_participants")
            .select("*")
            .eq("scene_id", scene_id)
            .eq("is_active", True)
            .execute()
        )

        rows = getattr(res, "data", None) or []

        for row in rows:
            character_name = normalize_oc_name(row.get("character_name") or "")
            if character_name == target:
                return row

        return None

    async def auto_add_participant_by_oc_name(self, scene_id: str, oc_name: str):
        existing = await self.get_participant_by_oc_name(scene_id, oc_name)
        if existing:
            return existing

        oc_row = await self.get_global_oc_by_name(oc_name)
        if not oc_row:
            return None

        try:
            self.sb().table("rp_scene_participants").insert({
                "scene_id": scene_id,
                "character_id": oc_row["character_id"],
                "user_id": int(oc_row["user_id"]),
                "character_name": oc_row["name"],
            }).execute()

        except Exception:
            pass

        return await self.get_participant_by_oc_name(scene_id, oc_name)

    async def resolve_participant_for_message(self, scene_row: dict, message: discord.Message):
        scene_id = scene_row["scene_id"]

        if message.webhook_id is None:
            return await self.get_participant(
                scene_id,
                int(message.author.id),
            )

        webhook_name = get_message_display_name(message)

        participant = await self.get_participant_by_oc_name(
            scene_id,
            webhook_name,
        )

        if participant:
            return participant

        if bool(scene_row.get("auto_join")):
            return await self.auto_add_participant_by_oc_name(
                scene_id,
                webhook_name,
            )

        return None

    async def save_tracked_post(
        self,
        *,
        scene_row: dict,
        message: discord.Message,
        participant: dict,
    ):
        words = count_words(message.content)
        preview = message.content.strip()

        if len(preview) > 250:
            preview = preview[:247] + "..."

        payload = {
            "scene_id": scene_row["scene_id"],
            "message_id": int(message.id),
            "guild_id": int(message.guild.id),
            **get_rp_location_payload(message),
            "user_id": int(participant["user_id"]),
            "character_id": participant["character_id"],
            "character_name": participant["character_name"],
            "word_count": words,
            "content_preview": preview,
            "posted_at": message.created_at.astimezone(timezone.utc).isoformat(),
        }

        self.sb().table("rp_posts").upsert(
            payload,
            on_conflict="message_id",
        ).execute()

    async def delete_tracked_post(self, message_id: int):
        self.sb().table("rp_posts").delete().eq(
            "message_id",
            int(message_id),
        ).execute()

    async def get_open_event_for_channel(self, guild_id: int, channel_id: int):
        res = (
            self.sb()
            .table("rp_events")
            .select("*")
            .eq("guild_id", guild_id)
            .eq("channel_id", channel_id)
            .eq("status", "open")
            .limit(1)
            .execute()
        )

        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def get_event_from_context(self, interaction: discord.Interaction):
        if not interaction.guild:
            return None

        if isinstance(interaction.channel, discord.Thread):
            channel_id = int(interaction.channel.parent_id or 0)
        else:
            channel_id = int(interaction.channel.id)

        if not channel_id:
            return None

        return await self.get_open_event_for_channel(
            int(interaction.guild.id),
            channel_id,
        )

    async def register_thread_as_event_scene(
        self,
        *,
        thread: discord.Thread,
        event_row: dict,
        opened_by: int,
    ):
        existing = await self.get_scene_by_thread(int(thread.id))
        if existing:
            return existing

        title = (thread.name or "Event Scene").strip()[:120]

        ins = (
            self.sb()
            .table("rp_scenes")
            .insert({
                "guild_id": int(thread.guild.id),
                "channel_id": int(thread.parent_id or event_row["channel_id"]),
                "thread_id": int(thread.id),
                "event_id": event_row["event_id"],
                "title": title,
                "scene_type": "event",
                "xp_eligible": bool(event_row.get("xp_eligible", True)),
                "auto_join": True,
                "status": "open",
                "opened_by": int(opened_by or 0),
            })
            .execute()
        )

        rows = getattr(ins, "data", None) or []
        scene_row = rows[0] if rows else None

        try:
            embed = discord.Embed(
                title="Tracked Event Scene",
                description=f"**{title}** is now being tracked for **{event_row['title']}**.",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(
                name="XP Eligible",
                value="Yes" if event_row.get("xp_eligible") else "No",
                inline=True,
            )
            embed.add_field(
                name="Auto Join",
                value="On — Tupper names must match registered OC names.",
                inline=False,
            )
            await thread.send(embed=embed)
        except Exception:
            pass

        return scene_row

    # ─────────────────────────────────────────────────────────────────────
    # RP XP APPROVAL HELPERS
    # ─────────────────────────────────────────────────────────────────────

    async def get_approval_channel(self):
        if not RP_XP_APPROVAL_CHANNEL_ID:
            return None

        channel = self.bot.get_channel(RP_XP_APPROVAL_CHANNEL_ID)
        if channel:
            return channel

        try:
            return await self.bot.fetch_channel(RP_XP_APPROVAL_CHANNEL_ID)
        except Exception:
            return None

    async def get_audit_channel(self):
        if not RP_XP_AUDIT_CHANNEL_ID:
            return None

        channel = self.bot.get_channel(RP_XP_AUDIT_CHANNEL_ID)
        if channel:
            return channel

        try:
            return await self.bot.fetch_channel(RP_XP_AUDIT_CHANNEL_ID)
        except Exception:
            return None

    def build_approval_view(self, claim_row: dict) -> discord.ui.View:
        claim_id = claim_row["claim_id"]
        status = claim_row.get("status", "pending")

        view = discord.ui.View(timeout=None)

        view.add_item(discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"rp_xp:approve:{claim_id}",
            disabled=status != "pending",
        ))
        view.add_item(discord.ui.Button(
            label="Adjust XP",
            style=discord.ButtonStyle.primary,
            custom_id=f"rp_xp:adjust:{claim_id}",
            disabled=status != "pending",
        ))
        view.add_item(discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"rp_xp:deny:{claim_id}",
            disabled=status != "pending",
        ))

        if status == "denied":
            view.add_item(discord.ui.Button(
                label="Reopen",
                style=discord.ButtonStyle.secondary,
                custom_id=f"rp_xp:reopen:{claim_id}",
                disabled=False,
            ))

        return view

    def build_claim_embed(self, claim_row: dict) -> discord.Embed:
        status = claim_row.get("status", "pending")
        status_label = {
            "pending": "Pending Review",
            "approved": "Approved",
            "denied": "Denied",
        }.get(status, status.title())

        if status == "approved":
            color = discord.Color.green()
        elif status == "denied":
            color = discord.Color.red()
        else:
            color = discord.Color.gold()

        claim_type = str(claim_row.get("claim_type") or "scene").title()
        character_name = claim_row.get("character_name") or "Unknown OC"

        embed = discord.Embed(
            title=f"RP XP Approval — {status_label}",
            description=f"**{character_name}**",
            color=color,
        )

        embed.add_field(name="Type", value=claim_type, inline=True)
        embed.add_field(name="Words", value=f"`{claim_row.get('word_count', 0)}`", inline=True)
        embed.add_field(name="Posts", value=f"`{claim_row.get('post_count', 0)}`", inline=True)

        embed.add_field(
            name="Estimated XP",
            value=f"`{claim_row.get('estimated_xp', 0)}`",
            inline=True,
        )

        approved_xp = claim_row.get("approved_xp")
        embed.add_field(
            name="Approved XP",
            value="—" if approved_xp is None else f"`{approved_xp}`",
            inline=True,
        )

        embed.add_field(name="Status", value=status_label, inline=True)

        payout_status = str(claim_row.get("payout_status") or "unpaid")
        xp_tx_id = claim_row.get("xp_tx_id")
        payout_text = f"`{payout_status}`"
        if xp_tx_id:
            payout_text += f"\nTX: `{str(xp_tx_id)[:8]}`"

        embed.add_field(name="XP Payout", value=payout_text, inline=True)

        locations = safe_locations(claim_row.get("locations"))
        location_lines = []

        guild_id = int(claim_row.get("guild_id") or 0)

        for loc in locations[:8]:
            title = str(loc.get("title") or "RP Location")
            thread_id = int(loc.get("thread_id") or 0)
            words = int(loc.get("words") or 0)
            posts = int(loc.get("posts") or 0)

            if guild_id and thread_id:
                link = thread_jump_url(guild_id, thread_id)
                location_lines.append(
                    f"• [{title}]({link}) — `{words}` words / `{posts}` posts"
                )
            else:
                location_lines.append(
                    f"• {title} — `{words}` words / `{posts}` posts"
                )

        if len(locations) > 8:
            location_lines.append(f"• ...and `{len(locations) - 8}` more location(s).")

        embed.add_field(
            name="RP Location(s)",
            value="\n".join(location_lines) if location_lines else "No locations recorded.",
            inline=False,
        )

        reason = claim_row.get("review_reason")
        if reason:
            embed.add_field(name="Review Reason", value=str(reason)[:1024], inline=False)

        reviewed_by = claim_row.get("reviewed_by")
        if reviewed_by:
            embed.add_field(name="Reviewed By", value=f"<@{reviewed_by}>", inline=True)

        embed.set_footer(
            text=f"Claim ID: {str(claim_row.get('claim_id'))[:8]} • Approved claims pay XP automatically."
        )

        return embed

    def build_claim_audit_embed(
        self,
        *,
        claim_row: dict,
        action_label: str,
        actor_id: int,
        reason: str | None = None,
    ) -> discord.Embed:
        action_lower = action_label.lower()

        if "approved" in action_lower or "paid" in action_lower:
            color = discord.Color.green()
        elif "denied" in action_lower:
            color = discord.Color.red()
        elif "reopened" in action_lower:
            color = discord.Color.blurple()
        elif "failed" in action_lower:
            color = discord.Color.orange()
        else:
            color = discord.Color.gold()

        character_name = claim_row.get("character_name") or "Unknown OC"
        claim_type = str(claim_row.get("claim_type") or "scene").title()
        guild_id = int(claim_row.get("guild_id") or 0)

        embed = discord.Embed(
            title=f"RP XP {action_label}",
            description=f"**{character_name}**",
            color=color,
        )

        embed.add_field(name="Action By", value=f"<@{actor_id}>", inline=True)
        embed.add_field(name="Type", value=claim_type, inline=True)
        embed.add_field(name="Status", value=str(claim_row.get("status", "unknown")).title(), inline=True)

        embed.add_field(name="Words", value=f"`{claim_row.get('word_count', 0)}`", inline=True)
        embed.add_field(name="Posts", value=f"`{claim_row.get('post_count', 0)}`", inline=True)
        embed.add_field(name="Estimated XP", value=f"`{claim_row.get('estimated_xp', 0)}`", inline=True)

        approved_xp = claim_row.get("approved_xp")
        embed.add_field(
            name="Approved XP",
            value="—" if approved_xp is None else f"`{approved_xp}`",
            inline=True,
        )

        payout_status = str(claim_row.get("payout_status") or "unpaid")
        xp_tx_id = claim_row.get("xp_tx_id")
        payout_text = f"`{payout_status}`"
        if xp_tx_id:
            payout_text += f"\nTX: `{str(xp_tx_id)[:8]}`"
        payout_error = claim_row.get("payout_error")
        if payout_error:
            payout_text += f"\nError: {str(payout_error)[:500]}"
        embed.add_field(name="XP Payout", value=payout_text, inline=True)

        if reason:
            embed.add_field(name="Reason", value=str(reason)[:1024], inline=False)

        locations = safe_locations(claim_row.get("locations"))
        location_lines = []

        for loc in locations[:6]:
            title = str(loc.get("title") or "RP Location")
            thread_id = int(loc.get("thread_id") or 0)
            words = int(loc.get("words") or 0)
            posts = int(loc.get("posts") or 0)

            if guild_id and thread_id:
                location_lines.append(
                    f"• [{title}]({thread_jump_url(guild_id, thread_id)}) — `{words}` words / `{posts}` posts"
                )
            else:
                location_lines.append(
                    f"• {title} — `{words}` words / `{posts}` posts"
                )

        if len(locations) > 6:
            location_lines.append(f"• ...and `{len(locations) - 6}` more location(s).")

        embed.add_field(
            name="RP Location(s)",
            value="\n".join(location_lines) if location_lines else "No locations recorded.",
            inline=False,
        )

        approval_channel_id = claim_row.get("approval_channel_id")
        approval_message_id = claim_row.get("approval_message_id")
        if guild_id and approval_channel_id and approval_message_id:
            embed.add_field(
                name="Approval Card",
                value=f"[Jump to approval card]({message_jump_url(guild_id, int(approval_channel_id), int(approval_message_id))})",
                inline=False,
            )

        embed.set_footer(text=f"Claim ID: {claim_row.get('claim_id')}")
        return embed

    async def send_claim_audit_log(
        self,
        *,
        claim_row: dict,
        action_label: str,
        actor_id: int,
        reason: str | None = None,
    ):
        channel = await self.get_audit_channel()
        if channel is None:
            print("[rp xp audit] Audit channel not found or not configured.")
            return

        try:
            embed = self.build_claim_audit_embed(
                claim_row=claim_row,
                action_label=action_label,
                actor_id=actor_id,
                reason=reason,
            )
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[rp xp audit] error: {e}")
            traceback.print_exc()

    def build_xp_award_notes(self, claim: dict, reason: str | None) -> str:
        source_kind = str(claim.get("claim_type") or "rp").title()
        base = f"Approved {source_kind} RP XP claim {claim.get('claim_id')}"
        if reason:
            base += f". Review reason: {reason}"
        return base[:1000]

    async def payout_rp_xp_claim(
        self,
        *,
        claim: dict,
        approved_xp: int,
        actor_id: int,
        reason: str | None,
    ) -> dict[str, Any]:
        if approved_xp <= 0:
            raise XPValidationError("Approved XP must be greater than 0 to pay out.")

        xp_service = XPService(self.sb())
        claim_id = str(claim["claim_id"])
        claim_type = str(claim.get("claim_type") or "rp")
        title_kind = "Event" if claim_type == "event" else "Scene"

        return xp_service.award_xp(
            guild_id=int(claim["guild_id"]),
            character_id=str(claim["character_id"]),
            amount=int(approved_xp),
            source="rp",
            title=f"RP {title_kind} XP: {claim.get('character_name') or 'OC'}",
            actor_discord_id=int(actor_id),
            external_ref=f"rp_xp_claim:{claim_id}",
            notes=self.build_xp_award_notes(claim, reason),
        )

    async def find_existing_claim(
        self,
        *,
        claim_type: str,
        source_id: str,
        character_id: str,
    ):
        query = (
            self.sb()
            .table("rp_xp_claims")
            .select("*")
            .eq("claim_type", claim_type)
            .eq("character_id", character_id)
            .limit(1)
        )

        if claim_type == "scene":
            query = query.eq("scene_id", source_id)
        else:
            query = query.eq("event_id", source_id)

        res = query.execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def create_rp_xp_claim(
        self,
        *,
        guild_id: int,
        claim_type: str,
        scene_id: str | None,
        event_id: str | None,
        character_id: str,
        user_id: int,
        character_name: str,
        word_count: int,
        post_count: int,
        estimated_xp: int,
        locations: list[dict[str, Any]],
        created_by: int,
    ):
        source_id = scene_id if claim_type == "scene" else event_id
        if not source_id:
            return None

        existing = await self.find_existing_claim(
            claim_type=claim_type,
            source_id=source_id,
            character_id=character_id,
        )
        if existing:
            return existing

        ins = (
            self.sb()
            .table("rp_xp_claims")
            .insert({
                "guild_id": int(guild_id),
                "claim_type": claim_type,
                "scene_id": scene_id,
                "event_id": event_id,
                "character_id": character_id,
                "user_id": int(user_id),
                "character_name": character_name,
                "word_count": int(word_count),
                "post_count": int(post_count),
                "estimated_xp": int(estimated_xp),
                "approved_xp": None,
                "status": "pending",
                "locations": locations,
                "created_by": int(created_by),
            })
            .execute()
        )

        rows = getattr(ins, "data", None) or []
        return rows[0] if rows else None

    async def dispatch_approval_card(self, claim_row: dict) -> bool:
        channel = await self.get_approval_channel()
        if channel is None:
            print("[rp xp approval] Approval channel not found or not configured.")
            return False

        embed = self.build_claim_embed(claim_row)
        view = self.build_approval_view(claim_row)

        msg = await channel.send(embed=embed, view=view)

        upd = (
            self.sb()
            .table("rp_xp_claims")
            .update({
                "approval_channel_id": int(channel.id),
                "approval_message_id": int(msg.id),
            })
            .eq("claim_id", claim_row["claim_id"])
            .execute()
        )

        rows = getattr(upd, "data", None) or []
        return bool(rows)

    async def send_scene_approval_claims(
        self,
        *,
        scene_row: dict,
        closed_by: int,
    ) -> int:
        if not bool(scene_row.get("xp_eligible")):
            return 0

        posts = (
            self.sb()
            .table("rp_posts")
            .select("character_id, character_name, user_id, word_count")
            .eq("scene_id", scene_row["scene_id"])
            .execute()
        )
        post_rows = getattr(posts, "data", None) or []

        totals: dict[str, dict[str, Any]] = {}
        for post in post_rows:
            cid = post["character_id"]
            if cid not in totals:
                totals[cid] = {
                    "character_id": cid,
                    "character_name": post["character_name"],
                    "user_id": int(post["user_id"]),
                    "words": 0,
                    "posts": 0,
                }
            totals[cid]["words"] += int(post.get("word_count") or 0)
            totals[cid]["posts"] += 1

        sent = 0
        guild_id = int(scene_row["guild_id"])
        thread_id = int(scene_row["thread_id"])
        title = scene_row.get("title") or "RP Scene"

        for data in totals.values():
            estimated_xp = xp_from_words(data["words"])
            if estimated_xp <= 0:
                continue

            locations = [{
                "title": title,
                "thread_id": thread_id,
                "words": data["words"],
                "posts": data["posts"],
            }]

            claim = await self.create_rp_xp_claim(
                guild_id=guild_id,
                claim_type="scene",
                scene_id=scene_row["scene_id"],
                event_id=None,
                character_id=data["character_id"],
                user_id=data["user_id"],
                character_name=data["character_name"],
                word_count=data["words"],
                post_count=data["posts"],
                estimated_xp=estimated_xp,
                locations=locations,
                created_by=closed_by,
            )

            if claim and claim.get("status") == "pending":
                ok = await self.dispatch_approval_card(claim)
                if ok:
                    sent += 1

        return sent

    async def send_event_approval_claims(
        self,
        *,
        event_row: dict,
        scenes: list[dict[str, Any]],
        closed_by: int,
    ) -> int:
        if not bool(event_row.get("xp_eligible")):
            return 0

        scene_ids = [s["scene_id"] for s in scenes]
        if not scene_ids:
            return 0

        scene_map = {s["scene_id"]: s for s in scenes}

        posts_res = (
            self.sb()
            .table("rp_posts")
            .select("character_id, character_name, user_id, word_count, scene_id")
            .in_("scene_id", scene_ids)
            .execute()
        )
        posts = getattr(posts_res, "data", None) or []

        totals: dict[str, dict[str, Any]] = {}

        for post in posts:
            cid = post["character_id"]
            sid = post["scene_id"]
            scene = scene_map.get(sid) or {}

            if cid not in totals:
                totals[cid] = {
                    "character_id": cid,
                    "character_name": post["character_name"],
                    "user_id": int(post["user_id"]),
                    "words": 0,
                    "posts": 0,
                    "locations": {},
                }

            words = int(post.get("word_count") or 0)

            totals[cid]["words"] += words
            totals[cid]["posts"] += 1

            if sid not in totals[cid]["locations"]:
                totals[cid]["locations"][sid] = {
                    "title": scene.get("title") or "Event Thread",
                    "thread_id": int(scene.get("thread_id") or 0),
                    "words": 0,
                    "posts": 0,
                }

            totals[cid]["locations"][sid]["words"] += words
            totals[cid]["locations"][sid]["posts"] += 1

        sent = 0
        guild_id = int(event_row["guild_id"])

        for data in totals.values():
            estimated_xp = xp_from_words(data["words"])
            if estimated_xp <= 0:
                continue

            locations = list(data["locations"].values())

            claim = await self.create_rp_xp_claim(
                guild_id=guild_id,
                claim_type="event",
                scene_id=None,
                event_id=event_row["event_id"],
                character_id=data["character_id"],
                user_id=data["user_id"],
                character_name=data["character_name"],
                word_count=data["words"],
                post_count=data["posts"],
                estimated_xp=estimated_xp,
                locations=locations,
                created_by=closed_by,
            )

            if claim and claim.get("status") == "pending":
                ok = await self.dispatch_approval_card(claim)
                if ok:
                    sent += 1

        return sent

    async def fetch_claim(self, claim_id: str):
        res = (
            self.sb()
            .table("rp_xp_claims")
            .select("*")
            .eq("claim_id", claim_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    async def refresh_claim_message(self, claim_row: dict):
        channel_id = claim_row.get("approval_channel_id")
        message_id = claim_row.get("approval_message_id")

        if not channel_id or not message_id:
            return

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))

            msg = await channel.fetch_message(int(message_id))
            await msg.edit(
                embed=self.build_claim_embed(claim_row),
                view=self.build_approval_view(claim_row),
            )

        except Exception as e:
            print(f"[rp xp approval refresh] error: {e}")
            traceback.print_exc()

    async def review_rp_xp_claim(
        self,
        *,
        interaction: discord.Interaction,
        claim_id: str,
        status: str,
        approved_xp: int | None,
        reason: str | None,
    ):
        claim = await self.fetch_claim(claim_id)
        if not claim:
            return await interaction.response.send_message(
                "I could not find that RP XP claim.",
                ephemeral=True,
            )

        if claim.get("status") != "pending":
            return await interaction.response.send_message(
                f"This claim has already been reviewed as **{claim.get('status')}**.",
                ephemeral=True,
            )

        now_iso = interaction.created_at.astimezone(timezone.utc).isoformat()
        actor_id = int(interaction.user.id)

        payload: dict[str, Any] = {
            "status": status,
            "reviewed_by": actor_id,
            "reviewed_at": now_iso,
            "review_reason": reason,
        }

        payout_result: dict[str, Any] | None = None
        payout_error: str | None = None

        if status == "approved":
            final_xp = int(approved_xp if approved_xp is not None else claim["estimated_xp"])
            if final_xp <= 0:
                return await interaction.response.send_message(
                    "Approved XP must be greater than 0.",
                    ephemeral=True,
                )

            try:
                payout_result = await self.payout_rp_xp_claim(
                    claim=claim,
                    approved_xp=final_xp,
                    actor_id=actor_id,
                    reason=reason,
                )
            except XPDuplicateAwardError:
                payout_error = "Duplicate XP award blocked by XPService. Check this OC's XP history before retrying."
            except XPValidationError as e:
                payout_error = f"XP validation failed: {e}"
            except XPServiceError:
                traceback.print_exc()
                payout_error = "XPService failed while paying this claim."
            except Exception as e:
                traceback.print_exc()
                payout_error = f"Unexpected payout error: {e}"

            if payout_error:
                failed_payload = {
                    "payout_status": "failed",
                    "payout_error": payout_error,
                }
                try:
                    fail_update = (
                        self.sb()
                        .table("rp_xp_claims")
                        .update(failed_payload)
                        .eq("claim_id", claim_id)
                        .execute()
                    )
                    fail_rows = getattr(fail_update, "data", None) or []
                    failed_claim = fail_rows[0] if fail_rows else await self.fetch_claim(claim_id)
                    if failed_claim:
                        await self.refresh_claim_message(failed_claim)
                        await self.send_claim_audit_log(
                            claim_row=failed_claim,
                            action_label="Payout Failed",
                            actor_id=actor_id,
                            reason=payout_error,
                        )
                except Exception:
                    traceback.print_exc()

                return await interaction.response.send_message(
                    f"❌ I could not pay this RP XP claim: {payout_error}",
                    ephemeral=True,
                )

            tx_id = None
            if payout_result:
                tx_id = payout_result.get("xp_tx_id")

            payload.update({
                "approved_xp": final_xp,
                "xp_tx_id": str(tx_id) if tx_id else None,
                "paid_at": now_iso,
                "paid_by": actor_id,
                "payout_status": "paid",
                "payout_error": None,
            })
        else:
            payload.update({
                "approved_xp": None,
                "payout_status": "unpaid",
                "payout_error": None,
            })

        upd = (
            self.sb()
            .table("rp_xp_claims")
            .update(payload)
            .eq("claim_id", claim_id)
            .execute()
        )
        rows = getattr(upd, "data", None) or []
        updated = rows[0] if rows else await self.fetch_claim(claim_id)

        if updated:
            await self.refresh_claim_message(updated)

            estimated = int(updated.get("estimated_xp") or 0)
            approved = updated.get("approved_xp")
            if status == "approved" and approved is not None and int(approved) != estimated:
                action_label = "Adjusted and Paid"
            elif status == "approved":
                action_label = "Approved and Paid"
            else:
                action_label = "Denied"

            await self.send_claim_audit_log(
                claim_row=updated,
                action_label=action_label,
                actor_id=actor_id,
                reason=reason,
            )

        if not interaction.response.is_done():
            if status == "approved":
                final_xp = updated.get("approved_xp") if updated else approved_xp
                tx_text = ""
                if updated and updated.get("xp_tx_id"):
                    tx_text = f" TX `{str(updated.get('xp_tx_id'))[:8]}`."
                await interaction.response.send_message(
                    f"✅ Approved and paid **{final_xp} XP** to **{claim['character_name']}**.{tx_text}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ Denied RP XP for **{claim['character_name']}**.",
                    ephemeral=True,
                )

    async def reopen_rp_xp_claim(
        self,
        *,
        interaction: discord.Interaction,
        claim_id: str,
    ):
        claim = await self.fetch_claim(claim_id)
        if not claim:
            return await interaction.response.send_message(
                "I could not find that RP XP claim.",
                ephemeral=True,
            )

        if claim.get("status") != "denied":
            return await interaction.response.send_message(
                "Only denied RP XP claims can be reopened.",
                ephemeral=True,
            )

        reason = f"Reopened by {interaction.user} for another review."

        upd = (
            self.sb()
            .table("rp_xp_claims")
            .update({
                "status": "pending",
                "approved_xp": None,
                "reviewed_by": None,
                "reviewed_at": None,
                "review_reason": reason,
                "xp_tx_id": None,
                "paid_at": None,
                "paid_by": None,
                "payout_status": "unpaid",
                "payout_error": None,
            })
            .eq("claim_id", claim_id)
            .execute()
        )

        rows = getattr(upd, "data", None) or []
        updated = rows[0] if rows else await self.fetch_claim(claim_id)

        if updated:
            await self.refresh_claim_message(updated)
            await self.send_claim_audit_log(
                claim_row=updated,
                action_label="Reopened",
                actor_id=int(interaction.user.id),
                reason=reason,
            )

        return await interaction.response.send_message(
            f"🔁 Reopened RP XP claim for **{claim['character_name']}**.",
            ephemeral=True,
        )


class RPTrackerCog(
    RPTools,
    commands.GroupCog,
    group_name="scene",
    group_description="Tracked RP scene commands",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="open", description="Open a tracked RP scene thread")
    @app_commands.describe(
        title="Scene title",
        oc="Your OC",
        scene_type="Scene type",
    )
    @app_commands.autocomplete(oc=oc_name_autocomplete)
    @app_commands.choices(scene_type=SCENE_TYPES)
    async def scene_open(
        self,
        interaction: discord.Interaction,
        title: str,
        oc: str,
        scene_type: app_commands.Choice[str],
    ):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.followup.send(
                "Scenes can only be opened inside a normal server text channel.",
                ephemeral=True,
            )

        title = (title or "").strip()
        if not title or len(title) > 120:
            return await interaction.followup.send(
                "Scene title must be between 1 and 120 characters.",
                ephemeral=True,
            )

        user_id = int(interaction.user.id)

        try:
            open_event = await self.get_open_event_for_channel(
                int(interaction.guild.id),
                int(interaction.channel.id),
            )

            if open_event:
                return await interaction.followup.send(
                    "This channel already has an open RP event. "
                    "Just create a normal thread here instead — Keystone will track it automatically.",
                    ephemeral=True,
                )

            oc_row = await self.get_owned_oc_by_name(user_id, oc)
            if not oc_row:
                return await interaction.followup.send(
                    "OC not found, or that OC is not yours.",
                    ephemeral=True,
                )

            xp_eligible = scene_type.value not in ("mission", "combat")

            thread = await interaction.channel.create_thread(
                name=f"rp-{title[:80]}",
                type=discord.ChannelType.public_thread,
                reason=f"RP scene opened by {interaction.user}",
            )

            ins = (
                self.sb()
                .table("rp_scenes")
                .insert({
                    "guild_id": int(interaction.guild.id),
                    "channel_id": int(interaction.channel.id),
                    "thread_id": int(thread.id),
                    "title": title,
                    "scene_type": scene_type.value,
                    "xp_eligible": xp_eligible,
                    "auto_join": False,
                    "status": "open",
                    "opened_by": user_id,
                })
                .execute()
            )

            rows = getattr(ins, "data", None) or []
            if not rows:
                return await interaction.followup.send(
                    "Scene thread was created, but Supabase did not return a scene row.",
                    ephemeral=True,
                )

            scene_row = rows[0]

            self.sb().table("rp_scene_participants").insert({
                "scene_id": scene_row["scene_id"],
                "character_id": oc_row["character_id"],
                "user_id": user_id,
                "character_name": oc_row["name"],
            }).execute()

            embed = discord.Embed(
                title="Tracked RP Scene Opened",
                description=f"**{title}**",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(name="Type", value=scene_type.name, inline=True)
            embed.add_field(name="XP Eligible", value="Yes" if xp_eligible else "No", inline=True)
            embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Starting OC", value=f"**{oc_row['name']}**", inline=False)
            embed.add_field(name="Thread", value=thread.mention, inline=False)
            embed.set_footer(
                text="Tupper/webhook names must match joined OC names to be counted."
            )

            await thread.send(embed=embed)

            return await interaction.followup.send(
                f"✅ Scene opened: {thread.mention}",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[scene open] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error opening RP scene.",
                ephemeral=True,
            )

    @app_commands.command(name="join", description="Join or rejoin the current tracked scene with one of your OCs")
    @app_commands.describe(oc="Your OC")
    @app_commands.autocomplete(oc=oc_name_autocomplete)
    async def scene_join(self, interaction: discord.Interaction, oc: str):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        user_id = int(interaction.user.id)

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            oc_row = await self.get_owned_oc_by_name(user_id, oc)
            if not oc_row:
                return await interaction.followup.send(
                    "OC not found, or that OC is not yours.",
                    ephemeral=True,
                )

            # Current database rule:
            # one participant row per scene + user.
            #
            # So if this player already has a row in this scene, we update that
            # row instead of inserting. This fixes accidental /scene leave and
            # also lets staff/player recover by switching the row back to the
            # correct OC.
            existing_res = (
                self.sb()
                .table("rp_scene_participants")
                .select("*")
                .eq("scene_id", scene_row["scene_id"])
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            existing_rows = getattr(existing_res, "data", None) or []

            if existing_rows:
                old_name = str(existing_rows[0].get("character_name") or "your previous OC")

                self.sb().table("rp_scene_participants").update({
                    "is_active": True,
                    "character_id": oc_row["character_id"],
                    "character_name": oc_row["name"],
                }).eq("scene_id", scene_row["scene_id"]).eq(
                    "user_id", user_id
                ).execute()

                return await interaction.followup.send(
                    f"✅ Rejoined **{scene_row['title']}** as **{oc_row['name']}**.\n"
                    f"Previous scene slot: **{old_name}**.\n"
                    "If posts were missed while you were marked as left, run `/scene rescan`.",
                    ephemeral=True,
                )

            self.sb().table("rp_scene_participants").insert({
                "scene_id": scene_row["scene_id"],
                "character_id": oc_row["character_id"],
                "user_id": user_id,
                "character_name": oc_row["name"],
                "is_active": True,
            }).execute()

            return await interaction.followup.send(
                f"✅ Joined **{scene_row['title']}** as **{oc_row['name']}**.",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[scene join] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Could not join or rejoin this scene.",
                ephemeral=True,
            )

    @app_commands.command(name="leave", description="Leave the current tracked scene")
    async def scene_leave(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        user_id = int(interaction.user.id)

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            self.sb().table("rp_scene_participants").update({
                "is_active": False,
            }).eq("scene_id", scene_row["scene_id"]).eq("user_id", user_id).execute()

            return await interaction.followup.send(
                "✅ You left this tracked scene. Your previous posts remain logged.",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[scene leave] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error leaving scene.",
                ephemeral=True,
            )

    @app_commands.command(name="info", description="Show info and current totals for this tracked scene")
    async def scene_info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            parts = (
                self.sb()
                .table("rp_scene_participants")
                .select("character_id, character_name, user_id, is_active")
                .eq("scene_id", scene_row["scene_id"])
                .execute()
            )
            participants = getattr(parts, "data", None) or []

            posts = (
                self.sb()
                .table("rp_posts")
                .select("character_id, character_name, word_count")
                .eq("scene_id", scene_row["scene_id"])
                .execute()
            )
            post_rows = getattr(posts, "data", None) or []

            totals: dict[str, dict] = {}
            for p in participants:
                totals[p["character_id"]] = {
                    "name": p["character_name"],
                    "words": 0,
                    "posts": 0,
                    "active": p.get("is_active", True),
                }

            for post in post_rows:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "words": 0,
                        "posts": 0,
                        "active": True,
                    }
                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1

            lines = []
            for data in totals.values():
                active = "" if data["active"] else " *(left)*"
                xp = xp_from_words(data["words"]) if scene_row["xp_eligible"] else 0
                lines.append(
                    f"**{data['name']}**{active}: "
                    f"`{data['words']}` words, `{data['posts']}` posts, estimated `{xp}` XP"
                )

            embed = discord.Embed(
                title=f"Scene Info: {scene_row['title']}",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(name="Type", value=scene_row["scene_type"], inline=True)
            embed.add_field(name="XP Eligible", value="Yes" if scene_row["xp_eligible"] else "No", inline=True)
            embed.add_field(name="Status", value=scene_row["status"], inline=True)
            embed.add_field(
                name="Participants",
                value="\n".join(lines) if lines else "No participants yet.",
                inline=False,
            )

            return await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[scene info] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error fetching scene info.",
                ephemeral=True,
            )

    @app_commands.command(name="me", description="Show your own tracked totals for this scene")
    async def scene_me(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            user_id = int(interaction.user.id)

            participants = await self.get_participants_for_user(
                scene_row["scene_id"],
                user_id,
            )

            if not participants:
                return await interaction.followup.send(
                    "I do not see any tracked OC entries for you in this scene yet. "
                    "For event threads, post once through a Tupper whose name matches your registered OC.",
                    ephemeral=True,
                )

            character_ids = [p["character_id"] for p in participants]

            posts_res = (
                self.sb()
                .table("rp_posts")
                .select("character_id, character_name, word_count")
                .eq("scene_id", scene_row["scene_id"])
                .in_("character_id", character_ids)
                .execute()
            )
            posts = getattr(posts_res, "data", None) or []

            totals: dict[str, dict[str, Any]] = {}
            for p in participants:
                totals[p["character_id"]] = {
                    "name": p["character_name"],
                    "words": 0,
                    "posts": 0,
                }

            for post in posts:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "words": 0,
                        "posts": 0,
                    }
                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1

            lines = []
            for data in totals.values():
                xp = xp_from_words(data["words"]) if scene_row["xp_eligible"] else 0
                lines.append(
                    f"**{data['name']}** — `{data['words']}` words / "
                    f"`{data['posts']}` posts / estimated `{xp}` XP"
                )

            embed = discord.Embed(
                title=f"Your Scene Totals: {scene_row['title']}",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(
                name="Your OCs",
                value="\n".join(lines) if lines else "No tracked posts yet.",
                inline=False,
            )

            return await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[scene me] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error fetching your scene totals.",
                ephemeral=True,
            )

    @app_commands.command(name="rescan", description="Re-scan this scene and count missed RP posts")
    async def scene_rescan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        if not interaction.guild:
            return await interaction.followup.send(
                "Use this in a server, not DMs.",
                ephemeral=True,
            )

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            checked = 0
            saved = 0
            skipped = 0

            async for message in interaction.channel.history(
                limit=1000,
                oldest_first=True,
            ):
                checked += 1

                if not is_valid_rp_message(message):
                    skipped += 1
                    continue

                participant = await self.resolve_participant_for_message(
                    scene_row,
                    message,
                )

                if not participant:
                    skipped += 1
                    continue

                await self.save_tracked_post(
                    scene_row=scene_row,
                    message=message,
                    participant=participant,
                )
                saved += 1

            return await interaction.followup.send(
                f"✅ Scene rescan complete.\n"
                f"Checked: `{checked}` messages\n"
                f"Tracked/updated: `{saved}` posts\n"
                f"Skipped: `{skipped}` messages\n\n"
                "Run `/scene info` to confirm the updated totals.",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[scene rescan] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error rescanning this scene.",
                ephemeral=True,
            )

    @app_commands.command(name="close", description="Close the current scene and send RP XP approvals")
    async def scene_close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a tracked RP thread.",
                ephemeral=True,
            )

        user_id = int(interaction.user.id)

        try:
            scene_row = await self.get_active_scene_by_thread(int(interaction.channel.id))
            if not scene_row:
                return await interaction.followup.send(
                    "This is not an open tracked RP scene.",
                    ephemeral=True,
                )

            posts = (
                self.sb()
                .table("rp_posts")
                .select("character_id, character_name, user_id, word_count")
                .eq("scene_id", scene_row["scene_id"])
                .execute()
            )
            post_rows = getattr(posts, "data", None) or []

            totals: dict[str, dict] = {}
            for post in post_rows:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "user_id": post["user_id"],
                        "words": 0,
                        "posts": 0,
                    }
                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1

            self.sb().table("rp_scenes").update({
                "status": "closed",
                "closed_by": user_id,
                "closed_at": interaction.created_at.astimezone(timezone.utc).isoformat(),
            }).eq("scene_id", scene_row["scene_id"]).execute()

            approval_cards_sent = await self.send_scene_approval_claims(
                scene_row=scene_row,
                closed_by=user_id,
            )

            lines = []
            for data in totals.values():
                xp = xp_from_words(data["words"]) if scene_row["xp_eligible"] else 0
                lines.append(
                    f"**{data['name']}** — "
                    f"`{data['words']}` words / `{data['posts']}` posts / estimated `{xp}` XP"
                )

            embed = discord.Embed(
                title="RP Scene Closed",
                description=f"**{scene_row['title']}**",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Type", value=scene_row["scene_type"], inline=True)
            embed.add_field(
                name="XP Eligible",
                value="Yes" if scene_row["xp_eligible"] else "No",
                inline=True,
            )
            embed.add_field(
                name="Approval Cards Sent",
                value=f"`{approval_cards_sent}`",
                inline=True,
            )
            embed.add_field(
                name="Final Totals",
                value="\n".join(lines) if lines else "No tracked RP posts were logged.",
                inline=False,
            )
            embed.set_footer(text="Approval cards were sent to #rp-xp-approvals when XP was earned.")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[scene close] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error closing scene.",
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not isinstance(message.channel, discord.Thread):
            return

        if not message.guild:
            return

        if not is_valid_rp_message(message):
            return

        try:
            scene_row = await self.get_active_scene_by_thread(int(message.channel.id))
            if not scene_row:
                return

            participant = await self.resolve_participant_for_message(
                scene_row,
                message,
            )

            if not participant:
                return

            await self.save_tracked_post(
                scene_row=scene_row,
                message=message,
                participant=participant,
            )

        except Exception as e:
            print(f"[rp tracker on_message] error: {e}")
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not isinstance(after.channel, discord.Thread):
            return

        if not after.guild:
            return

        try:
            scene_row = await self.get_active_scene_by_thread(int(after.channel.id))
            if not scene_row:
                return

            if not is_valid_rp_message(after):
                await self.delete_tracked_post(int(after.id))
                return

            participant = await self.resolve_participant_for_message(
                scene_row,
                after,
            )

            if not participant:
                await self.delete_tracked_post(int(after.id))
                return

            await self.save_tracked_post(
                scene_row=scene_row,
                message=after,
                participant=participant,
            )

        except Exception as e:
            print(f"[rp tracker on_message_edit] error: {e}")
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        try:
            await self.delete_tracked_post(int(payload.message_id))

        except Exception as e:
            print(f"[rp tracker on_raw_message_delete] error: {e}")
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")

        if not custom_id.startswith("rp_xp:"):
            return

        try:
            parts = custom_id.split(":", 2)
            if len(parts) != 3:
                return

            action = parts[1]
            claim_id = parts[2]

            if action == "approve":
                claim = await self.fetch_claim(claim_id)
                if not claim:
                    return await interaction.response.send_message(
                        "I could not find that RP XP claim.",
                        ephemeral=True,
                    )

                await self.review_rp_xp_claim(
                    interaction=interaction,
                    claim_id=claim_id,
                    status="approved",
                    approved_xp=int(claim.get("estimated_xp") or 0),
                    reason="Approved as estimated.",
                )

            elif action == "adjust":
                await interaction.response.send_modal(RPXPAdjustModal(self, claim_id))

            elif action == "deny":
                await interaction.response.send_modal(RPXPDenyModal(self, claim_id))

            elif action == "reopen":
                await self.reopen_rp_xp_claim(
                    interaction=interaction,
                    claim_id=claim_id,
                )

        except Exception as e:
            print(f"[rp xp approval interaction] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error handling RP XP approval.",
                    ephemeral=True,
                )


class RPEventCog(
    RPTools,
    commands.GroupCog,
    group_name="event",
    group_description="Tracked RP event commands",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(name="open", description="Open an RP event umbrella in this channel")
    @app_commands.describe(
        title="Event title",
        xp_eligible="Whether scenes in this event can earn RP XP",
    )
    async def event_open(
        self,
        interaction: discord.Interaction,
        title: str,
        xp_eligible: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.followup.send(
                "Events can only be opened inside a normal server text channel.",
                ephemeral=True,
            )

        title = (title or "").strip()
        if not title or len(title) > 120:
            return await interaction.followup.send(
                "Event title must be between 1 and 120 characters.",
                ephemeral=True,
            )

        try:
            existing = await self.get_open_event_for_channel(
                int(interaction.guild.id),
                int(interaction.channel.id),
            )
            if existing:
                return await interaction.followup.send(
                    f"An event is already open in this channel: **{existing['title']}**.",
                    ephemeral=True,
                )

            ins = (
                self.sb()
                .table("rp_events")
                .insert({
                    "guild_id": int(interaction.guild.id),
                    "channel_id": int(interaction.channel.id),
                    "title": title,
                    "xp_eligible": bool(xp_eligible),
                    "auto_track_threads": True,
                    "status": "open",
                    "opened_by": int(interaction.user.id),
                })
                .execute()
            )

            rows = getattr(ins, "data", None) or []
            if not rows:
                return await interaction.followup.send(
                    "Could not open event. Supabase did not return a row.",
                    ephemeral=True,
                )

            event_row = rows[0]

            registered_count = 0
            for thread in interaction.channel.threads:
                scene_row = await self.register_thread_as_event_scene(
                    thread=thread,
                    event_row=event_row,
                    opened_by=int(interaction.user.id),
                )
                if scene_row:
                    registered_count += 1

            embed = discord.Embed(
                title="RP Event Opened",
                description=f"**{title}**",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            embed.add_field(name="XP Eligible", value="Yes" if xp_eligible else "No", inline=True)
            embed.add_field(name="Auto-Track Threads", value="Always On", inline=True)
            embed.add_field(
                name="Threads Registered Now",
                value=str(registered_count),
                inline=True,
            )
            embed.set_footer(
                text="New threads created in this channel will automatically be tracked."
            )

            return await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[event open] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error opening RP event.",
                ephemeral=True,
            )

    @app_commands.command(name="info", description="Show the open event status for this channel")
    async def event_info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            event_row = await self.get_event_from_context(interaction)
            if not event_row:
                return await interaction.followup.send(
                    "No open RP event found for this channel.",
                    ephemeral=True,
                )

            scenes_res = (
                self.sb()
                .table("rp_scenes")
                .select("scene_id,title,status,thread_id,xp_eligible")
                .eq("event_id", event_row["event_id"])
                .execute()
            )
            scenes = getattr(scenes_res, "data", None) or []
            scene_ids = [s["scene_id"] for s in scenes]

            posts = []
            if scene_ids:
                posts_res = (
                    self.sb()
                    .table("rp_posts")
                    .select("character_id,character_name,word_count,scene_id")
                    .in_("scene_id", scene_ids)
                    .execute()
                )
                posts = getattr(posts_res, "data", None) or []

            totals: dict[str, dict[str, Any]] = {}
            for post in posts:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "words": 0,
                        "posts": 0,
                    }
                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1

            open_count = sum(1 for s in scenes if s.get("status") == "open")
            closed_count = sum(1 for s in scenes if s.get("status") == "closed")

            top_lines = []
            for data in sorted(totals.values(), key=lambda x: x["words"], reverse=True)[:15]:
                xp = xp_from_words(data["words"]) if event_row["xp_eligible"] else 0
                top_lines.append(
                    f"**{data['name']}** — `{data['words']}` words / `{data['posts']}` posts / est. `{xp}` XP"
                )

            embed = discord.Embed(
                title=f"Event Info: {event_row['title']}",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(name="XP Eligible", value="Yes" if event_row["xp_eligible"] else "No", inline=True)
            embed.add_field(name="Auto-Track Threads", value="Always On", inline=True)
            embed.add_field(name="Tracked Threads", value=f"`{len(scenes)}` total / `{open_count}` open / `{closed_count}` closed", inline=False)
            embed.add_field(
                name="Current Event Totals",
                value="\n".join(top_lines) if top_lines else "No tracked posts yet.",
                inline=False,
            )

            return await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[event info] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error fetching event info.",
                ephemeral=True,
            )

    @app_commands.command(name="me", description="Show your own tracked totals for this event")
    async def event_me(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            event_row = await self.get_event_from_context(interaction)
            if not event_row:
                return await interaction.followup.send(
                    "No open RP event found for this channel.",
                    ephemeral=True,
                )

            user_id = int(interaction.user.id)

            scenes_res = (
                self.sb()
                .table("rp_scenes")
                .select("scene_id,title,status,thread_id,xp_eligible")
                .eq("event_id", event_row["event_id"])
                .execute()
            )
            scenes = getattr(scenes_res, "data", None) or []
            scene_ids = [s["scene_id"] for s in scenes]

            if not scene_ids:
                return await interaction.followup.send(
                    "This event has no tracked threads yet.",
                    ephemeral=True,
                )

            participants_res = (
                self.sb()
                .table("rp_scene_participants")
                .select("scene_id,character_id,character_name,user_id")
                .in_("scene_id", scene_ids)
                .eq("user_id", user_id)
                .execute()
            )
            participants = getattr(participants_res, "data", None) or []

            if not participants:
                return await interaction.followup.send(
                    "I do not see any tracked OC entries for you in this event yet. "
                    "Post once through a Tupper whose name matches your registered OC.",
                    ephemeral=True,
                )

            character_ids = list({p["character_id"] for p in participants})

            posts_res = (
                self.sb()
                .table("rp_posts")
                .select("character_id,character_name,word_count,scene_id")
                .in_("scene_id", scene_ids)
                .in_("character_id", character_ids)
                .execute()
            )
            posts = getattr(posts_res, "data", None) or []

            totals: dict[str, dict[str, Any]] = {}
            for p in participants:
                cid = p["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": p["character_name"],
                        "words": 0,
                        "posts": 0,
                        "scenes": set(),
                    }

            for post in posts:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "words": 0,
                        "posts": 0,
                        "scenes": set(),
                    }

                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1
                totals[cid]["scenes"].add(post["scene_id"])

            lines = []
            for data in sorted(totals.values(), key=lambda x: x["words"], reverse=True):
                xp = xp_from_words(data["words"]) if event_row["xp_eligible"] else 0
                lines.append(
                    f"**{data['name']}** — `{data['words']}` words / "
                    f"`{data['posts']}` posts / `{len(data['scenes'])}` threads / est. `{xp}` XP"
                )

            embed = discord.Embed(
                title=f"Your Event Totals: {event_row['title']}",
                color=discord.Color.dark_teal(),
            )
            embed.add_field(
                name="Your OCs",
                value="\n".join(lines) if lines else "No tracked posts yet.",
                inline=False,
            )

            return await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[event me] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error fetching your event totals.",
                ephemeral=True,
            )

    @app_commands.command(name="close", description="Close the open RP event and send RP XP approvals")
    async def event_close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        try:
            event_row = await self.get_event_from_context(interaction)
            if not event_row:
                return await interaction.followup.send(
                    "No open RP event found for this channel.",
                    ephemeral=True,
                )

            now_iso = interaction.created_at.astimezone(timezone.utc).isoformat()
            user_id = int(interaction.user.id)

            scenes_res = (
                self.sb()
                .table("rp_scenes")
                .select("scene_id,title,status,thread_id,xp_eligible")
                .eq("event_id", event_row["event_id"])
                .execute()
            )
            scenes = getattr(scenes_res, "data", None) or []
            scene_ids = [s["scene_id"] for s in scenes]

            if scene_ids:
                (
                    self.sb()
                    .table("rp_scenes")
                    .update({
                        "status": "closed",
                        "closed_by": user_id,
                        "closed_at": now_iso,
                    })
                    .eq("event_id", event_row["event_id"])
                    .eq("status", "open")
                    .execute()
                )

            (
                self.sb()
                .table("rp_events")
                .update({
                    "status": "closed",
                    "closed_by": user_id,
                    "closed_at": now_iso,
                })
                .eq("event_id", event_row["event_id"])
                .execute()
            )

            posts = []
            if scene_ids:
                posts_res = (
                    self.sb()
                    .table("rp_posts")
                    .select("character_id,character_name,word_count,scene_id")
                    .in_("scene_id", scene_ids)
                    .execute()
                )
                posts = getattr(posts_res, "data", None) or []

            totals: dict[str, dict[str, Any]] = {}
            for post in posts:
                cid = post["character_id"]
                if cid not in totals:
                    totals[cid] = {
                        "name": post["character_name"],
                        "words": 0,
                        "posts": 0,
                    }
                totals[cid]["words"] += int(post.get("word_count") or 0)
                totals[cid]["posts"] += 1

            approval_cards_sent = await self.send_event_approval_claims(
                event_row=event_row,
                scenes=scenes,
                closed_by=user_id,
            )

            lines = []
            for data in sorted(totals.values(), key=lambda x: x["words"], reverse=True)[:25]:
                xp = xp_from_words(data["words"]) if event_row["xp_eligible"] else 0
                lines.append(
                    f"**{data['name']}** — `{data['words']}` words / `{data['posts']}` posts / est. `{xp}` XP"
                )

            embed = discord.Embed(
                title="RP Event Closed",
                description=f"**{event_row['title']}**",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Tracked Threads Finalized", value=f"`{len(scenes)}`", inline=True)
            embed.add_field(name="XP Eligible", value="Yes" if event_row["xp_eligible"] else "No", inline=True)
            embed.add_field(name="Approval Cards Sent", value=f"`{approval_cards_sent}`", inline=True)
            embed.add_field(
                name="Final Event Totals",
                value="\n".join(lines) if lines else "No tracked RP posts were logged.",
                inline=False,
            )
            embed.set_footer(text="Approval cards were sent to #rp-xp-approvals when XP was earned.")

            return await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[event close] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error closing RP event.",
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if not thread.guild or not thread.parent_id:
            return

        try:
            event_row = await self.get_open_event_for_channel(
                int(thread.guild.id),
                int(thread.parent_id),
            )

            if not event_row:
                return

            await self.register_thread_as_event_scene(
                thread=thread,
                event_row=event_row,
                opened_by=int(getattr(thread, "owner_id", 0) or 0),
            )

        except Exception as e:
            print(f"[event on_thread_create] error: {e}")
            traceback.print_exc()


async def setup(bot: commands.Bot):
    await bot.add_cog(RPTrackerCog(bot))
    await bot.add_cog(RPEventCog(bot))