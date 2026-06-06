from __future__ import annotations

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

CANON_FICTION_FORUM_ID = int(os.getenv("CANON_FICTION_FORUM_ID", "0") or 0)
JOURNAL_XP_WORDS_PER_CHUNK = int(os.getenv("JOURNAL_XP_WORDS_PER_CHUNK", "300") or 300)
JOURNAL_XP_PER_CHUNK = int(os.getenv("JOURNAL_XP_PER_CHUNK", "3") or 3)

RP_XP_APPROVAL_CHANNEL_ID = int(os.getenv("RP_XP_APPROVAL_CHANNEL_ID", "0") or 0)
RP_XP_AUDIT_CHANNEL_ID = int(os.getenv("RP_XP_AUDIT_CHANNEL_ID", "1473718234174718109") or 0)


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


def journal_xp_from_words(words: int) -> int:
    chunks = words // JOURNAL_XP_WORDS_PER_CHUNK
    return chunks * JOURNAL_XP_PER_CHUNK


def is_valid_journal_message(message: discord.Message) -> bool:
    if message.author.bot and message.webhook_id is None:
        return False

    content = (message.content or "").strip()
    if not content:
        return False

    ignored_prefixes = ("//", "((", "[[", "ooc:", "OOC:", "!")
    if content.startswith(ignored_prefixes):
        return False

    return count_words(content) >= 5


