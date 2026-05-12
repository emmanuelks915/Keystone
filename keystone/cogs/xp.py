# cogs/xp.py
# XP / Progression commands for Keystone
# OC-based XP wallet + audit history + stat buying

import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.oc_service import get_active_character
from services.xp_service import (
    XPDuplicateAwardError,
    XPInsufficientError,
    XPService,
    XPServiceError,
    XPValidationError,
)

STAT_CHOICES = [
    app_commands.Choice(name="Strength", value="strength"),
    app_commands.Choice(name="Dexterity", value="dexterity"),
    app_commands.Choice(name="Stamina", value="stamina"),
    app_commands.Choice(name="Fortitude", value="fortitude"),
    app_commands.Choice(name="Affinity", value="affinity"),
    app_commands.Choice(name="Luck", value="luck"),
]

XP_SOURCE_CHOICES = [
    app_commands.Choice(name="Mission", value="mission"),
    app_commands.Choice(name="Event", value="event"),
    app_commands.Choice(name="Job", value="job"),
    app_commands.Choice(name="RP", value="rp"),
    app_commands.Choice(name="Bonus", value="bonus"),
    app_commands.Choice(name="Staff", value="staff"),
]


def _parse_dev_ids(bot: commands.Bot) -> set[int]:
    import os

    raw = (os.getenv("DEV_USER_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


class XPCog(commands.GroupCog, group_name="xp", group_description="XP and progression commands"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.xp = XPService(self.sb())
        super().__init__()

    # ── Supabase ──────────────────────────────────────────────────────────────
    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    # ── Permissions ───────────────────────────────────────────────────────────
    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False

        if isinstance(interaction.user, discord.Member):
            if interaction.user.guild_permissions.administrator:
                return True

            staff_roles: set[int] = getattr(self.bot, "staff_role_ids", set())
            if any(r.id in staff_roles for r in interaction.user.roles):
                return True

        if int(interaction.user.id) in _parse_dev_ids(self.bot):
            return True

        return False

    # ── Reply helpers ─────────────────────────────────────────────────────────
    async def _private(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ):
        kwargs = {"content": content, "embed": embed, "ephemeral": ephemeral}
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)

    # ── Autocomplete ──────────────────────────────────────────────────────────
    async def _character_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []

        sb = self.sb()
        q = (current or "").strip().lower()

        try:
            res = (
                sb.table("characters")
                .select("character_id,name,user_id")
                .order("name", desc=False)
                .limit(100)
                .execute()
            )
            rows = getattr(res, "data", None) or []

            out: list[app_commands.Choice[str]] = []
            for row in rows:
                character_id = str(row.get("character_id") or "")
                name = str(row.get("name") or "").strip()
                user_id = str(row.get("user_id") or "").strip()

                if not character_id or not name:
                    continue

                searchable = f"{name} {character_id} {user_id}".lower()
                if q and q not in searchable:
                    continue

                label = f"{name[:60]} • {character_id[:8]}"
                if user_id:
                    label += f" • {user_id}"

                out.append(
                    app_commands.Choice(
                        name=label[:100],
                        value=character_id,
                    )
                )

            return out[:25]
        except Exception:
            traceback.print_exc()
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # /xp balance
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="View XP balance for your active OC or another OC")
    @app_commands.autocomplete(character=_character_autocomplete)
    @app_commands.describe(character="Optional OC to inspect (staff/admin convenience)")
    async def balance(self, interaction: discord.Interaction, character: Optional[str] = None):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            if character:
                character_id = str(character)
                oc_name = self.xp.get_character_name(guild_id, character_id)
            else:
                active = get_active_character(sb, int(interaction.user.id))
                if not active:
                    return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")
                character_id = str(active.get("character_id") or "")
                oc_name = str(active.get("name") or "Unknown OC")

            wallet = self.xp.get_wallet(guild_id, character_id)

            embed = discord.Embed(title="✨ XP Balance", color=discord.Color.blurple())
            embed.add_field(name="OC", value=f"**{oc_name}**", inline=False)
            embed.add_field(name="Available XP", value=f"`{int(wallet.get('available_xp') or 0)}`", inline=True)
            embed.add_field(name="Total Earned", value=f"`{int(wallet.get('total_earned_xp') or 0)}`", inline=True)
            embed.add_field(name="Total Spent", value=f"`{int(wallet.get('total_spent_xp') or 0)}`", inline=True)
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except XPServiceError:
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching XP balance.")
        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching XP balance.")

    # ──────────────────────────────────────────────────────────────────────────
    # /xp history
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="history", description="View recent XP transactions for your active OC or another OC")
    @app_commands.autocomplete(character=_character_autocomplete)
    @app_commands.describe(character="Optional OC to inspect", limit="How many entries to show (max 15)")
    async def history(self, interaction: discord.Interaction, character: Optional[str] = None, limit: int = 10):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        limit = max(1, min(15, int(limit)))

        try:
            if character:
                character_id = str(character)
                oc_name = self.xp.get_character_name(guild_id, character_id)
            else:
                active = get_active_character(sb, int(interaction.user.id))
                if not active:
                    return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")
                character_id = str(active.get("character_id") or "")
                oc_name = str(active.get("name") or "Unknown OC")

            rows = self.xp.get_history(guild_id, character_id, limit=limit)

            if not rows:
                return await self._private(interaction, f"No XP history found for **{oc_name}**.")

            embed = discord.Embed(title="🧾 XP History", color=discord.Color.dark_teal())
            embed.description = f"Recent XP activity for **{oc_name}**"

            for row in rows:
                direction = str(row.get("direction") or "?").upper()
                amount = int(row.get("amount") or 0)
                source = str(row.get("source") or "unknown")
                ref_type = str(row.get("reference_type") or "").strip()
                ref_key = str(row.get("reference_key") or "").strip()
                reason = str(row.get("reason") or "").strip()
                created_at = str(row.get("created_at") or "")[:19].replace("T", " ")

                sign = "+" if direction == "EARN" else "-"
                extra = ""
                if ref_type or ref_key:
                    extra = f"\nRef: `{ref_type or '—'}` / `{ref_key or '—'}`"
                if reason:
                    extra += f"\n{reason}"

                embed.add_field(
                    name=f"{sign}{amount} XP • {source}",
                    value=f"`{created_at}`{extra}",
                    inline=False,
                )

            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=True)

        except XPServiceError:
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching XP history.")
        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error fetching XP history.")

    # ──────────────────────────────────────────────────────────────────────────
    # /xp award
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="award", description="Staff: Award XP to an OC")
    @app_commands.autocomplete(character=_character_autocomplete)
    @app_commands.choices(source=XP_SOURCE_CHOICES)
    @app_commands.describe(
        character="Which OC gets the XP",
        amount="How much XP to award",
        source="Why they are getting XP",
        title="Short title for the award",
        external_ref="Optional mission/event/job reference",
        notes="Optional notes"
    )
    async def award(
        self,
        interaction: discord.Interaction,
        character: str,
        amount: int,
        source: app_commands.Choice[str],
        title: str,
        external_ref: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private(interaction, "Amount must be greater than 0.")
        if not title.strip():
            return await self._private(interaction, "Title cannot be blank.")

        guild_id = int(interaction.guild.id)
        character_id = str(character)

        try:
            oc_name = self.xp.get_character_name(guild_id, character_id)

            result = self.xp.award_xp(
                guild_id=guild_id,
                character_id=character_id,
                amount=amount,
                source=source.value,
                title=title.strip(),
                actor_discord_id=int(interaction.user.id),
                external_ref=external_ref,
                notes=notes,
            )
            tx_id = result["xp_tx_id"]
            wallet = result["wallet"]

            embed = discord.Embed(title="✅ XP Awarded", color=discord.Color.green())
            embed.add_field(name="OC", value=f"**{oc_name}**", inline=False)
            embed.add_field(name="Amount", value=f"`{amount}` XP", inline=True)
            embed.add_field(name="Source", value=f"`{source.value}`", inline=True)
            embed.add_field(name="Title", value=title.strip(), inline=False)
            if external_ref:
                embed.add_field(name="Reference", value=f"`{external_ref}`", inline=False)
            if notes:
                embed.add_field(name="Notes", value=notes[:1000], inline=False)
            embed.add_field(name="New Balance", value=f"`{int(wallet.get('available_xp') or 0)}` XP", inline=False)
            if tx_id:
                embed.set_footer(text=f"TX: {str(tx_id)[:8]}")
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except XPDuplicateAwardError:
            return await self._private(interaction, "That XP award looks like a duplicate and was blocked.")
        except XPValidationError as e:
            return await self._private(interaction, f"❌ {e}")
        except XPServiceError:
            traceback.print_exc()
            return await self._private(interaction, "Server error awarding XP.")
        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error awarding XP.")

    # ──────────────────────────────────────────────────────────────────────────
    # /xp buy_stat
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="buy_stat", description="Spend XP to increase a stat on your active OC")
    @app_commands.choices(stat=STAT_CHOICES)
    @app_commands.describe(stat="Which stat to increase", points="How many points to buy")
    async def buy_stat(self, interaction: discord.Interaction, stat: app_commands.Choice[str], points: int = 1):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if points <= 0:
            return await self._private(interaction, "Points must be greater than 0.")
        if points > 100:
            return await self._private(interaction, "Too many points at once (max 100).")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            active = get_active_character(sb, int(interaction.user.id))
            if not active:
                return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")

            character_id = str(active.get("character_id") or "")
            oc_name = str(active.get("name") or "Unknown OC")

            result = self.xp.buy_stat_points(
                guild_id=guild_id,
                character_id=character_id,
                stat_key=stat.value,
                points=points,
                actor_discord_id=int(interaction.user.id),
            )

            old_value = int(result["old_value"])
            new_value = int(result["new_value"])
            wallet = result["wallet"]
            cost_paid = result["xp_cost"]

            embed = discord.Embed(title="📈 Stat Increased", color=discord.Color.gold())
            embed.add_field(name="OC", value=f"**{oc_name}**", inline=False)
            embed.add_field(name="Stat", value=stat.name, inline=True)
            embed.add_field(name="Points Bought", value=f"`{points}`", inline=True)
            embed.add_field(name="Old → New", value=f"`{old_value}` → `{new_value}`", inline=True)
            if cost_paid is not None:
                embed.add_field(name="XP Cost", value=f"`{cost_paid}` XP", inline=False)
            embed.add_field(name="Remaining XP", value=f"`{int(wallet.get('available_xp') or 0)}` XP", inline=False)
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except XPInsufficientError:
            return await self._private(interaction, "❌ Not enough XP for that stat purchase.")
        except XPValidationError as e:
            return await self._private(interaction, f"❌ {e}")
        except XPServiceError:
            traceback.print_exc()
            return await self._private(interaction, "Server error buying stat points.")
        except Exception:
            traceback.print_exc()
            return await self._private(interaction, "Server error buying stat points.")


async def setup(bot: commands.Bot):
    await bot.add_cog(XPCog(bot))