# cogs/missions.py
from __future__ import annotations

import os
import re
from typing import Optional, List, Dict

import discord
from discord import app_commands
from discord.ext import commands

# --------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------

# 🔹 Guild-scope (Skyfall only)
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# GMs / staff who can create missions
GM_ROLE_IDS = {
    1374730886490357828,
    1374730886507139073,
    1374730886507139074,
    1374730886507139072,
    1374730886507139075,
    1381086606261223545,
    1374730886507139076,
}

# Mission board channel + ping role (set in Railway env or use defaults)
MISSION_BOARD_CHANNEL_ID = int(os.getenv("MISSION_BOARD_CHANNEL_ID", "0") or 0)
MISSION_PING_ROLE_ID = int(os.getenv("MISSION_PING_ROLE_ID", "1374730886356144193") or 0)

# ----- Choices from your GM Guide / server setup -----

MISSION_FORMAT_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name="Live", value="live"),
    app_commands.Choice(name="Standard", value="standard"),
]

MISSION_TYPE_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name="Combat", value="combat"),
    app_commands.Choice(name="Investigation / Mystery", value="investigation"),
    app_commands.Choice(name="Recon / Scouting", value="recon"),
    app_commands.Choice(name="Escort / Protection", value="escort"),
    app_commands.Choice(name="Rescue / Extraction", value="rescue"),
    app_commands.Choice(name="Social / Political", value="social"),
    app_commands.Choice(name="Non-Combat", value="noncombat"),
    app_commands.Choice(name="Other", value="other"),
]

DIFFICULTY_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Standard", value="standard"),
    app_commands.Choice(name="Hard", value="hard"),
    app_commands.Choice(name="Extreme", value="extreme"),
    app_commands.Choice(name="Lethal", value="lethal"),
]

SQUAD_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name="Black Gryphons", value="Black Gryphons"),
    app_commands.Choice(name="Jade Jaguars", value="Jade Jaguars"),
    app_commands.Choice(name="Coral Wyverns", value="Coral Wyverns"),
    app_commands.Choice(name="White Wolves", value="White Wolves"),
]

RANK_CHOICES: List[app_commands.Choice[str]] = [
    app_commands.Choice(name="Imperial Recruit", value="Imperial Recruit"),
    app_commands.Choice(name="Imperial Squire", value="Imperial Squire"),
    app_commands.Choice(name="Imperial Knight", value="Imperial Knight"),
    app_commands.Choice(name="Imperial General", value="Imperial General"),
    app_commands.Choice(name="Lieutenant", value="Lieutenant"),
    app_commands.Choice(name="Captain", value="Captain"),
]


