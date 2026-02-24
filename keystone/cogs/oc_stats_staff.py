# cogs/oc_stats_staff.py
from __future__ import annotations

import traceback
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

from services.db import get_supabase_client  # use your shared helper if you have it

SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

STATS_STAFF_ROLE_ID = 1374730886490357822

STAT_KEYS = {
    "dexterity", "reflexes", "strength", "durability", "mana", "magic_output", "magic_control", "luck"
}

def _is_stats_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == STATS_STAFF_ROLE_ID for r in interaction.user.roles)

class OCStatsStaff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sb = get_supabase_client()

    async def oc_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        q = (current or "").strip()
        res = self.sb.table("ocs").select("oc_name").ilike("oc_name", f"%{q}%").limit(20).execute()
        names = [r["oc_name"] for r in (res.data or []) if r.get("oc_name")]
        seen = set()
        out: List[app_commands.Choice[str]] = []
        for n in names:
            k = n.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(app_commands.Choice(name=n, value=n))
        return out[:20]

    def _get_oc(self, oc_name: str) -> dict:
        res = self.sb.table("ocs").select("*").eq("oc_name", oc_name).limit(1).execute()
        if not res.data:
            raise ValueError("OC not found.")
        return res.data[0]

    @app_commands.command(name="oc_stats_set", description="(Stats Staff) Set an OC stat exactly.")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.autocomplete(oc_name=oc_autocomplete)
    async def oc_stats_set(self, interaction: discord.Interaction, oc_name: str, stat: str, value: int, reason: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        stat = stat.lower().replace(" ", "_")
        if stat not in STAT_KEYS:
            return await interaction.response.send_message(f"Invalid stat. Use one of: {', '.join(sorted(STAT_KEYS))}", ephemeral=True)

        try:
            oc = self._get_oc(oc_name)
            oc_id = oc["oc_id"]
            actor_id = str(interaction.user.id)

            cur = self.sb.table("oc_stats").select("*").eq("oc_id", oc_id).limit(1).execute()
            row = (cur.data[0] if cur.data else {"oc_id": oc_id})
            old_val = int(row.get(stat, 0) or 0)
            new_val = int(value)
            delta = new_val - old_val

            self.sb.table("oc_stats").upsert({"oc_id": oc_id, stat: new_val}).execute()
            self.sb.table("oc_stat_logs").insert({
                "oc_id": oc_id,
                "stat_key": stat,
                "delta": delta,
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": actor_id,
                "reason": reason or "staff set",
            }).execute()

            await interaction.response.send_message(f"✅ **{oc_name}** {stat}: {old_val} → **{new_val}**", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message(f"Server error: {e}", ephemeral=True)

    @app_commands.command(name="oc_stats_add", description="(Stats Staff) Add/Subtract from an OC stat.")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.autocomplete(oc_name=oc_autocomplete)
    async def oc_stats_add(self, interaction: discord.Interaction, oc_name: str, stat: str, amount: int, reason: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        stat = stat.lower().replace(" ", "_")
        if stat not in STAT_KEYS:
            return await interaction.response.send_message(f"Invalid stat. Use one of: {', '.join(sorted(STAT_KEYS))}", ephemeral=True)

        try:
            oc = self._get_oc(oc_name)
            oc_id = oc["oc_id"]
            actor_id = str(interaction.user.id)

            cur = self.sb.table("oc_stats").select("*").eq("oc_id", oc_id).limit(1).execute()
            row = (cur.data[0] if cur.data else {"oc_id": oc_id})
            old_val = int(row.get(stat, 0) or 0)
            new_val = max(0, old_val + int(amount))
            delta = new_val - old_val

            self.sb.table("oc_stats").upsert({"oc_id": oc_id, stat: new_val}).execute()
            self.sb.table("oc_stat_logs").insert({
                "oc_id": oc_id,
                "stat_key": stat,
                "delta": delta,
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": actor_id,
                "reason": reason or "staff add",
            }).execute()

            await interaction.response.send_message(f"✅ **{oc_name}** {stat}: {old_val} → **{new_val}** (Δ{delta})", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message(f"Server error: {e}", ephemeral=True)

    @app_commands.command(name="oc_stats_approve", description="(Stats Staff) Approve an OC's stat allocation.")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.autocomplete(oc_name=oc_autocomplete)
    async def oc_stats_approve(self, interaction: discord.Interaction, oc_name: str, note: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        try:
            oc = self._get_oc(oc_name)
            self.sb.table("ocs").update({
                "stats_status": "approved",
                "stats_reviewed_by": str(interaction.user.id),
                "stats_reviewed_at": "now()"
            }).eq("oc_id", oc["oc_id"]).execute()

            await interaction.response.send_message(f"✅ **{oc_name}** marked as **approved**.", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message(f"Server error: {e}", ephemeral=True)

    @app_commands.command(name="oc_stats_needs_changes", description="(Stats Staff) Mark an OC as needing changes.")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.autocomplete(oc_name=oc_autocomplete)
    async def oc_stats_needs_changes(self, interaction: discord.Interaction, oc_name: str, note: str):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        try:
            oc = self._get_oc(oc_name)
            self.sb.table("ocs").update({
                "stats_status": "needs_changes",
                "stats_reviewed_by": str(interaction.user.id),
                "stats_reviewed_at": "now()"
            }).eq("oc_id", oc["oc_id"]).execute()

            await interaction.response.send_message(f"⚠️ **{oc_name}** marked **needs_changes**: {note}", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message(f"Server error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OCStatsStaff(bot))
