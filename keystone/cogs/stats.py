from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.stat_calculator import calculate_derived_stats
from services.stats_service import StatsService
from services.traits_service import TraitsService
from services.trait_modifier_service import apply_trait_modifiers


CORE_STAT_ORDER = [
    "strength",
    "dexterity",
    "stamina",
    "magic_affinity",
    "mana",
]

CORE_STAT_LABELS = {
    "strength": "Strength",
    "dexterity": "Dexterity",
    "stamina": "Stamina",
    "magic_affinity": "Magic Affinity",
    "mana": "Mana",
}

DERIVED_STAT_LABELS = {
    "reaction_score": "Reaction Score",
    "fortitude": "Fortitude",
    "safe_output": "Safe Output",
    "magic_safe_output": "Magic Safe Output",
    "ap": "Action Points",
    "carry_capacity": "Carry Capacity",
}


class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sb = bot.supabase
        self.stats = StatsService(self.sb)
        self.traits = TraitsService(self.sb)

    stats_group = app_commands.Group(name="stats", description="Character stat tools")

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return any(role.id in self.bot.staff_role_ids for role in member.roles)

    async def character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            user_id = interaction.user.id
            is_staff = self._is_staff(interaction)

            query = (
                self.sb.table("characters")
                .select("character_id,name,user_id,is_active")
                .order("name")
                .limit(100)
            )

            if not is_staff:
                query = query.eq("user_id", str(user_id))

            res = query.execute()
            rows = res.data or []

            current_lower = current.strip().lower()
            if current_lower:
                rows = [
                    row for row in rows
                    if current_lower in str(row.get("name", "")).lower()
                    or current_lower in str(row.get("character_id", "")).lower()
                    or current_lower in str(row.get("user_id", "")).lower()
                ]

            choices: list[app_commands.Choice[str]] = []
            for row in rows[:25]:
                name = str(row.get("name") or "Unnamed Character")
                char_id = str(row.get("character_id"))
                is_active = bool(row.get("is_active", False))
                status = "active" if is_active else "inactive"

                if is_staff:
                    owner = str(row.get("user_id", "unknown"))
                    label = f"{name} • {status} • {owner}"
                else:
                    label = f"{name} • {status}"

                choices.append(
                    app_commands.Choice(
                        name=label[:100],
                        value=char_id,
                    )
                )

            return choices
        except Exception:
            return []

    async def stat_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.strip().lower()

        choices = []
        for key in ["strength", "dexterity", "stamina", "magic_affinity", "mana"]:
            label = {
                "strength": "Strength",
                "dexterity": "Dexterity",
                "stamina": "Stamina",
                "magic_affinity": "magic_affinity",
                "mana": "Mana",
            }[key]
            if not current_lower or current_lower in key or current_lower in label.lower():
                choices.append(app_commands.Choice(name=label, value=key))

        return choices[:25]

    def _build_core_stat_map(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        core_map: dict[str, int] = {}

        for row in rows:
            stat_def = row["definition"]
            stat = row["stat"]

            raw_key = str(stat_def.get("key") or "").strip().lower()
            value = int(stat.get("value") or 0)

            # Map live DB keys to the calculator's expected keys
            if raw_key == "strength":
                core_map["strength"] = value
            elif raw_key == "dexterity":
                core_map["dexterity"] = value
            elif raw_key == "stamina":
                core_map["stamina"] = value
            elif raw_key in ("magic_affinity", "magic_affinity"):
                core_map["magic_affinity"] = value
            elif raw_key == "mana":
                core_map["mana"] = value
            else:
                # ignore derived / non-core / legacy rows like fortitude, luck, etc.
                continue

        for key in CORE_STAT_ORDER:
            core_map.setdefault(key, 0)

        return core_map

    def _apply_derived_multipliers(
        self,
        *,
        derived_stats: dict[str, int],
        derived_multipliers: dict[str, float],
    ) -> dict[str, int]:
        if not derived_multipliers:
            return dict(derived_stats)

        out = dict(derived_stats)

        for key, mult in derived_multipliers.items():
            if key not in out:
                continue
            try:
                out[key] = int(out[key] * float(mult))
            except Exception:
                continue

        return out

    def _get_character_row(self, character_id: str) -> dict[str, Any] | None:
        try:
            res = (
                self.sb.table("characters")
                .select("*")
                .eq("character_id", character_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _status_text(self, is_active: bool | None) -> str:
        if is_active is True:
            return "Active"
        if is_active is False:
            return "Inactive"
        return "Unknown"

    def _build_compact_view_message(
        self,
        *,
        character_name: str | None,
        is_active: bool | None,
        core_stats: dict[str, int],
        derived_stats: dict[str, int],
        luck: int = 0,
        carry_used: int = 0,
        carry_capacity: int | None = None,
        active_loadout_name: str | None = None,
        equipped_weapon: str | None = None,
        trait_names: list[str] | None = None,
    ) -> str:
        name = character_name or "Unknown Character"
        status = self._status_text(is_active)
        cc = carry_capacity if carry_capacity is not None else derived_stats["carry_capacity"]

        core_line = (
            f"**STR** {core_stats['strength']} | "
            f"**DEX** {core_stats['dexterity']} | "
            f"**STA** {core_stats['stamina']} | "
            f"**MAG** {core_stats['magic_affinity']} | "
            f"**MANA** {core_stats['mana']}"
        )

        derived_line = (
            f"**Reaction** {derived_stats['reaction_score']} | "
            f"**Fortitude** {derived_stats['fortitude']} | "
            f"**AP** {derived_stats['ap']} | "
            f"**CC** {carry_used}/{cc} | "
            f"**Luck** {luck:+d}"
        )

        output_line = (
            f"**Safe Output** {derived_stats['safe_output']} | "
            f"**Magic Output** {derived_stats['magic_safe_output']}"
        )

        gear_line = (
            f"**Loadout** {active_loadout_name or 'None'} | "
            f"**Weapon** {equipped_weapon or 'None'}"
        )

        traits_line = ""
        if trait_names:
            traits_line = "\n**Traits** " + ", ".join(trait_names[:8])

        return f"## {name} ({status})\n{core_line}\n{derived_line}\n{output_line}\n{gear_line}{traits_line}"

    def _build_sheet_embed(
        self,
        *,
        character_id: str,
        character_row: dict[str, Any],
        core_stats: dict[str, int],
        derived_stats: dict[str, int],
        luck: int,
        carry_used: int,
        traits: list[str],
        active_effects: list[str],
        equipped_weapon: str | None,
        active_loadout_name: str | None,
        warnings: list[str],
        carry_capacity_total: int,
    ) -> discord.Embed:
        character_name = str(character_row.get("name") or "Unnamed Character")
        status_text = self._status_text(character_row.get("is_active"))

        embed = discord.Embed(
            title=f"{character_name} — Character Sheet",
            description=(
                f"Character ID: `{character_id}`\n"
                f"Status: **{status_text}**"
            ),
            color=discord.Color.orange(),
        )

        core_lines = [
            f"**{CORE_STAT_LABELS[key]}**: {core_stats.get(key, 0)}"
            for key in CORE_STAT_ORDER
        ]

        derived_lines = [
            f"**Reaction Score**: {derived_stats['reaction_score']}",
            f"**Fortitude**: {derived_stats['fortitude']}",
            f"**Safe Output**: {derived_stats['safe_output']}",
            f"**Magic Safe Output**: {derived_stats['magic_safe_output']}",
            f"**Action Points**: {derived_stats['ap']}",
            f"**Carry Capacity**: {carry_capacity_total}",
        ]

        support_lines = [
            f"**Luck**: {luck:+d}",
            f"**Carry Usage**: {carry_used}/{carry_capacity_total}",
            f"**Active Loadout**: {active_loadout_name or 'None'}",
            f"**Equipped Weapon**: {equipped_weapon or 'None'}",
        ]

        trait_text = "\n".join(f"• {t}" for t in traits) if traits else "No traits tracked."
        effects_text = "\n".join(f"• {e}" for e in active_effects) if active_effects else "No active effects."
        warnings_text = "\n".join(f"• {w}" for w in warnings) if warnings else "No warnings."

        embed.add_field(name="Core Stats", value="\n".join(core_lines), inline=False)
        embed.add_field(name="Derived Stats", value="\n".join(derived_lines), inline=False)
        embed.add_field(name="Combat / Utility", value="\n".join(support_lines), inline=False)
        embed.add_field(name="Traits", value=trait_text, inline=False)
        embed.add_field(name="Active Effects", value=effects_text, inline=False)
        embed.add_field(name="Warnings", value=warnings_text, inline=False)

        embed.set_footer(
            text="Core stats are stored. Trait modifiers are applied. Derived stats are then calculated automatically."
        )
        return embed

    def _get_active_effects(self, character_row: dict[str, Any]) -> list[str]:
        raw = character_row.get("active_effects")
        if isinstance(raw, list):
            return [str(x) for x in raw if x]
        if isinstance(raw, str) and raw.strip():
            return [x.strip() for x in raw.split(",") if x.strip()]
        return []

    def _get_active_loadout_name(self, character_row: dict[str, Any]) -> str | None:
        raw = character_row.get("active_loadout_name")
        if raw is None:
            return None
        txt = str(raw).strip()
        return txt or None

    def _get_carry_used_from_inventory(
        self,
        *,
        guild_id: int,
        character_id: str,
    ) -> int:
        try:
            ent_res = (
                self.sb.table("inventory_entries")
                .select("item_id,qty")
                .eq("guild_id", guild_id)
                .eq("character_id", character_id)
                .execute()
            )
            entries = ent_res.data or []
            if not entries:
                return 0

            item_ids = [str(e["item_id"]) for e in entries if e.get("item_id")]
            if not item_ids:
                return 0

            items_res = (
                self.sb.table("items")
                .select("item_id,wu,is_active")
                .eq("guild_id", guild_id)
                .in_("item_id", item_ids)
                .execute()
            )
            items = items_res.data or []

            by_id: dict[str, dict[str, Any]] = {
                str(item["item_id"]): item for item in items
            }

            total = 0
            for entry in entries:
                item_id = str(entry.get("item_id"))
                qty = int(entry.get("qty") or 0)
                item = by_id.get(item_id)

                if not item:
                    continue
                if item.get("is_active") is False:
                    continue

                wu = item.get("wu")
                try:
                    wu_value = int(wu) if wu is not None else 0
                except Exception:
                    wu_value = 0

                total += wu_value * qty

            return total
        except Exception:
            return 0

    def _get_carry_used(self, character_row: dict[str, Any], guild_id: int | None = None) -> int:
        character_id = character_row.get("character_id")
        if guild_id is not None and character_id:
            real_total = self._get_carry_used_from_inventory(
                guild_id=guild_id,
                character_id=str(character_id),
            )
            if real_total >= 0:
                return real_total

        for key in ("carry_used", "current_carry", "carry_weight"):
            value = character_row.get(key)
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    pass
        return 0

    def _get_active_loadout_row(
        self,
        *,
        guild_id: int,
        character_id: str,
        loadout_name: str | None,
    ) -> dict[str, Any] | None:
        if not loadout_name:
            return None

        try:
            res = (
                self.sb.table("inventory_loadouts")
                .select("*")
                .eq("guild_id", guild_id)
                .eq("character_id", character_id)
                .eq("loadout_name", loadout_name)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _get_equipped_weapon_from_loadout(
        self,
        *,
        guild_id: int,
        loadout_row: dict[str, Any] | None,
    ) -> str | None:
        if not loadout_row:
            return None

        items_map = loadout_row.get("items") or {}
        if not isinstance(items_map, dict) or not items_map:
            return None

        item_ids = [str(item_id) for item_id, qty in items_map.items() if int(qty or 0) > 0]
        if not item_ids:
            return None

        try:
            res = (
                self.sb.table("items")
                .select("item_id,name,item_class,is_active")
                .eq("guild_id", guild_id)
                .in_("item_id", item_ids)
                .execute()
            )
            items = res.data or []
        except Exception:
            return None

        weapon_classes = {
            "weapon",
            "gun",
            "sword",
            "blade",
            "bow",
            "staff",
            "wand",
            "firearm",
        }

        for item in items:
            if item.get("is_active") is False:
                continue

            item_class = str(item.get("item_class") or "").strip().lower()
            if item_class in weapon_classes:
                name = item.get("name")
                if name:
                    return str(name)

        for item in items:
            if item.get("is_active") is False:
                continue
            name = item.get("name")
            if name:
                return str(name)

        return None

    def _build_warnings(
        self,
        *,
        carry_capacity_total: int,
        derived_stats: dict[str, int],
        carry_used: int,
        equipped_weapon: str | None,
        active_effects: list[str],
        active_loadout_name: str | None,
    ) -> list[str]:
        warnings: list[str] = []

        if carry_used > carry_capacity_total:
            warnings.append("Over carry capacity.")
        elif carry_used == carry_capacity_total:
            warnings.append("At maximum carry capacity.")

        if derived_stats["ap"] <= 1:
            warnings.append("Low action point economy.")

        if derived_stats["safe_output"] <= 0:
            warnings.append("Physical safe output is critically low.")

        if derived_stats["magic_safe_output"] <= 0:
            warnings.append("Magical safe output is critically low.")

        if not active_loadout_name:
            warnings.append("No active loadout equipped.")

        if not equipped_weapon:
            warnings.append("No equipped weapon tracked.")

        if active_effects:
            warnings.append("Active effects may modify combat performance.")

        return warnings

    def _get_stat_name_map(self, guild_id: int) -> dict[str, str]:
        try:
            res = (
                self.sb.table("stat_definitions")
                .select("stat_key,display_name")
                .execute()
            )
            rows = res.data or []
            return {
                str(row["stat_key"]): str(row.get("display_name") or row.get("stat_key") or "Unknown Stat")
                for row in rows
            }
        except Exception:
            return {}

    def _get_stat_history(
        self,
        *,
        guild_id: int,
        character_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            res = (
                self.sb.table("oc_stat_changes")
                .select("created_at, old_value, new_value, delta, reason, actor_discord_id, stat_key, xp_cost")
                .eq("guild_id", guild_id)
                .eq("character_id", character_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception:
            return []

    def _build_history_embed(
        self,
        *,
        character_id: str,
        character_row: dict[str, Any],
        history_rows: list[dict[str, Any]],
        stat_name_map: dict[str, str],
        requested_limit: int,
    ) -> discord.Embed:
        character_name = str(character_row.get("name") or "Unnamed Character")
        status_text = self._status_text(character_row.get("is_active"))

        embed = discord.Embed(
            title=f"{character_name} — Stat History",
            description=(
                f"Character ID: `{character_id}`\n"
                f"Status: **{status_text}**\n"
                f"Showing up to **{requested_limit}** most recent changes."
            ),
            color=discord.Color.orange(),
        )

        if not history_rows:
            embed.add_field(
                name="Recent Changes",
                value="No stat history found for this character.",
                inline=False,
            )
            return embed

        lines: list[str] = []
        for row in history_rows:
            stat_name = stat_name_map.get(str(row.get("stat_key")), "Unknown Stat")
            old_value = row.get("old_value")
            new_value = row.get("new_value")
            delta = int(row.get("delta") or 0)
            reason = row.get("reason") or "No reason provided"
            actor_id = row.get("actor_discord_id")
            created_at = row.get("created_at") or "Unknown time"
            xp_cost = row.get("xp_cost")

            actor_text = f"<@{actor_id}>" if actor_id else "Unknown actor"
            delta_text = f"{delta:+d}"
            xp_line = f"\nXP Cost: `{xp_cost}`" if xp_cost not in (None, 0) else ""

            lines.append(
                f"**{stat_name}** — `{old_value}` → `{new_value}` ({delta_text}){xp_line}\n"
                f"By: {actor_text}\n"
                f"Reason: {reason}\n"
                f"When: `{created_at}`"
            )

        embed.add_field(
            name="Recent Changes",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )

        return embed

    async def _load_sheet_payload(
        self,
        interaction: discord.Interaction,
        character_id: str,
    ) -> dict[str, Any] | None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return None

        guild_id = interaction.guild_id
        character_row = self._get_character_row(character_id)

        if not character_row:
            await interaction.response.send_message(
                "That character could not be found.",
                ephemeral=True,
            )
            return None

        rows = self.stats.get_all_character_stats(
            guild_id=guild_id,
            character_id=character_id,
            include_hidden=False,
        )

        if not rows:
            await interaction.response.send_message(
                "No stat definitions exist for this server yet.",
                ephemeral=True,
            )
            return None

        base_core_stats = self._build_core_stat_map(rows)

        trait_bundles = self.traits.get_character_traits(
            guild_id=guild_id,
            character_id=character_id,
        )

        trait_result = apply_trait_modifiers(
            core_stats=base_core_stats,
            trait_bundles=trait_bundles,
        )

        core_stats = trait_result["core_stats"]
        trait_extras = trait_result["extras"]

        derived_stats = calculate_derived_stats(core_stats)

        base_luck = 0
        carry_used = self._get_carry_used(character_row, guild_id=guild_id)
        active_effects = self._get_active_effects(character_row)
        active_loadout_name = self._get_active_loadout_name(character_row)
        active_loadout_row = self._get_active_loadout_row(
            guild_id=guild_id,
            character_id=character_id,
            loadout_name=active_loadout_name,
        )
        equipped_weapon = self._get_equipped_weapon_from_loadout(
            guild_id=guild_id,
            loadout_row=active_loadout_row,
        )

        trait_names = [
            str(bundle.get("trait", {}).get("name"))
            for bundle in trait_bundles
            if bundle.get("trait", {}).get("name")
        ]

        total_luck = base_luck + int(trait_extras.get("luck_bonus", 0))
        carry_capacity_total = int(derived_stats["carry_capacity"]) + int(trait_extras.get("carry_capacity_bonus", 0))

        warnings = self._build_warnings(
            carry_capacity_total=carry_capacity_total,
            derived_stats=derived_stats,
            carry_used=carry_used,
            equipped_weapon=equipped_weapon,
            active_effects=active_effects,
            active_loadout_name=active_loadout_name,
        )

        return {
            "character_row": character_row,
            "core_stats": core_stats,
            "derived_stats": derived_stats,
            "luck": total_luck,
            "carry_used": carry_used,
            "traits": trait_names,
            "active_effects": active_effects,
            "active_loadout_name": active_loadout_name,
            "equipped_weapon": equipped_weapon,
            "warnings": warnings,
            "carry_capacity_total": carry_capacity_total,
        }

    @stats_group.command(name="view", description="Quick view of a character's stats")
    @app_commands.describe(character="Select a character")
    @app_commands.autocomplete(character=character_autocomplete)
    async def view_stats(self, interaction: discord.Interaction, character: str):
        payload = await self._load_sheet_payload(interaction, character)
        if payload is None:
            return

        msg = self._build_compact_view_message(
            character_name=payload["character_row"].get("name"),
            is_active=payload["character_row"].get("is_active"),
            core_stats=payload["core_stats"],
            derived_stats=payload["derived_stats"],
            luck=payload["luck"],
            carry_used=payload["carry_used"],
            carry_capacity=payload["carry_capacity_total"],
            active_loadout_name=payload["active_loadout_name"],
            equipped_weapon=payload["equipped_weapon"],
            trait_names=payload["traits"],
        )

        await interaction.response.send_message(msg)

    @stats_group.command(name="sheet", description="View a full character sheet")
    @app_commands.describe(character="Select a character")
    @app_commands.autocomplete(character=character_autocomplete)
    async def sheet_stats(self, interaction: discord.Interaction, character: str):
        payload = await self._load_sheet_payload(interaction, character)
        if payload is None:
            return

        embed = self._build_sheet_embed(
            character_id=character,
            character_row=payload["character_row"],
            core_stats=payload["core_stats"],
            derived_stats=payload["derived_stats"],
            luck=payload["luck"],
            carry_used=payload["carry_used"],
            traits=payload["traits"],
            active_effects=payload["active_effects"],
            equipped_weapon=payload["equipped_weapon"],
            active_loadout_name=payload["active_loadout_name"],
            warnings=payload["warnings"],
            carry_capacity_total=payload["carry_capacity_total"],
        )

        await interaction.response.send_message(embed=embed)

    @stats_group.command(name="history", description="View recent stat changes for a character")
    @app_commands.describe(
        character="Select a character",
        limit="How many recent changes to show (1-25)",
    )
    @app_commands.autocomplete(character=character_autocomplete)
    async def history_stats(
        self,
        interaction: discord.Interaction,
        character: str,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        character_id = character
        character_row = self._get_character_row(character_id)

        if not character_row:
            await interaction.response.send_message(
                "That character could not be found.",
                ephemeral=True,
            )
            return

        history_rows = self._get_stat_history(
            guild_id=guild_id,
            character_id=character_id,
            limit=limit,
        )
        stat_name_map = self._get_stat_name_map(guild_id)

        embed = self._build_history_embed(
            character_id=character_id,
            character_row=character_row,
            history_rows=history_rows,
            stat_name_map=stat_name_map,
            requested_limit=limit,
        )

        await interaction.response.send_message(embed=embed)

    @stats_group.command(name="add", description="Add to a character stat")
    @app_commands.describe(
        character="Select a character",
        stat="Select a core stat",
        amount="How much to add or subtract",
        reason="Optional reason for the change",
    )
    @app_commands.autocomplete(character=character_autocomplete, stat=stat_autocomplete)
    async def add_stat(
        self,
        interaction: discord.Interaction,
        character: str,
        stat: str,
        amount: int,
        reason: str | None = None,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not self._is_staff(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return

        character_id = character
        guild_id = interaction.guild_id
        stat_key = self.stats.normalize_stat_key(stat)

        try:
            result = self.stats.add_stat(
                guild_id=guild_id,
                character_id=character_id,
                stat_key=stat_key,
                amount=amount,
                actor_discord_id=interaction.user.id,
                reason=reason,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to update stat: {e}",
                ephemeral=True,
            )
            return

        character_row = self._get_character_row(character_id)
        character_name = (
            str(character_row.get("name"))
            if character_row and character_row.get("name")
            else character_id
        )
        stat_name = result["definition"]["display_name"]

        await interaction.response.send_message(
            f"Updated **{stat_name}** for **{character_name}**:\n"
            f"`{result['old_value']}` → **`{result['new_value']}`** "
            f"(`{result['delta']:+d}`)"
        )

    @stats_group.command(name="set", description="Set a character stat to an exact value")
    @app_commands.describe(
        character="Select a character",
        stat="Select a core stat",
        value="The exact value to set",
        reason="Optional reason for the change",
    )
    @app_commands.autocomplete(character=character_autocomplete, stat=stat_autocomplete)
    async def set_stat(
        self,
        interaction: discord.Interaction,
        character: str,
        stat: str,
        value: int,
        reason: str | None = None,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not self._is_staff(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return

        character_id = character
        guild_id = interaction.guild_id
        stat_key = self.stats.normalize_stat_key(stat)

        try:
            result = self.stats.set_stat(
                guild_id=guild_id,
                character_id=character_id,
                stat_key=stat_key,
                new_value=value,
                actor_discord_id=interaction.user.id,
                reason=reason,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to set stat: {e}",
                ephemeral=True,
            )
            return

        character_row = self._get_character_row(character_id)
        character_name = (
            str(character_row.get("name"))
            if character_row and character_row.get("name")
            else character_id
        )
        stat_name = result["definition"]["display_name"]

        await interaction.response.send_message(
            f"Set **{stat_name}** for **{character_name}**:\n"
            f"`{result['old_value']}` → **`{result['new_value']}`** "
            f"(`{result['delta']:+d}`)"
        )


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
