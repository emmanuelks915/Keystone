from __future__ import annotations

import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.traits_service import TraitsService


TIER_ORDER = {
    "origin": 0,
    "minor": 1,
    "reliable": 2,
    "keystone": 3,
    "negative": 4,
}

ALLOWED_TIERS = {"origin", "minor", "reliable", "keystone", "negative"}

CLASS_PATH_KEYS = {
    "mana_circuits",
    "mana_circuits_mage",
    "magic_circuits",
    "forgeborn",
    "gunslinger",
    "gunslinger_training",
    "loyal_companion",
}

REWARD_SCALING_KEYS = {
    "quiet_benefactor",
    "selective_fortune",
    "self_made_survivor",
}

FALLBACK_REQUIRES_ANY: dict[str, set[str]] = {
    "greater_knowledge": {"source_sensitivity", "source_sensitive"},
    "natural_leader": {"charming", "threatening"},
    "crowd_sense": {"perceptive"},
    "hardy_constitution": {"bears_fortitude", "bear_s_fortitude", "bear_fortitude"},
    "inner_light": {"lucky", "lucky_spark"},
    "silver_ear": {"perceptive"},
    "enhanced_physique": {"gorilla_strength", "cat_s_grace", "cats_grace"},
    "merlins_skill": {
        "dragon_s_insight",
        "dragons_insight",
        "leviathan_depth",
        "leviathans_depth",
    },
}

FALLBACK_INCOMPATIBLE_KEYS: dict[str, set[str]] = {
    "charming": {"threatening"},
    "threatening": {"charming"},
    "logistics_mind": {"big_spender"},
    "big_spender": {"logistics_mind"},
    "weak_body": {
        "gorilla_strength",
        "cat_s_grace",
        "cats_grace",
        "bears_fortitude",
        "bear_s_fortitude",
        "bear_fortitude",
    },
    "inflamed_mana_circuits": {
        "dragon_s_insight",
        "dragons_insight",
        "leviathan_depth",
        "leviathans_depth",
    },
}


class TraitsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sb = bot.supabase
        self.traits = TraitsService(self.sb)

    traits_group = app_commands.Group(name="traits", description="Character trait tools")

    # -------------------------------------------------------------------------
    # Basic helpers
    # -------------------------------------------------------------------------
    def _normalize_key(self, value: Any) -> str:
        text = str(value or "").casefold().strip()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        return text.strip("_")

    def _trait_tier(self, trait: dict[str, Any]) -> str:
        return str(trait.get("tier") or "").strip().lower()

    def _trait_name(self, trait: dict[str, Any]) -> str:
        return str(trait.get("name") or "Trait")

    def _trait_keys(self, trait: dict[str, Any]) -> set[str]:
        keys: set[str] = set()

        slug_key = self._normalize_key(trait.get("slug"))
        name_key = self._normalize_key(trait.get("name"))

        if slug_key:
            keys.add(slug_key)
        if name_key:
            keys.add(name_key)

        # Helps names like "Mana Circuits (Mage)" match "mana_circuits".
        for key in list(keys):
            if key.endswith("_mage"):
                keys.add(key.removesuffix("_mage").rstrip("_"))

        return {key for key in keys if key}

    def _all_trait_keys(self, traits: list[dict[str, Any]]) -> set[str]:
        keys: set[str] = set()
        for trait in traits:
            keys |= self._trait_keys(trait)
        return keys

    def _format_key_list(self, keys: set[str]) -> str:
        if not keys:
            return "None"
        return ", ".join(f"`{key.replace('_', ' ')}`" for key in sorted(keys))

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False

        staff_role_ids = set(getattr(self.bot, "staff_role_ids", set()) or set())

        if any(role.id in staff_role_ids for role in member.roles):
            return True

        perms = member.guild_permissions
        return bool(perms.manage_guild or perms.administrator)

    async def _private_err(self, interaction: discord.Interaction, content: str):
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True)
        return await interaction.response.send_message(content, ephemeral=True)

    def _can_view_character(self, interaction: discord.Interaction, character_row: dict[str, Any]) -> bool:
        if self._is_staff(interaction):
            return True

        owner_id = str(character_row.get("user_id") or "")
        return owner_id == str(interaction.user.id)

    def _format_sheet_url(self, sheet_url: str | None) -> str:
        if not sheet_url:
            return "Not linked."
        return f"<{sheet_url}>"

    # -------------------------------------------------------------------------
    # Autocomplete
    # -------------------------------------------------------------------------
    async def character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            user_id = int(interaction.user.id)
            is_staff = self._is_staff(interaction)

            query = (
                self.sb.table("characters")
                .select("character_id,name,user_id,is_active")
                .order("name")
                .limit(100)
            )

            if not is_staff:
                query = query.eq("user_id", user_id)

            res = query.execute()
            rows = res.data or []

            q = (current or "").strip().lower()
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

    async def trait_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        try:
            guild_id = int(interaction.guild_id)
            q = (current or "").strip().lower()

            res = (
                self.sb.table("traits")
                .select("trait_id,name,slug,tier,cost,is_active")
                .eq("guild_id", guild_id)
                .eq("is_active", True)
                .order("name")
                .limit(100)
                .execute()
            )
            rows = res.data or []

            if q:
                rows = [
                    row for row in rows
                    if q in str(row.get("name", "")).lower()
                    or q in str(row.get("slug", "")).lower()
                ]

            out: list[app_commands.Choice[str]] = []
            for row in rows[:25]:
                name = str(row.get("name") or "Trait")
                tier = str(row.get("tier") or "?")
                cost = int(row.get("cost") or 0)
                label = f"{name} • {tier} • {cost:+d}"
                out.append(app_commands.Choice(name=label[:100], value=str(row["trait_id"])))

            return out
        except Exception:
            return []

    async def owned_trait_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        try:
            namespace = interaction.namespace
            character_id = getattr(namespace, "character", None)
            if not character_id:
                return []

            guild_id = int(interaction.guild_id)
            q = (current or "").strip().lower()

            trait_bundles = self.traits.get_character_traits(
                guild_id=guild_id,
                character_id=str(character_id),
            )

            out: list[app_commands.Choice[str]] = []
            for bundle in trait_bundles:
                trait = bundle.get("trait") or {}
                name = str(trait.get("name") or "Trait")
                slug = str(trait.get("slug") or "")
                trait_id = str(trait.get("trait_id") or "")
                tier = str(trait.get("tier") or "?")
                cost = int(trait.get("cost") or 0)

                searchable = f"{name} {slug}".lower()
                if q and q not in searchable:
                    continue

                label = f"{name} • {tier} • {cost:+d}"
                out.append(app_commands.Choice(name=label[:100], value=trait_id))

            return out[:25]
        except Exception:
            return []

    # -------------------------------------------------------------------------
    # DB helpers
    # -------------------------------------------------------------------------
    def _get_character_row(self, character_id: str) -> dict[str, Any] | None:
        try:
            res = (
                self.sb.table("characters")
                .select("character_id,name,user_id,is_active,sheet_url")
                .eq("character_id", character_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _get_trait_row(self, guild_id: int, trait_id: str) -> dict[str, Any] | None:
        try:
            res = (
                self.sb.table("traits")
                .select("*")
                .eq("guild_id", guild_id)
                .eq("trait_id", trait_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _get_all_traits(self, guild_id: int) -> list[dict[str, Any]]:
        try:
            res = (
                self.sb.table("traits")
                .select("*")
                .eq("guild_id", guild_id)
                .eq("is_active", True)
                .execute()
            )
            rows = res.data or []
            rows.sort(
                key=lambda r: (
                    TIER_ORDER.get(str(r.get("tier") or "").strip().lower(), 999),
                    str(r.get("name") or "").lower(),
                )
            )
            return rows
        except Exception:
            return []

    # -------------------------------------------------------------------------
    # Rule helpers
    # -------------------------------------------------------------------------
    def _get_incompatible_slugs(self, trait: dict[str, Any]) -> set[str]:
        req = trait.get("requirements_json") or {}
        if not isinstance(req, dict):
            return set()

        raw = req.get("incompatible_slugs") or []
        if not isinstance(raw, list):
            return set()

        return {
            self._normalize_key(slug)
            for slug in raw
            if self._normalize_key(slug)
        }

    def _get_requires_any(self, trait: dict[str, Any]) -> set[str]:
        req = trait.get("requirements_json") or {}
        if not isinstance(req, dict):
            return set()

        raw = req.get("requires_any") or []
        if not isinstance(raw, list):
            return set()

        return {
            self._normalize_key(slug)
            for slug in raw
            if self._normalize_key(slug)
        }

    def _get_effective_requires_any(self, trait: dict[str, Any]) -> set[str]:
        required = set(self._get_requires_any(trait))

        for key in self._trait_keys(trait):
            required |= FALLBACK_REQUIRES_ANY.get(key, set())

        return {self._normalize_key(key) for key in required if self._normalize_key(key)}

    def _get_effective_incompatible_keys(self, trait: dict[str, Any]) -> set[str]:
        incompatible = set(self._get_incompatible_slugs(trait))

        for key in self._trait_keys(trait):
            incompatible |= FALLBACK_INCOMPATIBLE_KEYS.get(key, set())

        return {self._normalize_key(key) for key in incompatible if self._normalize_key(key)}

    def _try_int(self, value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def _get_trait_point_limits(self, trait_rows: list[dict[str, Any]]) -> dict[str, Any]:
        free_limit = 5
        hard_cap = 8
        reasons: list[str] = []

        for trait in trait_rows:
            name = self._trait_name(trait)
            keys = self._trait_keys(trait)

            req = trait.get("requirements_json") or {}
            if not isinstance(req, dict):
                req = {}

            for key in ("free_positive_limit", "positive_free_limit", "free_trait_point_limit"):
                value = self._try_int(req.get(key))
                if value is not None:
                    free_limit = max(free_limit, value)

            for key in ("max_positive_points", "positive_point_cap", "max_trait_points"):
                value = self._try_int(req.get(key))
                if value is not None:
                    hard_cap = min(hard_cap, value)
                    reasons.append(f"{name}: max {value}")

            if "quiet_benefactor" in keys:
                free_limit = max(free_limit, 7)
                hard_cap = min(hard_cap, 7)
                reasons.append("Quiet Benefactor: max 7")

            if "selective_fortune" in keys:
                free_limit = max(free_limit, 6)
                hard_cap = min(hard_cap, 6)
                reasons.append("Selective Fortune: max 6")

            if "self_made_survivor" in keys:
                free_limit = min(free_limit, 5)
                hard_cap = min(hard_cap, 5)
                reasons.append("Self-Made Survivor: max 5")

        free_limit = min(free_limit, hard_cap)

        return {
            "free_limit": free_limit,
            "hard_cap": hard_cap,
            "reasons": reasons,
        }

    def _calculate_trait_points(self, trait_bundles: list[dict[str, Any]]) -> dict[str, int]:
        positive = 0
        negative = 0
        origin = 0

        trait_rows: list[dict[str, Any]] = []

        for bundle in trait_bundles:
            trait = bundle.get("trait") or {}
            if trait:
                trait_rows.append(trait)

            tier = self._trait_tier(trait)
            cost = int(trait.get("cost") or 0)

            if tier == "origin":
                origin += 1
            elif cost >= 0:
                positive += cost
            else:
                negative += abs(cost)

        limits = self._get_trait_point_limits(trait_rows)
        free_limit = int(limits["free_limit"])
        hard_cap = int(limits["hard_cap"])
        overdraft_limit = min(hard_cap, free_limit + negative)

        return {
            "positive": positive,
            "negative": negative,
            "origin": origin,
            "free_limit": free_limit,
            "hard_cap": hard_cap,
            "net_overdraft_room": overdraft_limit,
        }

    def _validate_full_trait_build(self, trait_rows: list[dict[str, Any]]) -> str | None:
        if not trait_rows:
            return None

        trait_ids = [
            str(t.get("trait_id") or "")
            for t in trait_rows
            if str(t.get("trait_id") or "")
        ]

        if len(trait_ids) != len(set(trait_ids)):
            return "That character already has this trait."

        origin_traits = [
            trait for trait in trait_rows
            if self._trait_tier(trait) == "origin"
        ]
        if len(origin_traits) > 1:
            return "A character may only have one Origin trait."

        seen_groups: dict[str, str] = {}
        for trait in trait_rows:
            group = str(trait.get("exclusive_group") or "").strip().lower()
            if not group:
                continue

            trait_name = self._trait_name(trait)
            if group in seen_groups:
                return f"Trait conflict: `{trait_name}` conflicts with `{seen_groups[group]}`."

            seen_groups[group] = trait_name

        class_path_traits = [
            trait for trait in trait_rows
            if self._trait_keys(trait) & CLASS_PATH_KEYS
        ]
        if len(class_path_traits) > 1:
            first = self._trait_name(class_path_traits[0])
            second = self._trait_name(class_path_traits[1])
            return f"Trait conflict: `{first}` conflicts with `{second}`. A character may only have one class path trait."

        reward_traits = [
            trait for trait in trait_rows
            if self._trait_keys(trait) & REWARD_SCALING_KEYS
        ]
        if len(reward_traits) > 1:
            first = self._trait_name(reward_traits[0])
            second = self._trait_name(reward_traits[1])
            return f"Trait conflict: `{first}` conflicts with `{second}`. Reward-scaling Keystone traits cannot be combined."

        if class_path_traits and reward_traits:
            class_trait = self._trait_name(class_path_traits[0])
            reward_trait = self._trait_name(reward_traits[0])
            return (
                f"Trait conflict: `{reward_trait}` cannot be combined with `{class_trait}`. "
                "Choose either a reward-focused progression identity or a class-defining specialization path."
            )

        all_keys = self._all_trait_keys(trait_rows)

        for trait in trait_rows:
            trait_name = self._trait_name(trait)
            trait_keys = self._trait_keys(trait)
            available_keys = all_keys - trait_keys

            incompatible = self._get_effective_incompatible_keys(trait)
            if incompatible & available_keys:
                conflict_keys = incompatible & available_keys
                return (
                    f"Trait conflict: `{trait_name}` is incompatible with "
                    f"{self._format_key_list(conflict_keys)}."
                )

            requires_any = self._get_effective_requires_any(trait)
            if requires_any and not (requires_any & available_keys):
                return (
                    f"`{trait_name}` requires at least one of: "
                    f"{self._format_key_list(requires_any)}"
                )

        positive = 0
        negative = 0

        for trait in trait_rows:
            tier = self._trait_tier(trait)
            cost = int(trait.get("cost") or 0)

            if tier == "origin":
                continue
            if cost >= 0:
                positive += cost
            else:
                negative += abs(cost)

        limits = self._get_trait_point_limits(trait_rows)
        free_limit = int(limits["free_limit"])
        hard_cap = int(limits["hard_cap"])
        reasons = limits.get("reasons") or []

        if positive > hard_cap:
            reason_text = f" because of {', '.join(reasons)}" if reasons else ""
            return f"A character may not exceed {hard_cap} positive trait points{reason_text}."

        if positive > free_limit and positive > free_limit + negative:
            needed = positive - free_limit
            return (
                f"This build overdrafts trait points. It needs at least "
                f"{needed} point(s) of Negative Traits."
            )

        return None

    def _validate_trait_rules(
        self,
        *,
        existing_trait_bundles: list[dict[str, Any]],
        new_trait: dict[str, Any],
        origin_override: bool = False,
    ) -> str | None:
        existing_traits = [bundle.get("trait") or {} for bundle in existing_trait_bundles]
        existing_traits = [trait for trait in existing_traits if trait]

        if self._trait_tier(new_trait) == "origin" and not origin_override:
            return (
                "Origin traits are creation-only. If this is a staff correction, "
                "rerun the command with `origin_override: True`."
            )

        preview_traits = existing_traits + [new_trait]
        return self._validate_full_trait_build(preview_traits)

    def _validate_trait_removal(
        self,
        *,
        existing_trait_bundles: list[dict[str, Any]],
        removed_trait: dict[str, Any],
    ) -> str | None:
        existing_traits = [bundle.get("trait") or {} for bundle in existing_trait_bundles]
        existing_traits = [trait for trait in existing_traits if trait]

        removed_id = str(removed_trait.get("trait_id") or "")
        removed_once = False
        remaining_traits: list[dict[str, Any]] = []

        for trait in existing_traits:
            trait_id = str(trait.get("trait_id") or "")
            if trait_id == removed_id and not removed_once:
                removed_once = True
                continue
            remaining_traits.append(trait)

        validation_error = self._validate_full_trait_build(remaining_traits)
        if validation_error:
            return (
                f"Removing **{self._trait_name(removed_trait)}** would make this build illegal: "
                f"{validation_error}"
            )

        return None

    # -------------------------------------------------------------------------
    # Embed builders
    # -------------------------------------------------------------------------
    def _build_view_embed(
        self,
        *,
        character_row: dict[str, Any],
        trait_bundles: list[dict[str, Any]],
    ) -> discord.Embed:
        character_name = str(character_row.get("name") or "Unnamed Character")
        status = "Active" if character_row.get("is_active") else "Inactive"
        owner_id = character_row.get("user_id")
        sheet_url = character_row.get("sheet_url")

        description_lines = [
            f"**Status:** {status}",
        ]

        if owner_id:
            description_lines.append(f"**Player:** <@{owner_id}>")

        description_lines.append(f"**Sheet:** {self._format_sheet_url(sheet_url)}")

        embed = discord.Embed(
            title=f"{character_name} — Traits",
            description="\n".join(description_lines),
            color=discord.Color.orange(),
        )

        points = self._calculate_trait_points(trait_bundles)
        summary = (
            f"**Positive Points:** {points['positive']}\n"
            f"**Negative Points:** {points['negative']}\n"
            f"**Origin Traits:** {points['origin']}\n"
            f"**Free Positive Limit:** {points['free_limit']}\n"
            f"**Overdraft Limit:** {points['free_limit']} + {points['negative']} = {points['net_overdraft_room']}\n"
            f"**Hard Positive Cap:** {points['hard_cap']}"
        )
        embed.add_field(name="Trait Summary", value=summary, inline=False)

        groups: dict[str, list[str]] = {
            "origin": [],
            "minor": [],
            "reliable": [],
            "keystone": [],
            "negative": [],
        }

        for bundle in trait_bundles:
            trait = bundle.get("trait") or {}
            name = str(trait.get("name") or "Trait")
            cost = int(trait.get("cost") or 0)
            tier = self._trait_tier(trait) or "minor"
            desc = str(trait.get("description") or "").strip()

            line = f"• **{name}** ({cost:+d})"
            if desc:
                line += f"\n  {desc[:140]}"

            groups.setdefault(tier, []).append(line)

        for tier in ["origin", "keystone", "reliable", "minor", "negative"]:
            lines = groups.get(tier) or []
            if not lines:
                continue

            embed.add_field(
                name=tier.title(),
                value="\n".join(lines)[:1024],
                inline=False,
            )

        if not trait_bundles:
            embed.add_field(name="Traits", value="No traits assigned.", inline=False)

        return embed

    def _build_list_embed(
        self,
        *,
        guild_id: int,
        tier: str | None,
    ) -> discord.Embed:
        rows = self._get_all_traits(guild_id)
        if tier:
            rows = [row for row in rows if self._trait_tier(row) == tier]

        embed = discord.Embed(
            title="Available Traits",
            description=f"Showing {'all active traits' if not tier else f'active `{tier}` traits'}",
            color=discord.Color.blurple(),
        )

        if not rows:
            embed.add_field(name="Traits", value="No traits found.", inline=False)
            return embed

        grouped: dict[str, list[str]] = {}
        for row in rows:
            t = self._trait_tier(row) or "unknown"
            grouped.setdefault(t, [])
            grouped[t].append(
                f"• **{row.get('name', 'Trait')}** ({int(row.get('cost') or 0):+d})"
            )

        for group_name in ["origin", "keystone", "reliable", "minor", "negative"]:
            lines = grouped.get(group_name)
            if not lines:
                continue

            embed.add_field(
                name=group_name.title(),
                value="\n".join(lines)[:1024],
                inline=False,
            )

        return embed

    def _build_info_embed(self, trait_row: dict[str, Any]) -> discord.Embed:
        name = str(trait_row.get("name") or "Trait")
        tier = self._trait_tier(trait_row) or "unknown"
        cost = int(trait_row.get("cost") or 0)
        desc = str(trait_row.get("description") or "No description.")
        slug = str(trait_row.get("slug") or "unknown")

        req = trait_row.get("requirements_json") or {}
        if not isinstance(req, dict):
            req = {}

        effects = trait_row.get("effects_json") or {}
        if not isinstance(effects, dict):
            effects = {}

        incompatible = self._get_effective_incompatible_keys(trait_row)
        requires_any = self._get_effective_requires_any(trait_row)
        exclusive_group = str(trait_row.get("exclusive_group") or "").strip() or "None"

        passives = effects.get("passives") or {}
        roll_modifiers = effects.get("roll_modifiers") or []

        embed = discord.Embed(
            title=name,
            description=desc,
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Basics",
            value=(
                f"**Tier:** {tier.title()}\n"
                f"**Cost:** {cost:+d}\n"
                f"**Slug:** `{slug}`\n"
                f"**Exclusive Group:** `{exclusive_group}`"
            ),
            inline=False,
        )

        req_lines: list[str] = []
        if requires_any:
            req_lines.append("**Requires Any:** " + self._format_key_list(requires_any))
        if incompatible:
            req_lines.append("**Incompatible:** " + self._format_key_list(incompatible))

        other_req_keys = []
        for key in sorted(req.keys()):
            if key in {
                "requires_any",
                "incompatible_slugs",
                "free_positive_limit",
                "positive_free_limit",
                "free_trait_point_limit",
                "max_positive_points",
                "positive_point_cap",
                "max_trait_points",
            }:
                continue
            other_req_keys.append(f"`{key}`")

        if other_req_keys:
            req_lines.append("**Other Metadata:** " + ", ".join(other_req_keys))

        embed.add_field(
            name="Requirements / Rules",
            value="\n".join(req_lines) if req_lines else "None.",
            inline=False,
        )

        limits = self._get_trait_point_limits([trait_row])
        if limits["free_limit"] != 5 or limits["hard_cap"] != 8:
            embed.add_field(
                name="Point Limit Impact",
                value=(
                    f"**Free Positive Limit:** {limits['free_limit']}\n"
                    f"**Hard Positive Cap:** {limits['hard_cap']}"
                ),
                inline=False,
            )

        passive_lines: list[str] = []
        if isinstance(passives, dict):
            for key, value in passives.items():
                passive_lines.append(f"**{key}:** `{value}`")

        roll_lines: list[str] = []
        if isinstance(roll_modifiers, list) and roll_modifiers:
            for mod in roll_modifiers[:10]:
                if not isinstance(mod, dict):
                    continue
                target = str(mod.get("target") or "unknown")
                value = mod.get("value")
                roll_lines.append(f"• `{target}`: `{value}`")

        embed.add_field(
            name="Passives",
            value="\n".join(passive_lines)[:1024] if passive_lines else "None.",
            inline=False,
        )
        embed.add_field(
            name="Roll Modifiers",
            value="\n".join(roll_lines)[:1024] if roll_lines else "None.",
            inline=False,
        )

        return embed

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------
    @traits_group.command(name="view", description="View a character's traits")
    @app_commands.describe(character="Select a character")
    @app_commands.autocomplete(character=character_autocomplete)
    async def view_traits(self, interaction: discord.Interaction, character: str):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = int(interaction.guild_id)
        character_row = self._get_character_row(character)

        if not character_row:
            await interaction.response.send_message("Character not found.", ephemeral=True)
            return

        if not self._can_view_character(interaction, character_row):
            await interaction.response.send_message(
                "You can only view traits for your own OCs.",
                ephemeral=True,
            )
            return

        trait_bundles = self.traits.get_character_traits(
            guild_id=guild_id,
            character_id=character,
        )

        embed = self._build_view_embed(
            character_row=character_row,
            trait_bundles=trait_bundles,
        )
        await interaction.response.send_message(embed=embed)

    @traits_group.command(name="list", description="List available traits")
    @app_commands.describe(tier="Optional trait tier filter")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="Origin", value="origin"),
            app_commands.Choice(name="Minor", value="minor"),
            app_commands.Choice(name="Reliable", value="reliable"),
            app_commands.Choice(name="Keystone", value="keystone"),
            app_commands.Choice(name="Negative", value="negative"),
        ]
    )
    async def list_traits(
        self,
        interaction: discord.Interaction,
        tier: str | None = None,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        tier = tier.strip().lower() if tier else None

        if tier not in ({None} | ALLOWED_TIERS):
            await interaction.response.send_message(
                "Tier must be one of: origin, minor, reliable, keystone, negative.",
                ephemeral=True,
            )
            return

        embed = self._build_list_embed(
            guild_id=int(interaction.guild_id),
            tier=tier,
        )
        await interaction.response.send_message(embed=embed)

    @traits_group.command(name="info", description="View full info for a trait")
    @app_commands.describe(trait="Select a trait")
    @app_commands.autocomplete(trait=trait_autocomplete)
    async def info_trait(self, interaction: discord.Interaction, trait: str):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = int(interaction.guild_id)
        trait_row = self._get_trait_row(guild_id, trait)

        if not trait_row:
            await interaction.response.send_message("Trait not found.", ephemeral=True)
            return

        embed = self._build_info_embed(trait_row)
        await interaction.response.send_message(embed=embed)

    @traits_group.command(name="add", description="Staff: add a trait to a character")
    @app_commands.describe(
        character="Select a character",
        trait="Select a trait",
        notes="Optional approval note",
        origin_override="Staff correction only: allow a creation-only Origin trait.",
    )
    @app_commands.autocomplete(character=character_autocomplete, trait=trait_autocomplete)
    async def add_trait(
        self,
        interaction: discord.Interaction,
        character: str,
        trait: str,
        notes: str | None = None,
        origin_override: bool = False,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not self._is_staff(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return

        guild_id = int(interaction.guild_id)
        character_row = self._get_character_row(character)

        if not character_row:
            await interaction.response.send_message("Character not found.", ephemeral=True)
            return

        trait_row = self._get_trait_row(guild_id, trait)

        if not trait_row:
            await interaction.response.send_message("Trait not found.", ephemeral=True)
            return

        existing_trait_bundles = self.traits.get_character_traits(
            guild_id=guild_id,
            character_id=character,
        )

        validation_error = self._validate_trait_rules(
            existing_trait_bundles=existing_trait_bundles,
            new_trait=trait_row,
            origin_override=origin_override,
        )
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return

        try:
            self.traits.add_trait_to_character(
                guild_id=guild_id,
                character_id=character,
                trait_id=str(trait_row["trait_id"]),
                approved_by=interaction.user.id,
                notes=notes,
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to add trait: {e}",
                ephemeral=True,
            )
            return

        override_note = " Origin override used." if origin_override else ""
        await interaction.response.send_message(
            f"Added **{trait_row['name']}** to **{character_row.get('name', 'Character')}**.{override_note}"
        )

    @traits_group.command(name="remove", description="Staff: remove a trait from a character")
    @app_commands.describe(
        character="Select a character",
        trait="Select one of that character's current traits",
        origin_override="Staff correction only: allow removing a creation-only Origin trait.",
    )
    @app_commands.autocomplete(character=character_autocomplete, trait=owned_trait_autocomplete)
    async def remove_trait(
        self,
        interaction: discord.Interaction,
        character: str,
        trait: str,
        origin_override: bool = False,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not self._is_staff(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return

        guild_id = int(interaction.guild_id)
        character_row = self._get_character_row(character)

        if not character_row:
            await interaction.response.send_message("Character not found.", ephemeral=True)
            return

        trait_row = self._get_trait_row(guild_id, trait)

        if not trait_row:
            await interaction.response.send_message("Trait not found.", ephemeral=True)
            return

        if self._trait_tier(trait_row) == "origin" and not origin_override:
            await interaction.response.send_message(
                "Origin traits are creation-only. If this is a staff correction, "
                "rerun the command with `origin_override: True`.",
                ephemeral=True,
            )
            return

        existing_trait_bundles = self.traits.get_character_traits(
            guild_id=guild_id,
            character_id=character,
        )

        existing_ids = {
            str((bundle.get("trait") or {}).get("trait_id"))
            for bundle in existing_trait_bundles
        }

        if str(trait_row["trait_id"]) not in existing_ids:
            await interaction.response.send_message(
                "That character does not currently have this trait.",
                ephemeral=True,
            )
            return

        validation_error = self._validate_trait_removal(
            existing_trait_bundles=existing_trait_bundles,
            removed_trait=trait_row,
        )
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return

        try:
            self.traits.remove_trait_from_character(
                guild_id=guild_id,
                character_id=character,
                trait_id=str(trait_row["trait_id"]),
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to remove trait: {e}",
                ephemeral=True,
            )
            return

        override_note = " Origin override used." if origin_override else ""
        await interaction.response.send_message(
            f"Removed **{trait_row['name']}** from **{character_row.get('name', 'Character')}**.{override_note}"
        )


async def setup(bot):
    await bot.add_cog(TraitsCog(bot))