def thread_jump_url(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def message_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class JournalXPAdjustModal(discord.ui.Modal):
    def __init__(self, cog: "JournalTrackerCog", claim_id: str):
        super().__init__(title="Adjust Canon Fiction XP")
        self.cog = cog
        self.claim_id = claim_id

        self.approved_xp = discord.ui.TextInput(
            label="Approved XP",
            placeholder="Example: 12",
            required=True,
            max_length=8,
        )
        self.reason = discord.ui.TextInput(
            label="Reason",
            placeholder="Example: Adjusted for OOC text / partial eligibility.",
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

            await self.cog.review_journal_claim(
                interaction=interaction,
                claim_id=self.claim_id,
                status="approved",
                approved_xp=int(raw_xp),
                reason=str(self.reason.value or "").strip(),
            )

        except Exception as e:
            print(f"[journal xp adjust modal] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error adjusting Canon Fiction XP claim.",
                    ephemeral=True,
                )


class JournalXPDenyModal(discord.ui.Modal):
    def __init__(self, cog: "JournalTrackerCog", claim_id: str):
        super().__init__(title="Deny Canon Fiction XP")
        self.cog = cog
        self.claim_id = claim_id

        self.reason = discord.ui.TextInput(
            label="Denial reason",
            placeholder="Example: This post is not XP eligible.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )

        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.cog.review_journal_claim(
                interaction=interaction,
                claim_id=self.claim_id,
                status="denied",
                approved_xp=None,
                reason=str(self.reason.value or "").strip(),
            )

        except Exception as e:
            print(f"[journal xp deny modal] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error denying Canon Fiction XP claim.",
                    ephemeral=True,
                )


class JournalTrackerCog(
    commands.GroupCog,
    group_name="journal",
    group_description="Canon Fiction / journal XP commands",
):
    """Canon Fiction / solo writing XP tracker.

    Players use `/journal count` or `/journal submit` inside a Canon Fiction
    forum post/thread. The cog counts eligible writing in the thread, creates
    an approval card, and pays through XPService after staff approval.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

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

    def build_claim_view(self, claim_row: dict) -> discord.ui.View:
        claim_id = claim_row["claim_id"]
        status = claim_row.get("status", "pending")

        view = discord.ui.View(timeout=None)

        view.add_item(discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"journal_xp:approve:{claim_id}",
            disabled=status != "pending",
        ))
        view.add_item(discord.ui.Button(
            label="Adjust XP",
            style=discord.ButtonStyle.primary,
            custom_id=f"journal_xp:adjust:{claim_id}",
            disabled=status != "pending",
        ))
        view.add_item(discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"journal_xp:deny:{claim_id}",
            disabled=status != "pending",
        ))

        if status == "denied":
            view.add_item(discord.ui.Button(
                label="Reopen",
                style=discord.ButtonStyle.secondary,
                custom_id=f"journal_xp:reopen:{claim_id}",
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

        character_name = claim_row.get("character_name") or "Unknown OC"
        guild_id = int(claim_row.get("guild_id") or 0)
        journal_thread_id = int(claim_row.get("journal_thread_id") or 0)

        embed = discord.Embed(
            title=f"Canon Fiction XP — {status_label}",
            description=f"**{character_name}**",
            color=color,
        )

        embed.add_field(name="Words", value=f"`{claim_row.get('word_count', 0)}`", inline=True)
        embed.add_field(name="Posts Counted", value=f"`{claim_row.get('post_count', 0)}`", inline=True)
        embed.add_field(name="Rate", value=f"`{JOURNAL_XP_WORDS_PER_CHUNK}` words = `{JOURNAL_XP_PER_CHUNK}` XP", inline=True)

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
        embed.add_field(name="XP Payout", value=payout_text, inline=True)

        if guild_id and journal_thread_id:
            embed.add_field(
                name="Canon Fiction Post",
                value=f"[Jump to post]({thread_jump_url(guild_id, journal_thread_id)})",
                inline=False,
            )

        reason = claim_row.get("review_reason")
        if reason:
            embed.add_field(name="Review Reason", value=str(reason)[:1024], inline=False)

        reviewed_by = claim_row.get("reviewed_by")
        if reviewed_by:
            embed.add_field(name="Reviewed By", value=f"<@{reviewed_by}>", inline=True)

        embed.set_footer(
            text=f"Claim ID: {str(claim_row.get('claim_id'))[:8]} • Canon Fiction / journal XP"
        )

        return embed

    def build_audit_embed(
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
        guild_id = int(claim_row.get("guild_id") or 0)
        thread_id = int(claim_row.get("journal_thread_id") or 0)

        embed = discord.Embed(
            title=f"Canon Fiction XP {action_label}",
            description=f"**{character_name}**",
            color=color,
        )

        embed.add_field(name="Action By", value=f"<@{actor_id}>", inline=True)
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

        if guild_id and thread_id:
            embed.add_field(
                name="Canon Fiction Post",
                value=f"[Jump to post]({thread_jump_url(guild_id, thread_id)})",
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

        if reason:
            embed.add_field(name="Reason", value=str(reason)[:1024], inline=False)

        embed.set_footer(text=f"Claim ID: {claim_row.get('claim_id')}")
        return embed

    async def send_audit_log(
        self,
        *,
        claim_row: dict,
        action_label: str,
        actor_id: int,
        reason: str | None = None,
    ):
        channel = await self.get_audit_channel()
        if channel is None:
            print("[journal xp audit] Audit channel not found or not configured.")
            return

        try:
            await channel.send(embed=self.build_audit_embed(
                claim_row=claim_row,
                action_label=action_label,
                actor_id=actor_id,
                reason=reason,
            ))
        except Exception as e:
            print(f"[journal xp audit] error: {e}")
            traceback.print_exc()

    async def dispatch_approval_card(self, claim_row: dict) -> bool:
        channel = await self.get_approval_channel()
        if channel is None:
            print("[journal xp approval] Approval channel not found or not configured.")
            return False

        msg = await channel.send(
            embed=self.build_claim_embed(claim_row),
            view=self.build_claim_view(claim_row),
        )

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

    async def fetch_claim(self, claim_id: str):
        res = (
            self.sb()
            .table("rp_xp_claims")
            .select("*")
            .eq("claim_id", claim_id)
            .eq("claim_type", "journal")
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
                view=self.build_claim_view(claim_row),
            )

        except Exception as e:
            print(f"[journal xp approval refresh] error: {e}")
            traceback.print_exc()

    def build_xp_award_notes(self, claim: dict, reason: str | None) -> str:
        base = f"Approved Canon Fiction XP claim {claim.get('claim_id')}"
        if reason:
            base += f". Review reason: {reason}"
        return base[:1000]

    async def payout_journal_claim(
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

        return xp_service.award_xp(
            guild_id=int(claim["guild_id"]),
            character_id=str(claim["character_id"]),
            amount=int(approved_xp),
            source="rp",
            title=f"Canon Fiction XP: {claim.get('character_name') or 'OC'}",
            actor_discord_id=int(actor_id),
            external_ref=f"journal_xp_claim:{claim['claim_id']}",
            notes=self.build_xp_award_notes(claim, reason),
        )

    async def review_journal_claim(
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
                "I could not find that Canon Fiction XP claim.",
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

        payout_error: str | None = None
        payout_result: dict[str, Any] | None = None

        if status == "approved":
            final_xp = int(approved_xp if approved_xp is not None else claim["estimated_xp"])
            if final_xp <= 0:
                return await interaction.response.send_message(
                    "Approved XP must be greater than 0.",
                    ephemeral=True,
                )

            try:
                payout_result = await self.payout_journal_claim(
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
                failed_update = (
                    self.sb()
                    .table("rp_xp_claims")
                    .update({
                        "payout_status": "failed",
                        "payout_error": payout_error,
                    })
                    .eq("claim_id", claim_id)
                    .execute()
                )
                failed_rows = getattr(failed_update, "data", None) or []
                failed_claim = failed_rows[0] if failed_rows else await self.fetch_claim(claim_id)

                if failed_claim:
                    await self.refresh_claim_message(failed_claim)
                    await self.send_audit_log(
                        claim_row=failed_claim,
                        action_label="Payout Failed",
                        actor_id=actor_id,
                        reason=payout_error,
                    )

                return await interaction.response.send_message(
                    f"❌ I could not pay this Canon Fiction XP claim: {payout_error}",
                    ephemeral=True,
                )

            tx_id = payout_result.get("xp_tx_id") if payout_result else None
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

            await self.send_audit_log(
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
                    f"❌ Denied Canon Fiction XP for **{claim['character_name']}**.",
                    ephemeral=True,
                )

    async def reopen_journal_claim(
        self,
        *,
        interaction: discord.Interaction,
        claim_id: str,
    ):
        claim = await self.fetch_claim(claim_id)
        if not claim:
            return await interaction.response.send_message(
                "I could not find that Canon Fiction XP claim.",
                ephemeral=True,
            )

        if claim.get("status") != "denied":
            return await interaction.response.send_message(
                "Only denied Canon Fiction XP claims can be reopened.",
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
            await self.send_audit_log(
                claim_row=updated,
                action_label="Reopened",
                actor_id=int(interaction.user.id),
                reason=reason,
            )

        return await interaction.response.send_message(
            f"🔁 Reopened Canon Fiction XP claim for **{claim['character_name']}**.",
            ephemeral=True,
        )

    async def count_author_posts_in_thread(
        self,
        *,
        thread: discord.Thread,
        author_id: int,
        oc_name: str | None = None,
    ) -> tuple[int, int]:
        """Count eligible Canon Fiction writing in a forum thread.

        Normal Discord posts are matched by the player's Discord ID.
        Tupper/webhook posts are matched by OC name, because their author is the
        webhook/bot rather than the player.
        """
        words = 0
        posts = 0
        target_oc = normalize_oc_name(oc_name or "")

        async for message in thread.history(limit=1000, oldest_first=True):
            if not is_valid_journal_message(message):
                continue

            is_user_message = int(message.author.id) == int(author_id)

            is_matching_tupper = False
            if target_oc and message.webhook_id is not None:
                is_matching_tupper = normalize_oc_name(get_message_display_name(message)) == target_oc

            if not is_user_message and not is_matching_tupper:
                continue

            words += count_words(message.content)
            posts += 1

        return words, posts

    async def find_existing_journal_claim(
        self,
        *,
        guild_id: int,
        journal_thread_id: int,
        character_id: str,
    ):
        res = (
            self.sb()
            .table("rp_xp_claims")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("claim_type", "journal")
            .eq("journal_thread_id", int(journal_thread_id))
            .eq("character_id", character_id)
            .limit(1)
            .execute()
        )

        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def is_canon_fiction_thread(self, thread: discord.Thread) -> bool:
        if not CANON_FICTION_FORUM_ID:
            return True
        return int(thread.parent_id or 0) == CANON_FICTION_FORUM_ID

    @app_commands.command(name="count", description="Count your Canon Fiction / journal words in this post")
    @app_commands.describe(
        oc="Optional: your OC name. Use this when the post was made through Tupper.",
    )
    @app_commands.autocomplete(oc=oc_name_autocomplete)
    async def journal_count(self, interaction: discord.Interaction, oc: str | None = None):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside a Canon Fiction forum post/thread.",
                ephemeral=True,
            )

        if not self.is_canon_fiction_thread(interaction.channel):
            return await interaction.followup.send(
                "This does not look like the configured Canon Fiction forum.",
                ephemeral=True,
            )

        try:
            oc_row = None
            oc_name = None

            if oc:
                oc_row = await self.get_owned_oc_by_name(int(interaction.user.id), oc)
                if not oc_row:
                    return await interaction.followup.send(
                        "OC not found, or that OC is not yours. Make sure the OC is registered with Keystone.",
                        ephemeral=True,
                    )
                oc_name = oc_row["name"]

            words, posts = await self.count_author_posts_in_thread(
                thread=interaction.channel,
                author_id=int(interaction.user.id),
                oc_name=oc_name,
            )

            estimated_xp = journal_xp_from_words(words)

            note = ""
            if not oc_name and words == 0:
                note = (
                    "\n\nIf this post was made through Tupper, run `/journal count` again "
                    "and pick the OC so Keystone can match the Tupper name."
                )

            label = f" for **{oc_name}**" if oc_name else f" for **{interaction.user.display_name}**"
            return await interaction.followup.send(
                f"Canon Fiction count{label}:\n"
                f"Words: `{words}`\n"
                f"Posts counted: `{posts}`\n"
                f"Estimated XP: `{estimated_xp}`\n"
                f"Rate: `{JOURNAL_XP_WORDS_PER_CHUNK}` words = `{JOURNAL_XP_PER_CHUNK}` XP"
                f"{note}",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[journal count] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error while counting this Canon Fiction post.",
                ephemeral=True,
            )

    @app_commands.command(name="submit", description="Submit this Canon Fiction / journal post for XP approval")
    @app_commands.describe(oc="Your registered OC for this Canon Fiction post")
    @app_commands.autocomplete(oc=oc_name_autocomplete)
    async def journal_submit(self, interaction: discord.Interaction, oc: str):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.followup.send(
                "Use this inside the Canon Fiction forum post/thread you want to submit.",
                ephemeral=True,
            )

        if not interaction.guild:
            return await interaction.followup.send(
                "Use this in the server, not DMs.",
                ephemeral=True,
            )

        if not self.is_canon_fiction_thread(interaction.channel):
            return await interaction.followup.send(
                "This does not look like the configured Canon Fiction forum.",
                ephemeral=True,
            )

        try:
            oc_row = await self.get_owned_oc_by_name(int(interaction.user.id), oc)
            if not oc_row:
                return await interaction.followup.send(
                    "OC not found, or that OC is not yours. Make sure the OC is registered with Keystone.",
                    ephemeral=True,
                )

            words, posts = await self.count_author_posts_in_thread(
                thread=interaction.channel,
                author_id=int(interaction.user.id),
                oc_name=oc_row["name"],
            )

            estimated_xp = journal_xp_from_words(words)

            if estimated_xp <= 0:
                return await interaction.followup.send(
                    f"This post currently has `{words}` eligible words for **{oc_row['name']}**.\n"
                    f"Canon Fiction XP requires at least `{JOURNAL_XP_WORDS_PER_CHUNK}` words "
                    f"for `{JOURNAL_XP_PER_CHUNK}` XP.",
                    ephemeral=True,
                )

            existing = await self.find_existing_journal_claim(
                guild_id=int(interaction.guild.id),
                journal_thread_id=int(interaction.channel.id),
                character_id=oc_row["character_id"],
            )

            if existing:
                if existing.get("approval_message_id"):
                    return await interaction.followup.send(
                        f"A Canon Fiction XP claim already exists for **{oc_row['name']}** in this post "
                        f"with status **{existing.get('status')}**.",
                        ephemeral=True,
                    )

                ok = await self.dispatch_approval_card(existing)
                if ok:
                    return await interaction.followup.send(
                        "✅ Existing Canon Fiction XP claim found and approval card resent.",
                        ephemeral=True,
                    )

                return await interaction.followup.send(
                    "A claim already exists, but I could not send the approval card. "
                    "Check `RP_XP_APPROVAL_CHANNEL_ID` in Railway.",
                    ephemeral=True,
                )

            ins = (
                self.sb()
                .table("rp_xp_claims")
                .insert({
                    "guild_id": int(interaction.guild.id),
                    "claim_type": "journal",
                    "scene_id": None,
                    "event_id": None,
                    "journal_forum_id": int(interaction.channel.parent_id or 0) or CANON_FICTION_FORUM_ID or None,
                    "journal_thread_id": int(interaction.channel.id),
                    "journal_message_id": int(interaction.channel.id),
                    "character_id": oc_row["character_id"],
                    "user_id": int(interaction.user.id),
                    "character_name": oc_row["name"],
                    "word_count": int(words),
                    "post_count": int(posts),
                    "estimated_xp": int(estimated_xp),
                    "approved_xp": None,
                    "status": "pending",
                    "locations": [{
                        "title": str(interaction.channel.name or "Canon Fiction Post"),
                        "thread_id": int(interaction.channel.id),
                        "words": int(words),
                        "posts": int(posts),
                    }],
                    "created_by": int(interaction.user.id),
                })
                .execute()
            )

            rows = getattr(ins, "data", None) or []
            claim = rows[0] if rows else None

            if not claim:
                return await interaction.followup.send(
                    "I could not create the Canon Fiction XP claim.",
                    ephemeral=True,
                )

            sent = await self.dispatch_approval_card(claim)

            if sent:
                return await interaction.followup.send(
                    f"✅ Canon Fiction XP submitted for **{oc_row['name']}**.\n"
                    f"Words: `{words}` / Posts counted: `{posts}` / Estimated XP: `{estimated_xp}`\n"
                    f"Sent to staff for approval.",
                    ephemeral=True,
                )

            return await interaction.followup.send(
                f"⚠️ Claim saved, but I could not send the approval card.\n"
                f"Words: `{words}` / Posts counted: `{posts}` / Estimated XP: `{estimated_xp}`\n"
                "Check `RP_XP_APPROVAL_CHANNEL_ID` in Railway.",
                ephemeral=True,
            )

        except Exception as e:
            print(f"[journal submit] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error while submitting Canon Fiction XP.",
                ephemeral=True,
            )

    # Prefix fallbacks kept for emergencies while Discord slash command sync catches up.
    @commands.command(name="journal_count")
    async def journal_count_prefix(self, ctx: commands.Context, *, oc: str | None = None):
        return await ctx.reply(
            "Use `/journal count` now. If this is a Tupper post, pick your OC in the command option.",
            mention_author=False,
        )

    @commands.command(name="journal_submit")
    async def journal_submit_prefix(self, ctx: commands.Context, *, oc: str):
        return await ctx.reply(
            "Use `/journal submit` now and pick your OC in the command option.",
            mention_author=False,
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")

        if not custom_id.startswith("journal_xp:"):
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
                        "I could not find that Canon Fiction XP claim.",
                        ephemeral=True,
                    )

                await self.review_journal_claim(
                    interaction=interaction,
                    claim_id=claim_id,
                    status="approved",
                    approved_xp=int(claim.get("estimated_xp") or 0),
                    reason="Approved as estimated.",
                )

            elif action == "adjust":
                await interaction.response.send_modal(JournalXPAdjustModal(self, claim_id))

            elif action == "deny":
                await interaction.response.send_modal(JournalXPDenyModal(self, claim_id))

            elif action == "reopen":
                await self.reopen_journal_claim(
                    interaction=interaction,
                    claim_id=claim_id,
                )

        except Exception as e:
            print(f"[journal xp interaction] error: {e}")
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Server error handling Canon Fiction XP approval.",
                    ephemeral=True,
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(JournalTrackerCog(bot))
