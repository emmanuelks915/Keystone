from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.autocomplete_service import oc_name_autocomplete
from services.traits_service import TraitsService


NAME_RE = re.compile(r"^[A-Za-z0-9 _'\-]{1,64}$")

TIER_ORDER = {
    "origin": 0,
    "keystone": 1,
    "reliable": 2,
    "minor": 3,
    "negative": 4,
}

ADDABLE_TRAIT_TIERS = ["minor", "reliable", "keystone", "negative"]

MAX_STARTING_TRAITS = 8
DRAFT_TIMEOUT_SECONDS = 30 * 60

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


@dataclass
class OCRegistrationDraft:
    guild_id: int
    owner_user_id: int
    owner_display_name: str
    controller_user_id: int
    controller_display_name: str
    name: str
    sheet_url: str | None = None
    origin_trait_id: str | None = None
    trait_ids: list[str] = field(default_factory=list)
    add_trait_tier: str = "minor"


class OCRegistrationModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "OCCog",
        *,
        target_member: discord.Member | None = None,
    ):
        title = "Register OC"
        if target_member is not None:
            title = f"Register OC for {target_member.display_name}"[:45]

        super().__init__(title=title)
        self.cog = cog
        self.target_member = target_member

        self.oc_name = discord.ui.TextInput(
            label="OC Name",
            placeholder="Example: Meris Philon",
            required=True,
            max_length=64,
        )

        self.sheet_url = discord.ui.TextInput(
            label="Character Sheet Link",
            placeholder="Paste the Google Docs/Sheets/Notion link here",
            required=False,
            max_length=300,
        )

        self.add_item(self.oc_name)
        self.add_item(self.sheet_url)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._start_registration_from_modal(
            interaction,
            name=str(self.oc_name.value or ""),
            sheet_url=str(self.sheet_url.value or ""),
            target_member=self.target_member,
        )


