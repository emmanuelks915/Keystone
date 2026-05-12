from __future__ import annotations

import random
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

CHECK_TARGETS = {
    "strength": "Strength",
    "dexterity": "Dexterity",
    "stamina": "Stamina",
    "magic_affinity": "Magic Affinity",
    "mana": "Mana",
    "reaction_score": "Reaction Score",
    "fortitude": "Fortitude",
    "safe_output": "Safe Output",
    "magic_safe_output": "Magic Safe Output",
    "ap": "Action Points",
    "carry_capacity": "Carry Capacity",
    "perception": "Perception",
    "persuasion": "Persuasion",
    "intimidation": "Intimidation",
    "dodge": "Dodge",
}

CHECK_MODES = {
    "normal": "Normal",
    "advantage": "Advantage",
    "disadvantage": "Disadvantage",
}


class ChecksCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sb = bot.supabase
        self.stats = StatsService(self.sb)
        self.traits = TraitsService(self.sb)

    check_group = app_commands.Group(name="check", description="Run stat and trait-based checks")

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

            q = current.strip().lower()
            if q:
                rows = [
                    row for row in rows
                    if q in str(row.get("name", "")).lower()
                ]

            out: list[app_commands.Choice[str]] = []
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

                out.append(app_commands.Choice(name=label[:100], value=char_id))

            return out
        except Exception:
            return []

    async def target_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        q = current.strip().lower()

        out: list[app_commands.Choice[str]] = []
        for key, label in CHECK_TARGETS.items():
            if not q or q in key or q in label.lower():
                out.append(app_commands.Choice(name=label, value=key))

        return out[:25]

    async def mode_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        q = current.strip().lower()

        out: list[app_commands.Choice[str]] = []
        for key, label in CHECK_MODES.items():
            if not q or q in key or q in label.lower():
                out.append(app_commands.Choice(name=label, value=key))

        return out[:25]

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

    def _build_core_stat_map(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        core_map: dict[str, int] = {}

        for row in rows:
            stat_def = row["definition"]
            stat = row["stat"]
            key = self.stats.normalize_stat_key(stat_def["key"])
            core_map[key] = int(stat["value"])

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

    def _get_roll_modifier_total(
        self,
        *,
        trait_bundles: list[dict[str, Any]],
        target: str,
    ) -> tuple[int, list[str]]:
        total = 0
        sources: list[str] = []

        for bundle in trait_bundles:
            trait = bundle.get("trait") or {}
            trait_name = str(trait.get("name") or "Trait")
            effects = trait.get("effects_json") or {}
            roll_modifiers = effects.get("roll_modifiers") or []

            if not isinstance(roll_modifiers, list):
                continue

            for mod in roll_modifiers:
                if not isinstance(mod, dict):
                    continue

                mod_target = str(mod.get("target") or "").strip().lower()
                if mod_target != target:
                    continue

                try:
                    value = int(mod.get("value") or 0)
                except Exception:
                    continue

                total += value
                sources.append(f"{trait_name} ({value:+d})")

        return total, sources

    def _resolve_rolls(self, mode: str) -> tuple[int, int | None, int]:
        roll1 = random.randint(1, 20)

        if mode == "advantage":
            roll2 = random.randint(1, 20)
            final = max(roll1, roll2)
            return roll1, roll2, final

        if mode == "disadvantage":
            roll2 = random.randint(1, 20)
            final = min(roll1, roll2)
            return roll1, roll2, final

        return roll1, None, roll1

    def _load_check_context(
        self,
        *,
        guild_id: int,
        character_id: str,
        target: str,
    ) -> dict[str, Any] | None:
        character_row = self._get_character_row(character_id)
        if not character_row:
            return None

        rows = self.stats.get_all_character_stats(
            guild_id=guild_id,
            character_id=character_id,
            include_hidden=False,
        )
        if not rows:
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
        derived_stats = self._apply_derived_multipliers(
            derived_stats=derived_stats,
            derived_multipliers=trait_extras.get("derived_multipliers", {}),
        )

        if target in core_stats:
            base_value = int(core_stats[target])
        elif target in derived_stats:
            base_value = int(derived_stats[target])
        else:
            base_value = 0

        roll_bonus, roll_sources = self._get_roll_modifier_total(
            trait_bundles=trait_bundles,
            target=target,
        )

        return {
            "character_row": character_row,
            "core_stats": core_stats,
            "derived_stats": derived_stats,
            "trait_bundles": trait_bundles,
            "base_value": base_value,
            "roll_bonus": roll_bonus,
            "roll_sources": roll_sources,
        }

    def _log_check(
        self,
        *,
        guild_id: int,
        runner_discord_id: int | None,
        character_id: str,
        opponent_character_id: str | None,
        check_type: str,
        target: str,
        mode: str,
        base_value: int,
        roll_bonus: int,
        die_roll_1: int | None,
        die_roll_2: int | None,
        die_roll_final: int,
        total: int,
        dc: int | None,
        outcome: str | None,
        winner_character_id: str | None,
        note: str | None,
    ) -> None:
        try:
            row = {
                "guild_id": guild_id,
                "runner_discord_id": runner_discord_id,
                "character_id": character_id,
                "opponent_character_id": opponent_character_id,
                "check_type": check_type,
                "target": target,
                "mode": mode,
                "base_value": base_value,
                "roll_bonus": roll_bonus,
                "die_roll_1": die_roll_1,
                "die_roll_2": die_roll_2,
                "die_roll_final": die_roll_final,
                "total": total,
                "dc": dc,
                "outcome": outcome,
                "winner_character_id": winner_character_id,
                "note": note,
            }
            self.sb.table("check_logs").insert(row).execute()
        except Exception:
            pass

    def _get_check_history(
        self,
        *,
        guild_id: int,
        character_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            res = (
                self.sb.table("check_logs")
                .select("*")
                .eq("guild_id", guild_id)
                .eq("character_id", character_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception:
            return []

    def _build_check_embed(
        self,
        *,
        character_name: str,
        target_label: str,
        mode: str,
        base_value: int,
        roll_bonus: int,
        roll_sources: list[str],
        die_roll_1: int,
        die_roll_2: int | None,
        die_roll_final: int,
        total: int,
        dc: int | None,
        note: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{character_name} — Check",
            color=discord.Color.orange(),
        )

        result_lines = [
            f"**Target**: {target_label}",
            f"**Mode**: {CHECK_MODES.get(mode, mode.title())}",
            f"**Base Value**: {base_value}",
            f"**Trait Bonus**: {roll_bonus:+d}",
        ]

        if die_roll_2 is None:
            result_lines.append(f"**d20 Roll**: {die_roll_final}")
        else:
            result_lines.append(f"**Rolls**: {die_roll_1}, {die_roll_2}")
            result_lines.append(f"**Chosen Roll**: {die_roll_final}")

        result_lines.append(f"**Total**: {total}")

        if dc is not None:
            outcome = "PASS" if total >= dc else "FAIL"
            result_lines.append(f"**DC**: {dc}")
            result_lines.append(f"**Outcome**: **{outcome}**")

        embed.add_field(
            name="Result",
            value="\n".join(result_lines),
            inline=False,
        )

        if roll_sources:
            embed.add_field(
                name="Trait Modifiers",
                value="\n".join(f"• {src}" for src in roll_sources)[:1024],
                inline=False,
            )

        if note:
            embed.add_field(
                name="Note",
                value=note[:1024],
                inline=False,
            )

        return embed

    def _build_contest_embed(
        self,
        *,
        target_label: str,
        mode: str,
        a_name: str,
        a_base: int,
        a_bonus: int,
        a_roll_1: int,
        a_roll_2: int | None,
        a_roll_final: int,
        a_total: int,
        a_sources: list[str],
        b_name: str,
        b_base: int,
        b_bonus: int,
        b_roll_1: int,
        b_roll_2: int | None,
        b_roll_final: int,
        b_total: int,
        b_sources: list[str],
        winner_text: str,
        note: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Contest — {target_label}",
            color=discord.Color.gold(),
        )
        embed.description = f"**Mode**: {CHECK_MODES.get(mode, mode.title())}\n**Winner**: {winner_text}"

        def side_block(name: str, base: int, bonus: int, r1: int, r2: int | None, rf: int, total: int) -> str:
            lines = [
                f"**Base Value**: {base}",
                f"**Trait Bonus**: {bonus:+d}",
            ]
            if r2 is None:
                lines.append(f"**d20 Roll**: {rf}")
            else:
                lines.append(f"**Rolls**: {r1}, {r2}")
                lines.append(f"**Chosen Roll**: {rf}")
            lines.append(f"**Total**: {total}")
            return "\n".join(lines)

        embed.add_field(name=a_name, value=side_block(a_name, a_base, a_bonus, a_roll_1, a_roll_2, a_roll_final, a_total), inline=True)
        embed.add_field(name=b_name, value=side_block(b_name, b_base, b_bonus, b_roll_1, b_roll_2, b_roll_final, b_total), inline=True)

        if a_sources:
            embed.add_field(name=f"{a_name} Trait Modifiers", value="\n".join(f"• {s}" for s in a_sources)[:1024], inline=False)
        if b_sources:
            embed.add_field(name=f"{b_name} Trait Modifiers", value="\n".join(f"• {s}" for s in b_sources)[:1024], inline=False)

        if note:
            embed.add_field(name="Note", value=note[:1024], inline=False)

        return embed

    def _build_history_embed(
        self,
        *,
        character_name: str,
        history_rows: list[dict[str, Any]],
        requested_limit: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{character_name} — Check History",
            description=f"Showing up to **{requested_limit}** most recent logged checks.",
            color=discord.Color.blurple(),
        )

        if not history_rows:
            embed.add_field(
                name="Recent Checks",
                value="No check history found for this character.",
                inline=False,
            )
            return embed

        lines: list[str] = []
        for row in history_rows:
            target = str(row.get("target") or "unknown")
            mode = str(row.get("mode") or "normal")
            total = int(row.get("total") or 0)
            die_roll_final = int(row.get("die_roll_final") or 0)
            base_value = int(row.get("base_value") or 0)
            roll_bonus = int(row.get("roll_bonus") or 0)
            dc = row.get("dc")
            outcome = row.get("outcome") or "—"
            created_at = str(row.get("created_at") or "Unknown time")

            line = (
                f"**{CHECK_TARGETS.get(target, target.title())}** "
                f"({CHECK_MODES.get(mode, mode.title())})\n"
                f"`Base {base_value}` + `Bonus {roll_bonus:+d}` + `Roll {die_roll_final}` = **{total}**\n"
                f"DC: `{dc if dc is not None else '—'}` • Outcome: `{outcome}`\n"
                f"When: `{created_at}`"
            )
            lines.append(line)

        embed.add_field(
            name="Recent Checks",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )
        return embed

    @check_group.command(name="run", description="Run a stat or trait-based check")
    @app_commands.describe(
        character="Select a character",
        target="What to check",
        mode="Roll mode",
        dc="Optional DC to compare against",
        note="Optional note/context for the check",
    )
    @app_commands.autocomplete(
        character=character_autocomplete,
        target=target_autocomplete,
        mode=mode_autocomplete,
    )
    async def run_check(
        self,
        interaction: discord.Interaction,
        character: str,
        target: str,
        mode: str = "normal",
        dc: int | None = None,
        note: str | None = None,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        character_id = character
        target = target.strip().lower()
        mode = mode.strip().lower()

        if mode not in CHECK_MODES:
            await interaction.response.send_message(
                "Mode must be one of: normal, advantage, disadvantage.",
                ephemeral=True,
            )
            return

        context = self._load_check_context(
            guild_id=guild_id,
            character_id=character_id,
            target=target,
        )
        if context is None:
            await interaction.response.send_message(
                "That character or stat context could not be loaded.",
                ephemeral=True,
            )
            return

        roll_1, roll_2, roll_final = self._resolve_rolls(mode)
        total = int(context["base_value"]) + int(context["roll_bonus"]) + int(roll_final)

        character_name = str(context["character_row"].get("name") or "Unnamed Character")
        target_label = CHECK_TARGETS.get(target, target.replace("_", " ").title())

        embed = self._build_check_embed(
            character_name=character_name,
            target_label=target_label,
            mode=mode,
            base_value=int(context["base_value"]),
            roll_bonus=int(context["roll_bonus"]),
            roll_sources=context["roll_sources"],
            die_roll_1=roll_1,
            die_roll_2=roll_2,
            die_roll_final=roll_final,
            total=total,
            dc=dc,
            note=note,
        )

        outcome: str | None = None
        if dc is not None:
            outcome = "PASS" if total >= dc else "FAIL"

        self._log_check(
            guild_id=guild_id,
            runner_discord_id=interaction.user.id,
            character_id=character_id,
            opponent_character_id=None,
            check_type="run",
            target=target,
            mode=mode,
            base_value=int(context["base_value"]),
            roll_bonus=int(context["roll_bonus"]),
            die_roll_1=roll_1,
            die_roll_2=roll_2,
            die_roll_final=roll_final,
            total=total,
            dc=dc,
            outcome=outcome,
            winner_character_id=character_id if outcome == "PASS" else None,
            note=note,
        )

        await interaction.response.send_message(embed=embed)

    @check_group.command(name="contest", description="Run a contested check between two characters")
    @app_commands.describe(
        character_a="First character",
        character_b="Second character",
        target="What both sides are contesting",
        mode="Roll mode",
        note="Optional note/context for the contest",
    )
    @app_commands.autocomplete(
        character_a=character_autocomplete,
        character_b=character_autocomplete,
        target=target_autocomplete,
        mode=mode_autocomplete,
    )
    async def contest_check(
        self,
        interaction: discord.Interaction,
        character_a: str,
        character_b: str,
        target: str,
        mode: str = "normal",
        note: str | None = None,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        target = target.strip().lower()
        mode = mode.strip().lower()

        if character_a == character_b:
            await interaction.response.send_message(
                "Choose two different characters.",
                ephemeral=True,
            )
            return

        if mode not in CHECK_MODES:
            await interaction.response.send_message(
                "Mode must be one of: normal, advantage, disadvantage.",
                ephemeral=True,
            )
            return

        context_a = self._load_check_context(
            guild_id=guild_id,
            character_id=character_a,
            target=target,
        )
        context_b = self._load_check_context(
            guild_id=guild_id,
            character_id=character_b,
            target=target,
        )

        if context_a is None or context_b is None:
            await interaction.response.send_message(
                "One or both characters could not be loaded.",
                ephemeral=True,
            )
            return

        a_r1, a_r2, a_rf = self._resolve_rolls(mode)
        b_r1, b_r2, b_rf = self._resolve_rolls(mode)

        a_total = int(context_a["base_value"]) + int(context_a["roll_bonus"]) + int(a_rf)
        b_total = int(context_b["base_value"]) + int(context_b["roll_bonus"]) + int(b_rf)

        a_name = str(context_a["character_row"].get("name") or "Character A")
        b_name = str(context_b["character_row"].get("name") or "Character B")
        target_label = CHECK_TARGETS.get(target, target.replace("_", " ").title())

        if a_total > b_total:
            winner_text = a_name
            winner_character_id = character_a
        elif b_total > a_total:
            winner_text = b_name
            winner_character_id = character_b
        else:
            winner_text = "Tie"
            winner_character_id = None

        embed = self._build_contest_embed(
            target_label=target_label,
            mode=mode,
            a_name=a_name,
            a_base=int(context_a["base_value"]),
            a_bonus=int(context_a["roll_bonus"]),
            a_roll_1=a_r1,
            a_roll_2=a_r2,
            a_roll_final=a_rf,
            a_total=a_total,
            a_sources=context_a["roll_sources"],
            b_name=b_name,
            b_base=int(context_b["base_value"]),
            b_bonus=int(context_b["roll_bonus"]),
            b_roll_1=b_r1,
            b_roll_2=b_r2,
            b_roll_final=b_rf,
            b_total=b_total,
            b_sources=context_b["roll_sources"],
            winner_text=winner_text,
            note=note,
        )

        contest_outcome_a = "WIN" if winner_character_id == character_a else "LOSS" if winner_character_id == character_b else "TIE"
        contest_outcome_b = "WIN" if winner_character_id == character_b else "LOSS" if winner_character_id == character_a else "TIE"

        self._log_check(
            guild_id=guild_id,
            runner_discord_id=interaction.user.id,
            character_id=character_a,
            opponent_character_id=character_b,
            check_type="contest",
            target=target,
            mode=mode,
            base_value=int(context_a["base_value"]),
            roll_bonus=int(context_a["roll_bonus"]),
            die_roll_1=a_r1,
            die_roll_2=a_r2,
            die_roll_final=a_rf,
            total=a_total,
            dc=None,
            outcome=contest_outcome_a,
            winner_character_id=winner_character_id,
            note=note,
        )

        self._log_check(
            guild_id=guild_id,
            runner_discord_id=interaction.user.id,
            character_id=character_b,
            opponent_character_id=character_a,
            check_type="contest",
            target=target,
            mode=mode,
            base_value=int(context_b["base_value"]),
            roll_bonus=int(context_b["roll_bonus"]),
            die_roll_1=b_r1,
            die_roll_2=b_r2,
            die_roll_final=b_rf,
            total=b_total,
            dc=None,
            outcome=contest_outcome_b,
            winner_character_id=winner_character_id,
            note=note,
        )

        await interaction.response.send_message(embed=embed)

    @check_group.command(name="history", description="View recent logged checks for a character")
    @app_commands.describe(
        character="Select a character",
        limit="How many recent checks to show (1-25)",
    )
    @app_commands.autocomplete(character=character_autocomplete)
    async def check_history(
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
        character_row = self._get_character_row(character)
        if not character_row:
            await interaction.response.send_message(
                "That character could not be found.",
                ephemeral=True,
            )
            return

        history_rows = self._get_check_history(
            guild_id=guild_id,
            character_id=character,
            limit=limit,
        )

        character_name = str(character_row.get("name") or "Unnamed Character")
        embed = self._build_history_embed(
            character_name=character_name,
            history_rows=history_rows,
            requested_limit=limit,
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(ChecksCog(bot))