class Missions(commands.Cog):
    """
    Mission system.

    - /mission_create: GM posts a mission to the mission board
      (auto mission code, dropdown format/type, optional banner attachment).
    - /mission_signup: players sign up their OC; updates the board post.
    - /mission_my: players can see missions their OCs are signed up for.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------- helpers -----------------

    def _supabase(self):
        """Grab the Supabase client from the bot (set in bot.py)."""
        return self.bot.supabase

    def _extract_data(self, res):
        """
        Handle both supabase-py styles:
        - res.data (SupabaseResponse)
        - {"data": [...]} (dict-style)
        """
        try:
            return res.data
        except AttributeError:
            if isinstance(res, dict):
                return res.get("data", None)
            return None

    def _is_gm(self, member: discord.abc.User | discord.Member) -> bool:
        """GM / staff check based on role IDs or admin."""
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        return any(r.id in GM_ROLE_IDS for r in member.roles)

    async def _generate_mission_code(self) -> str:
        """
        Auto-generate a mission code like M-0001, M-0002, ...

        Looks at the missions table for the most recent code, then increments.
        If anything is weird, falls back to M-0001.
        """
        supabase = self._supabase()
        try:
            res = (
                supabase.table("missions")
                .select("code, created_at")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = self._extract_data(res) or []
            if not rows:
                return "M-0001"

            last_code = rows[0].get("code") or ""
            m = re.search(r"(\d+)$", last_code)
            if not m:
                return "M-0001"

            next_num = int(m.group(1)) + 1
            return f"M-{next_num:04d}"
        except Exception as e:
            print(f"[Missions] _generate_mission_code error: {e}")
            return "M-0001"

    async def _find_mission_by_code(self, code: str) -> Optional[dict]:
        """Look up a mission row by its code, e.g. 'M-0003'."""
        supabase = self._supabase()
        try:
            res = (
                supabase.table("missions")
                .select(
                    "mission_id, code, name, format, type, difficulty, max_ocs, "
                    "gm_discord_id, summary, rules, eligibility, status, "
                    "channel_id, message_id, image_url"
                )
                .eq("code", code)
                .limit(1)
                .execute()
            )
            rows = self._extract_data(res) or []
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Missions] _find_mission_by_code error: {e}")
            return None

    async def _search_ocs_for_owner(
        self,
        owner_discord_id: str,
        partial: str,
        limit: int = 15,
    ) -> List[dict]:
        """
        For mission signup: show only this user's OCs in autocomplete.
        """
        supabase = self._supabase()
        try:
            query = (
                supabase.table("ocs")
                .select("oc_id, oc_name")
                .eq("owner_discord_id", owner_discord_id)
            )
            if partial:
                query = query.ilike("oc_name", f"%{partial}%")
            res = query.order("oc_name").limit(limit).execute()
            rows = self._extract_data(res) or []
            return rows
        except Exception as e:
            print(f"[Missions] _search_ocs_for_owner error: {e}")
            return []

    async def _oc_name_autocomplete_signup(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """
        Autocomplete for /mission_signup OC name: only show the caller's OCs.
        """
        if not interaction.user:
            return []
        owner_id = str(interaction.user.id)
        rows = await self._search_ocs_for_owner(owner_id, current, limit=15)
        choices: List[app_commands.Choice[str]] = []
        for row in rows:
            oc_name = row.get("oc_name")
            if not oc_name:
                continue
            choices.append(app_commands.Choice(name=oc_name, value=oc_name))
        return choices

    # ----------------------------------------------------------------
    # /mission_create
    # ----------------------------------------------------------------

    @app_commands.command(
        name="mission_create",
        description="GM: create a mission post in the mission board.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        name="Mission name (title).",
        mission_format="How the mission will be run.",
        mission_type="What kind of mission it is.",
        difficulty="Overall difficulty / risk.",
        max_ocs="Max number of OCs you’re taking.",
        summary="Short teaser / hook for players.",
        rules="Mission rules / expectations.",
        eligibility="Who can join (ranks, squads, limits).",
        banner="Optional image or gif attachment for the mission banner.",
    )
    @app_commands.choices(
        mission_format=MISSION_FORMAT_CHOICES,
        mission_type=MISSION_TYPE_CHOICES,
        difficulty=DIFFICULTY_CHOICES,
    )
    async def mission_create(
        self,
        interaction: discord.Interaction,
        name: str,
        mission_format: app_commands.Choice[str],
        mission_type: app_commands.Choice[str],
        difficulty: app_commands.Choice[str],
        max_ocs: app_commands.Range[int, 1, 20],
        summary: str,
        rules: str,
        eligibility: str,
        banner: Optional[discord.Attachment] = None,
    ):
        # --- perms ---
        if not self._is_gm(interaction.user):
            return await interaction.response.send_message(
                "❌ You don't have permission to create missions.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()

        # --- generate mission code ---
        code = await self._generate_mission_code()

        # banner URL (if attachment provided and is an image/gif)
        banner_url = None
        if banner is not None:
            # No heavy validation; Discord will serve the file.
            banner_url = banner.url

        # --- insert into DB ---
        try:
            insert_data = {
                "code": code,
                "name": name,
                "format": mission_format.value,
                "type": mission_type.value,
                "difficulty": difficulty.value,
                "max_ocs": int(max_ocs),
                "gm_discord_id": str(interaction.user.id),
                "summary": summary,
                "rules": rules,
                "eligibility": eligibility,
                "image_url": banner_url,
                "status": "open",
            }

            res = (
                supabase.table("missions")
                .insert(insert_data)
                .select("mission_id")
                .single()
                .execute()
            )
            row = self._extract_data(res) or {}
            mission_pk = row.get("mission_id")
        except Exception as e:
            print(f"[Missions] mission_create DB error: {e}")
            return await interaction.followup.send(
                "❌ Could not save this mission to the database.",
                ephemeral=False,
            )

        # --- build the embed for the mission board ---
        title = f"[{code}] {name}"
        fmt_label = mission_format.name
        type_label = mission_type.name
        diff_label = difficulty.name

        desc_lines = [
            f"**Format:** {fmt_label}",
            f"**Type:** {type_label}",
            f"**Difficulty:** {diff_label}",
            f"**Max OCs:** {int(max_ocs)}",
            "",
            "**Summary**",
            summary,
            "",
            "**Mission Rules**",
            rules,
            "",
            "**Eligibility**",
            eligibility,
        ]

        embed = discord.Embed(
            title=title,
            description="\n".join(desc_lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(
            text=f"GM: {interaction.user.display_name} • Use /mission_signup to join."
        )

        if banner_url:
            embed.set_image(url=banner_url)

        # --- send to mission board ---
        channel = None
        if interaction.guild:
            if MISSION_BOARD_CHANNEL_ID:
                channel = interaction.guild.get_channel(MISSION_BOARD_CHANNEL_ID)
            if channel is None:
                channel = interaction.channel

        if channel is None:
            return await interaction.followup.send(
                "❌ Could not find a channel to post this mission in.",
                ephemeral=False,
            )

        content = None
        if MISSION_PING_ROLE_ID:
            content = f"<@&{MISSION_PING_ROLE_ID}>"

        try:
            if banner is not None and banner_url:
                # send with attachment so Discord hosts it properly
                msg = await channel.send(
                    content=content,
                    embed=embed,
                    files=[await banner.to_file()],
                )
            else:
                msg = await channel.send(content=content, embed=embed)
        except Exception as e:
            print(f"[Missions] mission_create send error: {e}")
            return await interaction.followup.send(
                "❌ Mission saved, but I couldn't post in the mission board.",
                ephemeral=False,
            )

        # --- update DB with message info (for later edits on signup) ---
        if mission_pk is not None:
            try:
                supabase.table("missions").update(
                    {
                        "channel_id": str(channel.id),
                        "message_id": str(msg.id),
                    }
                ).eq("mission_id", mission_pk).execute()
            except Exception as e:
                print(f"[Missions] mission_create post-link update error: {e}")

        # --- confirm to GM ---
        await interaction.followup.send(
            f"✅ Mission **{name}** created with code `{code}` and posted in {channel.mention}.",
            ephemeral=True,
        )

    # ----------------------------------------------------------------
    # /mission_signup
    # ----------------------------------------------------------------

    @app_commands.command(
        name="mission_signup",
        description="Sign up your OC for a mission.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        mission_code="Mission code shown in the title, e.g. M-0003",
        oc_name="Your registered OC name.",
        squad="Your OC's squad.",
        rank="Your OC's current rank.",
        bst="Your OC's BST (for GM reference).",
        magic="Short description of your OC's magic.",
    )
    @app_commands.choices(
        squad=SQUAD_CHOICES,
        rank=RANK_CHOICES,
    )
    @app_commands.autocomplete(oc_name=_oc_name_autocomplete_signup)
    async def mission_signup(
        self,
        interaction: discord.Interaction,
        mission_code: str,
        oc_name: str,
        squad: app_commands.Choice[str],
        rank: app_commands.Choice[str],
        bst: app_commands.Range[int, 0, 999],
        magic: str,
    ):
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()
        player_id = str(interaction.user.id)

        # --- find mission ---
        mission = await self._find_mission_by_code(mission_code)
        if not mission:
            return await interaction.followup.send(
                f"❌ I couldn't find a mission with code `{mission_code}`.",
                ephemeral=False,
            )

        if mission.get("status") not in ("open", None):
            return await interaction.followup.send(
                f"❌ Mission `{mission_code}` is not open for signups.",
                ephemeral=False,
            )

        mission_id = mission["mission_id"]
        max_ocs = mission.get("max_ocs") or 0

        # --- resolve OC (must belong to this user) ---
        try:
            oc_res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, avatar_url")
                .eq("owner_discord_id", player_id)
                .ilike("oc_name", oc_name)
                .limit(1)
                .execute()
            )
            oc_rows = self._extract_data(oc_res) or []
        except Exception as e:
            print(f"[Missions] mission_signup OC lookup error: {e}")
            return await interaction.followup.send(
                "❌ Error looking up your OC. Make sure it's registered.",
                ephemeral=False,
            )

        if not oc_rows:
            return await interaction.followup.send(
                f"❌ I couldn't find an OC named `{oc_name}` for your account.",
                ephemeral=False,
            )

        oc_row = oc_rows[0]
        oc_id = oc_row["oc_id"]
        oc_name_norm = oc_row["oc_name"]
        avatar_url = oc_row.get("avatar_url")

        # --- check if already signed up / check current count ---
        try:
            # check duplicate
            dup_res = (
                supabase.table("mission_signups")
                .select("signup_id")
                .eq("mission_id", mission_id)
                .eq("oc_id", oc_id)
                .limit(1)
                .execute()
            )
            dup_rows = self._extract_data(dup_res) or []
            if dup_rows:
                return await interaction.followup.send(
                    f"❌ **{oc_name_norm}** is already signed up for `{mission_code}`.",
                    ephemeral=False,
                )

            # count signups
            count_res = (
                supabase.table("mission_signups")
                .select("signup_id", count="exact")
                .eq("mission_id", mission_id)
                .execute()
            )
            current_count = getattr(count_res, "count", None)
            if current_count is None:
                rows = self._extract_data(count_res) or []
                current_count = len(rows)

            if max_ocs and current_count >= max_ocs:
                return await interaction.followup.send(
                    f"❌ Mission `{mission_code}` is already full "
                    f"({current_count}/{max_ocs} OCs).",
                    ephemeral=False,
                )
        except Exception as e:
            print(f"[Missions] mission_signup duplicate/count error: {e}")
            return await interaction.followup.send(
                "❌ Error checking mission signups.",
                ephemeral=False,
            )

        # --- insert signup row ---
        try:
            insert_data = {
                "mission_id": mission_id,
                "oc_id": oc_id,
                "oc_name": oc_name_norm,
                "player_discord_id": player_id,
                "squad": squad.value,
                "rank": rank.value,
                "bst": int(bst),
                "magic": magic,
            }
            supabase.table("mission_signups").insert(insert_data).execute()
        except Exception as e:
            print(f"[Missions] mission_signup insert error: {e}")
            return await interaction.followup.send(
                "❌ Could not save your signup to the database.",
                ephemeral=False,
            )

        # --- rebuild signups list for the mission board embed ---
        signups: List[dict]
        try:
            su_res = (
                supabase.table("mission_signups")
                .select("oc_name, squad, rank, player_discord_id")
                .eq("mission_id", mission_id)
                .order("created_at", asc=True)
                .execute()
            )
            signups = self._extract_data(su_res) or []
        except Exception as e:
            print(f"[Missions] mission_signup fetch signups error: {e}")
            signups = []

        # Only try to edit board post if we have message ids stored
        channel_id = mission.get("channel_id")
        message_id = mission.get("message_id")

        if interaction.guild and channel_id and message_id:
            try:
                channel = interaction.guild.get_channel(int(channel_id))
                if channel:
                    msg = await channel.fetch_message(int(message_id))
                    embed = msg.embeds[0] if msg.embeds else discord.Embed(
                        title=f"[{mission['code']}] {mission['name']}",
                        color=discord.Color.orange(),
                    )

                    fmt_label = mission.get("format") or "—"
                    type_label = mission.get("type") or "—"
                    diff_label = mission.get("difficulty") or "—"
                    max_ocs_val = max_ocs or 0

                    base_lines = [
                        f"**Format:** {fmt_label}",
                        f"**Type:** {type_label}",
                        f"**Difficulty:** {diff_label}",
                        f"**Max OCs:** {max_ocs_val}",
                        "",
                        "**Summary**",
                        mission.get("summary") or "—",
                        "",
                        "**Mission Rules**",
                        mission.get("rules") or "—",
                        "",
                        "**Eligibility**",
                        mission.get("eligibility") or "—",
                    ]

                    if signups:
                        base_lines.append("")
                        base_lines.append(
                            f"**Signups ({len(signups)}/{max_ocs_val})**"
                        )
                        for s in signups:
                            oc_n = s.get("oc_name", "Unknown OC")
                            sq = s.get("squad", "—")
                            rk = s.get("rank", "—")
                            pid = s.get("player_discord_id")
                            if pid:
                                line = f"• **{oc_n}** — {sq} ({rk}) — <@{pid}>"
                            else:
                                line = f"• **{oc_n}** — {sq} ({rk})"
                            base_lines.append(line)

                    embed.description = "\n".join(base_lines)
                    await msg.edit(embed=embed)
            except Exception as e:
                print(f"[Missions] mission_signup board update error: {e}")

        # --- confirmation to player (with OC avatar thumbnail if present) ---
        confirm = discord.Embed(
            title="✅ Mission Signup Confirmed",
            description=(
                f"**Mission:** `{mission_code}` — {mission['name']}\n"
                f"**OC:** {oc_name_norm}\n"
                f"**Squad:** {squad.name}\n"
                f"**Rank:** {rank.name}\n"
                f"**BST:** {int(bst)}\n"
                f"**Magic:** {magic}"
            ),
            color=discord.Color.green(),
        )
        confirm.set_footer(text="GM will contact you if anything changes.")

        if avatar_url:
            confirm.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=confirm, ephemeral=False)

    # ----------------------------------------------------------------
    # /mission_my – list missions a player's OCs are signed up for
    # ----------------------------------------------------------------

    @app_commands.command(
        name="mission_my",
        description="Show missions your OCs are currently signed up for.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def mission_my(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        supabase = self._supabase()
        player_id = str(interaction.user.id)

        # fetch signups for this player
        try:
            su_res = (
                supabase.table("mission_signups")
                .select("mission_id, oc_name, squad, rank")
                .eq("player_discord_id", player_id)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
            )
            signups = self._extract_data(su_res) or []
        except Exception as e:
            print(f"[Missions] mission_my signups error: {e}")
            return await interaction.followup.send(
                "❌ Error fetching your mission signups.", ephemeral=False
            )

        if not signups:
            return await interaction.followup.send(
                "You aren't signed up for any missions yet.", ephemeral=False
            )

        mission_ids = {row["mission_id"] for row in signups if row.get("mission_id")}
        mission_map: Dict[int, dict] = {}

        if mission_ids:
            try:
                m_res = (
                    supabase.table("missions")
                    .select("mission_id, code, name, status, difficulty, format, type")
                    .in_("mission_id", list(mission_ids))
                    .execute()
                )
                m_rows = self._extract_data(m_res) or []
                for m in m_rows:
                    mission_map[m["mission_id"]] = m
            except Exception as e:
                print(f"[Missions] mission_my missions error: {e}")

        # Group signups by mission_id
        grouped: Dict[int, List[dict]] = {}
        for s in signups:
            mid = s.get("mission_id")
            if mid is None:
                continue
            grouped.setdefault(mid, []).append(s)

        embed = discord.Embed(
            title="📋 Your Mission Signups",
            description="Missions your OCs are currently registered for.",
            color=discord.Color.blurple(),
        )

        for mid, oc_list in grouped.items():
            m = mission_map.get(mid)
            if not m:
                continue
            code = m.get("code", "M-????")
            name = m.get("name", "Unknown Mission")
            status = m.get("status", "open")
            diff = m.get("difficulty", "—")
            fmt = m.get("format", "—")
            mtype = m.get("type", "—")

            header = f"`{code}` — {name}"
            lines = [
                f"Status: **{status}** • Difficulty: **{diff}**",
                f"Format: **{fmt}** • Type: **{mtype}**",
                "",
                "OCs:",
            ]
            for s in oc_list:
                oc_n = s.get("oc_name", "Unknown OC")
                sq = s.get("squad", "—")
                rk = s.get("rank", "—")
                lines.append(f"• **{oc_n}** — {sq} ({rk})")

            embed.add_field(name=header, value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Missions(bot))