class OriginTraitSelect(discord.ui.Select):
    def __init__(self, cog: "OCCog", key: tuple[int, int, int], draft: OCRegistrationDraft):
        self.cog = cog
        self.key = key

        options: list[discord.SelectOption] = []

        if draft.origin_trait_id:
            options.append(
                discord.SelectOption(
                    label="Clear Origin",
                    value="__clear_origin__",
                    description="Remove the currently selected Origin trait.",
                    emoji="🧹",
                )
            )

        rows = cog._get_active_trait_rows(draft.guild_id, tier_filter="origin")

        remaining_slots = 25 - len(options)
        for row in rows[:remaining_slots]:
            trait_id = str(row.get("trait_id") or "")
            name = str(row.get("name") or "Trait")
            tier = str(row.get("tier") or "?")
            cost = int(row.get("cost") or 0)

            options.append(
                discord.SelectOption(
                    label=f"{name} • {tier} • {cost:+d}"[:100],
                    value=trait_id,
                    default=(trait_id == draft.origin_trait_id),
                )
            )

        disabled = False
        if not options:
            disabled = True
            options = [
                discord.SelectOption(
                    label="No Origin traits available",
                    value="__none__",
                )
            ]

        super().__init__(
            placeholder="Choose or change Origin trait",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        draft = self.cog._get_registration_draft(self.key)
        if not draft:
            return await self.cog._private_err(interaction, "This OC registration draft expired. Please start again.")

        value = self.values[0]
        if value == "__none__":
            return await self.cog._private_err(interaction, "There is no Origin trait to select.")

        old_origin = draft.origin_trait_id

        if value == "__clear_origin__":
            draft.origin_trait_id = None
        else:
            row = self.cog._get_trait_row(draft.guild_id, value)
            if not row:
                return await self.cog._private_err(interaction, "That Origin trait could not be found.")
            if self.cog._trait_tier(row) != "origin":
                return await self.cog._private_err(interaction, "That trait is not an Origin trait.")

            draft.origin_trait_id = value

        selected_traits = self.cog._get_draft_selected_traits(draft)
        validation_error = self.cog._validate_selected_traits(selected_traits=selected_traits)
        if validation_error:
            draft.origin_trait_id = old_origin
            return await self.cog._private_err(interaction, validation_error)

        await interaction.response.edit_message(
            embed=self.cog._build_registration_embed(draft),
            view=OCRegistrationView(self.cog, self.key),
        )


class TraitTierSelect(discord.ui.Select):
    def __init__(self, cog: "OCCog", key: tuple[int, int, int], draft: OCRegistrationDraft):
        self.cog = cog
        self.key = key

        labels = {
            "minor": "Minor Traits",
            "reliable": "Reliable Traits",
            "keystone": "Keystone / Class Traits",
            "negative": "Negative Traits",
        }

        options = [
            discord.SelectOption(
                label=labels[tier],
                value=tier,
                default=(draft.add_trait_tier == tier),
            )
            for tier in ADDABLE_TRAIT_TIERS
        ]

        super().__init__(
            placeholder="Choose trait category to add from",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        draft = self.cog._get_registration_draft(self.key)
        if not draft:
            return await self.cog._private_err(interaction, "This OC registration draft expired. Please start again.")

        value = self.values[0]
        if value not in ADDABLE_TRAIT_TIERS:
            return await self.cog._private_err(interaction, "That trait category is not valid.")

        draft.add_trait_tier = value

        await interaction.response.edit_message(
            embed=self.cog._build_registration_embed(draft),
            view=OCRegistrationView(self.cog, self.key),
        )


class AddTraitSelect(discord.ui.Select):
    def __init__(self, cog: "OCCog", key: tuple[int, int, int], draft: OCRegistrationDraft):
        self.cog = cog
        self.key = key

        tier_label = draft.add_trait_tier.title()
        options: list[discord.SelectOption] = []

        if len(draft.trait_ids) < MAX_STARTING_TRAITS:
            rows = cog._get_candidate_addable_trait_rows(draft, tier_filter=draft.add_trait_tier)

            for row in rows[:25]:
                trait_id = str(row.get("trait_id") or "")
                name = str(row.get("name") or "Trait")
                tier = str(row.get("tier") or "?")
                cost = int(row.get("cost") or 0)

                options.append(
                    discord.SelectOption(
                        label=f"{name} • {tier} • {cost:+d}"[:100],
                        value=trait_id,
                    )
                )

        disabled = False
        placeholder = f"Add a {tier_label} trait"

        if len(draft.trait_ids) >= MAX_STARTING_TRAITS:
            disabled = True
            placeholder = f"Max {MAX_STARTING_TRAITS} starting traits selected"
            options = [
                discord.SelectOption(
                    label="Trait limit reached",
                    value="__none__",
                )
            ]
        elif not options:
            disabled = True
            placeholder = f"No legal {tier_label} traits available"
            options = [
                discord.SelectOption(
                    label=f"No legal {tier_label} traits available",
                    value="__none__",
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        draft = self.cog._get_registration_draft(self.key)
        if not draft:
            return await self.cog._private_err(interaction, "This OC registration draft expired. Please start again.")

        value = self.values[0]
        if value == "__none__":
            return await self.cog._private_err(interaction, "There is no trait to add from this category.")

        if len(draft.trait_ids) >= MAX_STARTING_TRAITS:
            return await self.cog._private_err(
                interaction,
                f"You can only select up to **{MAX_STARTING_TRAITS}** starting traits.",
            )

        if value in draft.trait_ids or value == draft.origin_trait_id:
            return await self.cog._private_err(interaction, "That trait is already selected.")

        row = self.cog._get_trait_row(draft.guild_id, value)
        if not row:
            return await self.cog._private_err(interaction, "That trait could not be found.")

        if self.cog._trait_tier(row) == "origin":
            return await self.cog._private_err(interaction, "Origin traits must be selected in the Origin dropdown.")

        old_trait_ids = list(draft.trait_ids)
        draft.trait_ids.append(value)

        selected_traits = self.cog._get_draft_selected_traits(draft)
        validation_error = self.cog._validate_selected_traits(selected_traits=selected_traits)
        if validation_error:
            draft.trait_ids = old_trait_ids
            return await self.cog._private_err(interaction, validation_error)

        await interaction.response.edit_message(
            embed=self.cog._build_registration_embed(draft),
            view=OCRegistrationView(self.cog, self.key),
        )


class RemoveTraitSelect(discord.ui.Select):
    def __init__(self, cog: "OCCog", key: tuple[int, int, int], draft: OCRegistrationDraft):
        self.cog = cog
        self.key = key

        options: list[discord.SelectOption] = []

        for trait_id in draft.trait_ids[:25]:
            row = cog._get_trait_row(draft.guild_id, trait_id)
            if not row:
                continue

            name = str(row.get("name") or "Trait")
            tier = str(row.get("tier") or "?")
            cost = int(row.get("cost") or 0)

            options.append(
                discord.SelectOption(
                    label=f"{name} • {tier} • {cost:+d}"[:100],
                    value=str(trait_id),
                )
            )

        disabled = False
        if not options:
            disabled = True
            options = [
                discord.SelectOption(
                    label="No traits selected yet",
                    value="__none__",
                )
            ]

        super().__init__(
            placeholder="Remove a selected starting trait",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        draft = self.cog._get_registration_draft(self.key)
        if not draft:
            return await self.cog._private_err(interaction, "This OC registration draft expired. Please start again.")

        value = self.values[0]
        if value == "__none__":
            return await self.cog._private_err(interaction, "There is no trait to remove.")

        if value not in draft.trait_ids:
            return await self.cog._private_err(interaction, "That trait is not currently selected.")

        old_trait_ids = list(draft.trait_ids)
        draft.trait_ids.remove(value)

        selected_traits = self.cog._get_draft_selected_traits(draft)
        validation_error = self.cog._validate_selected_traits(selected_traits=selected_traits)
        if validation_error:
            draft.trait_ids = old_trait_ids
            return await self.cog._private_err(
                interaction,
                f"Removing that trait would make the build illegal: {validation_error}",
            )

        await interaction.response.edit_message(
            embed=self.cog._build_registration_embed(draft),
            view=OCRegistrationView(self.cog, self.key),
        )


class OCRegistrationView(discord.ui.View):
    def __init__(self, cog: "OCCog", key: tuple[int, int, int]):
        super().__init__(timeout=DRAFT_TIMEOUT_SECONDS)
        self.cog = cog
        self.key = key

        draft = cog._get_registration_draft(key)
        if draft:
            self.add_item(OriginTraitSelect(cog, key, draft))
            self.add_item(TraitTierSelect(cog, key, draft))
            self.add_item(AddTraitSelect(cog, key, draft))
            self.add_item(RemoveTraitSelect(cog, key, draft))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        draft = self.cog._get_registration_draft(self.key)
        if not draft:
            await self.cog._private_err(interaction, "This OC registration draft expired. Please start again.")
            return False

        if int(interaction.user.id) != draft.controller_user_id:
            await self.cog._private_err(
                interaction,
                f"Only **{draft.controller_display_name}** can edit this OC registration draft.",
            )
            return False

        return True

    @discord.ui.button(label="Submit OC", style=discord.ButtonStyle.success, row=4)
    async def submit_oc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._submit_registration(interaction, self.key)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel_registration(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.registration_drafts.pop(self.key, None)

        embed = discord.Embed(
            title="OC Registration Cancelled",
            description="This OC registration draft was cancelled.",
            color=discord.Color.red(),
        )

        await interaction.response.edit_message(embed=embed, view=None)


class OCCog(commands.GroupCog, group_name="oc", group_description="Character (OC) commands"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.registration_drafts: dict[tuple[int, int, int], OCRegistrationDraft] = {}
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def trait_service(self) -> TraitsService:
        return TraitsService(self.sb())

    # -------------------------------------------------------------------------
    # Reply helpers
    # -------------------------------------------------------------------------
    async def _public_ok(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    async def _private_err(self, interaction: discord.Interaction, content: str):
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True)
        return await interaction.response.send_message(content, ephemeral=True)

    # -------------------------------------------------------------------------
    # Shared trait rule helpers
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

    def _selected_trait_summary(self, selected_traits: list[dict[str, Any]]) -> dict[str, int]:
        positive = 0
        negative = 0
        origin = 0

        for trait in selected_traits:
            tier = self._trait_tier(trait)
            cost = int(trait.get("cost") or 0)

            if tier == "origin":
                origin += 1
            elif cost >= 0:
                positive += cost
            else:
                negative += abs(cost)

        limits = self._get_trait_point_limits(selected_traits)
        free_limit = int(limits["free_limit"])
        hard_cap = int(limits["hard_cap"])
        overdraft_limit = min(hard_cap, free_limit + negative)

        return {
            "positive": positive,
            "negative": negative,
            "origin": origin,
            "free_limit": free_limit,
            "hard_cap": hard_cap,
            "overdraft_limit": overdraft_limit,
        }

    def _validate_selected_traits(
        self,
        *,
        selected_traits: list[dict[str, Any]],
    ) -> str | None:
        if not selected_traits:
            return None

        trait_ids = [
            str(t.get("trait_id") or "")
            for t in selected_traits
            if str(t.get("trait_id") or "")
        ]

        if len(trait_ids) != len(set(trait_ids)):
            return "You selected the same trait more than once."

        origin_traits = [
            trait for trait in selected_traits
            if self._trait_tier(trait) == "origin"
        ]
        if len(origin_traits) > 1:
            return "You may only choose one Origin trait."

        seen_groups: dict[str, str] = {}
        for trait in selected_traits:
            group = str(trait.get("exclusive_group") or "").strip().lower()
            if not group:
                continue

            trait_name = self._trait_name(trait)
            if group in seen_groups:
                return f"Trait conflict: `{trait_name}` conflicts with `{seen_groups[group]}`."

            seen_groups[group] = trait_name

        class_path_traits = [
            trait for trait in selected_traits
            if self._trait_keys(trait) & CLASS_PATH_KEYS
        ]
        if len(class_path_traits) > 1:
            first = self._trait_name(class_path_traits[0])
            second = self._trait_name(class_path_traits[1])
            return f"Trait conflict: `{first}` conflicts with `{second}`. A character may only have one class path trait."

        reward_traits = [
            trait for trait in selected_traits
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

        all_keys = self._all_trait_keys(selected_traits)

        for trait in selected_traits:
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

        for trait in selected_traits:
            tier = self._trait_tier(trait)
            cost = int(trait.get("cost") or 0)

            if tier == "origin":
                continue
            if cost >= 0:
                positive += cost
            else:
                negative += abs(cost)

        limits = self._get_trait_point_limits(selected_traits)
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

    def _would_trait_be_legal_for_build(
        self,
        *,
        selected_traits: list[dict[str, Any]],
        candidate_trait: dict[str, Any],
    ) -> bool:
        preview_traits = list(selected_traits) + [candidate_trait]
        return self._validate_selected_traits(selected_traits=preview_traits) is None

    # -------------------------------------------------------------------------
    # Registration draft helpers
    # -------------------------------------------------------------------------
    def _draft_key(
        self,
        *,
        guild_id: int,
        controller_user_id: int,
        owner_user_id: int,
    ) -> tuple[int, int, int]:
        return (guild_id, controller_user_id, owner_user_id)

    def _get_registration_draft(self, key: tuple[int, int, int]) -> OCRegistrationDraft | None:
        return self.registration_drafts.get(key)

    def _clean_sheet_url(self, raw_url: str) -> str | None:
        url = (raw_url or "").strip()
        if not url:
            return None

        if len(url) > 300:
            raise ValueError("The character sheet link is too long. Please use a shorter link.")

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Please provide a valid sheet link starting with `http://` or `https://`.")

        return url

    def _format_sheet_url(self, sheet_url: str | None) -> str:
        if not sheet_url:
            return "Not linked."
        return f"<{sheet_url}>"

    async def _start_registration_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        sheet_url: str,
        target_member: discord.Member | None = None,
    ):
        if not interaction.guild:
            return await self._private_err(interaction, "This command must be used in a server, not DMs.")

        raw = (name or "").strip()
        if not raw:
            return await self._private_err(interaction, "Please provide a valid OC name.")
        if len(raw) > 64:
            return await self._private_err(interaction, "OC name is too long. Max is **64** characters.")
        if not NAME_RE.match(raw):
            return await self._private_err(
                interaction,
                "OC name has invalid characters. Allowed: letters, numbers, spaces, `-`, `_`, and `'`.",
            )

        try:
            clean_sheet_url = self._clean_sheet_url(sheet_url)
        except ValueError as e:
            return await self._private_err(interaction, str(e))

        guild_id = int(interaction.guild.id)
        controller_user_id = int(interaction.user.id)
        controller_display_name = interaction.user.display_name

        owner = target_member or interaction.user
        owner_user_id = int(owner.id)
        owner_display_name = owner.display_name

        if getattr(owner, "bot", False):
            return await self._private_err(interaction, "OCs cannot be registered for bot accounts.")

        try:
            existing = (
                self.sb()
                .table("characters")
                .select("character_id, name")
                .eq("user_id", owner_user_id)
                .execute()
            )
            rows = existing.data or []
            for r in rows:
                if (r.get("name") or "").casefold() == raw.casefold():
                    return await self._private_err(
                        interaction,
                        f"**{owner_display_name}** already has an OC named **{r['name']}**.",
                    )
        except Exception as e:
            print(f"[oc register duplicate check] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error checking existing OCs.")

        key = self._draft_key(
            guild_id=guild_id,
            controller_user_id=controller_user_id,
            owner_user_id=owner_user_id,
        )

        draft = OCRegistrationDraft(
            guild_id=guild_id,
            owner_user_id=owner_user_id,
            owner_display_name=owner_display_name,
            controller_user_id=controller_user_id,
            controller_display_name=controller_display_name,
            name=raw,
            sheet_url=clean_sheet_url,
        )

        self.registration_drafts[key] = draft

        await interaction.response.send_message(
            embed=self._build_registration_embed(draft),
            view=OCRegistrationView(self, key),
            ephemeral=True,
        )

    # -------------------------------------------------------------------------
    # Trait DB helpers
    # -------------------------------------------------------------------------
    def _get_trait_row(self, guild_id: int, trait_id: str) -> dict[str, Any] | None:
        try:
            res = (
                self.sb()
                .table("traits")
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

    def _get_active_trait_rows(
        self,
        guild_id: int,
        *,
        tier_filter: str | None = None,
        exclude_origin: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            res = (
                self.sb()
                .table("traits")
                .select("trait_id,name,slug,tier,cost,is_active,exclusive_group,requirements_json")
                .eq("guild_id", guild_id)
                .eq("is_active", True)
                .order("name")
                .limit(300)
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[oc traits fetch] error: {e}")
            traceback.print_exc()
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            tier = self._trait_tier(row)

            if tier_filter is not None and tier != tier_filter:
                continue

            if exclude_origin and tier == "origin":
                continue

            out.append(row)

        return out

    def _get_draft_selected_traits(self, draft: OCRegistrationDraft) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        if draft.origin_trait_id:
            origin_row = self._get_trait_row(draft.guild_id, draft.origin_trait_id)
            if origin_row:
                rows.append(origin_row)

        for trait_id in draft.trait_ids:
            row = self._get_trait_row(draft.guild_id, trait_id)
            if row:
                rows.append(row)

        return rows

    def _get_candidate_addable_trait_rows(
        self,
        draft: OCRegistrationDraft,
        *,
        tier_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        selected_ids = set(draft.trait_ids)
        if draft.origin_trait_id:
            selected_ids.add(draft.origin_trait_id)

        selected_traits = self._get_draft_selected_traits(draft)

        rows = self._get_active_trait_rows(
            draft.guild_id,
            tier_filter=tier_filter,
            exclude_origin=True,
        )

        out: list[dict[str, Any]] = []
        for row in rows:
            trait_id = str(row.get("trait_id") or "")
            if not trait_id or trait_id in selected_ids:
                continue

            if not self._would_trait_be_legal_for_build(
                selected_traits=selected_traits,
                candidate_trait=row,
            ):
                continue

            out.append(row)

        return out

    def _build_trait_selection_lines(self, selected_traits: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []

        for trait in sorted(
            selected_traits,
            key=lambda t: (
                TIER_ORDER.get(self._trait_tier(t), 999),
                str(t.get("name") or "").lower(),
            ),
        ):
            lines.append(
                f"• **{trait.get('name', 'Trait')}** "
                f"(`{trait.get('tier', '?')}`, {int(trait.get('cost') or 0):+d})"
            )

        return lines

    # -------------------------------------------------------------------------
    # Embed builders
    # -------------------------------------------------------------------------
    def _build_registration_embed(self, draft: OCRegistrationDraft) -> discord.Embed:
        selected_traits = self._get_draft_selected_traits(draft)
        summary = self._selected_trait_summary(selected_traits)

        origin_line = "Not selected."
        if draft.origin_trait_id:
            origin_row = self._get_trait_row(draft.guild_id, draft.origin_trait_id)
            if origin_row:
                origin_line = (
                    f"**{origin_row.get('name', 'Origin')}** "
                    f"(`{origin_row.get('tier', '?')}`, {int(origin_row.get('cost') or 0):+d})"
                )
            else:
                origin_line = "Selected Origin could not be found."

        normal_trait_rows = []
        for trait_id in draft.trait_ids:
            row = self._get_trait_row(draft.guild_id, trait_id)
            if row:
                normal_trait_rows.append(row)

        trait_lines = self._build_trait_selection_lines(normal_trait_rows)
        traits_value = "\n".join(trait_lines) if trait_lines else "No starting traits selected."

        embed = discord.Embed(
            title="OC Registration Draft",
            description=(
                f"**OC Name:** {draft.name}\n"
                f"**Player:** <@{draft.owner_user_id}>\n"
                f"**Sheet:** {self._format_sheet_url(draft.sheet_url)}"
            ),
            color=discord.Color.dark_teal(),
        )

        embed.add_field(
            name="Origin",
            value=origin_line,
            inline=False,
        )

        embed.add_field(
            name=f"Starting Traits ({len(draft.trait_ids)}/{MAX_STARTING_TRAITS})",
            value=traits_value[:1024],
            inline=False,
        )

        embed.add_field(
            name="Trait Summary",
            value=(
                f"**Positive Points:** {summary['positive']}\n"
                f"**Negative Points:** {summary['negative']}\n"
                f"**Origin Traits:** {summary['origin']}\n"
                f"**Free Positive Limit:** {summary['free_limit']}\n"
                f"**Overdraft Limit:** {summary['free_limit']} + {summary['negative']} = {summary['overdraft_limit']}\n"
                f"**Hard Positive Cap:** {summary['hard_cap']}"
            ),
            inline=False,
        )

        validation_error = self._validate_selected_traits(selected_traits=selected_traits)
        if validation_error:
            embed.add_field(
                name="Status",
                value=f"⚠️ {validation_error}",
                inline=False,
            )
            embed.color = discord.Color.orange()
        else:
            embed.add_field(
                name="Status",
                value="✅ Draft is currently legal. Submit when ready.",
                inline=False,
            )

        embed.set_footer(
            text=f"Adding from: {draft.add_trait_tier.title()} traits. Use the category dropdown to change trait type."
        )
        return embed

    def _build_created_embed(
        self,
        *,
        actor: discord.abc.User,
        draft: OCRegistrationDraft,
        selected_traits: list[dict[str, Any]],
        primary: dict[str, Any],
    ) -> discord.Embed:
        summary = self._selected_trait_summary(selected_traits)

        embed = discord.Embed(
            title="OC Created",
            description=(
                f"✅ **{actor.display_name}** registered **{draft.name}** for <@{draft.owner_user_id}>.\n"
                f"**Sheet:** {self._format_sheet_url(draft.sheet_url)}"
            ),
            color=discord.Color.dark_teal(),
        )

        embed.add_field(
            name="Currency",
            value=f"{primary.get('emoji') or ''} **{primary['name']}** (`{primary['ticker']}`)",
            inline=False,
        )

        if selected_traits:
            embed.add_field(
                name="Starting Traits",
                value="\n".join(self._build_trait_selection_lines(selected_traits))[:1024],
                inline=False,
            )
            embed.add_field(
                name="Trait Summary",
                value=(
                    f"**Positive Points:** {summary['positive']}\n"
                    f"**Negative Points:** {summary['negative']}\n"
                    f"**Origin Traits:** {summary['origin']}\n"
                    f"**Free Positive Limit:** {summary['free_limit']}\n"
                    f"**Overdraft Limit:** {summary['free_limit']} + {summary['negative']} = {summary['overdraft_limit']}\n"
                    f"**Hard Positive Cap:** {summary['hard_cap']}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Starting Traits",
                value="No starting traits selected.",
                inline=False,
            )

        return embed

    # -------------------------------------------------------------------------
    # Final registration submit
    # -------------------------------------------------------------------------
    async def _submit_registration(self, interaction: discord.Interaction, key: tuple[int, int, int]):
        draft = self._get_registration_draft(key)
        if not draft:
            return await self._private_err(interaction, "This OC registration draft expired. Please start again.")

        selected_traits = self._get_draft_selected_traits(draft)
        expected_trait_count = len(draft.trait_ids) + (1 if draft.origin_trait_id else 0)

        if len(selected_traits) != expected_trait_count:
            return await self._private_err(
                interaction,
                "One or more selected traits could not be found. Please remove and re-add the missing trait.",
            )

        validation_error = self._validate_selected_traits(selected_traits=selected_traits)
        if validation_error:
            return await self._private_err(interaction, validation_error)

        await interaction.response.defer(ephemeral=True)

        sb = self.sb()

        try:
            sb.table("users").upsert({"user_id": draft.owner_user_id}).execute()

            existing = (
                sb.table("characters")
                .select("character_id, name")
                .eq("user_id", draft.owner_user_id)
                .execute()
            )
            rows = existing.data or []
            for r in rows:
                if (r.get("name") or "").casefold() == draft.name.casefold():
                    return await interaction.followup.send(
                        f"<@{draft.owner_user_id}> already has an OC named **{r['name']}**.",
                        ephemeral=True,
                    )

            payload: dict[str, Any] = {
                "user_id": draft.owner_user_id,
                "name": draft.name,
            }

            if draft.sheet_url:
                payload["sheet_url"] = draft.sheet_url

            ins = sb.table("characters").insert(payload).execute()
            data = ins.data or []
            if not data:
                return await interaction.followup.send(
                    "Could not create OC. Supabase did not return a row.",
                    ephemeral=True,
                )

            char = data[0]
            character_id = str(char["character_id"])

            trait_service = self.trait_service()
            for trait_row in selected_traits:
                trait_service.add_trait_to_character(
                    guild_id=draft.guild_id,
                    character_id=character_id,
                    trait_id=str(trait_row["trait_id"]),
                    approved_by=interaction.user.id,
                    notes="Selected during guided OC registration",
                )

            primary = get_primary_currency(sb, draft.guild_id)
            ensure_wallet(sb, character_id, primary["currency_id"])

        except Exception as e:
            print(f"[oc guided submit] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send(
                "Server error creating OC. If this mentions `sheet_url` or schema cache, run the SQL patch and reload Supabase schema.",
                ephemeral=True,
            )

        self.registration_drafts.pop(key, None)

        submitted_embed = self._build_registration_embed(draft)
        submitted_embed.title = "OC Registration Submitted"
        submitted_embed.color = discord.Color.green()
        submitted_embed.set_footer(text="This draft has been submitted and locked.")

        try:
            await interaction.edit_original_response(embed=submitted_embed, view=None)
        except Exception:
            pass

        public_embed = self._build_created_embed(
            actor=interaction.user,
            draft=draft,
            selected_traits=selected_traits,
            primary=primary,
        )

        await interaction.followup.send(embed=public_embed, ephemeral=False)

    # -------------------------------------------------------------------------
    # /oc create and /oc register
    # -------------------------------------------------------------------------
    @app_commands.command(name="create", description="Start guided OC registration")
    async def create(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await self._private_err(interaction, "This command must be used in a server, not DMs.")

        await interaction.response.send_modal(OCRegistrationModal(self))

    @app_commands.command(name="register", description="Start guided OC registration")
    async def register(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await self._private_err(interaction, "This command must be used in a server, not DMs.")

        await interaction.response.send_modal(OCRegistrationModal(self))

    @app_commands.command(name="staff_register", description="Staff: register an OC for another player")
    @app_commands.describe(player="The player who owns this OC")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def staff_register(self, interaction: discord.Interaction, player: discord.Member):
        if not interaction.guild:
            return await self._private_err(interaction, "This command must be used in a server, not DMs.")

        if player.bot:
            return await self._private_err(interaction, "OCs cannot be registered for bot accounts.")

        await interaction.response.send_modal(
            OCRegistrationModal(
                self,
                target_member=player,
            )
        )

    @staff_register.error
    async def staff_register_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            return await self._private_err(interaction, "You need **Manage Server** permission to use this command.")

        print(f"[oc staff_register error] {error}")
        traceback.print_exception(type(error), error, error.__traceback__)
        return await self._private_err(interaction, "Something went wrong with `/oc staff_register`.")

    # -------------------------------------------------------------------------
    # /oc list
    # -------------------------------------------------------------------------
    @app_commands.command(name="list", description="List your OCs")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        user_id = int(interaction.user.id)
        sb = self.sb()

        try:
            res = (
                sb.table("characters")
                .select("name, is_active, created_at, sheet_url")
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[oc list] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error fetching your OCs.")

        if not rows:
            return await self._private_err(interaction, "You don’t have any OCs yet. Use `/oc create`.")

        lines = []
        for r in rows:
            active_tag = " ⭐" if r.get("is_active") else ""
            sheet_url = r.get("sheet_url")
            sheet_tag = f" — <{sheet_url}>" if sheet_url else ""
            lines.append(f"- **{r['name']}**{active_tag}{sheet_tag}")

        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s OCs",
            description="\n".join(lines)[:4096],
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="⭐ = active OC")

        return await self._public_ok(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # /oc view
    # -------------------------------------------------------------------------
    @app_commands.command(name="view", description="View one of your OCs")
    @app_commands.describe(name="The OC name to view")
    @app_commands.autocomplete(name=oc_name_autocomplete)
    async def view(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        user_id = int(interaction.user.id)
        raw = (name or "").strip()
        if not raw:
            return await self._private_err(interaction, "Please provide an OC name.")

        try:
            res = (
                self.sb()
                .table("characters")
                .select("character_id, name, is_active, created_at, sheet_url")
                .eq("user_id", user_id)
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[oc view] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error fetching your OC.")

        matches = [r for r in rows if (r.get("name") or "").casefold() == raw.casefold()]
        if not matches:
            return await self._private_err(interaction, "OC not found. Use `/oc list` to see your OCs.")

        oc = matches[0]

        embed = discord.Embed(
            title=str(oc.get("name") or "OC"),
            description=(
                f"**Player:** {interaction.user.mention}\n"
                f"**Active:** {'Yes ⭐' if oc.get('is_active') else 'No'}\n"
                f"**Sheet:** {self._format_sheet_url(oc.get('sheet_url'))}"
            ),
            color=discord.Color.dark_teal(),
        )

        return await self._public_ok(interaction, embed=embed)

    # -------------------------------------------------------------------------
    # /oc select
    # -------------------------------------------------------------------------
    @app_commands.command(name="select", description="Set one of your OCs as active by name")
    @app_commands.describe(name="The OC name to set active")
    @app_commands.autocomplete(name=oc_name_autocomplete)
    async def select(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        user_id = int(interaction.user.id)
        sb = self.sb()

        raw = (name or "").strip()
        if not raw:
            return await self._private_err(interaction, "Please provide an OC name.")

        try:
            res = (
                sb.table("characters")
                .select("character_id, name")
                .eq("user_id", user_id)
                .execute()
            )
            rows = res.data or []
            if not rows:
                return await self._private_err(interaction, "You don’t have any OCs yet. Use `/oc create`.")

            matches = [r for r in rows if (r.get("name") or "").casefold() == raw.casefold()]

            if not matches:
                suggestions = [
                    r["name"]
                    for r in rows
                    if (r.get("name") or "").casefold().startswith(raw.casefold())
                ][:5]

                if suggestions:
                    return await self._private_err(
                        interaction,
                        "I couldn’t find that OC name. Did you mean:\n- " + "\n- ".join(suggestions),
                    )

                return await self._private_err(interaction, "OC not found. Use `/oc list` to see your OCs.")

            chosen = matches[0]
            cid = chosen["character_id"]
            oc_name = chosen["name"]

            sb.table("characters").update({"is_active": False}).eq("user_id", user_id).execute()
            sb.table("characters").update({"is_active": True}).eq("character_id", cid).execute()

        except Exception as e:
            print(f"[oc select] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error selecting OC.")

        return await self._public_ok(
            interaction,
            content=f"✅ **{interaction.user.display_name}** set active OC to **{oc_name}**.",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OCCog(bot))