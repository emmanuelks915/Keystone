# cogs/ap.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config.skyfall import SKYFALL_GUILD, STAFF_ROLE_ID, AP_EMOJI
from services.db import get_supabase_client


# ---------- helpers ----------
def _is_staff(member: discord.Member) -> bool:
    return any(r.id == STAFF_ROLE_ID for r in getattr(member, "roles", []))


async def oc_name_autocomplete_for_selected_player(interaction: discord.Interaction, current: str):
    """
    Autocomplete OC names for the selected `player` arg on /ap add|remove|set.
    Falls back to global search if player isn't chosen yet.
    """
    sb = get_supabase_client()
    cur = (current or "").strip()

    try:
        ns = getattr(interaction, "namespace", None)
        player = getattr(ns, "player", None) if ns else None

        q = sb.table("ocs").select("oc_name")

        # If staff already selected a player, filter to that player's OCs
        if player:
            q = q.eq("owner_discord_id", str(player.id))

        if cur:
            q = q.ilike("oc_name", f"%{cur}%")

        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


def _get_oc_by_owner_and_name(sb, owner_id: int, oc_name: str):
    res = (
        sb.table("ocs")
        .select("oc_id, oc_name, owner_discord_id, avatar_url")
        .eq("owner_discord_id", str(owner_id))
        .eq("oc_name", oc_name)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _ensure_wallet(sb, oc_id: str) -> None:
    sb.table("oc_wallets").upsert({"oc_id": oc_id}, on_conflict="oc_id").execute()


def _get_ap_balance(sb, oc_id: str) -> int:
    _ensure_wallet(sb, oc_id)
    res = sb.table("oc_wallets").select("ap_balance").eq("oc_id", oc_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return int(rows[0].get("ap_balance") or 0) if rows else 0


def _set_ap_balance(sb, oc_id: str, new_balance: int) -> int:
    """
    STRICT setter:
    - Raises if new_balance < 0
    - This prevents silent clamping from masking bugs in purchase flows.
    """
    _ensure_wallet(sb, oc_id)
    new_balance = int(new_balance)
    if new_balance < 0:
        raise ValueError("AP balance cannot go below 0.")
    sb.table("oc_wallets").update({"ap_balance": new_balance}).eq("oc_id", oc_id).execute()
    return new_balance


def _add_ap(sb, oc_id: str, delta: int) -> int:
    cur = _get_ap_balance(sb, oc_id)
    return _set_ap_balance(sb, oc_id, cur + int(delta))


def _charge_ap(sb, oc_id: str, cost: int) -> int:
    """
    Charge AP (cost must be >= 0). Raises if insufficient AP.
    Returns new balance.
    """
    cost = int(cost or 0)
    if cost < 0:
        raise ValueError("AP cost must be >= 0.")

    cur = _get_ap_balance(sb, oc_id)
    if cost > 0 and cur < cost:
        raise ValueError(f"Not enough AP. Need {cost}, you have {cur}.")
    return _set_ap_balance(sb, oc_id, cur - cost)


# ✅ GroupCog = auto /ap group registration
@app_commands.guilds(SKYFALL_GUILD)
class AP(commands.GroupCog, group_name="ap"):
    """AP (Ability Points) staff commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /ap add (STAFF) -> PUBLIC
    @app_commands.command(name="add", description="(Staff) Add AP to a player's OC.")
    @app_commands.describe(player="The OC owner", oc_name="Exact OC name", amount="How much AP to add")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_for_selected_player)
    async def add(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            return await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)

        await interaction.response.defer(ephemeral=False)

        if amount <= 0:
            return await interaction.followup.send("Amount must be a positive integer.", ephemeral=True)

        sb = get_supabase_client()
        oc_name = (oc_name or "").strip()

        oc = _get_oc_by_owner_and_name(sb, player.id, oc_name)
        if not oc:
            return await interaction.followup.send("That OC was not found for that player.", ephemeral=True)

        after = _add_ap(sb, oc["oc_id"], amount)

        embed = discord.Embed(
            title="AP Updated",
            description=f"OC **{oc['oc_name']}** • Owner <@{player.id}>",
            color=discord.Color.green(),
        )
        embed.add_field(name="Change", value=f"+{amount} {AP_EMOJI}", inline=True)
        embed.add_field(name="New Balance", value=f"{after} {AP_EMOJI}", inline=True)
        if oc.get("avatar_url"):
            embed.set_thumbnail(url=oc["avatar_url"])

        await interaction.followup.send(embed=embed, ephemeral=False)

    # /ap remove (STAFF) -> PUBLIC
    @app_commands.command(name="remove", description="(Staff) Remove AP from a player's OC.")
    @app_commands.describe(player="The OC owner", oc_name="Exact OC name", amount="How much AP to remove")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_for_selected_player)
    async def remove(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            return await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)

        await interaction.response.defer(ephemeral=False)

        if amount <= 0:
            return await interaction.followup.send("Amount must be a positive integer.", ephemeral=True)

        sb = get_supabase_client()
        oc_name = (oc_name or "").strip()

        oc = _get_oc_by_owner_and_name(sb, player.id, oc_name)
        if not oc:
            return await interaction.followup.send("That OC was not found for that player.", ephemeral=True)

        before = _get_ap_balance(sb, oc["oc_id"])

        # clamp behavior for staff remove: remove up to current
        removed = min(before, amount)
        after = _set_ap_balance(sb, oc["oc_id"], before - removed)

        embed = discord.Embed(
            title="AP Updated",
            description=f"OC **{oc['oc_name']}** • Owner <@{player.id}>",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Change", value=f"-{removed} {AP_EMOJI}", inline=True)
        embed.add_field(name="New Balance", value=f"{after} {AP_EMOJI}", inline=True)
        if oc.get("avatar_url"):
            embed.set_thumbnail(url=oc["avatar_url"])

        await interaction.followup.send(embed=embed, ephemeral=False)

    # /ap set (STAFF) -> PUBLIC
    @app_commands.command(name="set", description="(Staff) Set an OC's AP to an exact value.")
    @app_commands.describe(player="The OC owner", oc_name="Exact OC name", amount="New AP balance (>= 0)")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_for_selected_player)
    async def set(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            return await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)

        await interaction.response.defer(ephemeral=False)

        if amount < 0:
            return await interaction.followup.send("Amount must be 0 or higher.", ephemeral=True)

        sb = get_supabase_client()
        oc_name = (oc_name or "").strip()

        oc = _get_oc_by_owner_and_name(sb, player.id, oc_name)
        if not oc:
            return await interaction.followup.send("That OC was not found for that player.", ephemeral=True)

        before = _get_ap_balance(sb, oc["oc_id"])
        after = _set_ap_balance(sb, oc["oc_id"], amount)
        delta = after - before
        sign = "+" if delta >= 0 else "-"

        embed = discord.Embed(
            title="AP Updated",
            description=f"OC **{oc['oc_name']}** • Owner <@{player.id}>",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Change", value=f"{sign}{abs(delta)} {AP_EMOJI}", inline=True)
        embed.add_field(name="New Balance", value=f"{after} {AP_EMOJI}", inline=True)
        if oc.get("avatar_url"):
            embed.set_thumbnail(url=oc["avatar_url"])

        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AP(bot))
