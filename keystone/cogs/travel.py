from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks


TRAVEL_BOARD_CHANNEL_ID = 1501983286321348658
KEYSTONE_AUDIT_CHANNEL_ID = 1473718234174718109


def format_minutes(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60

    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def format_dt(value: str | None) -> str:
    if not value:
        return "Unknown"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:f> (<t:{int(dt.timestamp())}:R>)"
    except Exception:
        return value


def time_remaining(eta_str: str | None) -> str:
    if not eta_str:
        return "Unknown"

    try:
        eta = datetime.fromisoformat(eta_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)

        if eta <= now:
            return "Arriving now"

        delta = eta - now
        total_minutes = int(delta.total_seconds() // 60)

        hours = total_minutes // 60
        mins = total_minutes % 60

        if hours and mins:
            return f"{hours}h {mins}m remaining"
        if hours:
            return f"{hours}h remaining"
        return f"{mins}m remaining"

    except Exception:
        return "Unknown"


def short_uuid(value: str) -> str:
    return str(value)[:8]


class PlayerTravelCancelSelect(discord.ui.Select):
    def __init__(self, cog: "TravelCog", rows: list[dict], reason: str | None):
        self.cog = cog
        self.reason = reason

        options: list[discord.SelectOption] = []
        for row in rows[:25]:
            oc = row.get("character_name") or "Unknown OC"
            route = f"{row['origin_city']} → {row['destination_city']}"
            method = row["method_display_name"]
            travel_id = str(row["travel_id"])

            options.append(
                discord.SelectOption(
                    label=f"{oc} | {route}"[:100],
                    description=f"{method} • ETA {format_dt(row.get('eta_at'))}"[:100],
                    value=travel_id,
                )
            )

        super().__init__(
            placeholder="Choose which travel to cancel...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        travel_id = self.values[0]

        try:
            res = (
                self.cog.sb()
                .rpc(
                    "cancel_travel_by_id_for_user",
                    {
                        "p_discord_id": interaction.user.id,
                        "p_travel_id": travel_id,
                        "p_reason": self.reason,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.response.send_message(
                    "❌ That travel could not be cancelled.",
                    ephemeral=True,
                )
                return

            data = res.data[0]
            oc = data.get("character_name") or "Unknown OC"

            embed = discord.Embed(
                title="🛑 Travel Cancelled",
                description=f"Cancelled travel for **{oc}**.",
                color=discord.Color.red(),
            )
            embed.add_field(
                name="Route",
                value=f"{data['origin_city']} → {data['destination_city']}",
                inline=False,
            )
            embed.add_field(name="Status", value=data["status"].title(), inline=True)

            if self.reason:
                embed.add_field(name="Reason", value=self.reason, inline=False)

            await interaction.response.edit_message(embed=embed, view=None)

            await self.cog.send_audit_log(
                title="🛑 Travel Cancelled",
                user=interaction.user,
                oc=oc,
                route=f"{data['origin_city']} → {data['destination_city']}",
                method=None,
                reason=self.reason,
                travel_id=travel_id,
            )
            await self.cog.update_board()

        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ Error cancelling travel:\n```{e}```",
                ephemeral=True,
            )


class PlayerTravelCancelView(discord.ui.View):
    def __init__(self, cog: "TravelCog", rows: list[dict], reason: str | None):
        super().__init__(timeout=120)
        self.add_item(PlayerTravelCancelSelect(cog, rows, reason))


class StaffTravelCancelSelect(discord.ui.Select):
    def __init__(self, cog: "TravelAdminCog", rows: list[dict], reason: str | None):
        self.cog = cog
        self.reason = reason
        self.row_by_id = {str(row["travel_id"]): row for row in rows}

        options: list[discord.SelectOption] = []
        for row in rows[:25]:
            oc = row.get("character_name") or "Unknown OC"
            leader = f"User {row['leader_discord_id']}"
            route = f"{row['origin_city']} → {row['destination_city']}"
            method = row["method_display_name"]
            travel_id = str(row["travel_id"])

            options.append(
                discord.SelectOption(
                    label=f"{oc} | {route}"[:100],
                    description=f"{method} • {leader} • {short_uuid(travel_id)}"[:100],
                    value=travel_id,
                )
            )

        super().__init__(
            placeholder="Choose a travel to cancel...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        travel_id = self.values[0]
        original_row = self.row_by_id.get(travel_id, {})

        try:
            res = (
                self.cog.sb()
                .rpc(
                    "staff_cancel_travel",
                    {
                        "p_travel_id": travel_id,
                        "p_staff_discord_id": interaction.user.id,
                        "p_reason": self.reason,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.response.send_message(
                    "❌ Travel not found or already inactive.",
                    ephemeral=True,
                )
                return

            data = res.data[0]

            embed = discord.Embed(
                title="🛑 Travel Cancelled (Staff)",
                color=discord.Color.dark_red(),
            )
            embed.add_field(
                name="Route",
                value=f"{data['origin_city']} → {data['destination_city']}",
                inline=False,
            )
            embed.add_field(name="Status", value=data["status"].title(), inline=True)

            if self.reason:
                embed.add_field(name="Reason", value=self.reason, inline=False)

            await interaction.response.edit_message(embed=embed, view=None)

            travel_cog = self.cog.bot.get_cog("TravelCog")
            if travel_cog:
                await travel_cog.send_audit_log(
                    title="🛑 Travel Cancelled by Staff",
                    user=interaction.user,
                    oc=original_row.get("character_name"),
                    route=f"{data['origin_city']} → {data['destination_city']}",
                    method=original_row.get("method_display_name"),
                    reason=self.reason,
                    travel_id=travel_id,
                )
                if hasattr(travel_cog, "update_board"):
                    await travel_cog.update_board()

        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ Error cancelling travel:\n```{e}```",
                ephemeral=True,
            )


class StaffTravelCancelView(discord.ui.View):
    def __init__(self, cog: "TravelAdminCog", rows: list[dict], reason: str | None):
        super().__init__(timeout=120)
        self.add_item(StaffTravelCancelSelect(cog, rows, reason))


class GroupTravelJoinSelect(discord.ui.Select):
    def __init__(self, cog: "TravelCog", rows: list[dict], oc: str | None):
        self.cog = cog
        self.oc = oc

        options: list[discord.SelectOption] = []
        for row in rows[:25]:
            leader = f"User {row['leader_discord_id']}"
            leader_oc = row.get("leader_character_name") or "Unknown OC"
            route = f"{row['origin_city']} → {row['destination_city']}"
            method = row["method_display_name"]
            count = row.get("passenger_count") or 0
            travel_id = str(row["travel_id"])

            options.append(
                discord.SelectOption(
                    label=f"{route} | {method}"[:100],
                    description=f"Leader: {leader_oc} • {leader} • Party: {count}"[:100],
                    value=travel_id,
                )
            )

        super().__init__(
            placeholder="Choose a group travel to join...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        travel_id = self.values[0]

        try:
            res = (
                self.cog.sb()
                .rpc(
                    "group_join_travel",
                    {
                        "p_travel_id": travel_id,
                        "p_discord_id": interaction.user.id,
                        "p_oc_name": self.oc,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.response.send_message(
                    "❌ Could not join that group travel.",
                    ephemeral=True,
                )
                return

            data = res.data[0]
            character_name = data.get("character_name") or self.oc or "Unknown OC"

            embed = discord.Embed(
                title="🚉 Joined Group Travel",
                description=f"{interaction.user.mention} joined as **{character_name}**.",
                color=discord.Color.green(),
            )
            embed.add_field(
                name="Route",
                value=f"{data['origin_city']} → {data['destination_city']}",
                inline=False,
            )
            embed.add_field(name="Method", value=data["method_display_name"], inline=True)
            embed.add_field(name="ETA", value=format_dt(data.get("eta_at")), inline=False)
            embed.set_footer(text=f"Travel ID: {data['travel_id']}")

            await interaction.response.edit_message(embed=embed, view=None)

            await self.cog.send_audit_log(
                title="🚉 Group Travel Joined",
                user=interaction.user,
                oc=character_name,
                route=f"{data['origin_city']} → {data['destination_city']}",
                method=data["method_display_name"],
                eta=format_dt(data.get("eta_at")),
                travel_id=str(data["travel_id"]),
            )
            await self.cog.update_board()

        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ Error joining group travel:\n```{e}```",
                ephemeral=True,
            )


class GroupTravelJoinView(discord.ui.View):
    def __init__(self, cog: "TravelCog", rows: list[dict], oc: str | None):
        super().__init__(timeout=120)
        self.add_item(GroupTravelJoinSelect(cog, rows, oc))


class TravelCog(commands.GroupCog, group_name="travel", group_description="Travel system"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

        if not self.arrival_loop.is_running():
            self.arrival_loop.start()

    def cog_unload(self):
        if self.arrival_loop.is_running():
            self.arrival_loop.cancel()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    async def get_channel_safe(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        return channel

    async def send_audit_log(
        self,
        *,
        title: str,
        user: discord.abc.User,
        oc: str | None,
        route: str,
        method: str | None,
        reason: str | None = None,
        travel_id: str | None = None,
        eta: str | None = None,
    ):
        channel = await self.get_channel_safe(KEYSTONE_AUDIT_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        embed = discord.Embed(title=title, color=discord.Color.dark_teal())
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)

        if oc:
            embed.add_field(name="OC", value=oc, inline=True)
        embed.add_field(name="Route", value=route, inline=False)

        if method:
            embed.add_field(name="Method", value=method, inline=True)
        if eta:
            embed.add_field(name="ETA", value=eta, inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        if travel_id:
            embed.set_footer(text=f"Travel ID: {travel_id}")

        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def send_arrival_notice(self, travel: dict):
        channel = await self.get_channel_safe(TRAVEL_BOARD_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        leader_id = travel.get("leader_discord_id")
        oc = travel.get("character_name") or "Unknown OC"

        embed = discord.Embed(
            title="✅ Arrival Notice",
            description=f"<@{leader_id}> has arrived as **{oc}**.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Route",
            value=f"{travel.get('origin_city')} → {travel.get('destination_city')}",
            inline=False,
        )
        embed.add_field(name="Method", value=str(travel.get("method_display_name") or "Unknown"), inline=True)
        embed.add_field(name="Arrived", value=format_dt(str(travel.get("arrived_at"))), inline=False)
        embed.set_footer(text=f"Travel ID: {travel.get('travel_id')}")

        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    async def oc_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            q = (current or "").strip().lower()
            res = (
                self.sb()
                .table("characters")
                .select("name,is_active")
                .eq("user_id", int(interaction.user.id))
                .order("name")
                .limit(50)
                .execute()
            )

            choices: list[app_commands.Choice[str]] = []

            for row in res.data or []:
                name = str(row.get("name") or "")
                active = bool(row.get("is_active"))
                label = f"{name} ⭐" if active else name

                if q and q not in name.lower():
                    continue

                choices.append(app_commands.Choice(name=label[:100], value=name))

            return choices[:25]

        except Exception:
            return []

    async def city_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            q = (current or "").strip().lower()
            res = (
                self.sb()
                .table("travel_cities")
                .select("name,is_active")
                .eq("is_active", True)
                .order("name")
                .limit(50)
                .execute()
            )

            choices: list[app_commands.Choice[str]] = []
            for row in res.data or []:
                name = str(row.get("name") or "")
                if q and q not in name.lower():
                    continue
                choices.append(app_commands.Choice(name=name[:100], value=name))

            return choices[:25]
        except Exception:
            return []

    async def method_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            q = (current or "").strip().lower()
            res = (
                self.sb()
                .table("travel_methods")
                .select("name,display_name,is_active,staff_only")
                .eq("is_active", True)
                .order("display_name")
                .limit(50)
                .execute()
            )

            can_staff = bool(interaction.user.guild_permissions.manage_guild if interaction.guild else False)
            choices: list[app_commands.Choice[str]] = []

            for row in res.data or []:
                if row.get("staff_only") and not can_staff:
                    continue

                name = str(row.get("name") or "")
                display = str(row.get("display_name") or name)
                searchable = f"{name} {display}".lower()

                if q and q not in searchable:
                    continue

                choices.append(app_commands.Choice(name=display[:100], value=name))

            return choices[:25]
        except Exception:
            return []

    @tasks.loop(seconds=60)
    async def arrival_loop(self):
        try:
            res = self.sb().rpc("process_travel_arrivals").execute()

            if res.data:
                for travel in res.data:
                    print(
                        "[Travel Arrival] "
                        f"{travel.get('origin_city')} → {travel.get('destination_city')} "
                        f"(Travel ID: {travel.get('travel_id')})"
                    )

                    await self.send_arrival_notice(travel)

                    user = self.bot.get_user(int(travel["leader_discord_id"]))
                    if user is None:
                        try:
                            user = await self.bot.fetch_user(int(travel["leader_discord_id"]))
                        except Exception:
                            user = None

                    if user:
                        await self.send_audit_log(
                            title="✅ Travel Arrived",
                            user=user,
                            oc=travel.get("character_name"),
                            route=f"{travel.get('origin_city')} → {travel.get('destination_city')}",
                            method=travel.get("method_display_name"),
                            travel_id=str(travel.get("travel_id")),
                        )

            await self.update_board()

        except Exception as e:
            print(f"[Travel Loop Error] {e}")

    @arrival_loop.before_loop
    async def before_arrival_loop(self):
        await self.bot.wait_until_ready()

    def build_board_embed(self, rows: list[dict]) -> discord.Embed:
        embed = discord.Embed(
            title="🚉 Doranswyr Travel Board",
            description="Live departures, arrivals, and disruptions.",
            color=discord.Color.teal(),
        )

        if not rows:
            embed.add_field(
                name="No Active Travel",
                value="The board is quiet right now.",
                inline=False,
            )
            embed.set_footer(text="Auto-updating travel board")
            return embed

        in_transit = []
        delayed = []
        arrived = []

        for row in rows:
            status = row.get("status")
            oc_name = row.get("character_name") or row.get("leader_character_name")
            count = row.get("passenger_count")
            count_text = f" | Party: {count}" if count else ""
            oc_text = f" | {oc_name}" if oc_name else ""

            base_line = (
                f"**{row['origin_city']} → {row['destination_city']}** "
                f"| {row['method_display_name']}{oc_text}{count_text}"
            )

            if status in ("delayed", "disrupted"):
                delayed.append(f"⚠️ {base_line} | ETA: {format_dt(row.get('eta_at'))}")
            elif status == "arrived":
                arrived.append(f"✅ {base_line} | Arrived: {format_dt(row.get('arrived_at'))}")
            else:
                in_transit.append(f"🚂 {base_line} | ETA: {format_dt(row.get('eta_at'))}")

        if in_transit:
            embed.add_field(
                name="Departures / In Transit",
                value="\n".join(in_transit[:10]),
                inline=False,
            )

        if delayed:
            embed.add_field(
                name="Delays / Disruptions",
                value="\n".join(delayed[:10]),
                inline=False,
            )

        if arrived:
            embed.add_field(
                name="Recent Arrivals",
                value="\n".join(arrived[:10]),
                inline=False,
            )

        embed.set_footer(text="Auto-updating travel board")
        return embed

    async def get_board_rows(self, guild_id: int) -> list[dict]:
        res = self.sb().rpc("get_travel_board", {"p_guild_id": guild_id}).execute()
        return res.data or []

    async def get_joinable_group_rows(self, guild_id: int) -> list[dict]:
        res = self.sb().rpc("get_joinable_group_travels", {"p_guild_id": guild_id}).execute()
        return res.data or []

    async def update_board(self):
        try:
            channel = await self.get_channel_safe(TRAVEL_BOARD_CHANNEL_ID)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return

            guild = getattr(channel, "guild", None)
            if guild is None:
                return

            sb = self.sb()
            config = (
                sb.table("travel_board_config")
                .select("*")
                .eq("guild_id", guild.id)
                .execute()
            )

            if not config.data:
                return

            message_id = config.data[0].get("message_id")
            rows = await self.get_board_rows(guild.id)
            embed = self.build_board_embed(rows)

            if message_id:
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.edit(embed=embed)
                    return
                except Exception as e:
                    print(f"[Travel Board] Could not edit existing board message: {e}")

            msg = await channel.send(embed=embed)

            sb.table("travel_board_config").upsert(
                {
                    "guild_id": guild.id,
                    "channel_id": channel.id,
                    "message_id": msg.id,
                }
            ).execute()

        except Exception as e:
            print(f"[Board Update Error] {e}")

    @app_commands.command(name="quote", description="Get a travel estimate between two cities.")
    @app_commands.describe(
        origin="Starting city",
        destination="Destination city",
        method="Travel method",
    )
    @app_commands.autocomplete(
        origin=city_autocomplete,
        destination=city_autocomplete,
        method=method_autocomplete,
    )
    async def quote(self, interaction: discord.Interaction, origin: str, destination: str, method: str):
        await interaction.response.defer()

        try:
            res = (
                self.sb()
                .rpc(
                    "travel_quote",
                    {
                        "p_origin_city": origin,
                        "p_destination_city": destination,
                        "p_method": method,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.followup.send(
                    f"❌ No valid route found for **{origin} → {destination}** using **{method}**.",
                    ephemeral=True,
                )
                return

            data = res.data[0]
            weather = data.get("weather") or {}
            weather_type = weather.get("weather_type", "Unknown")

            embed = discord.Embed(title="🚉 Travel Quote", color=discord.Color.blurple())
            embed.add_field(name="Route", value=f"{data['origin_city']} → {data['destination_city']}", inline=False)
            embed.add_field(name="Method", value=data["method_display_name"], inline=True)
            embed.add_field(name="Travel Time", value=format_minutes(data["final_minutes"]), inline=True)
            embed.add_field(name="Cost", value=str(data["final_cost"]), inline=True)
            embed.add_field(name="Risk", value=data["risk_level"].capitalize(), inline=True)
            embed.add_field(name="Weather", value=weather_type, inline=True)

            conditions = data.get("route_conditions") or []
            if conditions:
                embed.add_field(
                    name="⚠️ Active Conditions",
                    value="\n".join(c.get("condition_type", "Unknown") for c in conditions),
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error generating quote:\n```{e}```", ephemeral=True)

    @app_commands.command(name="start", description="Start solo travel between two cities.")
    @app_commands.describe(
        oc="OC traveling. If blank, your active selected OC is used.",
        origin="Starting city",
        destination="Destination city",
        method="Travel method",
    )
    @app_commands.autocomplete(
        oc=oc_autocomplete,
        origin=city_autocomplete,
        destination=city_autocomplete,
        method=method_autocomplete,
    )
    async def start(
        self,
        interaction: discord.Interaction,
        origin: str,
        destination: str,
        method: str,
        oc: str | None = None,
    ):
        await interaction.response.defer()

        try:
            res = (
                self.sb()
                .rpc(
                    "travel_start",
                    {
                        "p_guild_id": interaction.guild_id,
                        "p_leader_discord_id": interaction.user.id,
                        "p_origin_city": origin,
                        "p_destination_city": destination,
                        "p_method": method,
                        "p_oc_name": oc,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.followup.send("❌ Travel could not be started.", ephemeral=True)
                return

            data = res.data[0]
            weather = data.get("weather") or {}
            weather_type = weather.get("weather_type", "Unknown")
            character_name = data.get("character_name") or oc or "Unknown OC"

            embed = discord.Embed(
                title="🚂 Travel Started",
                description=f"{interaction.user.mention} has departed as **{character_name}**.",
                color=discord.Color.green(),
            )
            embed.add_field(name="OC", value=character_name, inline=True)
            embed.add_field(name="Route", value=f"{data['origin_city']} → {data['destination_city']}", inline=False)
            embed.add_field(name="Method", value=data["method_display_name"], inline=True)
            embed.add_field(name="Travel Time", value=format_minutes(data["final_minutes"]), inline=True)
            embed.add_field(name="Cost", value=str(data["final_cost"]), inline=True)
            embed.add_field(name="Risk", value=data["risk_level"].capitalize(), inline=True)
            embed.add_field(name="Weather", value=weather_type, inline=True)
            embed.add_field(name="ETA", value=format_dt(data.get("eta_at")), inline=False)

            conditions = data.get("route_conditions") or []
            if conditions:
                embed.add_field(
                    name="⚠️ Active Conditions",
                    value="\n".join(c.get("condition_type", "Unknown") for c in conditions),
                    inline=False,
                )

            embed.set_footer(text=f"Travel ID: {data['travel_id']}")
            await interaction.followup.send(embed=embed)

            await self.send_audit_log(
                title="🚂 Travel Started",
                user=interaction.user,
                oc=character_name,
                route=f"{data['origin_city']} → {data['destination_city']}",
                method=data["method_display_name"],
                eta=format_dt(data.get("eta_at")),
                travel_id=str(data["travel_id"]),
            )

            await self.update_board()

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error starting travel:\n```{e}```", ephemeral=True)

    @app_commands.command(name="group_start", description="Start group travel that others can join.")
    @app_commands.describe(
        oc="Leader OC traveling. If blank, your active selected OC is used.",
        origin="Starting city",
        destination="Destination city",
        method="Travel method",
    )
    @app_commands.autocomplete(
        oc=oc_autocomplete,
        origin=city_autocomplete,
        destination=city_autocomplete,
        method=method_autocomplete,
    )
    async def group_start(
        self,
        interaction: discord.Interaction,
        origin: str,
        destination: str,
        method: str,
        oc: str | None = None,
    ):
        await interaction.response.defer()

        try:
            res = (
                self.sb()
                .rpc(
                    "travel_group_start",
                    {
                        "p_guild_id": interaction.guild_id,
                        "p_leader_discord_id": interaction.user.id,
                        "p_origin_city": origin,
                        "p_destination_city": destination,
                        "p_method": method,
                        "p_oc_name": oc,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.followup.send("❌ Group travel could not be started.", ephemeral=True)
                return

            data = res.data[0]
            weather = data.get("weather") or {}
            weather_type = weather.get("weather_type", "Unknown")
            character_name = data.get("character_name") or oc or "Unknown OC"

            embed = discord.Embed(
                title="🚉 Group Travel Opened",
                description=(
                    f"{interaction.user.mention} opened group travel as **{character_name}**.\n"
                    f"Others can join with `/travel join`."
                ),
                color=discord.Color.green(),
            )
            embed.add_field(name="Leader OC", value=character_name, inline=True)
            embed.add_field(name="Route", value=f"{data['origin_city']} → {data['destination_city']}", inline=False)
            embed.add_field(name="Method", value=data["method_display_name"], inline=True)
            embed.add_field(name="Travel Time", value=format_minutes(data["final_minutes"]), inline=True)
            embed.add_field(name="Cost", value=str(data["final_cost"]), inline=True)
            embed.add_field(name="Risk", value=data["risk_level"].capitalize(), inline=True)
            embed.add_field(name="Weather", value=weather_type, inline=True)
            embed.add_field(name="ETA", value=format_dt(data.get("eta_at")), inline=False)
            embed.set_footer(text=f"Travel ID: {data['travel_id']}")

            await interaction.followup.send(embed=embed)

            await self.send_audit_log(
                title="🚉 Group Travel Opened",
                user=interaction.user,
                oc=character_name,
                route=f"{data['origin_city']} → {data['destination_city']}",
                method=data["method_display_name"],
                eta=format_dt(data.get("eta_at")),
                travel_id=str(data["travel_id"]),
            )

            await self.update_board()

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error starting group travel:\n```{e}```", ephemeral=True)

    @app_commands.command(name="join", description="Join an active group travel.")
    @app_commands.describe(oc="OC joining. If blank, your active selected OC is used.")
    @app_commands.autocomplete(oc=oc_autocomplete)
    async def join(self, interaction: discord.Interaction, oc: str | None = None):
        await interaction.response.defer(ephemeral=True)

        try:
            rows = await self.get_joinable_group_rows(interaction.guild_id)

            if not rows:
                await interaction.followup.send("❌ There are no joinable group travels right now.", ephemeral=True)
                return

            embed = discord.Embed(
                title="🚉 Join Group Travel",
                description="Pick the active group travel you want to join.",
                color=discord.Color.green(),
            )

            await interaction.followup.send(
                embed=embed,
                view=GroupTravelJoinView(self, rows, oc),
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error opening group join menu:\n```{e}```", ephemeral=True)

    @app_commands.command(name="status", description="Check your current travel status.")
    @app_commands.describe(oc="OC to check. If blank, your active selected OC is used.")
    @app_commands.autocomplete(oc=oc_autocomplete)
    async def status(self, interaction: discord.Interaction, oc: str | None = None):
        await interaction.response.defer(ephemeral=True)

        try:
            res = (
                self.sb()
                .rpc(
                    "get_active_travel",
                    {
                        "p_discord_id": interaction.user.id,
                        "p_oc_name": oc,
                    },
                )
                .execute()
            )

            if not res.data:
                await interaction.followup.send(
                    "🧍 No active travel found for that OC. If no OC was filled in, I checked your active selected OC.",
                    ephemeral=True,
                )
                return

            data = res.data[0]
            character_name = data.get("character_name")

            embed = discord.Embed(title="🧭 Travel Status", color=discord.Color.gold())

            if character_name:
                embed.add_field(name="OC", value=character_name, inline=True)

            embed.add_field(name="Route", value=f"{data['origin_city']} → {data['destination_city']}", inline=False)
            embed.add_field(name="Method", value=data["method_display_name"], inline=True)
            embed.add_field(name="Status", value=data["status"].replace("_", " ").title(), inline=True)
            embed.add_field(name="Departed", value=format_dt(data.get("departed_at")), inline=False)
            embed.add_field(name="ETA", value=format_dt(data.get("eta_at")), inline=False)
            embed.add_field(name="Time Remaining", value=time_remaining(data.get("eta_at")), inline=False)
            embed.set_footer(text=f"Travel ID: {data['travel_id']}")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error fetching travel status:\n```{e}```", ephemeral=True)

    @app_commands.command(name="history", description="View travel history for yourself or another user.")
    @app_commands.describe(
        user="Optional user to check. If blank, checks yourself.",
        oc="Optional OC name filter.",
    )
    @app_commands.autocomplete(oc=oc_autocomplete)
    async def history(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        oc: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)

        try:
            target = user or interaction.user

            res = (
                self.sb()
                .rpc("get_travel_history", {"p_discord_id": target.id, "p_limit": 15})
                .execute()
            )

            rows = res.data or []

            if oc:
                rows = [
                    row for row in rows
                    if str(row.get("character_name") or "").casefold() == oc.casefold()
                ]

            if not rows:
                await interaction.followup.send("📜 No travel history found.")
                return

            embed = discord.Embed(
                title=f"📜 Travel History — {target.display_name}",
                color=discord.Color.purple(),
            )

            lines = []
            for row in rows[:10]:
                status = row["status"].replace("_", " ").title()
                oc_name = row.get("character_name")
                oc_line = f"**OC:** {oc_name}\n" if oc_name else ""

                line = (
                    f"{oc_line}"
                    f"**{row['origin_city']} → {row['destination_city']}**\n"
                    f"{row['method_display_name']} | {status}\n"
                    f"Departed: {format_dt(row.get('departed_at'))}"
                )

                if row.get("arrived_at"):
                    line += f"\nArrived/Ended: {format_dt(row.get('arrived_at'))}"

                lines.append(line)

            embed.description = "\n\n".join(lines)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error loading history:\n```{e}```")

    @app_commands.command(name="cancel", description="Cancel one of your active travels.")
    @app_commands.describe(reason="Optional reason for cancellation")
    async def cancel(self, interaction: discord.Interaction, reason: str | None = None):
        await interaction.response.defer(ephemeral=True)

        try:
            res = self.sb().rpc("get_cancelable_travels", {"p_discord_id": interaction.user.id}).execute()
            rows = res.data or []

            if not rows:
                await interaction.followup.send("❌ You have no active travels to cancel.", ephemeral=True)
                return

            if len(rows) == 1:
                row = rows[0]
                cancel_res = (
                    self.sb()
                    .rpc(
                        "cancel_travel_by_id_for_user",
                        {
                            "p_discord_id": interaction.user.id,
                            "p_travel_id": row["travel_id"],
                            "p_reason": reason,
                        },
                    )
                    .execute()
                )

                data = cancel_res.data[0]
                oc = data.get("character_name") or "Unknown OC"

                embed = discord.Embed(
                    title="🛑 Travel Cancelled",
                    description=f"Cancelled travel for **{oc}**.",
                    color=discord.Color.red(),
                )
                embed.add_field(name="Route", value=f"{data['origin_city']} → {data['destination_city']}", inline=False)
                embed.add_field(name="Status", value=data["status"].title(), inline=True)

                if reason:
                    embed.add_field(name="Reason", value=reason, inline=False)

                await interaction.followup.send(embed=embed, ephemeral=True)

                await self.send_audit_log(
                    title="🛑 Travel Cancelled",
                    user=interaction.user,
                    oc=oc,
                    route=f"{data['origin_city']} → {data['destination_city']}",
                    method=row.get("method_display_name"),
                    reason=reason,
                    travel_id=str(row["travel_id"]),
                )

                await self.update_board()
                return

            embed = discord.Embed(
                title="🛑 Choose Travel to Cancel",
                description="You have multiple active travels. Pick the one you want to cancel.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(
                embed=embed,
                view=PlayerTravelCancelView(self, rows, reason),
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error cancelling travel:\n```{e}```", ephemeral=True)

    @app_commands.command(name="board", description="Show the public travel board.")
    async def board(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            rows = await self.get_board_rows(interaction.guild_id)
            embed = self.build_board_embed(rows)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error loading travel board:\n```{e}```", ephemeral=True)

    @app_commands.command(name="init_board", description="Initialize the auto-updating travel board.")
    async def init_board(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            channel = await self.get_channel_safe(TRAVEL_BOARD_CHANNEL_ID)

            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send(
                    "❌ The configured board channel is not a valid text channel.",
                    ephemeral=True,
                )
                return

            rows = await self.get_board_rows(interaction.guild_id)
            embed = self.build_board_embed(rows)

            msg = await channel.send(embed=embed)

            self.sb().table("travel_board_config").upsert(
                {
                    "guild_id": interaction.guild_id,
                    "channel_id": channel.id,
                    "message_id": msg.id,
                }
            ).execute()

            await interaction.followup.send(
                f"✅ Travel board initialized in {channel.mention}.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error initializing travel board:\n```{e}```", ephemeral=True)


class TravelAdminCog(commands.GroupCog, group_name="travel_admin", group_description="Travel admin commands"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    async def get_active_admin_rows(self, guild_id: int) -> list[dict]:
        res = self.sb().rpc("get_active_travels_admin", {"p_guild_id": guild_id}).execute()
        return res.data or []

    @app_commands.command(name="active", description="View active travels. Staff only.")
    async def active(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You do not have permission to use this.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            rows = await self.get_active_admin_rows(interaction.guild_id)

            if not rows:
                await interaction.followup.send("✅ No active travels right now.", ephemeral=True)
                return

            embed = discord.Embed(title="🧭 Active Travels", color=discord.Color.orange())

            lines = []
            for row in rows[:20]:
                short_id = short_uuid(row["travel_id"])
                leader = f"<@{row['leader_discord_id']}>"
                status = row["status"].replace("_", " ").title()
                oc_name = row.get("character_name")
                oc_text = f" | **OC:** {oc_name}" if oc_name else ""

                lines.append(
                    f"`{short_id}` | {leader}{oc_text}\n"
                    f"**{row['origin_city']} → {row['destination_city']}** | {row['method_display_name']}\n"
                    f"{status} | ETA: {format_dt(row.get('eta_at'))}"
                )

            embed.description = "\n\n".join(lines)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error loading active travels:\n```{e}```", ephemeral=True)

    @app_commands.command(name="cancel_menu", description="Cancel an active travel from a dropdown. Staff only.")
    @app_commands.describe(reason="Optional reason for cancellation")
    async def cancel_menu(self, interaction: discord.Interaction, reason: str | None = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You do not have permission to use this.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            rows = await self.get_active_admin_rows(interaction.guild_id)

            if not rows:
                await interaction.followup.send("✅ No active travels to cancel.", ephemeral=True)
                return

            embed = discord.Embed(
                title="🛑 Choose Travel to Cancel",
                description="Pick the active travel you want to cancel.",
                color=discord.Color.dark_red(),
            )

            await interaction.followup.send(
                embed=embed,
                view=StaffTravelCancelView(self, rows, reason),
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(f"⚠️ Error opening cancel menu:\n```{e}```", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TravelCog(bot))
    await bot.add_cog(TravelAdminCog(bot))