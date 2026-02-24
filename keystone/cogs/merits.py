from __future__ import annotations

from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

# 🔹 Guild-scope (Skyfall only)
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# All staff roles that can use merit commands
STAFF_ROLE_IDS = {
    1374730886490357828,
    1374730886507139073,
    1374730886507139074,
    1374730886507139072,
    1374730886507139075,
    1381086606261223545,
    1374730886507139076,
}


class Merits(commands.Cog):
    """Merit / Demerit system for OCs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------- helpers -----------------

    def _supabase(self):
        """Small helper to grab the Supabase client from the bot."""
        return self.bot.supabase  # assumes you set this in bot.py

    def _extract_data(self, res):
        """
        Handle both supabase-py styles:
        - res.data (SupabaseResponse)
        - {"data": [...]} (dict style)
        """
        try:
            return res.data
        except AttributeError:
            if isinstance(res, dict):
                return res.get("data", None)
            return None

    def _is_staff(self, member: discord.abc.User | discord.Member) -> bool:
        """
        Staff check:
        - Server admins
        - Anyone with at least one of STAFF_ROLE_IDS
        """
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        return any(r.id in STAFF_ROLE_IDS for r in member.roles)

    async def _get_current_cycle(self) -> Optional[dict]:
        """Return the current merit cycle row, or None."""
        supabase = self._supabase()
        res = (
            supabase.table("merit_cycles")
            .select("id, name, start_date, end_date")
            .eq("is_current", True)
            .limit(1)
            .execute()
        )
        data = self._extract_data(res)
        return data[0] if data else None

    async def _find_oc_by_name(self, name: str) -> Optional[dict]:
        """Find an OC by name (case-insensitive, first match)."""
        supabase = self._supabase()
        res = (
            supabase.table("ocs")
            .select("oc_id, owner_discord_id, oc_name")
            .ilike("oc_name", name)
            .limit(1)
            .execute()
        )
        data = self._extract_data(res)
        return data[0] if data else None

    async def _search_ocs(self, partial: str, limit: int = 15) -> List[dict]:
        """Return a small list of OCs whose names match `partial`."""
        supabase = self._supabase()
        if not partial:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, owner_discord_id")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        else:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, owner_discord_id")
                .ilike("oc_name", f"%{partial}%")
                .order("oc_name")
                .limit(limit)
                .execute()
            )
        data = self._extract_data(res)
        return data or []

    # ----------------- autocomplete -----------------

    async def oc_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete OC names based on what the staff types."""
        try:
            ocs = await self._search_ocs(current, limit=15)
        except Exception as e:
            # If autocomplete errors, swallow it and return empty options
            print(f"[Merits] Autocomplete error: {e}")
            return []

        choices: List[app_commands.Choice[str]] = []
        for oc in ocs:
            oc_name = oc["oc_name"]
            owner_id_str = oc["owner_discord_id"]

            # Default label is just the OC name
            label = oc_name
            # Try to show player’s display name instead of a raw ID
            try:
                owner = self.bot.get_user(int(owner_id_str))
                if owner:
                    label = f"{oc_name} ({owner.display_name})"
            except Exception:
                pass

            choices.append(app_commands.Choice(name=label, value=oc_name))
        return choices

    # ----------------- /merit_add -----------------

    @app_commands.command(
        name="merit_add",
        description="Give a merit or demerit to an OC.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Registered OC name.",
        type="Merit or demerit type.",
        reason_short="Short reason (will show in summary).",
        reason_detail="Optional longer explanation.",
        source="Optional source: mission code, scene, etc.",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Merit (+1)", value="merit"),
            app_commands.Choice(name="IC Demerit (-2)", value="demerit_ic"),
            app_commands.Choice(name="OOC Demerit (-1)", value="demerit_oc"),
        ]
    )
    async def merit_add(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        type: app_commands.Choice[str],
        reason_short: str,
        reason_detail: Optional[str] = None,
        source: Optional[str] = None,
    ):
        # permissions
        if not self._is_staff(interaction.user):
            # permission errors can stay private
            return await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )

        # public response so everyone sees the merit embed
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        try:
            # find OC
            oc = await self._find_oc_by_name(oc_name)
            if not oc:
                return await interaction.followup.send(
                    f"❌ Could not find an OC named `{oc_name}`.",
                    ephemeral=False,
                )

            oc_id = oc["oc_id"]
            owner_discord_id = oc["owner_discord_id"]
            oc_name = oc["oc_name"]  # normalized

            # get current cycle
            cycle = await self._get_current_cycle()
            if not cycle:
                return await interaction.followup.send(
                    "❌ No current merit cycle is set. Ask an admin to set one.",
                    ephemeral=False,
                )
            cycle_id = cycle["id"]

            # map type -> points
            points_map = {"merit": 1, "demerit_ic": -2, "demerit_oc": -1}
            points = points_map.get(type.value)
            if points is None:
                return await interaction.followup.send(
                    "❌ Invalid merit type.",
                    ephemeral=False,
                )

            # insert row
            insert_data = {
                "oc_id": oc_id,
                "owner_discord_id": owner_discord_id,
                "oc_name": oc_name,
                "cycle_id": cycle_id,
                "mission_id": None,  # you can wire this later to missions
                "type": type.value,
                "points": points,
                "reason_short": reason_short,
                "reason_detail": reason_detail,
                "source": source,
                "staff_id": str(interaction.user.id),
                "channel_id": str(interaction.channel.id)
                if interaction.channel
                else None,
            }

            res = supabase.table("merit_entries").insert(insert_data).execute()
            data = self._extract_data(res)
            if not data:
                return await interaction.followup.send(
                    "❌ Failed to insert merit into the database.",
                    ephemeral=False,
                )

        except Exception as e:
            print(f"[Merits] merit_add error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while processing this merit.",
                ephemeral=False,
            )

        # pretty response
        color = discord.Color.green() if points > 0 else discord.Color.red()
        embed = discord.Embed(
            title="Merit Updated",
            description=f"Entry created for **{oc_name}**.",
            color=color,
        )
        embed.add_field(name="OC", value=oc_name, inline=True)
        embed.add_field(name="Type", value=type.name, inline=True)
        embed.add_field(name="Points", value=f"{points:+d}", inline=True)
        embed.add_field(name="Cycle", value=cycle["name"], inline=True)
        embed.add_field(name="Reason", value=reason_short, inline=False)
        if reason_detail:
            embed.add_field(name="Details", value=reason_detail, inline=False)
        if source:
            embed.add_field(name="Source", value=source, inline=False)
        embed.set_footer(text=f"Issued by {interaction.user}")

        await interaction.followup.send(embed=embed, ephemeral=False)

        # optional: send to AuditLog
        audit_cog = self.bot.get_cog("AuditLog")
        if audit_cog:
            try:
                await audit_cog.post(embed)
            except Exception as e:
                print(f"[Merits] AuditLog error in merit_add: {e}")

    # ----------------- /merit_summary -----------------

    @app_commands.command(
        name="merit_summary",
        description="Show total merits/demerits for an OC.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Registered OC name.",
        cycle_name="Specific cycle name (blank = current cycle).",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def merit_summary(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        cycle_name: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        supabase = self._supabase()

        try:
            oc = await self._find_oc_by_name(oc_name)
            if not oc:
                return await interaction.followup.send(
                    f"❌ Could not find an OC named `{oc_name}`.",
                    ephemeral=True,
                )

            oc_id = oc["oc_id"]
            oc_name = oc["oc_name"]

            # resolve cycle
            cycle_id = None
            cycle_label = "All cycles"

            if cycle_name:
                cres = (
                    supabase.table("merit_cycles")
                    .select("id, name")
                    .ilike("name", cycle_name)
                    .limit(1)
                    .execute()
                )
                cdata = self._extract_data(cres)
                if cdata:
                    cycle_id = cdata[0]["id"]
                    cycle_label = cdata[0]["name"]
            else:
                cycle = await self._get_current_cycle()
                if cycle:
                    cycle_id = cycle["id"]
                    cycle_label = cycle["name"]

            # query scores view (safe: handle 0 rows)
            query = (
                supabase.table("merit_scores")
                .select("total_points, merit_points, demerit_points")
                .eq("oc_id", oc_id)
            )
            if cycle_id:
                query = query.eq("cycle_id", cycle_id)

            res = query.limit(1).execute()
            rows = self._extract_data(res) or []
            data = rows[0] if rows else None

            total = (
                data["total_points"] if data and data["total_points"] is not None else 0
            )
            merits = (
                data["merit_points"] if data and data["merit_points"] is not None else 0
            )
            demerits = (
                data["demerit_points"]
                if data and data["demerit_points"] is not None
                else 0
            )

        except Exception as e:
            print(f"[Merits] merit_summary error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while fetching merit summary.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"Merit Summary — {oc_name}",
            description=f"Cycle: **{cycle_label}**",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Total Score", value=str(total), inline=True)
        embed.add_field(name="Merit Points", value=str(merits), inline=True)
        embed.add_field(name="Demerit Points", value=str(demerits), inline=True)
        embed.set_footer(
            text="Positive = good standing. Negative = needs improvement."
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------- /merit_history -----------------

    @app_commands.command(
        name="merit_history",
        description="Show recent merit entries for an OC.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Registered OC name.",
        limit="How many recent entries to show (max 25).",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def merit_history(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        # public so everyone sees the history
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        try:
            oc = await self._find_oc_by_name(oc_name)
            if not oc:
                return await interaction.followup.send(
                    f"❌ Could not find an OC named `{oc_name}`.",
                    ephemeral=False,
                )

            oc_id = oc["oc_id"]
            oc_name = oc["oc_name"]

            res = (
                supabase.table("merit_entries")
                .select("created_at, type, points, reason_short, source")
                .eq("oc_id", oc_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            data = self._extract_data(res) or []

        except Exception as e:
            print(f"[Merits] merit_history error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while fetching merit history.",
                ephemeral=False,
            )

        if not data:
            return await interaction.followup.send(
                f"ℹ️ No merit entries found for **{oc_name}**.",
                ephemeral=False,
            )

        lines: List[str] = []
        for row in data:
            points = row["points"]
            type_label = row["type"]
            reason = row["reason_short"] or "No reason provided"
            src = f" • Source: {row['source']}" if row.get("source") else ""
            ts_str = str(row["created_at"])
            lines.append(
                f"• `{ts_str}` — **{points:+}** ({type_label}) — {reason}{src}"
            )

        embed = discord.Embed(
            title=f"Merit History — {oc_name}",
            description="\n".join(lines),
            color=discord.Color.dark_gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=False)

    # ----------------- /oc_set_squad -----------------

    @app_commands.command(
        name="oc_set_squad",
        description="STAFF: set an OC's current squad.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Registered OC name.",
        squad="Squad to assign this OC to.",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    @app_commands.choices(
        squad=[
            app_commands.Choice(name="Black Gryphons", value="Black Gryphons"),
            app_commands.Choice(name="Jade Jaguars", value="Jade Jaguars"),
            app_commands.Choice(name="Coral Wyverns", value="Coral Wyverns"),
            app_commands.Choice(name="White Wolves", value="White Wolves"),
        ]
    )
    async def oc_set_squad(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        squad: app_commands.Choice[str],
    ):
        # staff-only
        if not self._is_staff(interaction.user):
            return await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        try:
            # find OC
            oc = await self._find_oc_by_name(oc_name)
            if not oc:
                return await interaction.followup.send(
                    f"❌ Could not find an OC named `{oc_name}`.",
                    ephemeral=False,
                )

            oc_id = oc["oc_id"]
            oc_name = oc["oc_name"]

            # find squad row
            sres = (
                supabase.table("squads")
                .select("squad_id, name")
                .eq("name", squad.value)
                .limit(1)
                .execute()
            )
            sdata = self._extract_data(sres) or []
            if not sdata:
                return await interaction.followup.send(
                    f"❌ Squad `{squad.value}` does not exist in the database.",
                    ephemeral=False,
                )

            squad_row = sdata[0]
            squad_id = squad_row["squad_id"]

            # update OC
            ures = (
                supabase.table("ocs")
                .update({"current_squad_id": squad_id})
                .eq("oc_id", oc_id)
                .execute()
            )
            _ = self._extract_data(ures)  # just to trigger errors if any

        except Exception as e:
            print(f"[Merits] oc_set_squad error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while updating the OC's squad.",
                ephemeral=False,
            )

        embed = discord.Embed(
            title="Squad Updated",
            description=f"**{oc_name}** is now in **{squad.value}**.",
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"Updated by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=False)

    # ----------------- /merit_squad_leaderboard -----------------

    @app_commands.command(
        name="merit_squad_leaderboard",
        description="Show total squad merit scores for the current cycle.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def merit_squad_leaderboard(self, interaction: discord.Interaction):
        # public so everyone can see squad rankings
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        try:
            cycle = await self._get_current_cycle()
            if not cycle:
                return await interaction.followup.send(
                    "❌ No current merit cycle is set.",
                    ephemeral=False,
                )

            cycle_id = cycle["id"]

            # 1) get OC scores for this cycle
            res = (
                supabase.table("merit_scores")
                .select("oc_id, total_points")
                .eq("cycle_id", cycle_id)
                .execute()
            )
            score_rows = self._extract_data(res) or []
            if not score_rows:
                return await interaction.followup.send(
                    "ℹ️ No merit data yet for this cycle.",
                    ephemeral=False,
                )

            oc_ids = [row["oc_id"] for row in score_rows]

            # 2) fetch OCs to get their current_squad_id
            ocs_res = (
                supabase.table("ocs")
                .select("oc_id, current_squad_id")
                .in_("oc_id", oc_ids)
                .execute()
            )
            ocs_data = self._extract_data(ocs_res) or []
            oc_to_squad: dict[str, Optional[str]] = {
                row["oc_id"]: row.get("current_squad_id") for row in ocs_data
            }

            # 3) aggregate points per squad_id
            squad_totals: dict[str, int] = {}
            squad_counts: dict[str, int] = {}

            for row in score_rows:
                oc_id = row["oc_id"]
                total_points = row.get("total_points") or 0
                squad_id = oc_to_squad.get(oc_id)

                # ignore OCs with no squad
                if not squad_id:
                    continue

                squad_totals[squad_id] = squad_totals.get(squad_id, 0) + total_points
                squad_counts[squad_id] = squad_counts.get(squad_id, 0) + 1

            if not squad_totals:
                return await interaction.followup.send(
                    "ℹ️ No squads currently have OCs with merit scores this cycle.",
                    ephemeral=False,
                )

            squad_ids = list(squad_totals.keys())

            # 4) fetch squad names
            squads_res = (
                supabase.table("squads")
                .select("squad_id, name")
                .in_("squad_id", squad_ids)
                .execute()
            )
            squads_data = self._extract_data(squads_res) or []
            squad_name_map = {row["squad_id"]: row["name"] for row in squads_data}

        except Exception as e:
            print(f"[Merits] merit_squad_leaderboard error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while fetching the squad leaderboard.",
                ephemeral=False,
            )

        # 5) build sorted leaderboard
        # sort by total points desc
        sorted_squads = sorted(
            squad_totals.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )

        lines: List[str] = []
        for rank, (squad_id, total) in enumerate(sorted_squads, start=1):
            name = squad_name_map.get(squad_id, "Unknown Squad")
            count = squad_counts.get(squad_id, 0)
            lines.append(
                f"**{rank}. {name}** — {total:+} points across {count} OC(s)"
            )

        embed = discord.Embed(
            title=f"Squad Merit Leaderboard — {cycle['name']}",
            description="\n".join(lines),
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="Only OCs with a current squad are counted.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    # ----------------- /merit_leaderboard -----------------

    @app_commands.command(
        name="merit_leaderboard",
        description="Show top OCs by merit score for the current cycle.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def merit_leaderboard(self, interaction: discord.Interaction):
        # public so anyone can see rankings
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        try:
            cycle = await self._get_current_cycle()
            if not cycle:
                return await interaction.followup.send(
                    "❌ No current merit cycle is set.",
                    ephemeral=False,
                )

            cycle_id = cycle["id"]

            res = (
                supabase.table("merit_scores")
                .select("oc_id, total_points")
                .eq("cycle_id", cycle_id)
                .order("total_points", desc=True)
                .limit(15)
                .execute()
            )
            scores = self._extract_data(res) or []

            if not scores:
                return await interaction.followup.send(
                    "ℹ️ No merit data yet for this cycle.",
                    ephemeral=False,
                )

            # fetch OC names in one go
            oc_ids = [row["oc_id"] for row in scores]
            ocs_res = (
                supabase.table("ocs")
                .select("oc_id, oc_name")
                .in_("oc_id", oc_ids)
                .execute()
            )
            oc_data = self._extract_data(ocs_res) or []
            oc_name_map = {row["oc_id"]: row["oc_name"] for row in oc_data}

        except Exception as e:
            print(f"[Merits] merit_leaderboard error: {e}")
            return await interaction.followup.send(
                "❌ An error occurred while fetching the leaderboard.",
                ephemeral=False,
            )

        lines = []
        for i, row in enumerate(scores, start=1):
            name = oc_name_map.get(row["oc_id"], "Unknown OC")
            total = row["total_points"]
            lines.append(f"**{i}. {name}** — {total:+} points")

        embed = discord.Embed(
            title=f"Merit Leaderboard — {cycle['name']}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Merits(bot))
